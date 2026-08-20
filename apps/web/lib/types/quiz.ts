import type { PlanAdjustResult, PlanGoalAnalysis, PlanResource, PlanSkillGap, PlanStructure } from "./plans";

// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

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
  kb_id?: string | null;
  question_count?: number;
  attempt_count?: number;
  score?: number | null;
  error_count?: number;
  error_question_ids?: string[];
  kc_count?: number;
  evidence_policy?: "mastery_eligible" | "calibration_only" | string;
  plan_mode?: "course_bound" | "goal_only" | "standalone" | string;
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

export type PracticeRecommendationStatus =
  | "ready"
  | "needs_scope"
  | "preparing"
  | "needs_upload";

export type PracticeRecommendationAction =
  | "review"
  | "quiz"
  | "choose_scope"
  | "upload"
  | "sample_diagnostic";

export interface PracticeRecommendation {
  status: PracticeRecommendationStatus;
  action: PracticeRecommendationAction;
  source: "due_review" | "plan_task" | "weak_kc" | "cold_start" | "mastery" | "choose_scope" | "sample";
  title: string;
  /** 稳定推荐理由码（后端 ReasonCode），经 lib/reason-text.ts 转文案。 */
  reason_code: string;
  reason_params: Record<string, string | number>;
  question_count: number;
  difficulty_mix: Record<"easy" | "mid" | "hard", number>;
  kb: { id: string; name: string } | null;
  scope_options: Array<{ id: string; name: string }>;
  adjustments: Array<"easier" | "harder" | "switch_kc" | "switch_kb">;
  plan_id?: string | null;
  task_id?: string | null;
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
  plan_task_completion?: PlanTaskCompletion | null;
}

export interface PlanTaskCompletion {
  task_id: string;
  status: "pending" | "done" | "skipped";
  completed: boolean;
  changed?: boolean;
  error_code?: string;
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
  goal_analysis?: PlanGoalAnalysis;
  skill_gaps?: PlanSkillGap[];
  plan_mode?: "course_bound" | "goal_only";
  [key: string]: unknown;
}

export interface GraphDoneData {
  thread_id: string;
  schema_version?: 2;
  outcome?: "succeeded" | "cancelled_by_user" | "generation_failed" | "validation_failed";
  result: {
    quiz_id?: string;
    questions?: QuizQuestion[];
    plan_id?: string;
    plan?: PlanStructure;
    [key: string]: unknown;
  } | null;
  error?: {
    code: string;
    message: string;
    stage?: string;
    retryable?: boolean;
    attempts?: number;
  } | null;
}

/** 图工作流的 trace_event 负载：{node, event, ...}（与对话页的 {type,...} 不同）。 */
export interface GraphTraceData {
  node?: string;
  event?: string;
  [key: string]: unknown;
}
