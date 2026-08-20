"use client";

// 学习计划（F3）：发起表单 → SSE 流 + interrupt 计划确认（阶段树预览）→ 确认落库；
// 计划列表 + 日历视图（stages/days/tasks 渲染为周网格卡片）。
// 「计划是活的」：任务复选框（乐观更新+失败回滚）、头部进度条、
// 「根据学情调整计划」插入补强任务并高亮、调整历史时间线。

import {
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";

import { useRouter, useSearchParams } from "next/navigation";

import {
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  SkeletonList,
  Spinner,
  fmtDateTime,
} from "@/components/common";
import { FlowTimeline, type FlowItem } from "@/components/flow-timeline";
import { PlanDetailPanel } from "@/components/plans/plan-detail-panel";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import {
  PLAN_GOAL_EXAMPLES,
  isPlanGoalReady,
  parsePlanIntent,
  planGoalReadiness,
} from "@/lib/plan-goal";
import { streamSse } from "@/lib/sse";
import type {ErrorEventData, GraphDoneData, GraphInterruptData, GraphTraceData, KbOut, NodeUpdateData, PlanListItem, PlanMode, PlanStructure} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";
import * as Parts from "@/components/plans/plan-parts";

function PlansView() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user } = useAuth();
  const userId = user?.id ?? "anonymous";
  const requestedPlanId = searchParams.get("id");
  const requestedMode = searchParams.get("mode");
  const requestedCreate = searchParams.get("create");
  const requestedKbId = searchParams.get("kb");
  const [plans, setPlans] = useState<PlanListItem[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<PlanListItem | null>(null);

  // 表单
  const [goal, setGoal] = useState("");
  const [currentLevel, setCurrentLevel] = useState("");
  const [targetOutcome, setTargetOutcome] = useState("");
  const [successCriteria, setSuccessCriteria] = useState("");
  const [planMode, setPlanMode] = useState<PlanMode>(
    requestedMode === "course_bound" ? "course_bound" : "goal_only",
  );
  const [kbId, setKbId] = useState(requestedKbId ?? "");
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

  // 计划列表只有一个请求入口；reload 会使旧响应失效（latest-wins）。
  const plansListRes = useApiResource<PlanListItem[]>("/api/plans", {
    errorMessage: "计划列表加载失败",
  });
  const reloadPlans = plansListRes.reload;
  const kbListRes = useApiResource<KbOut[]>("/api/kb");
  const kbs = kbListRes.data ?? [];
  const preferredPlanIdRef = useRef<string | null>(null);

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 选中项/错误横幅的单向同步, 无纯派生替代 */
  useEffect(() => {
    const list = plansListRes.data;
    if (!list) return;
    setPlans(list);
    // ?id= 深链解析（实施指令 5.5）：有 kb 的计划 canonical 位置是课程计划 tab，
    // 页内 client-side replace（需查列表数据，不做服务端重定向）。
    const requested = requestedPlanId
      ? (list.find((plan) => plan.id === requestedPlanId) ?? null)
      : null;
    if (requested?.kb_id && requested.plan_mode === "course_bound") {
      router.replace(`/courses/${requested.kb_id}?tab=plan`);
      return;
    }
    setSelected((current) => {
      const preferredId = preferredPlanIdRef.current;
      const preferred = preferredId
        ? (list.find((plan) => plan.id === preferredId) ?? null)
        : null;
      if (preferred) preferredPlanIdRef.current = null;
      return requested ?? preferred ?? list.find((plan) => plan.id === current?.id) ?? list[0] ?? null;
    });
  }, [plansListRes.data, requestedPlanId, router]);

  useEffect(() => {
    setListError(plansListRes.error);
    if (plansListRes.error) setPlans((prev) => prev ?? []);
  }, [plansListRes.error]);

  // URL entry points are explicit: Today/onboarding can open the goal-only
  // flow without requiring a hidden course selection.
  useEffect(() => {
    if (requestedMode === "course_bound" || requestedMode === "goal_only") {
      setPlanMode(requestedMode);
    }
    if (requestedMode === "course_bound" && requestedKbId) {
      setKbId(requestedKbId);
    }
    if (requestedCreate === "1") setShowCreate(true);
  }, [requestedCreate, requestedKbId, requestedMode]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // 恢复本地待确认/生成中的计划草稿（含中断确认态）
  /* eslint-disable react-hooks/set-state-in-effect -- localStorage 草稿 → 表单/阶段的单向恢复, 无纯派生替代 */
  useEffect(() => {
    if (requestedCreate === "1" && (requestedMode || requestedKbId)) return;
    const pending = Parts.readPendingPlan(userId);
    if (!pending) return;
    setGoal(pending.form.goal);
    setCurrentLevel(pending.form.currentLevel ?? "");
    setTargetOutcome(pending.form.targetOutcome ?? "");
    setSuccessCriteria(pending.form.successCriteria ?? "");
    setPlanMode(pending.form.planMode ?? "goal_only");
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
  }, [requestedCreate, requestedKbId, requestedMode, userId]);
  /* eslint-enable react-hooks/set-state-in-effect */

  useEffect(() => {
    if (phase !== "streaming" && phase !== "resuming") return;
    const timer = setInterval(() => {
      const pending = Parts.readPendingPlan(userId);
      if (!pending) {
        setPhase("idle");
        reloadPlans();
        return;
      }
      if (pending.status === "await_confirm" && pending.interrupt) {
        setInterruptData(pending.interrupt);
        setPhase("await_confirm");
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [phase, reloadPlans, userId]);

  /** 切换选中计划：有 kb 的计划点击直达课程计划 tab；全局计划留在本页看详情。 */
  const selectPlan = (plan: PlanListItem | null) => {
    if (plan?.kb_id) {
      router.push(`/courses/${plan.kb_id}?tab=plan`);
      return;
    }
    setSelected(plan);
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
                    planMode,
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
                preferredPlanIdRef.current = planId;
                reloadPlans();
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
      reloadPlans,
      successCriteria,
      targetOutcome,
      totalDays,
      userId,
      planMode,
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
    if (planMode === "course_bound" && !kbId) {
      setFlowError("请先选择这次计划使用的知识库范围。");
      return;
    }
    const effectiveKbId = planMode === "course_bound" ? kbId : "";
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
        planMode,
        kbId: effectiveKbId,
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
      plan_mode: planMode,
      kb_id: effectiveKbId || undefined,
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
      planMode,
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
  const scopeReady = planMode === "goal_only" || Boolean(kbId);

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
              <fieldset className="rounded-xl border border-line bg-white p-3">
                <legend className="px-1 text-body-sm font-semibold text-ink">计划来源</legend>
                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                  <button
                    type="button"
                    aria-pressed={planMode === "course_bound"}
                    onClick={() => setPlanMode("course_bound")}
                    className={`rounded-lg border px-3 py-2 text-left transition-colors ${planMode === "course_bound" ? "border-brand-400 bg-brand-50 text-brand-700" : "border-line-strong text-ink-secondary hover:border-brand-300"}`}
                  >
                    <span className="block text-body-sm font-semibold">基于课程资料</span>
                    <span className="mt-0.5 block text-caption text-ink-muted">任务、练习和掌握度都限定在选定课程内</span>
                  </button>
                  <button
                    type="button"
                    aria-pressed={planMode === "goal_only"}
                    onClick={() => { setPlanMode("goal_only"); setKbId(""); }}
                    className={`rounded-lg border px-3 py-2 text-left transition-colors ${planMode === "goal_only" ? "border-brand-400 bg-brand-50 text-brand-700" : "border-line-strong text-ink-secondary hover:border-brand-300"}`}
                  >
                    <span className="block text-body-sm font-semibold">暂不绑定资料</span>
                    <span className="mt-0.5 block text-caption text-ink-muted">先做目标拆解和基线练习，不更新正式掌握度</span>
                  </button>
                </div>
              </fieldset>
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
              {planMode === "course_bound" && (
                <div className="rounded-xl border border-brand-100 bg-brand-50/40 p-3" aria-label="选择学习范围">
                  <p className="text-body-sm font-medium text-ink">这次计划学习哪一份资料？</p>
                  <p className="mt-1 text-caption text-ink-muted">请选择一次范围，其他背景和成果仍可稍后补充。</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {kbs.map((kb) => (
                      <button type="button" key={kb.id} aria-pressed={kbId === kb.id} onClick={() => setKbId(kb.id)} className={`rounded-full border px-3 py-1.5 text-body-sm font-medium ${kbId === kb.id ? "border-brand-500 bg-brand-600 text-white" : "border-brand-200 bg-white text-brand-700 hover:border-brand-400"}`}>
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
                <ErrorBanner message={listError} onRetry={reloadPlans} />
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
                        <span className="mt-1 flex flex-wrap items-center gap-1.5">
                          <span>{p.status === "active" ? "进行中" : p.status === "completed" ? "已完成" : "已归档"}</span>
                          <span className={`rounded-full px-1.5 py-0.5 ${p.plan_mode === "course_bound" ? "bg-success-50 text-success-700" : "bg-warning-50 text-warning-700"}`}>{p.plan_mode === "course_bound" ? "课程资料" : "目标起步"}</span>
                          <span>· 每日 {p.budget_hours} 小时 · {fmtDateTime(p.created_at)}</span>
                        </span>
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
              planMode={interruptData.plan_mode === "course_bound" ? "course_bound" : planMode}
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

          {selected ? (
            <PlanDetailPanel
              planId={selected.id}
              goal={selected.goal}
              status={selected.status}
              budgetHours={selected.budget_hours}
              onChanged={reloadPlans}
            />
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

export default function PlansPage() {
  return (
    <Suspense fallback={<div className="mx-auto w-full max-w-[1180px] px-5 py-6" />}>
      <PlansView />
    </Suspense>
  );
}
