"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export type Workspace = "learn" | "practice" | "plan" | "growth";

const WORKSPACES: Record<Workspace, { label: string; href: string }[]> = {
  learn: [
    { label: "学习对话", href: "/chat" },
    { label: "知识库", href: "/kb" },
  ],
  practice: [
    { label: "智能练习", href: "/quiz" },
    { label: "分级辅导", href: "/tutor" },
    { label: "题目资料", href: "/resources" },
  ],
  plan: [
    { label: "学习计划", href: "/plans" },
    { label: "今日复习", href: "/reviews" },
  ],
  growth: [
    { label: "学习画像", href: "/profile" },
    { label: "学情报告", href: "/reports" },
    { label: "错题本", href: "/errors" },
  ],
};

/**
 * 五工作台下的稳定二级入口。它只负责信息架构，不复制任何业务页面，
 * 因而旧路由、接口调用和页面状态都保持不变。
 */
export function WorkspaceNav({ workspace }: { workspace: Workspace }) {
  const pathname = usePathname();

  return (
    <nav
      aria-label={`${workspace} 工作台功能`}
      className="flex w-fit max-w-full gap-1 overflow-x-auto rounded-control border border-line bg-white p-1"
    >
      {WORKSPACES[workspace].map((item) => {
        const active =
          pathname === item.href || pathname.startsWith(`${item.href}/`);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={`shrink-0 rounded-[7px] px-3 py-1.5 text-xs font-medium transition-colors sm:text-[13px] ${
              active
                ? "bg-ink text-white"
                : "text-ink-secondary hover:bg-surface-muted hover:text-ink"
            }`}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
