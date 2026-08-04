import { describe, expect, it } from "vitest";

import { normalizeMathDisplayText, splitBareInlineMath } from "@/lib/katex";

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
});
