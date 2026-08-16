// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// ---- 机构（对齐 routers/orgs.py）----

export interface OrgOut {
  id: string;
  name: string;
  /** 仅 admin 可见，member 视图为 null */
  invite_code: string | null;
  role: "admin" | "member";
  member_count: number;
  storage_quota_mb: number;
  created_at: string;
}

export interface OrgMemberOut {
  user_id: string;
  email: string;
  role: "admin" | "member";
  joined_at: string;
}
