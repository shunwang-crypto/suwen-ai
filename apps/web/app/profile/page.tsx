"use client";

// AI 眼中的我（F8）：掌握度雷达图（纯 SVG polygon，≤12 维）+ 掌握度列表（进度条+confidence）
// + 错题分布条形（纯 div）+ 最近掌握度变化流水；每行「我已掌握」→ POST override 后即时刷新。

import { useEffect, useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  LoadingState,
  Spinner,
  fmtDateTime,
  fmtPct,
} from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import type { MasteryItem, OverrideResult, ProfileResponse } from "@/lib/types";

const CAUSE_LABELS: Record<string, string> = {
  concept_confusion: "概念混淆",
  formula_error: "公式用错",
  calc_slip: "计算失误",
  misread: "审题偏差",
  knowledge_gap: "知识缺口",
};

const STATE_LABELS: Record<string, string> = {
  new: "新错题",
  reviewing: "复习中",
  mastered: "已掌握",
};

const EVIDENCE_LABELS: Record<string, string> = {
  quiz_attempt: "练习作答",
  review: "复习作答",
  user_override: "用户纠正",
};

const RADAR_MAX_DIMS = 12;

/** 纯 SVG 雷达图：≤12 维，轴标 KC 名；网格环 25/50/75/100%。 */
function MasteryRadar({ items }: { items: MasteryItem[] }) {
  const dims = items.slice(0, RADAR_MAX_DIMS);
  if (dims.length < 3) {
    return (
      <p className="py-8 text-center text-xs text-slate-400">
        至少需要 3 个知识点才能绘制雷达图，先去练习积累证据吧
      </p>
    );
  }
  const size = 340;
  const cx = size / 2;
  const cy = size / 2;
  const radius = 108;
  const labelRadius = radius + 20;
  const n = dims.length;

  const point = (i: number, r: number): [number, number] => {
    const angle = (Math.PI * 2 * i) / n - Math.PI / 2;
    return [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
  };

  const ringPath = (ratio: number) =>
    dims
      .map((_, i) => point(i, radius * ratio).map((v) => v.toFixed(1)).join(","))
      .join(" ");

  const dataPoints = dims.map((d, i) => point(i, radius * Math.max(0.02, d.p_mastery)));

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${size} ${size}`}
        className="mx-auto block w-full max-w-90"
        role="img"
        aria-label={`掌握度雷达图，共 ${n} 个知识点`}
      >
        {/* 网格环（递弱视觉） */}
        {[0.25, 0.5, 0.75, 1].map((r) => (
          <polygon
            key={r}
            points={ringPath(r)}
            fill="none"
            stroke="#e2e8f0"
            strokeWidth={r === 1 ? 1.2 : 0.8}
          />
        ))}
        {/* 轴线 */}
        {dims.map((_, i) => {
          const [x, y] = point(i, radius);
          return (
            <line
              key={i}
              x1={cx}
              y1={cy}
              x2={x}
              y2={y}
              stroke="#e2e8f0"
              strokeWidth={0.8}
            />
          );
        })}
        {/* 数据多边形 */}
        <polygon
          points={dataPoints.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}
          fill="rgba(79, 70, 229, 0.14)"
          stroke="#4f46e5"
          strokeWidth={2}
          strokeLinejoin="round"
        />
        {/* 数据点（白描边与填充分离重叠标记） */}
        {dataPoints.map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r={3.2} fill="#4f46e5" stroke="#fff" strokeWidth={1.5}>
            <title>
              {dims[i].name}：{fmtPct(dims[i].p_mastery)}
            </title>
          </circle>
        ))}
        {/* 轴标签 */}
        {dims.map((d, i) => {
          const [x, y] = point(i, labelRadius);
          const anchor =
            Math.abs(x - cx) < 8 ? "middle" : x > cx ? "start" : "end";
          const name = d.name.length > 7 ? `${d.name.slice(0, 6)}…` : d.name;
          return (
            <text
              key={i}
              x={x}
              y={y + 3}
              textAnchor={anchor}
              fontSize={10.5}
              fill="#64748b"
            >
              <title>{d.name}</title>
              {name}
            </text>
          );
        })}
      </svg>
    </div>
  );
}

/** 计数条形行（纯 div 宽度百分比）。 */
function CountBars({
  data,
  labels,
}: {
  data: Record<string, number>;
  labels: Record<string, string>;
}) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) {
    return <p className="py-3 text-center text-xs text-slate-400">暂无数据</p>;
  }
  const max = Math.max(...entries.map(([, n]) => n), 1);
  return (
    <ul className="space-y-2">
      {entries.map(([key, n]) => (
        <li key={key} className="flex items-center gap-2 text-xs">
          <span className="w-16 shrink-0 truncate text-slate-500" title={key}>
            {labels[key] ?? key}
          </span>
          <span className="h-3.5 min-w-0 flex-1 rounded-full bg-slate-100">
            <span
              className="block h-full rounded-full bg-indigo-500"
              style={{ width: `${Math.max((n / max) * 100, 4)}%` }}
              title={`${labels[key] ?? key}：${n}`}
            />
          </span>
          <span className="w-8 shrink-0 text-right font-mono text-slate-600">{n}</span>
        </li>
      ))}
    </ul>
  );
}

export default function ProfilePage() {
  const [profile, setProfile] = useState<ProfileResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [overridingId, setOverridingId] = useState<string | null>(null);

  const [reloadKey, setReloadKey] = useState(0);

  // 画像加载：reloadKey 自增触发（effect 内联 IIFE，同 chat 页模式）
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await apiJson<ProfileResponse>("/api/profile");
        if (!alive) return;
        setProfile(res);
        setError(null);
      } catch (err) {
        if (alive) {
          setError(err instanceof ApiError ? err.message : "画像加载失败");
        }
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [reloadKey]);

  const reload = () => {
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  const override = async (item: MasteryItem) => {
    if (overridingId) return;
    setOverridingId(item.kc_id);
    setBanner(null);
    try {
      await apiJson<OverrideResult>(`/api/profile/kc/${item.kc_id}/override`, {
        body: { p_mastery: 0.9 },
      });
      // 静默即时刷新（含变化流水里新增的 user_override 记录）
      setProfile(await apiJson<ProfileResponse>("/api/profile"));
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "纠正失败，请重试");
    } finally {
      setOverridingId(null);
    }
  };

  if (loading) {
    return (
      <div className="mx-auto w-full max-w-5xl p-4">
        <LoadingState text="正在加载 AI 眼中的你…" />
      </div>
    );
  }

  if (error || !profile) {
    return (
      <div className="mx-auto w-full max-w-5xl p-4">
        <ErrorBanner message={error ?? "画像加载失败"} onRetry={reload} />
      </div>
    );
  }

  const { mastery, error_distribution, recent_changes } = profile;

  return (
    <div className="mx-auto w-full max-w-5xl space-y-4 p-4">
      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}

      {mastery.length === 0 && recent_changes.length === 0 ? (
        <EmptyState
          title="AI 还不认识你"
          description="完成一次练习或复习后，这里会生成你的掌握度画像"
        />
      ) : (
        <>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card
              title="掌握度雷达"
              extra={
                <span className="text-xs text-slate-400">
                  {Math.min(mastery.length, RADAR_MAX_DIMS)} / {mastery.length} 个知识点
                </span>
              }
            >
              <MasteryRadar items={mastery} />
            </Card>

            <Card title="掌握度明细">
              {mastery.length === 0 ? (
                <p className="py-3 text-center text-xs text-slate-400">
                  暂无掌握度记录
                </p>
              ) : (
                <ul className="max-h-96 space-y-2.5 overflow-y-auto pr-1">
                  {mastery.map((m) => (
                    <li key={m.kc_id} className="flex items-center gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-baseline justify-between gap-2">
                          <span
                            className="truncate text-sm font-medium text-slate-700"
                            title={m.name}
                          >
                            {m.name}
                          </span>
                          <span className="shrink-0 font-mono text-xs text-slate-500">
                            {fmtPct(m.p_mastery)}
                            <span className="ml-1.5 text-slate-400">
                              置信 {fmtPct(m.confidence)}
                            </span>
                          </span>
                        </div>
                        <div className="mt-1 h-2 rounded-full bg-slate-100">
                          <div
                            className={`h-full rounded-full ${
                              m.p_mastery >= 0.7
                                ? "bg-emerald-500"
                                : m.p_mastery >= 0.4
                                  ? "bg-indigo-500"
                                  : "bg-amber-500"
                            }`}
                            style={{ width: `${Math.max(m.p_mastery * 100, 2)}%` }}
                          />
                        </div>
                      </div>
                      <button
                        type="button"
                        disabled={overridingId !== null || m.p_mastery >= 0.9}
                        onClick={() => void override(m)}
                        title="用户对画像有最终解释权：直写 p_mastery=0.9 并留审计日志"
                        className="flex shrink-0 items-center gap-1 rounded-lg border border-slate-200 px-2 py-1 text-xs font-medium text-slate-600 hover:border-indigo-300 hover:text-indigo-700 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        {overridingId === m.kc_id && <Spinner className="h-3 w-3" />}
                        我已掌握
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="错题分布 · 按错因">
              <CountBars data={error_distribution.by_cause} labels={CAUSE_LABELS} />
            </Card>
            <Card title="错题分布 · 按状态">
              <CountBars data={error_distribution.by_state} labels={STATE_LABELS} />
            </Card>
          </div>

          <Card title="最近掌握度变化">
            {recent_changes.length === 0 ? (
              <p className="py-3 text-center text-xs text-slate-400">暂无变化记录</p>
            ) : (
              <ul className="divide-y divide-slate-100">
                {recent_changes.map((c) => {
                  const up = c.new_p >= c.old_p;
                  return (
                    <li
                      key={c.id}
                      className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm"
                    >
                      <span className="w-32 shrink-0 text-xs text-slate-400">
                        {fmtDateTime(c.at)}
                      </span>
                      <span
                        className="min-w-0 flex-1 truncate font-medium text-slate-700"
                        title={c.kc_name || c.kc_id}
                      >
                        {c.kc_name || `KC ${c.kc_id.slice(0, 8)}`}
                      </span>
                      <span
                        className={`font-mono text-xs ${
                          up ? "text-emerald-600" : "text-red-600"
                        }`}
                      >
                        {c.old_p.toFixed(2)} → {c.new_p.toFixed(2)}
                      </span>
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-500">
                        {EVIDENCE_LABELS[c.evidence_type] ?? c.evidence_type}
                        {c.weight !== 1 && ` ×${c.weight}`}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
