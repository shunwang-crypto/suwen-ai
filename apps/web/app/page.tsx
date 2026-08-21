"use client";

// 营销落地页（未登录首页）。已登录用户进入 v3「今日」总览。

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { Features } from "@/components/landing/features";
import { Flow } from "@/components/landing/flow";
import { Hero } from "@/components/landing/hero";
import { SiteFooter } from "@/components/landing/site-footer";
import { useAuth } from "@/lib/auth-context";

export default function Home() {
  const { status } = useAuth();
  const router = useRouter();

  // 已登录跳总览；AppShell 同步处理，避免恢复登录态时闪回营销页。
  useEffect(() => {
    if (status === "authed") router.replace("/today");
  }, [status, router]);

  if (status === "authed") {
    return (
      <div className="flex h-dvh items-center justify-center bg-surface-muted">
        <div className="flex flex-col items-center gap-3 text-ink-muted">
          <span className="h-8 w-8 animate-spin rounded-full border-2 border-indigo-200 border-t-indigo-600" />
          <span className="text-sm">正在进入今日学习…</span>
        </div>
      </div>
    );
  }

  return (
    <main className="min-h-dvh bg-surface-muted">
      {/* 无 JS（SEO 快照/爬虫渲染）时滚动入场不触发，直接全部可见 */}
      <noscript>
        <style>{`.fx-reveal{opacity:1;transform:none}`}</style>
      </noscript>
      <Hero />
      <Features />
      <Flow />
      <SiteFooter />
    </main>
  );
}
