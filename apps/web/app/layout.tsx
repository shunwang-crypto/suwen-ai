import type { Metadata } from "next";

import "katex/dist/katex.min.css";
import "./globals.css";

import { AuthProvider } from "@/lib/auth-context";
import { AppShell } from "@/components/app-shell";

export const metadata: Metadata = {
  title: {
    default: "EduAgents · 智能学习助教",
    template: "%s · EduAgents",
  },
  description: "多智能体驱动的个性化学习平台：知识库问答、练习、计划与复习",
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
        </AuthProvider>
      </body>
    </html>
  );
}
