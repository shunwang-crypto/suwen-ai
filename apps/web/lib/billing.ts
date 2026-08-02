// 计费（P3）前端局部类型与工具（P1 先例：不动 lib/types.ts）。
// 后端契约已冻结，字段形状勿改：
//   GET  /api/billing/wallet             -> { balance: "10.0000", currency: "CNY" }
//   GET  /api/billing/ledger?limit=50    -> { data: LedgerEntryOut[] }
//   GET  /api/billing/pricing            -> 计费、充值与邀请活动配置
//   POST /api/billing/orders             -> { id, ..., status: "pending", provider: "mock" }
//   POST /api/billing/orders/{id}/mock-pay -> { status: "paid", balance }

export type LedgerKind =
  | "signup_grant"
  | "consume"
  | "recharge"
  | "refund"
  | "admin_adjust";

export interface WalletOut {
  balance: string;
  currency: string;
}

export interface LedgerEntryOut {
  id: string;
  delta: string;
  balance_after: string;
  kind: LedgerKind;
  ref: string | null;
  note: string | null;
  created_at: string;
}

export interface LedgerPageOut {
  data: LedgerEntryOut[];
}

export interface PricingOut {
  markup: number;
  signup_grant: string;
  recharge_enabled: boolean;
  referral_campaign: {
    enabled: boolean;
    url: string | null;
    reward_credits: string;
  };
  note: string;
}

export interface OrderOut {
  id: string;
  status: string;
  provider: string;
}

export interface MockPayOut {
  status: string;
  balance: string;
}

/** 充值金额白名单（与后端订单接口一致，1 元 = 1 额度）。 */
export const RECHARGE_AMOUNTS = [10, 30, 68, 128] as const;

/** 余额低于该值时顶栏徽章/余额卡转琥珀警示。 */
export const LOW_BALANCE_THRESHOLD = 1;

/** 余额变化广播（充值成功 / 收到 402 时触发，顶栏徽章监听后刷新）。 */
export const WALLET_CHANGED_EVENT = "edu:wallet-changed";

export function emitWalletChanged(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(WALLET_CHANGED_EVENT));
  }
}

/** 金额展示："10.0000" -> "10.00"。解析失败原样返回（绝不编造数字）。 */
export function fmtAmount(v: string | null | undefined): string {
  if (v == null || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return v;
  return n.toFixed(2);
}
