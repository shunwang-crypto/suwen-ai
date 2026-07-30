import type { Metadata } from "next";

import "katex/dist/katex.min.css";
import "./globals.css";

import { AuthProvider } from "@/lib/auth-context";
import { AppShell } from "@/components/app-shell";
import { FeedbackWidget } from "@/components/feedback-widget";

export const metadata: Metadata = {
  title: {
    default: "苏问 AI课堂 · 个性化学习平台",
    template: "%s · 苏问",
  },
  description: "你的 AI 苏格拉底：知识库问答、分级辅导、练习、计划与复习",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" className="h-full antialiased">
      <body className="h-full">
        <AuthProvider>
          <AppShell>{children}</AppShell>
          <FeedbackWidget />
        </AuthProvider>
      </body>
    </html>
  );
}
