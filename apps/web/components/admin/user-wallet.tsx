"use client";
// 用户钱包分区（admin 用户 tab）：用户角色/状态标签与钱包管理。

import { useEffect, useState } from "react";

import { Card, EmptyState, ErrorBanner, LoadingState, fmtDateTime } from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import type {AdminUserOut, AdminUserDetailOut, AdminUserPageOut, AdminWalletAdjustOut} from "@/lib/types";

const ADMIN_ROLE_LABEL: Record<string, string> = {
  student: "学生",
  teacher: "教师",
  admin: "管理员",
};

const ADMIN_STATUS_LABEL: Record<string, string> = {
  active: "正常",
  disabled: "已停用",
};

export function UserWalletSection() {
  const [query, setQuery] = useState("");
  const [users, setUsers] = useState<AdminUserOut[]>([]);
  const [selected, setSelected] = useState<AdminUserOut | null>(null);
  const [delta, setDelta] = useState("");
  const [note, setNote] = useState("");
  const [reference, setReference] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [total, setTotal] = useState(0);
  const [detail, setDetail] = useState<AdminUserDetailOut | null>(null);

  const loadPage = async (nextPage = 1) => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiJson<AdminUserPageOut>(`/api/admin/users?page=${nextPage}&page_size=20`);
      setUsers(data.items);
      setPage(data.page);
      setHasMore(data.has_more);
      setTotal(data.total);
      setSelected(null);
      setDetail(null);
    } catch (err) {
      setUsers([]);
      setError(err instanceof ApiError ? err.message : "用户列表加载失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  };

  // 首屏列表（A5 收敛: 复用 loadPage, 初始加载也走 loading 态）
  /* eslint-disable react-hooks/set-state-in-effect -- 首屏复用 loadPage, 其 setState 均为加载反馈 */
  useEffect(() => {
    void loadPage();
  }, []);
  /* eslint-enable react-hooks/set-state-in-effect */

  const searchUsers = async () => {
    const normalized = query.trim();
    if (!normalized) {
      await loadPage();
      return;
    }
    if (normalized.length < 3) {
      setError("邮箱查询至少输入 3 个字符");
      return;
    }
    setLoading(true);
    setError(null);
    setSuccess(null);
    setSelected(null);
    try {
      const params = new URLSearchParams({ query: normalized, limit: "50" });
      setUsers(await apiJson<AdminUserOut[]>(`/api/admin/users?${params.toString()}`));
      setPage(1);
      setHasMore(false);
      setTotal(0);
      setDetail(null);
    } catch (err) {
      setUsers([]);
      setError(err instanceof ApiError ? err.message : "用户查询失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  };

  const openDetail = async (item: AdminUserOut) => {
    setSelected(item);
    setDetail(null);
    setError(null);
    try {
      setDetail(await apiJson<AdminUserDetailOut>(`/api/admin/users/${item.user_id}`));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "用户详情加载失败");
    }
  };

  const adjust = async () => {
    if (busy) return;
    if (!selected) {
      setError("请先选择一个用户");
      return;
    }
    if (!delta.trim() || !note.trim() || !reference.trim()) {
      setError("请填写调额数值、原因和唯一业务凭证");
      return;
    }
    const confirmation = `确定为 ${selected.email} 增加 ${delta.trim()} 苏问 AI 额度吗？`;
    if (!window.confirm(confirmation)) return;
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      const result = await apiJson<AdminWalletAdjustOut>(
        `/api/admin/users/${selected.user_id}/wallet-adjust`,
        {
          method: "POST",
          body: {
            delta: delta.trim(),
            note: note.trim(),
            reference: reference.trim(),
          },
        },
      );
      setSelected((current) => (current ? { ...current, wallet_balance: result.balance } : current));
      setUsers((current) =>
        current.map((item) =>
          item.user_id === result.user_id ? { ...item, wallet_balance: result.balance } : item,
        ),
      );
      setSuccess(`调额成功，当前余额为 ${result.balance} 苏问 AI 额度。`);
      setDelta("");
      setReference("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "调额失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <Card title="用户与额度">
        <form
          className="flex flex-col gap-2 sm:flex-row"
          onSubmit={(event) => {
            event.preventDefault();
            void searchUsers();
          }}
        >
          <label className="min-w-0 flex-1">
            <span className="sr-only">按邮箱搜索用户</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="输入邮箱或邮箱前缀（至少 3 个字符）"
              type="search"
              maxLength={320}
              className="w-full rounded-xl border border-line-strong px-3 py-2 text-sm text-ink outline-none placeholder:text-ink-muted focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            />
          </label>
          <button
            type="submit"
            disabled={loading}
            className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? "搜索中…" : "搜索用户"}
          </button>
        </form>
        <p className="mt-2 text-xs text-ink-muted">仅限管理员使用；搜索结果不包含密码、验证码或令牌。</p>
      </Card>

      {error && <ErrorBanner message={error} onClose={() => setError(null)} />}
      {success && (
        <p role="status" className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-700">
          {success}
        </p>
      )}

      {loading ? (
        <LoadingState text="用户列表加载中…" />
      ) : users.length === 0 ? (
        <EmptyState
          title={query.trim() ? "没有找到匹配用户" : "暂无用户"}
          description={query.trim() ? "请检查邮箱或换一个前缀。" : "最近注册用户会默认显示在这里。"}
        />
      ) : (
        <Card title="搜索结果">
          <div className="overflow-x-auto">
            <table className="w-full min-w-160 text-left text-sm">
              <thead>
                <tr className="border-b border-line text-xs text-ink-muted">
                  <th className="py-1.5 pr-2 font-medium">邮箱</th>
                  <th className="py-1.5 pr-2 font-medium">角色</th>
                  <th className="py-1.5 pr-2 font-medium">状态</th>
                  <th className="py-1.5 pr-2 font-medium">注册时间</th>
                  <th className="py-1.5 pr-2 text-right font-medium">当前额度</th>
                  <th className="py-1.5 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {users.map((item) => (
                  <tr key={item.user_id} className="border-b border-line align-middle">
                    <td className="max-w-64 truncate py-2 pr-2 text-xs text-ink-secondary">{item.email}</td>
                    <td className="whitespace-nowrap py-2 pr-2 text-xs text-ink-secondary">
                      {ADMIN_ROLE_LABEL[item.role] ?? item.role}
                    </td>
                    <td className="whitespace-nowrap py-2 pr-2 text-xs text-ink-secondary">
                      {ADMIN_STATUS_LABEL[item.status] ?? item.status}
                    </td>
                    <td className="whitespace-nowrap py-2 pr-2 text-xs text-ink-muted">
                      {fmtDateTime(item.created_at)}
                    </td>
                    <td className="whitespace-nowrap py-2 pr-2 text-right font-mono text-xs text-ink-secondary">
                      {item.wallet_balance} 额度
                    </td>
                    <td className="py-2 text-right">
                      <button
                        type="button"
                        onClick={() => void openDetail(item)}
                        className={`rounded-lg border px-2.5 py-1 text-xs font-medium ${
                          selected?.user_id === item.user_id
                            ? "border-indigo-500 bg-indigo-50 text-indigo-700"
                            : "border-line-strong text-ink-secondary hover:bg-surface-muted"
                        }`}
                      >
                        {selected?.user_id === item.user_id ? "已打开" : "详情"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-4 flex items-center justify-between border-t border-line pt-3 text-xs text-ink-muted">
            <span>共 {total} 位用户 · 第 {page} 页</span>
            <span className="flex gap-2">
              <button type="button" disabled={loading || page <= 1} onClick={() => void loadPage(page - 1)} className="rounded-lg border border-line px-2.5 py-1 disabled:opacity-40">上一页</button>
              <button type="button" disabled={loading || !hasMore} onClick={() => void loadPage(page + 1)} className="rounded-lg border border-line px-2.5 py-1 disabled:opacity-40">下一页</button>
            </span>
          </div>
        </Card>
      )}

      {detail && (
        <Card title="用户详情" extra={<span className="text-xs text-ink-muted">只显示脱敏信息</span>}>
          <div className="grid gap-3 text-xs text-ink-secondary sm:grid-cols-3">
            <span>邮箱验证：{detail.email_verified ? "已验证" : "未验证"}</span>
            <span>知识库：{detail.knowledge_base_count}</span>
            <span>错题：{detail.error_count}</span>
            <span>计划：{detail.plan_count}</span>
            <span>平台调用：{detail.platform_call_count}</span>
            <span>自带 Key 调用：{detail.byok_call_count}</span>
          </div>
          <div className="mt-4 rounded-xl bg-surface-muted p-3 text-xs text-ink-secondary">
            <p className="font-medium text-ink-secondary">自带模型服务</p>
            {detail.model_credential.configured ? (
              <p className="mt-1">{detail.model_credential.provider} · {detail.model_credential.enabled ? "已启用" : "已暂停"} · 仅显示后四位：••••{detail.model_credential.key_last_four}</p>
            ) : <p className="mt-1">未配置</p>}
          </div>
        </Card>
      )}

      {selected && (
        <Card
          title="调整苏问 AI 额度"
          extra={<span className="text-xs text-ink-muted">已选择 {selected.email}</span>}
        >
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              void adjust();
            }}
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-ink-muted">快捷值：</span>
              <button
                type="button"
                onClick={() => {
                  setDelta("30.0000");
                  setNote("硅基流动有效邀请奖励");
                }}
                className="rounded-lg border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-xs font-semibold text-indigo-700 hover:bg-indigo-100"
              >
                +30.0000 邀请奖励
              </button>
            </div>
            <div className="grid gap-3 md:grid-cols-3">
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-ink-secondary">调额数值（字符串）</span>
                <input
                  value={delta}
                  onChange={(event) => setDelta(event.target.value)}
                  placeholder="例如 30.0000 或 -5.0000"
                  inputMode="decimal"
                  maxLength={16}
                  className="w-full rounded-xl border border-line-strong px-3 py-2 font-mono text-sm text-ink outline-none placeholder:font-sans placeholder:text-ink-muted focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                  required
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-ink-secondary">原因</span>
                <input
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  placeholder="例如：硅基流动有效邀请奖励"
                  maxLength={256}
                  className="w-full rounded-xl border border-line-strong px-3 py-2 text-sm text-ink outline-none placeholder:text-ink-muted focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                  required
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-ink-secondary">唯一业务凭证</span>
                <input
                  value={reference}
                  onChange={(event) => setReference(event.target.value)}
                  placeholder="例如：siliconflow-referral:唯一值"
                  maxLength={128}
                  className="w-full rounded-xl border border-line-strong px-3 py-2 font-mono text-sm text-ink outline-none placeholder:font-sans placeholder:text-ink-muted focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                  required
                />
              </label>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-xs text-ink-muted">
                当前余额：<span className="font-mono font-semibold text-ink-secondary">{selected.wallet_balance}</span> 苏问 AI 额度
              </p>
              <button
                type="submit"
                disabled={busy}
                className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy ? "提交中…" : "确认调额"}
              </button>
            </div>
          </form>
        </Card>
      )}
    </div>
  );
}
