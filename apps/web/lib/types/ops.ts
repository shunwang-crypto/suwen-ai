// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

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

// ---- 故障注入（对齐 routers/admin.py）----

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
  trip_breaker?: "cheap" | "standard" | "strong" | "plan" | "trenddao-text" | "trenddao-vision" | "trenddao-plan" | "all";
  reset_breakers?: boolean;
}
