import { describe, expect, it } from "vitest";

import { normalizeMathDisplayText } from "@/lib/katex";

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
