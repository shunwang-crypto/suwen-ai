"use client";

// 402 额度不足全局提示：lib/api.ts 在收到 code=INSUFFICIENT_CREDITS 时回调这里。
// 底部居中琥珀 toast：「额度不足」 + 去充值链接；8s 自动消失，可手动关闭。
// 挂载于 AppShell（登录后所有页面生效）；动效走 animate-fade-up，
// prefers-reduced-motion 由 globals.css 全局降级。

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { setOnInsufficientCredits } from "@/lib/api";
import { emitWalletChanged } from "@/lib/billing";

const AUTO_HIDE_MS = 8_000;

export function CreditsToast() {
  const [visible, setVisible] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setOnInsufficientCredits(() => {
      setVisible(true);
      // 402 说明余额已见底，顺带让顶栏徽章刷新
      emitWalletChanged();
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setVisible(false), AUTO_HIDE_MS);
    });
    return () => {
      setOnInsufficientCredits(null);
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  if (!visible) return null;

  return (
    <div
      role="status"
      className="fixed bottom-6 left-1/2 z-50 flex w-max max-w-[calc(100vw-2rem)] -translate-x-1/2 animate-fade-up items-center gap-3 rounded-card border border-amber-300 bg-amber-50 py-2.5 pl-4 pr-2.5 text-sm text-amber-800 shadow-floating"
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="h-4.5 w-4.5 shrink-0 text-amber-500"
        aria-hidden
      >
        <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0ZM12 9v4m0 4h.01" />
      </svg>
      <span className="min-w-0">额度不足，无法完成本次请求</span>
      <Link
        href="/billing"
        onClick={() => setVisible(false)}
        className="shrink-0 rounded-control bg-amber-500 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-amber-600"
      >
        去充值
      </Link>
      <button
        type="button"
        aria-label="关闭提示"
        onClick={() => setVisible(false)}
        className="shrink-0 rounded-control p-1 text-amber-400 transition-colors hover:bg-amber-100 hover:text-amber-600"
      >
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          className="h-3.5 w-3.5"
          aria-hidden
        >
          <path d="M6 6l12 12M18 6 6 18" />
        </svg>
      </button>
    </div>
  );
}
