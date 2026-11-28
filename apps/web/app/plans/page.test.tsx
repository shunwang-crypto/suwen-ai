import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { readPendingPlan, writePendingPlan } from "@/components/plans/plan-parts";
import { apiJson } from "@/lib/api";
import { streamSse } from "@/lib/sse";
import type { GraphInterruptData } from "@/lib/types";
import PlansPage from "./page";

const router = { push: vi.fn(), replace: vi.fn() };
const params = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => params,
}));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ user: { id: "learner" } }) }));
vi.mock("@/lib/api", async (load) => ({ ...await load<typeof import("@/lib/api")>(), apiJson: vi.fn() }));
vi.mock("@/lib/sse", () => ({ streamSse: vi.fn() }));

it("keeps rejected edits after refresh and submits corrections to the original thread", async () => {
  const thread = "plan:learner:local";
  const interrupt: GraphInterruptData = {
    kind: "plan_confirm", thread_id: thread, resume_url: `/api/threads/${thread}/resume`,
    plan: { stages: [{ name: "练习", days: [{ day: 1, tasks: [{
      task_id: "t1", title: "原任务", minutes: 10, kc_names: ["换算"], status: "pending",
    }] }] }] },
  };
  writePendingPlan("learner", {
    thread_id: thread, status: "await_confirm", startedAt: Date.now(), interrupt,
    form: { goal: "单位换算", currentLevel: "", targetOutcome: "", successCriteria: "",
      planMode: "goal_only", kbId: "", hours: 0.5, totalDays: 1 },
  });
  let completed = false;
  vi.mocked(apiJson).mockImplementation(async (path) => path.endsWith("/agents")
    ? { status: completed ? "completed" : "awaiting_confirmation", pending: interrupt, events: [] } : []);
  vi.mocked(streamSse)
    .mockImplementationOnce(async function* () {
      yield { event: "error", data: { code: "PLAN_EDIT_INVALID", message: "第 1 天超出学习时长上限。" } };
    })
    .mockImplementationOnce(async function* () {
      completed = true;
      yield { event: "done", data: { thread_id: thread, result: { plan_id: "saved" } } };
    });

  const view = render(<PlansPage />);
  const title = () => screen.getByRole("textbox", { name: "第 1 天任务 1 标题" });
  const minutes = () => screen.getByRole("spinbutton", { name: /第 1 天任务 1/ });
  await screen.findByDisplayValue("原任务");
  fireEvent.change(title(), { target: { value: "我的独立练习" } });
  fireEvent.change(minutes(), { target: { value: "45" } });
  fireEvent.click(screen.getByRole("button", { name: "确认并保存修改后的计划" }));
  expect(await screen.findByText("第 1 天超出学习时长上限。")).toBeInTheDocument();
  expect(minutes()).toHaveValue(45);
  expect(title()).toHaveValue("我的独立练习");
  expect(readPendingPlan("learner")?.status).toBe("await_confirm");
  view.unmount();

  render(<PlansPage />);
  await screen.findByDisplayValue("我的独立练习");
  expect(minutes()).toHaveValue(45);
  fireEvent.click(screen.getByRole("button", { name: "恢复原草案" }));
  expect(title()).toHaveValue("原任务");
  expect(minutes()).toHaveValue(10);
  fireEvent.change(minutes(), { target: { value: "25" } });
  fireEvent.click(screen.getByRole("button", { name: "确认并保存修改后的计划" }));
  await waitFor(() => expect(readPendingPlan("learner")).toBeNull());
  const sent = vi.mocked(streamSse).mock.calls[1];
  expect(sent[0]).toBe(interrupt.resume_url);
  expect(sent[1]).toMatchObject({ value: { approved: true, plan: { stages: [
    { days: [{ tasks: [{ title: "原任务", minutes: 25 }] }] },
  ] } } });
});
