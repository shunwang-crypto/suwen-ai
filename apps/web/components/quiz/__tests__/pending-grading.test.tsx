import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PendingGrading } from "@/components/quiz/pending-grading";

describe("pending grading recovery", () => {
  it("offers regrading saved answers and explains that evidence is unchanged", () => {
    const retry = vi.fn();
    render(<PendingGrading count={2} retryAvailable busy={false} onRetry={retry} />);
    expect(screen.getByRole("status")).toHaveTextContent("2 题待评分");
    expect(screen.getByRole("status")).toHaveTextContent("不更新掌握度、错题或复习进度");
    fireEvent.click(screen.getByRole("button", { name: "重新评分已保存的答案" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("blocks another retry while scoring and after the limit", () => {
    const retry = vi.fn();
    const { rerender } = render(<PendingGrading retryAvailable busy onRetry={retry} />);
    expect(screen.getByRole("button")).toBeDisabled();
    fireEvent.click(screen.getByRole("button"));
    expect(retry).not.toHaveBeenCalled();
    rerender(<PendingGrading retryAvailable={false} busy={false} onRetry={retry} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("已保存答案供后续复核");
  });
});
