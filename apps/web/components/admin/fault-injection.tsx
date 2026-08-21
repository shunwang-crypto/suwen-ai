"use client";

// 故障注入分区（admin 故障 tab）：熔断分层/状态控制与观测。

import { useEffect, useState } from "react";

import {
  ErrorBanner,
  LoadingState,
  Spinner,
} from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import type {BreakerState, FaultPatch, FaultState} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

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
export function FaultInjectionSection() {
  // 状态加载（A5 收敛: useApiResource; patch 成功仍直接落 state）
  const faultRes = useApiResource<FaultState>("/api/admin/faults", {
    errorMessage: "故障注入状态加载失败",
  });
  const loading = faultRes.loading;
  const [state, setState] = useState<FaultState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 滑块本地态：拖动只改本地，松手/失焦或点「应用」才发请求（避免每帧打接口）
  const [latency, setLatency] = useState(0);
  const [errorRate, setErrorRate] = useState(0);

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 控制台初始态的单向同步 */
  useEffect(() => {
    const s = faultRes.data;
    if (!s) return;
    setState(s);
    setLatency(s.latency_ms);
    setErrorRate(s.error_rate);
  }, [faultRes.data]);

  useEffect(() => {
    if (faultRes.error) setError(faultRes.error);
  }, [faultRes.error]);
  /* eslint-enable react-hooks/set-state-in-effect */

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
        active ? "border-red-400 ring-1 ring-red-200" : "border-line"
      }`}
    >
      {active && (
        <div className="flex items-center gap-2 rounded-t-2xl bg-red-600 px-4 py-2 text-sm font-semibold text-white">
          <span className="h-2 w-2 animate-pulse rounded-full bg-white" aria-hidden />
          故障注入生效中 —— 当前所有 live/record 网关调用都会被人为劣化
        </div>
      )}
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2.5">
        <h2 className="text-sm font-semibold text-ink">故障注入</h2>
        <span className="flex items-center gap-2 text-xs">
          {busy && <Spinner />}
          <span className="text-ink-muted">当前网关模式</span>
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

        {state?.note && <p className="text-xs text-ink-muted">{state.note}</p>}

        {/* provider 宕机开关 */}
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line px-4 py-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-ink">模拟 provider 宕机</p>
            <p className="mt-0.5 text-xs text-ink-muted">
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
              state?.provider_down ? "bg-red-500" : "bg-surface-muted"
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
          <div className="rounded-xl border border-line px-4 py-3">
            <div className="mb-2 flex items-baseline justify-between gap-2">
              <span className="text-sm font-medium text-ink">附加延迟</span>
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
            <p className="mt-1 text-body-sm text-ink-muted">
              每次上游调用额外 sleep 这么久，用来触发超时与 p95 抬升。
            </p>
          </div>

          <div className="rounded-xl border border-line px-4 py-3">
            <div className="mb-2 flex items-baseline justify-between gap-2">
              <span className="text-sm font-medium text-ink">随机错误率</span>
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
            <p className="mt-1 text-body-sm text-ink-muted">
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
        <div className="rounded-xl border border-line px-4 py-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm font-medium text-ink">熔断器状态</span>
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
                className="rounded-xl border border-line-strong bg-white px-3 py-1.5 text-xs font-medium text-ink-secondary hover:bg-surface-muted disabled:cursor-not-allowed disabled:opacity-50"
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
              <p className="py-2 text-xs text-ink-muted">暂无熔断器信息</p>
            )}
            {(state?.routing ?? []).length > 0 && (
              <div className="mt-4 border-t border-line pt-3">
                <p className="mb-2 text-xs font-medium text-ink-muted">业务路由候选</p>
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  {(state?.routing ?? []).map((candidate) => {
                    const meta = BREAKER_STATE_META[candidate.state] ?? BREAKER_STATE_META.closed;
                    return (
                      <button
                        key={candidate.id}
                        type="button"
                        disabled={busy || candidate.disabled}
                        onClick={() => void patch({ trip_breaker: candidate.id as FaultPatch["trip_breaker"] })}
                        className={`rounded-xl border px-3 py-2 text-left disabled:cursor-not-allowed disabled:opacity-50 ${meta.cls}`}
                        title={candidate.last_failure_code ?? "无最近失败"}
                      >
                        <span className="block truncate font-mono text-xs">{candidate.id}</span>
                        <span className="mt-1 block truncate text-body-sm opacity-80">
                          {candidate.provider} · {candidate.model || "未配置"}
                        </span>
                        <span className="mt-1 flex items-center gap-1.5 text-xs font-semibold">
                          <span className={`h-2 w-2 rounded-full ${meta.dot}`} aria-hidden />
                          {candidate.disabled ? "已禁用" : meta.text}
                          {candidate.last_failure_code ? ` · ${candidate.last_failure_code}` : ""}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
