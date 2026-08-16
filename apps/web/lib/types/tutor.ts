import type { MasteryChange, PlanTaskCompletion } from "./quiz";

// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// ---- 辅导（F4，对齐 routers/plans.py tutor_turn）----

export interface TutorTurnResult {
  thread_local_id: string;
  reply: string;
  hint_level: number;
  stuck_type: string;
  /** summarize 节点产出的 JSON 字符串（未结束时为空串） */
  session_summary: string;
  mastery_changes: MasteryChange[];
  plan_task_completion?: PlanTaskCompletion | null;
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

// ---- 今日行动（唯一决策源 GET /api/next-actions）----

export interface NextActionItem {
  type: "review" | "resume_insight" | "weak_practice" | "plan_task" | "cold_start" | string;
  title: string;
  reason: string;
  href: string;
}

export interface NextActionsResponse {
  items: NextActionItem[];
}
