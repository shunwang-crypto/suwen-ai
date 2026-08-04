"use client";

import type { ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

import { normalizeMarkdownMath, splitBareInlineMath } from "@/lib/katex";

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
  data?: Record<string, unknown>;
}

const CITATION_RE = /\[(\d{1,3})\]/g;
const CITATION_HREF_RE = /^#citation-(\d{1,3})$/;
const SKIP_CITATION_NODES = new Set(["code", "inlineCode", "link", "math", "inlineMath"]);
const SKIP_BARE_MATH_NODES = new Set([
  "code",
  "inlineCode",
  "link",
  "linkReference",
  "math",
  "inlineMath",
]);

/** 把正文中 `(\\infty/\\infty)` 这类漏写定界符的局部 TeX 交给现有 KaTeX 链路。 */
function remarkBareInlineMath() {
  return (tree: MarkdownNode) => {
    const walk = (node: MarkdownNode) => {
      if (!node.children || SKIP_BARE_MATH_NODES.has(node.type)) return;
      const children: MarkdownNode[] = [];
      for (const child of node.children) {
        if (child.type !== "text" || !child.value) {
          walk(child);
          children.push(child);
          continue;
        }
        for (const segment of splitBareInlineMath(child.value)) {
          if (segment.type === "text") {
            children.push({ type: "text", value: segment.content });
          } else {
            children.push({
              type: "inlineMath",
              value: segment.content,
              // mdast-util-math normally attaches this while parsing `$...$`.
              // This node is created after parsing, so provide the same HAST bridge explicitly.
              data: {
                hName: "code",
                hProperties: {
                  className: [
                    "language-math",
                    segment.display ? "math-display" : "math-inline",
                  ],
                },
                hChildren: [{ type: "text", value: segment.content }],
              },
            });
          }
        }
      }
      node.children = children;
    };
    walk(tree);
  };
}

/** 把正文里的 [N] 转为内部链接节点，代码、公式和普通链接中的文本保持原样。 */
function remarkCitations() {
  return (tree: MarkdownNode) => {
    const walk = (node: MarkdownNode) => {
      if (!node.children || SKIP_CITATION_NODES.has(node.type)) return;
      const children: MarkdownNode[] = [];
      for (const child of node.children) {
        if (child.type !== "text" || !child.value) {
          walk(child);
          children.push(child);
          continue;
        }
        let last = 0;
        let match: RegExpExecArray | null;
        CITATION_RE.lastIndex = 0;
        while ((match = CITATION_RE.exec(child.value)) !== null) {
          if (match.index > last) {
            children.push({ type: "text", value: child.value.slice(last, match.index) });
          }
          children.push({
            type: "link",
            url: `#citation-${match[1]}`,
            children: [{ type: "text", value: match[0] }],
          });
          last = match.index + match[0].length;
        }
        if (last === 0) {
          children.push(child);
        } else if (last < child.value.length) {
          children.push({ type: "text", value: child.value.slice(last) });
        }
      }
      node.children = children;
    };
    walk(tree);
  };
}

export function MarkdownAnswer({
  text,
  onCitationClick,
  activeCitation,
  citationIndexes = [],
}: {
  text: string;
  onCitationClick?: (idx: number) => void;
  activeCitation?: number | null;
  citationIndexes?: number[];
}) {
  const availableCitations = new Set(citationIndexes);
  const components: Components = {
    h1: ({ children }) => (
      <h1 className="mb-3 mt-6 text-xl font-semibold leading-8 text-ink first:mt-0">
        {children}
      </h1>
    ),
    h2: ({ children }) => (
      <h2 className="mb-2.5 mt-5 text-lg font-semibold leading-7 text-ink first:mt-0">
        {children}
      </h2>
    ),
    h3: ({ children }) => (
      <h3 className="mb-2 mt-4 text-[15px] font-semibold leading-6 text-ink first:mt-0">
        {children}
      </h3>
    ),
    h4: ({ children }) => (
      <h4 className="mb-1.5 mt-3 font-semibold text-ink first:mt-0">{children}</h4>
    ),
    p: ({ children }) => <p className="my-3 first:mt-0 last:mb-0">{children}</p>,
    strong: ({ children }) => <strong className="font-semibold text-ink">{children}</strong>,
    ul: ({ children }) => (
      <ul className="my-3 list-disc space-y-1.5 pl-6 marker:text-ink-muted">{children}</ul>
    ),
    ol: ({ children }) => (
      <ol className="my-3 list-decimal space-y-1.5 pl-6 marker:text-ink-muted">{children}</ol>
    ),
    li: ({ children }) => <li className="pl-1">{children}</li>,
    blockquote: ({ children }) => (
      <blockquote className="my-4 border-l-2 border-line-strong pl-4 text-ink-secondary">
        {children}
      </blockquote>
    ),
    hr: () => <hr className="my-5 border-0 border-t border-line" />,
    pre: ({ children }) => (
      <pre className="my-4 overflow-x-auto rounded-xl bg-[#f4f4f2] p-4 text-[13px] leading-6 text-ink">
        {children}
      </pre>
    ),
    code: ({ className, children }) => {
      const value = String(children).replace(/\n$/, "");
      const block = Boolean(className) || String(children).endsWith("\n");
      return block ? (
        <code className={className}>{value}</code>
      ) : (
        <code className="rounded bg-[#f1f1ef] px-1.5 py-0.5 text-[0.92em] text-ink">
          {children}
        </code>
      );
    },
    table: ({ children }) => (
      <div className="my-4 overflow-x-auto">
        <table className="min-w-full border-collapse text-left text-[13px]">{children}</table>
      </div>
    ),
    th: ({ children }) => (
      <th className="border border-line bg-surface-muted px-3 py-2 font-semibold text-ink">
        {children}
      </th>
    ),
    td: ({ children }) => <td className="border border-line px-3 py-2">{children}</td>,
    a: ({ href, children }) => {
      const citation = href?.match(CITATION_HREF_RE);
      if (citation) {
        const idx = Number(citation[1]);
        if (!availableCitations.has(idx)) return <>{children}</>;
        const active = activeCitation === idx;
        return (
          <button
            type="button"
            onClick={() => onCitationClick?.(idx)}
            className={`mx-0.5 inline-flex h-4.5 min-w-4.5 items-center justify-center rounded px-1 align-text-top text-xs font-semibold transition-colors ${
              active
                ? "bg-indigo-600 text-white"
                : "bg-indigo-100 text-indigo-700 hover:bg-indigo-200"
            }`}
            aria-label={`查看引用 ${idx}`}
          >
            {String(children).replace(/[\[\]]/g, "")}
          </button>
        );
      }
      return (
        <a
          href={href}
          target="_blank"
          rel="noreferrer noopener"
          className="text-brand-700 underline decoration-brand-200 underline-offset-2 hover:decoration-brand-500"
        >
          {children as ReactNode}
        </a>
      );
    },
  };

  return (
    <div className="markdown-answer break-words">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath, remarkBareInlineMath, remarkCitations]}
        rehypePlugins={[[rehypeKatex, { strict: false, throwOnError: false, errorColor: "inherit" }]]}
        components={components}
      >
        {normalizeMarkdownMath(text)}
      </ReactMarkdown>
    </div>
  );
}
