"use client";

// 学习计划（F3）：发起表单 → SSE 流 + interrupt 计划确认（阶段树预览）→ 确认落库；
// 计划列表 + 日历视图（stages/days/tasks 渲染为周网格卡片）。

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  LoadingState,
  Spinner,
  fmtDateTime,
} from "@/components/common";
import { FlowTimeline, type FlowItem } from "@/components/flow-timeline";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import { streamSse } from "@/lib/sse";
import type {
  ErrorEventData,
  GraphDoneData,
  GraphInterruptData,
  GraphTraceData,
  KbOut,
  NodeUpdateData,
  PlanListItem,
  PlanStructure,
  PlanResource,
} from "@/lib/types";

type Phase = "idle" | "streaming" | "await_confirm" | "resuming";

/** 阶段配色（固定顺序分配，不随过滤变化）。 */
const STAGE_COLORS = [
  { chip: "bg-indigo-50 text-indigo-700", bar: "bg-indigo-400" },
  { chip: "bg-violet-50 text-violet-700", bar: "bg-violet-400" },
  { chip: "bg-sky-50 text-sky-700", bar: "bg-sky-400" },
  { chip: "bg-emerald-50 text-emerald-700", bar: "bg-emerald-400" },
  { chip: "bg-amber-50 text-amber-700", bar: "bg-amber-400" },
];

interface FlatDay {
  day: number;
  stageIdx: number;
  stageName: string;
  tasks: PlanStructure["stages"][number]["days"][number]["tasks"];
}

function flattenDays(structure: PlanStructure | null): FlatDay[] {
  if (!structure?.stages) return [];
  const out: FlatDay[] = [];
  structure.stages.forEach((stage, stageIdx) => {
    for (const day of stage.days ?? []) {
      out.push({
        day: day.day,
        stageIdx,
        stageName: stage.name,
        tasks: day.tasks ?? [],
      });
    }
  });
  return out.sort((a, b) => a.day - b.day);
}

/** 日历视图：按 7 天一周分组的网格卡片。 */
function PlanCalendar({ structure }: { structure: PlanStructure }) {
  const days = flattenDays(structure);
  if (days.length === 0) {
    return <EmptyState title="该计划暂无日程" />;
  }
  const weeks: FlatDay[][] = [];
  for (let i = 0; i < days.length; i += 7) {
    weeks.push(days.slice(i, i + 7));
  }
  return (
    <div className="space-y-4">
      {/* 阶段图例 */}
      <div className="flex flex-wrap gap-1.5">
        {structure.stages.map((s, i) => {
          const color = STAGE_COLORS[i % STAGE_COLORS.length];
          return (
            <span
              key={i}
              className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${color.chip}`}
            >
              <span className={`h-1.5 w-1.5 rounded-full ${color.bar}`} />
              {s.name}（{s.days?.length ?? 0} 天）
            </span>
          );
        })}
      </div>

      {weeks.map((week, wi) => (
        <div key={wi}>
          <p className="mb-1.5 text-xs font-medium text-slate-400">第 {wi + 1} 周</p>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 md:grid-cols-4 xl:grid-cols-7">
            {week.map((d) => {
              const color = STAGE_COLORS[d.stageIdx % STAGE_COLORS.length];
              const total = d.tasks.reduce((s, t) => s + (t.minutes || 0), 0);
              return (
                <div
                  key={d.day}
                  className="flex flex-col overflow-hidden rounded-xl border border-slate-200 bg-white"
                >
                  <div className={`h-1 ${color.bar}`} />
                  <div className="flex items-center justify-between px-2.5 pt-2">
                    <span className="text-xs font-semibold text-slate-700">
                      第 {d.day} 天
                    </span>
                    <span className="text-[10px] text-slate-400">{total} 分钟</span>
                  </div>
                  <ul className="space-y-1 p-2">
                    {d.tasks.map((t, ti) => (
                      <li
                        key={ti}
                        className="rounded-lg bg-slate-50 px-2 py-1.5"
                        title={d.stageName}
                      >
                        <p className="text-xs leading-snug text-slate-700">
                          <MathText text={t.title} />
                        </p>
                        <p className="mt-0.5 flex flex-wrap items-center gap-1">
                          <span className="rounded bg-white px-1 py-0.5 text-[10px] font-medium text-slate-500">
                            {t.minutes} 分钟
                          </span>
                          {t.kc_names.map((kc) => (
                            <span
                              key={kc}
                              className={`max-w-24 truncate rounded px-1 py-0.5 text-[10px] ${color.chip}`}
                            >
                              {kc}
                            </span>
                          ))}
                        </p>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

/** interrupt=plan_confirm 的阶段树预览卡片。 */
function PlanConfirmCard({
  plan,
  resources,
  onConfirm,
  onCancel,
}: {
  plan: PlanStructure;
  resources: PlanResource[];
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const totalDays = plan.stages.reduce((s, st) => s + (st.days?.length ?? 0), 0);
  return (
    <div className="rounded-2xl border border-indigo-200 bg-indigo-50/50 p-4">
      <h3 className="text-sm font-semibold text-slate-900">学习计划草案待确认</h3>
      <p className="mt-0.5 text-xs text-slate-500">
        共 {plan.stages.length} 个阶段 / {totalDays} 天
        {resources.length > 0 && ` · 关联 ${resources.length} 条学习资源`}
        ，确认后计划才会保存。
      </p>

      <ul className="mt-3 space-y-2">
        {plan.stages.map((stage, i) => {
          const color = STAGE_COLORS[i % STAGE_COLORS.length];
          const minutes = (stage.days ?? []).reduce(
            (s, d) => s + d.tasks.reduce((x, t) => x + (t.minutes || 0), 0),
            0,
          );
          return (
            <li key={i} className="rounded-xl border border-indigo-100 bg-white p-3">
              <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-slate-800">
                <span className={`h-2 w-2 rounded-full ${color.bar}`} />
                {stage.name}
                <span className="text-xs font-normal text-slate-400">
                  {stage.days?.length ?? 0} 天 · 约 {Math.round(minutes / 60)} 小时
                </span>
              </p>
              <ul className="mt-1.5 space-y-0.5 pl-4">
                {(stage.days ?? []).slice(0, 3).map((d) => (
                  <li key={d.day} className="text-xs text-slate-500">
                    第 {d.day} 天：
                    {d.tasks.map((t) => t.title).join("；") || "自主安排"}
                  </li>
                ))}
                {(stage.days?.length ?? 0) > 3 && (
                  <li className="text-xs text-slate-400">
                    …另有 {(stage.days?.length ?? 0) - 3} 天
                  </li>
                )}
              </ul>
            </li>
          );
        })}
      </ul>

      {resources.length > 0 && (
        <details className="mt-3 rounded-xl border border-indigo-100 bg-white p-3">
          <summary className="cursor-pointer text-xs font-medium text-slate-600">
            学习资源（{resources.length}）
          </summary>
          <ul className="mt-1.5 space-y-0.5">
            {resources.map((r, i) => (
              <li key={i} className="truncate text-xs text-slate-500">
                <span className="mr-1 rounded bg-slate-100 px-1 py-0.5 text-[10px] text-slate-500">
                  {r.type === "kb" ? "知识库" : "网页"}
                </span>
                {r.title}
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100"
        >
          取消
        </button>
        <button
          type="button"
          onClick={onConfirm}
          className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700"
        >
          确认并保存计划
        </button>
      </div>
    </div>
  );
}

export default function PlansPage() {
  const [kbs, setKbs] = useState<KbOut[]>([]);
  const [plans, setPlans] = useState<PlanListItem[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<PlanListItem | null>(null);

  // 表单
  const [goal, setGoal] = useState("");
  const [kbId, setKbId] = useState("");
  const [hours, setHours] = useState(1.5);
  const [totalDays, setTotalDays] = useState(14);

  // SSE 流
  const [phase, setPhase] = useState<Phase>("idle");
  const [timeline, setTimeline] = useState<FlowItem[]>([]);
  const [interruptData, setInterruptData] = useState<GraphInterruptData | null>(null);
  const [flowError, setFlowError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // 首个 setState 在 await 之后（effect 内不允许同步 setState）
  const loadPlans = useCallback(async (): Promise<PlanListItem[]> => {
    try {
      const list = await apiJson<PlanListItem[]>("/api/plans");
      setPlans(list);
      setListError(null);
      return list;
    } catch (err) {
      setPlans((prev) => prev ?? []);
      setListError(err instanceof ApiError ? err.message : "计划列表加载失败");
      return [];
    }
  }, []);

  // 首屏并行拉取计划与知识库列表（effect 内联 IIFE，同 chat 页模式）
  useEffect(() => {
    let alive = true;
    (async () => {
      const [plansRes, kbsRes] = await Promise.allSettled([
        apiJson<PlanListItem[]>("/api/plans"),
        apiJson<KbOut[]>("/api/kb"),
      ]);
      if (!alive) return;
      if (plansRes.status === "fulfilled") setPlans(plansRes.value);
      else {
        setPlans([]);
        setListError("计划列表加载失败");
      }
      setKbs(kbsRes.status === "fulfilled" ? kbsRes.value : []);
    })();
    return () => {
      alive = false;
      abortRef.current?.abort();
    };
  }, []);

  const consume = useCallback(
    async (path: string, body: unknown) => {
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        for await (const msg of streamSse(path, body, {
          signal: controller.signal,
        })) {
          switch (msg.event) {
            case "node_update":
              setTimeline((prev) => [
                ...prev,
                { kind: "node", data: msg.data as NodeUpdateData },
              ]);
              break;
            case "trace_event":
              setTimeline((prev) => [
                ...prev,
                { kind: "trace", data: msg.data as GraphTraceData },
              ]);
              break;
            case "interrupt":
              setInterruptData(msg.data as GraphInterruptData);
              setPhase("await_confirm");
              break;
            case "done": {
              const d = msg.data as GraphDoneData;
              const planId = d.result?.plan_id;
              setPhase("idle");
              setInterruptData(null);
              if (planId) {
                setNotice(null);
                void loadPlans().then((list) => {
                  setSelected(
                    list.find((p) => p.id === planId) ?? list[0] ?? null,
                  );
                });
              } else {
                setNotice("已取消，计划未保存。");
              }
              break;
            }
            case "error": {
              const d = msg.data as ErrorEventData;
              setFlowError(d.message || d.code);
              setPhase("idle");
              break;
            }
            default:
              break;
          }
        }
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") return;
        setFlowError(
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : "网络异常，请稍后重试",
        );
        setPhase("idle");
      } finally {
        abortRef.current = null;
        // 流意外结束（无 done/interrupt）时兜底回到空闲态
        setPhase((p) => (p === "await_confirm" ? p : "idle"));
      }
    },
    [loadPlans],
  );

  const startPlan = (e: FormEvent) => {
    e.preventDefault();
    if (!goal.trim() || phase !== "idle") return;
    setTimeline([]);
    setInterruptData(null);
    setFlowError(null);
    setNotice(null);
    setSelected(null);
    setPhase("streaming");
    void consume("/api/plans", {
      goal: goal.trim(),
      kb_id: kbId || undefined,
      budget_hours_per_day: hours,
      total_days: totalDays,
    });
  };

  const resume = (approved: boolean) => {
    if (!interruptData) return;
    setPhase("resuming");
    void consume(interruptData.resume_url, { value: { approved } });
  };

  const busy = phase === "streaming" || phase === "resuming";

  return (
    <div className="mx-auto w-full max-w-6xl space-y-4 p-4">
      {flowError && (
        <ErrorBanner message={flowError} onClose={() => setFlowError(null)} />
      )}
      {notice && (
        <div className="flex items-center justify-between rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-700">
          <span>{notice}</span>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="text-amber-400 hover:text-amber-600"
            aria-label="关闭提示"
          >
            ✕
          </button>
        </div>
      )}

      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        {/* 左：发起 + 列表 */}
        <aside className="w-full shrink-0 space-y-4 lg:w-80">
          <Card title="制定学习计划">
            <form onSubmit={startPlan} className="space-y-3">
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-500">
                  学习目标
                </span>
                <textarea
                  value={goal}
                  onChange={(e) => setGoal(e.target.value)}
                  rows={3}
                  maxLength={2000}
                  placeholder="如：两周内掌握微积分基础，能独立完成课后习题"
                  className="w-full resize-none rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-500">
                  绑定知识库（可选）
                </span>
                <select
                  value={kbId}
                  onChange={(e) => setKbId(e.target.value)}
                  className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                >
                  <option value="">不绑定</option>
                  {kbs.map((kb) => (
                    <option key={kb.id} value={kb.id}>
                      {kb.name}
                    </option>
                  ))}
                </select>
              </label>
              <div className="grid grid-cols-2 gap-2">
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-slate-500">
                    每日小时
                  </span>
                  <input
                    type="number"
                    min={0.5}
                    max={12}
                    step={0.5}
                    value={hours}
                    onChange={(e) =>
                      setHours(Math.max(0.5, Math.min(12, Number(e.target.value) || 0.5)))
                    }
                    className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-slate-500">
                    总天数
                  </span>
                  <input
                    type="number"
                    min={1}
                    max={90}
                    value={totalDays}
                    onChange={(e) =>
                      setTotalDays(
                        Math.max(1, Math.min(90, Math.trunc(Number(e.target.value) || 1))),
                      )
                    }
                    className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                  />
                </label>
              </div>
              <button
                type="submit"
                disabled={!goal.trim() || busy || phase === "await_confirm"}
                className="flex w-full items-center justify-center gap-2 rounded-xl bg-indigo-600 py-2.5 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy && <Spinner className="h-4 w-4 border-white/40 border-t-white" />}
                {busy ? "正在规划…" : "生成计划草案"}
              </button>
            </form>
          </Card>

          <Card title="我的计划">
            {listError && (
              <div className="mb-2">
                <ErrorBanner message={listError} onRetry={() => void loadPlans()} />
              </div>
            )}
            {plans === null ? (
              <LoadingState text="计划加载中…" />
            ) : plans.length === 0 ? (
              <p className="py-4 text-center text-xs text-slate-400">
                还没有学习计划
              </p>
            ) : (
              <ul className="space-y-1">
                {plans.map((p) => (
                  <li key={p.id}>
                    <button
                      type="button"
                      onClick={() => setSelected(p)}
                      className={`w-full rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                        selected?.id === p.id
                          ? "bg-indigo-50 text-indigo-700"
                          : "text-slate-700 hover:bg-slate-100"
                      }`}
                    >
                      <span className="block truncate font-medium">{p.goal}</span>
                      <span className="block text-xs font-normal text-slate-400">
                        每日 {p.budget_hours} 小时 · {fmtDateTime(p.created_at)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </aside>

        {/* 右：流程 / 日历 */}
        <div className="min-w-0 flex-1 space-y-4">
          {(timeline.length > 0 || busy) && (
            <FlowTimeline items={timeline} streaming={busy} />
          )}

          {phase === "await_confirm" && interruptData?.plan && (
            <PlanConfirmCard
              plan={interruptData.plan}
              resources={interruptData.resources ?? []}
              onConfirm={() => resume(true)}
              onCancel={() => resume(false)}
            />
          )}
          {phase === "resuming" && (
            <p className="flex items-center gap-2 text-sm text-slate-500">
              <Spinner /> 正在保存计划…
            </p>
          )}

          {selected ? (
            <Card
              title={
                <span className="block max-w-100 truncate">{selected.goal}</span>
              }
              extra={
                <span className="text-xs text-slate-400">
                  每日 {selected.budget_hours} 小时
                </span>
              }
            >
              {selected.structure ? (
                <PlanCalendar structure={selected.structure} />
              ) : (
                <EmptyState title="该计划缺少结构数据" />
              )}
            </Card>
          ) : (
            phase === "idle" &&
            timeline.length === 0 && (
              <EmptyState
                title="选择一个计划查看日历"
                description="或在左侧填写目标，AI 将规划分阶段的每日任务"
              />
            )
          )}
        </div>
      </div>
    </div>
  );
}
