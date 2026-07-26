"use client";

// 全局应用外壳：左侧导航 + 顶栏用户菜单 + 认证门卫。
// /login /register 为公开页；其余页面未登录一律跳 /login。
// 视觉升级：分组导航（学习/管理）、活动项主色左条、毛玻璃顶栏、
// 确定性头像配色、路由切换内容淡入、framer-motion 抽屉/菜单动效。

import { AnimatePresence, MotionConfig, motion } from "framer-motion";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { BrandMark } from "@/components/ui/brand";
import { useAuth } from "@/lib/auth-context";

// "/" 为营销落地页（app/page.tsx）：未登录可见，已登录由下方 effect 跳 /chat
const PUBLIC_PATHS = ["/", "/login", "/register"];

// 需要登录但不套侧边导航外壳的全屏聚焦流程（P2-4 Onboarding）
const BARE_AUTHED_PATHS = ["/onboarding"];

interface NavItem {
  href: string;
  label: string;
  icon: ReactNode;
  adminOnly?: boolean;
  /** teacher 或 admin 可见 */
  teacherOnly?: boolean;
}

function icon(d: string): ReactNode {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-5 w-5 shrink-0"
      aria-hidden
    >
      <path d={d} />
    </svg>
  );
}

/** 导航分组：学习（学生日常）/ 管理（机构与教学运营） */
const NAV_GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "学习",
    items: [
      {
        href: "/chat",
        label: "对话",
        icon: icon("M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z"),
      },
      {
        href: "/kb",
        label: "知识库",
        icon: icon("M4 19V5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2Zm0 0a2 2 0 0 0 2 2h13"),
      },
      {
        href: "/quiz",
        label: "练习",
        icon: icon("M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2m-6 9 2 2 4-4"),
      },
      {
        href: "/tutor",
        label: "辅导",
        icon: icon("M12 4 2 9l10 5 10-5-10-5Zm-6 7.8V16c0 1.66 2.69 3 6 3s6-1.34 6-3v-4.2M22 9v5"),
      },
      {
        href: "/plans",
        label: "学习计划",
        icon: icon("M8 2v4M16 2v4M3 9h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm4 9h6"),
      },
      {
        href: "/reviews",
        label: "复习",
        icon: icon("M3 12a9 9 0 1 0 3-6.7L3 8m0-5v5h5"),
      },
      {
        href: "/errors",
        label: "错题本",
        icon: icon("M4 19.5A2.5 2.5 0 0 1 6.5 17H20M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Zm7 5-4 6m0-6 4 6"),
      },
      {
        href: "/reports",
        label: "学情报告",
        icon: icon("M3 3v18h18M8 17V9m4 8V5m4 12v-6"),
      },
      {
        href: "/profile",
        label: "我的画像",
        icon: icon("M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8a7 7 0 0 1 14 0"),
      },
      {
        href: "/settings",
        label: "设置",
        icon: icon(
          "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
        ),
      },
    ],
  },
  {
    label: "管理",
    items: [
      {
        href: "/orgs",
        label: "机构",
        icon: icon("M3 21h18M5 21V7l7-4 7 4v14M9 9h.01M9 13h.01M9 17h.01M15 9h.01M15 13h.01M15 17h.01"),
      },
      {
        href: "/teacher",
        label: "组卷导出",
        teacherOnly: true,
        icon: icon("M4 4h11l5 5v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm10 0v6h6M8 14h8M8 18h5"),
      },
      {
        href: "/admin",
        label: "运营",
        adminOnly: true,
        icon: icon("M12 3v2m0 14v2M5.2 5.2l1.4 1.4m10.8 10.8 1.4 1.4M3 12h2m14 0h2M5.2 18.8l1.4-1.4M17.4 6.6l1.4-1.4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z"),
      },
      {
        href: "/evals",
        label: "评估看板",
        adminOnly: true,
        icon: icon("M4 20h16M7 20V10m5 10V4m5 16v-7"),
      },
    ],
  },
];

const TITLE_MAP: Record<string, string> = {
  "/chat": "对话",
  "/kb": "知识库",
  "/orgs": "机构",
  "/quiz": "练习",
  "/tutor": "辅导",
  "/plans": "学习计划",
  "/reviews": "复习",
  "/errors": "错题本",
  "/reports": "学情报告",
  "/profile": "我的画像",
  "/settings": "设置",
  "/teacher": "组卷导出",
  "/admin": "运营",
  "/evals": "评估看板",
};

/** 确定性头像配色：同一邮箱永远同一渐变（完整类名字面量，Tailwind 可静态提取） */
const AVATAR_GRADIENTS = [
  "from-indigo-500 to-violet-500",
  "from-sky-500 to-indigo-500",
  "from-emerald-500 to-teal-500",
  "from-amber-500 to-orange-500",
  "from-rose-500 to-pink-500",
  "from-violet-500 to-fuchsia-500",
];

function avatarGradient(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return AVATAR_GRADIENTS[h % AVATAR_GRADIENTS.length];
}

function NavLinks({
  isAdmin,
  isTeacher,
  pathname,
  onNavigate,
}: {
  isAdmin: boolean;
  isTeacher: boolean;
  pathname: string;
  onNavigate?: () => void;
}) {
  return (
    <nav className="flex flex-1 flex-col overflow-y-auto p-3">
      {NAV_GROUPS.map((group) => {
        const items = group.items.filter(
          (item) =>
            (!item.adminOnly || isAdmin) && (!item.teacherOnly || isTeacher),
        );
        if (items.length === 0) return null;
        return (
          <div key={group.label} className="mt-5 flex flex-col gap-1 first:mt-1">
            <p className="px-3 pb-1 text-[11px] font-medium tracking-widest text-slate-400">
              {group.label}
            </p>
            {items.map((item) => {
              const active =
                pathname === item.href || pathname.startsWith(`${item.href}/`);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  onClick={onNavigate}
                  className={`group relative flex items-center gap-3 rounded-control px-3 py-2 text-sm font-medium transition-colors duration-200 ${
                    active
                      ? "bg-brand-50/90 text-brand-700 shadow-subtle"
                      : "text-slate-600 hover:bg-slate-100/80 hover:text-slate-900"
                  }`}
                >
                  {active && (
                    <span
                      aria-hidden
                      className="absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-full bg-brand-gradient"
                    />
                  )}
                  <span
                    className={`transition-[transform,color] duration-200 group-hover:scale-110 ${
                      active
                        ? "text-brand-600"
                        : "text-slate-400 group-hover:text-brand-600"
                    }`}
                  >
                    {item.icon}
                  </span>
                  {item.label}
                </Link>
              );
            })}
          </div>
        );
      })}
    </nav>
  );
}

function Brand() {
  return (
    <div className="flex h-14 shrink-0 items-center border-b border-slate-200/60 px-5">
      <BrandMark size={28} withWordmark className="text-slate-900" />
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, status, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const isPublic = PUBLIC_PATHS.includes(pathname);

  useEffect(() => {
    if (isPublic) {
      if (status === "authed") router.replace("/chat");
      return;
    }
    if (status === "guest") router.replace("/login");
  }, [isPublic, status, router]);

  if (isPublic) {
    return <>{children}</>;
  }

  if (status !== "authed") {
    return (
      <div className="flex h-dvh items-center justify-center bg-aurora">
        <div className="flex flex-col items-center gap-4 text-slate-400">
          <span className="animate-pulse-soft">
            <BrandMark size={40} />
          </span>
          <span className="h-6 w-6 animate-spin rounded-full border-2 border-brand-200 border-t-brand-600" />
          <span className="text-sm">
            {status === "loading" ? "正在恢复登录状态…" : "正在跳转到登录页…"}
          </span>
        </div>
      </div>
    );
  }

  // Onboarding（P2-4）：已登录的全屏聚焦流程，不套导航外壳（跳过/完成后自行跳转）
  if (BARE_AUTHED_PATHS.includes(pathname)) {
    return <>{children}</>;
  }

  const isAdmin = user?.role === "admin";
  const isTeacher = user?.role === "teacher" || isAdmin;
  const title =
    Object.entries(TITLE_MAP).find(
      ([p]) => pathname === p || pathname.startsWith(`${p}/`),
    )?.[1] ?? "EduAgents";

  const handleLogout = async () => {
    setMenuOpen(false);
    await logout();
    router.replace("/login");
  };

  return (
    // reducedMotion="user"：偏好减少动效时，framer-motion（WAAPI）动画同步降级
    <MotionConfig reducedMotion="user">
    <div className="flex h-dvh overflow-hidden bg-slate-50">
      {/* 桌面端侧边导航：浅色渐变底 */}
      <aside className="hidden w-60 shrink-0 flex-col border-r border-slate-200/70 bg-gradient-to-b from-white via-slate-50 to-brand-50/50 md:flex">
        <Brand />
        <NavLinks isAdmin={isAdmin} isTeacher={isTeacher} pathname={pathname} />
        <div className="border-t border-slate-200/60 p-4 text-xs text-slate-400">
          多智能体学习平台
        </div>
      </aside>

      {/* 移动端抽屉导航（framer-motion 滑入/滑出） */}
      <AnimatePresence>
        {drawerOpen && (
          <motion.div
            key="drawer"
            className="fixed inset-0 z-40 md:hidden"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18 }}
          >
            <button
              type="button"
              aria-label="关闭导航"
              className="absolute inset-0 bg-slate-900/40 backdrop-blur-[2px]"
              onClick={() => setDrawerOpen(false)}
            />
            <motion.aside
              className="absolute inset-y-0 left-0 flex w-64 flex-col bg-gradient-to-b from-white via-slate-50 to-brand-50/50 shadow-floating"
              initial={{ x: -280 }}
              animate={{ x: 0 }}
              exit={{ x: -280 }}
              transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
            >
              <Brand />
              <NavLinks
                isAdmin={isAdmin}
                isTeacher={isTeacher}
                pathname={pathname}
                onNavigate={() => setDrawerOpen(false)}
              />
            </motion.aside>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* 顶栏：毛玻璃 */}
        <header className="glass z-10 flex h-14 shrink-0 items-center justify-between border-b border-slate-200/60 px-4">
          <div className="flex items-center gap-3">
            <button
              type="button"
              className="rounded-control p-1.5 text-slate-500 transition-colors hover:bg-slate-100 md:hidden"
              onClick={() => setDrawerOpen(true)}
              aria-label="打开导航"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                className="h-5 w-5"
                aria-hidden
              >
                <path d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            </button>
            <h1 className="text-sm font-semibold text-slate-900">{title}</h1>
          </div>

          <div className="relative">
            <button
              type="button"
              onClick={() => setMenuOpen((v) => !v)}
              className="flex items-center gap-2 rounded-full py-1 pl-1 pr-2 transition-colors hover:bg-slate-100"
            >
              <span
                className={`flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br text-sm font-semibold text-white shadow-subtle ${avatarGradient(
                  user?.email ?? "?",
                )}`}
              >
                {(user?.email?.[0] ?? "?").toUpperCase()}
              </span>
              <span className="hidden max-w-40 truncate text-sm text-slate-700 sm:block">
                {user?.email}
              </span>
            </button>
            {menuOpen && (
              <button
                type="button"
                aria-label="关闭菜单"
                className="fixed inset-0 z-10 cursor-default"
                onClick={() => setMenuOpen(false)}
              />
            )}
            <AnimatePresence>
              {menuOpen && (
                <motion.div
                  key="user-menu"
                  initial={{ opacity: 0, y: -6, scale: 0.98 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: -4, scale: 0.98 }}
                  transition={{ duration: 0.15, ease: "easeOut" }}
                  className="absolute right-0 z-20 mt-2 w-56 rounded-card border border-slate-200/80 bg-white p-1.5 shadow-floating"
                >
                  <div className="px-3 py-2">
                    <p className="truncate text-sm font-medium text-slate-900">
                      {user?.email}
                    </p>
                    <p className="text-xs text-slate-400">
                      {isAdmin ? "管理员" : isTeacher ? "教师" : "学习者"}
                    </p>
                  </div>
                  <div className="my-1 border-t border-slate-100" />
                  <button
                    type="button"
                    onClick={handleLogout}
                    className="w-full rounded-control px-3 py-2 text-left text-sm text-danger-600 transition-colors hover:bg-danger-50"
                  >
                    退出登录
                  </button>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </header>

        {/* 路由切换时内容区淡入（key 触发重挂载；仅 opacity 动画，不影响 fixed 定位后代） */}
        <main
          key={pathname}
          className="min-h-0 flex-1 animate-fade-in overflow-y-auto"
        >
          {children}
        </main>
      </div>
    </div>
    </MotionConfig>
  );
}
