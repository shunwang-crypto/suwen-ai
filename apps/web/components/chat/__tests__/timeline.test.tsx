import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ProcessTimeline,
  type TimelineItem,
} from "@/components/chat/timeline";

describe("ProcessTimeline", () => {
  it("shows a cache hit as a readable pre-answer status", () => {
    const items: TimelineItem[] = [
      {
        kind: "trace",
        data: { type: "cache", source: "l1", score: 0.98 },
      },
    ];

    render(<ProcessTimeline items={items} streaming />);

    const toggle = screen.getByRole("button", { name: /过程时间线/ });
    expect(toggle).toHaveTextContent("缓存匹配：已命中 L1 缓存");
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("缓存匹配", { selector: "span" })).toBeInTheDocument();
  });

  it("turns retrieval events into a generation progress summary", () => {
    const items: TimelineItem[] = [
      { kind: "node", data: { node: "query_plan", status: "done" } },
      {
        kind: "trace",
        data: { type: "retrieval", returned: 4, queries: 1 },
      },
    ];

    render(<ProcessTimeline items={items} streaming />);

    expect(screen.getByRole("button", { name: /过程时间线/ })).toHaveTextContent(
      "资料已就绪，正在生成回答…",
    );
  });

  it("reports active token generation regardless of the last completed node", () => {
    const items: TimelineItem[] = [
      { kind: "node", data: { node: "retrieve", status: "done" } },
    ];

    render(<ProcessTimeline items={items} streaming answerStarted />);

    expect(screen.getByRole("button", { name: /过程时间线/ })).toHaveTextContent(
      "正在生成回答…",
    );
  });

  it("does not reserve space when no process event exists", () => {
    const { container } = render(<ProcessTimeline items={[]} streaming />);

    expect(container).toBeEmptyDOMElement();
  });
});
