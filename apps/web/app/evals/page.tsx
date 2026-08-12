"use client";

// 本地检索评估看板（仅管理员）：把临时评测产物入库后集中展示。
// 上：导入 / 重跑（job 轮询）操作条；中：历史跑分表（可勾选 A/B 做 delta 对比）；
// 下：单次跑分详情展示 headline 指标卡、按 category 分桶柱和拒答正确率。
// 图表一律纯 SVG / 纯 CSS div，颜色只作辅助——每根柱都带文字数值与 title tooltip。
// 每张卡片底部固定标注 embedding/rerank、语料与黄金集版本。

import { useEffect, useState, type ReactNode } from "react";

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
import type {
  EvalCompareResponse,
  EvalImportResponse,
  EvalJobInfo,
  EvalRunDetail,
  EvalRunJobStart,
  EvalRunListItem,
  EvalRunsResponse,
  RetrievalCategoryRow,
  RetrievalEvalMetrics,
  RetrievalRefusal,
} from "@/lib/types";

const DEFAULT_GOLDEN = "eval/golden_set/v2.jsonl";
const POLL_INTERVAL_MS = 3000;
const POLL_MAX_TICKS = 200;

// 色盲友好三色（已按 OKLab/CVD ΔE 校验），且每根柱都有文字标签，颜色只作辅助编码。
type CatSeriesKey = "hit@1" | "hit@3" | "hit@5" | "mrr";

const CAT_SERIES: { key: CatSeriesKey; label: string; color: string }[] = [
  { key: "hit@1", label: "hit@1", color: "#4f46e5" },
  { key: "hit@3", label: "hit@3", color: "#0d9488" },
  { key: "hit@5", label: "hit@5", color: "#d97706" },
  { key: "mrr", label: "MRR", color: "#be123c" },
];

const HEADLINE_ORDER: { key: keyof NonNullable<RetrievalEvalMetrics["headline"]>; label: string }[] =
  [
    { key: "hit@1", label: "hit@1" },
    { key: "hit@3", label: "hit@3" },
    { key: "hit@5", label: "hit@5" },
    { key: "mrr", label: "MRR" },
    { key: "ndcg@5", label: "nDCG@5" },
    { key: "ndcg@10", label: "nDCG@10" },
  ];

// ---- 小工具 ----

function fmtNum(v: number | null | undefined, digits = 4): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}

function KindBadge() {
  return (
    <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-xs font-medium whitespace-nowrap text-indigo-700">
      检索评估
    </span>
  );
}

/** 数据口径脚注：评分加分项，每张卡片底部固定出现。 */
function BasisNote({ children }: { children: ReactNode }) {
  return (
    <p className="mt-3 border-t border-slate-100 pt-2 text-body-sm leading-relaxed text-slate-400">
      数据口径：{children}
    </p>
  );
}

function MetricTile({
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

function SuccessBanner({
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
function JobPill({ job }: { job: EvalJobInfo }) {
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
function CategoryBars({ rows }: { rows: RetrievalCategoryRow[] }) {
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

function RefusalBlock({ refusal }: { refusal: RetrievalRefusal }) {
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
                    {r.question}
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

function RetrievalDetail({
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

function RunDetailPanel({ id }: { id: string }) {
  const [detail, setDetail] = useState<EvalRunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 调用侧用 key={id} 强制重挂载，故这里不需要在 effect 里重置 loading
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await apiJson<EvalRunDetail>(`/api/evals/${id}`);
        if (!alive) return;
        setDetail(res);
        setError(null);
      } catch (err) {
        if (!alive) return;
        setDetail(null);
        setError(err instanceof ApiError ? err.message : "跑分详情加载失败");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [id]);

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

function CompareSection({ leftId, rightId }: { leftId: string; rightId: string }) {
  const [data, setData] = useState<EvalCompareResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 同上：调用侧按 A/B 组合 key 重挂载
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await apiJson<EvalCompareResponse>(
          `/api/evals/compare?ids=${encodeURIComponent(leftId)},${encodeURIComponent(rightId)}`,
        );
        if (!alive) return;
        setData(res);
        setError(null);
      } catch (err) {
        if (!alive) return;
        setData(null);
        setError(err instanceof ApiError ? err.message : "对比加载失败");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [leftId, rightId]);

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
          评估看板仅对管理员开放，请使用管理员账号登录。
        </p>
      </div>
    </div>
  );
}

export default function EvalsPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  const [runs, setRuns] = useState<EvalRunListItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const [importing, setImporting] = useState(false);

  const [formOpen, setFormOpen] = useState(false);
  const [kbId, setKbId] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [golden, setGolden] = useState(DEFAULT_GOLDEN);
  const [label, setLabel] = useState("手动重跑");
  const [noSparse, setNoSparse] = useState(false);
  const [noRerank, setNoRerank] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<EvalJobInfo | null>(null);

  const [pickA, setPickA] = useState<string | null>(null);
  const [pickB, setPickB] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);

  // 列表加载
  useEffect(() => {
    if (!isAdmin) return;
    let alive = true;
    (async () => {
      try {
        const res = await apiJson<EvalRunsResponse>("/api/evals");
        if (!alive) return;
        setRuns(res.runs ?? []);
        setError(null);
      } catch (err) {
        if (!alive) return;
        setRuns([]);
        setError(err instanceof ApiError ? err.message : "评估列表加载失败");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [isAdmin, reloadKey]);

  // 重跑任务轮询：每 3s 一次，最多 200 次，complete/not_found 即停并刷新列表
  useEffect(() => {
    if (!jobId) return;
    let alive = true;
    let ticks = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      if (!alive) return;
      ticks += 1;
      try {
        const info = await apiJson<EvalJobInfo>(`/api/evals/jobs/${jobId}`);
        if (!alive) return;
        setJob(info);
        if (info.status === "complete" || info.status === "not_found") {
          setJobId(null);
          if (info.status === "not_found") {
            setBanner("评估任务已丢失（服务可能重启），请重新发起。");
          } else if (info.success === false) {
            setBanner(info.error || "评估任务执行失败。");
          } else {
            setOk("评估任务完成，已刷新历史跑分列表。");
          }
          setReloadKey((k) => k + 1);
          return;
        }
      } catch (err) {
        if (!alive) return;
        setJobId(null);
        setBanner(err instanceof ApiError ? err.message : "评估任务状态查询失败");
        return;
      }
      if (ticks >= POLL_MAX_TICKS) {
        setJobId(null);
        setBanner("评估任务轮询已达上限（200 次），请稍后手动刷新列表查看结果。");
        return;
      }
      timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
    };

    timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [jobId]);

  const doImport = async () => {
    if (importing) return;
    setImporting(true);
    setBanner(null);
    setOk(null);
    try {
      const res = await apiJson<EvalImportResponse>("/api/evals/import", {
        method: "POST",
      });
      const failed = (res.items ?? []).filter((i) => i.status === "error");
      setOk(
        `导入完成：新增 ${res.imported} 条，跳过 ${res.skipped} 条（内容哈希未变）` +
          (failed.length > 0 ? `，${failed.length} 条解析失败` : ""),
      );
      if (failed.length > 0) {
        setBanner(
          failed.map((f) => `${f.source}：${f.error ?? "解析失败"}`).join("；"),
        );
      }
      setReloadKey((k) => k + 1);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "导入评估产物失败");
    } finally {
      setImporting(false);
    }
  };

  const submitRun = async () => {
    if (submitting || !kbId.trim()) return;
    setSubmitting(true);
    setBanner(null);
    setOk(null);
    try {
      const started = await apiJson<EvalRunJobStart>("/api/evals/run", {
        body: {
          kb_id: kbId.trim(),
          ...(tenantId.trim() ? { tenant_id: tenantId.trim() } : {}),
          golden: golden.trim() || DEFAULT_GOLDEN,
          no_sparse: noSparse,
          no_rerank: noRerank,
          label: label.trim() || "手动重跑",
        },
      });
      setJob({
        job_id: started.job_id,
        status: (started.status as EvalJobInfo["status"]) ?? "queued",
        success: null,
        eval_run_id: null,
        error: null,
      });
      setJobId(started.job_id);
      setFormOpen(false);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "发起评估失败");
    } finally {
      setSubmitting(false);
    }
  };

  const togglePick = (slot: "A" | "B", id: string) => {
    if (slot === "A") {
      setPickA((prev) => (prev === id ? null : id));
      setPickB((prev) => (prev === id ? null : prev));
    } else {
      setPickB((prev) => (prev === id ? null : id));
      setPickA((prev) => (prev === id ? null : prev));
    }
  };

  if (!isAdmin) return <NoAccess />;

  return (
    <div className="mx-auto w-full max-w-6xl space-y-4 p-4">
      {/* 操作条 */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-slate-900">评估看板</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            展示的都是明确环境生成的评测产物，不做任何数字美化；没有新产物时不显示旧数字。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {job && <JobPill job={job} />}
          <button
            type="button"
            disabled={importing}
            onClick={() => void doImport()}
            className="flex items-center gap-2 rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {importing && <Spinner />}
            导入评估产物
          </button>
          <button
            type="button"
            onClick={() => setFormOpen((v) => !v)}
            className="rounded-xl bg-indigo-600 px-3.5 py-2 text-sm font-semibold text-white hover:bg-indigo-700"
          >
            {formOpen ? "收起" : "重新评估"}
          </button>
        </div>
      </div>

      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}
      {ok && <SuccessBanner message={ok} onClose={() => setOk(null)} />}
      {error && (
        <ErrorBanner
          message={error}
          onRetry={() => {
            setLoading(true);
            setError(null);
            setReloadKey((k) => k + 1);
          }}
        />
      )}

      {/* 内联重跑表单 */}
      {formOpen && (
        <Card title="重新跑一次检索评估">
          <div className="space-y-3">
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-xs text-slate-500">
                  kb_id（必填，被评估的知识库 UUID）
                </span>
                <input
                  value={kbId}
                  onChange={(e) => setKbId(e.target.value)}
                  placeholder="粘贴知识库 UUID"
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 font-mono text-sm text-slate-900 outline-none placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-slate-500">
                  黄金集路径（相对仓库根）
                </span>
                <input
                  value={golden}
                  onChange={(e) => setGolden(e.target.value)}
                  placeholder={DEFAULT_GOLDEN}
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 font-mono text-sm text-slate-900 outline-none placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-slate-500">
                  tenant_id（可选，跨租户评估时填）
                </span>
                <input
                  value={tenantId}
                  onChange={(e) => setTenantId(e.target.value)}
                  placeholder="留空则用当前租户"
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 font-mono text-sm text-slate-900 outline-none placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-slate-500">本次跑分标签</span>
                <input
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder="手动重跑"
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-2 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={noSparse}
                  onChange={(e) => setNoSparse(e.target.checked)}
                  className="h-4 w-4 accent-indigo-600"
                />
                关闭稀疏检索（消融 BM25）
              </label>
              <label className="flex items-center gap-2 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={noRerank}
                  onChange={(e) => setNoRerank(e.target.checked)}
                  className="h-4 w-4 accent-indigo-600"
                />
                关闭重排（消融 rerank）
              </label>
              <div className="ml-auto flex items-center gap-2">
                {submitting && <Spinner />}
                <button
                  type="button"
                  disabled={submitting || !kbId.trim() || jobId !== null}
                  onClick={() => void submitRun()}
                  className="rounded-xl bg-indigo-600 px-3.5 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {jobId ? "已有任务在跑" : "发起评估"}
                </button>
              </div>
            </div>
            <p className="text-body-sm text-slate-400">
              发起后每 {POLL_INTERVAL_MS / 1000}s 轮询一次任务状态（最多{" "}
              {POLL_MAX_TICKS} 次），完成后自动刷新列表。
              消融开关会写进报告的 report_config，用于做 A/B 对比。
            </p>
          </div>
        </Card>
      )}

      {/* 历史跑分 */}
      <Card
        title="历史跑分"
        extra={
          <span className="text-xs text-slate-400">
            {runs ? `${runs.length} 次` : "—"} · 点击行查看详情
          </span>
        }
      >
        {loading ? (
          <LoadingState text="评估列表加载中…" />
        ) : !runs || runs.length === 0 ? (
          <EmptyState
            title="还没有任何评估记录"
            description="点击「导入评估产物」把临时评估目录中的新结果入库。"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-200 text-left text-sm">
              <thead>
                <tr className="border-b border-slate-100 text-xs text-slate-400">
                  <th className="py-2 pr-2 font-medium">对比</th>
                  <th className="py-2 pr-2 font-medium">时间</th>
                  <th className="py-2 pr-2 font-medium">类型</th>
                  <th className="py-2 pr-2 font-medium">标签</th>
                  <th className="py-2 pr-2 font-medium">黄金集版本</th>
                  <th className="py-2 pr-2 font-medium">关键指标</th>
                  <th className="py-2 font-medium">数据口径</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const active = detailId === r.id;
                  return (
                    <tr
                      key={r.id}
                      onClick={() => setDetailId(active ? null : r.id)}
                      className={`cursor-pointer border-b border-slate-50 align-top transition-colors ${
                        active ? "bg-indigo-50/60" : "hover:bg-slate-50"
                      }`}
                    >
                      <td className="py-2.5 pr-2" onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center gap-1">
                          {(["A", "B"] as const).map((slot) => {
                            const picked =
                              slot === "A" ? pickA === r.id : pickB === r.id;
                            return (
                              <button
                                key={slot}
                                type="button"
                                aria-pressed={picked}
                                onClick={() => togglePick(slot, r.id)}
                                title={`把该次跑分选为对比${slot}`}
                                className={`h-6 w-6 rounded-md border text-xs font-semibold transition-colors ${
                                  picked
                                    ? "border-indigo-600 bg-indigo-600 text-white"
                                    : "border-slate-300 bg-white text-slate-400 hover:bg-slate-100"
                                }`}
                              >
                                {slot}
                              </button>
                            );
                          })}
                        </div>
                      </td>
                      <td className="py-2.5 pr-2 text-xs whitespace-nowrap text-slate-600">
                        {fmtDateTime(r.at)}
                      </td>
                      <td className="py-2.5 pr-2">
                        <KindBadge />
                      </td>
                      <td className="py-2.5 pr-2">
                        <span className="text-sm font-medium text-slate-800">
                          {r.label}
                        </span>
                        <span
                          className="ml-1.5 font-mono text-body-sm text-slate-400"
                          title={r.source}
                        >
                          {r.source}
                        </span>
                      </td>
                      <td className="py-2.5 pr-2 font-mono text-xs whitespace-nowrap text-slate-500">
                        {r.golden_set_version ?? "—"}
                      </td>
                      <td className="py-2.5 pr-2">
                        {r.summary.length === 0 ? (
                          <span className="text-xs text-slate-400">—</span>
                        ) : (
                          <span className="flex flex-wrap gap-1">
                            {r.summary.map((s) => (
                              <span
                                key={s.key}
                                className="rounded-md bg-slate-100 px-1.5 py-0.5 font-mono text-body-sm text-slate-700"
                                title={`${s.key} = ${s.value}`}
                              >
                                {s.key} {s.display}
                              </span>
                            ))}
                          </span>
                        )}
                      </td>
                      <td className="py-2.5 text-body-sm text-slate-400">
                        {r.data_basis ?? "—"}
                        {r.corpus_version ? (
                          <>
                            <br />
                            {r.corpus_version}
                          </>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {(pickA || pickB) && (
          <p className="mt-3 text-xs text-slate-500">
            已选 A：
            <span className="font-mono">
              {pickA ? pickA.slice(0, 8) : "（未选）"}
            </span>
            　已选 B：
            <span className="font-mono">
              {pickB ? pickB.slice(0, 8) : "（未选）"}
            </span>
            {!(pickA && pickB) && "　—— 再选一个即可生成 delta 对比"}
            <button
              type="button"
              onClick={() => {
                setPickA(null);
                setPickB(null);
              }}
              className="ml-3 rounded-md border border-slate-300 px-2 py-0.5 text-body-sm text-slate-600 hover:bg-slate-100"
            >
              清空选择
            </button>
          </p>
        )}
      </Card>

      {pickA && pickB && (
        <CompareSection key={`${pickA}|${pickB}`} leftId={pickA} rightId={pickB} />
      )}

      {detailId && <RunDetailPanel key={detailId} id={detailId} />}
    </div>
  );
}
