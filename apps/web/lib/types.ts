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
