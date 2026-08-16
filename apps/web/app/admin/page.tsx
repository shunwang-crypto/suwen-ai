"use client";

// 运营面板（F10，仅管理员）：成本/token 大数卡片、按 feature/model/mode 条形（纯 div）、
// 日趋势折线（纯 SVG polyline）、top 用户表、latency p50/p95 表，
// time-travel 虚拟时钟控制卡（演示遗忘曲线的操作台），
// 以及「检索命中测试」分区（四阶段召回/融合/重排全过程）。

import { useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  LoadingState,
  Spinner,
  fmtDateTime,
} from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type {AdminLatencyResponse, AdminUsageResponse, TimeTravelInfo} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";
import { BarRows, DAY_OPTIONS, DailyTrend, StatTile, fmtCny, fmtInt } from "@/components/admin/usage-parts";
import { RetrievalTestSection } from "@/components/admin/retrieval-test";
import { FaultInjectionSection } from "@/components/admin/fault-injection";
import { FeedbackAdminSection } from "@/components/admin/feedback-admin";
import { UserWalletSection } from "@/components/admin/user-wallet";
import { RegistrationInvitesSection } from "@/components/admin/registration-invites";
import { NoAccess } from "@/components/admin/no-access";

export default function AdminPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  const [tab, setTab] = useState<
    "ops" | "retrieval" | "faults" | "feedback" | "invites" | "users"
  >("ops");
  const [days, setDays] = useState(7);
  const [banner, setBanner] = useState<string | null>(null);
  const [travelBusy, setTravelBusy] = useState(false);

  const [reloadKey, setReloadKey] = useState(0);

  // 数据加载（A5 收敛: 三块独立 useApiResource; days/reloadKey 经 deps 重载;
  // 切换时间窗口时保留旧数据展示，新数据到达后整体替换）
  const usageRes = useApiResource<AdminUsageResponse>(
    isAdmin ? `/api/admin/usage?days=${days}` : null,
    { errorMessage: "用量数据加载失败", deps: [reloadKey] },
  );
  const latencyRes = useApiResource<AdminLatencyResponse>(
    isAdmin ? `/api/admin/usage/latency?days=${days}` : null,
    { deps: [reloadKey] },
  );
  const ttRes = useApiResource<TimeTravelInfo>(
    isAdmin ? "/api/admin/time-travel" : null,
    { deps: [reloadKey] },
  );
  const usage = usageRes.data;
  const latency = latencyRes.data;
  const { data: timeTravel, setData: setTimeTravel } = ttRes;
  const loading = isAdmin && (usageRes.loading || latencyRes.loading || ttRes.loading);
  const error = usageRes.error;

  const reload = () => setReloadKey((k) => k + 1);

  const travel = async (deltaDays: number) => {
    if (travelBusy || deltaDays === 0) return;
    setTravelBusy(true);
    setBanner(null);
    try {
      await apiJson<TimeTravelInfo>("/api/admin/time-travel", {
        body: { days: deltaDays },
      });
      setTimeTravel(await apiJson<TimeTravelInfo>("/api/admin/time-travel"));
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "时间偏移操作失败");
    } finally {
      setTravelBusy(false);
    }
  };

  if (!isAdmin) return <NoAccess />;

  return (
    <div className="mx-auto w-full max-w-6xl space-y-4 p-4">
      {/* 分区切换：运营与成本 / 检索命中测试 */}
      <div className="flex w-fit items-center gap-1 rounded-xl border border-slate-200 bg-white p-1">
        {(
          [
            ["ops", "运营与成本"],
            ["retrieval", "检索命中测试"],
            ["faults", "故障注入"],
            ["feedback", "用户反馈"],
            ["invites", "邀请码管理"],
            ["users", "用户与额度"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={`rounded-lg px-3.5 py-1.5 text-xs font-medium transition-colors ${
              tab === key
                ? "bg-indigo-600 text-white"
                : "text-slate-600 hover:bg-slate-100"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "retrieval" && <RetrievalTestSection />}

      {tab === "faults" && <FaultInjectionSection />}

      {tab === "feedback" && <FeedbackAdminSection />}

      {tab === "invites" && <RegistrationInvitesSection />}

      {tab === "users" && <UserWalletSection />}

      {tab === "ops" && (
        <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold text-slate-900">运营与成本</h2>
        <div className="flex items-center gap-1 rounded-xl border border-slate-200 bg-white p-1">
          {DAY_OPTIONS.map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDays(d)}
              className={`rounded-lg px-3 py-1 text-xs font-medium transition-colors ${
                days === d
                  ? "bg-indigo-600 text-white"
                  : "text-slate-600 hover:bg-slate-100"
              }`}
            >
              近 {d} 天
            </button>
          ))}
        </div>
      </div>

      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}
      {error && <ErrorBanner message={error} onRetry={reload} />}

      {/* time-travel 控制卡：演示遗忘曲线的操作台 */}
      <div className="rounded-2xl border border-violet-200 bg-violet-50/50 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-slate-900">
              虚拟时钟（time-travel）
            </h3>
            <p className="mt-0.5 text-xs text-slate-500">
              当前偏移{" "}
              <span className="font-mono font-semibold text-violet-700">
                {timeTravel ? `+${timeTravel.offset_days.toFixed(2)} 天` : "—"}
              </span>
              {timeTravel?.virtual_now && (
                <>
                  {" "}· 虚拟当前时间{" "}
                  <span className="font-mono">{fmtDateTime(timeTravel.virtual_now)}</span>
                </>
              )}
              　推进后 FSRS 到期与复习队列随之前进，可现场演示遗忘曲线。
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {travelBusy && <Spinner />}
            <button
              type="button"
              disabled={travelBusy}
              onClick={() => void travel(1)}
              className="rounded-xl bg-violet-600 px-3.5 py-2 text-sm font-semibold text-white hover:bg-violet-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              +1 天
            </button>
            <button
              type="button"
              disabled={travelBusy}
              onClick={() => void travel(3)}
              className="rounded-xl bg-violet-600 px-3.5 py-2 text-sm font-semibold text-white hover:bg-violet-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              +3 天
            </button>
            <button
              type="button"
              disabled={travelBusy || !timeTravel || timeTravel.offset_days === 0}
              onClick={() => timeTravel && void travel(-timeTravel.offset_days)}
              className="rounded-xl border border-violet-300 bg-white px-3.5 py-2 text-sm font-medium text-violet-700 hover:bg-violet-100 disabled:cursor-not-allowed disabled:opacity-50"
            >
              清零
            </button>
          </div>
        </div>
      </div>

      {loading ? (
        <LoadingState text="运营数据加载中…" />
      ) : !usage ? (
        <EmptyState title="暂无用量数据" />
      ) : (
        <>
          {/* 大数卡片 */}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatTile
              label={`总成本（近 ${usage.days} 天）`}
              value={fmtCny(usage.totals.cost_cny)}
              sub={`${fmtInt(usage.totals.calls)} 次模型调用`}
            />
            <StatTile
              label="输入 tokens"
              value={fmtInt(usage.totals.prompt_tokens)}
            />
            <StatTile
              label="输出 tokens"
              value={fmtInt(usage.totals.completion_tokens)}
            />
            <StatTile
              label="缓存命中 tokens"
              value={fmtInt(usage.totals.cached_tokens)}
              sub="命中前缀缓存的部分按折扣计价"
            />
          </div>

          <Card title="日趋势 · 成本">
            <DailyTrend daily={usage.daily} />
          </Card>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card title="按功能（成本）">
              <BarRows
                rows={usage.by_feature.map((f) => ({
                  key: f.feature,
                  label: f.feature,
                  value: f.cost_cny,
                  detail: `${fmtCny(f.cost_cny)} · ${f.calls} 次`,
                }))}
              />
            </Card>
            <Card title="按模型（成本）">
              <BarRows
                rows={usage.by_model.map((m) => ({
                  key: `${m.model}-${m.tier}`,
                  label: `${m.model}（${m.tier}）`,
                  value: m.cost_cny,
                  detail: `${fmtCny(m.cost_cny)} · ${m.calls} 次`,
                }))}
              />
            </Card>
            <Card title="按模式（成本）">
              <BarRows
                rows={usage.by_mode.map((m) => ({
                  key: m.mode,
                  label: m.mode,
                  value: m.cost_cny,
                  detail: `${fmtCny(m.cost_cny)} · ${m.calls} 次`,
                }))}
              />
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Top 用户（按成本，ID 已脱敏）">
              {usage.top_users.length === 0 ? (
                <p className="py-3 text-center text-xs text-slate-400">暂无数据</p>
              ) : (
                <div className="overflow-x-auto">
                <table className="w-full min-w-72 text-left text-sm">
                  <thead>
                    <tr className="border-b border-slate-100 text-xs text-slate-400">
                      <th className="py-1.5 font-medium">用户</th>
                      <th className="py-1.5 text-right font-medium">调用次数</th>
                      <th className="py-1.5 text-right font-medium">成本</th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.top_users.map((u) => (
                      <tr key={u.user} className="border-b border-slate-50">
                        <td className="py-1.5 font-mono text-xs text-slate-600">
                          {u.user}…
                        </td>
                        <td className="py-1.5 text-right text-slate-600">
                          {fmtInt(u.calls)}
                        </td>
                        <td className="py-1.5 text-right font-mono text-slate-700">
                          {fmtCny(u.cost_cny)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                </div>
              )}
            </Card>

            <Card title="延迟分位 · 按功能">
              {!latency || latency.by_feature.length === 0 ? (
                <p className="py-3 text-center text-xs text-slate-400">暂无数据</p>
              ) : (
                <div className="overflow-x-auto">
                <table className="w-full min-w-80 text-left text-sm">
                  <thead>
                    <tr className="border-b border-slate-100 text-xs text-slate-400">
                      <th className="py-1.5 font-medium">功能</th>
                      <th className="py-1.5 text-right font-medium">调用次数</th>
                      <th className="py-1.5 text-right font-medium">p50 (ms)</th>
                      <th className="py-1.5 text-right font-medium">p95 (ms)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {latency.by_feature.map((f) => (
                      <tr key={f.feature} className="border-b border-slate-50">
                        <td className="py-1.5 font-mono text-xs text-slate-600">
                          {f.feature}
                        </td>
                        <td className="py-1.5 text-right text-slate-600">
                          {fmtInt(f.calls)}
                        </td>
                        <td className="py-1.5 text-right font-mono text-slate-700">
                          {f.p50_ms != null ? f.p50_ms.toFixed(0) : "—"}
                        </td>
                        <td className="py-1.5 text-right font-mono text-slate-700">
                          {f.p95_ms != null ? f.p95_ms.toFixed(0) : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                </div>
              )}
            </Card>
          </div>
        </>
      )}
        </>
      )}
    </div>
  );
}
