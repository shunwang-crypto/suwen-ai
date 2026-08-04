import { describe, expect, it } from "vitest";

import {
  normalizeMarkdownMath,
  normalizeMathDisplayText,
  renderMathInText,
  splitBareInlineMath,
  tokenizeMathDisplay,
} from "@/lib/katex";

describe("math display normalization", () => {
  it("wraps an obvious bare LaTeX expression for display only", () => {
    expect(normalizeMathDisplayText("\\lim_{n \\to \\infty} \\frac{1}{n^2+1}"))
      .toBe("$$\\lim_{n \\to \\infty} \\frac{1}{n^2+1}$$");
  });

  it("supports TeX paren and bracket delimiters", () => {
    expect(normalizeMathDisplayText("答案是 \\(x^2\\)\n\\[x+y=1\\]"))
      .toBe("答案是 $x^2$\n\n\n$$\nx+y=1\n$$\n\n");
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

  it("uses the strict delimiter repair in every non-Markdown preview", () => {
    const text = "$$ 就是两个重要极限之一。例子：$$ \\lim_{x \\to 0} \\frac{\\sin x}{x}$$ 代入";
    expect(tokenizeMathDisplay(text)).toEqual([
      { type: "text", content: " 就是两个重要极限之一。例子： " },
      { type: "math", content: "\\lim_{x \\to 0} \\frac{\\sin x}{x}", display: false },
      { type: "text", content: " 代入" },
    ]);
  });

  it("renders bare TeX in mixed prose without swallowing Chinese text", () => {
    const segments = tokenizeMathDisplay("当 (\\infty/\\infty) 时\n\\sum_{n=1}^{\\infty} \\frac1{n^2}");
    expect(segments).toEqual([
      { type: "text", content: "当 (" },
      { type: "math", content: "\\infty/\\infty", display: false },
      { type: "text", content: ") 时\n" },
      { type: "math", content: "\\sum_{n=1}^{\\infty} \\frac1{n^2}", display: true },
    ]);
  });

  it("keeps code, links, prices and invalid formulas as safe literal text", () => {
    const text = "`$x$` [公式 $y$](https://example.com/$5) 价格 $5，无效 $\\notARealCommand{x}$";
    const rendered = renderMathInText(text);
    expect(rendered).toContain("`$x$`");
    expect(rendered).toContain("[公式 $y$](https://example.com/$5)");
    expect(rendered).toContain("价格 $5");
    expect(rendered).toContain("无效 $\\notARealCommand{x}$");
    expect(rendered).not.toContain("katex-error");
  });

  it("produces safe native MathML for the teacher print view", () => {
    const rendered = renderMathInText("<script>alert(1)</script> 答案 $x^2$", {
      output: "mathml",
    });
    expect(rendered).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(rendered).toContain("<math");
    expect(rendered).not.toContain("<script>");
    expect(rendered).not.toContain("katex-error");
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

  it("renders a bare TeX suffix after Chinese prose and preserves punctuation", () => {
    expect(splitBareInlineMath("选项：\\sum_{n=1}^{\\infty} \\frac{1}{n^2}"))
      .toEqual([
        { type: "text", content: "选项：" },
        { type: "math", content: "\\sum_{n=1}^{\\infty} \\frac{1}{n^2}" },
      ]);
    expect(splitBareInlineMath("结果为 \\frac{1}{2}。"))
      .toEqual([
        { type: "text", content: "结果为 " },
        { type: "math", content: "\\frac{1}{2}" },
        { type: "text", content: "。" },
      ]);
  });

  it("does not mistake Windows paths for bare trailing formulas", () => {
    expect(splitBareInlineMath("路径 C:\\temp")).toEqual([
      { type: "text", content: "路径 C:\\temp" },
    ]);
    expect(splitBareInlineMath("路径 C:\\alpha")).toEqual([
      { type: "text", content: "路径 C:\\alpha" },
    ]);
    expect(splitBareInlineMath("路径 /tmp/\\alpha")).toEqual([
      { type: "text", content: "路径 /tmp/\\alpha" },
    ]);
  });

  it("allows Chinese inside an explicit TeX text command", () => {
    expect(splitBareInlineMath("结果：\\text{速度}=\\frac{s}{t}"))
      .toEqual([
        { type: "text", content: "结果：" },
        { type: "math", content: "\\text{速度}=\\frac{s}{t}" },
      ]);
    expect(tokenizeMathDisplay("函数 $\\operatorname{正弦}(x)$"))
      .toEqual([
        { type: "text", content: "函数 " },
        { type: "math", content: "\\operatorname{正弦}(x)", display: false },
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

  it("does not let Chinese double-dollar noise consume the following real formula", () => {
    const text = "$$ 就是两个重要极限之一 [1]。另一个例子：$$ \\lim_{x \\to 0} \\frac{\\sin x}{x}$$ 代入";
    expect(normalizeMarkdownMath(text))
      .toBe(" 就是两个重要极限之一 [1]。另一个例子： $\\lim_{x \\to 0} \\frac{\\sin x}{x}$ 代入");
  });

  it("auto-closes a valid final inline formula and escapes an invalid orphan dollar", () => {
    expect(normalizeMarkdownMath("最终是 $\\frac{1}{2}"))
      .toBe("最终是 $\\frac{1}{2}$");
    expect(normalizeMarkdownMath("普通 $中文内容不会进公式"))
      .toBe("普通 \\$中文内容不会进公式");
  });

  it("auto-closes one unambiguous final display formula", () => {
    expect(normalizeMarkdownMath("最终推导：$$\\frac{1}{2}"))
      .toBe("最终推导：\n\n$$\n\\frac{1}{2}\n$$\n");
  });
});
