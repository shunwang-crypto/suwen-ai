import type { MasteryChange, PlanTaskCompletion, QuizQuestion } from "./quiz";

// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

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
  correct: boolean | null;
  grading_status?: "pending" | "scored";
  submission_id?: string;
  grading_revision?: number;
  retry_available?: boolean;
  state: string;
  /** 答错时由错因自动分类刷新（stub 网关不分类，保留原值） */
  cause: string;
  next_due: string;
  answer: string;
  explanation: string;
  mastery_changes: MasteryChange[];
  idempotent?: boolean;
  plan_task_completion?: PlanTaskCompletion | null;
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
  /** 所属题目资料的关联知识库（无资料或未挂库为 null，供「未归类」筛选展示） */
  linked_kb_id: string | null;
  /** 尚未产生 FSRS/掌握度证据时可修改判分语义字段。 */
  content_editable: boolean;
  is_variant: boolean;
  created_at: string;
  question: ErrorQuestion | null;
}

export interface TutorContextOut {
  problem: string;
  error_item_id?: string;
  kb_id?: string | null;
  plan_id?: string;
  task_id?: string;
  kc_ids?: string[];
}

export interface TutorEntrypointsOut {
  today_task:
    | { status: "ready"; plan_id: string; task_id: string }
    | { status: "empty"; reason: "NO_PENDING_TODAY_TASK" };
  errors:
    | { status: "ready"; error_ids: string[] }
    | { status: "empty"; reason: "NO_AVAILABLE_ERRORS"; error_ids: [] };
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
