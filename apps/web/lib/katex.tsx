"use client";

// 数学渲染工具：行内 $..$ 与块级 $$..$$ -> katex.renderToString。
// 无效表达式原样输出不崩；流式场景按契约 §5.7 「完整公式才渲染，半截公式缓冲」。

import katex from "katex";
import { Fragment, type ReactNode } from "react";

export type MathSegment =
  | { type: "text"; content: string }
  | { type: "math"; content: string; display: boolean }
  /** 流式未闭合的公式尾巴（含起始定界符），等待后续 token 补全 */
  | { type: "pending"; content: string };

/**
 * 把文本切成 文本/公式/未闭合尾巴 三类片段。
 * - `$$...$$` 块级；`$...$` 行内（内部不允许换行，视作普通美元符）。
 * - `\$` 转义为普通字符。
 * - 末尾未闭合的定界符 -> pending（流式缓冲；终态当纯文本）。
 */
export function tokenizeMath(text: string): MathSegment[] {
  const segments: MathSegment[] = [];
  let plain = "";
  let i = 0;

  const flushPlain = () => {
    if (plain) {
      segments.push({ type: "text", content: plain });
      plain = "";
    }
  };

  while (i < text.length) {
    const ch = text[i];
    if (ch === "\\" && text[i + 1] === "$") {
      plain += "$";
      i += 2;
      continue;
    }
    if (ch !== "$") {
      plain += ch;
      i += 1;
      continue;
    }
    // 块级 $$...$$
    if (text.startsWith("$$", i)) {
      const close = text.indexOf("$$", i + 2);
      if (close === -1) {
        flushPlain();
        segments.push({ type: "pending", content: text.slice(i) });
        return segments;
      }
      const expr = text.slice(i + 2, close);
      flushPlain();
      segments.push({ type: "math", content: expr, display: true });
      i = close + 2;
      continue;
    }
    // 行内 $...$
    let close = -1;
    for (let j = i + 1; j < text.length; j += 1) {
      if (text[j] === "$" && text[j - 1] !== "\\") {
        close = j;
        break;
      }
      if (text[j] === "\n") break; // 行内公式不跨行
    }
    if (close === -1) {
      const rest = text.slice(i);
      // 尾部含换行说明只是普通 $（如价格），不当作待补全公式
      if (rest.includes("\n")) {
        plain += "$";
        i += 1;
        continue;
      }
      flushPlain();
      segments.push({ type: "pending", content: rest });
      return segments;
    }
    const expr = text.slice(i + 1, close);
    if (!expr.trim()) {
      plain += text.slice(i, close + 1); // 空表达式原样输出
      i = close + 1;
      continue;
    }
    flushPlain();
    segments.push({ type: "math", content: expr, display: false });
    i = close + 1;
  }
  flushPlain();
  return segments;
}

/** 渲染单个公式为 HTML；解析失败返回 null（调用方原样输出）。 */
function tryRenderKatex(expr: string, display: boolean): string | null {
  try {
    return katex.renderToString(expr, {
      displayMode: display,
      throwOnError: true,
      strict: false,
    });
  } catch {
    return null;
  }
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * 工具函数：整段文本 -> HTML 字符串（文本部分已转义）。
 * 无效公式带定界符原样输出，绝不抛异常。
 */
export function renderMathInText(text: string): string {
  return tokenizeMath(text)
    .map((seg) => {
      if (seg.type === "text" || seg.type === "pending") {
        return escapeHtml(seg.content);
      }
      const html = tryRenderKatex(seg.content, seg.display);
      if (html === null) {
        const delim = seg.display ? "$$" : "$";
        return escapeHtml(`${delim}${seg.content}${delim}`);
      }
      return seg.display
        ? `<div class="overflow-x-auto py-1">${html}</div>`
        : html;
    })
    .join("");
}

const CITATION_RE = /\[(\d{1,3})\]/g;

/** 文本片段内渲染 [N] 引用标记（可点击、可高亮）。 */
function renderTextWithCitations(
  content: string,
  keyPrefix: string,
  onCitationClick?: (idx: number) => void,
  activeCitation?: number | null,
): ReactNode {
  if (!onCitationClick) return content;
  const nodes: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  CITATION_RE.lastIndex = 0;
  while ((m = CITATION_RE.exec(content)) !== null) {
    if (m.index > last) nodes.push(content.slice(last, m.index));
    const idx = Number(m[1]);
    const active = activeCitation === idx;
    nodes.push(
      <button
        key={`${keyPrefix}-c${m.index}`}
        type="button"
        onClick={() => onCitationClick(idx)}
        className={`mx-0.5 inline-flex h-4.5 min-w-4.5 items-center justify-center rounded px-1 align-text-top text-xs font-semibold transition-colors ${
          active
            ? "bg-indigo-600 text-white"
            : "bg-indigo-100 text-indigo-700 hover:bg-indigo-200"
        }`}
        aria-label={`查看引用 ${idx}`}
      >
        {idx}
      </button>,
    );
    last = m.index + m[0].length;
  }
  if (nodes.length === 0) return content;
  if (last < content.length) nodes.push(content.slice(last));
  return nodes;
}

export interface MathTextProps {
  text: string;
  /** 流式中：未闭合公式以「缓冲中」弱化样式显示原文，完整后才渲染 */
  streaming?: boolean;
  /** 提供后，文本中的 [N] 渲染为可点击引用标记 */
  onCitationClick?: (idx: number) => void;
  activeCitation?: number | null;
  className?: string;
}

/** 含数学公式的文本渲染组件（whitespace-pre-wrap 保留换行）。 */
export function MathText({
  text,
  streaming = false,
  onCitationClick,
  activeCitation,
  className,
}: MathTextProps) {
  const segments = tokenizeMath(text);
  return (
    <span className={`whitespace-pre-wrap break-words ${className ?? ""}`}>
      {segments.map((seg, i) => {
        const key = `s${i}`;
        if (seg.type === "text") {
          return (
            <Fragment key={key}>
              {renderTextWithCitations(
                seg.content,
                key,
                onCitationClick,
                activeCitation,
              )}
            </Fragment>
          );
        }
        if (seg.type === "pending") {
          // 半截公式：流式时弱化显示等待补全；终态按原文输出
          return (
            <span key={key} className={streaming ? "text-slate-400" : undefined}>
              {seg.content}
            </span>
          );
        }
        const html = tryRenderKatex(seg.content, seg.display);
        if (html === null) {
          const delim = seg.display ? "$$" : "$";
          return <span key={key}>{`${delim}${seg.content}${delim}`}</span>;
        }
        return seg.display ? (
          <span
            key={key}
            className="block overflow-x-auto py-1"
            dangerouslySetInnerHTML={{ __html: html }}
          />
        ) : (
          <span key={key} dangerouslySetInnerHTML={{ __html: html }} />
        );
      })}
    </span>
  );
}
