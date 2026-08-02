"use client";

import Link from "next/link";
import { useEffect, useRef, type ReactNode } from "react";

import { MathText } from "@/lib/katex";
import type { PlanResource, PlanTask } from "@/lib/types";

export interface PlanTaskLocation {
  task: PlanTask;
  stageName: string;
  day: number;
  planKbId?: string | null;
}

function resourceHref(resource: PlanResource): string | null {
  if (resource.url) return resource.url;
  if (resource.kb_id) return `/kb?kb=${encodeURIComponent(resource.kb_id)}`;
  return null;
}

function ActionLink({ href, children, external = false }: { href: string; children: ReactNode; external?: boolean }) {
  const className = "inline-flex min-h-9 items-center justify-center rounded-[8px] border border-brand-200 bg-brand-50 px-3 text-[11px] font-medium text-brand-700 transition-colors hover:bg-brand-100";
  return external ? (
    <a href={href} target="_blank" rel="noreferrer" className={className}>{children}</a>
  ) : (
    <Link href={href} className={className}>{children}</Link>
  );
}

export function PlanTaskDrawer({ location, onClose }: { location: PlanTaskLocation; onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const { task, stageName, day, planKbId } = location;
  const resource = task.resource ?? null;
  const kbId = resource?.kb_id ?? planKbId ?? null;
  const topic = task.kc_names?.[0] ?? task.title;
  const count = task.practice_count && task.practice_count >= 1 && task.practice_count <= 20 ? task.practice_count : 3;
  const difficulty = task.difficulty && ["easy", "mid", "hard"].includes(task.difficulty) ? task.difficulty : "mid";
  const quizHref = `/quiz?topic=${encodeURIComponent(topic)}${kbId ? `&kb=${encodeURIComponent(kbId)}` : ""}&count=${count}&difficulty=${encodeURIComponent(difficulty)}`;
  const tutorHref = `/tutor?problem=${encodeURIComponent(task.objective || task.title)}${kbId ? `&kb=${encodeURIComponent(kbId)}` : ""}`;
  // The errors UI/API supports kb_id filtering.  A task without a KB uses the
  // existing all-errors page rather than inventing a topic query it cannot read.
  const reviewHref = kbId ? `/errors?kb_id=${encodeURIComponent(kbId)}` : "/errors";
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

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/35 p-0 sm:items-center sm:p-5" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="plan-task-title" className="max-h-[92vh] w-full overflow-y-auto rounded-t-[18px] bg-white p-5 shadow-2xl sm:max-w-[620px] sm:rounded-[16px]">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-[10px] font-medium text-brand-700">{stageName} · 第 {day} 天</p>
            <h2 id="plan-task-title" className="mt-1 text-[18px] font-semibold text-ink"><MathText text={task.title} /></h2>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} className="shrink-0 rounded-[8px] border border-line-strong px-2.5 py-1.5 text-[11px] text-ink-secondary hover:bg-surface-muted" aria-label="关闭任务详情">关闭</button>
        </div>

        <div className="mt-5 grid gap-4 text-[12px] text-ink-secondary">
          <section><h3 className="font-semibold text-ink">学习目标</h3><p className="mt-1 leading-5">{task.objective || "完成本任务并能独立应用相关知识。"}</p></section>
          <section><h3 className="font-semibold text-ink">建议步骤</h3><ol className="mt-1 list-decimal space-y-1 pl-5 leading-5">{(task.instructions?.length ? task.instructions : ["梳理关键概念", "完成练习并检查理解"]).map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ol></section>
          <section><h3 className="font-semibold text-ink">完成标准</h3><p className="mt-1 leading-5">{task.completion_criteria || "能够独立完成任务并解释关键步骤。"}</p></section>
          <div className="flex flex-wrap gap-2 text-[10px] text-ink-muted"><span className="rounded-full bg-surface-muted px-2 py-1">预计 {task.minutes} 分钟</span>{task.kc_names?.map((name) => <span key={name} className="rounded-full bg-brand-50 px-2 py-1 text-brand-700">{name}</span>)}<span className={`rounded-full px-2 py-1 ${task.done ? "bg-success-50 text-success-700" : "bg-warning-50 text-warning-700"}`}>{task.done ? "已完成" : "未完成"}</span></div>
          <section><h3 className="font-semibold text-ink">关联资料</h3>{resource && resourceUrl ? <div className="mt-1 rounded-[8px] border border-line bg-surface-muted/50 p-2.5"><p className="truncate text-ink">{resource.title}</p>{resource.source_type === "kb_chunk" && <p className="mt-1 text-[10px] text-ink-muted">将打开正确知识库；当前资料页不支持精确 chunk 定位。</p>}<div className="mt-2"><ActionLink href={resourceUrl} external={Boolean(resource.url)}>阅读关联资料 →</ActionLink></div></div> : <p className="mt-1 text-ink-muted">本任务没有可用的持久化资料关联。</p>}</section>
        </div>

        <div className="mt-5 flex flex-wrap gap-2 border-t border-line pt-4">
          {showRead && resourceUrl && <ActionLink href={resourceUrl} external={Boolean(resource?.url)}>阅读资料</ActionLink>}
          {showPractice && <ActionLink href={quizHref}>开始针对性练习</ActionLink>}
          {showTutor && <ActionLink href={tutorHref}>进入分级辅导</ActionLink>}
          {showReview && <ActionLink href={reviewHref}>复习相关错题</ActionLink>}
        </div>
      </div>
    </div>
  );
}
