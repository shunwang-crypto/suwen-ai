"use client";

// 学习计划（F3）：发起表单 → SSE 流 + interrupt 计划确认（阶段树预览）→ 确认落库；
// 计划列表 + 日历视图（stages/days/tasks 渲染为周网格卡片）。
// 「计划是活的」：任务复选框（乐观更新+失败回滚）、头部进度条、
// 「根据学情调整计划」插入补强任务并高亮、调整历史时间线。

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react";

import { ModelMarkdown } from "@/components/chat/model-markdown";
import {
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  SkeletonList,
  Spinner,
  fmtDateTime,
  fmtPct,
} from "@/components/common";
import { FlowTimeline, type FlowItem } from "@/components/flow-timeline";
import { KcDependencyPath } from "@/components/plans/kc-dependency-path";
import { PlanTaskDrawer, type PlanTaskLocation } from "@/components/plans/plan-task-drawer";
import { ApiError, apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { MathText } from "@/lib/katex";
import {
  PLAN_GOAL_EXAMPLES,
  isPlanGoalReady,
  parsePlanIntent,
  planGoalReadiness,
} from "@/lib/plan-goal";
import { streamSse } from "@/lib/sse";
import type {ErrorEventData, GraphDoneData, GraphInterruptData, GraphTraceData, KbOut, NodeUpdateData, PlanAdjustResult, PlanAdjustmentItem, PlanDetail, PlanListItem, PlanStructure, PlanToggleResult} from "@/lib/types";
import * as Parts from "@/components/plans/plan-parts";

export default function PlansPage() {
  const { user } = useAuth();
  const userId = user?.id ?? "anonymous";
  const search = useSyncExternalStore(
    Parts.emptySubscribe,
    () => window.location.search,
    () => "",
  );
  const requestedPlanId = new URLSearchParams(search).get("id");
  const [kbs, setKbs] = useState<KbOut[]>([]);
  const [plans, setPlans] = useState<PlanListItem[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<PlanListItem | null>(null);

  // 计划详情（含 done/进度）+ 调整历史
  const [detail, setDetail] = useState<PlanDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [adjustments, setAdjustments] = useState<PlanAdjustmentItem[]>([]);
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(new Set());
  const [highlightKeys, setHighlightKeys] = useState<Set<string>>(new Set());
  const [adjusting, setAdjusting] = useState(false);
  const [adjustResult, setAdjustResult] = useState<PlanAdjustResult | null>(null);
  const [taskBanner, setTaskBanner] = useState<string | null>(null);
  const [detailKey, setDetailKey] = useState(0);
  const [openTask, setOpenTask] = useState<PlanTaskLocation | null>(null);

  // 表单
  const [goal, setGoal] = useState("");
  const [currentLevel, setCurrentLevel] = useState("");
  const [targetOutcome, setTargetOutcome] = useState("");
  const [successCriteria, setSuccessCriteria] = useState("");
  const [kbId, setKbId] = useState("");
  const [hours, setHours] = useState<number | null>(null);
  const [totalDays, setTotalDays] = useState<number | null>(null);
  const [deadline, setDeadline] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  // SSE 流
  const [phase, setPhase] = useState<Parts.Phase>("idle");
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
      if (plansRes.status === "fulfilled") {
        setPlans(plansRes.value);
        setSelected(
          plansRes.value.find((plan) => plan.id === requestedPlanId) ??
            plansRes.value[0] ??
            null,
        );
      }
      else {
        setPlans([]);
        setListError("计划列表加载失败");
      }
      const availableKbs = kbsRes.status === "fulfilled" ? kbsRes.value : [];
      setKbs(availableKbs);
      const pending = Parts.readPendingPlan(userId);
      if (!pending && availableKbs.length === 1) setKbId(availableKbs[0].id);
      if (pending) {
        setGoal(pending.form.goal);
        setCurrentLevel(pending.form.currentLevel ?? "");
        setTargetOutcome(pending.form.targetOutcome ?? "");
        setSuccessCriteria(pending.form.successCriteria ?? "");
        setKbId(pending.form.kbId);
        setHours(pending.form.hours);
        setTotalDays(pending.form.totalDays);
        setSelected(null);
        setShowCreate(true);
        setInterruptData(pending.interrupt ?? null);
        setPhase(
          pending.status === "generating"
            ? "streaming"
            : pending.status === "resuming"
              ? "resuming"
              : "await_confirm",
        );
      }
    })();
    return () => {
      alive = false;
    };
  }, [requestedPlanId, userId]);

  useEffect(() => {
    if (phase !== "streaming" && phase !== "resuming") return;
    const timer = setInterval(() => {
      const pending = Parts.readPendingPlan(userId);
      if (!pending) {
        setPhase("idle");
        void loadPlans();
        return;
      }
      if (pending.status === "await_confirm" && pending.interrupt) {
        setInterruptData(pending.interrupt);
        setPhase("await_confirm");
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [phase, loadPlans, userId]);

  // 选中计划后拉详情 + 调整历史（detailKey 自增可强制重拉；首个 setState 在 await 之后）
  const selectedPlanId = selected?.id ?? "";
  useEffect(() => {
    if (!selectedPlanId) return;
    let alive = true;
    (async () => {
      const [detailRes, adjRes] = await Promise.allSettled([
        apiJson<PlanDetail>(`/api/plans/${selectedPlanId}`),
        apiJson<PlanAdjustmentItem[]>(`/api/plans/${selectedPlanId}/adjustments`),
      ]);
      if (!alive) return;
      if (detailRes.status === "fulfilled") {
        setDetail(detailRes.value);
        setDetailError(null);
      } else {
        setDetail(null);
        setDetailError(
          detailRes.reason instanceof ApiError
            ? detailRes.reason.message
            : "计划详情加载失败",
        );
      }
      setAdjustments(adjRes.status === "fulfilled" ? adjRes.value : []);
    })();
    return () => {
      alive = false;
    };
  }, [selectedPlanId, detailKey]);

  /** 切换选中计划：详情态在事件处理器里清空（effect 内不允许同步 setState）。 */
  const selectPlan = (plan: PlanListItem | null) => {
    setSelected(plan);
    setDetail(null);
    setDetailError(null);
    setAdjustments([]);
    setAdjustResult(null);
    setHighlightKeys(new Set());
    setTaskBanner(null);
    setOpenTask(null);
  };

  /** 任务三态更新：乐观更新；失败只恢复当前任务，保留其他并发更新。 */
  const updateTaskStatus = async (taskId: string, status: Parts.PlanTaskStatus) => {
    if (!detail || detail.status !== "active" || pendingKeys.has(taskId)) return;
    const snapshot = detail;
    const previousTask = Parts.flattenDays(detail.structure)
      .flatMap((day) => day.tasks)
      .find((task) => task.task_id === taskId);
    if (!previousTask) return;
    const previousStatus = Parts.normalizedTaskStatus(previousTask);
    setDetail(Parts.withTaskStatus(detail, taskId, status));
    setPendingKeys((prev) => new Set(prev).add(taskId));
    setTaskBanner(null);
    try {
      const res = await apiJson<PlanToggleResult>(
        `/api/plans/${snapshot.id}/tasks/${taskId}/toggle`,
        { body: { status } },
      );
      // 以服务端进度为准（并发勾选时避免本地计数漂移）
      setDetail((prev) =>
        prev && prev.id === snapshot.id ? { ...prev, progress: res.progress } : prev,
      );
    } catch (err) {
      setDetail((prev) =>
        prev && prev.id === snapshot.id
          ? Parts.withTaskStatus(prev, taskId, previousStatus)
          : prev,
      );
      setTaskBanner(
        err instanceof ApiError ? err.message : "任务状态更新失败，已回滚",
      );
    } finally {
      setPendingKeys((prev) => {
        const next = new Set(prev);
        next.delete(taskId);
        return next;
      });
    }
  };

  /** 根据学情调整计划：插入补强任务 → 重拉详情并高亮新任务。 */
  const runAdjust = async () => {
    if (!detail || detail.status !== "active" || adjusting) return;
    setAdjusting(true);
    setTaskBanner(null);
    try {
      const res = await apiJson<PlanAdjustResult>(`/api/plans/${detail.id}/adjust`, {
        method: "POST",
        body: {},
      });
      setAdjustResult(res);
      setHighlightKeys(new Set(res.inserted.map((t) => t.task_id)));
      setDetailKey((k) => k + 1);
    } catch (err) {
      setTaskBanner(err instanceof ApiError ? err.message : "计划调整失败，请重试");
    } finally {
      setAdjusting(false);
    }
  };

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
              {
                const interrupt = msg.data as GraphInterruptData;
                const pending = Parts.readPendingPlan(userId);
                Parts.writePendingPlan(userId, {
                  status: "await_confirm",
                  interrupt,
                  startedAt: pending?.startedAt ?? Date.now(),
                  form: pending?.form ?? {
                    goal,
                    currentLevel,
                    targetOutcome,
                    successCriteria,
                    kbId,
                    hours,
                    totalDays,
                  },
                });
                setInterruptData(interrupt);
              }
              setPhase("await_confirm");
              break;
            case "done": {
              const d = msg.data as GraphDoneData;
              const planId = d.result?.plan_id;
              Parts.clearPendingPlan(userId);
              setPhase("idle");
              setInterruptData(null);
              if (planId) {
                setNotice(null);
                setDetail(null);
                setAdjustments([]);
                setAdjustResult(null);
                setHighlightKeys(new Set());
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
              const pending = Parts.readPendingPlan(userId);
              if (path.includes("/api/threads/") && pending?.interrupt) {
                Parts.writePendingPlan(userId, { ...pending, status: "await_confirm" });
                setPhase("await_confirm");
              } else {
                Parts.clearPendingPlan(userId);
                setPhase("idle");
              }
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
        const pending = Parts.readPendingPlan(userId);
        if (path.includes("/api/threads/") && pending?.interrupt) {
          Parts.writePendingPlan(userId, { ...pending, status: "await_confirm" });
          setPhase("await_confirm");
        } else {
          Parts.clearPendingPlan(userId);
          setPhase("idle");
        }
      } finally {
        abortRef.current = null;
        // 流意外结束（无 done/interrupt）时兜底回到空闲态
        setPhase((p) => (p === "await_confirm" ? p : "idle"));
      }
    },
    [
      currentLevel,
      goal,
      hours,
      kbId,
      loadPlans,
      successCriteria,
      targetOutcome,
      totalDays,
      userId,
    ],
  );

  const startPlan = (e: FormEvent) => {
    e.preventDefault();
    const goalForm = { goal, currentLevel, targetOutcome, successCriteria };
    if (!isPlanGoalReady(goalForm) || phase !== "idle") return;
    const intent = parsePlanIntent(goal);
    const today = new Date();
    const selectedDeadline = deadline || intent.deadline;
    const deadlineDays = selectedDeadline
      ? Math.max(1, Math.min(90, Math.ceil((new Date(`${selectedDeadline}T00:00:00`).getTime() - new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()) / 86_400_000)))
      : null;
    const selectedHours = intent.dailyHours ?? hours;
    const selectedTotalDays = deadlineDays ?? intent.totalDays ?? totalDays;
    if (selectedHours == null || selectedTotalDays == null) {
      setFlowError("请先确认截止日期/周期，以及每天可投入时间；系统不会替你猜默认值。");
      return;
    }
    if (kbs.length > 1 && !kbId) {
      setFlowError("请先选择这次计划使用的知识库范围。");
      return;
    }
    setHours(selectedHours);
    setTotalDays(selectedTotalDays);
    Parts.clearPendingPlan(userId);
    Parts.writePendingPlan(userId, {
      status: "generating",
      startedAt: Date.now(),
      form: {
        goal: goal.trim(),
        currentLevel: currentLevel.trim(),
        targetOutcome: targetOutcome.trim(),
        successCriteria: successCriteria.trim(),
        kbId,
        hours: selectedHours,
        totalDays: selectedTotalDays,
      },
    });
    setTimeline([]);
    setInterruptData(null);
    setFlowError(null);
    setNotice(null);
    selectPlan(null);
    setPhase("streaming");
    void consume("/api/plans", {
      goal: goal.trim(),
      current_level: currentLevel.trim() || undefined,
      target_outcome: targetOutcome.trim(),
      success_criteria: successCriteria.trim(),
      kb_id: kbId || undefined,
      budget_hours_per_day: selectedHours,
      total_days: selectedTotalDays,
    });
  };

  const resume = (approved: boolean, plan?: PlanStructure) => {
    if (!interruptData) return;
    const pending = Parts.readPendingPlan(userId);
    const form = pending?.form ?? {
      goal,
      currentLevel,
      targetOutcome,
      successCriteria,
      kbId,
      hours,
      totalDays,
    };
    Parts.writePendingPlan(userId, {
      status: "resuming",
      interrupt: interruptData,
      startedAt: pending?.startedAt ?? Date.now(),
      form,
    });
    setPhase("resuming");
    // 编辑版随 approved 一起回传; 服务端 confirm_plan 会重跑硬校验并以其结果为准
    void consume(interruptData.resume_url, {
      value: approved ? { approved: true, ...(plan ? { plan } : {}) } : { approved: false },
    });
  };

  const busy = phase === "streaming" || phase === "resuming";
  const goalForm = { goal, currentLevel, targetOutcome, successCriteria };
  const goalReady = isPlanGoalReady(goalForm);
  const goalReadiness = planGoalReadiness(goalForm);
  const planIntent = parsePlanIntent(goal);
  const confirmedDeadline = deadline || planIntent.deadline;
  const confirmedTotalDays = totalDays ?? planIntent.totalDays;
  const scheduleReady =
    (planIntent.dailyHours ?? hours) != null &&
    Boolean(confirmedDeadline || confirmedTotalDays);
  const scopeReady = kbs.length <= 1 || Boolean(kbId);

  const openDependencyTask = (taskId: string) => {
    if (!detail) return;
    for (const stage of detail.structure.stages) {
      for (const day of stage.days ?? []) {
        const task = (day.tasks ?? []).find((item) => item.task_id === taskId);
        if (task) {
          setOpenTask({
            task,
            stageName: stage.name,
            day: day.day,
            planId: detail.id,
            planKbId: detail.kb_id,
            readOnly: detail.status !== "active",
          });
          return;
        }
      }
    }
  };

  return (
    <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      <PageHeader
        title="学习计划"
        subtitle="AI 规划分阶段每日任务；计划是活的——勾选完成、按学情插入补强任务"
        action={
          <button
            type="button"
            onClick={() => setShowCreate((value) => !value)}
            className="rounded-[8px] bg-brand-600 px-4 py-2 text-body-sm font-medium text-white transition-colors hover:bg-brand-800"
          >
            新建计划
          </button>
        }
      />

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

      <div className="space-y-4">
        {/* 左：发起 + 列表 */}
        <aside className="w-full shrink-0 space-y-4">
          {(showCreate || plans?.length === 0) && <Card title="制定学习计划">
            <form onSubmit={startPlan} className="space-y-3">
              <div className="rounded-xl border border-brand-100 bg-brand-50/40 p-3">
                <p className="text-caption font-medium text-ink">你想在什么时候，达到什么结果？</p>
                <p id="plan-goal-help" className="mt-1 text-body-sm leading-5 text-ink-muted">
                  用一句话告诉我学习范围、希望达到的结果和时间；已有资料与画像会用于推导起点和节奏。
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5" aria-label="目标示例">
                  {PLAN_GOAL_EXAMPLES.map((example) => (
                    <button
                      key={example.label}
                      type="button"
                      onClick={() => {
                        setGoal(example.goal);
                        setCurrentLevel(example.currentLevel);
                        setTargetOutcome(example.targetOutcome);
                        setSuccessCriteria(example.successCriteria);
                      }}
                      className="rounded-full border border-brand-200 bg-white px-2.5 py-1 text-caption font-medium text-brand-700 hover:bg-brand-50"
                    >
                      使用“{example.label}”示例
                    </button>
                  ))}
                </div>
              </div>
              <label className="block">
                <span className="mb-1 block text-caption font-medium text-slate-500">
                  一句话目标 <span className="text-rose-500">*</span>
                </span>
                <textarea
                  value={goal}
                  onChange={(e) => setGoal(e.target.value)}
                  rows={2}
                  maxLength={2000}
                  required
                  aria-describedby="plan-goal-help"
                  placeholder="如：极限与导数基础"
                  className="w-full resize-none rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <div className="rounded-xl border border-line bg-surface-muted/60 p-3" aria-live="polite">
                <p className="text-body-sm font-medium text-ink">请确认计划节奏</p>
                <p className="mt-1 text-body-sm leading-5 text-ink-muted">
                  {confirmedDeadline || confirmedTotalDays
                    ? `${planIntent.deadline || planIntent.totalDays ? "已从目标识别出" : "已确认"}${confirmedDeadline ? `截止 ${confirmedDeadline}` : `周期 ${confirmedTotalDays} 天`}`
                    : "这句话没有明确截止日期或周期，请补充一次。"}
                  {planIntent.dailyHours != null
                    ? `；每天 ${planIntent.dailyHours} 小时`
                    : hours != null
                      ? `；已确认每天 ${hours} 小时`
                      : "；每天投入时间仍需确认。"}
                </p>
                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                  {!planIntent.deadline && !planIntent.totalDays && totalDays == null && (
                    <label className="block text-body-sm text-ink-secondary">
                      截止日期
                      <input type="date" value={deadline} onChange={(event) => setDeadline(event.target.value)} className="mt-1 w-full rounded-lg border border-line-strong bg-white px-2.5 py-2 text-[13px] text-ink" />
                    </label>
                  )}
                  {planIntent.dailyHours == null && (
                    <label className="block text-body-sm text-ink-secondary">
                      每天可投入（小时）
                      <input type="number" min={0.5} max={12} step={0.5} value={hours ?? ""} onChange={(event) => setHours(event.target.value ? Math.max(0.5, Math.min(12, Number(event.target.value))) : null)} className="mt-1 w-full rounded-lg border border-line-strong bg-white px-2.5 py-2 text-[13px] text-ink" />
                    </label>
                  )}
                </div>
                {confirmedDeadline && <p className="mt-2 text-body-sm text-success-700">已确认截止日期：{confirmedDeadline}</p>}
              </div>
              {kbs.length > 1 && !kbId && (
                <div className="rounded-xl border border-brand-100 bg-brand-50/40 p-3" aria-label="选择学习范围">
                  <p className="text-body-sm font-medium text-ink">这次计划学习哪一份资料？</p>
                  <p className="mt-1 text-caption text-ink-muted">请选择一次范围，其他背景和成果仍可稍后补充。</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {kbs.map((kb) => (
                      <button type="button" key={kb.id} onClick={() => setKbId(kb.id)} className="rounded-full border border-brand-200 bg-white px-3 py-1.5 text-body-sm font-medium text-brand-700 hover:border-brand-400">
                        {kb.name}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              <details className="rounded-xl border border-line bg-white">
                <summary className="cursor-pointer px-3 py-2.5 text-body-sm font-medium text-ink-secondary">补充背景、范围和计划时长（可选）</summary>
                <div className="space-y-3 border-t border-line p-3">
              <label className="block">
                <span className="mb-1 block text-caption font-medium text-slate-500">
                  当前基础 <span className="font-normal text-slate-400">（可选）</span>
                </span>
                <input
                  value={currentLevel}
                  onChange={(e) => setCurrentLevel(e.target.value)}
                  maxLength={200}
                  placeholder="如：会基本代数运算，但极限概念不熟"
                  className="w-full rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-caption font-medium text-slate-500">
                  期望成果 <span className="font-normal text-slate-400">（可选）</span>
                </span>
                <textarea
                  value={targetOutcome}
                  onChange={(e) => setTargetOutcome(e.target.value)}
                  rows={2}
                  maxLength={500}
                  placeholder="如：能独立判断常见极限题型并完成基础导数计算"
                  className="w-full resize-none rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-caption font-medium text-slate-500">
                  验收标准 <span className="font-normal text-slate-400">（可选）</span>
                </span>
                <textarea
                  value={successCriteria}
                  onChange={(e) => setSuccessCriteria(e.target.value)}
                  rows={2}
                  maxLength={500}
                  placeholder="如：不看提示完成 10 道教材基础题，至少正确 8 道并能说明错因"
                  className="w-full resize-none rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <div className="grid grid-cols-2 gap-1.5 text-caption sm:grid-cols-4" aria-label="目标完整度" aria-live="polite">
                {([
                  ["学习主题", goalReadiness.topic],
                  ["当前基础", goalReadiness.baseline],
                  ["期望成果", goalReadiness.outcome],
                  ["验收标准", goalReadiness.criteria],
                ] as const).map(([label, ready]) => (
                  <span key={label} className={`rounded-lg px-2 py-1.5 ${ready ? "bg-success-50 text-success-700" : "bg-surface-muted text-ink-muted"}`}>
                    {ready ? "✓" : "○"} {label}
                  </span>
                ))}
              </div>
              <div className="grid grid-cols-2 gap-2">
                <label className="block">
                  <span className="mb-1 block text-caption font-medium text-slate-500">
                    每日小时
                  </span>
                  <input
                    type="number"
                    min={0.5}
                    max={12}
                    step={0.5}
                    value={hours ?? ""}
                    onChange={(e) => setHours(e.target.value ? Math.max(0.5, Math.min(12, Number(e.target.value))) : null)}
                    className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-caption font-medium text-slate-500">
                    总天数
                  </span>
                  <input
                    type="number"
                    min={1}
                    max={90}
                    value={totalDays ?? ""}
                    onChange={(e) => setTotalDays(e.target.value ? Math.max(1, Math.min(90, Math.trunc(Number(e.target.value)))) : null)}
                    className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                  />
                </label>
              </div>
                </div>
              </details>
              <button
                type="submit"
                disabled={!goalReady || !scheduleReady || !scopeReady || busy || phase === "await_confirm"}
                className="flex w-full items-center justify-center gap-2 rounded-[8px] bg-brand-600 py-2.5 text-body-sm font-medium text-white hover:bg-brand-800 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy && <Spinner className="h-4 w-4 border-white/40 border-t-white" />}
                {busy ? "正在规划…" : "生成计划草案"}
              </button>
              {!goalReady && (
                <p className="text-center text-caption text-ink-muted">
                  请先写一句话目标。
                </p>
              )}
              {goalReady && !scheduleReady && (
                <p className="text-center text-caption text-warning-700">
                  请确认截止日期/周期和每天投入时间后再生成计划。
                </p>
              )}
              {goalReady && scheduleReady && !scopeReady && (
                <p className="text-center text-caption text-warning-700">
                  请先选择一次学习范围。
                </p>
              )}
            </form>
          </Card>}

          <Card title="我的计划">
            {listError && (
              <div className="mb-2">
                <ErrorBanner message={listError} onRetry={() => void loadPlans()} />
              </div>
            )}
            {plans === null ? (
              <SkeletonList rows={3} />
            ) : plans.length === 0 ? (
              <p className="py-4 text-center text-caption text-slate-400">
                还没有学习计划
              </p>
            ) : (
              <ul className="flex gap-2 overflow-x-auto pb-1">
                {plans.map((p) => (
                  <li key={p.id} className="min-w-[240px] flex-1">
                    <button
                      type="button"
                      onClick={() => selectPlan(p)}
                      className={`w-full rounded-[9px] border px-3 py-2 text-left text-body-sm transition-colors ${
                        selected?.id === p.id
                          ? "border-brand-300 bg-brand-50 text-brand-700"
                          : "border-line bg-white text-ink-secondary hover:border-brand-200"
                      }`}
                    >
                      <span className="block truncate font-medium">{p.goal}</span>
                      <span className="block text-caption font-normal text-slate-400">
                        {p.status === "active" ? "进行中 · " : p.status === "completed" ? "已完成 · " : "已归档 · "}每日 {p.budget_hours} 小时 · {fmtDateTime(p.created_at)}
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
            <Parts.PlanConfirmCard
              plan={interruptData.plan}
              resources={interruptData.resources ?? []}
              goalAnalysis={interruptData.goal_analysis}
              skillGaps={interruptData.skill_gaps}
              onConfirm={(edited) => resume(true, edited)}
              onCancel={() => resume(false)}
            />
          )}
          {phase === "resuming" && (
            <p className="flex items-center gap-2 text-sm text-slate-500">
              <Spinner /> 正在保存计划…
            </p>
          )}

          {taskBanner && (
            <ErrorBanner message={taskBanner} onClose={() => setTaskBanner(null)} />
          )}

          {selected ? (
            <>
              <Card
                title={
                  <span className="block max-w-100 truncate">{selected.goal}</span>
                }
                extra={
                  <span className="text-caption text-slate-400">
                    {selected.status === "active" ? "进行中 · " : selected.status === "completed" ? "已完成 · " : "已归档 · "}每日 {selected.budget_hours} 小时
                  </span>
                }
              >
                {/* 计划头部：任务粒度进度（分段式，每阶段一段）+ 学情调整入口 */}
                <div className="mb-3 flex flex-wrap items-center gap-3">
                  <div className="min-w-40 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-caption font-medium text-slate-500">
                        已完成 {detail?.progress.done ?? 0} / {detail?.progress.total ?? 0} 个任务
                        {(detail?.progress.skipped ?? 0) > 0 &&
                          ` · 跳过 ${detail?.progress.skipped ?? 0} 个`}
                      </span>
                      <span className="font-mono text-caption text-slate-500">
                        {fmtPct(detail?.progress.effective_ratio ?? detail?.progress.ratio ?? 0)}
                      </span>
                    </div>
                    {detail && detail.structure.stages.length > 0 ? (
                      <div className="mt-1 flex h-2 gap-0.5 overflow-hidden rounded-full">
                        {detail.structure.stages.map((stage, si) => {
                          const tasks = (stage.days ?? []).flatMap(
                            (d) => d.tasks ?? [],
                          );
                          const doneCount = tasks.filter(
                            (task) => Parts.normalizedTaskStatus(task) === "done",
                          ).length;
                          const skippedCount = tasks.filter(
                            (task) => Parts.normalizedTaskStatus(task) === "skipped",
                          ).length;
                          const effectiveTotal = tasks.length - skippedCount;
                          const color = Parts.STAGE_COLORS[si % Parts.STAGE_COLORS.length];
                          return (
                            <div
                              key={si}
                              className="h-full min-w-2 bg-slate-100"
                              style={{ flexGrow: Math.max(tasks.length, 1) }}
                              title={`${stage.name}：完成 ${doneCount} 个，跳过 ${skippedCount} 个`}
                            >
                              <div
                                className={`h-full ${color.bar} transition-all duration-300`}
                                style={{
                                  width: `${
                                    effectiveTotal
                                      ? Math.round((doneCount / effectiveTotal) * 100)
                                      : tasks.length
                                        ? 100
                                        : 0
                                  }%`,
                                }}
                              />
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <div className="mt-1 h-2 rounded-full bg-slate-100">
                        <div
                          className="h-full rounded-full bg-indigo-500 transition-all duration-300"
                          style={{
                            width: `${Math.round((detail?.progress.effective_ratio ?? detail?.progress.ratio ?? 0) * 100)}%`,
                          }}
                        />
                      </div>
                    )}
                  </div>
                  <button
                    type="button"
                    disabled={!detail || detail.status !== "active" || adjusting}
                    onClick={() => void runAdjust()}
                    title="按当前掌握度（p<0.4 的知识点）在最近未完成日插入补强任务"
                    className="flex items-center gap-1.5 rounded-xl border border-amber-200 bg-amber-50 px-3.5 py-2 text-sm font-medium text-amber-700 hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {adjusting && <Spinner className="h-3.5 w-3.5" />}
                    {adjusting ? "正在调整…" : "根据学情调整计划"}
                  </button>
                </div>

                {adjustResult && (
                  <div
                    className={`mb-3 rounded-xl border px-3.5 py-2.5 text-sm ${
                      adjustResult.inserted.length > 0
                        ? "border-amber-300 bg-amber-50"
                        : "border-slate-200 bg-slate-50"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2 text-slate-700">
                      <div className="min-w-0"><ModelMarkdown text={adjustResult.reason} /></div>
                      <button
                        type="button"
                        onClick={() => {
                          setAdjustResult(null);
                          setHighlightKeys(new Set());
                        }}
                        className="shrink-0 text-slate-400 hover:text-slate-600"
                        aria-label="关闭调整结果"
                      >
                        ✕
                      </button>
                    </div>
                    {adjustResult.inserted.length > 0 && (
                      <ul className="mt-2 space-y-1">
                        {adjustResult.inserted.map((t) => (
                          <li
                            key={t.task_id}
                            className="flex flex-wrap items-center gap-2 rounded-lg bg-white px-2.5 py-1.5 text-caption"
                          >
                            <span className="rounded bg-amber-500 px-1.5 py-0.5 font-medium text-white">
                              第 {t.day} 天
                            </span>
                            <span className="font-medium text-slate-700"><MathText text={t.title} /></span>
                            <span className="text-slate-400">{t.minutes} 分钟</span>
                            <span className="text-slate-400">
                              当前掌握度 {fmtPct(t.p_mastery)}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}

                {detail?.structure.goal_analysis && (
                  <details className="mb-3 rounded-xl border border-brand-100 bg-brand-50/30 px-3.5 py-3">
                    <summary className="cursor-pointer text-body-sm font-medium text-brand-700">
                      查看确认时的目标与验收标准
                    </summary>
                    <p className="mt-2 text-caption leading-5 text-ink-secondary">
                      {detail.structure.goal_analysis.summary}
                    </p>
                    <div className="mt-2 grid gap-2 text-body-sm sm:grid-cols-2">
                      <div className="rounded-lg bg-white px-2.5 py-2 text-ink-secondary">
                        <strong className="block text-caption text-ink-muted">可验证成果</strong>
                        {detail.structure.goal_analysis.outcomes.map((item) => (
                          <p key={item} className="mt-1">• {item}</p>
                        ))}
                      </div>
                      <div className="rounded-lg bg-white px-2.5 py-2 text-ink-secondary">
                        <strong className="block text-caption text-ink-muted">验收标准</strong>
                        {detail.structure.goal_analysis.success_criteria.map((item) => (
                          <p key={item} className="mt-1">• {item}</p>
                        ))}
                      </div>
                    </div>
                    {detail.structure.skill_gaps && detail.structure.skill_gaps.length > 0 && (
                      <p className="mt-2 text-body-sm text-warning-700">
                        创建时优先补强：{detail.structure.skill_gaps.map((gap) => `${gap.skill}（${gap.zone}）`).join("、")}
                      </p>
                    )}
                  </details>
                )}

                {detail?.kc_dependency?.levels.length ? (
                  <KcDependencyPath
                    dependency={detail.kc_dependency}
                    onTaskOpen={openDependencyTask}
                  />
                ) : null}

                {detailError && (
                  <div className="mb-3">
                    <ErrorBanner
                      message={detailError}
                      onRetry={() => setDetailKey((k) => k + 1)}
                    />
                  </div>
                )}

                {detail ? (
                  <Parts.PlanCalendar
                    key={detail.id}
                    structure={detail.structure}
                    onStatus={detail.status === "active" ? (taskId, status) => void updateTaskStatus(taskId, status) : undefined}
                    onTaskOpen={(location) => setOpenTask({ ...location, planId: detail.id, planKbId: detail.kb_id, readOnly: detail.status !== "active" })}
                    pendingKeys={pendingKeys}
                    highlightKeys={highlightKeys}
                  />
                ) : detailError ? (
                  <EmptyState title="该计划缺少结构数据" illustration="map" />
                ) : (
                  <SkeletonList rows={4} />
                )}
              </Card>

              {openTask && <PlanTaskDrawer location={openTask} onClose={() => setOpenTask(null)} onCompleted={() => setDetailKey((key) => key + 1)} />}

              <Card
                title="调整历史"
                extra={
                  <span className="text-caption text-slate-400">
                    共 {adjustments.length} 次
                  </span>
                }
              >
                {adjustments.length === 0 ? (
                  <p className="py-3 text-center text-caption text-slate-400">
                    还没有调整记录；低分测评或点击「根据学情调整计划」都会在这里留痕
                  </p>
                ) : (
                  <ol className="ml-1.5 space-y-4 border-l-2 border-slate-100 pl-5">
                    {adjustments.map((a) => {
                      const auto = a.trigger === "low_score_attempt";
                      return (
                        <li key={a.id} className="relative">
                          {/* 时间线节点：低分自动触发=琥珀，手动=靛蓝 */}
                          <span
                            aria-hidden
                            className={`absolute -left-[27px] top-0.5 flex h-4 w-4 items-center justify-center rounded-full ring-4 ring-white ${
                              auto ? "bg-amber-500" : "bg-indigo-500"
                            }`}
                          >
                            <svg
                              viewBox="0 0 24 24"
                              fill="none"
                              stroke="#fff"
                              strokeWidth="3"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              className="h-2.5 w-2.5"
                            >
                              {auto ? (
                                <path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" />
                              ) : (
                                <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5Z" />
                              )}
                            </svg>
                          </span>
                          <div className="rounded-xl bg-slate-50/80 px-3 py-2.5 transition-colors hover:bg-slate-50">
                            <p className="flex flex-wrap items-center gap-2">
                              <span className="text-caption text-slate-400">
                                {fmtDateTime(a.at)}
                              </span>
                              <span
                                className={`rounded-full px-2 py-0.5 text-caption font-medium ${
                                  auto
                                    ? "bg-amber-100/70 text-amber-700"
                                    : "bg-indigo-50 text-indigo-600"
                                }`}
                              >
                                {Parts.TRIGGER_LABELS[a.trigger] ?? a.trigger}
                              </span>
                              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-caption font-medium text-amber-700">
                                插入 {a.inserted.length} 个任务
                              </span>
                            </p>
                            <div className="mt-1 text-sm leading-relaxed text-slate-700">
                              <ModelMarkdown text={a.reason} />
                            </div>
                          </div>
                        </li>
                      );
                    })}
                  </ol>
                )}
              </Card>
            </>
          ) : (
            phase === "idle" &&
            timeline.length === 0 && (
              <EmptyState
                title="选择一个计划查看日历"
                description="或在左侧填写目标，AI 将规划分阶段的每日任务"
                illustration="map"
              />
            )
          )}
        </div>
      </div>
    </div>
  );
}
