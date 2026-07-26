// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

export interface UserOut {
  id: string;
  email: string;
  role: string;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
}

export interface SessionOut {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface MessageOut {
  id: string;
  role: string;
  content: string;
  created_at: string;
}

export interface KbOut {
  id: string;
  name: string;
  subject: string;
  kb_version: number;
  created_at: string;
}

// ---- SSE 事件数据（七类）----

export interface TokenEventData {
  text: string;
}

export interface NodeUpdateData {
  node: string;
  status: string;
  detail?: string;
  [key: string]: unknown;
}

export interface TraceEventData {
  type: string;
  route?: string;
  reason?: string;
  [key: string]: unknown;
}

export interface CitationData {
  idx: number;
  doc_id: string;
  chunk_id: string;
  page?: number | null;
  highlight?: string;
  doc_name?: string;
  heading_path?: string;
  sanitized?: boolean;
  [key: string]: unknown;
}

export interface InterruptData {
  kind: string;
  payload: unknown;
  resume_url: string;
}

export interface DoneData {
  session_id: string;
  refused?: boolean;
  usage?: {
    cost_cny?: number;
    mode?: string;
    latency_ms?: number;
  };
  [key: string]: unknown;
}

export interface ErrorEventData {
  code: string;
  message: string;
}

// ---- 知识库（M2/M4，对齐 routers/kb.py）----

export interface DocumentOut {
  id: string;
  kb_id: string;
  filename: string;
  size_bytes: number;
  status: string;
  parse_backend: string;
  error_msg: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ChunkOut {
  id: string;
  parent_id: string | null;
  text: string;
  heading_path: string;
  page_start: number | null;
  page_end: number | null;
  kc_ids: unknown[];
  embedding_status: string;
}

export interface RetrieveDebugChunk {
  chunk_id: string;
  score: number;
  rerank_score: number | null;
  source: string;
  heading_path: string;
  page_start: number | null;
  text: string;
}

export interface RetrieveDebugResult {
  query: string;
  dense_hits: number;
  sparse_hits: number;
  used_sparse: boolean;
  used_rerank: boolean;
  confident: boolean;
  chunks: RetrieveDebugChunk[];
}

// ---- 练习（F5，对齐 routers/quiz.py 与 quiz_graph.py）----

export interface BlueprintItem {
  kc_name: string;
  type: string;
  difficulty: string;
}

export interface Blueprint {
  items: BlueprintItem[];
  summary?: string;
}

export interface QuizListItem {
  id: string;
  title: string;
  status: string;
  created_at: string;
}

export interface QuizQuestion {
  id: string;
  type: string;
  stem: string;
  options: string[];
  difficulty: string;
  kc_ids: string[];
}

export interface QuizDetail {
  id: string;
  title: string;
  blueprint: Blueprint | null;
  questions: QuizQuestion[];
}

export interface MasteryChange {
  kc_id: string;
  old_p: number;
  new_p: number;
}

export interface AttemptResultItem {
  question_id: string;
  correct: boolean;
  llm_score: number | null;
  scored_reason: string | null;
  explanation: string;
  answer: string;
  mastery_changes: MasteryChange[];
}

export interface AttemptResult {
  total: number;
  correct: number;
  results: AttemptResultItem[];
}

// ---- 图工作流 SSE（stream_graph_run：interrupt 负载平铺在顶层）----

export interface GraphInterruptData {
  kind: string;
  thread_id: string;
  resume_url: string;
  blueprint?: Blueprint;
  plan?: PlanStructure;
  resources?: PlanResource[];
  [key: string]: unknown;
}

export interface GraphDoneData {
  thread_id: string;
  result: {
    quiz_id?: string;
    questions?: QuizQuestion[];
    plan_id?: string;
    plan?: PlanStructure;
    [key: string]: unknown;
  };
}

/** 图工作流的 trace_event 负载：{node, event, ...}（与对话页的 {type,...} 不同）。 */
export interface GraphTraceData {
  node?: string;
  event?: string;
  [key: string]: unknown;
}

// ---- 学习计划（F3，对齐 routers/plans.py 与 plan_graph.py）----

export interface PlanTask {
  title: string;
  minutes: number;
  kc_names: string[];
  resource_ref: number | null;
}

export interface PlanDay {
  day: number;
  tasks: PlanTask[];
}

export interface PlanStage {
  name: string;
  days: PlanDay[];
}

export interface PlanStructure {
  stages: PlanStage[];
}

export interface PlanResource {
  type: string;
  title: string;
  url?: string;
  doc_id?: string;
  chunk_id?: string;
}

export interface PlanListItem {
  id: string;
  goal: string;
  budget_hours: number;
  structure: PlanStructure | null;
  created_at: string;
}

// ---- 复习（F6，对齐 routers/reviews.py）----

export interface ReviewItem {
  error_item_id: string;
  state: string;
  cause: string;
  due: string | null;
  question: QuizQuestion;
}

export interface ReviewTodayResponse {
  items: ReviewItem[];
  now: string;
}

export interface ReviewAnswerResult {
  correct: boolean;
  state: string;
  next_due: string;
  mastery_changes: MasteryChange[];
}

export interface ReviewVariantResult {
  error_item_id: string;
  variant_of: string;
  question: QuizQuestion;
}

// ---- 画像（F8，对齐 routers/profile.py）----

export interface MasteryItem {
  kc_id: string;
  name: string;
  p_mastery: number;
  confidence: number;
  last_seen: string | null;
}

export interface MasteryLogItem {
  id: string;
  kc_id: string;
  kc_name: string;
  old_p: number;
  new_p: number;
  evidence_type: string;
  evidence_id: string | null;
  weight: number;
  at: string;
}

export interface ProfileResponse {
  mastery: MasteryItem[];
  error_distribution: {
    by_cause: Record<string, number>;
    by_state: Record<string, number>;
  };
  recent_changes: MasteryLogItem[];
}

export interface OverrideResult {
  kc_id: string;
  kc_name: string;
  old_p: number;
  new_p: number;
  log_id: string;
}

// ---- 运营面板（F10，对齐 routers/ops.py 与 routers/admin.py）----

export interface UsageTotals {
  calls: number;
  cost_cny: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
}

export interface UsageByFeature {
  feature: string;
  calls: number;
  cost_cny: number;
  tokens: number;
}

export interface UsageByModel {
  model: string;
  tier: string;
  calls: number;
  cost_cny: number;
}

export interface UsageByMode {
  mode: string;
  calls: number;
  cost_cny: number;
}

export interface UsageDaily {
  date: string;
  calls: number;
  cost_cny: number;
  tokens: number;
}

export interface UsageTopUser {
  user: string;
  calls: number;
  cost_cny: number;
}

export interface AdminUsageResponse {
  days: number;
  totals: UsageTotals;
  by_feature: UsageByFeature[];
  by_model: UsageByModel[];
  by_mode: UsageByMode[];
  daily: UsageDaily[];
  top_users: UsageTopUser[];
}

export interface LatencyRow {
  feature: string;
  calls: number;
  p50_ms: number | null;
  p95_ms: number | null;
}

export interface AdminLatencyResponse {
  days: number;
  by_feature: LatencyRow[];
}

export interface TimeTravelInfo {
  offset_days: number;
  offset_seconds: number;
  virtual_now?: string;
}
