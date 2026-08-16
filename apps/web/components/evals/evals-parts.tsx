"use client";

// 本地检索评估看板（仅管理员）：把临时评测产物入库后集中展示。
// 上：导入 / 重跑（job 轮询）操作条；中：历史跑分表（可勾选 A/B 做 delta 对比）；
// 下：单次跑分详情展示 headline 指标卡、按 category 分桶柱和拒答正确率。
// 图表一律纯 SVG / 纯 CSS div，颜色只作辅助——每根柱都带文字数值与 title tooltip。
// 每张卡片底部固定标注 embedding/rerank、语料与黄金集版本。

import { type ReactNode } from "react";

import {Card, ErrorBanner, LoadingState, Spinner, fmtDateTime} from "@/components/common";
import { MathText } from "@/lib/katex";
import type {EvalCompareResponse, EvalJobInfo, EvalRunDetail, RetrievalCategoryRow, RetrievalEvalMetrics, RetrievalRefusal} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

export const DEFAULT_GOLDEN = "eval/golden_set/v2.jsonl";
export const POLL_INTERVAL_MS = 3000;
export const POLL_MAX_TICKS = 200;

// 色盲友好三色（已按 OKLab/CVD ΔE 校验），且每根柱都有文字标签，颜色只作辅助编码。
export type CatSeriesKey = "hit@1" | "hit@3" | "hit@5" | "mrr";

export const CAT_SERIES: { key: CatSeriesKey; label: string; color: string }[] = [
  { key: "hit@1", label: "hit@1", color: "#4f46e5" },
  { key: "hit@3", label: "hit@3", color: "#0d9488" },
  { key: "hit@5", label: "hit@5", color: "#d97706" },
  { key: "mrr", label: "MRR", color: "#be123c" },
];

export const HEADLINE_ORDER: { key: keyof NonNullable<RetrievalEvalMetrics["headline"]>; label: string }[] =
  [
    { key: "hit@1", label: "hit@1" },
    { key: "hit@3", label: "hit@3" },
    { key: "hit@5", label: "hit@5" },
    { key: "mrr", label: "MRR" },
    { key: "ndcg@5", label: "nDCG@5" },
    { key: "ndcg@10", label: "nDCG@10" },
  ];

// ---- 小工具 ----

export function fmtNum(v: number | null | undefined, digits = 4): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}

export function KindBadge() {
  return (
    <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-xs font-medium whitespace-nowrap text-indigo-700">
      检索评估
    </span>
  );
}

/** 数据口径脚注：评分加分项，每张卡片底部固定出现。 */
export function BasisNote({ children }: { children: ReactNode }) {
  return (
    <p className="mt-3 border-t border-slate-100 pt-2 text-body-sm leading-relaxed text-slate-400">
      数据口径：{children}
    </p>
  );
}

export function MetricTile({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5">
      <p className="text-xs text-slate-400">{label}</p>
      <p className="mt-0.5 font-mono text-xl font-semibold tracking-tight text-slate-900">
        {value}
      </p>
      {sub && <p className="mt-0.5 text-body-sm text-slate-400">{sub}</p>}
    </div>
  );
}

export function SuccessBanner({
  message,
  onClose,
}: {
  message: string;
  onClose: () => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-700">
      <span className="min-w-0 break-words">{message}</span>
      <button
        type="button"
        onClick={onClose}
        className="shrink-0 text-emerald-400 hover:text-emerald-600"
        aria-label="关闭提示"
      >
        ✕
      </button>
    </div>
  );
}

/** 重跑任务状态 pill（queued / in_progress / complete / failed）。 */
export function JobPill({ job }: { job: EvalJobInfo }) {
  const failed =
    job.status === "not_found" ||
    (job.status === "complete" && job.success === false);
  const conf = failed
    ? { text: "评估失败", cls: "bg-red-100 text-red-700 border-red-200" }
    : job.status === "complete"
      ? { text: "评估完成", cls: "bg-emerald-100 text-emerald-700 border-emerald-200" }
      : job.status === "in_progress"
        ? { text: "评估进行中", cls: "bg-indigo-100 text-indigo-700 border-indigo-200" }
        : { text: "排队中", cls: "bg-slate-100 text-slate-600 border-slate-200" };
  const running = !failed && job.status !== "complete";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-semibold ${conf.cls}`}
    >
      {running && <Spinner className="h-3 w-3" />}
      {conf.text}
      <span className="font-mono font-normal opacity-70">#{job.job_id.slice(0, 8)}</span>
    </span>
  );
}

// ---- 检索详情 ----

/** 按 category 分桶柱（纯 CSS div）：每根柱带系列名 + 数值 label + title tooltip。 */
export function CategoryBars({ rows }: { rows: RetrievalCategoryRow[] }) {
  const present = CAT_SERIES.filter((s) =>
    rows.some((r) => typeof r[s.key] === "number"),
  );
  // hit@3 / hit@5 存在时最多三根柱（MRR 让位），老报告只有 hit@1 时用 MRR 补位
  const series = present.length > 3 ? present.filter((s) => s.key !== "mrr") : present;

  if (rows.length === 0 || series.length === 0) {
    return (
      <p className="py-4 text-center text-xs text-slate-400">
        该报告未提供按 category 的分桶指标
      </p>
    );
  }

  const scoreOf = (r: RetrievalCategoryRow) =>
    r["hit@1"] ?? r.mrr ?? r["hit@3"] ?? r["hit@5"] ?? 1;
  const weakest = rows.reduce((a, b) => (scoreOf(b) < scoreOf(a) ? b : a), rows[0]);

  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
        {series.map((s) => (
          <span key={s.key} className="flex items-center gap-1.5">
            <span
              className="h-2.5 w-2.5 rounded-sm"
              style={{ background: s.color }}
              aria-hidden
            />
            {s.label}
          </span>
        ))}
        <span className="ml-auto text-slate-400">柱长 = 指标值（0 → 1，越长越好）</span>
      </div>

      {rows.map((r) => {
        const weak = r.category === weakest.category;
        return (
          <div
            key={r.category}
            className={`rounded-xl border px-3 py-2.5 ${
              weak ? "border-amber-300 bg-amber-50/50" : "border-slate-200 bg-white"
            }`}
          >
            <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-xs font-semibold text-slate-700">{r.category}</span>
              <span className="flex items-center gap-2 text-body-sm text-slate-400">
                {weak && (
                  <span className="rounded-full bg-amber-100 px-2 py-0.5 font-semibold text-amber-700">
                    最弱分桶 · 优先补语料
                  </span>
                )}
                n = {r.n} 题
              </span>
            </div>
            <div className="space-y-1">
              {series.map((s) => {
                const v = r[s.key];
                return (
                  <div key={s.key} className="flex items-center gap-2">
                    <span className="w-12 shrink-0 font-mono text-body-sm text-slate-500">
                      {s.label}
                    </span>
                    <div className="h-3 min-w-0 flex-1 rounded-full bg-slate-100">
                      {typeof v === "number" && (
                        <div
                          className="h-full rounded-full"
                          style={{
                            width: `${Math.min(Math.max(v * 100, 1.5), 100)}%`,
                            background: s.color,
                          }}
                          title={`${r.category} · ${s.label} = ${fmtNum(v)}（n=${r.n}）`}
                        />
                      )}
                    </div>
                    <span className="w-14 shrink-0 text-right font-mono text-body-sm text-slate-700">
                      {fmtNum(v)}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function RefusalBlock({ refusal }: { refusal: RetrievalRefusal }) {
  const rows = refusal.rows ?? [];
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-3">
        <span className="font-mono text-2xl font-semibold text-slate-900">
          {fmtNum(refusal.rate, 4)}
        </span>
        <span className="text-xs text-slate-500">
          拒答正确 {refusal.correct} / {refusal.total} 题（该拒答的题确实低于置信阈值）
        </span>
      </div>
      {rows.length === 0 ? (
        <p className="text-xs text-slate-400">该报告未提供逐题拒答明细。</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-160 text-left text-sm">
            <thead>
              <tr className="border-b border-slate-100 text-xs text-slate-400">
                <th className="py-1.5 font-medium">qid</th>
                <th className="py-1.5 font-medium">问题</th>
                <th className="py-1.5 text-right font-medium">召回块</th>
                <th className="py-1.5 text-right font-medium">top1 分数</th>
                <th className="py-1.5 font-medium">top1 标题路径</th>
                <th className="py-1.5 font-medium">判定</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.qid}
                  className={`border-b border-slate-50 ${r.ok ? "" : "bg-red-50/60"}`}
                >
                  <td className="py-1.5 pr-2 font-mono text-xs text-slate-500">{r.qid}</td>
                  <td className="max-w-80 py-1.5 pr-2 text-xs text-slate-700">
                    <MathText text={r.question} />
                  </td>
                  <td className="py-1.5 pr-2 text-right font-mono text-xs text-slate-600">
                    {r.n_chunks}
                  </td>
                  <td
                    className={`py-1.5 pr-2 text-right font-mono text-xs ${
                      r.ok ? "text-slate-600" : "font-semibold text-red-700"
                    }`}
                  >
                    {fmtNum(r.top1_score)}
                  </td>
                  <td
                    className="max-w-60 truncate py-1.5 pr-2 text-xs text-slate-500"
                    title={r.top1_heading ?? ""}
                  >
                    {r.top1_heading || "—"}
                  </td>
                  <td className="py-1.5 whitespace-nowrap">
                    {r.ok ? (
                      <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">
                        正确拒答
                      </span>
                    ) : (
                      <span className="rounded-full bg-red-100 px-2 py-0.5 text-xs font-semibold text-red-700">
                        误报高置信
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function RetrievalDetail({
  detail,
  metrics,
}: {
  detail: EvalRunDetail;
  metrics: RetrievalEvalMetrics;
}) {
  const cfg = metrics.report_config ?? detail.config?.report_config ?? {};
  const headline = metrics.headline ?? {};
  const tiles = HEADLINE_ORDER.filter(
    (h) => typeof headline[h.key] === "number",
  );
  const goldenVersion =
    metrics.golden_set_version ?? detail.golden_set_version ?? "—";
  const corpus = detail.corpus_version ?? "—";

  const basis = (
    <>
      embedding <span className="font-mono">{cfg.embedding_mode ?? "未知"}</span>
      {cfg.embedding_model ? `（${cfg.embedding_model}）` : ""} · rerank{" "}
      <span className="font-mono">{cfg.rerank_mode ?? "未知"}</span>
      {cfg.rerank_model ? `（${cfg.rerank_model}）` : ""} · 语料 {corpus} · 黄金集{" "}
      {goldenVersion}
      {metrics.generated_at ? ` · 报告生成于 ${fmtDateTime(metrics.generated_at)}` : ""}
    </>
  );

  return (
    <div className="space-y-4">
      <Card
        title="整体检索指标"
        extra={
          <span className="text-xs text-slate-400">
            {metrics.n_questions != null ? `${metrics.n_questions} 问` : "—"}
            {metrics.n_answerable != null ? ` · 可答 ${metrics.n_answerable}` : ""}
            {metrics.n_refusal != null ? ` · 应拒答 ${metrics.n_refusal}` : ""}
          </span>
        }
      >
        {tiles.length === 0 ? (
          <p className="py-3 text-center text-xs text-slate-400">
            该报告未提供 headline 指标
          </p>
        ) : (
          <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-6">
            {tiles.map((h) => (
              <MetricTile key={h.key} label={h.label} value={fmtNum(headline[h.key])} />
            ))}
          </div>
        )}
        {metrics.confident && (
          <p className="mt-3 text-xs text-slate-500">
            高置信命中{" "}
            <span className="font-mono font-semibold text-slate-800">
              {metrics.confident.n} / {metrics.confident.total}
            </span>
            （top1 分数越过 confidence_threshold{" "}
            <span className="font-mono">{cfg.confidence_threshold ?? "—"}</span>{" "}
            的问题数；其余走拒答兜底）
          </p>
        )}
        {Object.keys(cfg).length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {Object.entries(cfg).map(([k, v]) => (
              <span
                key={k}
                className="rounded-md bg-slate-100 px-2 py-0.5 font-mono text-body-sm text-slate-600"
              >
                {k}={String(v)}
              </span>
            ))}
          </div>
        )}
        <BasisNote>{basis}</BasisNote>
      </Card>

      <Card title="按题型分桶（category）">
        <CategoryBars rows={metrics.by_category ?? []} />
        <BasisNote>{basis}</BasisNote>
      </Card>

      <Card title="拒答正确率（不该答的题有没有硬答）">
        {metrics.refusal ? (
          <RefusalBlock refusal={metrics.refusal} />
        ) : (
          <p className="py-4 text-center text-xs text-slate-400">
            该报告未包含拒答子集
          </p>
        )}
        <BasisNote>{basis}</BasisNote>
      </Card>
    </div>
  );
}

// ---- 详情容器 ----

export function RunDetailPanel({ id }: { id: string }) {
  // 调用侧用 key={id} 强制重挂载; 详情加载（A5 收敛: useApiResource 派生）
  const { data: detail, loading, error } = useApiResource<EvalRunDetail>(
    `/api/evals/${id}`,
    { errorMessage: "跑分详情加载失败" },
  );

  if (loading) return <LoadingState text="跑分详情加载中…" />;
  if (error) return <ErrorBanner message={error} />;
  if (!detail) return null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <KindBadge />
        <h3 className="text-sm font-semibold text-slate-900">{detail.label}</h3>
        <span className="font-mono text-xs text-slate-400">{detail.source}</span>
        <span className="ml-auto text-xs text-slate-400">{fmtDateTime(detail.at)}</span>
      </div>
      <RetrievalDetail detail={detail} metrics={detail.metrics} />
    </div>
  );
}

// ---- A/B 对比 ----

export function CompareSection({ leftId, rightId }: { leftId: string; rightId: string }) {
  // 同上：调用侧按 A/B 组合 key 重挂载; 对比加载（A5 收敛: useApiResource 派生）
  const { data, loading, error } = useApiResource<EvalCompareResponse>(
    `/api/evals/compare?ids=${encodeURIComponent(leftId)},${encodeURIComponent(rightId)}`,
    { errorMessage: "对比加载失败" },
  );

  return (
    <Card title="A / B 对比">
      {loading ? (
        <LoadingState text="对比计算中…" />
      ) : error ? (
        <ErrorBanner message={error} />
      ) : !data ? null : (
        <div className="space-y-3">
          <div className="grid gap-2 sm:grid-cols-2">
            {(
              [
                ["A（基准）", data.left],
                ["B（对照）", data.right],
              ] as const
            ).map(([tag, side]) => (
              <div key={tag} className="rounded-xl border border-slate-200 px-3 py-2">
                <p className="text-xs text-slate-400">{tag}</p>
                <p className="mt-0.5 flex flex-wrap items-center gap-2 text-sm font-medium text-slate-800">
                  <KindBadge />
                  {side.label}
                </p>
                <p className="mt-0.5 font-mono text-body-sm text-slate-400">
                  {fmtDateTime(side.at)} · {side.source}
                </p>
              </div>
            ))}
          </div>

          {data.deltas.length === 0 ? (
            <p className="py-3 text-center text-xs text-slate-400">没有可比较的同名指标</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-120 text-left text-sm">
                <thead>
                  <tr className="border-b border-slate-100 text-xs text-slate-400">
                    <th className="py-1.5 font-medium">指标</th>
                    <th className="py-1.5 text-right font-medium">A</th>
                    <th className="py-1.5 text-right font-medium">B</th>
                    <th className="py-1.5 text-right font-medium">Δ (B−A)</th>
                    <th className="py-1.5 text-right font-medium">相对变化</th>
                  </tr>
                </thead>
                <tbody>
                  {data.deltas.map((d) => {
                    const up = d.delta > 0;
                    const flat = d.delta === 0;
                    const cls = flat
                      ? "text-slate-400"
                      : up
                        ? "text-emerald-700"
                        : "text-red-700";
                    return (
                      <tr key={d.metric} className="border-b border-slate-50">
                        <td className="py-1.5 pr-2 font-mono text-xs text-slate-700">
                          {d.metric}
                        </td>
                        <td className="py-1.5 pr-2 text-right font-mono text-xs text-slate-600">
                          {fmtNum(d.left)}
                        </td>
                        <td className="py-1.5 pr-2 text-right font-mono text-xs text-slate-600">
                          {fmtNum(d.right)}
                        </td>
                        <td
                          className={`py-1.5 pr-2 text-right font-mono text-xs font-semibold ${cls}`}
                        >
                          {flat ? "—" : up ? "↑" : "↓"} {flat ? "" : d.delta > 0 ? "+" : ""}
                          {fmtNum(d.delta)}
                        </td>
                        <td className={`py-1.5 text-right font-mono text-xs ${cls}`}>
                          {d.pct == null
                            ? "—"
                            : `${d.pct > 0 ? "+" : ""}${d.pct.toFixed(1)}%`}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {(data.left_only.length > 0 || data.right_only.length > 0) && (
            <div className="grid gap-2 text-xs sm:grid-cols-2">
              <div className="rounded-xl border border-slate-200 px-3 py-2">
                <p className="mb-1 text-slate-400">仅 A 有（{data.left_only.length}）</p>
                <p className="font-mono break-words text-slate-600">
                  {data.left_only.length > 0 ? data.left_only.join("、") : "—"}
                </p>
              </div>
              <div className="rounded-xl border border-slate-200 px-3 py-2">
                <p className="mb-1 text-slate-400">仅 B 有（{data.right_only.length}）</p>
                <p className="font-mono break-words text-slate-600">
                  {data.right_only.length > 0 ? data.right_only.join("、") : "—"}
                </p>
              </div>
            </div>
          )}
          <p className="text-body-sm text-slate-400">
            Δ 与相对变化按 B−A 计算；绿色 ↑ 表示 B 更高，红色 ↓ 表示 B 更低。
            hit、MRR 和 nDCG 均为越高越好。
          </p>
        </div>
      )}
    </Card>
  );
}

export function NoAccess() {
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
          评估看板仅对管理员开放，请使用管理员账号登录。
        </p>
      </div>
    </div>
  );
}
