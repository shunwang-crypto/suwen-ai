"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { EmptyState, ErrorBanner, SkeletonList } from "@/components/common";
import { AssessmentPanel } from "@/components/resources/assessment-panel";
import { ResourceReview } from "@/components/resources/resource-review";
import { ResourceUploadDialog } from "@/components/resources/resource-upload-dialog";
import { SourcePreview } from "@/components/resources/source-preview";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ApiError, apiJson } from "@/lib/api";
import type {
  LearningResourceOut,
  ResourceQuestionOut,
} from "@/lib/types";

type ResourceFilter = "all" | "questions" | "reference" | "archived";

const RESOURCE_TYPE_LABELS: Record<string, string> = {
  textbook: "教材",
  lecture: "讲义",
  notes: "笔记",
  exam_paper: "真实试卷",
  mock_paper: "模拟卷",
  workbook: "练习册",
  worksheet: "题单 / 练习纸",
  homework: "作业",
  single_question: "单题来源",
  other: "其他题目资料",
};

const STATUS_LABELS: Record<string, string> = {
  uploaded: "待开始识别",
  extracting: "识别中",
  reviewing: "待校对",
  ready: "已就绪",
  failed: "识别失败",
  archived: "已归档",
};

const STATUS_STYLES: Record<string, string> = {
  uploaded: "bg-surface-muted text-ink-secondary",
  extracting: "bg-info-50 text-info-700",
  reviewing: "bg-warning-50 text-warning-700",
  ready: "bg-success-50 text-success-700",
  failed: "bg-danger-50 text-danger-700",
  archived: "bg-surface-muted text-ink-muted",
};

function typeLabel(type: string): string {
  return RESOURCE_TYPE_LABELS[type] ?? "其他题目资料";
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium" }).format(new Date(value));
}

function isQuestionResource(resource: LearningResourceOut): boolean {
  return resource.retrieval_policy === "questions_only" && !["textbook", "lecture", "notes"].includes(resource.resource_type);
}

function isReferenceResource(resource: LearningResourceOut): boolean {
  return !isQuestionResource(resource);
}

function friendlyError(reason: unknown, fallback: string): string {
  if (reason instanceof ApiError) {
    const messages: Record<string, string> = {
      RESOURCE_DELETE_FAILED: "原文件删除失败，资料保留为可重试状态。",
      RESOURCE_HAS_ERROR_ITEMS: "这份资料已有错题记录，请先归档。",
      RESOURCE_HAS_ASSESSMENTS: "这份资料已有真实作答记录，请先归档，避免破坏历史。",
      RESOURCE_HAS_ATTEMPTS: "这份资料已有历史作答，不能直接删除。",
      RESOURCE_ARCHIVED: "资料已经归档，请先恢复后再操作。",
      EXTRACTION_QUEUE_FAILED: "识别任务入队失败，可以稍后重试。",
      RESOURCE_SOURCE_REQUIRED: "手动资料没有原文件，请直接添加题目。",
    };
    return messages[reason.code] ?? reason.message;
  }
  return reason instanceof Error ? reason.message : fallback;
}

function ResourceIcon({ resource }: { resource: LearningResourceOut }) {
  const label = resource.source_filename?.slice(resource.source_filename.lastIndexOf(".") + 1).toUpperCase() || "题";
  return <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-[10px] text-[10px] font-semibold ${isQuestionResource(resource) ? "bg-brand-50 text-brand-700" : "bg-success-50 text-success-700"}`}>{label.slice(0, 4)}</span>;
}

function ResourceCard({
  resource,
  onOpen,
  onArchive,
  onRestore,
  onDelete,
  onExtract,
  questionResource,
  busy,
}: {
  resource: LearningResourceOut;
  onOpen: () => void;
  onArchive: () => void;
  onRestore: () => void;
  onDelete: () => void;
  onExtract: () => void;
  questionResource: boolean;
  busy: boolean;
}) {
  return (
    <article className="rounded-[13px] border border-line bg-white p-4 transition-shadow hover:shadow-card">
      <div className="flex items-start gap-3">
        <ResourceIcon resource={resource} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5"><h2 className="truncate text-[13px] font-semibold text-ink">{resource.title}</h2><span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${STATUS_STYLES[resource.status] ?? "bg-surface-muted text-ink-secondary"}`}>{statusLabel(resource.status)}</span></div>
          <p className="mt-1 text-[10px] text-ink-muted">{typeLabel(resource.resource_type)} · {resource.subject || "通用"} · {formatDate(resource.created_at)}</p>
        </div>
      </div>
      <div className="mt-4 grid grid-cols-3 gap-2 rounded-[9px] bg-surface-muted px-3 py-2.5 text-[10px]"><div><p className="text-ink-muted">题目</p><p className="mt-0.5 font-semibold text-ink">{resource.question_count}</p></div><div><p className="text-ink-muted">待校对</p><p className="mt-0.5 font-semibold text-warning-700">{resource.pending_question_count}</p></div><div><p className="text-ink-muted">来源</p><p className="mt-0.5 truncate font-medium text-ink" title={resource.source_filename ?? undefined}>{resource.source_filename ?? "手动创建"}</p></div></div>
      {resource.status === "failed" && typeof resource.metadata.error === "string" && <p className="mt-3 rounded-[8px] bg-danger-50 px-3 py-2 text-[10px] leading-4 text-danger-700">识别未完成：{resource.metadata.error}</p>}
      <footer className="mt-4 flex flex-wrap items-center justify-between gap-2"><button type="button" onClick={onOpen} className="rounded-[8px] bg-ink px-3.5 py-2 text-[11px] font-medium text-white">{questionResource && resource.status === "extracting" ? "查看识别进度" : "查看资料"}</button><div className="flex flex-wrap gap-1.5">{questionResource && (resource.status === "uploaded" || resource.status === "failed") && resource.source_url && <button type="button" onClick={onExtract} disabled={busy} className="rounded-[8px] border border-line-strong px-2.5 py-2 text-[10px] text-ink-secondary disabled:opacity-40">{busy ? "提交中…" : resource.status === "failed" ? "重试识别" : "开始识别"}</button>}{resource.status === "archived" ? <button type="button" onClick={onRestore} disabled={busy} className="rounded-[8px] border border-line-strong px-2.5 py-2 text-[10px] text-ink-secondary disabled:opacity-40">恢复</button> : <button type="button" onClick={onArchive} disabled={busy} className="rounded-[8px] border border-line-strong px-2.5 py-2 text-[10px] text-ink-secondary disabled:opacity-40">归档</button>}{(resource.status === "uploaded" || resource.status === "failed" || resource.status === "archived") && <button type="button" onClick={onDelete} disabled={busy} className="rounded-[8px] border border-line-strong px-2.5 py-2 text-[10px] text-danger-600 disabled:opacity-40">删除</button>}</div></footer>
    </article>
  );
}

function ReferenceResourceView({
  resource,
  onBack,
}: {
  resource: LearningResourceOut;
  onBack: () => void;
}) {
  return (
    <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-6 sm:px-8 lg:px-12 lg:py-8">
      <button type="button" onClick={onBack} className="text-[11px] text-ink-secondary hover:text-ink">← 返回题目资料</button>
      <header className="rounded-[13px] border border-line bg-white p-5">
        <p className="text-[10px] font-medium uppercase tracking-[0.1em] text-ink-muted">参考资料</p>
        <h1 className="mt-1.5 text-[22px] font-semibold tracking-[-0.03em] text-ink">{resource.title}</h1>
        <p className="mt-2 text-[11px] leading-5 text-ink-secondary">这是历史参考资料。请在知识库中管理教材、讲义和笔记；参考资料不会进入题目识别、真实作答或错题选择流程。</p>
      </header>
      <SourcePreview resource={resource} />
    </div>
  );
}

export default function ResourcesPage() {
  const [resources, setResources] = useState<LearningResourceOut[] | null>(null);
  const [selectedResource, setSelectedResource] = useState<LearningResourceOut | null>(null);
  const [questions, setQuestions] = useState<ResourceQuestionOut[]>([]);
  const [selectedLoading, setSelectedLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [filter, setFilter] = useState<ResourceFilter>("all");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [assessmentOpen, setAssessmentOpen] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<LearningResourceOut | null>(null);

  const loadResources = useCallback(async () => {
    try {
      const list = await apiJson<LearningResourceOut[]>("/api/resources");
      setResources(list);
      setListError(null);
    } catch (reason) {
      setListError(friendlyError(reason, "题目资料加载失败"));
      setResources((previous) => previous ?? []);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadResources(), 0);
    return () => window.clearTimeout(timer);
  }, [loadResources]);

  const loadResource = useCallback(async (resourceId: string) => {
    setSelectedLoading(true);
    setBanner(null);
    try {
      const resource = await apiJson<LearningResourceOut>(`/api/resources/${resourceId}`);
      const resourceQuestions = isQuestionResource(resource)
        ? await apiJson<ResourceQuestionOut[]>(`/api/resources/${resourceId}/questions`)
        : [];
      setSelectedResource(resource);
      setQuestions(resourceQuestions);
      setResources((previous) => previous?.map((item) => item.id === resource.id ? resource : item) ?? previous);
    } catch (reason) {
      setBanner(friendlyError(reason, "题目资料详情加载失败"));
    } finally {
      setSelectedLoading(false);
    }
  }, []);

  const openResource = (resource: LearningResourceOut) => {
    setSelectedResource(resource);
    setQuestions([]);
    void loadResource(resource.id);
  };

  const reloadSelected = useCallback(async () => {
    if (!selectedResource) return;
    await loadResource(selectedResource.id);
  }, [loadResource, selectedResource]);

  useEffect(() => {
    if (!selectedResource || !["uploaded", "extracting"].includes(selectedResource.status)) return;
    const timer = window.setInterval(() => {
      void loadResource(selectedResource.id);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [loadResource, selectedResource]);

  const runExtract = async (resource: LearningResourceOut) => {
    setBusyId(resource.id);
    setBanner(null);
    try {
      await apiJson(`/api/resources/${resource.id}/extract`, { method: "POST", body: {} });
      await loadResources();
      if (selectedResource?.id === resource.id) await loadResource(resource.id);
    } catch (reason) {
      setBanner(friendlyError(reason, "识别任务提交失败"));
    } finally {
      setBusyId(null);
    }
  };

  const archive = async (resource: LearningResourceOut) => {
    setBusyId(resource.id);
    setBanner(null);
    try {
      const updated = await apiJson<LearningResourceOut>(`/api/resources/${resource.id}/archive`, { method: "POST", body: {} });
      setResources((previous) => previous?.map((item) => item.id === resource.id ? { ...item, ...updated } : item) ?? previous);
      if (selectedResource?.id === resource.id) setSelectedResource((previous) => previous ? { ...previous, ...updated } : previous);
    } catch (reason) {
      setBanner(friendlyError(reason, "归档失败"));
    } finally {
      setBusyId(null);
    }
  };

  const restore = async (resource: LearningResourceOut) => {
    setBusyId(resource.id);
    setBanner(null);
    try {
      const updated = await apiJson<LearningResourceOut>(`/api/resources/${resource.id}/restore`, { method: "POST", body: {} });
      setResources((previous) => previous?.map((item) => item.id === resource.id ? { ...item, ...updated } : item) ?? previous);
      if (selectedResource?.id === resource.id) setSelectedResource((previous) => previous ? { ...previous, ...updated } : previous);
    } catch (reason) {
      setBanner(friendlyError(reason, "恢复失败"));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (resource: LearningResourceOut) => {
    setBusyId(resource.id);
    setBanner(null);
    try {
      await apiJson<void>(`/api/resources/${resource.id}`, { method: "DELETE" });
      setResources((previous) => previous?.filter((item) => item.id !== resource.id) ?? previous);
      if (selectedResource?.id === resource.id) {
        setSelectedResource(null);
        setQuestions([]);
      }
      setDeleteTarget(null);
    } catch (reason) {
      setBanner(friendlyError(reason, "删除失败"));
    } finally {
      setBusyId(null);
    }
  };

  const requestDelete = (resource: LearningResourceOut) => setDeleteTarget(resource);

  const handleCreated = async (resource: LearningResourceOut) => {
    setUploadOpen(false);
    setBanner(resource.source_url ? "原文件已保存，正在启动异步识别…" : "资料已创建，可以手动添加题目。");
    setSelectedResource(resource);
    setQuestions([]);
    if (resource.source_url && isQuestionResource(resource)) {
      try {
        await apiJson(`/api/resources/${resource.id}/extract`, { method: "POST", body: {} });
      } catch (reason) {
      setBanner(friendlyError(reason, "题目资料已创建，但识别任务提交失败，请在列表中重试。"));
      }
    }
    await loadResources();
    await loadResource(resource.id);
  };

  const filteredResources = useMemo(() => (resources ?? []).filter((resource) => {
    if (filter === "archived") return resource.status === "archived";
    if (resource.status === "archived") return false;
    if (filter === "questions") return isQuestionResource(resource);
    if (filter === "reference") return !isQuestionResource(resource);
    return true;
  }), [filter, resources]);

  if (selectedResource) {
    if (selectedLoading && questions.length === 0) {
      return <div className="mx-auto w-full max-w-[1480px] space-y-4 px-5 py-6 sm:px-8 lg:px-12"><div className="rounded-[13px] border border-line bg-white p-5"><SkeletonList rows={6} /></div></div>;
    }
    if (isReferenceResource(selectedResource)) {
      return <ReferenceResourceView resource={selectedResource} onBack={() => { setSelectedResource(null); void loadResources(); }} />;
    }
    return <><ResourceReview key={`${selectedResource.id}:${questions.map((question) => `${question.id}:${question.verification_status}`).join("|")}`} resource={selectedResource} questions={questions} onBack={() => { setSelectedResource(null); setAssessmentOpen(false); void loadResources(); }} onReload={reloadSelected} onResourceChanged={(resource) => setSelectedResource(resource)} onStartAssessment={() => setAssessmentOpen(true)} />{assessmentOpen && <AssessmentPanel resource={selectedResource} questions={questions} onClose={() => setAssessmentOpen(false)} />}</>;
  }

  return (
    <div className="mx-auto w-full max-w-[1180px] space-y-5 px-5 py-6 sm:px-8 lg:px-12 lg:py-8">
      {listError && <ErrorBanner message={listError} onRetry={() => void loadResources()} />}
      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}
      <header className="flex flex-wrap items-end justify-between gap-4"><div><p className="text-[10px] font-medium uppercase tracking-[0.1em] text-ink-muted">练习巩固</p><h1 className="mt-1.5 text-[25px] font-semibold tracking-[-0.04em] text-ink">题目资料</h1><p className="mt-1.5 max-w-[620px] text-[12px] leading-5 text-ink-secondary">上传真实试卷、模拟卷、练习册、作业或题单，校对识别结果后选择错题或保存作答记录。</p></div><button type="button" onClick={() => setUploadOpen(true)} className="rounded-[8px] bg-ink px-4 py-2.5 text-[12px] font-medium text-white hover:bg-black">导入题目资料</button></header>
      <div className="flex flex-wrap items-center justify-between gap-3"><div className="flex w-fit flex-wrap gap-1 rounded-[9px] border border-line bg-white p-1">{([ ["all", "全部资料"], ["questions", "题目资料"], ["reference", "参考资料"], ["archived", "已归档"] ] as Array<[ResourceFilter, string]>).map(([value, label]) => <button key={value} type="button" onClick={() => setFilter(value)} className={`rounded-[7px] px-3 py-1.5 text-[11px] font-medium ${filter === value ? "bg-ink text-white" : "text-ink-secondary hover:bg-surface-muted"}`}>{label}</button>)}</div><p className="text-[10px] text-ink-muted">{filteredResources.length} 份资料</p></div>
      {resources === null ? <div className="rounded-[13px] border border-line bg-white p-5"><SkeletonList rows={5} /></div> : filteredResources.length === 0 ? <EmptyState title={filter === "archived" ? "暂无归档资料" : "还没有题目资料"} description="可以上传一份试卷、练习册、作业或题单，也可以先创建一个空资料手动录入题目。" illustration="docs"><button type="button" onClick={() => setUploadOpen(true)} className="mt-2 rounded-[8px] bg-ink px-4 py-2 text-[12px] font-medium text-white">导入第一份题目资料</button></EmptyState> : <div className="grid gap-4 md:grid-cols-2">{filteredResources.map((resource) => <ResourceCard key={resource.id} resource={resource} questionResource={isQuestionResource(resource)} onOpen={() => openResource(resource)} onArchive={() => void archive(resource)} onRestore={() => void restore(resource)} onDelete={() => requestDelete(resource)} onExtract={() => void runExtract(resource)} busy={busyId === resource.id} />)}</div>}
      <section className="rounded-[12px] border border-line bg-white px-4 py-3.5 text-[11px] leading-5 text-ink-secondary"><span className="font-medium text-ink">边界说明：</span>知识库管理教材、讲义和笔记，用于 AI 检索与引用；题目资料用于拆题、错题和真实作答。题目资料不会自动进入普通聊天检索；待校对题、未核验答案也不会进入复习、FSRS、掌握度、报告或推荐。</section>
      {uploadOpen && <ResourceUploadDialog onClose={() => setUploadOpen(false)} onCreated={(resource) => void handleCreated(resource)} />}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="删除题目资料？"
        description="如果已有作答或错题记录，服务端会保护历史并拒绝删除。原文件删除成功后，这份资料才会从列表移除。"
        confirmLabel="删除"
        destructive
        busy={busyId === deleteTarget?.id}
        onCancel={() => { if (busyId === null) setDeleteTarget(null); }}
        onConfirm={() => { if (deleteTarget) void remove(deleteTarget); }}
      />
    </div>
  );
}
