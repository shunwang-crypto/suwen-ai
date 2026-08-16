// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// ---- 画像（F8，对齐 routers/profile.py）----

export interface MasteryItem {
  kc_id: string;
  name: string;
  p_mastery: number | null;
  /** 无证据不是薄弱；只有 observed 才能定性强弱。 */
  evidence_state: "unobserved" | "estimated" | "observed";
  evidence_state_label?: string;
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
  weak_threshold: number;
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
