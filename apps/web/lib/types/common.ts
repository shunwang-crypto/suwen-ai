// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

export interface UserOut {
  id: string;
  email: string;
  display_name: string | null;
  role: string;
  created_at: string;
  email_verified_at: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
}

export type RegistrationMode = "closed" | "invite" | "open";

export interface PublicRegistrationConfigOut {
  registration_mode: RegistrationMode;
  email_verification_required: boolean;
}

export interface RegistrationInviteOut {
  id: string;
  code: string | null;
  status: "active" | "used" | "revoked" | "expired";
  expires_at: string | null;
  max_uses: number;
  used_count: number;
  created_at: string;
  used_by: string | null;
  used_by_email: string | null;
}

export interface AdminUserOut {
  user_id: string;
  email: string;
  role: string;
  status: string;
  wallet_balance: string;
  created_at: string;
}

export interface AdminUserListItemOut extends AdminUserOut {
  email_verified: boolean;
  last_login_at: string | null;
  last_active_at: string | null;
  today_platform_cost_cny: string;
  total_platform_cost_cny: string;
  platform_call_count: number;
  byok_call_count: number;
  knowledge_base_count: number;
  error_count: number;
  plan_count: number;
}

export interface AdminUserPageOut {
  items: AdminUserListItemOut[];
  page: number;
  page_size: number;
  total: number;
  has_more: boolean;
}

export interface AdminUserDetailOut extends AdminUserListItemOut {
  model_credential: {
    configured: boolean;
    provider: string | null;
    enabled: boolean | null;
    fallback_to_platform: boolean | null;
    key_last_four: string | null;
    normal_model: string | null;
    reasoning_model: string | null;
  };
  recent_admin_adjustments: {
    delta: string;
    balance_after: string;
    note: string | null;
    reference: string | null;
    created_at: string;
  }[];
}

export interface AdminWalletAdjustOut {
  user_id: string;
  previous_balance: string;
  delta: string;
  balance: string;
  note: string;
  reference: string;
}

export interface SessionOut {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  /** 会话类型（阶段 6 方案 A）：chat=普通对话，tutor=苏格拉底辅导 */
  kind?: "chat" | "tutor";
}

export interface MessageOut {
  id: string;
  role: string;
  content: string;
  created_at: string;
  /** 服务端 meta_json 透出：truncated（断流未完成）/blocked/mode/citations 等 */
  meta?: Record<string, unknown>;
}

export interface ChatAttachmentOut {
  id: string;
  filename: string;
  mime_type: string;
  kind: "document" | "image";
  size_bytes: number;
  created_at: string;
}

export interface ChatAttachmentCapabilities {
  max_file_mb: number;
  max_files_per_message: number;
  document_suffixes: string[];
  image_suffixes: string[];
  vision_enabled: boolean;
  thinking_enabled: boolean;
  web_search_enabled: boolean;
}

export interface KbOut {
  id: string;
  name: string;
  subject: string;
  kb_version: number;
  created_at: string;
  /** 机构库：owner_type='org' 时带 org 标识；writable=当前用户可写 */
  owner_type: "user" | "org";
  org_id: string | null;
  org_name: string | null;
  writable: boolean;
  summary: {
    source_total: number;
    source_ready: number;
    source_processing: number;
    source_failed: number;
    verified_questions: number;
    pending_questions: number;
    plan_id: string | null;
    plan_status: "active" | "completed" | "archived" | null;
    plan_done_tasks: number;
    plan_total_tasks: number;
    plan_progress_ratio: number;
    last_activity_at: string | null;
    next_action:
      | "upload_source"
      | "wait_for_sources"
      | "retry_source"
      | "continue_plan"
      | "create_plan";
  };
}
