"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type { PlanResource, PlanTask, PlanToggleResult } from "@/lib/types";

export interface PlanTaskLocation {
  task: PlanTask;
  stageName: string;
  day: number;
  planId?: string;
  planKbId?: string | null;
}

function resourceHref(resource: PlanResource): string | null {
  if (resource.url) return resource.url;
  if (resource.kb_id) return `/kb?kb=${encodeURIComponent(resource.kb_id)}`;
  return null;
}

function ActionLink({ href, children, external = false }: { href: string; children: ReactNode; external?: boolean }) {
  const className = "inline-flex min-h-9 items-center justify-center rounded-[8px] border border-brand-200 bg-brand-50 px-3 text-body-sm font-medium text-brand-700 transition-colors hover:bg-brand-100";
  return external ? (
    <a href={href} target="_blank" rel="noreferrer" className={className}>{children}</a>
  ) : (
    <Link href={href} className={className}>{children}</Link>
  );
}

export function PlanTaskDrawer({
  location,
  onClose,
  onCompleted,
}: {
  location: PlanTaskLocation;
  onClose: () => void;
  onCompleted?: (result: PlanToggleResult) => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const { task, stageName, day, planId, planKbId } = location;
  const [completing, setCompleting] = useState(false);
  const [completionError, setCompletionError] = useState<string | null>(null);
  const [locallyDone, setLocallyDone] = useState(Boolean(task.done));
  const resource = task.resource ?? null;
  const kbId = resource?.kb_id ?? planKbId ?? null;
  const topic = task.kc_names?.[0] ?? task.title;
  const count = task.practice_count && task.practice_count >= 1 && task.practice_count <= 20 ? task.practice_count : 3;
  const difficulty = task.difficulty && ["easy", "mid", "hard"].includes(task.difficulty) ? task.difficulty : "mid";
  const attribution = planId && task.task_key
    ? `&plan_id=${encodeURIComponent(planId)}&task_key=${encodeURIComponent(task.task_key)}`
    : "";
  const quizHref = `/quiz?topic=${encodeURIComponent(topic)}${kbId ? `&kb=${encodeURIComponent(kbId)}` : ""}&count=${count}&difficulty=${encodeURIComponent(difficulty)}${attribution}`;
  const tutorHref = planId && task.task_key
    ? `/tutor?plan_id=${encodeURIComponent(planId)}&task_key=${encodeURIComponent(task.task_key)}${kbId ? `&kb=${encodeURIComponent(kbId)}` : ""}`
    : `/tutor${kbId ? `?kb=${encodeURIComponent(kbId)}` : ""}`;
  const reviewHref = attribution ? `/reviews?${attribution.slice(1)}` : "/reviews";
  const resourceUrl = resource ? resourceHref(resource) : null;
  const action = task.action_type ?? "tutor";

  useEffect(() => {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        "button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex='-1'])",
      ));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      openerRef.current?.focus();
    };
  }, [onClose]);

  const showPractice = action === "practice" || action === "mixed";
  const showTutor = action === "tutor" || action === "mixed" || (!resource && action === "read");
  const showReview = action === "review_errors" || action === "mixed";
  const showRead = Boolean(resourceUrl) && (action === "read" || action === "mixed");

  const completeReading = async () => {
    if (!planId || !task.task_key || completing) return;
    setCompleting(true);
    setCompletionError(null);
    try {
      const result = await apiJson<PlanToggleResult>(
        `/api/plans/${encodeURIComponent(planId)}/tasks/${encodeURIComponent(task.task_key)}/complete`,
        { method: "POST", body: {} },
      );
      setLocallyDone(true);
      onCompleted?.(result);
    } catch (error) {
      setCompletionError(error instanceof ApiError ? error.message : "任务完成状态保存失败，请重试");
    } finally {
      setCompleting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/35 p-0 sm:items-center sm:p-5" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="plan-task-title" className="max-h-[92vh] w-full overflow-y-auto rounded-t-[18px] bg-white p-5 shadow-2xl sm:max-w-[620px] sm:rounded-[16px]">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-caption font-medium text-brand-700">{stageName} · 第 {day} 天</p>
            <h2 id="plan-task-title" className="mt-1 text-[18px] font-semibold text-ink"><MathText text={task.title} /></h2>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} className="shrink-0 rounded-[8px] border border-line-strong px-2.5 py-1.5 text-body-sm text-ink-secondary hover:bg-surface-muted" aria-label="关闭任务详情">关闭</button>
        </div>

        <div className="mt-5 grid gap-4 text-body-sm text-ink-secondary">
          <section><h3 className="font-semibold text-ink">学习目标</h3><div className="mt-1 leading-5"><MathText text={task.objective || "完成本任务并能独立应用相关知识。"} /></div></section>
          <section><h3 className="font-semibold text-ink">建议步骤</h3><ol className="mt-1 list-decimal space-y-1 pl-5 leading-5">{(task.instructions?.length ? task.instructions : ["梳理关键概念", "完成练习并检查理解"]).map((item, index) => <li key={`${item}-${index}`}><MathText text={item} /></li>)}</ol></section>
          <section><h3 className="font-semibold text-ink">完成标准</h3><div className="mt-1 leading-5"><MathText text={task.completion_criteria || "能够独立完成任务并解释关键步骤。"} /></div></section>
          <div className="flex flex-wrap gap-2 text-caption text-ink-muted"><span className="rounded-full bg-surface-muted px-2 py-1">预计 {task.minutes} 分钟</span>{task.kc_names?.map((name) => <span key={name} className="rounded-full bg-brand-50 px-2 py-1 text-brand-700">{name}</span>)}<span className={`rounded-full px-2 py-1 ${task.status === "skipped" && !locallyDone ? "bg-surface-muted text-ink-secondary" : locallyDone ? "bg-success-50 text-success-700" : "bg-warning-50 text-warning-700"}`}>{task.status === "skipped" && !locallyDone ? "已跳过" : locallyDone ? "已完成" : "未完成"}</span></div>
          <section><h3 className="font-semibold text-ink">关联资料</h3>{resource && resourceUrl ? <div className="mt-1 rounded-[8px] border border-line bg-surface-muted/50 p-2.5"><p className="truncate text-ink">{resource.title}</p>{resource.source_type === "kb_chunk" && <p className="mt-1 text-caption text-ink-muted">将打开正确知识库；当前资料页不支持精确 chunk 定位。</p>}<div className="mt-2"><ActionLink href={resourceUrl} external={Boolean(resource.url)}>阅读关联资料 →</ActionLink></div></div> : <p className="mt-1 text-ink-muted">本任务没有可用的持久化资料关联。</p>}</section>
        </div>

        <div className="mt-5 flex flex-wrap gap-2 border-t border-line pt-4">
          {task.locked && (task.lock_blockers?.length ?? 0) > 0 && (
            <p className="w-full rounded-[8px] border border-warning-200 bg-warning-50 px-3 py-2 text-body-sm leading-5 text-warning-700">
              先修知识点尚未掌握：
              {task.lock_blockers!.map((b) => `${b.kc_name}（${b.zone}）`).join("、")}。
              建议先补齐再做本任务；仍可继续，由你决定。
            </p>
          )}
          {showRead && resourceUrl && <ActionLink href={resourceUrl} external={Boolean(resource?.url)}>阅读资料</ActionLink>}
          {showPractice && <ActionLink href={quizHref}>开始针对性练习</ActionLink>}
          {showTutor && <ActionLink href={tutorHref}>进入分级辅导</ActionLink>}
          {showReview && <ActionLink href={reviewHref}>复习相关错题</ActionLink>}
          {showRead && planId && task.task_key && !locallyDone && <button type="button" disabled={completing} onClick={() => void completeReading()} className="inline-flex min-h-9 items-center justify-center rounded-[8px] bg-brand-600 px-3 text-body-sm font-medium text-white transition-colors hover:bg-brand-800 disabled:cursor-not-allowed disabled:opacity-60">{completing ? "保存中…" : "我已阅读，标记完成"}</button>}
        </div>
        {completionError && <p role="alert" className="mt-3 text-body-sm text-danger-700">{completionError}</p>}
      </div>
    </div>
  );
}
