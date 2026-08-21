"use client";

// 文档表格（F2）：文件名/大小/两阶段状态机徽章/失败原因/操作（分块预览、重建索引、删除）。
// 状态机: pending→parsing→recognizing?→chunking→embedding→ready/partial_ready→enriching→enriched。

import { FxStyles, fmtBytes, fmtDateTime } from "@/components/common";
import type { DocumentOut } from "@/lib/types";

interface StatusMeta {
  label: string;
  className: string;
  active?: boolean;
  /** 徽章 tooltip：解释这个状态对用户意味着什么（尤其区分「可检索」与「已增强」）。 */
  hint?: string;
}

const STATUS_META: Record<string, StatusMeta> = {
  pending: { label: "等待处理", className: "bg-surface-muted text-ink-secondary", active: true },
  parsing: { label: "正在提取文本", className: "bg-indigo-50 text-indigo-600", active: true },
  recognizing: {
    label: "正在识别扫描页面",
    className: "bg-amber-50 text-amber-700",
    active: true,
  },
  chunking: { label: "正在分块", className: "bg-indigo-50 text-indigo-600", active: true },
  embedding: { label: "正在建立索引", className: "bg-indigo-50 text-indigo-600", active: true },
  ready: {
    label: "可检索",
    className: "bg-emerald-50 text-emerald-700",
    hint: "文档已索引、可被检索与引用；知识点与依赖图尚未生成",
  },
  partial_ready: {
    label: "部分页面失败",
    className: "bg-amber-50 text-amber-700",
    hint: "部分页面未能识别，其余内容已可检索",
  },
  enriching: {
    label: "知识增强中",
    className: "bg-violet-50 text-violet-600",
    active: true,
    hint: "正在从文档生成知识点与依赖关系",
  },
  enriched: {
    label: "已增强",
    className: "bg-emerald-100 text-emerald-800",
    hint: "已生成知识点与依赖图，可进入学习图谱",
  },
  failed: {
    label: "失败",
    className: "bg-red-50 text-red-700",
    hint: "处理失败，可尝试重建索引",
  },
};

/** 处于处理中状态（需要继续轮询）的判断。 */
export function isDocActive(status: string): boolean {
  return Boolean(STATUS_META[status]?.active);
}

export function DocStatusBadge({ status }: { status: string }) {
  const meta = STATUS_META[status] ?? {
    label: status,
    className: "bg-surface-muted text-ink-secondary",
  };
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${meta.className}`}
      role="status"
      aria-label={meta.label}
      title={meta.hint ?? meta.label}
    >
      <FxStyles />
      {/* 8px 状态圆点：处理中呼吸动画（reduced-motion 静态），终态常亮 */}
      <span
        className={`h-2 w-2 rounded-full bg-current ${meta.active ? "edu-breathe" : "opacity-70"}`}
      />
      {meta.label}
    </span>
  );
}

/** 摄取分段步骤条只表达真实阶段，不伪造百分比。 */
const INGEST_STEPS = ["文本提取", "扫描页识别", "分块", "索引", "完成", "增强"];
/** status → 当前真实阶段；6 表示基础摄取和增强全部完成。failed 不渲染步骤条。 */
const STATUS_STEP: Record<string, number> = {
  pending: 0,
  parsing: 0,
  recognizing: 1,
  chunking: 2,
  embedding: 3,
};

export function IngestSteps({ status }: { status: string }) {
  const step = STATUS_STEP[status];
  if (step == null) return null;
  return (
    <span
      className="mt-1 flex items-center gap-0.5"
      role="img"
      aria-label={`摄取进度：${INGEST_STEPS.map((s, i) => (i === step ? `${s}（处理中）` : s)).join(" → ")}`}
    >
      {INGEST_STEPS.map((label, i) => (
        <span
          key={label}
          title={`${label}${i < step ? "（已完成）" : i === step ? "（进行中）" : ""}`}
          className={`h-1 w-5 rounded-full ${
            i < step
              ? "bg-emerald-400"
              : i === step
                ? "edu-breathe bg-indigo-500"
                : "bg-surface-muted"
          }`}
        />
      ))}
    </span>
  );
}

export function DocumentTable({
  documents,
  busyDocId,
  onPreview,
  onReindex,
  onReEnrich,
  onDelete,
  readOnly = false,
}: {
  documents: DocumentOut[];
  busyDocId: string | null;
  onPreview: (doc: DocumentOut) => void;
  onReindex: (doc: DocumentOut) => void;
  /** 重新触发知识增强（生成知识点/依赖图）；只读视图或未提供时不显示该按钮。 */
  onReEnrich?: (doc: DocumentOut) => void;
  onDelete: (doc: DocumentOut) => void;
  /** 机构库 member 只读视图：隐藏重建索引/删除 */
  readOnly?: boolean;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-130 text-left text-sm">
        <thead>
          <tr className="border-b border-line text-xs text-ink-muted">
            <th className="py-2 pr-3 font-medium">文件名</th>
            <th className="py-2 pr-3 font-medium">大小</th>
            <th className="py-2 pr-3 font-medium">状态</th>
            <th className="py-2 pr-3 font-medium">更新时间</th>
            <th className="py-2 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          {documents.map((doc) => {
            const busy = busyDocId === doc.id;
            const failedPages = (doc.recognition_meta_json?.pages ?? []).filter(
              (page) => page.recognition_status === "failed",
            );
            return (
              <tr
                key={doc.id}
                className="border-b border-line align-top transition-colors hover:bg-indigo-50/40"
              >
                <td className="max-w-60 py-2.5 pr-3">
                  <p className="flex items-center gap-1.5 font-medium text-ink">
                    <span className="min-w-0 truncate" title={doc.filename}>
                      {doc.filename}
                    </span>
                    {doc.parse_backend === "web" && (
                      <span
                        className="shrink-0 rounded-full bg-sky-50 px-1.5 py-0.5 text-caption font-medium text-sky-700"
                        title="从网址导入"
                      >
                        网页
                      </span>
                    )}
                    {doc.parse_backend === "pdf-vision" && (
                      <span className="shrink-0 rounded-full bg-amber-50 px-1.5 py-0.5 text-caption font-medium text-amber-700">
                        扫描识别
                      </span>
                    )}
                    {doc.parse_backend === "pdf-mixed" && (
                      <span className="shrink-0 rounded-full bg-amber-50 px-1.5 py-0.5 text-caption font-medium text-amber-700">
                        混合 PDF
                      </span>
                    )}
                  </p>
                  {doc.error_msg && (
                    <p
                      className={`mt-0.5 break-words text-xs ${doc.status === "failed" ? "text-red-600" : "text-amber-700"}`}
                    >
                      {doc.error_msg}
                    </p>
                  )}
                </td>
                <td className="whitespace-nowrap py-2.5 pr-3 text-ink-muted">
                  {fmtBytes(doc.size_bytes)}
                </td>
                <td className="whitespace-nowrap py-2.5 pr-3">
                  <DocStatusBadge
                    status={
                      failedPages.length > 0 && ["ready", "enriched"].includes(doc.status)
                        ? "partial_ready"
                        : doc.status
                    }
                  />
                  <IngestSteps status={doc.status} />
                </td>
                <td className="whitespace-nowrap py-2.5 pr-3 text-xs text-ink-muted">
                  {fmtDateTime(doc.updated_at)}
                </td>
                <td className="whitespace-nowrap py-2.5">
                  <span className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => onPreview(doc)}
                      className="rounded-lg px-2 py-1 text-xs font-medium text-indigo-600 hover:bg-indigo-50"
                    >
                      分块预览
                    </button>
                    {!readOnly && (
                      <>
                        <button
                          type="button"
                          disabled={busy || isDocActive(doc.status)}
                          onClick={() => onReindex(doc)}
                          className="rounded-lg px-2 py-1 text-xs font-medium text-ink-secondary hover:bg-surface-muted disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          重建索引
                        </button>
                        {onReEnrich &&
                          ["ready", "partial_ready", "enriched"].includes(doc.status) && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => onReEnrich(doc)}
                              className="rounded-lg px-2 py-1 text-xs font-medium text-violet-600 hover:bg-violet-50 disabled:cursor-not-allowed disabled:opacity-40"
                              title={
                                doc.error_msg
                                  ? "上次知识增强失败，点此重新生成知识点与依赖图"
                                  : "重新生成知识点与依赖图"
                              }
                            >
                              重新增强
                            </button>
                          )}
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => onDelete(doc)}
                          className="rounded-lg px-2 py-1 text-xs font-medium text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          删除
                        </button>
                      </>
                    )}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
