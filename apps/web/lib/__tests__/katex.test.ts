import { describe, expect, it } from "vitest";

import {
  normalizeMarkdownMath,
  normalizeMathDisplayText,
  splitBareInlineMath,
} from "@/lib/katex";

describe("math display normalization", () => {
  it("wraps an obvious bare LaTeX expression for display only", () => {
    expect(normalizeMathDisplayText("\\lim_{n \\to \\infty} \\frac{1}{n^2+1}"))
      .toBe("$$\\lim_{n \\to \\infty} \\frac{1}{n^2+1}$$");
  });

  it("supports TeX paren and bracket delimiters", () => {
    expect(normalizeMathDisplayText("答案是 \\(x^2\\)\n\\[x+y=1\\]"))
      .toBe("答案是 $x^2$\n$$x+y=1$$");
  });

  it("does not wrap ordinary Chinese question text", () => {
    const text = "求函数 \\lim_{x \\to 0} 的极限，并说明理由";
    expect(normalizeMathDisplayText(text)).toBe(text);
  });

  it("supports arbitrary valid KaTeX commands in non-Markdown previews", () => {
    expect(normalizeMathDisplayText("\\mathbb{R} \\supset \\{\\alpha,\\beta\\}"))
      .toBe("$$\\mathbb{R} \\supset \\{\\alpha,\\beta\\}$$");
    expect(normalizeMathDisplayText("C:\\temp")).toBe("C:\\temp");
  });
});

describe("bare inline math normalization", () => {
  it("extracts a parenthesized TeX expression from Chinese prose", () => {
    expect(splitBareInlineMath("如果极限是 (0/0) 或 (\\infty/\\infty) 这种未定式"))
      .toEqual([
        { type: "text", content: "如果极限是 (0/0) 或 (" },
        { type: "math", content: "\\infty/\\infty" },
        { type: "text", content: ") 这种未定式" },
      ]);
  });

  it("supports nested parentheses inside a formula", () => {
    expect(splitBareInlineMath("计算 (\\lim_{x \\to 0} f(x)) 的值"))
      .toEqual([
        { type: "text", content: "计算 (" },
        { type: "math", content: "\\lim_{x \\to 0} f(x)" },
        { type: "text", content: ") 的值" },
      ]);
  });

  it("leaves existing delimited math and prose-like content unchanged", () => {
    expect(splitBareInlineMath("已有 $(\\infty)$ 公式")).toEqual([
      { type: "text", content: "已有 $(\\infty)$ 公式" },
    ]);
    expect(splitBareInlineMath("说明 (see \\infty above) 即可")).toEqual([
      { type: "text", content: "说明 (see \\infty above) 即可" },
    ]);
  });

  it("recognizes arbitrary valid KaTeX commands and standalone formula lines", () => {
    expect(splitBareInlineMath("集合 (\\mathbb{R} \\setminus \\{0\\}) 中\n\\alpha+\\beta=\\gamma"))
      .toEqual([
        { type: "text", content: "集合 (" },
        { type: "math", content: "\\mathbb{R} \\setminus \\{0\\}" },
        { type: "text", content: ") 中\n" },
        { type: "math", content: "\\alpha+\\beta=\\gamma", display: true },
      ]);
  });
});

describe("Markdown math delimiter normalization", () => {
  it("normalizes TeX paren and bracket delimiters before Markdown parsing", () => {
    expect(normalizeMarkdownMath("行内 \\(x^2\\)\n\\[\\lim_{x \\to 0} x\\]"))
      .toBe("行内 $x^2$\n\n\n$$\n\\lim_{x \\to 0} x\n$$\n\n");
  });

  it("normalizes inline double dollars while preserving standalone display math", () => {
    expect(normalizeMarkdownMath("结论 $$x^2=1$$ 成立\n$$\\frac{1}{2}$$"))
      .toBe("结论 $x^2=1$ 成立\n$$\n\\frac{1}{2}\n$$");
  });

  it("repairs common mismatched single and double dollar delimiters", () => {
    expect(normalizeMarkdownMath("$\\lim_{x \\to 0}x$$ 后文 $$x^2=1$ 结束"))
      .toBe("$\\lim_{x \\to 0}x$ 后文 $x^2=1$ 结束");
  });

  it("does not rewrite fenced code, inline code, or links", () => {
    const text = [
      "`\\[x^2\\]`",
      "",
      "```tex",
      "\\[x^2\\]",
      "```",
      "",
      "[\\[x^2\\]](https://example.com/math)",
    ].join("\n");
    expect(normalizeMarkdownMath(text)).toBe(text);
  });

  it("wraps standalone KaTeX environments but does not double-wrap existing blocks", () => {
    const align = "\\begin{align}x&=1\\\\y&=2\\end{align}";
    expect(normalizeMarkdownMath(align)).toBe(`$$\n${align}\n$$`);
    expect(normalizeMarkdownMath(`$$\n${align}\n$$`)).toBe(`$$\n${align}\n$$`);
  });

  it("does not treat ordinary dollar prices as math", () => {
    expect(normalizeMarkdownMath("价格是 $5，会员价 $3.50。公式 $5$ 保持。"))
      .toBe("价格是 \\$5，会员价 \\$3.50。公式 $5$ 保持。");
  });
});
