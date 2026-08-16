import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { PlanTaskDrawer, type PlanTaskLocation } from "@/components/plans/plan-task-drawer";
import type { PlanTask } from "@/lib/types";

const KB_ID = "11111111-1111-4111-8111-111111111111";

function task(action_type: PlanTask["action_type"] = "mixed"): PlanTask {
  return {
    task_id: "55555555-5555-4555-8555-555555555555",
    status: "pending",
    title: "二次函数图像",
    objective: "能够根据顶点式判断二次函数的开口与顶点。",
    instructions: ["复习顶点式", "完成三道变式题", "用自己的话解释判断过程"],
    completion_criteria: "独立完成三道题，并能解释每一步。",
    minutes: 25,
    kc_names: ["二次函数"],
    action_type,
    practice_count: 8,
    difficulty: "hard",
    resource: {
      id: "22222222-2222-4222-8222-222222222222",
      source_type: "kb_chunk",
      title: "二次函数讲义",
      kb_id: KB_ID,
      doc_id: "33333333-3333-4333-8333-333333333333",
      chunk_id: "44444444-4444-4444-8444-444444444444",
    },
  };
}

function location(overrides: Partial<PlanTask> = {}): PlanTaskLocation {
  return {
    task: { ...task(), ...overrides },
    stageName: "函数基础",
    day: 2,
    planKbId: KB_ID,
  };
}

function DrawerHarness({ initial = location() }: { initial?: PlanTaskLocation }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>打开任务</button>
      {open && <PlanTaskDrawer location={initial} onClose={() => setOpen(false)} />}
    </>
  );
}

describe("PlanTaskDrawer", () => {
  it("renders executable task requirements and all mixed actions", () => {
    render(<PlanTaskDrawer location={location()} onClose={() => {}} />);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "二次函数图像" })).toBeInTheDocument();
    expect(screen.getByText("函数基础 · 第 2 天")).toBeInTheDocument();
    expect(screen.getByText("能够根据顶点式判断二次函数的开口与顶点。")).toBeInTheDocument();
    expect(screen.getByText("复习顶点式")).toBeInTheDocument();
    expect(screen.getByText("独立完成三道题，并能解释每一步。")).toBeInTheDocument();
    expect(screen.getByText("预计 25 分钟")).toBeInTheDocument();
    expect(screen.getAllByText("二次函数").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: "阅读资料" }).length).toBe(1);
    expect(screen.getByRole("link", { name: "开始针对性练习" })).toHaveAttribute(
      "href",
      "/practice/session?mode=quiz&from=%2Fplans&topic=%E4%BA%8C%E6%AC%A1%E5%87%BD%E6%95%B0&kb=11111111-1111-4111-8111-111111111111&count=8&difficulty=hard",
    );
    expect(screen.getByRole("link", { name: "进入分级辅导" })).toHaveAttribute(
      "href",
      "/tutor?kb=11111111-1111-4111-8111-111111111111",
    );
    expect(screen.getByRole("link", { name: "复习相关错题" })).toHaveAttribute(
      "href",
      "/practice/session?mode=review&from=%2Fplans",
    );
  });

  it("uses only owned plan/task identifiers for tutor context", () => {
    render(
      <PlanTaskDrawer
        location={{
          ...location(),
          planId: "22222222-2222-4222-8222-222222222222",
          task: { ...task(), task_id: "00000000-0000-4000-8000-000000000001" },
        }}
        onClose={() => {}}
      />,
    );
    const href = screen.getByRole("link", { name: "进入分级辅导" }).getAttribute("href") ?? "";
    expect(href).toBe("/tutor?plan_id=22222222-2222-4222-8222-222222222222&task_id=00000000-0000-4000-8000-000000000001&kb=11111111-1111-4111-8111-111111111111");
    expect(href).not.toContain("problem=");
    expect(href).not.toContain(encodeURIComponent("能够根据顶点式判断二次函数的开口与顶点。"));
  });

  it.each([
    ["read", "阅读资料"],
    ["practice", "开始针对性练习"],
    ["tutor", "进入分级辅导"],
    ["review_errors", "复习相关错题"],
  ] as const)("only shows the %s action for that action type", (action, label) => {
    render(<PlanTaskDrawer location={location({ action_type: action })} onClose={() => {}} />);
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    const actionLabels = ["阅读资料", "开始针对性练习", "进入分级辅导", "复习相关错题"];
    for (const other of actionLabels.filter((candidate) => candidate !== label)) {
      expect(screen.queryByRole("link", { name: other })).not.toBeInTheDocument();
    }
  });

  it("hides reading when a task has no usable resource and clamps invalid practice count", () => {
    render(
      <PlanTaskDrawer
        location={location({ action_type: "practice", practice_count: 50, resource: null })}
        onClose={() => {}}
      />,
    );
    expect(screen.queryByRole("link", { name: "阅读资料" })).not.toBeInTheDocument();
    expect(screen.getByText("本任务没有可用的持久化资料关联。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "开始针对性练习" })).toHaveAttribute(
      "href",
      "/practice/session?mode=quiz&from=%2Fplans&topic=%E4%BA%8C%E6%AC%A1%E5%87%BD%E6%95%B0&kb=11111111-1111-4111-8111-111111111111&count=3&difficulty=hard",
    );
  });

  it("supports Escape, backdrop close, focus on open, focus trap, and opener restore", async () => {
    const user = userEvent.setup();
    render(<DrawerHarness />);
    await user.click(screen.getByRole("button", { name: "打开任务" }));
    const close = screen.getByRole("button", { name: "关闭任务详情" });
    expect(document.activeElement).toBe(close);

    const links = screen.getAllByRole("link");
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(links[links.length - 1]);
    await user.tab();
    expect(document.activeElement).toBe(close);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开任务" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "打开任务" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("dialog"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("presentation"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
