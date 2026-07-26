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

import {
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
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
    <div className="mx-auto w-full max-w-6xl space-y-4 p-4">
      <PageHeader
        title="知识库管理"
        subtitle="上传讲义/教材或导入网页，自动进入 解析 → 切块 → 向量化 → 知识增强 流水线"
      />
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
      {/* 左：库列表 */}
      <aside className="w-full shrink-0 lg:w-64">
        <Card
          title="知识库"
          extra={
            <button
              type="button"
              onClick={() => setShowCreate((v) => !v)}
              className="rounded-lg bg-indigo-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-indigo-700"
            >
              新建
            </button>
          }
        >
          {showCreate && (
            <form onSubmit={createKb} className="mb-3 space-y-2">
              <input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                maxLength={255}
                placeholder="库名称（如：高等数学）"
                className="w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
              />
              <input
                value={newSubject}
                onChange={(e) => setNewSubject(e.target.value)}
                maxLength={64}
                placeholder="学科（可选，默认 general）"
                className="w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
              />
              <button
                type="submit"
                disabled={!newName.trim() || creating}
                className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-indigo-600 py-1.5 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {creating && <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />}
                创建
              </button>
            </form>
          )}

          {kbError && (
            <div className="mb-2">
              <ErrorBanner message={kbError} onRetry={reloadKbs} />
            </div>
          )}

          {kbs === null ? (
            <SkeletonList rows={3} />
          ) : kbs.length === 0 ? (
            <p className="py-4 text-center text-xs text-slate-400">
              还没有知识库，点击「新建」开始
            </p>
          ) : (
            <div className="space-y-3">
              {(["user", "org"] as const).map((ownerType) => {
                const group = kbs.filter((k) => k.owner_type === ownerType);
                if (group.length === 0) return null;
                return (
                  <div key={ownerType}>
                    <p className="mb-1 px-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                      {ownerType === "user" ? "个人库" : "机构库"}
                    </p>
                    <ul className="space-y-1">
                      {group.map((kb) => (
                        <li key={kb.id} className="group flex items-center gap-1">
                          <button
                            type="button"
                            onClick={() => setSelectedId(kb.id)}
                            className={`min-w-0 flex-1 rounded-lg px-3 py-2 text-left text-sm transition-all duration-200 ${
                              kb.id === selectedId
                                ? "bg-indigo-50 font-medium text-indigo-700"
                                : "text-slate-600 hover:-translate-y-0.5 hover:bg-slate-100 hover:shadow-[0_4px_14px_-4px_rgba(79,70,229,0.18)] motion-reduce:hover:translate-y-0"
                            }`}
                          >
                            <span className="flex items-center gap-1.5">
                              <span className="min-w-0 truncate">{kb.name}</span>
                              {kb.owner_type === "org" && (
                                <span
                                  className="shrink-0 rounded-full bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700"
                                  title={`机构：${kb.org_name ?? ""}${kb.writable ? "（可管理）" : "（只读）"}`}
                                >
                                  {kb.org_name ?? "机构"}
                                </span>
                              )}
                            </span>
                            <span className="block truncate text-xs font-normal text-slate-400">
                              {kb.subject} · v{kb.kb_version}
                              {kb.owner_type === "org" && !kb.writable && " · 只读"}
                            </span>
                          </button>
                          {kb.writable && (
                            <button
                              type="button"
                              onClick={() => void deleteKb(kb)}
                              className="shrink-0 rounded-lg p-1.5 text-slate-300 hover:bg-red-50 hover:text-red-600"
                              aria-label={`删除知识库 ${kb.name}`}
                            >
                              <svg
                                viewBox="0 0 24 24"
                                fill="none"
                                stroke="currentColor"
                                strokeWidth="1.8"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                className="h-4 w-4"
                                aria-hidden
                              >
                                <path d="M3 6h18m-2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                              </svg>
                            </button>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                );
              })}
            </div>
          )}
        </Card>
      </aside>

      {/* 右：选中库详情 */}
      <div className="min-w-0 flex-1 space-y-4">
        {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}

        {!selected ? (
          <EmptyState
            title="选择或新建一个知识库"
            description="上传讲义/教材后，对话与出题即可引用溯源"
            illustration="docs"
          />
        ) : (
          <>
            <Card
              title={`文档 · ${selected.name}`}
              extra={
                selected.owner_type === "org" ? (
                  <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs text-amber-700">
                    {selected.org_name ?? "机构"} ·{" "}
                    {selected.writable ? "管理员" : "成员只读"}
                  </span>
                ) : undefined
              }
            >
              <div className="space-y-4">
                {selected.writable ? (
                  <UploadZone
                    uploading={uploading}
                    importing={importing}
                    onFiles={(f) => void uploadFiles(f)}
                    onImportUrl={(u) => void importUrl(u)}
                  />
                ) : (
                  <p className="rounded-xl border border-dashed border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-500">
                    机构库由管理员统一维护；你可以在对话页勾选该库进行联合检索问答。
                  </p>
                )}
                {docsError && (
                  <ErrorBanner
                    message={docsError}
                    onRetry={() => void loadDocs(selected.id)}
                  />
                )}
                {docs === null || docsKbId !== selected.id ? (
                  <SkeletonList rows={3} />
                ) : docs.length === 0 ? (
                  <p className="py-2 text-center text-xs text-slate-400">
                    {selected.writable
                      ? "暂无文档，拖拽上传后自动进入 解析 → 切块 → 向量化 → 知识增强 流水线"
                      : "该机构库暂无文档"}
                  </p>
                ) : (
                  <DocumentTable
                    documents={docs}
                    busyDocId={busyDocId}
                    onPreview={setPreviewDoc}
                    onReindex={(d) => void reindexDoc(d)}
                    onDelete={(d) => void deleteDoc(d)}
                    readOnly={!selected.writable}
                  />
                )}
              </div>
            </Card>

            {/* 学习指南（对标 NotebookLM Source guide）：摄取完成后「下一步干嘛」 */}
            <Card
              title="学习指南"
              extra={
                <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700">
                  摄取后自动生成
                </span>
              }
            >
              <StudyGuidePanel
                key={selected.id}
                kbId={selected.id}
                kbName={selected.name}
                writable={selected.writable}
              />
            </Card>

            {/* 检索调试与写操作同权限：机构库仅 admin 可见（后端同样 403 兜底） */}
            {selected.writable && (
              <Card
                title="检索调试面板"
                extra={
                  <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-xs text-indigo-600">
                    过程全透明
                  </span>
                }
              >
                <RetrieveDebug kbId={selected.id} />
              </Card>
            )}
          </>
        )}
      </div>

      {previewDoc && selectedId && (
        <ChunksDrawer
          kbId={selectedId}
          doc={previewDoc}
          onClose={() => setPreviewDoc(null)}
        />
      )}
      </div>
    </div>
  );
}
