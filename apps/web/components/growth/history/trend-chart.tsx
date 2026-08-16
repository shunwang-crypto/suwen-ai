

"use client";

// 学情报告（F7）：掌握度雷达（章节聚合轴 + 知识库切换 tab；轴数<5 或移动端降级条形图，
// 防轴标签重叠）+ 30 天趋势（答题数/正确率双系列，纯 SVG、两联面板共享横轴——不做双 Y 轴）
// + 薄弱点卡片（按科目分节，「红线/待巩固」两口径分开计数，节内 p 升序 top5 + 展开全部）
// + 可执行建议（带跳转）+ narrative 三段式诊断（统一由 ModelMarkdown 渲染）。


import {
  PageHeader,
  SkeletonPage,
  Spinner,
  fmtPct,
} from "@/components/common";
import type {
  LearningReportResponse,
  ReportRadarItem,
  ReportTrendPoint,
  ReportWeakPoint,
} from "@/lib/types";

// ---- 后端可选字段与空值降级 ----

/** 章节聚合轴的下钻明细项（?kb_id= 时每轴附带）。 */
interface RadarAxisKcDetail {
  kc_id?: string;
  name?: string;
  p_mastery?: number | null;
}

/** 雷达轴：章节聚合模式附带 kc_count/weak_count/kcs；KC 模式与旧契约一致。 */
type RadarAxis = ReportRadarItem & {
  kc_count?: number | null;
  weak_count?: number | null;
  kcs?: RadarAxisKcDetail[] | null;
};

/** 薄弱点：分科目契约附带 subject；缺失时降级为单节展示。 */
type SubjectWeakPoint = ReportWeakPoint & { subject?: string | null };

/** 报告响应扩展：axes_kind（"chapter"=章节聚合轴）与「待巩固」阈值。 */
type ScaledReportResponse = LearningReportResponse & {
  axes_kind?: string | null;
  consolidate_threshold?: number | null;
  /** 待巩固档（红线以上、未达定向出题线），与 weak_points 同结构 */
  consolidate_points?: SubjectWeakPoint[] | null;
  report_status?: "pending" | "generating" | "completed" | "unavailable" | "failed";
  report_updated_at?: string | null;
  cached?: boolean;
};

/** 每节薄弱点默认展示条数（p 升序 top5，其余「展开全部」）。 */

/** 30 天趋势：上联答题数（柱），下联正确率（折线，固定 0–100% 刻度），共享横轴。 */
export function TrendChart({ trend }: { trend: ReportTrendPoint[] }) {
  const hasData = trend.some((t) => t.attempts > 0);
  if (!hasData) {
    return (
      <p className="py-6 text-center text-caption text-slate-400">
        近 30 天暂无作答记录，完成练习后这里会出现趋势
      </p>
    );
  }

  const w = 640;
  const panelH = 96;
  const pad = { l: 34, r: 14, t: 14, b: 8 };
  const axisH = 18; // 底部日期标签带
  const innerW = w - pad.l - pad.r;
  const innerH = panelH - pad.t - pad.b;
  const n = trend.length;
  const slot = innerW / n;
  const xc = (i: number) => pad.l + slot * (i + 0.5);

  const maxAttempts = Math.max(...trend.map((t) => t.attempts), 1);
  const yAttempts = (v: number) => pad.t + innerH - (v / maxAttempts) * innerH;
  const yAcc = (v: number) => pad.t + innerH - v * innerH;

  // 正确率折线按连续非 null 段绘制（无作答日断线，不伪造 0%）
  const segments: number[][] = [];
  let cur: number[] = [];
  trend.forEach((t, i) => {
    if (t.accuracy != null) {
      cur.push(i);
    } else if (cur.length > 0) {
      segments.push(cur);
      cur = [];
    }
  });
  if (cur.length > 0) segments.push(cur);

  const barW = Math.max(slot * 0.55, 3);

  return (
    <div className="overflow-x-auto">
      <div className="min-w-120">
        {/* 上联：答题数（单系列，标题即图例） */}
        <p className="mb-1 flex items-center gap-1.5 text-caption text-slate-500">
          <span className="h-2 w-2 rounded-full bg-indigo-500" aria-hidden />
          每日答题数
        </p>
        <svg
          viewBox={`0 0 ${w} ${panelH}`}
          className="block w-full"
          role="img"
          aria-label="近 30 天每日答题数"
        >
          <line
            x1={pad.l}
            y1={pad.t + innerH}
            x2={w - pad.r}
            y2={pad.t + innerH}
            stroke="#e2e8f0"
            strokeWidth={1}
          />
          <text x={pad.l - 6} y={pad.t + 4} fontSize={10} fill="#94a3b8" textAnchor="end">
            {maxAttempts}
          </text>
          <line
            x1={pad.l}
            y1={pad.t}
            x2={w - pad.r}
            y2={pad.t}
            stroke="#f1f5f9"
            strokeWidth={1}
          />
          {trend.map((t, i) =>
            t.attempts > 0 ? (
              <rect
                key={t.date}
                x={xc(i) - barW / 2}
                y={yAttempts(t.attempts)}
                width={barW}
                height={pad.t + innerH - yAttempts(t.attempts)}
                rx={1.5}
                fill="#6366f1"
              >
                <title>
                  {t.date}：{t.attempts} 题（对 {t.correct}）
                </title>
              </rect>
            ) : null,
          )}
        </svg>

        {/* 下联：正确率（固定 0–100%） */}
        <p className="mb-1 mt-2 flex items-center gap-1.5 text-caption text-slate-500">
          <span className="h-2 w-2 rounded-full bg-emerald-500" aria-hidden />
          每日正确率（无作答日断线）
        </p>
        <svg
          viewBox={`0 0 ${w} ${panelH + axisH}`}
          className="block w-full"
          role="img"
          aria-label="近 30 天每日正确率"
        >
          {[0, 0.5, 1].map((v) => (
            <g key={v}>
              <line
                x1={pad.l}
                y1={yAcc(v)}
                x2={w - pad.r}
                y2={yAcc(v)}
                stroke={v === 0 ? "#e2e8f0" : "#f1f5f9"}
                strokeWidth={1}
              />
              <text
                x={pad.l - 6}
                y={yAcc(v) + 3.5}
                fontSize={10}
                fill="#94a3b8"
                textAnchor="end"
              >
                {Math.round(v * 100)}%
              </text>
            </g>
          ))}
          {segments.map((seg) =>
            seg.length > 1 ? (
              <polyline
                key={`seg-${seg[0]}`}
                points={seg
                  .map((i) => `${xc(i).toFixed(1)},${yAcc(trend[i].accuracy ?? 0).toFixed(1)}`)
                  .join(" ")}
                fill="none"
                stroke="#10b981"
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            ) : null,
          )}
          {trend.map((t, i) =>
            t.accuracy != null ? (
              <circle
                key={t.date}
                cx={xc(i)}
                cy={yAcc(t.accuracy)}
                r={2.8}
                fill="#10b981"
                stroke="#fff"
                strokeWidth={1.2}
              >
                <title>
                  {t.date}：正确率 {fmtPct(t.accuracy)}（{t.correct}/{t.attempts}）
                </title>
              </circle>
            ) : null,
          )}
          {/* 首尾日期标签（共享横轴，只标一次） */}
          <text x={pad.l} y={panelH + axisH - 6} fontSize={10} fill="#94a3b8">
            {trend[0].date}
          </text>
          <text
            x={w - pad.r}
            y={panelH + axisH - 6}
            fontSize={10}
            fill="#94a3b8"
            textAnchor="end"
          >
            {trend[n - 1].date}
          </text>
        </svg>
      </div>
    </div>
  );
}

/** 大数字统计卡：渐变数字 + 趋势箭头（近 7 天 vs 前 7 天，均由已拉取的 trend 就地计算）。
 *  薄弱口径分开命名：红线（<红线阈值）为主数字，待巩固另行标注，消除跨页数字打架。 */
