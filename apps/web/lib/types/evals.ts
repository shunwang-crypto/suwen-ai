// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// ---- 本地检索评估看板（对齐 routers/evals.py）----
// 历史检索报告字段口径不一，故指标一律 optional。

export type EvalKind = "retrieval";

/** 列表页的关键指标缩略（0..5 条，后端已格式化好 display）。 */
export interface EvalSummaryMetric {
  key: string;
  value: number;
  display: string;
}

export interface EvalRunListItem {
  id: string;
  kind: EvalKind;
  label: string;
  source: string;
  at: string;
  generated_at?: string | null;
  corpus_version?: string | null;
  golden_set_version?: string | null;
  /** 数据口径速览，如 "embedding live / rerank live" */
  data_basis?: string | null;
  summary: EvalSummaryMetric[];
}

export interface EvalRunsResponse {
  runs: EvalRunListItem[];
}

/** 报告自带的检索参数快照（值全是字符串，键随版本可能增减）。 */
export type EvalReportConfig = Record<string, string | undefined>;

export interface EvalRunConfig {
  kind?: string;
  source?: string;
  content_sha256?: string;
  label?: string;
  imported_at?: string;
  report_config?: EvalReportConfig;
}

export type RetrievalHeadlineKey =
  | "hit@1"
  | "hit@3"
  | "hit@5"
  | "mrr"
  | "ndcg@5"
  | "ndcg@10";

export type RetrievalHeadline = Partial<Record<RetrievalHeadlineKey, number>>;

export interface RetrievalCategoryRow {
  category: string;
  n: number;
  "hit@1"?: number;
  "hit@3"?: number;
  "hit@5"?: number;
  mrr?: number;
}

export interface RetrievalRefusalRow {
  qid: string;
  question: string;
  n_chunks: number;
  top1_score: number;
  top1_heading?: string | null;
  /** false = 该拒答题被高置信命中（误报），需红标 */
  ok: boolean;
}

export interface RetrievalRefusal {
  correct: number;
  total: number;
  rate: number;
  rows?: RetrievalRefusalRow[];
}

export interface RetrievalEvalMetrics {
  kind: "retrieval";
  generated_at?: string;
  golden_set?: string;
  golden_set_version?: string;
  n_questions?: number;
  n_answerable?: number;
  n_refusal?: number;
  headline?: RetrievalHeadline;
  confident?: { n: number; total: number };
  by_category?: RetrievalCategoryRow[];
  refusal?: RetrievalRefusal;
  report_config?: EvalReportConfig;
}

export interface EvalRunDetail {
  id: string;
  kind: EvalKind;
  label: string;
  source: string;
  at: string;
  corpus_version?: string | null;
  golden_set_version?: string | null;
  config?: EvalRunConfig;
  metrics: RetrievalEvalMetrics;
}

export interface EvalCompareSide {
  id: string;
  label: string;
  kind: EvalKind;
  at: string;
  source: string;
}

export interface EvalCompareDelta {
  metric: string;
  left: number;
  right: number;
  delta: number;
  /** left 为 0 时后端给 null */
  pct: number | null;
}

export interface EvalCompareResponse {
  left: EvalCompareSide;
  right: EvalCompareSide;
  deltas: EvalCompareDelta[];
  left_only: string[];
  right_only: string[];
}

export interface EvalImportItem {
  source: string;
  kind: string;
  status: "imported" | "skipped" | "error";
  id?: string | null;
  error?: string | null;
}

export interface EvalImportResponse {
  imported: number;
  skipped: number;
  items: EvalImportItem[];
}

export interface EvalRunJobStart {
  job_id: string;
  status: string;
}

export type EvalJobStatus = "queued" | "in_progress" | "complete" | "not_found";

export interface EvalJobInfo {
  job_id: string;
  status: EvalJobStatus;
  success: boolean | null;
  eval_run_id: string | null;
  error: string | null;
}
