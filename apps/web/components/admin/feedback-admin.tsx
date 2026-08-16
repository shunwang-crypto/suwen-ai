"use client";

// 反馈管理分区（admin 反馈 tab）：反馈列表筛选与处理。

import { useEffect, useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  LoadingState,
  fmtDateTime,
} from "@/components/common";
import { FEEDBACK_CATEGORY_LABELS } from "@/components/feedback-widget";
import { ApiError, apiJson } from "@/lib/api";
import type {FeedbackCategory, FeedbackListResponse, FeedbackOut} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

// ---- 用户反馈（P2-1）：admin 工单列表，status 筛选 + 关闭 ----

const FEEDBACK_FILTERS = [
  ["", "全部"],
  ["open", "处理中"],
  ["closed", "已关闭"],
] as const;

export function FeedbackAdminSection() {
  const [statusFilter, setStatusFilter] = useState<"" | "open" | "closed">("open");
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  // 列表加载（A5 收敛: useApiResource; statusFilter 走路径, reloadKey 走 deps;
  // error 保留本地状态, patch 失败也写同一通道）
  const feedbackQuery = new URLSearchParams({ page_size: "50" });
  if (statusFilter) feedbackQuery.set("status", statusFilter);
  const feedbackRes = useApiResource<FeedbackListResponse>(
    `/api/admin/feedback?${feedbackQuery}`,
    { errorMessage: "反馈列表加载失败", deps: [reloadKey] },
  );
  const { data, loading } = feedbackRes;

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源失败 → 错误横幅的单向同步 */
  useEffect(() => {
    setError(feedbackRes.error);
  }, [feedbackRes.error]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const setStatus = async (item: FeedbackOut, status: "open" | "closed") => {
    if (busyId) return;
    setBusyId(item.id);
    setError(null);
    try {
      await apiJson<FeedbackOut>(`/api/admin/feedback/${item.id}`, {
        method: "PATCH",
        body: { status },
      });
      setReloadKey((k) => k + 1);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "操作失败，请重试");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Card
      title="用户反馈"
      extra={
        <div className="flex items-center gap-1 rounded-xl bg-slate-100 p-1">
          {FEEDBACK_FILTERS.map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => {
                setStatusFilter(key);
              }}
              className={`rounded-lg px-2.5 py-1 text-xs font-medium transition-colors ${
                statusFilter === key
                  ? "bg-white text-indigo-700 shadow-sm"
                  : "text-slate-500 hover:text-slate-700"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      }
    >
      {error && (
        <div className="mb-2">
          <ErrorBanner message={error} onClose={() => setError(null)} />
        </div>
      )}
      {loading ? (
        <LoadingState text="反馈列表加载中…" />
      ) : !data || data.items.length === 0 ? (
        <EmptyState
          title="暂无反馈"
          description="用户从右下角「反馈」入口提交后会出现在这里"
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-160 text-left text-sm">
            <thead>
              <tr className="border-b border-slate-100 text-xs text-slate-400">
                <th className="py-1.5 pr-2 font-medium">时间</th>
                <th className="py-1.5 pr-2 font-medium">用户</th>
                <th className="py-1.5 pr-2 font-medium">分类</th>
                <th className="py-1.5 pr-2 font-medium">内容</th>
                <th className="py-1.5 pr-2 font-medium">页面 / request_id</th>
                <th className="py-1.5 pr-2 font-medium">状态</th>
                <th className="py-1.5 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((it) => (
                <tr key={it.id} className="border-b border-slate-50 align-top">
                  <td className="whitespace-nowrap py-2 pr-2 text-xs text-slate-500">
                    {fmtDateTime(it.created_at)}
                  </td>
                  <td className="max-w-40 truncate py-2 pr-2 text-xs text-slate-600">
                    {it.email ?? it.user_id ?? "—"}
                  </td>
                  <td className="whitespace-nowrap py-2 pr-2 text-xs text-slate-600">
                    {FEEDBACK_CATEGORY_LABELS[it.category as FeedbackCategory] ??
                      it.category}
                  </td>
                  <td className="max-w-80 py-2 pr-2 text-xs leading-relaxed text-slate-700">
                    {it.content}
                  </td>
                  <td className="max-w-52 py-2 pr-2 font-mono text-caption text-slate-400">
                    <span className="block truncate" title={it.page_url}>
                      {it.page_url || "—"}
                    </span>
                    <span className="block truncate" title={it.request_id}>
                      {it.request_id || "—"}
                    </span>
                  </td>
                  <td className="whitespace-nowrap py-2 pr-2">
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                        it.status === "closed"
                          ? "bg-slate-100 text-slate-500"
                          : "bg-amber-50 text-amber-700"
                      }`}
                    >
                      {it.status === "closed" ? "已关闭" : "处理中"}
                    </span>
                  </td>
                  <td className="whitespace-nowrap py-2 text-right">
                    {it.status === "open" ? (
                      <button
                        type="button"
                        disabled={busyId === it.id}
                        onClick={() => void setStatus(it, "closed")}
                        className="rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100 disabled:opacity-50"
                      >
                        {busyId === it.id ? "关闭中…" : "关闭"}
                      </button>
                    ) : (
                      <button
                        type="button"
                        disabled={busyId === it.id}
                        onClick={() => void setStatus(it, "open")}
                        className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-400 hover:bg-slate-50 disabled:opacity-50"
                      >
                        重开
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
