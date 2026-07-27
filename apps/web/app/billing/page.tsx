"use client";

// 钱包页（P3 计费）：余额大卡 / 计费说明 / 演示充值（mock 通道）/ 额度流水。
// 后端契约已冻结（见 lib/billing.ts 头注释），四块数据各自独立加载：
// 后端未就绪时对应卡片展示骨架 -> 错误态（可重试），绝不写死或编造余额。

import { useEffect, useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  Skeleton,
  SkeletonList,
  fmtDateTime,
  fmtRelative,
} from "@/components/common";
import { Celebrate } from "@/components/ui/celebrate";
import { ApiError, apiJson } from "@/lib/api";
import {
  LOW_BALANCE_THRESHOLD,
  RECHARGE_AMOUNTS,
  emitWalletChanged,
  fmtAmount,
  type LedgerEntryOut,
  type LedgerKind,
  type LedgerPageOut,
  type MockPayOut,
  type OrderOut,
  type PricingOut,
  type WalletOut,
} from "@/lib/billing";

// ---- 流水 kind 徽章：五色语义化 ----

const KIND_META: Record<LedgerKind, { label: string; cls: string }> = {
  signup_grant: {
    label: "注册赠送",
    cls: "bg-indigo-50 text-indigo-700 ring-indigo-200/70",
  },
  recharge: {
    label: "充值",
    cls: "bg-emerald-50 text-emerald-700 ring-emerald-200/70",
  },
  consume: {
    label: "消费",
    cls: "bg-slate-100 text-slate-600 ring-slate-200/80",
  },
  refund: { label: "退款", cls: "bg-sky-50 text-sky-700 ring-sky-200/70" },
  admin_adjust: {
    label: "人工调整",
    cls: "bg-amber-50 text-amber-700 ring-amber-200/70",
  },
};

function KindBadge({ kind }: { kind: string }) {
  const meta = KIND_META[kind as LedgerKind] ?? {
    label: kind,
    cls: "bg-slate-100 text-slate-600 ring-slate-200/80",
  };
  return (
    <span
      className={`inline-flex whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${meta.cls}`}
    >
      {meta.label}
    </span>
  );
}

/** 流水变动金额：正绿负红，带显式符号。 */
function DeltaText({ delta }: { delta: string }) {
  const n = Number(delta);
  const positive = Number.isFinite(n) ? n >= 0 : !delta.startsWith("-");
  const text = Number.isFinite(n) ? `${n >= 0 ? "+" : ""}${n.toFixed(2)}` : delta;
  return (
    <span
      className={`font-mono font-medium tabular-nums ${
        positive ? "text-emerald-600" : "text-red-600"
      }`}
    >
      {text}
    </span>
  );
}

function fmtMarkup(m: number): string {
  return Number.isInteger(m) ? String(m) : m.toFixed(1);
}

// 404 = 计费服务暂未开通/未就绪（部署节奏差异），给友好中文而非裸 "Not Found"
const errText = (err: unknown, fallback: string) =>
  err instanceof ApiError
    ? err.status === 404
      ? "计费服务暂未就绪，请稍后再试"
      : err.message
    : fallback;

type RechargePhase = "pick" | "pending" | "paid";

export default function BillingPage() {
  // 三块数据独立加载，reloadKey +1 触发重拉（首个 setState 在 await 之后，仓库先例）

  // ---- 余额 ----
  const [wallet, setWallet] = useState<WalletOut | null>(null);
  const [walletLoading, setWalletLoading] = useState(true);
  const [walletError, setWalletError] = useState<string | null>(null);
  const [walletKey, setWalletKey] = useState(0);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await apiJson<WalletOut>("/api/billing/wallet");
        if (!alive) return;
        setWallet(data);
        setWalletError(null);
      } catch (err) {
        if (!alive) return;
        setWallet(null);
        setWalletError(errText(err, "余额加载失败，请稍后重试"));
      } finally {
        if (alive) setWalletLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [walletKey]);

  const reloadWallet = () => {
    setWalletLoading(true);
    setWalletError(null);
    setWalletKey((k) => k + 1);
  };

  // ---- 定价说明 ----
  const [pricing, setPricing] = useState<PricingOut | null>(null);
  const [pricingLoading, setPricingLoading] = useState(true);
  const [pricingError, setPricingError] = useState<string | null>(null);
  const [pricingKey, setPricingKey] = useState(0);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await apiJson<PricingOut>("/api/billing/pricing");
        if (!alive) return;
        setPricing(data);
        setPricingError(null);
      } catch (err) {
        if (!alive) return;
        setPricing(null);
        setPricingError(errText(err, "计费说明加载失败，请稍后重试"));
      } finally {
        if (alive) setPricingLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [pricingKey]);

  const reloadPricing = () => {
    setPricingLoading(true);
    setPricingError(null);
    setPricingKey((k) => k + 1);
  };

  // ---- 流水 ----
  const [ledger, setLedger] = useState<LedgerEntryOut[] | null>(null);
  const [ledgerLoading, setLedgerLoading] = useState(true);
  const [ledgerError, setLedgerError] = useState<string | null>(null);
  const [ledgerKey, setLedgerKey] = useState(0);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const page = await apiJson<LedgerPageOut>(
          "/api/billing/ledger?limit=50",
        );
        if (!alive) return;
        setLedger(page.data);
        setLedgerError(null);
      } catch (err) {
        if (!alive) return;
        setLedger(null);
        setLedgerError(errText(err, "流水加载失败，请稍后重试"));
      } finally {
        if (alive) setLedgerLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [ledgerKey]);

  const reloadLedger = () => {
    setLedgerLoading(true);
    setLedgerError(null);
    setLedgerKey((k) => k + 1);
  };

  // ---- 充值（mock 通道）----
  const [amount, setAmount] = useState<number>(RECHARGE_AMOUNTS[0]);
  const [phase, setPhase] = useState<RechargePhase>("pick");
  const [order, setOrder] = useState<{ id: string; amount: number } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [payError, setPayError] = useState<string | null>(null);
  const [paidBalance, setPaidBalance] = useState<string | null>(null);
  const [celebrateKey, setCelebrateKey] = useState(0);

  const createOrder = async () => {
    setBusy(true);
    setPayError(null);
    try {
      const created = await apiJson<OrderOut>("/api/billing/orders", {
        body: { amount_cny: amount },
      });
      setOrder({ id: created.id, amount });
      setPhase("pending");
    } catch (err) {
      setPayError(errText(err, "创建订单失败，请稍后重试"));
    } finally {
      setBusy(false);
    }
  };

  const mockPay = async () => {
    if (!order) return;
    setBusy(true);
    setPayError(null);
    try {
      const res = await apiJson<MockPayOut>(
        `/api/billing/orders/${order.id}/mock-pay`,
        { body: {} },
      );
      setPaidBalance(res.balance);
      setPhase("paid");
      setCelebrateKey((k) => k + 1);
      // 余额来自支付响应，先本地更新再全量刷新（含流水与顶栏徽章）
      setWallet((prev) => (prev ? { ...prev, balance: res.balance } : prev));
      emitWalletChanged();
      reloadWallet();
      reloadLedger();
    } catch (err) {
      setPayError(errText(err, "支付失败，请稍后重试"));
    } finally {
      setBusy(false);
    }
  };

  const resetRecharge = () => {
    setOrder(null);
    setPhase("pick");
    setPayError(null);
    setPaidBalance(null);
  };

  const lowBalance =
    wallet !== null && Number(wallet.balance) < LOW_BALANCE_THRESHOLD;

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-4 md:p-6">
      <PageHeader
        title="钱包"
        subtitle="额度用于对话、出题、辅导等 AI 功能，1 额度 = 1 元人民币"
      />

      <div className="grid gap-4 lg:grid-cols-5">
        {/* 余额大卡 */}
        <Card title="当前余额" className="lg:col-span-2">
          {walletLoading ? (
            <div role="status" aria-label="加载中" className="space-y-3 py-1">
              <Skeleton className="h-10 w-36" />
              <Skeleton className="h-3 w-44 max-w-full" />
              <span className="sr-only">加载中…</span>
            </div>
          ) : walletError ? (
            <ErrorBanner message={walletError} onRetry={reloadWallet} />
          ) : wallet ? (
            <div className="py-1">
              <p className="flex items-baseline gap-2">
                <span className="text-4xl font-bold tabular-nums tracking-tight text-slate-900">
                  {fmtAmount(wallet.balance)}
                </span>
                <span className="text-sm text-slate-400">额度</span>
              </p>
              <p className="mt-2 text-xs text-slate-400">
                1 额度 = 1 元（{wallet.currency}），AI 功能按实际用量扣减。
              </p>
              {lowBalance && (
                <p className="mt-3 inline-flex items-start gap-1.5 rounded-control bg-amber-50 px-2.5 py-1.5 text-xs font-medium text-amber-700 ring-1 ring-inset ring-amber-200/80">
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="mt-px h-3.5 w-3.5 shrink-0"
                    aria-hidden
                  >
                    <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0ZM12 9v4m0 4h.01" />
                  </svg>
                  余额不足 1 额度，AI 功能可能无法使用，请先充值
                </p>
              )}
            </div>
          ) : null}
        </Card>

        {/* 定价说明卡（数据全部来自 pricing 接口） */}
        <Card title="计费说明" className="lg:col-span-3">
          {pricingLoading ? (
            <div role="status" aria-label="加载中" className="space-y-2.5 py-1">
              <Skeleton className="h-3.5 w-4/5" />
              <Skeleton className="h-3.5 w-3/5" />
              <Skeleton className="h-3.5 w-2/3" />
              <span className="sr-only">加载中…</span>
            </div>
          ) : pricingError ? (
            <ErrorBanner message={pricingError} onRetry={reloadPricing} />
          ) : pricing ? (
            <>
              <ul className="space-y-2.5 text-sm text-slate-600">
                <li className="flex items-start gap-2.5">
                  <span
                    className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-brand-400"
                    aria-hidden
                  />
                  <span>
                    AI 调用按模型用量成本的{" "}
                    <b className="text-slate-800">
                      {fmtMarkup(pricing.markup)} 倍
                    </b>{" "}
                    折算扣减额度，用多少扣多少
                  </span>
                </li>
                <li className="flex items-start gap-2.5">
                  <span
                    className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent-400"
                    aria-hidden
                  />
                  <span>
                    新账号注册即赠{" "}
                    <b className="text-slate-800">
                      {fmtAmount(pricing.signup_grant)} 额度
                    </b>
                  </span>
                </li>
                <li className="flex items-start gap-2.5">
                  <span
                    className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-400"
                    aria-hidden
                  />
                  <span>
                    <b className="text-slate-800">缓存命中免费</b>
                    ：相同请求命中缓存时不扣任何额度
                  </span>
                </li>
              </ul>
              {pricing.note && (
                <p className="mt-3 border-t border-slate-100 pt-2.5 text-xs leading-relaxed text-slate-400">
                  {pricing.note}
                </p>
              )}
            </>
          ) : null}
        </Card>
      </div>

      {/* 充值卡（演示 mock 通道） */}
      <Card
        title="充值"
        extra={
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 ring-1 ring-inset ring-amber-200/80">
            演示环境
          </span>
        }
      >
        {phase === "pick" && (
          <div>
            {payError && (
              <div className="mb-3">
                <ErrorBanner
                  message={payError}
                  onClose={() => setPayError(null)}
                />
              </div>
            )}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {RECHARGE_AMOUNTS.map((v) => (
                <button
                  key={v}
                  type="button"
                  aria-pressed={amount === v}
                  onClick={() => setAmount(v)}
                  className={`rounded-control border px-3 py-3 text-center transition-colors ${
                    amount === v
                      ? "border-brand-500 bg-brand-50 text-brand-700 shadow-subtle"
                      : "border-slate-300 bg-white text-slate-600 hover:border-brand-300 hover:text-brand-600"
                  }`}
                >
                  <span className="block text-lg font-semibold tabular-nums">
                    ¥{v}
                  </span>
                  <span className="mt-0.5 block text-xs opacity-70">
                    +{v} 额度
                  </span>
                </button>
              ))}
            </div>
            <div className="mt-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
              <p className="text-xs text-slate-400">
                支持金额：10 / 30 / 68 / 128 元，充值后额度立即到账。
              </p>
              <button
                type="button"
                onClick={() => void createOrder()}
                disabled={busy}
                className="btn-brand rounded-control px-5 py-2 text-sm font-semibold text-white shadow-card hover:shadow-floating disabled:cursor-not-allowed disabled:opacity-60"
              >
                {busy ? "创建订单中…" : `充值 ¥${amount}`}
              </button>
            </div>
          </div>
        )}

        {phase === "pending" && order && (
          <div className="space-y-3">
            {/* 醒目琥珀横幅：演示支付通道 */}
            <div className="flex items-start gap-3 rounded-card border-2 border-amber-400/70 bg-amber-50 px-4 py-3">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="mt-0.5 h-5 w-5 shrink-0 text-amber-500"
                aria-hidden
              >
                <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0ZM12 9v4m0 4h.01" />
              </svg>
              <div className="min-w-0 text-sm text-amber-800">
                <p className="font-semibold">演示支付通道，未接入真实支付</p>
                <p className="mt-0.5 break-all text-xs text-amber-700/90">
                  订单 ¥{order.amount}（{order.id.slice(0, 8)}
                  …）已创建，点击「模拟支付」立即到账，仅用于功能演示，不产生真实扣款。
                </p>
              </div>
            </div>
            {payError && (
              <ErrorBanner message={payError} onClose={() => setPayError(null)} />
            )}
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => void mockPay()}
                disabled={busy}
                className="btn-brand rounded-control px-5 py-2 text-sm font-semibold text-white shadow-card hover:shadow-floating disabled:cursor-not-allowed disabled:opacity-60"
              >
                {busy ? "支付中…" : `模拟支付 ¥${order.amount}`}
              </button>
              <button
                type="button"
                onClick={resetRecharge}
                disabled={busy}
                className="rounded-control border border-slate-300 px-4 py-2 text-sm font-medium text-slate-500 transition-colors hover:border-slate-400 hover:text-slate-700 disabled:opacity-60"
              >
                取消
              </button>
            </div>
          </div>
        )}

        {phase === "paid" && (
          <div className="relative space-y-3">
            {/* 庆祝彩带：从卡片底部抛洒（reduced-motion 自动降级为静态星标） */}
            <Celebrate key={celebrateKey} />
            <div className="rounded-card border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">
              <p className="flex items-center gap-1.5 font-semibold">
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-4 w-4"
                  aria-hidden
                >
                  <path d="M20 6 9 17l-5-5" />
                </svg>
                支付成功，+{order?.amount ?? "—"} 额度已到账
              </p>
              {paidBalance && (
                <p className="mt-0.5 text-xs">
                  当前余额 {fmtAmount(paidBalance)} 额度
                </p>
              )}
            </div>
            <button
              type="button"
              onClick={resetRecharge}
              className="rounded-control border border-slate-300 px-4 py-2 text-sm font-medium text-slate-500 transition-colors hover:border-brand-300 hover:text-brand-600"
            >
              再充一笔
            </button>
          </div>
        )}
      </Card>

      {/* 额度流水 */}
      <Card
        title="额度流水"
        extra={
          <button
            type="button"
            onClick={reloadLedger}
            disabled={ledgerLoading}
            className="text-xs font-medium text-brand-600 transition-colors hover:text-brand-700 disabled:opacity-60"
          >
            刷新
          </button>
        }
      >
        {ledgerLoading ? (
          <SkeletonList rows={5} />
        ) : ledgerError ? (
          <ErrorBanner message={ledgerError} onRetry={reloadLedger} />
        ) : !ledger || ledger.length === 0 ? (
          <EmptyState
            illustration="chart"
            title="暂无额度流水"
            description="注册赠送、充值与 AI 消费的每一笔变动都会记录在这里"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="border-b border-slate-100 text-left text-xs text-slate-400">
                  <th className="py-2 pr-3 font-medium">类型</th>
                  <th className="py-2 pr-3 font-medium">变动</th>
                  <th className="py-2 pr-3 font-medium">变动后余额</th>
                  <th className="py-2 pr-3 font-medium">备注</th>
                  <th className="py-2 font-medium">时间</th>
                </tr>
              </thead>
              <tbody>
                {ledger.map((row) => (
                  <tr
                    key={row.id}
                    className="border-b border-slate-50 last:border-0"
                  >
                    <td className="py-2.5 pr-3">
                      <KindBadge kind={row.kind} />
                    </td>
                    <td className="py-2.5 pr-3">
                      <DeltaText delta={row.delta} />
                    </td>
                    <td className="py-2.5 pr-3 font-mono text-xs tabular-nums text-slate-500">
                      {fmtAmount(row.balance_after)}
                    </td>
                    <td
                      className="max-w-52 truncate py-2.5 pr-3 text-xs text-slate-500"
                      title={row.note ?? row.ref ?? undefined}
                    >
                      {row.note || row.ref || "—"}
                    </td>
                    <td className="whitespace-nowrap py-2.5 text-xs text-slate-400">
                      <time title={fmtDateTime(row.created_at)}>
                        {fmtRelative(row.created_at)}
                      </time>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
