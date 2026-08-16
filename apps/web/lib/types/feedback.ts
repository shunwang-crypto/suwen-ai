// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// ---- 用户反馈（P2-1，对齐 routers/feedback.py）----

export type FeedbackCategory = "bug" | "answer_wrong" | "abuse" | "other";

export interface FeedbackOut {
  id: string;
  request_id: string;
  category: FeedbackCategory | string;
  content: string;
  page_url: string;
  status: "open" | "closed";
  created_at: string;
  /** admin 列表附带（GET /api/admin/feedback）；用户自查接口无此二字段 */
  user_id?: string;
  email?: string | null;
}

export interface FeedbackListResponse {
  items: FeedbackOut[];
  total: number;
  page?: number;
  page_size?: number;
  pages?: number;
}
