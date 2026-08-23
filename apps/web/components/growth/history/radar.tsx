

"use client";

// 学情报告（F7）：掌握度雷达（章节聚合轴 + 知识库切换 tab；observed 轴数<5 或移动端降级条形图，
// 防轴标签重叠）+ 30 天趋势（答题数/正确率双系列，纯 SVG、两联面板共享横轴——不做双 Y 轴）
// + 薄弱点卡片（按科目分节，「红线/待巩固」两口径分开计数，节内 p 升序 top5 + 展开全部）
// + 可执行建议（带跳转）+ narrative 三段式诊断（统一由 ModelMarkdown 渲染）。


import {fmtPct} from "@/components/common";
import { MasteryRadar } from "@/components/radar";
import type {ReportRadarAxis} from "@/lib/types";

// ---- 后端可选字段与空值降级 ----

/** 每节薄弱点默认展示条数（p 升序 top5，其余「展开全部」）。 */

/** 30 天趋势：上联答题数（柱），下联正确率（折线，固定 0–100% 刻度），共享横轴。 */
export function AxisBars({ axes, weakThreshold }: { axes: ReportRadarAxis[]; weakThreshold: number }) {
  return (
    <ul className="space-y-2">
      {axes.map((a) => {
        // 聚合轴只有存在 observed KC 时才有 p_mastery；空值表示尚无可展示证据。
        const observed = a.p_mastery != null;
        const value = a.p_mastery ?? 0;
        const barColor =
          !observed
            ? "bg-surface-muted"
            : value < weakThreshold
            ? "bg-rose-400"
            : "bg-indigo-500";
        return (
          <li key={a.key || a.name} className="flex items-center gap-2 text-caption">
            <span
              className="w-24 shrink-0 truncate text-ink-secondary sm:w-32"
              title={a.name}
            >
              {a.name}
            </span>
            <span className="h-2.5 min-w-0 flex-1 rounded-full bg-surface-muted">
              <span
                className={`block h-full rounded-full ${barColor}`}
                style={{ width: observed ? `${Math.max(value * 100, 2)}%` : "0%" }}
              />
            </span>
            <span className="w-10 shrink-0 text-right font-mono text-ink-secondary">
              {!observed ? "待验证" : fmtPct(value)}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** 章节聚合轴下钻：每轴 KC 数 / 薄弱数徽章 + KC 明细（后端契约字段缺失时不渲染）。 */
export function AxisDrilldown({ axes }: { axes: ReportRadarAxis[] }) {
  const rows = axes.filter(
    (a) => a.kc_count != null || a.weak_count != null || (a.kcs?.length ?? 0) > 0,
  );
  if (rows.length === 0) return null;
  return (
    <div className="mt-3 space-y-1.5 border-t border-line pt-2.5">
      {rows.map((a) => (
        <details
          key={a.key || a.name}
          className="rounded-lg bg-surface-muted/70 px-2.5 py-1.5"
        >
          <summary className="flex cursor-pointer flex-wrap items-center gap-1.5 text-body-sm">
            <span
              className="min-w-0 flex-1 truncate font-medium text-ink-secondary"
              title={a.name}
            >
              {a.name}
            </span>
            <span className="shrink-0 font-mono text-ink-muted">
              {a.p_mastery == null ? "待验证" : fmtPct(a.p_mastery)}
            </span>
            {a.kc_count != null && (
              <span className="shrink-0 rounded-full bg-surface-muted/70 px-1.5 py-0.5 text-caption text-ink-secondary">
                {a.kc_count} 个知识点
              </span>
            )}
            {a.weak_count != null && a.weak_count > 0 && (
              <span className="shrink-0 rounded-full bg-rose-50 px-1.5 py-0.5 text-caption font-medium text-rose-600">
                {a.weak_count} 个薄弱
              </span>
            )}
          </summary>
          {a.kcs && a.kcs.length > 0 ? (
            <ul className="mt-1.5 space-y-1 border-t border-line/60 pt-1.5">
              {a.kcs.map((kc, i) => (
                <li
                  key={kc.kc_id ?? `${kc.name}-${i}`}
                  className="flex items-center justify-between gap-2 text-body-sm"
                >
                  <span className="min-w-0 truncate text-ink-secondary" title={kc.name}>
                    {kc.name ?? "未命名知识点"}
                  </span>
                  <span className="shrink-0 font-mono text-ink-muted">
                    {kc.p_mastery != null &&
                    (kc.evidence_state == null || kc.evidence_state === "observed")
                      ? fmtPct(kc.p_mastery)
                      : "待验证"}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-body-sm text-ink-muted">该轴暂无下钻明细</p>
          )}
        </details>
      ))}
    </div>
  );
}

/** 雷达区：observed 轴数<5 降级条形图；≥5 桌面画雷达、移动端用条形图（防轴标签重叠）。 */
export function RadarPanel({ axes, isChapter, weakThreshold }: { axes: ReportRadarAxis[]; isChapter: boolean; weakThreshold: number }) {
  if (axes.length === 0) {
    return (
      <p className="py-6 text-center text-caption text-ink-muted">
        暂无掌握度证据，完成练习后这里会出现掌握度轴
      </p>
    );
  }
  const observedAxes = axes.filter((axis) => axis.p_mastery != null);
  // Fall back to bars whenever fewer than five observed axes remain, or when
  // unknown/estimated axes leave too little evidence
  // for a meaningful polygon, instead of showing an empty radar beside data.
  const useBars = observedAxes.length < 5;
  return (
    <>
      {useBars ? (
        <AxisBars axes={axes} weakThreshold={weakThreshold} />
      ) : (
        <>
          <div className="hidden sm:block">
            <MasteryRadar items={axes} />
          </div>
          <div className="sm:hidden">
            <AxisBars axes={axes} weakThreshold={weakThreshold} />
          </div>
        </>
      )}
      {isChapter && <AxisDrilldown axes={axes} />}
    </>
  );
}

/** 薄弱点卡片：两口径徽章 + 掌握度 + 错题数 + 归因链（可能源于《前置 KC》未掌握）。 */
