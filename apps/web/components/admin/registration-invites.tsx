"use client";

// 注册邀请码分区（admin 邀请 tab）：邀请码生成与列表管理。

import { useEffect, useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  LoadingState,
  fmtDateTime,
} from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import type {RegistrationInviteOut} from "@/lib/types";

const INVITE_STATUS_LABEL: Record<RegistrationInviteOut["status"], string> = {
  active: "有效",
  used: "已用",
  revoked: "已撤销",
  expired: "已过期",
};

export function RegistrationInvitesSection() {
  const [items, setItems] = useState<RegistrationInviteOut[]>([]);
  const [generated, setGenerated] = useState<RegistrationInviteOut[]>([]);
  const [count, setCount] = useState(10);
  const [maxUses, setMaxUses] = useState(1);
  const [expiresInDays, setExpiresInDays] = useState(7);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      setItems(await apiJson<RegistrationInviteOut[]>("/api/admin/registration-invites"));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "邀请码列表加载失败");
    } finally {
      setLoading(false);
    }
  };

  // 首屏列表（A5 收敛: 复用 load, 初始加载也走 loading 态）
  /* eslint-disable react-hooks/set-state-in-effect -- 首屏复用 load, 其 setState 均为加载反馈 */
  useEffect(() => {
    void load();
  }, []);
  /* eslint-enable react-hooks/set-state-in-effect */

  const create = async (requestedCount: number) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const body = {
        count: requestedCount,
        max_uses: maxUses,
        expires_in_hours: expiresInDays > 0 ? expiresInDays * 24 : null,
      };
      const result =
        requestedCount === 1
          ? [
              await apiJson<RegistrationInviteOut>("/api/admin/registration-invites", {
                method: "POST",
                body,
              }),
            ]
          : await apiJson<RegistrationInviteOut[]>(
              "/api/admin/registration-invites/batch",
              { method: "POST", body },
            );
      setGenerated(result);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "邀请码创建失败");
    } finally {
      setBusy(false);
    }
  };

  const copyCodes = async (codes: string[], key: string) => {
    await navigator.clipboard.writeText(codes.join("\n"));
    setCopied(key);
    window.setTimeout(() => setCopied(null), 1800);
  };

  const revoke = async (item: RegistrationInviteOut) => {
    if (busy || item.status !== "active") return;
    if (!window.confirm("撤销后该邀请码将无法注册，确认继续吗？")) return;
    setBusy(true);
    try {
      await apiJson<RegistrationInviteOut>(
        `/api/admin/registration-invites/${item.id}/revoke`,
        { method: "POST" },
      );
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "邀请码撤销失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <Card title="创建邀请码">
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="block">
            <span className="mb-1 block text-xs text-ink-muted">数量</span>
            <input
              type="number"
              min={1}
              max={100}
              value={count}
              onChange={(e) => setCount(Math.min(100, Math.max(1, Number(e.target.value))))}
              className="w-full rounded-xl border border-line-strong px-3 py-2 text-sm"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-ink-muted">每枚可使用次数</span>
            <input
              type="number"
              min={1}
              max={100}
              value={maxUses}
              onChange={(e) => setMaxUses(Math.min(100, Math.max(1, Number(e.target.value))))}
              className="w-full rounded-xl border border-line-strong px-3 py-2 text-sm"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-ink-muted">有效期（天，0 为不过期）</span>
            <input
              type="number"
              min={0}
              max={365}
              value={expiresInDays}
              onChange={(e) => setExpiresInDays(Math.min(365, Math.max(0, Number(e.target.value))))}
              className="w-full rounded-xl border border-line-strong px-3 py-2 text-sm"
            />
          </label>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => void create(1)}
            className="rounded-xl border border-indigo-300 bg-white px-3.5 py-2 text-sm font-semibold text-indigo-700 hover:bg-indigo-50 disabled:opacity-50"
          >
            生成一枚
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void create(count)}
            className="rounded-xl bg-indigo-600 px-3.5 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {busy ? "生成中…" : `批量生成 ${count} 枚`}
          </button>
        </div>
        <p className="mt-2 text-xs text-ink-muted">
          邀请码只在创建成功后的本次结果中显示一次，列表不会提供恢复或查看原文的入口。
        </p>
      </Card>

      {error && <ErrorBanner message={error} onClose={() => setError(null)} />}

      {generated.length > 0 && (
        <Card
          title="本次生成结果"
          extra={
            <button
              type="button"
              onClick={() =>
                void copyCodes(
                  generated.flatMap((item) => (item.code ? [item.code] : [])),
                  "generated",
                )
              }
              className="rounded-lg border border-indigo-200 px-2.5 py-1 text-xs font-semibold text-indigo-700 hover:bg-indigo-50"
            >
              {copied === "generated" ? "已复制" : "复制全部"}
            </button>
          }
        >
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {generated.map((item) => (
              <div key={item.id} className="flex items-center justify-between gap-2 rounded-xl bg-indigo-50 px-3 py-2">
                <code className="text-xs font-semibold tracking-wide text-indigo-800">{item.code}</code>
                <button
                  type="button"
                  onClick={() => item.code && void copyCodes([item.code], item.id)}
                  className="shrink-0 text-xs text-indigo-600 hover:underline"
                >
                  {copied === item.id ? "已复制" : "复制"}
                </button>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card title="邀请码记录" extra={<button type="button" onClick={() => void load()} className="text-xs text-indigo-600 hover:underline">刷新</button>}>
        {loading ? (
          <LoadingState text="邀请码列表加载中…" />
        ) : items.length === 0 ? (
          <EmptyState title="暂无邀请码" description="创建邀请码后，记录会显示在这里。" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-160 text-left text-sm">
              <thead>
                <tr className="border-b border-line text-xs text-ink-muted">
                  <th className="py-1.5 pr-2 font-medium">状态</th>
                  <th className="py-1.5 pr-2 font-medium">创建时间</th>
                  <th className="py-1.5 pr-2 font-medium">有效期</th>
                  <th className="py-1.5 pr-2 font-medium">使用次数</th>
                  <th className="py-1.5 pr-2 font-medium">使用者</th>
                  <th className="py-1.5 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id} className="border-b border-line">
                    <td className="py-2 pr-2">
                      <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs text-ink-secondary">
                        {INVITE_STATUS_LABEL[item.status]}
                      </span>
                    </td>
                    <td className="whitespace-nowrap py-2 pr-2 text-xs text-ink-muted">{fmtDateTime(item.created_at)}</td>
                    <td className="whitespace-nowrap py-2 pr-2 text-xs text-ink-muted">{item.expires_at ? fmtDateTime(item.expires_at) : "不过期"}</td>
                    <td className="py-2 pr-2 text-xs text-ink-secondary">{item.used_count} / {item.max_uses}</td>
                    <td className="py-2 pr-2 text-xs text-ink-secondary">{item.used_by_email ?? "—"}</td>
                    <td className="py-2 text-right">
                      {item.status === "active" && (
                        <button type="button" disabled={busy} onClick={() => void revoke(item)} className="rounded-lg border border-red-200 px-2.5 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50">撤销</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
