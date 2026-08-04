import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MathText } from "@/lib/katex";

describe("MathText formula rendering across non-Markdown screens", () => {
  it("does not let Chinese double-dollar noise consume the real formula", () => {
    const source = "$$ 就是两个重要极限之一。例子：$$ \\lim_{x \\to 0} \\frac{\\sin x}{x}$$ 代入";
    const { container } = render(<MathText text={source} />);

    expect(container.querySelectorAll(".katex")).toHaveLength(1);
    expect(container.querySelector(".katex-error")).not.toBeInTheDocument();
    expect(container.textContent).toContain("就是两个重要极限之一。例子：");
    expect(container.textContent).toContain("代入");
    expect(container.textContent).not.toContain("$$");
    // KaTeX 的无障碍 MathML annotation 会保留 TeX 源码，但可见 HTML 不会显示源码。
    expect(container.querySelector(".katex-html")?.textContent).not.toContain("\\lim");
  });

  it("renders bare mixed TeX and keeps malformed content readable", () => {
    const { container } = render(
      <MathText text={"当 (\\infty/\\infty) 时使用极限；无效 $\\notARealCommand{x}$"} />,
    );

    expect(container.querySelectorAll(".katex")).toHaveLength(1);
    expect(container.querySelector(".katex-error")).not.toBeInTheDocument();
    expect(container.textContent).toContain("无效 $\\notARealCommand{x}$");
  });

  it("wraps long inline formulas in a local horizontal scroller", () => {
    const { container } = render(<MathText text={"结果 $\\frac{a_1+a_2+a_3}{b_1+b_2+b_3}$"} />);
    const formulaWrapper = container.querySelector(".katex")?.parentElement;

    expect(formulaWrapper).toHaveClass("max-w-full", "overflow-x-auto");
  });

  it("renders a bare TeX suffix after a Chinese label", () => {
    const { container } = render(
      <MathText text={"选项：\\sum_{n=1}^{\\infty} \\frac{1}{n^2}"} />,
    );

    expect(container.querySelectorAll(".katex")).toHaveLength(1);
    expect(container.querySelector(".katex-error")).not.toBeInTheDocument();
    expect(container.querySelector(".katex-html")?.textContent).not.toContain("\\sum");
  });
});
