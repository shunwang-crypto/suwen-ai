// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// ---- 学情报告（F7，对齐 routers/reports.py）----

export type EvidenceState = "unobserved" | "estimated" | "observed";

/** 章节/知识库聚合雷达轴（learning report.radar）。 */
export interface ReportRadarAxisKcDetail {
  kc_id: string;
  name: string;
  p_mastery: number | null;
  evidence_state: EvidenceState;
  evidence_state_label?: string;
}

export interface ReportRadarAxis {
  /** 机器稳定键；旧快照可能没有该字段。 */
  key?: string;
  name: string;
  kb_id?: string | null;
  p_mastery: number | null;
  kc_count?: number | null;
  weak_count?: number | null;
  kcs?: ReportRadarAxisKcDetail[] | null;
}

/** 兼容字段 learning report.radar_kcs（按活动量排序的单 KC 轴）。 */
export interface ReportRadarKcItem {
  kc_id: string;
  name: string;
  p_mastery: number | null;
  evidence_state: EvidenceState;
  evidence_state_label?: string;
  activity: number;
}

/** @deprecated Use ReportRadarKcItem for radar_kcs or ReportRadarAxis for radar. */
export type ReportRadarItem = ReportRadarKcItem;

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
  evidence_state?: EvidenceState;
}

export interface ReportWeakPoint {
  kc_id: string;
  name: string;
  p_mastery: number | null;
  evidence_state?: EvidenceState;
  error_count: number;
  possible_root_cause: ReportRootCause[];
}

export interface LearningReportResponse {
  radar: ReportRadarAxis[];
  radar_kcs?: ReportRadarKcItem[];
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
