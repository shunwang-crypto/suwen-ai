// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// ---- 学习计划（F3，对齐 routers/plans.py 与 plan_graph.py）----

export type PlanMode = "course_bound" | "goal_only";
export type PlanGroundingStatus = "kb_grounded" | "web_assisted" | "unreferenced";

export interface PlanGoalAnalysis {
  summary: string;
  quality: "clear" | "partial" | "vague";
  outcomes: string[];
  success_criteria: string[];
  constraints: string[];
  missing_information: Array<
    "scope" | "current_level" | "target_outcome" | "success_criteria"
  >;
}

export interface PlanSkillGap {
  skill: string;
  weak_kc: string;
  required_level: 1 | 2 | 3;
  zone: "较弱" | "待巩固";
  goal_reason?: string;
  gap_reason?: string;
}

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
  /** 服务端生成的稳定 UUID；结构调整不会改变身份，前端只回传不自造。 */
  task_id: string;
  status: "pending" | "done" | "skipped";
  /** 先修未掌握 → 派生锁定态（服务端按实时掌握度计算，不落库，故不进 status） */
  locked?: boolean;
  /** 阻塞该任务的先修知识点及其定性区间（掌握度小数不出境） */
  lock_blockers?: PlanTaskLockBlocker[];
  /** "adjust" = 学情调整插入的补强任务 */
  source?: string;
  /** 目标起步计划的练习只做校准，不更新正式掌握度。 */
  evidence_policy?: "mastery_eligible" | "calibration_only";
  concept_status?: "course_grounded" | "provisional";
}

export interface PlanTaskLockBlocker {
  kc_name: string;
  zone: string;
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
  /** 确认时锁定的可验证目标快照；旧计划无此字段。 */
  goal_analysis?: PlanGoalAnalysis;
  /** 创建时的定性技能差距快照；不含精确掌握度。 */
  skill_gaps?: PlanSkillGap[];
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
  plan_mode: PlanMode;
  grounding_status: PlanGroundingStatus;
  structure: PlanStructure | null;
  created_at: string;
  status: "active" | "completed" | "archived";
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

export interface PlanKcDependencyTaskStatus {
  task_id: string;
  status: "pending" | "done" | "skipped";
}

export interface PlanKcDependencyNode {
  kc_id: string;
  kc_name: string;
  task_ids: string[];
  task_statuses: PlanKcDependencyTaskStatus[];
  is_current_task: boolean;
  is_prerequisite: boolean;
  is_blocking: boolean;
  blocking_task_ids: string[];
}

export interface PlanKcDependencyLevel {
  index: number;
  nodes: PlanKcDependencyNode[];
}

export interface PlanKcDependency {
  levels: PlanKcDependencyLevel[];
  edges: Array<{ from_kc_id: string; to_kc_id: string }>;
  /**
   * 主学习线：到「最深的计划任务 KC」的关键路径，服务端沿真实先修边确定性算出。
   * 相邻两点必然有真实边；前端高亮时仍会再校验一次，绝不把 A→C 当捷径高亮。
   */
  primary_route?: string[];
}

export interface PlanDetail {
  id: string;
  goal: string;
  budget_hours: number;
  kb_id: string | null;
  kb_name: string | null;
  plan_mode: PlanMode;
  grounding_status: PlanGroundingStatus;
  version: number;
  structure: PlanStructure;
  resources: PlanResource[];
  schedule?: PlanSchedule;
  progress: PlanProgress;
  /** 只读的当前计划 KC 及递归先修闭包；不含掌握度数值。 */
  kc_dependency?: PlanKcDependency;
  /**
   * 计划里没有正式知识点承接的临时概念（0034）。只用于说明"这份计划打算覆盖什么"，
   * 不参与掌握度与依赖图 —— 它们尚未经资料或人工确认，不是知识点。
   */
  provisional_concepts?: Array<{ name: string; status: string }>;
  /**
   * 上面临时概念里，现在已能匹配到本库正式 KC（同名，可锚定）的条数（T1-1c）。
   * 资料事后增强出同名 KC 后 > 0，界面据此提示"计划可更新"并给出「重新锚定」入口；
   * 真正的 promote 落库在 POST /api/plans/{id}/re-anchor。
   */
  anchorable_concepts?: number;
  created_at: string;
  status: "active" | "completed" | "archived";
}

export interface PlanToggleResult {
  task_id: string;
  status: "pending" | "done" | "skipped";
  progress: PlanProgress;
  completed?: boolean;
  changed?: boolean;
  completion?: { source_type: string; source_id: string; completed_at: string };
}

/** 学情调整插入的补强任务。 */
export interface PlanInsertedTask {
  task_id: string;
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
