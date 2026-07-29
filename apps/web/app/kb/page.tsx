"use client";

// 知识库管理（F2）：库列表/新建/删除；文档上传（轮询两阶段状态机）；
// 分块预览抽屉；检索调试面板（评委演示用，数据全透明）。

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import Link from "next/link";

import {
  Card,
  EmptyState,
  ErrorBanner,
  SkeletonList,
  Spinner,
} from "@/components/common";
import { ChunksDrawer } from "@/components/kb/chunks-drawer";
import { DocumentTable, isDocActive } from "@/components/kb/document-table";
import { RetrieveDebug } from "@/components/kb/retrieve-debug";
import { StudyGuidePanel } from "@/components/kb/study-guide";
import { ALLOWED_SUFFIXES, UploadZone } from "@/components/kb/upload-zone";
import { ApiError, apiFetch, apiJson } from "@/lib/api";
import type { DocumentOut, KbOut } from "@/lib/types";

/** 上传/配额类错误码 → 中文提示（后端信封对未知码回显原码）。 */
const UPLOAD_ERROR_MESSAGES: Record<string, string> = {
  UNSUPPORTED_FILE_TYPE: `不支持的文件类型，仅支持 ${ALLOWED_SUFFIXES.join(" / ")}`,
  FILE_TOO_LARGE: "文件过大，超出单文件大小限制",
  EMPTY_FILE: "文件内容为空",
  FILE_SIGNATURE_MISMATCH: "文件签名与后缀不符（疑似伪装文件），已拦截",
  DOC_QUOTA_EXCEEDED: "文档数量已达配额上限",
  STORAGE_QUOTA_EXCEEDED: "存储空间已达配额上限",
  // URL 网页导入（parsing/web.py 的 WebFetchError.code；后端信封已带中文，这里兜底）
  URL_INVALID: "网址格式不合法",
  URL_SCHEME_NOT_ALLOWED: "仅支持 http/https 网址",
  URL_HOST_NOT_ALLOWED: "该地址指向内网或本机，已拒绝",
  URL_HOST_UNRESOLVABLE: "域名无法解析，请检查网址",
  URL_FETCH_FAILED: "网页抓取失败，请稍后重试或换一个网址",
  URL_CONTENT_UNSUPPORTED: "该网址不是网页正文（仅支持 HTML/文本）",
  URL_CONTENT_TOO_LARGE: "网页内容过大，已拒绝",
  URL_EXTRACT_EMPTY: "未能从该网页提取到正文内容",
};

function friendlyError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    return UPLOAD_ERROR_MESSAGES[err.code] ?? err.message;
  }
  return err instanceof Error ? err.message : fallback;
}

export default function KbPage() {
  // 库列表（kbsReload 自增触发重新加载：effect 内联 IIFE 模式）
  const [kbs, setKbs] = useState<KbOut[] | null>(null);
  const [kbsReload, setKbsReload] = useState(0);
  const [kbError, setKbError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newSubject, setNewSubject] = useState("");

  // 文档（docsKbId 标记 docs 属于哪个库：切库时自然呈现加载态，避免 effect 内同步 setState）
  const [docs, setDocs] = useState<DocumentOut[] | null>(null);
  const [docsKbId, setDocsKbId] = useState<string | null>(null);
  const [docsError, setDocsError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [busyDocId, setBusyDocId] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [previewDoc, setPreviewDoc] = useState<DocumentOut | null>(null);

  const selected = kbs?.find((k) => k.id === selectedId) ?? null;

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const list = await apiJson<KbOut[]>("/api/kb");
        if (!alive) return;
        setKbs(list);
        setKbError(null);
        setSelectedId((prev) =>
          prev && list.some((k) => k.id === prev) ? prev : (list[0]?.id ?? null),
        );
      } catch (err) {
        if (!alive) return;
        setKbs((prev) => prev ?? []);
        setKbError(err instanceof ApiError ? err.message : "知识库列表加载失败");
      }
    })();
    return () => {
      alive = false;
    };
  }, [kbsReload]);

  const reloadKbs = useCallback(() => setKbsReload((k) => k + 1), []);

  const loadDocs = useCallback(async (kbId: string, silent = false) => {
    try {
      const list = await apiJson<DocumentOut[]>(`/api/kb/${kbId}/documents`);
      setDocs(list);
      setDocsKbId(kbId);
      setDocsError(null);
    } catch (err) {
      if (!silent) {
        setDocsError(err instanceof ApiError ? err.message : "文档列表加载失败");
        setDocs([]);
        setDocsKbId(kbId);
      }
    }
  }, []);

  // 切库后的首次加载（内联 IIFE；后续静默刷新走 loadDocs）
  useEffect(() => {
    if (!selectedId) return;
    let alive = true;
    (async () => {
      try {
        const list = await apiJson<DocumentOut[]>(`/api/kb/${selectedId}/documents`);
        if (!alive) return;
        setDocs(list);
        setDocsKbId(selectedId);
        setDocsError(null);
      } catch (err) {
        if (!alive) return;
        setDocsError(err instanceof ApiError ? err.message : "文档列表加载失败");
        setDocs([]);
        setDocsKbId(selectedId);
      }
    })();
    return () => {
      alive = false;
    };
  }, [selectedId]);

  // 两阶段状态机轮询：存在处理中的文档时每 2s 静默刷新，直到全部 ready/enriched/failed
  const pollingRef = useRef(false);
  useEffect(() => {
    const active = Boolean(docs?.some((d) => isDocActive(d.status)));
    if (!selectedId || docsKbId !== selectedId || !active) return;
    const timer = setInterval(() => {
      if (pollingRef.current) return;
      pollingRef.current = true;
      void loadDocs(selectedId, true).finally(() => {
        pollingRef.current = false;
      });
    }, 2000);
    return () => clearInterval(timer);
  }, [docs, docsKbId, selectedId, loadDocs]);

  const createKb = async (e: FormEvent) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name || creating) return;
    setCreating(true);
    setBanner(null);
    try {
      const kb = await apiJson<KbOut>("/api/kb", {
        body: { name, subject: newSubject.trim() || "general" },
      });
      setNewName("");
      setNewSubject("");
      setShowCreate(false);
      setSelectedId(kb.id);
      reloadKbs();
    } catch (err) {
      setBanner(friendlyError(err, "新建知识库失败"));
    } finally {
      setCreating(false);
    }
  };

  const deleteKb = async (kb: KbOut) => {
    if (!window.confirm(`确定删除知识库「${kb.name}」？其下文档与向量将被清理。`)) {
      return;
    }
    setBanner(null);
    try {
      await apiJson<void>(`/api/kb/${kb.id}`, { method: "DELETE" });
      if (kb.id === selectedId) setSelectedId(null);
      reloadKbs();
    } catch (err) {
      setBanner(friendlyError(err, "删除知识库失败"));
    }
  };

  const uploadFiles = async (files: File[]) => {
    if (!selectedId || uploading) return;
    setBanner(null);
    setUploading(true);
    const failures: string[] = [];
    for (const file of files) {
      const suffix = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
      if (!ALLOWED_SUFFIXES.includes(suffix)) {
        failures.push(`${file.name}：${UPLOAD_ERROR_MESSAGES.UNSUPPORTED_FILE_TYPE}`);
        continue;
      }
      try {
        const form = new FormData();
        form.append("file", file);
        await apiFetch(`/api/kb/${selectedId}/documents`, {
          method: "POST",
          body: form,
        });
      } catch (err) {
        failures.push(`${file.name}：${friendlyError(err, "上传失败")}`);
      }
    }
    setUploading(false);
    if (failures.length > 0) setBanner(failures.join("；"));
    await loadDocs(selectedId, true);
  };

  // 从网址导入：后端抓正文 → 建文档行 → 复用两阶段摄取（新行由既有轮询接管）
  const importUrl = async (url: string) => {
    if (!selectedId || importing) return;
    setBanner(null);
    setImporting(true);
    try {
      await apiJson<DocumentOut>(`/api/kb/${selectedId}/documents/from-url`, {
        body: { url },
      });
      await loadDocs(selectedId, true);
    } catch (err) {
      setBanner(friendlyError(err, "网页导入失败"));
    } finally {
      setImporting(false);
    }
  };

  const reindexDoc = async (doc: DocumentOut) => {
    if (!selectedId) return;
    setBusyDocId(doc.id);
    setBanner(null);
    try {
      await apiJson<{ status: string }>(
        `/api/kb/${selectedId}/documents/${doc.id}/reindex`,
        { method: "POST", body: {} },
      );
      await loadDocs(selectedId, true);
    } catch (err) {
      setBanner(friendlyError(err, "重建索引失败"));
    } finally {
      setBusyDocId(null);
    }
  };

  const deleteDoc = async (doc: DocumentOut) => {
    if (!selectedId) return;
    if (!window.confirm(`确定删除文档「${doc.filename}」？相关向量与引用将被清理。`)) {
      return;
    }
    setBusyDocId(doc.id);
    setBanner(null);
    try {
      await apiJson<void>(`/api/kb/${selectedId}/documents/${doc.id}`, {
        method: "DELETE",
      });
      await loadDocs(selectedId, true);
    } catch (err) {
      setBanner(friendlyError(err, "删除文档失败"));
    } finally {
      setBusyDocId(null);
    }
  };

  return (
    <div className="mx-auto w-full max-w-[1100px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      {kbError && <ErrorBanner message={kbError} onRetry={reloadKbs} />}
      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}

      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <p className="text-[10px] text-ink-muted">知识库 {selected ? <><span className="mx-1">/</span><span className="text-ink-secondary">{selected.name}</span></> : null}</p>
          <div className="mt-2 flex items-center gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-success-50 text-[15px] font-semibold text-success-600">{selected?.name?.[0] ?? "知"}</span>
            <div className="min-w-0"><h1 className="truncate text-[21px] font-semibold tracking-[-0.03em] text-ink">{selected?.name ?? "知识库"}</h1><p className="mt-0.5 text-[11px] text-ink-muted">{selected ? `${docs?.length ?? 0} 份资料 · ${docs?.filter((doc) => doc.status === "ready").length ?? 0} 就绪 · ${selected.subject}` : "把课程资料整理成可检索、可引用的学习空间"}</p></div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {kbs && kbs.length > 0 && <select value={selectedId ?? ""} onChange={(e) => setSelectedId(e.target.value)} aria-label="切换知识库" className="h-9 max-w-52 rounded-[8px] border border-line-strong bg-white px-3 text-[12px] text-ink-secondary outline-none focus:border-brand-400">{kbs.map((kb) => <option key={kb.id} value={kb.id}>{kb.name}{kb.owner_type === "org" ? ` · ${kb.org_name ?? "机构"}` : ""}</option>)}</select>}
          <button type="button" onClick={() => setShowCreate((value) => !value)} className="h-9 rounded-[8px] bg-brand-600 px-4 text-[12px] font-medium text-white hover:bg-brand-800">新建知识库</button>
          {selected?.writable && <button type="button" onClick={() => void deleteKb(selected)} className="h-9 rounded-[8px] border border-line-strong bg-white px-3 text-[12px] text-danger-600 hover:bg-danger-50">删除</button>}
        </div>
      </header>

      {showCreate && (
        <form onSubmit={createKb} className="grid gap-2 rounded-[12px] border border-line bg-white p-4 sm:grid-cols-[minmax(0,1fr)_220px_auto]">
          <input value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={255} placeholder="知识库名称" className="h-9 rounded-[8px] border border-line-strong px-3 text-[12px] outline-none focus:border-brand-400" />
          <input value={newSubject} onChange={(e) => setNewSubject(e.target.value)} maxLength={64} placeholder="学科（可选）" className="h-9 rounded-[8px] border border-line-strong px-3 text-[12px] outline-none focus:border-brand-400" />
          <button type="submit" disabled={!newName.trim() || creating} className="flex h-9 items-center justify-center gap-1.5 rounded-[8px] bg-brand-600 px-4 text-[12px] font-medium text-white disabled:opacity-40">{creating && <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />}创建</button>
        </form>
      )}

      {kbs === null ? <div className="rounded-[14px] border border-line bg-white p-5"><SkeletonList rows={4} /></div> : kbs.length === 0 ? (
        <EmptyState title="建立第一个知识库" description="新账号不会显示示例教材。创建知识库后即可上传真实资料并生成学习指南。" illustration="docs"><button type="button" onClick={() => setShowCreate(true)} className="mt-2 rounded-[8px] bg-brand-600 px-4 py-2 text-[12px] font-medium text-white">新建知识库</button></EmptyState>
      ) : !selected ? (
        <EmptyState title="选择一个知识库" description="从上方选择已有知识库，或创建新的资料空间。" illustration="docs" />
      ) : (
        <>
          <section className="flex flex-wrap items-center justify-between gap-4 rounded-[14px] bg-brand-700 px-5 py-4 text-white">
            <div><h2 className="text-[13px] font-semibold">用这个知识库开始学习</h2><p className="mt-1 text-[10px] text-indigo-100">AI 只会引用这里的真实资料和来源；辅导与练习也可沿用当前范围。</p></div>
            <div className="flex flex-wrap gap-2"><Link href={`/chat?new=1&kb=${encodeURIComponent(selected.id)}`} className="rounded-[8px] bg-white px-3.5 py-2 text-[11px] font-medium text-brand-700">对话</Link><Link href={`/tutor?kb=${encodeURIComponent(selected.id)}`} className="rounded-[8px] bg-white/10 px-3.5 py-2 text-[11px] font-medium text-white ring-1 ring-inset ring-white/15">分级辅导</Link><Link href={`/quiz?kb=${encodeURIComponent(selected.id)}&topic=${encodeURIComponent(selected.name)}`} className="rounded-[8px] bg-white/10 px-3.5 py-2 text-[11px] font-medium text-white ring-1 ring-inset ring-white/15">智能练习</Link></div>
          </section>

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_230px] lg:items-start">
            <Card title="学习指南" extra={<span className="text-[10px] text-ink-muted">基于已就绪资料生成</span>}><StudyGuidePanel key={selected.id} kbId={selected.id} kbName={selected.name} writable={selected.writable} /></Card>
            <aside className="space-y-4">
              <section className="rounded-[14px] border border-line bg-white p-4"><h2 className="text-[12px] font-semibold text-ink">接入 AI 学习</h2><p className="mt-1 text-[10px] leading-4 text-ink-muted">在 AI 对话中使用当前知识库，回答会附带可打开的引用来源。</p><Link href={`/chat?new=1&kb=${encodeURIComponent(selected.id)}`} className="mt-3 flex h-8 items-center justify-center rounded-[7px] bg-brand-600 text-[11px] font-medium text-white">用这个知识库提问</Link>{selected.writable && <details className="mt-2"><summary className="cursor-pointer text-center text-[10px] text-ink-muted">检索检查（高级工具）</summary><div className="mt-3"><RetrieveDebug kbId={selected.id} /></div></details>}</section>
              <section className="rounded-[14px] border border-line bg-white p-4 text-[11px]"><dl className="space-y-2.5"><div className="flex justify-between"><dt className="text-ink-muted">归属</dt><dd className="font-medium text-ink">{selected.owner_type === "org" ? selected.org_name ?? "机构" : "个人"}</dd></div><div className="flex justify-between"><dt className="text-ink-muted">权限</dt><dd className="font-medium text-ink">{selected.writable ? "可编辑" : "只读"}</dd></div><div className="flex justify-between"><dt className="text-ink-muted">版本</dt><dd className="font-medium text-ink">v{selected.kb_version}</dd></div></dl></section>
            </aside>
          </div>

          <Card title={`学习资料 · ${docs?.length ?? 0}`} extra={selected.owner_type === "org" ? <span className="text-[10px] text-ink-muted">{selected.org_name ?? "机构"} · {selected.writable ? "可管理" : "成员只读"}</span> : undefined}>
            <div className="space-y-4">
              {selected.writable ? <UploadZone uploading={uploading} importing={importing} onFiles={(files) => void uploadFiles(files)} onImportUrl={(url) => void importUrl(url)} /> : <p className="rounded-[9px] border border-dashed border-line-strong bg-[#fafaf9] px-4 py-3 text-[11px] text-ink-muted">机构库由管理员维护；你仍可以检索、对话和查看引用。</p>}
              {docsError && <ErrorBanner message={docsError} onRetry={() => void loadDocs(selected.id)} />}
              {docs === null || docsKbId !== selected.id ? <SkeletonList rows={3} /> : docs.length === 0 ? <p className="py-6 text-center text-[11px] text-ink-muted">{selected.writable ? "暂无资料。上传文件或导入网页后会自动解析、切块并建立索引。" : "该机构库暂无资料。"}</p> : <DocumentTable documents={docs} busyDocId={busyDocId} onPreview={setPreviewDoc} onReindex={(doc) => void reindexDoc(doc)} onDelete={(doc) => void deleteDoc(doc)} readOnly={!selected.writable} />}
            </div>
          </Card>
        </>
      )}

      {previewDoc && selectedId && <ChunksDrawer kbId={selectedId} doc={previewDoc} onClose={() => setPreviewDoc(null)} />}
    </div>
  );
}
