import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TutorEntrypoints } from "@/components/tutor/tutor-entrypoints";
import type { TutorEntrypointsOut } from "@/lib/types";

const ready: TutorEntrypointsOut = {
  today_task: { status: "ready", plan_id: "plan-1", task_key: "stage0.day0.task0" },
  errors: { status: "ready", error_ids: ["error-1", "error-2"] },
};

describe("Tutor entrypoints", () => {
  it("renders only server-provided safe identifiers", () => {
    render(<TutorEntrypoints data={ready} onChooseNew={() => {}} />);

    expect(screen.getByRole("link", { name: "继续今天的任务" })).toHaveAttribute(
      "href",
      "/tutor?plan_id=plan-1&task_key=stage0.day0.task0",
    );
    expect(screen.getByRole("link", { name: "错题 1" })).toHaveAttribute(
      "href",
      "/tutor?error_id=error-1",
    );
    expect(screen.getByRole("link", { name: "错题 2" })).toHaveAttribute(
      "href",
      "/tutor?error_id=error-2",
    );
    expect(document.body.textContent).not.toContain("题干");
    expect(document.body.textContent).not.toContain("答案");
  });

  it("shows explicit empty states and keeps the new-question entry", () => {
    const onChooseNew = vi.fn();
    const data: TutorEntrypointsOut = {
      today_task: { status: "empty", reason: "NO_PENDING_TODAY_TASK" },
      errors: { status: "empty", reason: "NO_AVAILABLE_ERRORS", error_ids: [] },
    };
    render(<TutorEntrypoints data={data} onChooseNew={onChooseNew} />);

    expect(screen.getByText("今天没有待完成的辅导任务")).toBeInTheDocument();
    expect(screen.getByText("还没有可用错题")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "粘贴或上传新题目" }));
    expect(onChooseNew).toHaveBeenCalledOnce();
  });
});
