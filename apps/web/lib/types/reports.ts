// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// ---- 学情报告（F7，对齐 routers/reports.py）----

export interface ReportRadarItem {
  kc_id: string;
  name: string;
  p_mastery: number | null;
  evidence_state?: "unobserved" | "estimated" | "observed";
  evidence_state_label?: string;
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
  p_mastery: number | null;
  evidence_state?: "unobserved" | "estimated" | "observed";
}

export interface ReportWeakPoint {
  kc_id: string;
  name: string;
  p_mastery: number | null;
  evidence_state?: "unobserved" | "estimated" | "observed";
  error_count: number;
  possible_root_cause: ReportRootCause[];
}

export interface LearningReportResponse {
  radar: ReportRadarItem[];
  trend: ReportTrendPoint[];
  weak_points: ReportWeakPoint[];
  /** 薄弱判定红线 (0-1), 由后端 thresholds 唯一来源下发, 前端文案据此渲染 */
  weak_threshold?: number;
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
