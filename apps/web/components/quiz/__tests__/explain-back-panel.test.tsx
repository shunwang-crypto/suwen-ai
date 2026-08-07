import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ExplainBackPanel } from "@/components/quiz/explain-back-panel";
import { apiJson } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, apiJson: vi.fn() };
});

const mockedApiJson = vi.mocked(apiJson);

describe("ExplainBackPanel", () => {
  beforeEach(() => mockedApiJson.mockReset());

  it("submits the explanation and renders formula feedback through MathText", async () => {
    mockedApiJson.mockResolvedValue({
      id: "eb-1",
      understanding_score: 0.8,
      feedback: "你说明了 $x^2$ 的关键关系。",
      gap: "还需说明 $x \\ne 0$。",
      retry_guidance: "再用一个反例验证。",
      degraded: false,
    });
    const user = userEvent.setup();
    const { container } = render(
      <ExplainBackPanel kcId="kc-1" promptSpanRef="quiz:q:question:1" />,
    );

    await user.type(screen.getByRole("textbox"), "我先移项，再约分。");
    await user.click(screen.getByRole("button", { name: "获取表达反馈" }));

    await waitFor(() => expect(mockedApiJson).toHaveBeenCalledWith(
      "/api/metacognition/explain-back",
      { body: { student_text: "我先移项，再约分。", prompt_span_ref: "quiz:q:question:1", kc_id: "kc-1" } },
    ));
    expect(await screen.findByText("完整度 80%")).toBeInTheDocument();
    expect(container.querySelectorAll(".katex")).toHaveLength(2);
    expect(container.querySelector(".katex-error")).not.toBeInTheDocument();
  });

  it("keeps the text editable and offers retry when automatic analysis degrades", async () => {
    mockedApiJson.mockResolvedValue({
      id: "eb-2",
      understanding_score: null,
      feedback: "暂时无法自动评分，但你的解释已经保留下来。",
      gap: "",
      retry_guidance: "请稍后重试。",
      degraded: true,
    });
    const user = userEvent.setup();
    render(<ExplainBackPanel promptSpanRef="quiz:q:question:2" />);

    const input = screen.getByRole("textbox");
    await user.type(input, "保留这段解释");
    await user.click(screen.getByRole("button", { name: "获取表达反馈" }));

    expect(await screen.findByText("自动分析暂不可用")).toBeInTheDocument();
    expect(input).toHaveValue("保留这段解释");
    expect(screen.getByRole("button", { name: "重新获取反馈" })).toBeInTheDocument();
  });
});
