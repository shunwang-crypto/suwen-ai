"use client";

// 掌握度雷达（画像页专用，A1 雷达语义修复）：
// 旧版直接取 mastery 前 12 个 KC 当轴 —— 152 KC 真实规模下轴集合随数据漂移、
// 不可跨时间对比，且轴少时退化成「三角形雷达」。本组件改为「库 → 章节」聚合：
// - 库切换 tab：单库视图轴=章节（稳定、同构、可对比）；「全部」视图轴=库。
// - 轴数 < MIN_RADAR_AXES(5) 时自动降级为水平条形图（少轴雷达无形状可读性差）。
// - 轴可点击下钻，展开该章 KC 列表（薄弱在前，可跳「验证一下」3 题重估）。
// - 后端契约字段（kb_id/kb_name/subject/chapter，并行开发中）缺失时整体降级回
//   components/radar.tsx 的 top12 行为，页面不崩。
// 章节聚合在前端由 mastery[] 完成（一次请求同时喂雷达与明细树），与
// GET /api/reports/learning?kb_id= 的 chapter 轴聚合口径一致（kc_count/weak_count）。

import Link from "next/link";
import { useState } from "react";

import { Card, fmtPct } from "@/components/common";
import { MasteryRadar, RADAR_MAX_DIMS } from "@/components/radar";
import { masteryColor } from "@/components/ui/progress-ring";

/** 聚合输入行：可选字段缺失时安全降级。 */
export interface RadarSourceItem {
  kc_id: string;
  name: string;
  p_mastery: number | null;
  evidence_state?: "unobserved" | "estimated" | "observed";
  kb_id?: string | null;
  kb_name?: string | null;
  subject?: string | null;
  chapter?: string | null;
}

/** 轴数低于此值的雷达没有可读形状（3 轴即被审计点名的三角形），换条形图。 */
const MIN_RADAR_AXES = 5;

const ALL_TAB = "__all__";

interface AggAxis {
  key: string;
  label: string;
  /** 轴值 = 组内 KC 掌握度均值 */
  mean: number;
  weakCount: number;
  kcs: RadarSourceItem[];
}

/** 按 keyOf 分组聚合为轴，保持首次出现顺序（章节序稳定 → 轴序稳定可对比）。 */
function groupAxes(
  items: RadarSourceItem[],
  weakThreshold: number,
  keyOf: (m: RadarSourceItem) => string,
  labelOf: (m: RadarSourceItem) => string,
): AggAxis[] {
  const axes: AggAxis[] = [];
  const byKey = new Map<string, AggAxis>();
  for (const m of items) {
    const key = keyOf(m);
    let axis = byKey.get(key);
    if (!axis) {
      axis = { key, label: labelOf(m), mean: 0, weakCount: 0, kcs: [] };
      byKey.set(key, axis);
      axes.push(axis);
    }
    axis.kcs.push(m);
    if (m.evidence_state === "observed" && m.p_mastery != null && m.p_mastery < weakThreshold) axis.weakCount += 1;
  }
  for (const axis of axes) {
    const observed = axis.kcs.filter((item) => item.evidence_state === "observed" && item.p_mastery != null);
    axis.mean = observed.length ? observed.reduce((sum, item) => sum + (item.p_mastery ?? 0), 0) / observed.length : 0;
  }
  return axes;
}

const kbKey = (m: RadarSourceItem) => m.kb_id ?? m.kb_name ?? m.subject ?? "__kb__";
const kbLabel = (m: RadarSourceItem) => m.kb_name ?? m.subject ?? "未分组";
const chapterKey = (m: RadarSourceItem) => m.chapter ?? "__misc__";
const chapterLabel = (m: RadarSourceItem) => m.chapter ?? "未分章";

function axisTitle(a: AggAxis): string {
  return `${a.label}：平均掌握度 ${fmtPct(a.mean)} · ${a.kcs.length} 个知识点 · 薄弱 ${a.weakCount} 个（点击下钻）`;
}

/** 可点击聚合雷达（≥5 轴）：几何同 components/radar.tsx，轴组可点/可键盘触发。 */
function AggRadarChart({
  axes,
  selected,
  onSelect,
}: {
  axes: AggAxis[];
  selected: string | null;
  onSelect: (key: string) => void;
}) {
  const size = 340;
  const cx = size / 2;
  const cy = size / 2;
  const radius = 108;
  const labelRadius = radius + 20;
  const n = axes.length;

  const point = (i: number, r: number): [number, number] => {
    const angle = (Math.PI * 2 * i) / n - Math.PI / 2;
    return [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
  };

  const ringPath = (ratio: number) =>
    axes
      .map((_, i) => point(i, radius * ratio).map((v) => v.toFixed(1)).join(","))
      .join(" ");

  const dataPoints = axes.map((a, i) => point(i, radius * Math.max(0.02, a.mean)));

  return (
    <div className="relative">
      {/* 卡片光晕背景（纯 CSS 径向渐变，不影响 SVG 绘制） */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 mx-auto max-w-90"
        style={{
          background:
            "radial-gradient(circle at 50% 46%, rgba(99,102,241,0.10) 0%, rgba(99,102,241,0.04) 42%, transparent 68%)",
        }}
      />
      <svg
        viewBox={`0 0 ${size} ${size}`}
        className="relative mx-auto block w-full max-w-90"
        role="img"
        aria-label={`掌握度雷达图，共 ${n} 个聚合轴，点击轴可下钻该组知识点`}
      >
        {[0.25, 0.5, 0.75, 1].map((r) => (
          <polygon
            key={r}
            points={ringPath(r)}
            fill="none"
            stroke="#e2e8f0"
            strokeWidth={r === 1 ? 1.2 : 0.8}
          />
        ))}
        {axes.map((_, i) => {
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
        <polygon
          points={dataPoints.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}
          fill="rgba(79, 70, 229, 0.14)"
          stroke="#4f46e5"
          strokeWidth={2}
          strokeLinejoin="round"
        />
        {/* 轴交互组：热区线 + 数据点 + 标签，点击/回车下钻 */}
        {axes.map((a, i) => {
          const [px, py] = dataPoints[i];
          const [lx, ly] = point(i, labelRadius);
          const active = selected === a.key;
          const anchor = Math.abs(lx - cx) < 8 ? "middle" : lx > cx ? "start" : "end";
          const short = a.label.length > 7 ? `${a.label.slice(0, 6)}…` : a.label;
          return (
            <g
              key={a.key}
              role="button"
              tabIndex={0}
              aria-label={axisTitle(a)}
              className="cursor-pointer focus:outline-none"
              onClick={() => onSelect(a.key)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelect(a.key);
                }
              }}
            >
              <title>{axisTitle(a)}</title>
              {/* 透明加宽热区（沿整根轴，含标签方向） */}
              <line x1={cx} y1={cy} x2={lx} y2={ly} stroke="transparent" strokeWidth={18} />
              <circle
                cx={px}
                cy={py}
                r={active ? 4.6 : 3.2}
                fill={active ? "#4338ca" : "#4f46e5"}
                stroke="#fff"
                strokeWidth={1.5}
              />
              <text
                x={lx}
                y={ly + 3}
                textAnchor={anchor}
                fontSize={10.5}
                fill={active ? "#4338ca" : "#64748b"}
                fontWeight={active ? 700 : 400}
              >
                {short}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="relative mt-1 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-body-sm text-ink-muted">
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 rounded-sm border border-indigo-600"
            style={{ background: "rgba(79, 70, 229, 0.14)" }}
          />
          章均掌握度
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 rounded-sm border border-line-strong"
          />
          网格环 25 / 50 / 75 / 100%
        </span>
      </div>
    </div>
  );
}

/** 轴数不足时的降级视图：水平条形（同样可点击下钻）。 */
function AxisBars({
  axes,
  selected,
  onSelect,
}: {
  axes: AggAxis[];
  selected: string | null;
  onSelect: (key: string) => void;
}) {
  return (
    <div className="space-y-1.5 py-1">
      {axes.map((a) => {
        const active = selected === a.key;
        return (
          <button
            key={a.key}
            type="button"
            aria-pressed={active}
            title={axisTitle(a)}
            onClick={() => onSelect(a.key)}
            className={`flex w-full items-center gap-2.5 rounded-xl border px-2.5 py-2 text-left transition-colors ${
              active
                ? "border-indigo-300 bg-indigo-50/60"
                : "border-transparent hover:border-line hover:bg-surface-muted"
            }`}
          >
            <span
              className="w-24 shrink-0 truncate text-xs font-medium text-ink-secondary sm:w-32"
              title={a.label}
            >
              {a.label}
            </span>
            <span className="h-3.5 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-muted">
              <span
                className="block h-full rounded-full transition-[width] duration-500 motion-reduce:transition-none"
                style={{
                  width: `${Math.max(a.mean * 100, 3)}%`,
                  background: masteryColor(a.mean),
                }}
              />
            </span>
            <span className="w-10 shrink-0 text-right font-mono text-xs text-ink-secondary">
              {fmtPct(a.mean)}
            </span>
            {a.weakCount > 0 && (
              <span className="shrink-0 rounded-full bg-amber-50 px-1.5 py-px text-body-sm font-medium text-amber-700 ring-1 ring-inset ring-amber-200/70">
                薄弱 {a.weakCount}
              </span>
            )}
          </button>
        );
      })}
      <p className="pt-1 text-center text-body-sm text-ink-muted">
        聚合轴不足 {MIN_RADAR_AXES} 个，已降级为条形图（避免无形状的少轴雷达）
      </p>
    </div>
  );
}

/** 下钻面板：选中轴（章/库）内的 KC 列表，薄弱在前，可跳 3 题验证。 */
function DrillDownPanel({ axis, weakThreshold, onClose }: { axis: AggAxis; weakThreshold: number; onClose: () => void }) {
  const sorted = [...axis.kcs].sort((x, y) => (x.p_mastery == null ? 1 : x.p_mastery) - (y.p_mastery == null ? 1 : y.p_mastery));
  return (
    <div className="edu-fade-up mt-3 rounded-xl border border-indigo-100 bg-indigo-50/40 p-3">
      <div className="flex items-center justify-between gap-2">
        <p className="min-w-0 truncate text-xs font-semibold text-ink-secondary" title={axis.label}>
          {axis.label} · 均 {fmtPct(axis.mean)} · {axis.kcs.length} 个知识点
          {axis.weakCount > 0 ? ` · 薄弱 ${axis.weakCount}` : ""}
        </p>
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 rounded-lg px-1.5 py-0.5 text-xs text-ink-muted hover:bg-white hover:text-ink-secondary"
        >
          收起
        </button>
      </div>
      <ul className="mt-2 max-h-52 space-y-1 overflow-y-auto pr-1">
        {sorted.map((k) => (
          <li key={k.kc_id} className="flex items-center gap-2 text-xs">
            <Link
              href={`/practice/session?mode=quiz&from=${encodeURIComponent("/growth")}&kc=${encodeURIComponent(k.name)}&n=3`}
              title={`${k.name}：做 3 道该知识点的题，判分结果作为真实证据重估掌握度`}
              className="w-32 shrink-0 truncate font-medium text-ink-secondary hover:text-indigo-700 hover:underline sm:w-40"
            >
              {k.name}
            </Link>
            <span className="h-2.5 min-w-0 flex-1 overflow-hidden rounded-full bg-white ring-1 ring-inset ring-line/70">
              <span
                className="block h-full rounded-full"
                style={{
                  width: `${k.p_mastery == null ? 3 : Math.max(k.p_mastery * 100, 3)}%`,
                  background: k.p_mastery == null ? "#cbd5e1" : masteryColor(k.p_mastery),
                }}
              />
            </span>
            <span className="w-9 shrink-0 text-right font-mono text-ink-secondary">
              {k.p_mastery == null ? "—" : fmtPct(k.p_mastery)}
            </span>
            <span className="w-7 shrink-0 text-right text-body-sm font-medium text-amber-600">
              {k.evidence_state === "observed" && k.p_mastery != null && k.p_mastery < weakThreshold ? "薄弱" : k.evidence_state === "estimated" ? "估计" : k.evidence_state === "unobserved" ? "未知" : ""}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * 掌握度雷达卡片（画像页）：库 tab 切换 + 章节/库聚合雷达 + 少轴条形降级 + 轴下钻。
 * 数据缺 chapter 字段时降级回 top12 KC 雷达（旧行为，不崩）。
 */
export function MasteryRadarPanel({ items, weakThreshold }: { items: RadarSourceItem[]; weakThreshold: number }) {
  const [tab, setTab] = useState<string | null>(null);
  const [drill, setDrill] = useState<string | null>(null);

  // 契约就绪判定：任一行带 chapter 才启用聚合语义（后端并行开发中）
  const contractReady = items.some((m) => m.chapter != null && m.chapter !== "");

  if (!contractReady) {
    return (
      <Card
        title="掌握度雷达"
        extra={
          <span className="text-xs text-ink-muted">
            {Math.min(items.length, RADAR_MAX_DIMS)} / {items.length} 个知识点
          </span>
        }
      >
        <MasteryRadar items={items} />
      </Card>
    );
  }

  const kbTabs = groupAxes(items, weakThreshold, kbKey, kbLabel);
  const showAllTab = kbTabs.length > 1;
  const activeTab =
    tab != null && (tab === ALL_TAB ? showAllTab : kbTabs.some((t) => t.key === tab))
      ? tab
      : (kbTabs[0]?.key ?? ALL_TAB);

  const axes =
    activeTab === ALL_TAB
        ? kbTabs
        : groupAxes(
          kbTabs.find((t) => t.key === activeTab)?.kcs ?? [],
          weakThreshold,
          chapterKey,
          chapterLabel,
        );
  const shownAxes = axes.slice(0, RADAR_MAX_DIMS);
  const useBars = shownAxes.length < MIN_RADAR_AXES;
  const drillAxis = drill != null ? (shownAxes.find((a) => a.key === drill) ?? null) : null;

  const totalKcs = axes.reduce((s, a) => s + a.kcs.length, 0);
  const totalWeak = axes.reduce((s, a) => s + a.weakCount, 0);

  const pickTab = (key: string) => {
    setTab(key);
    setDrill(null);
  };
  const toggleDrill = (key: string) => setDrill((cur) => (cur === key ? null : key));

  const tabChip = (active: boolean) =>
    `rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
      active
        ? "border-indigo-600 bg-indigo-600 text-white shadow-sm"
        : "border-line bg-white text-ink-secondary hover:border-indigo-300 hover:text-indigo-700"
    }`;

  return (
    <Card
      title="掌握度雷达"
      extra={
        <span className="text-xs text-ink-muted">
          {activeTab === ALL_TAB
            ? `${axes.length} 个库`
            : `${shownAxes.length}${axes.length > shownAxes.length ? ` / ${axes.length}` : ""} 章`}
          {` · ${totalKcs} 个知识点`}
          {totalWeak > 0 ? ` · 薄弱 ${totalWeak}` : ""}
        </span>
      }
    >
      <div className="space-y-2.5">
        {/* 库切换 tab：单库轴=章节（稳定同构可对比），「全部」轴=库 */}
        <div className="flex flex-wrap items-center gap-1.5" aria-label="按知识库切换雷达视图">
          {kbTabs.map((t) => (
            <button
              key={t.key}
              type="button"
              aria-pressed={activeTab === t.key}
              onClick={() => pickTab(t.key)}
              className={tabChip(activeTab === t.key)}
            >
              {t.label}
            </button>
          ))}
          {showAllTab && (
            <button
              type="button"
              aria-pressed={activeTab === ALL_TAB}
              onClick={() => pickTab(ALL_TAB)}
              className={tabChip(activeTab === ALL_TAB)}
              title="跨库总览：每根轴是一个知识库的平均掌握度"
            >
              全部
            </button>
          )}
        </div>

        {useBars ? (
          <AxisBars axes={shownAxes} selected={drill} onSelect={toggleDrill} />
        ) : (
          <>
            <AggRadarChart axes={shownAxes} selected={drill} onSelect={toggleDrill} />
            <p className="text-center text-body-sm text-ink-muted">
              {axes.length > shownAxes.length ? `仅显示前 ${RADAR_MAX_DIMS} 章 · ` : ""}
              点击任意轴可下钻查看该{activeTab === ALL_TAB ? "库" : "章"}的知识点
            </p>
          </>
        )}

        {drillAxis && <DrillDownPanel axis={drillAxis} weakThreshold={weakThreshold} onClose={() => setDrill(null)} />}
      </div>
    </Card>
  );
}
