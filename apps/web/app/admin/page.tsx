"use client";

// 运营面板（F10，仅管理员）：成本/token 大数卡片、按 feature/model/mode 条形（纯 div）、
// 日趋势折线（纯 SVG polyline）、top 用户表、latency p50/p95 表，
// time-travel 虚拟时钟控制卡（演示遗忘曲线的操作台），
// 以及「检索命中测试」分区（四阶段召回/融合/重排全过程，评委演示 rerank 价值）。

import { useEffect, useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  LoadingState,
  Spinner,
  fmtDateTime,
} from "@/components/common";
import { FEEDBACK_CATEGORY_LABELS } from "@/components/feedback-widget";
import { RetrieveDebug } from "@/components/kb/retrieve-debug";
import { ApiError, apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type {
  AdminLatencyResponse,
  AdminUsageResponse,
  BreakerState,
  FaultPatch,
  FaultState,
  FeedbackCategory,
  FeedbackListResponse,
  FeedbackOut,
  KbOut,
  RegistrationInviteOut,
  TimeTravelInfo,
} from "@/lib/types";

const DAY_OPTIONS = [7, 14, 30];

function fmtCny(v: number): string {
  return `¥${v.toFixed(v > 0 && v < 1 ? 4 : 2)}`;
}

function fmtInt(v: number): string {
  return v.toLocaleString("zh-CN");
}

/** 大数卡片。 */
function StatTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <p className="text-xs text-slate-400">{label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight text-slate-900">
        {value}
      </p>
      {sub && <p className="mt-0.5 text-xs text-slate-400">{sub}</p>}
    </div>
  );
}

/** 分组条形（纯 div 宽度百分比，单一靛蓝系列）。 */
function BarRows({
  rows,
}: {
  rows: { key: string; label: string; value: number; detail: string }[];
}) {
  if (rows.length === 0) {
    return <p className="py-3 text-center text-xs text-slate-400">暂无数据</p>;
  }
  const max = Math.max(...rows.map((r) => r.value), 1e-9);
  return (
    <ul className="space-y-2.5">
      {rows.map((r) => (
        <li key={r.key} className="text-xs">
          <div className="mb-1 flex items-baseline justify-between gap-2">
            <span className="min-w-0 truncate font-mono text-slate-600" title={r.label}>
              {r.label}
            </span>
            <span className="shrink-0 text-slate-500">{r.detail}</span>
          </div>
          <div className="h-3.5 rounded-full bg-slate-100">
            <div
              className="h-full rounded-full bg-indigo-500"
              style={{ width: `${Math.max((r.value / max) * 100, 2)}%` }}
              title={`${r.label}：${fmtCny(r.value)}`}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

/** 日趋势折线（纯 SVG polyline，成本单轴）。 */
function DailyTrend({ daily }: { daily: AdminUsageResponse["daily"] }) {
  if (daily.length === 0) {
    return <p className="py-3 text-center text-xs text-slate-400">窗口内暂无调用</p>;
  }
  const w = 640;
  const h = 180;
  const pad = { l: 14, r: 14, t: 12, b: 22 };
  const max = Math.max(...daily.map((d) => d.cost_cny), 1e-9);
  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  const x = (i: number) =>
    pad.l + (daily.length === 1 ? innerW / 2 : (i / (daily.length - 1)) * innerW);
  const y = (v: number) => pad.t + innerH - (v / max) * innerH;
  const points = daily.map((d, i) => [x(i), y(d.cost_cny)] as const);

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="block min-w-120 w-full"
        role="img"
        aria-label="每日成本趋势"
      >
        {/* 基线与峰值参考线（递弱网格） */}
        <line
          x1={pad.l}
          y1={pad.t + innerH}
          x2={w - pad.r}
          y2={pad.t + innerH}
          stroke="#e2e8f0"
          strokeWidth={1}
        />
        <line
          x1={pad.l}
          y1={pad.t}
          x2={w - pad.r}
          y2={pad.t}
          stroke="#f1f5f9"
          strokeWidth={1}
        />
        <text x={pad.l} y={pad.t - 2} fontSize={10} fill="#94a3b8">
          峰值 {fmtCny(max)}
        </text>

        {daily.length > 1 && (
          <polyline
            points={points.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(" ")}
            fill="none"
            stroke="#4f46e5"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        )}
        {points.map(([px, py], i) => (
          <circle
            key={daily[i].date}
            cx={px}
            cy={py}
            r={3.2}
            fill="#4f46e5"
            stroke="#fff"
            strokeWidth={1.5}
          >
            <title>
              {daily[i].date}：{fmtCny(daily[i].cost_cny)} · {daily[i].calls} 次调用 ·{" "}
              {fmtInt(daily[i].tokens)} tokens
            </title>
          </circle>
        ))}
        {/* 首尾日期标签 */}
        <text x={pad.l} y={h - 6} fontSize={10} fill="#94a3b8">
          {daily[0].date}
        </text>
        {daily.length > 1 && (
          <text x={w - pad.r} y={h - 6} fontSize={10} fill="#94a3b8" textAnchor="end">
            {daily[daily.length - 1].date}
          </text>
        )}
      </svg>
    </div>
  );
}

/** 检索命中测试（管理端）：选可见库或手输任意 kb_id + query → 四阶段命中对比。 */
function RetrievalTestSection() {
  const [kbs, setKbs] = useState<KbOut[] | null>(null);
  const [kbError, setKbError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [manualId, setManualId] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const list = await apiJson<KbOut[]>("/api/kb");
        if (!alive) return;
        setKbs(list);
        setSelectedId((prev) => prev || (list[0]?.id ?? ""));
        setKbError(null);
      } catch (err) {
        if (!alive) return;
        setKbs([]);
        setKbError(err instanceof ApiError ? err.message : "知识库列表加载失败");
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // 手输 kb_id 优先（admin 可测任意库），否则用下拉选中的可见库
  const kbId = manualId.trim() || selectedId;

  return (
    <Card
      title="检索命中测试"
      extra={
        <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-xs text-indigo-600">
          dense / sparse / RRF / rerank 四阶段全透明
        </span>
      }
    >
      <div className="space-y-3">
        {kbError && <ErrorBanner message={kbError} onClose={() => setKbError(null)} />}

        <div className="grid gap-2 sm:grid-cols-2">
          <label className="block">
            <span className="mb-1 block text-xs text-slate-500">选择可见知识库</span>
            <select
              value={selectedId}
              onChange={(e) => setSelectedId(e.target.value)}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            >
              {(kbs ?? []).length === 0 && <option value="">（暂无可见知识库）</option>}
              {(kbs ?? []).map((kb) => (
                <option key={kb.id} value={kb.id}>
                  {kb.name}（{kb.subject} · v{kb.kb_version}）
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-slate-500">
              或直接输入 kb_id（优先生效）
            </span>
            <input
              value={manualId}
              onChange={(e) => setManualId(e.target.value)}
              placeholder="粘贴任意知识库 UUID"
              className="w-full rounded-xl border border-slate-300 px-3 py-2 font-mono text-sm text-slate-900 outline-none placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            />
          </label>
        </div>

        {kbs === null ? (
          <LoadingState text="知识库列表加载中…" />
        ) : !kbId ? (
          <EmptyState
            title="选择或输入一个知识库"
            description="然后输入 query，观察四阶段召回与 rerank 如何改变排序"
          />
        ) : (
          <RetrieveDebug key={kbId} kbId={kbId} />
        )}
      </div>
    </Card>
  );
}

// ---- 故障注入（M5-D2）：演示降级链路与熔断器，现场制造 provider 故障 ----

const BREAKER_TIER_LABEL: Record<string, string> = {
  cheap: "cheap · 低成本档",
  standard: "standard · 主模型",
  strong: "strong · 强模型",
};

const BREAKER_STATE_META: Record<
  BreakerState,
  { text: string; cls: string; dot: string }
> = {
  closed: {
    text: "闭合 · 正常放行",
    cls: "border-emerald-200 bg-emerald-50 text-emerald-700",
    dot: "bg-emerald-500",
  },
  half_open: {
    text: "半开 · 试探恢复",
    cls: "border-amber-300 bg-amber-50 text-amber-700",
    dot: "bg-amber-500",
  },
  open: {
    text: "断开 · 已熔断",
    cls: "border-red-300 bg-red-50 text-red-700",
    dot: "bg-red-500",
  },
};

/** 故障注入控制台：provider 宕机 / 附加延迟 / 错误率 + 熔断器一键操作与状态。 */
function FaultInjectionSection() {
  const [state, setState] = useState<FaultState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 滑块本地态：拖动只改本地，松手/失焦或点「应用」才发请求（避免每帧打接口）
  const [latency, setLatency] = useState(0);
  const [errorRate, setErrorRate] = useState(0);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const s = await apiJson<FaultState>("/api/admin/faults");
        if (!alive) return;
        setState(s);
        setLatency(s.latency_ms);
        setErrorRate(s.error_rate);
        setError(null);
      } catch (err) {
        if (!alive) return;
        setError(err instanceof ApiError ? err.message : "故障注入状态加载失败");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const patch = async (body: FaultPatch) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const s = await apiJson<FaultState>("/api/admin/faults", { body });
      setState(s);
      setLatency(s.latency_ms);
      setErrorRate(s.error_rate);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "故障注入设置失败");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <LoadingState text="故障注入状态加载中…" />;

  const dirty =
    state != null &&
    (latency !== state.latency_ms ||
      Math.abs(errorRate - state.error_rate) > 1e-9);
  const commitSliders = () => {
    if (!dirty) return;
    void patch({ latency_ms: latency, error_rate: errorRate });
  };

  const mode = state?.gateway_mode ?? "unknown";
  const inert = mode === "stub" || mode === "replay";
  const active = state?.active === true;

  return (
    <section
      className={`rounded-2xl border bg-white ${
        active ? "border-red-400 ring-1 ring-red-200" : "border-slate-200"
      }`}
    >
      {active && (
        <div className="flex items-center gap-2 rounded-t-2xl bg-red-600 px-4 py-2 text-sm font-semibold text-white">
          <span className="h-2 w-2 animate-pulse rounded-full bg-white" aria-hidden />
          故障注入生效中 —— 当前所有 live/record 网关调用都会被人为劣化
        </div>
      )}
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-4 py-2.5">
        <h2 className="text-sm font-semibold text-slate-800">故障注入</h2>
        <span className="flex items-center gap-2 text-xs">
          {busy && <Spinner />}
          <span className="text-slate-400">当前网关模式</span>
          <span
            className={`rounded-full px-2 py-0.5 font-mono font-semibold ${
              inert ? "bg-amber-100 text-amber-800" : "bg-indigo-100 text-indigo-700"
            }`}
          >
            {mode}
          </span>
        </span>
      </header>

      <div className="space-y-4 p-4">
        {error && <ErrorBanner message={error} onClose={() => setError(null)} />}

        {inert && (
          <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800">
            当前网关模式为 <span className="font-mono font-semibold">{mode}</span>
            ，故障注入不生效（stub/replay 是演示兜底通道，按设计不受影响）。
            要现场演示降级与熔断，请把网关切到 live 或 record。
          </div>
        )}

        {state?.note && <p className="text-xs text-slate-400">{state.note}</p>}

        {/* provider 宕机开关 */}
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 px-4 py-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-slate-800">模拟 provider 宕机</p>
            <p className="mt-0.5 text-xs text-slate-500">
              打开后上游一律直接失败，用于演示重试 → 降级档位 → 熔断的完整链路。
            </p>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={state?.provider_down === true}
            disabled={busy}
            onClick={() => void patch({ provider_down: !(state?.provider_down === true) })}
            className={`relative h-7 w-13 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
              state?.provider_down ? "bg-red-500" : "bg-slate-300"
            }`}
          >
            <span
              className={`absolute top-1 h-5 w-5 rounded-full bg-white transition-all ${
                state?.provider_down ? "left-7" : "left-1"
              }`}
            />
          </button>
        </div>

        {/* 延迟 / 错误率滑块 */}
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="rounded-xl border border-slate-200 px-4 py-3">
            <div className="mb-2 flex items-baseline justify-between gap-2">
              <span className="text-sm font-medium text-slate-800">附加延迟</span>
              <span className="font-mono text-sm font-semibold text-indigo-700">
                {latency} ms
              </span>
            </div>
            <input
              type="range"
              min={0}
              max={5000}
              step={100}
              value={latency}
              disabled={busy}
              onChange={(e) => setLatency(Number(e.target.value))}
              onPointerUp={commitSliders}
              onBlur={commitSliders}
              className="w-full accent-indigo-600"
              aria-label="附加延迟（毫秒）"
            />
            <p className="mt-1 text-[11px] text-slate-400">
              每次上游调用额外 sleep 这么久，用来触发超时与 p95 抬升。
            </p>
          </div>

          <div className="rounded-xl border border-slate-200 px-4 py-3">
            <div className="mb-2 flex items-baseline justify-between gap-2">
              <span className="text-sm font-medium text-slate-800">随机错误率</span>
              <span className="font-mono text-sm font-semibold text-indigo-700">
                {Math.round(errorRate * 100)}%
              </span>
            </div>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={errorRate}
              disabled={busy}
              onChange={(e) => setErrorRate(Number(e.target.value))}
              onPointerUp={commitSliders}
              onBlur={commitSliders}
              className="w-full accent-indigo-600"
              aria-label="随机错误率"
            />
            <p className="mt-1 text-[11px] text-slate-400">
              按该概率随机抛错，用来演示重试与熔断阈值的累积过程。
            </p>
          </div>
        </div>

        {dirty && (
          <div className="flex items-center justify-end gap-2">
            <span className="text-xs text-amber-700">滑块有未应用的改动</span>
            <button
              type="button"
              disabled={busy}
              onClick={commitSliders}
              className="rounded-xl bg-indigo-600 px-3.5 py-1.5 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              应用
            </button>
          </div>
        )}

        {/* 熔断器 */}
        <div className="rounded-xl border border-slate-200 px-4 py-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm font-medium text-slate-800">熔断器状态</span>
            <span className="flex items-center gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => void patch({ trip_breaker: "standard" })}
                className="rounded-xl border border-red-300 bg-white px-3 py-1.5 text-xs font-semibold text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                一键熔断主模型
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  void patch({
                    reset_breakers: true,
                    provider_down: false,
                    latency_ms: 0,
                    error_rate: 0,
                  })
                }
                className="rounded-xl border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
              >
                恢复全部
              </button>
            </span>
          </div>
          <div className="grid gap-2 sm:grid-cols-3">
            {(state?.breakers ?? []).map((b) => {
              const meta = BREAKER_STATE_META[b.state] ?? BREAKER_STATE_META.closed;
              return (
                <div
                  key={b.tier}
                  className={`flex items-center justify-between gap-2 rounded-xl border px-3 py-2 ${meta.cls}`}
                >
                  <span className="min-w-0 truncate font-mono text-xs">
                    {BREAKER_TIER_LABEL[b.tier] ?? b.tier}
                  </span>
                  <span className="flex shrink-0 items-center gap-1.5 text-xs font-semibold">
                    <span className={`h-2 w-2 rounded-full ${meta.dot}`} aria-hidden />
                    {meta.text}
                  </span>
                </div>
              );
            })}
            {(state?.breakers ?? []).length === 0 && (
              <p className="py-2 text-xs text-slate-400">暂无熔断器信息</p>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

// ---- 用户反馈（P2-1）：admin 工单列表，status 筛选 + 关闭 ----

const FEEDBACK_FILTERS = [
  ["", "全部"],
  ["open", "处理中"],
  ["closed", "已关闭"],
] as const;

function FeedbackAdminSection() {
  const [statusFilter, setStatusFilter] = useState<"" | "open" | "closed">("open");
  const [data, setData] = useState<FeedbackListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const params = new URLSearchParams({ page_size: "50" });
        if (statusFilter) params.set("status", statusFilter);
        const res = await apiJson<FeedbackListResponse>(
          `/api/admin/feedback?${params}`,
        );
        if (!alive) return;
        setData(res);
        setError(null);
      } catch (err) {
        if (!alive) return;
        setError(err instanceof ApiError ? err.message : "反馈列表加载失败");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [statusFilter, reloadKey]);

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
                setLoading(true);
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
                  <td className="max-w-52 py-2 pr-2 font-mono text-[10px] text-slate-400">
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

const INVITE_STATUS_LABEL: Record<RegistrationInviteOut["status"], string> = {
  active: "有效",
  used: "已用",
  revoked: "已撤销",
  expired: "已过期",
};

function RegistrationInvitesSection() {
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

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await apiJson<RegistrationInviteOut[]>(
          "/api/admin/registration-invites",
        );
        if (alive) {
          setItems(data);
          setError(null);
        }
      } catch (err) {
        if (alive) setError(err instanceof ApiError ? err.message : "邀请码列表加载失败");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

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
            <span className="mb-1 block text-xs text-slate-500">数量</span>
            <input
              type="number"
              min={1}
              max={100}
              value={count}
              onChange={(e) => setCount(Math.min(100, Math.max(1, Number(e.target.value))))}
              className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-slate-500">每枚可使用次数</span>
            <input
              type="number"
              min={1}
              max={100}
              value={maxUses}
              onChange={(e) => setMaxUses(Math.min(100, Math.max(1, Number(e.target.value))))}
              className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-slate-500">有效期（天，0 为不过期）</span>
            <input
              type="number"
              min={0}
              max={365}
              value={expiresInDays}
              onChange={(e) => setExpiresInDays(Math.min(365, Math.max(0, Number(e.target.value))))}
              className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
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
        <p className="mt-2 text-xs text-slate-400">
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
                <tr className="border-b border-slate-100 text-xs text-slate-400">
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
                  <tr key={item.id} className="border-b border-slate-50">
                    <td className="py-2 pr-2">
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
                        {INVITE_STATUS_LABEL[item.status]}
                      </span>
                    </td>
                    <td className="whitespace-nowrap py-2 pr-2 text-xs text-slate-500">{fmtDateTime(item.created_at)}</td>
                    <td className="whitespace-nowrap py-2 pr-2 text-xs text-slate-500">{item.expires_at ? fmtDateTime(item.expires_at) : "不过期"}</td>
                    <td className="py-2 pr-2 text-xs text-slate-600">{item.used_count} / {item.max_uses}</td>
                    <td className="py-2 pr-2 text-xs text-slate-600">{item.used_by_email ?? "—"}</td>
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

function NoAccess() {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white px-8 py-12 text-center">
        <span className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-red-50 text-red-500">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-6 w-6"
            aria-hidden
          >
            <path d="M12 15v2m-6 4h12a2 2 0 0 0 2-2v-6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2Zm10-10V7a4 4 0 0 0-8 0v4h8Z" />
          </svg>
        </span>
        <h2 className="text-lg font-semibold text-slate-900">没有访问权限</h2>
        <p className="mt-1 text-sm text-slate-500">
          运营面板仅对管理员开放，请使用管理员账号登录。
        </p>
      </div>
    </div>
  );
}

export default function AdminPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  const [tab, setTab] = useState<
    "ops" | "retrieval" | "faults" | "feedback" | "invites"
  >("ops");
  const [days, setDays] = useState(7);
  const [usage, setUsage] = useState<AdminUsageResponse | null>(null);
  const [latency, setLatency] = useState<AdminLatencyResponse | null>(null);
  const [timeTravel, setTimeTravel] = useState<TimeTravelInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [travelBusy, setTravelBusy] = useState(false);

  const [reloadKey, setReloadKey] = useState(0);

  // 数据加载：days/reloadKey 变化触发（effect 内联 IIFE，同 chat 页模式）；
  // 切换时间窗口时保留旧数据展示，新数据到达后整体替换。
  useEffect(() => {
    if (!isAdmin) return;
    let alive = true;
    (async () => {
      const [usageRes, latencyRes, ttRes] = await Promise.allSettled([
        apiJson<AdminUsageResponse>(`/api/admin/usage?days=${days}`),
        apiJson<AdminLatencyResponse>(`/api/admin/usage/latency?days=${days}`),
        apiJson<TimeTravelInfo>("/api/admin/time-travel"),
      ]);
      if (!alive) return;
      if (usageRes.status === "fulfilled") {
        setUsage(usageRes.value);
        setError(null);
      } else {
        setError(
          usageRes.reason instanceof ApiError
            ? usageRes.reason.message
            : "用量数据加载失败",
        );
      }
      if (latencyRes.status === "fulfilled") setLatency(latencyRes.value);
      if (ttRes.status === "fulfilled") setTimeTravel(ttRes.value);
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, [isAdmin, days, reloadKey]);

  const reload = () => {
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

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
