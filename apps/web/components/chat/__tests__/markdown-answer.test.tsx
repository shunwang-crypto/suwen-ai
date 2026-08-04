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
});
