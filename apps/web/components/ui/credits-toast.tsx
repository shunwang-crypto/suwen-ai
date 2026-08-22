"use client";

// 402 额度不足全局注册器：lib/api.ts 收到 INSUFFICIENT_CREDITS 后回调这里。
// 这里只接入统一 toast 总线，不再单独渲染第二套底部弹层。

import { useEffect } from "react";

import { setOnInsufficientCredits } from "@/lib/api";
import { emitWalletChanged } from "@/lib/billing";
import { toast } from "@/lib/toast";

export function CreditsToastRegistrar() {
  useEffect(() => {
    setOnInsufficientCredits(() => {
      // 402 说明余额已见底，顺带让顶栏徽章刷新
      emitWalletChanged();
      toast.warning("额度不足，无法完成本次请求", {
        action: { label: "去充值", href: "/billing" },
        durationMs: null,
      });
    });
    return () => {
      setOnInsufficientCredits(null);
    };
  }, []);

  return null;
}
