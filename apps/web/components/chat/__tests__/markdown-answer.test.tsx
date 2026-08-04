import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MarkdownAnswer } from "@/components/chat/markdown-answer";

describe("MarkdownAnswer math compatibility", () => {
  it("renders parenthesized bare TeX in natural-language answers", () => {
    const { container } = render(
      <MarkdownAnswer text={"如果极限是 (0/0) 或 (\\infty/\\infty) 这种未定式"} />,
    );

    expect(container.querySelectorAll(".katex")).toHaveLength(1);
    expect(container.querySelector("annotation")).toHaveTextContent("\\infty/\\infty");
    expect(container).toHaveTextContent("0/0");
  });

  it("does not rewrap existing math or interpret inline and fenced code", () => {
    const { container } = render(
      <MarkdownAnswer
        text={[
          "已有 $\\infty$ 公式。",
          "",
          "`(\\infty/\\infty)`",
          "",
          "```tex",
          "(\\infty/\\infty)",
          "```",
        ].join("\n")}
      />,
    );

    expect(container.querySelectorAll(".katex")).toHaveLength(1);
    expect(container.querySelectorAll("code")).toHaveLength(2);
    expect(container.querySelectorAll("code")[0]).toHaveTextContent("(\\infty/\\infty)");
  });

  it("does not interpret TeX-like text inside links", () => {
    const { container } = render(
      <MarkdownAnswer text={"[查看 (\\infty/\\infty)](https://example.com)"} />,
    );

    expect(container.querySelector(".katex")).not.toBeInTheDocument();
    expect(container.querySelector("a")).toHaveTextContent("(\\infty/\\infty)");
  });

  it("renders TeX bracket and paren delimiters instead of showing raw brackets", () => {
    const { container } = render(
      <MarkdownAnswer
        text={"先看：\\[\\lim_{x \\to 0}\\frac{\\sin x}{x}\\]\n再看 \\(x \\to 0\\)。"}
      />,
    );

    expect(container.querySelectorAll(".katex")).toHaveLength(2);
    expect(container.querySelectorAll(".katex-display")).toHaveLength(1);
    expect(container.querySelector(".katex-error")).not.toBeInTheDocument();
  });

  it("recovers inline double dollars and mismatched delimiters without swallowing prose", () => {
    const { container } = render(
      <MarkdownAnswer
        text={[
          "求导：$\\lim_{x \\to 0} \\frac{\\sin x}{x}$$ 这是第一个极限。",
          "另一个是 $$\\lim_{x \\to 0} \\frac{2\\sin x-\\sin 2x}{x-\\sin x}$$，代入 $x=0$。",
        ].join("\n")}
      />,
    );

    expect(container.querySelectorAll(".katex")).toHaveLength(3);
    expect(container.querySelector(".katex-error")).not.toBeInTheDocument();
    expect(container).toHaveTextContent("这是第一个极限");
    expect(container).toHaveTextContent("另一个是");
  });

  it("keeps citations clickable around multiple normalized formulas", () => {
    const { container } = render(
      <MarkdownAnswer
        text={[
          "$$\\lim_{x \\to 0} \\frac{\\sin x}{x}=1$$ 这是第一个重要极限 [1]。",
          "另一个是 $$\\lim_{x \\to 0} \\frac{2\\sin x-\\sin 2x}{x-\\sin x}$$ [2]。",
          "代入 $x=0$ 后仍为 $\\frac{0}{0}$ 型 [3]。",
        ].join("\n")}
        citationIndexes={[1, 2, 3]}
        onCitationClick={() => {}}
      />,
    );

    expect(container.querySelectorAll(".katex")).toHaveLength(4);
    expect(container.querySelector(".katex-error")).not.toBeInTheDocument();
    expect(container.querySelectorAll('button[aria-label^="查看引用"]')).toHaveLength(3);
    expect(container).toHaveTextContent("这是第一个重要极限");
  });
});
