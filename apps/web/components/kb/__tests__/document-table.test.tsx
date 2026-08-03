import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DocStatusBadge, IngestSteps } from "@/components/kb/document-table";

describe("knowledge document terminal states", () => {
  it("shows the parsing step without treating it as a terminal state", () => {
    render(<IngestSteps status="parsing" />);
    expect(screen.getByRole("img", { name: /文本提取/ })).toBeInTheDocument();
  });

  it("shows the scan recognition step", () => {
    render(<IngestSteps status="recognizing" />);
    expect(screen.getByRole("img", { name: /扫描页识别/ })).toBeInTheDocument();
  });

  it.each(["ready", "enriched", "partial_ready", "failed"])(
    "%s does not render a dynamic step bar",
    (status) => {
      const { container } = render(
        <>
          <DocStatusBadge status={status} />
          <IngestSteps status={status} />
        </>,
      );
      expect(container.querySelector('[role="img"]')).not.toBeInTheDocument();
    },
  );

  it("keeps partial failure readable and animated only for active work", () => {
    render(<DocStatusBadge status="partial_ready" />);
    expect(screen.getByRole("status", { name: "部分页面失败" })).toBeInTheDocument();
    expect(screen.getByRole("status")).not.toHaveAttribute("aria-label", expect.stringContaining("进行中"));

    const { container } = render(<DocStatusBadge status="enriching" />);
    expect(screen.getByRole("status", { name: "知识增强中" })).toBeInTheDocument();
    expect(container.querySelector(".edu-breathe")).toBeInTheDocument();
  });
});
