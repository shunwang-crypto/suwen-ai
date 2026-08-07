import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { PredictionSlider } from "@/components/quiz/prediction-slider";

function ControlledSlider() {
  const [value, setValue] = useState<number | undefined>();
  return <PredictionSlider questionNumber={2} value={value} onChange={setValue} />;
}

describe("PredictionSlider", () => {
  it("is optional, exposes an accessible five-point range, and can be cleared", () => {
    render(<ControlledSlider />);
    const range = screen.getByRole("slider", { name: "第 2 题作答前信心预测" });

    expect(range).toHaveAttribute("min", "1");
    expect(range).toHaveAttribute("max", "5");
    expect(range).toHaveAttribute("aria-valuetext", "未预测");
    expect(screen.getByText("可跳过")).toBeInTheDocument();

    fireEvent.change(range, { target: { value: "4" } });
    expect(range).toHaveValue("4");
    expect(range).toHaveAttribute("aria-valuetext", "比较有把握");

    fireEvent.click(screen.getByRole("button", { name: "清除预测" }));
    expect(range).toHaveAttribute("aria-valuetext", "未预测");
  });

  it("does not change while disabled", () => {
    const onChange = vi.fn();
    render(
      <PredictionSlider questionNumber={1} value={3} disabled onChange={onChange} />,
    );
    fireEvent.change(screen.getByRole("slider"), { target: { value: "5" } });
    expect(onChange).not.toHaveBeenCalled();
  });
});
