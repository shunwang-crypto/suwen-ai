"use client";

// 全局应用外壳：左侧导航 + 顶栏用户菜单 + 认证门卫。
// /login /register 为公开页；其余页面未登录一律跳 /login。
// 视觉升级：分组导航（学习/管理）、活动项主色左条、毛玻璃顶栏、
// 确定性头像配色、路由切换内容淡入、framer-motion 抽屉/菜单动效。

import { AnimatePresence, MotionConfig, motion } from "framer-motion";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { BalanceBadge } from "@/components/ui/balance-badge";
import { BrandMark } from "@/components/ui/brand";
import { CreditsToast } from "@/components/ui/credits-toast";
import { useAuth } from "@/lib/auth-context";

// "/" 为营销落地页（app/page.tsx）：未登录可见，已登录由下方 effect 跳 /today
// /forgot /reset 为密码找回流程（邮件链接可直达）
const PUBLIC_PATHS = ["/", "/login", "/register", "/forgot", "/reset"];

// 需要登录但不套侧边导航外壳的全屏聚焦流程（P2-4 Onboarding）
const BARE_AUTHED_PATHS = ["/onboarding"];

interface NavItem {
  href: string;
  label: string;
  icon: ReactNode;
  /** 同一工作台下的二级页面仍高亮主入口。 */
  activePaths?: string[];
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

/** v3 导航：学生端固定五个工作台；旧功能通过工作台二级导航进入。 */
const NAV_GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "",
    items: [
      {
        href: "/today",
        label: "今日",
        icon: icon("M4 5h16v15H4zM8 3v4M16 3v4M4 10h16M8 14h3M8 17h6"),
      },
      {
        href: "/chat",
        label: "学习",
        activePaths: ["/chat", "/kb"],
        icon: icon("M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z"),
      },
      {
        href: "/quiz",
        label: "练习",
        activePaths: ["/quiz", "/tutor"],
        icon: icon("M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2m-6 9 2 2 4-4"),
      },
      {
        href: "/plans",
        label: "计划",
        activePaths: ["/plans", "/reviews"],
        icon: icon("M8 2v4M16 2v4M3 9h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm4 9h6"),
      },
      {
        href: "/profile",
        label: "成长",
        activePaths: ["/profile", "/reports", "/errors"],
        icon: icon("M4 19V9m5 10V5m5 14v-7m5 7V3M3 21h18"),
      },
    ],
  },
  {
    label: "管理",
    items: [
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
  "/today": "今日",
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
  "/settings": "个人中心",
  "/billing": "钱包",
  "/teacher": "组卷导出",
  "/admin": "运营",
  "/evals": "评估看板",
};

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
    <nav className="flex min-h-0 flex-1 flex-col overflow-y-auto px-4 pb-3">
      {NAV_GROUPS.map((group) => {
        const items = group.items.filter(
          (item) =>
            (!item.adminOnly || isAdmin) && (!item.teacherOnly || isTeacher),
        );
        if (items.length === 0) return null;
        return (
          <div key={group.label || "primary"} className="mt-5 flex flex-col gap-1 first:mt-3">
            {group.label && (
              <p className="px-3 pb-1 text-[10px] font-medium tracking-[0.08em] text-ink-muted">
                {group.label}
              </p>
            )}
            {items.map((item) => {
              const paths = item.activePaths ?? [item.href];
              const active = paths.some(
                (path) => pathname === path || pathname.startsWith(`${path}/`),
              );
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  onClick={onNavigate}
                  className={`group relative flex h-10 items-center gap-3 rounded-control px-3 text-[13px] font-medium transition-colors duration-150 ${
                    active
                      ? "bg-surface-muted text-ink"
                      : "text-ink-secondary hover:bg-surface-muted hover:text-ink"
                  }`}
                >
                  {active && (
                    <span className="absolute -left-4 h-7 w-[3px] rounded-r bg-brand-600" aria-hidden />
                  )}
                  <span
                    className={`transition-colors duration-150 ${
                      active
                        ? "text-ink"
                        : "text-ink-muted group-hover:text-ink"
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
    <div className="flex h-[72px] shrink-0 items-center px-5">
      <Link href="/today" className="flex items-center gap-3" aria-label="EduAgents 今日">
        <span className="flex h-7 w-7 items-center justify-center rounded-[7px] bg-ink text-xs font-bold text-white">
          E
        </span>
        <span className="text-sm font-bold tracking-[-0.01em] text-ink">EduAgents</span>
      </Link>
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
      if (status === "authed") router.replace("/today");
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
  const isChatRoute = pathname === "/chat" || pathname.startsWith("/chat/");
  const isAccountRoute = ["/settings", "/orgs", "/billing"].some(
    (path) => pathname === path || pathname.startsWith(`${path}/`),
  );
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
    <div className="flex h-dvh overflow-hidden bg-canvas">
      {/* 桌面端侧边导航：v3 固定 220px，内容滚动而导航保持稳定。 */}
      <aside className="hidden w-[220px] shrink-0 flex-col border-r border-line bg-surface md:flex">
        <Brand />
        <NavLinks isAdmin={isAdmin} isTeacher={isTeacher} pathname={pathname} />
        <div className="relative border-t border-line p-4">
          {isAccountRoute && (
            <span className="absolute left-0 top-3 h-9 w-[3px] rounded-r bg-brand-600" aria-hidden />
          )}
          <div className={`flex items-center rounded-control p-1 ${isAccountRoute ? "bg-surface-muted" : ""}`}>
            <Link
              href="/settings"
              className="flex min-w-0 flex-1 items-center gap-2.5 rounded-control text-left"
            >
            <span className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-full bg-ink text-[13px] font-medium text-white">
              {(user?.email?.[0] ?? "学").toUpperCase()}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] font-medium text-slate-900">
                {user?.email}
              </span>
              <span className="block text-xs text-slate-400">
                {isAdmin ? "管理员" : isTeacher ? "教师" : "学习者"}
              </span>
            </span>
            </Link>
            <button
              type="button"
              onClick={() => setMenuOpen((value) => !value)}
              className="rounded-control px-2 py-1.5 text-lg leading-none text-ink-muted hover:bg-white hover:text-ink"
              aria-label="打开账户菜单"
            >
              ···
            </button>
          </div>
          {menuOpen && (
            <>
              <button
                type="button"
                aria-label="关闭菜单"
                className="fixed inset-0 z-10 cursor-default"
                onClick={() => setMenuOpen(false)}
              />
              <div className="absolute bottom-[72px] left-4 z-20 w-[188px] rounded-overlay border border-line bg-white p-1.5 shadow-floating">
                <Link
                  href="/settings"
                  onClick={() => setMenuOpen(false)}
                  className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                >
                  个人中心
                </Link>
                <Link
                  href="/orgs"
                  onClick={() => setMenuOpen(false)}
                  className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                >
                  机构与权限
                </Link>
                <Link
                  href="/billing"
                  onClick={() => setMenuOpen(false)}
                  className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                >
                  钱包与额度
                </Link>
                {isTeacher && (
                  <Link
                    href="/teacher"
                    onClick={() => setMenuOpen(false)}
                    className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                  >
                    组卷导出
                  </Link>
                )}
                {isAdmin && (
                  <>
                    <Link
                      href="/admin"
                      onClick={() => setMenuOpen(false)}
                      className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                    >
                      运营管理
                    </Link>
                    <Link
                      href="/evals"
                      onClick={() => setMenuOpen(false)}
                      className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                    >
                      评估看板
                    </Link>
                  </>
                )}
                <div className="my-1 border-t border-line" />
                <button
                  type="button"
                  onClick={handleLogout}
                  className="w-full rounded-control px-3 py-2 text-left text-sm text-danger-600 hover:bg-danger-50"
                >
                  退出登录
                </button>
              </div>
            </>
          )}
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
              className="absolute inset-y-0 left-0 flex w-[260px] flex-col bg-surface shadow-floating"
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
              <Link
                href="/settings"
                onClick={() => setDrawerOpen(false)}
                className="mx-4 mb-5 flex items-center gap-3 border-t border-line px-2 pt-4 text-[13px] font-medium text-ink-secondary"
              >
                <span className="flex h-8 w-8 items-center justify-center rounded-full bg-ink text-white">
                  {(user?.email?.[0] ?? "学").toUpperCase()}
                </span>
                <span className="min-w-0 flex-1 truncate">个人中心</span>
              </Link>
            </motion.aside>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* 顶栏：毛玻璃 */}
        <header className="glass z-10 flex h-14 shrink-0 items-center justify-between border-b border-line px-4 md:hidden">
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

          <div className="flex items-center gap-2 sm:gap-3">
            {/* 余额徽章：点击进 /billing，余额 <1 转琥珀警示 */}
            <BalanceBadge />

          <div className="relative">
            <button
              type="button"
              onClick={() => setMenuOpen((v) => !v)}
              className="flex items-center gap-2 rounded-full py-1 pl-1 pr-2 transition-colors hover:bg-slate-100"
            >
              <span className="flex h-8 w-8 items-center justify-center rounded-full bg-ink text-sm font-semibold text-white shadow-subtle">
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
                  <Link
                    href="/settings"
                    onClick={() => setMenuOpen(false)}
                    className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                  >
                    个人中心
                  </Link>
                  <Link
                    href="/orgs"
                    onClick={() => setMenuOpen(false)}
                    className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                  >
                    机构与权限
                  </Link>
                  <Link
                    href="/billing"
                    onClick={() => setMenuOpen(false)}
                    className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                  >
                    钱包与额度
                  </Link>
                  {isTeacher && (
                    <Link
                      href="/teacher"
                      onClick={() => setMenuOpen(false)}
                      className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                    >
                      组卷导出
                    </Link>
                  )}
                  {isAdmin && (
                    <>
                      <Link
                        href="/admin"
                        onClick={() => setMenuOpen(false)}
                        className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                      >
                        运营管理
                      </Link>
                      <Link
                        href="/evals"
                        onClick={() => setMenuOpen(false)}
                        className="block rounded-control px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
                      >
                        评估看板
                      </Link>
                    </>
                  )}
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
          </div>
        </header>

        {/* 路由切换时内容区淡入（key 触发重挂载；仅 opacity 动画，不影响 fixed 定位后代） */}
        <main
          key={pathname}
          className={`min-h-0 flex-1 animate-fade-in ${
            isChatRoute ? "overflow-hidden" : "overflow-y-auto"
          }`}
        >
          {children}
        </main>
      </div>

      {/* 402 额度不足全局提示（lib/api.ts 拦截后回调展示） */}
      <CreditsToast />
    </div>
    </MotionConfig>
  );
}
