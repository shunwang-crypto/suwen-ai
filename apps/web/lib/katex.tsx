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

const BARE_MATH_COMMAND_RE = /\\(?:lim|frac|sum|int|sqrt|left|right|sin|cos|tan|log|ln|infty|pi|cdot|times|leq|geq|neq|pm|to)\b/;
const CJK_RE = /[\u3400-\u9fff]/;
const PROTECTED_MARKDOWN_TOKEN_RE = /\u0000MATH-PROTECTED-(\d+)\u0000/g;

export type BareInlineMathSegment =
  | { type: "text"; content: string }
  | { type: "math"; content: string };

function isBareInlineMathCandidate(value: string): boolean {
  const candidate = value.trim();
  if (
    !candidate
    || candidate.length > 300
    || candidate.includes("\n")
    || candidate.includes("$")
    || candidate.includes("`")
    || CJK_RE.test(candidate)
    || !BARE_MATH_COMMAND_RE.test(candidate)
  ) {
    return false;
  }

  // 去掉 TeX 命令后若仍有较长英文单词，通常是自然语言说明，不自动转公式。
  const withoutCommands = candidate.replace(/\\[A-Za-z]+/g, "");
  return !/[A-Za-z]{3,}/.test(withoutCommands);
}

function looksLikeDelimitedMath(value: string): boolean {
  const candidate = value.trim();
  if (!candidate || candidate.length > 2000 || CJK_RE.test(candidate)) return false;
  const withoutCommands = candidate.replace(/\\[A-Za-z]+/g, "");
  if (/[A-Za-z]{4,}/.test(withoutCommands)) return false;
  return BARE_MATH_COMMAND_RE.test(candidate) || /[=^_{}<>+*/]/.test(candidate);
}

function protectMarkdownSyntax(text: string): { text: string; protectedParts: string[] } {
  const protectedParts: string[] = [];
  const save = (value: string) => {
    const token = `\u0000MATH-PROTECTED-${protectedParts.length}\u0000`;
    protectedParts.push(value);
    return token;
  };

  // 公式兼容只处理普通正文。代码块、行内代码和链接整体保持原始 Markdown。
  let protectedText = text.replace(
    /^( {0,3})(`{3,}|~{3,})[^\n]*(?:\n[\s\S]*?^\1\2[ \t]*(?=\n|$)|$)/gm,
    (match) => save(match),
  );
  protectedText = protectedText.replace(/(`+)([^`]*?)\1/g, (match) => save(match));
  protectedText = protectedText.replace(
    /!?\[(?:\\.|[^\]\\\n])*\]\((?:\\.|[^)\\\n])*\)/g,
    (match) => save(match),
  );
  return { text: protectedText, protectedParts };
}

/**
 * 在 Markdown 解析前修正常见的模型公式定界符漂移。
 * 只改变展示字符串，不回写消息；受保护的代码和链接不会被改写。
 */
export function normalizeMarkdownMath(text: string): string {
  const source = String(text ?? "");
  const protectedSyntax = protectMarkdownSyntax(source);
  let normalized = protectedSyntax.text
    // CommonMark 会先吃掉反斜杠，因此必须在 Markdown parse 之前转换 TeX 定界符。
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, expr: string) => `\n\n$$\n${expr.trim()}\n$$\n\n`)
    .replace(/\\\(([^\n]*?)\\\)/g, (_match, expr: string) => `$${expr.trim()}$`);

  // 修复模型偶发的 `$公式$$` / `$$公式$`，避免后面的中文被吞进一个红色 KaTeX error。
  normalized = normalized
    .replace(/(?<!\$)\$([^$\n]{1,2000}?)\$\$(?!\$)/g, (match, expr: string) => (
      looksLikeDelimitedMath(expr) ? `$${expr.trim()}$` : match
    ))
    .replace(/(?<!\$)\$\$([^$\n]{1,2000}?)\$(?!\$)/g, (match, expr: string) => (
      looksLikeDelimitedMath(expr) ? `$${expr.trim()}$` : match
    ));

  // `$$...$$` 与正文同行时按行内公式处理；独占一行的块公式保留 display 语义。
  normalized = normalized.replace(
    /(?<!\$)\$\$([^$\n]{1,2000}?)\$\$(?!\$)/g,
    (match, expr: string, offset: number, whole: string) => {
      if (!looksLikeDelimitedMath(expr)) return match;
      const lineStart = whole.lastIndexOf("\n", offset - 1) + 1;
      const nextLineBreak = whole.indexOf("\n", offset + match.length);
      const lineEnd = nextLineBreak === -1 ? whole.length : nextLineBreak;
      const before = whole.slice(lineStart, offset);
      const after = whole.slice(offset + match.length, lineEnd);
      return before.trim() || after.trim()
        ? `$${expr.trim()}$`
        : `$$\n${expr.trim()}\n$$`;
    },
  );

  return normalized.replace(PROTECTED_MARKDOWN_TOKEN_RE, (_match, index: string) => (
    protectedSyntax.protectedParts[Number(index)] ?? ""
  ));
}

/**
 * 识别 Markdown 普通文本节点里括号包裹、但漏写 `$` 定界符的 TeX。
 * 调用方应只把普通 text 节点传入；代码、链接和已解析公式由 Markdown AST 跳过。
 */
export function splitBareInlineMath(text: string): BareInlineMathSegment[] {
  const segments: BareInlineMathSegment[] = [];
  let plainStart = 0;
  let i = 0;
  let mathDelimiter: 0 | 1 | 2 = 0;

  while (i < text.length) {
    if (text[i] === "$" && text[i - 1] !== "\\") {
      const delimiter = text[i + 1] === "$" ? 2 : 1;
      if (mathDelimiter === 0) mathDelimiter = delimiter;
      else if (mathDelimiter === delimiter) mathDelimiter = 0;
      i += delimiter;
      continue;
    }
    const opening = text[i];
    if (mathDelimiter !== 0 || (opening !== "(" && opening !== "（")) {
      i += 1;
      continue;
    }
    const closing = opening === "(" ? ")" : "）";
    let depth = 1;
    let end = i + 1;
    while (end < text.length && text[end] !== "\n") {
      if (text[end] === opening) depth += 1;
      if (text[end] === closing) {
        depth -= 1;
        if (depth === 0) break;
      }
      end += 1;
    }
    if (depth !== 0) {
      i += 1;
      continue;
    }

    const candidate = text.slice(i + 1, end);
    if (!isBareInlineMathCandidate(candidate)) {
      i += 1;
      continue;
    }
    if (i + 1 > plainStart) {
      segments.push({ type: "text", content: text.slice(plainStart, i + 1) });
    }
    segments.push({ type: "math", content: candidate.trim() });
    plainStart = end;
    i = end + 1;
  }

  if (plainStart < text.length) {
    segments.push({ type: "text", content: text.slice(plainStart) });
  }
  return segments.length > 0 ? segments : [{ type: "text", content: text }];
}

/**
 * 仅用于展示的兼容层：把常见的 TeX 定界符统一成 MathText 能识别的形式，
 * 并对明显是单个公式、但历史数据漏掉定界符的字段做保守的块级包装。
 * 这里绝不改写 API payload 或数据库字段。
 */
export function normalizeMathDisplayText(text: string): string {
  const value = String(text ?? "");
  const delimited = value
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, expr: string) => `$$${expr}$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, expr: string) => `$${expr}$`);
  const trimmed = delimited.trim();
  if (!trimmed || /\$\$|(?<!\\)\$/.test(trimmed) || !BARE_MATH_COMMAND_RE.test(trimmed)) {
    return delimited;
  }
  // 中文题干、自然语言句子和说明文字不能整体包成公式。
  if (CJK_RE.test(trimmed) || /\b(?:the|this|find|calculate|solve|answer|where|let)\b/i.test(trimmed)) {
    return delimited;
  }
  // 要求字段从 TeX/数学 token 开始，避免把“说明文字 + 公式”误判为单一公式。
  if (!/^(?:\\[a-zA-Z]+|[A-Za-z0-9({[|+\-])/.test(trimmed)) return delimited;
  return `$$${trimmed}$$`;
}

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
  return tokenizeMath(normalizeMathDisplayText(text))
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
  const segments = tokenizeMath(normalizeMathDisplayText(text));
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
