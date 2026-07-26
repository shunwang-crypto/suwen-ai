"use client";

// 文档表格（F2）：文件名/大小/两阶段状态机徽章/失败原因/操作（分块预览、重建索引、删除）。
// 状态机: pending→parsing→chunking→embedding→ready→enriching→enriched | failed。

import { fmtBytes, fmtDateTime } from "@/components/common";
import type { DocumentOut } from "@/lib/types";

interface StatusMeta {
  label: string;
  className: string;
  active?: boolean;
}

const STATUS_META: Record<string, StatusMeta> = {
  pending: { label: "等待处理", className: "bg-slate-100 text-slate-600", active: true },
  parsing: { label: "解析中", className: "bg-indigo-50 text-indigo-600", active: true },
  chunking: { label: "切块中", className: "bg-indigo-50 text-indigo-600", active: true },
  embedding: { label: "向量化中", className: "bg-indigo-50 text-indigo-600", active: true },
  ready: { label: "已就绪", className: "bg-emerald-50 text-emerald-700" },
  enriching: { label: "知识增强中", className: "bg-violet-50 text-violet-600", active: true },
  enriched: { label: "已增强", className: "bg-emerald-100 text-emerald-800" },
  failed: { label: "失败", className: "bg-red-50 text-red-700" },
};

/** 处于处理中状态（需要继续轮询）的判断。 */
export function isDocActive(status: string): boolean {
  return Boolean(STATUS_META[status]?.active);
}

export function DocStatusBadge({ status }: { status: string }) {
  const meta = STATUS_META[status] ?? {
    label: status,
    className: "bg-slate-100 text-slate-600",
  };
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${meta.className}`}
    >
      {meta.active && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
      )}
      {meta.label}
    </span>
  );
}

export function DocumentTable({
  documents,
  busyDocId,
  onPreview,
  onReindex,
  onDelete,
  readOnly = false,
}: {
  documents: DocumentOut[];
  busyDocId: string | null;
  onPreview: (doc: DocumentOut) => void;
  onReindex: (doc: DocumentOut) => void;
  onDelete: (doc: DocumentOut) => void;
  /** 机构库 member 只读视图：隐藏重建索引/删除 */
  readOnly?: boolean;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-130 text-left text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-xs text-slate-400">
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
            return (
              <tr key={doc.id} className="border-b border-slate-100 align-top">
                <td className="max-w-60 py-2.5 pr-3">
                  <p className="flex items-center gap-1.5 font-medium text-slate-800">
                    <span className="min-w-0 truncate" title={doc.filename}>
                      {doc.filename}
                    </span>
                    {doc.parse_backend === "web" && (
                      <span
                        className="shrink-0 rounded-full bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium text-sky-700"
                        title="从网址导入"
                      >
                        网页
                      </span>
                    )}
                  </p>
                  {doc.status === "failed" && doc.error_msg && (
                    <p className="mt-0.5 break-words text-xs text-red-600">
                      {doc.error_msg}
                    </p>
                  )}
                </td>
                <td className="whitespace-nowrap py-2.5 pr-3 text-slate-500">
                  {fmtBytes(doc.size_bytes)}
                </td>
                <td className="whitespace-nowrap py-2.5 pr-3">
                  <DocStatusBadge status={doc.status} />
                </td>
                <td className="whitespace-nowrap py-2.5 pr-3 text-xs text-slate-400">
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
                          className="rounded-lg px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          重建索引
                        </button>
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
