"use client";

// 顶栏余额徽章：小额度胶囊，点击进 /billing。
// 刷新策略克制：挂载时拉一次 + 窗口聚焦（15s 节流）+ 余额变化广播（充值成功/402）。
// 后端计费接口未就绪（404 等）时整体隐藏，绝不显示编造的数字。

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { apiJson } from "@/lib/api";
import {
  LOW_BALANCE_THRESHOLD,
  WALLET_CHANGED_EVENT,
  fmtAmount,
  type WalletOut,
} from "@/lib/billing";

const FOCUS_THROTTLE_MS = 15_000;

export function BalanceBadge() {
  const [balance, setBalance] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "unavailable">(
    "loading",
  );
  const lastFetchAt = useRef(0);

  useEffect(() => {
    let alive = true;
    const load = async (force: boolean) => {
      if (!force && Date.now() - lastFetchAt.current < FOCUS_THROTTLE_MS) {
        return;
      }
      lastFetchAt.current = Date.now();
      try {
        const wallet = await apiJson<WalletOut>("/api/billing/wallet");
        if (!alive) return;
        setBalance(wallet.balance);
        setState("ready");
      } catch {
        // 接口未就绪/失败：静默隐藏（页面聚焦或余额广播时会再试）
        if (alive) setState("unavailable");
      }
    };
    void load(true);
    const onFocus = () => void load(false);
    const onChanged = () => void load(true);
    window.addEventListener("focus", onFocus);
    window.addEventListener(WALLET_CHANGED_EVENT, onChanged);
    return () => {
      alive = false;
      window.removeEventListener("focus", onFocus);
      window.removeEventListener(WALLET_CHANGED_EVENT, onChanged);
    };
  }, []);

  if (state === "loading") {
    return <span className="skeleton h-6 w-16 rounded-full" aria-hidden />;
  }
  if (state === "unavailable" || balance === null) return null;

  const low = Number(balance) < LOW_BALANCE_THRESHOLD;

  return (
    <Link
      href="/billing"
      title={low ? "额度不足，点击查看苏问 AI 额度" : "账户额度，点击查看苏问 AI 额度"}
      className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors ${
        low
          ? "border-amber-300 bg-amber-50 text-amber-700 hover:border-amber-400"
          : "border-slate-200 bg-white/70 text-slate-600 hover:border-brand-300 hover:text-brand-700"
      }`}
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={`h-3.5 w-3.5 ${low ? "text-amber-500" : "text-accent-500"}`}
        aria-hidden
      >
        <circle cx="12" cy="12" r="9" />
        <path d="m9 8 3 4 3-4M12 12v5M9.5 13.5h5" />
      </svg>
      <span className="font-mono tabular-nums">{fmtAmount(balance)}</span>
      {low && <span className="hidden sm:inline">余额不足</span>}
    </Link>
  );
}
