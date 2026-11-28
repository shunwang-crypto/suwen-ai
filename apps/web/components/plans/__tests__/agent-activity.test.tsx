import { act, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { AgentActivity, PlanReviewCard, SavedAgentActivity } from "@/components/plans/agent-activity";
import { apiJson } from "@/lib/api";
import type { FlowItem } from "@/components/flow-timeline";

vi.mock("@/lib/api", () => ({ apiJson: vi.fn() }));
beforeEach(() => vi.mocked(apiJson).mockReset());

it("shows the latest actual collaborator status and actionable review", () => {
  const items = ["queued", "started", "completed"].map((event) => ({ kind: "trace", data: {
    type: "agent", node: "agent_collaboration", event, task_id: "research", agent_name: "research-1", agent_role: "researcher", summary: "资料已核对",
  } })) as FlowItem[];
  render(<><AgentActivity items={items} /><PlanReviewCard review={{ approved: false, summary: "需修订", checks: { prerequisites: "fail" }, issues: [{ code: "ORDER", message: "练习早于教学", suggestion: "先学会概念再练习", task_keys: [] }] }} /></>);
  expect(screen.getByText("资料研究")).toBeInTheDocument();
  expect(screen.getByText("已完成")).toBeInTheDocument();
  expect(screen.queryByText("等待中")).not.toBeInTheDocument();
  expect(screen.getByText("先学后练：需修改")).toBeInTheDocument();
  expect(screen.getByText("先学会概念再练习")).toBeInTheDocument();
});

it("does not display a previous plan's review while another plan loads", async () => {
  let resolve!: (value: unknown) => void;
  vi.mocked(apiJson).mockResolvedValueOnce({ events: [], review: { approved: true, summary: "计划 A 审查" } });
  const view = render(<SavedAgentActivity planId="a" />);
  await screen.findByText("计划 A 审查");
  vi.mocked(apiJson).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
  view.rerender(<SavedAgentActivity planId="b" />);
  expect(screen.queryByText("计划 A 审查")).not.toBeInTheDocument();
  await act(async () => resolve({ events: [], review: { approved: true, summary: "计划 B 审查" } }));
  expect(screen.getByText("计划 B 审查")).toBeInTheDocument();
});
