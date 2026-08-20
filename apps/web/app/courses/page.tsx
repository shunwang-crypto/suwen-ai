"use client";

import { ArrowRight, BookOpen, CheckCircle2, Clock3, FileStack, Plus, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState, type FormEvent } from "react";

import { Card, EmptyState, ErrorBanner, SkeletonList, Spinner, fmtDateTime } from "@/components/common";
import { ResourceLibrary } from "@/components/resources/resource-library";
import { ApiError, apiJson } from "@/lib/api";
import type { KbOut } from "@/lib/types";

type CourseSummary = KbOut["summary"];
const ACTION_LABELS: Record<CourseSummary["next_action"], string> = {
  upload_source: "上传资料",
  wait_for_sources: "查看处理进度",
  retry_source: "处理失败，去修复",
  continue_plan: "继续计划",
  create_plan: "制定计划",
};

function actionHref(kb: KbOut) {
  const summary = kb.summary;
  if (summary.next_action === "continue_plan") return `/courses/${kb.id}?tab=plan`;
  if (summary.next_action === "create_plan") return `/plans?create=1&mode=course_bound&kb=${encodeURIComponent(kb.id)}`;
  return `/courses/${kb.id}`;
}

function CourseCard({ kb }: { kb: KbOut }) {
  const summary = kb.summary;
  const planLabel = summary.plan_status === "active"
    ? `${summary.plan_done_tasks}/${summary.plan_total_tasks} 个任务`
    : summary.plan_status === "completed" ? "计划已完成" : "尚未制定计划";
  return (
    <article className="flex h-full min-h-[222px] flex-col rounded-[8px] border border-line bg-white hover:border-brand-200">
      <Link href={`/courses/${kb.id}`} className="flex min-h-0 flex-1 flex-col p-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-300">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[8px] bg-success-50 text-success-700"><BookOpen className="h-4 w-4" aria-hidden /></span>
            <div className="min-w-0"><h2 className="truncate text-[14px] font-semibold text-ink">{kb.name}</h2><p className="mt-0.5 truncate text-caption text-ink-muted">{kb.subject === "general" ? "未设学科" : kb.subject}{kb.owner_type === "org" ? ` · ${kb.org_name ?? "机构课程"}` : ""}{!kb.writable ? " · 只读" : ""}</p></div>
          </div>
          {summary.source_failed > 0 ? <TriangleAlert className="h-4 w-4 shrink-0 text-danger-600" aria-label="有资料处理失败" /> : summary.source_processing > 0 ? <Clock3 className="h-4 w-4 shrink-0 text-warning-600" aria-label="资料处理中" /> : summary.source_ready > 0 ? <CheckCircle2 className="h-4 w-4 shrink-0 text-success-600" aria-label="资料已就绪" /> : null}
        </div>
        <dl className="mt-5 grid grid-cols-2 gap-x-4 gap-y-3 text-body-sm">
          <div><dt className="flex items-center gap-1.5 text-ink-muted"><FileStack className="h-3.5 w-3.5" aria-hidden />资料</dt><dd className="mt-1 font-medium text-ink">{summary.source_ready}/{summary.source_total} 就绪</dd></div>
          <div><dt className="text-ink-muted">已核验题目</dt><dd className="mt-1 font-medium text-ink">{summary.verified_questions} 道</dd></div>
          <div className="col-span-2"><dt className="flex justify-between gap-3 text-ink-muted"><span>学习计划</span><span>{planLabel}</span></dt><dd className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-muted"><span className="block h-full rounded-full bg-brand-600" style={{ width: `${Math.round(summary.plan_progress_ratio * 100)}%` }} /></dd></div>
        </dl>
        <p className="mt-auto pt-4 text-caption text-ink-muted">{summary.last_activity_at ? `最近活动 ${fmtDateTime(summary.last_activity_at)}` : "还没有学习活动"}{summary.source_processing > 0 ? ` · ${summary.source_processing} 份处理中` : ""}{summary.source_failed > 0 ? ` · ${summary.source_failed} 份失败` : ""}</p>
      </Link>
      <Link href={actionHref(kb)} className="flex h-10 items-center justify-between border-t border-line px-4 text-body-sm font-medium text-brand-700 hover:bg-brand-50/50">{ACTION_LABELS[summary.next_action]}<ArrowRight className="h-4 w-4" aria-hidden /></Link>
    </article>
  );
}

function CoursesView() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [kbs, setKbs] = useState<KbOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newSubject, setNewSubject] = useState("");
  const [creating, setCreating] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const showUnclassified = searchParams.get("view") === "unclassified";

  useEffect(() => {
    let alive = true;
    void apiJson<KbOut[]>("/api/kb").then((list) => { if (alive) { setKbs(list); setError(null); } }).catch((err: unknown) => { if (alive) { setKbs((prev) => prev ?? []); setError(err instanceof ApiError ? err.message : "课程列表加载失败"); } });
    return () => { alive = false; };
  }, [reloadKey]);

  const orderedKbs = useMemo(() => [...(kbs ?? [])].sort((a, b) => (b.summary.last_activity_at ?? b.created_at).localeCompare(a.summary.last_activity_at ?? a.created_at)), [kbs]);
  const createCourse = async (event: FormEvent) => {
    event.preventDefault();
    if (!newName.trim() || creating) return;
    setCreating(true); setBanner(null);
    try { const kb = await apiJson<KbOut>("/api/kb", { body: { name: newName.trim(), subject: newSubject.trim() || "general" } }); setNewName(""); setNewSubject(""); setShowCreate(false); router.push(`/courses/${kb.id}`); }
    catch (err) { setBanner(err instanceof ApiError ? err.message : "新建课程失败"); }
    finally { setCreating(false); }
  };

  return (
    <div className="mx-auto w-full max-w-[1120px] space-y-5 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      {error && <ErrorBanner message={error} onRetry={() => setReloadKey((key) => key + 1)} />}{banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}
      <header className="flex items-end justify-between gap-4"><div><h1 className="text-[22px] font-semibold text-ink">课程</h1><p className="mt-1 text-body-sm text-ink-muted">按资料范围查看学习状态与下一步。</p></div><button type="button" onClick={() => setShowCreate((value) => !value)} className="inline-flex h-9 items-center gap-2 rounded-[8px] bg-brand-600 px-4 text-body-sm font-medium text-white hover:bg-brand-800"><Plus className="h-4 w-4" aria-hidden />新建课程</button></header>
      {showCreate && <form onSubmit={createCourse} className="grid gap-2 border-y border-line bg-surface-muted/60 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_220px_auto]"><input value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={255} placeholder="课程名称" className="h-9 rounded-[8px] border border-line-strong bg-white px-3 text-body-sm outline-none focus:border-brand-400" /><input value={newSubject} onChange={(e) => setNewSubject(e.target.value)} maxLength={64} placeholder="学科（可选）" className="h-9 rounded-[8px] border border-line-strong bg-white px-3 text-body-sm outline-none focus:border-brand-400" /><button type="submit" disabled={!newName.trim() || creating} className="inline-flex h-9 items-center justify-center gap-1.5 rounded-[8px] bg-ink px-4 text-body-sm font-medium text-white disabled:opacity-40">{creating && <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />}创建</button></form>}
      {showUnclassified ? <ResourceLibrary scopeKbId="unclassified" /> : kbs === null ? <Card title="课程"><SkeletonList rows={4} /></Card> : orderedKbs.length === 0 ? <EmptyState title="建立第一门课程" description="创建课程后上传自己的教材、讲义或笔记。" illustration="docs"><button type="button" onClick={() => setShowCreate(true)} className="mt-2 inline-flex h-9 items-center gap-2 rounded-[8px] bg-brand-600 px-4 text-body-sm font-medium text-white"><Plus className="h-4 w-4" aria-hidden />新建课程</button></EmptyState> : <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{orderedKbs.map((kb) => <li key={kb.id}><CourseCard kb={kb} /></li>)}</ul>}
      <p className="pt-1 text-center text-caption text-ink-muted"><Link href="/practice?tab=errors&filter=unclassified" className="text-brand-700 underline-offset-2 hover:underline">查看未归类错题</Link></p>
    </div>
  );
}

export default function CoursesPage() {
  return (
    <Suspense fallback={<div className="mx-auto w-full max-w-[1120px] px-5 py-6" />}>
      <CoursesView />
    </Suspense>
  );
}
