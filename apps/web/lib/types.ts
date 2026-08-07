// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

export interface UserOut {
  id: string;
  email: string;
  display_name: string | null;
  role: string;
  created_at: string;
  email_verified_at: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
}

export type RegistrationMode = "closed" | "invite" | "open";

export interface PublicRegistrationConfigOut {
  registration_mode: RegistrationMode;
  email_verification_required: boolean;
}

export interface RegistrationInviteOut {
  id: string;
  code: string | null;
  status: "active" | "used" | "revoked" | "expired";
  expires_at: string | null;
  max_uses: number;
  used_count: number;
  created_at: string;
  used_by: string | null;
  used_by_email: string | null;
}

export interface AdminUserOut {
  user_id: string;
  email: string;
  role: string;
  status: string;
  wallet_balance: string;
  created_at: string;
}

export interface AdminUserListItemOut extends AdminUserOut {
  email_verified: boolean;
  last_login_at: string | null;
  last_active_at: string | null;
  today_platform_cost_cny: string;
  total_platform_cost_cny: string;
  platform_call_count: number;
  byok_call_count: number;
  knowledge_base_count: number;
  error_count: number;
  plan_count: number;
}

export interface AdminUserPageOut {
  items: AdminUserListItemOut[];
  page: number;
  page_size: number;
  total: number;
  has_more: boolean;
}

export interface AdminUserDetailOut extends AdminUserListItemOut {
  model_credential: {
    configured: boolean;
    provider: string | null;
    enabled: boolean | null;
    fallback_to_platform: boolean | null;
    key_last_four: string | null;
    normal_model: string | null;
    reasoning_model: string | null;
  };
  recent_admin_adjustments: {
    delta: string;
    balance_after: string;
    note: string | null;
    reference: string | null;
    created_at: string;
  }[];
}

export interface AdminWalletAdjustOut {
  user_id: string;
  previous_balance: string;
  delta: string;
  balance: string;
  note: string;
  reference: string;
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
  /** 服务端 meta_json 透出：truncated（断流未完成）/blocked/mode/citations 等 */
  meta?: Record<string, unknown>;
}

export interface ChatAttachmentOut {
  id: string;
  filename: string;
  mime_type: string;
  kind: "document" | "image";
  size_bytes: number;
  created_at: string;
}

export interface ChatAttachmentCapabilities {
  max_file_mb: number;
  max_files_per_message: number;
  document_suffixes: string[];
  image_suffixes: string[];
  vision_enabled: boolean;
  thinking_enabled: boolean;
  web_search_enabled: boolean;
}

export interface KbOut {
  id: string;
  name: string;
  subject: string;
  kb_version: number;
  created_at: string;
  /** Wave3 机构库：owner_type='org' 时带 org 标识；writable=当前用户可写 */
  owner_type: "user" | "org";
  org_id: string | null;
  org_name: string | null;
  writable: boolean;
}

// ---- 机构（Wave3，对齐 routers/orgs.py）----

export interface OrgOut {
  id: string;
  name: string;
  /** 仅 admin 可见，member 视图为 null */
  invite_code: string | null;
  role: "admin" | "member";
  member_count: number;
  storage_quota_mb: number;
  created_at: string;
}

export interface OrgMemberOut {
  user_id: string;
  email: string;
  role: "admin" | "member";
  joined_at: string;
}

// ---- SSE 事件数据 ----

export interface SessionEventData {
  session_id: string;
  run_id: string;
}

export interface ChatRunStatusOut {
  status: "running" | "idle";
  run_id: string | null;
}

export interface TokenEventData {
  text: string;
  /** 重生成序号：verify 触发二次生成时递增，前端按变化清空重排（防拼接双答案） */
  attempt?: number;
}

export interface FollowupsEventData {
  items: string[];
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
  /** 本条回答正文是否实际引用；未引用来源仍可在搜索结果面板中查看。 */
  cited?: boolean;
  doc_id: string;
  chunk_id: string;
  page?: number | null;
  highlight?: string;
  doc_name?: string;
  heading_path?: string;
  sanitized?: boolean;
  /** 联合检索：引用来源库（多库问答时标注归属） */
  kb_id?: string;
  kb_name?: string;
  source_type?: "kb" | "web";
  url?: string;
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
  recognition_meta_json: {
    page_count?: number;
    pages?: Array<{
      page_number: number;
      extraction_method: "native_text" | "scanned" | "mixed" | "failed";
      recognition_status: "ready" | "failed";
      native_text_chars: number;
      image_count: number;
      vision_used: boolean;
      warning_code?: string;
      error_code?: string;
    }>;
    warnings?: string[];
  };
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

// ---- 学习指南（GET /api/kb/{kb_id}/study-guide，对齐 worker/study_guide.py）----

export interface StudyGuideOutlineItem {
  title: string;
  summary: string;
  kc_names: string[];
}

export interface StudyGuideTerm {
  term: string;
  definition: string;
}

export interface StudyGuide {
  outline: StudyGuideOutlineItem[];
  glossary: StudyGuideTerm[];
  suggested_questions: string[];
  version: number;
  generated_at: string;
  doc_count: number;
}

export interface StudyGuideOut {
  kb_id: string;
  /** 未生成时为 null（前端据此显示「生成学习指南」按钮） */
  guide: StudyGuide | null;
  generation: {
    status: "idle" | "queued" | "running" | "completed" | "failed";
    job_id?: string;
    requested_at?: string;
    started_at?: string;
    finished_at?: string;
    message?: string;
  };
  writable: boolean;
}

/** 引用回原文定位（GET /api/kb/{kb_id}/chunks/{chunk_id}/context）。 */
export interface ChunkContextItem {
  id: string;
  text: string;
  heading_path: string;
  page_start: number | null;
  page_end: number | null;
}

export interface ChunkContextOut {
  chunk: ChunkContextItem;
  doc_id: string;
  doc_name: string;
  prev: ChunkContextItem[];
  next: ChunkContextItem[];
  parent_text: string | null;
  parent_heading_path: string | null;
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

/** 四阶段命中测试的单阶段条目（score 语义随阶段而变：cos/bm25/rrf/rerank）。 */
export interface RetrieveStageEntry {
  chunk_id: string;
  score: number | null;
}

export interface RetrieveChunkMeta {
  text: string;
  heading_path: string;
  page_start: number | null;
}

export interface RetrieveDebugStages {
  dense: RetrieveStageEntry[];
  sparse: RetrieveStageEntry[];
  fused: RetrieveStageEntry[];
  reranked: RetrieveStageEntry[];
  chunk_meta: Record<string, RetrieveChunkMeta>;
}

export interface RetrieveDebugResult {
  query: string;
  dense_hits: number;
  sparse_hits: number;
  used_sparse: boolean;
  used_rerank: boolean;
  confident: boolean;
  chunks: RetrieveDebugChunk[];
  stages?: RetrieveDebugStages;
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
  /** tutor 联动等场景由后端附带 KC 名（quiz 作答路径无此字段） */
  name?: string;
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
  /** F3-A3：正确率 <0.6 且存在活跃计划时，后端自动插入补强任务的结果（否则 null） */
  plan_adjustment?: (PlanAdjustResult & { plan_id: string }) | null;
}

export interface MetacognitionSummary {
  window: "day" | "week" | "all";
  sample_n: number;
  brier_score: number | null;
  overconfidence_rate: number | null;
}

export interface ExplainBackResult {
  id: string;
  understanding_score: number | null;
  feedback: string;
  gap: string;
  retry_guidance: string;
  degraded: boolean;
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
  objective?: string;
  instructions?: string[];
  completion_criteria?: string;
  minutes: number;
  kc_names: string[];
  resource_ref?: number | null;
  resource_id?: string | null;
  resource?: PlanResource | null;
  action_type?: "read" | "practice" | "tutor" | "review_errors" | "mixed" | string;
  /** 题量契约：服务端只接受 1–20。 */
  practice_count?: number | null;
  difficulty?: "easy" | "mid" | "hard" | string | null;
  /** 计划详情接口下发：stages/days/tasks 三层 0 基下标组成的任务键（前端只回传不自造） */
  task_key?: string;
  status?: "pending" | "done" | "skipped";
  done?: boolean;
  /** "adjust" = 学情调整插入的补强任务 */
  source?: string;
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
  id?: string;
  type?: string;
  source_type?: "kb_chunk" | "web" | string;
  title: string;
  kb_id?: string | null;
  url?: string;
  doc_id?: string | null;
  chunk_id?: string | null;
  created_at?: string;
}

export interface PlanListItem {
  id: string;
  goal: string;
  budget_hours: number;
  kb_id?: string | null;
  kb_name?: string | null;
  structure: PlanStructure | null;
  created_at: string;
  is_current?: boolean;
}

/** 任务粒度完成进度（GET /api/plans/{id}）。 */
export interface PlanProgress {
  total: number;
  done: number;
  skipped?: number;
  pending?: number;
  ratio: number;
  effective_ratio?: number;
}

export interface PlanSchedule {
  /** Business-calendar day number, computed by the API time-control clock. */
  current_day: number;
  today_tasks: string[];
  overdue_tasks: string[];
  upcoming_tasks: string[];
}

export interface PlanDetail {
  id: string;
  goal: string;
  budget_hours: number;
  kb_id: string | null;
  kb_name: string | null;
  version: number;
  structure: PlanStructure;
  resources: PlanResource[];
  schedule?: PlanSchedule;
  progress: PlanProgress;
  created_at: string;
}

export interface PlanToggleResult {
  task_key: string;
  status: "pending" | "done" | "skipped";
  done: boolean;
  progress: PlanProgress;
}

/** 学情调整插入的补强任务。 */
export interface PlanInsertedTask {
  task_key: string;
  day: number;
  stage_name: string;
  title: string;
  objective?: string;
  instructions?: string[];
  completion_criteria?: string;
  minutes: number;
  kc_id: string;
  kc_names: string[];
  action_type?: string;
  /** 题量契约：服务端只接受 1–20。 */
  practice_count?: number;
  difficulty?: string | null;
  p_mastery: number;
}

export interface PlanAdjustResult {
  inserted: PlanInsertedTask[];
  reason: string;
  adjustment_id: string | null;
  progress?: PlanProgress;
}

export interface PlanAdjustmentItem {
  id: string;
  reason: string;
  /** manual | low_score_attempt */
  trigger: string;
  inserted: PlanInsertedTask[];
  at: string;
}

// ---- 辅导（F4，对齐 routers/plans.py tutor_turn）----

export interface TutorTurnResult {
  thread_local_id: string;
  reply: string;
  hint_level: number;
  stuck_type: string;
  /** summarize 节点产出的 JSON 字符串（未结束时为空串） */
  session_summary: string;
  mastery_changes: MasteryChange[];
}

export type TutorDraftStatus = "uploaded" | "recognizing" | "review" | "confirmed" | "failed";

export interface TutorRecognizedQuestion {
  index: number;
  question_no: string;
  page_no: number | null;
  type: "single" | "multi" | "blank" | "short";
  stem: string;
  options: string[];
  confidence: number | null;
}

export interface TutorProblemDraft {
  id: string;
  status: TutorDraftStatus;
  selected_pages: number[];
  selected_question_index: number | null;
  stem: string;
  options: string[];
  known_conditions: string;
  current_thought: string;
  confirmed_problem_text: string;
  confirmed_page_no: number | null;
  thread_local_id: string | null;
  warnings: string[];
  error_code: string | null;
  recognition_attempts: number;
  questions: TutorRecognizedQuestion[];
  attachment: {
    resource_id: string;
    filename: string;
    mime_type: string;
    size_bytes: number;
    page_count: number | null;
    preview_url: string;
  } | null;
  vision_uses_platform_quota: boolean;
  created_at: string;
  updated_at: string;
}

export interface TutorSessionState {
  thread_local_id: string;
  problem: string;
  messages: Array<{
    role: "user" | "assistant";
    content: string;
    hint_level?: number;
  }>;
  hint_level: number;
  stuck_type: string;
  session_summary: string;
  pending: boolean;
}

/** session_summary 的解析结构（tutor_summarize.md 契约） */
export interface TutorSessionSummary {
  misconceptions: string[];
  review_suggestions: string[];
}

// ---- 学情报告（F7，对齐 routers/reports.py）----

export interface ReportRadarItem {
  kc_id: string;
  name: string;
  p_mastery: number;
  activity: number;
}

export interface ReportTrendPoint {
  date: string;
  attempts: number;
  correct: number;
  accuracy: number | null;
}

export interface ReportRootCause {
  kc_id: string;
  name: string;
  p_mastery: number;
}

export interface ReportWeakPoint {
  kc_id: string;
  name: string;
  p_mastery: number;
  error_count: number;
  possible_root_cause: ReportRootCause[];
}

export interface ReportAction {
  type: string;
  label: string;
  href: string;
}

export interface LearningReportResponse {
  radar: ReportRadarItem[];
  trend: ReportTrendPoint[];
  weak_points: ReportWeakPoint[];
  /** 薄弱判定红线 (0-1), 由后端 thresholds 唯一来源下发, 前端文案据此渲染 */
  weak_threshold?: number;
  actions: ReportAction[];
  narrative: string | null;
  narrative_note: string | null;
  metrics?: {
    attempt_count: number;
    active_days: number;
    accuracy: number | null;
    newly_mastered_count: number;
    slipped_count: number;
    attempt_count_delta: number;
    active_days_delta: number;
    accuracy_delta: number | null;
  };
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
  /** 全量到期数（B3：与开场卡 next-actions 同一后端函数取数，两处数字必须一致） */
  total_due?: number;
  now: string;
}

export interface ReviewAnswerResult {
  correct: boolean;
  state: string;
  /** 答错时由错因自动分类刷新（stub 网关不分类，保留原值） */
  cause: string;
  next_due: string;
  answer: string;
  explanation: string;
  mastery_changes: MasteryChange[];
}

export interface ReviewVariantResult {
  error_item_id: string;
  variant_of: string;
  question: QuizQuestion;
}

// ---- 错题本（F6，对齐 routers/reviews.py 的 errors_router）----

export interface ErrorQuestion {
  id: string;
  type: string;
  stem: string;
  options: string[];
  answer: string;
  explanation: string;
  difficulty: string;
  source_type: "generated" | "manual" | "image" | "document" | string;
  source_meta: Record<string, unknown>;
}

export interface ErrorItemOut {
  id: string;
  state: string;
  cause: string;
  verification_status: "pending" | "verified";
  kc_ids: string[];
  kc_names: string[];
  next_due: string | null;
  /** 该题在 attempts 里的错误作答次数 */
  error_count: number;
  my_answer: string;
  source_import_id: string | null;
  resource_id: string | null;
  resource_question_id: string | null;
  /** 尚未产生 FSRS/掌握度证据时可修改判分语义字段。 */
  content_editable: boolean;
  is_variant: boolean;
  created_at: string;
  question: ErrorQuestion | null;
}

export interface ErrorImportDraft {
  type: "single" | "multi" | "blank" | "short";
  stem: string;
  options: string[];
  my_answer: string;
  answer: string;
  explanation: string;
  kc_names: string[];
  difficulty: "easy" | "mid" | "hard";
}

export interface ErrorImportOut {
  id: string;
  source_type: "manual" | "image" | "document";
  filename: string | null;
  mime_type: string | null;
  source_url: string | null;
  status: "draft" | "confirmed" | "deleting";
  draft: ErrorImportDraft;
  recognition_warning: string | null;
  confirmed_error_item_id: string | null;
  created_at: string;
}

export interface ErrorImportCapabilities {
  max_file_mb: number;
  document_suffixes: string[];
  image_suffixes: string[];
  vision_enabled: boolean;
}

export interface ErrorListResponse {
  items: ErrorItemOut[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface ErrorKcCount {
  kc_id: string;
  name: string;
  count: number;
}

export interface ErrorStatsResponse {
  total: number;
  by_cause: Record<string, number>;
  by_state: Record<string, number>;
  pending_verification: number;
  by_kc: ErrorKcCount[];
}

// ---- 画像（F8，对齐 routers/profile.py）----

export interface MasteryItem {
  kc_id: string;
  name: string;
  p_mastery: number;
  /** 兼容字段：内部证据累计分，不是统计置信概率。 */
  confidence: number;
  /** 面向用户的定性证据强度，避免把内部 1.0 误读成“100% 正确”。 */
  evidence_strength?: "limited" | "moderate" | "strong";
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

/** 定向出题薄弱点（GET /api/profile/weak-points，与出题后端同源）。 */
export interface WeakPointItem {
  kc_id: string;
  name: string;
  p_mastery: number;
  /** 近 7 天真的错过（排序优先级更高，前端标「近期错过」） */
  recent_error: boolean;
}

export interface WeakPointsResponse {
  threshold: number;
  limit: number;
  weak_points: WeakPointItem[];
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

// ---- 评估看板（F9，对齐 routers/evals.py）----
// 历史报告字段口径不一（M2 老报告只有 hit@5/mrr/ndcg@10），故指标一律 optional。

export type EvalKind = "retrieval" | "simulation";

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

export interface SimulationEstimatorConfig {
  p_l0?: number;
  p_t?: number;
  slip?: number;
  guess?: number;
}

export interface SimulationConfig {
  n_students?: number;
  n_kcs?: number;
  t_rounds?: number;
  threshold?: number;
  max_questions?: number;
  learning_rate?: number;
  theta_max?: number;
  estimator?: SimulationEstimatorConfig;
}

/** 实验 A：掌握度估计精度（每臂一组）。 */
export interface SimulationArmEstimation {
  r2?: number;
  spearman?: number;
  convergence_round?: number | null;
  min_mae?: number;
  min_mae_round?: number;
  final_mae?: number;
  mae_curve_mean?: number[];
}

/** 实验 B：毕业所需题量（每臂一组）。 */
export interface SimulationArmEfficiency {
  mean: number;
  ci95?: number[];
  grad_rate?: number;
  final_theta_mean?: number;
  seed_mean_questions?: number[];
}

export interface SimulationComparison {
  mean_diff: number;
  diff_ci95?: number[];
  wilcoxon_w?: number;
  p_value: number;
  method?: string;
  cohens_d?: number;
}

export interface SimulationSensitivityRow {
  variant: string;
  mean_adaptive: number;
  mean_random: number;
  mean_ladder: number;
  mean_diff_a_vs_r: number;
  p_value: number;
  cohens_d: number;
  direction_holds: boolean;
}

export interface SimulationEvalMetrics {
  kind: "simulation";
  generated_at?: string;
  elapsed_seconds?: number;
  seeds?: number[];
  config?: SimulationConfig;
  experiment_a?: Record<string, SimulationArmEstimation>;
  experiment_b?: {
    arms?: Record<string, SimulationArmEfficiency>;
    comparisons?: Record<string, SimulationComparison>;
  };
  sensitivity?: SimulationSensitivityRow[];
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
  metrics: RetrievalEvalMetrics | SimulationEvalMetrics;
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
  same_kind: boolean;
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

export interface EvalRunRequest {
  kb_id: string;
  tenant_id?: string;
  golden?: string;
  no_sparse?: boolean;
  no_rerank?: boolean;
  label?: string;
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

// ---- 故障注入（M5-D2，对齐 routers/admin.py）----

export type BreakerState = "closed" | "open" | "half_open";

export interface FaultBreaker {
  tier: string;
  candidate_id?: string;
  state: BreakerState;
}

export interface ProviderRouteStatus {
  id: string;
  route: "text" | "vision" | "plan";
  provider: string;
  model: string;
  configured: boolean;
  disabled: boolean;
  routing_enabled: boolean;
  breaker_open: boolean;
  state: BreakerState;
  last_failure_code: string | null;
}

export interface FaultState {
  provider_down: boolean;
  latency_ms: number;
  error_rate: number;
  /** live / record / stub / replay；stub 与 replay 不受故障注入影响 */
  gateway_mode: string;
  active: boolean;
  note: string;
  breakers: FaultBreaker[];
  routing?: ProviderRouteStatus[];
}

export interface FaultPatch {
  provider_down?: boolean;
  latency_ms?: number;
  error_rate?: number;
  trip_breaker?: "cheap" | "standard" | "strong" | "plan" | "opencode-text" | "siliconflow-text" | "opencode-vision" | "siliconflow-vision" | "deepseek-plan" | "opencode-plan" | "siliconflow-plan" | "all";
  reset_breakers?: boolean;
}

// ---- 教师组卷导出（U-A4，对齐 routers/teacher.py）----

export interface TeacherQuizItem {
  id: string;
  title: string;
  status: string;
  created_at: string;
  kb_id: string | null;
  num_questions: number;
  /** easy/mid/hard → 题数 */
  difficulty_counts: Record<string, number>;
}

export interface TeacherExportQuestion {
  index: number;
  id: string;
  type: string;
  type_label: string;
  stem: string;
  options: string[];
  answer: string;
  explanation: string;
  difficulty: string;
  difficulty_label: string;
  kc_ids: string[];
}

export interface TeacherExportJson {
  format: "json";
  exported_at: string;
  exported_by: string;
  quiz: {
    id: string;
    title: string;
    status: string;
    created_at: string;
    kb_id: string | null;
    blueprint: Record<string, unknown>;
  };
  questions: TeacherExportQuestion[];
}

export interface TeacherPrintItem {
  index: number;
  type: string;
  type_label: string;
  stem: string;
  options: string[];
  difficulty: string;
  difficulty_label: string;
  points: number;
}

export interface TeacherPrintAnswer {
  index: number;
  answer: string;
  explanation: string;
}

export interface TeacherExportPrint {
  format: "print";
  exported_at: string;
  exported_by: string;
  quiz: { id: string; title: string; created_at: string; total: number };
  paper: { title: string; instructions: string; items: TeacherPrintItem[] };
  answer_key: { title: string; items: TeacherPrintAnswer[] };
}

// ---- 用户反馈（P2-1，对齐 routers/feedback.py）----

export type FeedbackCategory = "bug" | "answer_wrong" | "abuse" | "other";

export interface FeedbackOut {
  id: string;
  request_id: string;
  category: FeedbackCategory | string;
  content: string;
  page_url: string;
  status: "open" | "closed";
  created_at: string;
  /** admin 列表附带（GET /api/admin/feedback）；用户自查接口无此二字段 */
  user_id?: string;
  email?: string | null;
}

export interface FeedbackListResponse {
  items: FeedbackOut[];
  total: number;
  page?: number;
  page_size?: number;
  pages?: number;
}

// ---- 结构化学习资料与真实测评（0012，对齐 routers/resources.py/assessments.py）----

export type LearningResourceType =
  | "textbook"
  | "lecture"
  | "notes"
  | "exam_paper"
  | "mock_paper"
  | "workbook"
  | "worksheet"
  | "homework"
  | "single_question"
  | "other";

export type LearningResourceStatus =
  | "uploaded"
  | "extracting"
  | "reviewing"
  | "ready"
  | "failed"
  | "archived";

export type LearningResourceRetrievalPolicy = "none" | "reference" | "questions_only";

export interface LearningResourceOut {
  id: string;
  tenant_id: string;
  owner_type: "personal" | "organization";
  owner_id: string;
  resource_type: LearningResourceType;
  title: string;
  subject: string;
  grade: string | null;
  term: string | null;
  source_filename: string | null;
  filename: string | null;
  mime_type: string | null;
  size_bytes: number;
  storage_key: string | null;
  source_url: string | null;
  sha256: string | null;
  page_count: number | null;
  metadata: Record<string, unknown>;
  status: LearningResourceStatus;
  retrieval_policy: LearningResourceRetrievalPolicy;
  linked_kb_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  question_count: number;
  pending_question_count: number;
}

export type ResourceQuestionVerificationStatus = "pending" | "verified" | "rejected";

export interface ResourceQuestionOut {
  id: string;
  resource_question_id: string;
  question_id: string;
  ordinal: number;
  question_no: string | null;
  section_path: string | null;
  page_no: number | null;
  source_region: Record<string, unknown>;
  points: number | null;
  extraction_confidence: number | null;
  verification_status: ResourceQuestionVerificationStatus;
  type: string;
  stem: string;
  options: string[];
  answer?: string;
  detected_student_answer: string;
  detected_reference_answer?: string;
  explanation?: string;
  kc_ids: string[];
  kc_names: string[];
  difficulty: string;
  is_error_selected: boolean;
  /** 已有 FSRS 复习或掌握度证据后，题型/选项/答案/知识点不可编辑。 */
  content_editable: boolean;
}

export interface ResourceConfirmOut {
  resource_id: string;
  status: LearningResourceStatus;
  error_item_ids: string[];
  confirmed_question_count: number;
}

export type AssessmentAttemptType =
  | "formal_exam"
  | "mock_exam"
  | "homework"
  | "practice"
  | "imported_record";

export type AssessmentAttemptStatus = "draft" | "submitted" | "graded";

export interface AssessmentAttemptOut {
  id: string;
  resource_id: string | null;
  student_id: string;
  attempt_type: AssessmentAttemptType;
  title: string;
  taken_at: string | null;
  duration_minutes: number | null;
  score: number | null;
  total_score: number | null;
  status: AssessmentAttemptStatus;
  grader_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface AssessmentAnswerOut {
  id: string;
  resource_question_id: string;
  question_id: string;
  question: {
    type: string;
    stem: string;
    options: string[];
    answer?: string;
    explanation?: string;
  };
  response: string;
  score: number | null;
  max_score: number | null;
  is_correct: boolean | null;
  grading_status: "pending" | "graded" | "verified";
  teacher_comment: string | null;
  created_at: string;
  updated_at: string;
}

export interface AssessmentDetailOut extends AssessmentAttemptOut {
  answers: AssessmentAnswerOut[];
  answers_revealed: boolean;
}

export interface AssessmentGradeOut {
  attempt_id: string;
  status: AssessmentAttemptStatus;
  score: number | null;
  total_score: number | null;
  mastery_changes: MasteryChange[];
}

// ---- 判分申诉（P2-3，对齐 routers/reviews.py appeals_router）----

export interface AppealResult {
  /** adjusted（复核修正）| kept（维持原判）| pending_manual_review（202 登记待人工） */
  status: "adjusted" | "kept" | "pending_manual_review" | string;
  message: string;
  attempt_id: string;
  old_score: number | null;
  new_score: number | null;
  adjusted: boolean;
  /** 是否跨越 0.6 及格线（跨线时后端已回滚并重写掌握度） */
  crossed: boolean;
  mastery_changes: MasteryChange[];
}
