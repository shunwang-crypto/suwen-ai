// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

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
  selected_citations?: CitationData[];
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
