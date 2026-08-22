"use client";

// 课程工作区·资料 tab（UI 重构阶段 5 自 /materials 选中库视图迁入，剔除学习指南——
// 指南独占「指南」tab）：学习资料卡
// （上传两阶段摄取/文档表/重建索引/删除）+ 信息 aside（检索调试折叠）+ 题目资料库 +
// 分块预览抽屉。机构库成员只读表现与原页一致。

import { useCallback, useEffect, useRef, useState } from "react";

import {
  Card,
  ErrorBanner,
  SkeletonList,
} from "@/components/common";
import { ChunksDrawer } from "@/components/kb/chunks-drawer";
import { DocumentTable, isDocActive } from "@/components/kb/document-table";
import { RetrieveDebug } from "@/components/kb/retrieve-debug";
import { ALLOWED_SUFFIXES, UploadZone } from "@/components/kb/upload-zone";
import { ResourceLibrary } from "@/components/resources/resource-library";
import { ApiError, apiFetch, apiJson } from "@/lib/api";
import { createRequestEpoch } from "@/lib/request-epoch";
import { toast } from "@/lib/toast";
import type { DocumentOut, KbOut } from "@/lib/types";

/** 上传/配额类错误码 → 中文提示（与 /materials 同一套，迁移保持一致）。 */
const UPLOAD_ERROR_MESSAGES: Record<string, string> = {
  UNSUPPORTED_FILE_TYPE: `不支持的文件类型，仅支持 ${ALLOWED_SUFFIXES.join(" / ")}`,
  FILE_TOO_LARGE: "文件过大，超出单文件大小限制",
  EMPTY_FILE: "文件内容为空",
  FILE_SIGNATURE_MISMATCH: "文件签名与后缀不符（疑似伪装文件），已拦截",
  DOC_QUOTA_EXCEEDED: "文档数量已达配额上限",
  STORAGE_QUOTA_EXCEEDED: "存储空间已达配额上限",
  URL_INVALID: "网址格式不合法",
  URL_SCHEME_NOT_ALLOWED: "仅支持 http/https 网址",
  URL_HOST_NOT_ALLOWED: "该地址指向内网或本机，已拒绝",
  URL_HOST_UNRESOLVED: "域名无法解析，请检查域名",
  URL_FETCH_FAILED: "网页抓取失败，请稍后再试或换一个网址",
  URL_CONTENT_UNSUPPORTED: "该网址不是网页正文（仅支持 HTML/文本）",
  URL_CONTENT_TOO_LARGE: "网页内容过大，已拒绝",
  URL_EXTRACT_EMPTY: "未能从该网页提取到正文内容",
};

function friendlyError(err: unknown, fallback: string) {
  if (err instanceof ApiError) {
    return UPLOAD_ERROR_MESSAGES[err.code] ?? err.message;
  }
  return err instanceof Error ? err.message : fallback;
}

export function CourseMaterialsTab({ kb }: { kb: KbOut }) {
  const selectedId = kb.id;
  // 文档（docsKbId 标记 docs 属于哪个库：切库时自然呈现加载态）
  const [docs, setDocs] = useState<DocumentOut[] | null>(null);
  const [docsKbId, setDocsKbId] = useState<string | null>(null);
  const [docsError, setDocsError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [busyDocId, setBusyDocId] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [previewDoc, setPreviewDoc] = useState<DocumentOut | null>(null);
  // latest-wins 边界：初始加载/轮询/变更刷新共用，旧库响应不会覆盖当前库
  const docsRequestsRef = useRef(createRequestEpoch());
  const selectedIdRef = useRef<string | null>(selectedId);

  const loadDocs = useCallback(async (kbId: string, silent = false) => {
    if (selectedIdRef.current !== kbId) return;
    const request = docsRequestsRef.current.begin();
    try {
      const list = await apiJson<DocumentOut[]>(`/api/kb/${kbId}/documents`);
      if (
        !docsRequestsRef.current.isCurrent(request) ||
        selectedIdRef.current !== kbId
      )
        return;
      setDocs(list);
      setDocsKbId(kbId);
      setDocsError(null);
    } catch (err) {
      if (
        !docsRequestsRef.current.isCurrent(request) ||
        selectedIdRef.current !== kbId
      )
        return;
      if (!silent) {
        setDocsError(err instanceof ApiError ? err.message : "文档列表加载失败");
        setDocs([]);
        setDocsKbId(kbId);
      }
    }
  }, []);

  // 首次加载（kb 变化即重置；docsKbId !== selectedId 期间自然呈现加载态）
  useEffect(() => {
    selectedIdRef.current = selectedId;
    docsRequestsRef.current.invalidate();
    const requests = docsRequestsRef.current;
    const request = requests.begin();
    void (async () => {
      try {
        const list = await apiJson<DocumentOut[]>(`/api/kb/${selectedId}/documents`);
        if (!requests.isCurrent(request) || selectedIdRef.current !== selectedId) return;
        setDocs(list);
        setDocsKbId(selectedId);
        setDocsError(null);
      } catch (err) {
        if (!requests.isCurrent(request) || selectedIdRef.current !== selectedId) return;
        setDocsError(err instanceof ApiError ? err.message : "文档列表加载失败");
        setDocs([]);
        setDocsKbId(selectedId);
      }
    })();
    return () => {
      requests.invalidate();
    };
  }, [selectedId]);

  // 两阶段状态机轮询：存在处理中的文档时每 2s 静默刷新
  const pollingRef = useRef(false);
  useEffect(() => {
    const active = Boolean(
      docs?.some((d) => isDocActive(d.status, d.recognition_meta_json)),
    );
    if (docsKbId !== selectedId || !active) return;
    const timer = setInterval(() => {
      if (pollingRef.current) return;
      pollingRef.current = true;
      void loadDocs(selectedId, true).finally(() => {
        pollingRef.current = false;
      });
    }, 2000);
    return () => clearInterval(timer);
  }, [docs, docsKbId, selectedId, loadDocs]);

  const uploadFiles = async (files: File[]) => {
    if (uploading) return;
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
    const ok = files.length - failures.length;
    if (failures.length > 0) setBanner(failures.join("；"));
    if (ok > 0) toast.success(`已上传 ${ok} 个文件，正在解析建立索引…`);
    if (failures.length > 0) toast.error(`${failures.length} 个文件上传失败`);
    await loadDocs(selectedId, true);
  };

  const importUrl = async (url: string) => {
    if (importing) return;
    setBanner(null);
    setImporting(true);
    try {
      await apiJson<DocumentOut>(`/api/kb/${selectedId}/documents/from-url`, {
        body: { url },
      });
      toast.success("网页已导入，正在解析建立索引…");
      await loadDocs(selectedId, true);
    } catch (err) {
      const msg = friendlyError(err, "网页导入失败");
      setBanner(msg);
      toast.error(msg);
    } finally {
      setImporting(false);
    }
  };

  const reindexDoc = async (doc: DocumentOut) => {
    setBusyDocId(doc.id);
    setBanner(null);
    try {
      await apiJson<{ status: string }>(
        `/api/kb/${selectedId}/documents/${doc.id}/reindex`,
        { method: "POST", body: {} },
      );
      toast.success("已开始重建索引，完成后自动刷新");
      await loadDocs(selectedId, true);
    } catch (err) {
      const msg = friendlyError(err, "重建索引失败");
      setBanner(msg);
      toast.error(msg);
    } finally {
      setBusyDocId(null);
    }
  };

  // T0-1: 重新触发知识增强。增强失败时文档停在「可检索」并带 error_msg；此处调用真实端点
  // re-enrich 保留旧版本可用；enriched 文档用 recognition_meta_json 的 claim 标记刷新中，
  // 由轮询持续观察直到 worker 清除 claim，不依赖 status 是否暂时仍为 enriched。
  const reEnrichDoc = async (doc: DocumentOut) => {
    setBusyDocId(doc.id);
    setBanner(null);
    try {
      const res = await apiJson<{ status: string }>(
        `/api/kb/${selectedId}/documents/${doc.id}/re-enrich`,
        { method: "POST", body: {} },
      );
      if (res.status === "already_running") {
        toast.info("该文档正在知识增强中，请稍候");
      } else {
        toast.success("已重新触发知识增强，完成后自动刷新");
      }
      await loadDocs(selectedId, true);
    } catch (err) {
      const msg = friendlyError(err, "重新增强失败");
      setBanner(msg);
      toast.error(msg);
    } finally {
      setBusyDocId(null);
    }
  };

  const deleteDoc = async (doc: DocumentOut) => {
    if (!window.confirm(`确定删除文档「${doc.filename}」？相关向量与引用将被清理。`)) {
      return;
    }
    setBusyDocId(doc.id);
    setBanner(null);
    try {
      await apiJson<void>(`/api/kb/${selectedId}/documents/${doc.id}`, {
        method: "DELETE",
      });
      toast.success("文档已删除");
      await loadDocs(selectedId, true);
    } catch (err) {
      const msg = friendlyError(err, "删除文档失败");
      setBanner(msg);
      toast.error(msg);
    } finally {
      setBusyDocId(null);
    }
  };

  return (
    <div className="space-y-4">
      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_230px] lg:items-start">
        <Card title={`学习资料 · ${docs?.length ?? 0}`} extra={kb.owner_type === "org" ? <span className="text-caption text-ink-muted">{kb.org_name ?? "机构"} · {kb.writable ? "可管理" : "成员只读"}</span> : undefined}>
          <div className="space-y-4">
            {kb.writable ? (
              <UploadZone
                uploading={uploading}
                importing={importing}
                onFiles={(files) => void uploadFiles(files)}
                onImportUrl={(url) => void importUrl(url)}
              />
            ) : (
              <p className="rounded-control border border-dashed border-line-strong bg-canvas px-4 py-3 text-body-sm text-ink-muted">机构库由管理员维护；你仍可以检索、对话和查看引用。</p>
            )}
            {docsError && <ErrorBanner message={docsError} onRetry={() => void loadDocs(selectedId)} />}
            {docs === null || docsKbId !== selectedId ? (
              <SkeletonList rows={3} />
            ) : docs.length === 0 ? (
              <p className="py-6 text-center text-body-sm text-ink-muted">
                {kb.writable ? "暂无资料。上传文件或导入网页后会自动解析、切块并建立索引。" : "该机构库暂无资料。"}
              </p>
            ) : (
              <DocumentTable
                documents={docs}
                busyDocId={busyDocId}
                onPreview={setPreviewDoc}
                onReindex={(doc) => void reindexDoc(doc)}
                onReEnrich={(doc) => void reEnrichDoc(doc)}
                onDelete={(doc) => void deleteDoc(doc)}
                readOnly={!kb.writable}
              />
            )}
          </div>
        </Card>

        <aside className="space-y-4">
          {kb.writable && (
            <section className="rounded-overlay border border-line bg-white p-4">
              <h2 className="text-body-sm font-semibold text-ink">检索检查（高级工具）</h2>
              <p className="mt-1 text-body-sm leading-4 text-ink-muted">观察这门课资料在稠密/稀疏/融合/重排四阶段的命中。</p>
              <details className="mt-2">
                <summary className="cursor-pointer text-body-sm text-ink-muted">展开检索调试</summary>
                <div className="mt-3">
                  <RetrieveDebug kbId={selectedId} />
                </div>
              </details>
            </section>
          )}
          <section className="rounded-overlay border border-line bg-white p-4 text-body-sm">
            <dl className="space-y-2.5">
              <div className="flex justify-between"><dt className="text-ink-muted">归属</dt><dd className="font-medium text-ink">{kb.owner_type === "org" ? kb.org_name ?? "机构" : "个人"}</dd></div>
              <div className="flex justify-between"><dt className="text-ink-muted">权限</dt><dd className="font-medium text-ink">{kb.writable ? "可编辑" : "只读"}</dd></div>
              <div className="flex justify-between"><dt className="text-ink-muted">版本</dt><dd className="font-medium text-ink">v{kb.kb_version}</dd></div>
            </dl>
          </section>
        </aside>
      </div>

      <ResourceLibrary key={selectedId} scopeKbId={selectedId} />

      {previewDoc && (
        <ChunksDrawer
          kbId={selectedId}
          doc={previewDoc}
          onClose={() => setPreviewDoc(null)}
        />
      )}
    </div>
  );
}
