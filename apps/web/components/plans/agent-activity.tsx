"use client";

import type { FlowItem } from "@/components/flow-timeline";
import { useEffect, useState } from "react";
import { apiJson } from "@/lib/api";
import type { GraphTraceData } from "@/lib/types";
import type { PlanReview } from "@/lib/types/plans";

const ROLES: Record<string, string> = { researcher: "资料研究", planner: "学习规划", reviewer: "独立审查", quiz: "练习生成", tutor: "分级辅导" };
const STATUS: Record<string, string> = { queued: "等待中", started: "执行中", completed: "已完成", failed: "失败", cancelled: "已停止" };
const CHECKS: Record<string, string> = { prerequisites: "先学后练", baseline: "学习起点", time_budget: "时间与任务量", resources: "资料依据", outcomes: "目标覆盖", review_timeline: "复习顺序" };

export function AgentActivity({ items }: { items: FlowItem[] }) {
  const tasks = new Map<string, { name: string; role: string; status: string; summary: string }>();
  for (const item of items) {
    if (item.kind !== "trace" || item.data.type !== "agent" || typeof item.data.task_id !== "string") continue;
    const previous = tasks.get(item.data.task_id);
    const event = String(item.data.event ?? "");
    tasks.set(item.data.task_id, {
      name: String(item.data.agent_name ?? ""), role: String(item.data.agent_role ?? ""),
      status: STATUS[event] ?? previous?.status ?? "执行中",
      summary: event.startsWith("tool_") ? previous?.summary ?? "正在读取或核验资料" : String(item.data.summary ?? ""),
    });
  }
  if (!tasks.size) return null;
  return <section aria-label="Agent 协作进度" className="rounded-overlay border border-line bg-white p-4">
    <h3 className="text-body-sm font-semibold text-ink">协作进度</h3>
    <ol className="mt-3 grid gap-3 md:grid-cols-3">
      {[...tasks.entries()].map(([id, task]) => <li key={id} className="rounded-control bg-surface-muted p-3">
        <div className="flex items-center justify-between gap-2 text-body-sm">
          <strong className="text-ink">{ROLES[task.role] ?? task.role}</strong>
          <span className={task.status === "失败" ? "text-danger-600" : "text-ink-muted"}>{task.status}</span>
        </div>
        <p className="mt-1 text-caption text-ink-muted">{task.name}</p>
        <p className="mt-2 text-body-sm leading-relaxed text-ink-secondary">{task.summary}</p>
      </li>)}
    </ol>
  </section>;
}

export function PlanReviewCard({ review }: { review?: PlanReview }) {
  if (!review) return null;
  return <section aria-label="计划审查结果" className="rounded-overlay border border-line bg-white p-4">
    <h3 className="text-body-sm font-semibold text-ink">{review.approved ? "独立审查已通过" : "计划需要修改"}</h3>
    <p className="mt-2 text-body-sm leading-relaxed text-ink-secondary">{review.summary}</p>
    {review.checks && <ul className="mt-3 flex flex-wrap gap-2">{Object.entries(review.checks).map(([key, value]) => <li key={key} className="rounded-control bg-surface-muted px-2 py-1 text-caption text-ink-secondary">
      {CHECKS[key] ?? key}：{value === "pass" ? "通过" : value === "fail" ? "需修改" : "不适用"}
    </li>)}</ul>}
    {review.issues?.length ? <ul className="mt-3 space-y-2">{review.issues.map((issue, index) => <li key={index} className="text-body-sm text-ink-secondary"><p>{issue.message}</p><p className="mt-1 text-ink-muted">{issue.suggestion}</p></li>)}</ul> : null}
  </section>;
}

export function SavedAgentActivity({ planId }: { planId: string }) {
  const [activity, setActivity] = useState<{ planId: string; events: GraphTraceData[]; review?: PlanReview } | null>(null);
  useEffect(() => {
    let disposed = false;
    void apiJson<{ events: GraphTraceData[]; review?: PlanReview }>(`/api/plans/${planId}/agents`)
      .then((value) => { if (!disposed) setActivity({ ...value, planId }); })
      .catch(() => { if (!disposed) setActivity(null); });
    return () => { disposed = true; };
  }, [planId]);
  if (!activity || activity.planId !== planId) return null;
  return <div className="space-y-3">
    <AgentActivity items={activity.events.map((data) => ({ kind: "trace", data }))} />
    <PlanReviewCard review={activity.review} />
  </div>;
}
