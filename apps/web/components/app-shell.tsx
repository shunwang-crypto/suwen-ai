"use client";

import { AnimatePresence, MotionConfig, motion } from "framer-motion";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { BrandMark } from "@/components/ui/brand";
import { CreditsToast } from "@/components/ui/credits-toast";
import { useAuth } from "@/lib/auth-context";
import {
  booleanStorageCodec,
  useHydratedLocalStorage,
} from "@/lib/use-hydrated-local-storage";

const PUBLIC_PATHS = ["/", "/login", "/register", "/forgot", "/reset"];
const BARE_AUTHED_PATHS = ["/onboarding"];
const NAV_COLLAPSED_KEY = "suwen:navigation-collapsed";

interface NavItem {
  href: string;
  label: string;
  icon: ReactNode;
}

interface NavGroup {
  label?: string;
  items: NavItem[];
}

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-[17px] w-[17px] shrink-0"
      aria-hidden
    >
      {children}
    </svg>
  );
}

function FigmaNavIcon({ name }: { name: string }) {
  const asset = `/figma/icons/${name}.svg`;
  return (
    <span className="flex h-[17px] w-[17px] shrink-0 items-center justify-center" aria-hidden>
      <span
        className="block h-[14px] w-[14px] bg-current"
        style={{
          WebkitMaskImage: `url(${asset})`,
          maskImage: `url(${asset})`,
          WebkitMaskPosition: "center",
          maskPosition: "center",
          WebkitMaskRepeat: "no-repeat",
          maskRepeat: "no-repeat",
          WebkitMaskSize: "contain",
          maskSize: "contain",
        }}
      />
    </span>
  );
}

const NAV_GROUPS: NavGroup[] = [
  {
    items: [
      {
        href: "/today",
        label: "今日",
        icon: <FigmaNavIcon name="today" />,
      },
    ],
  },
  {
    label: "学习与资料",
    items: [
      {
        href: "/chat",
        label: "AI 对话",
        icon: <FigmaNavIcon name="chat" />,
      },
      {
        href: "/tutor",
        label: "分级辅导",
        icon: <FigmaNavIcon name="tutor" />,
      },
      {
        href: "/kb",
        label: "知识库",
        icon: <FigmaNavIcon name="knowledge" />,
      },
    ],
  },
  {
    label: "练习巩固",
    items: [
      {
        href: "/quiz",
        label: "智能练习",
        icon: <FigmaNavIcon name="practice" />,
      },
      {
        href: "/reviews",
        label: "今日复习",
        icon: <FigmaNavIcon name="review" />,
      },
      {
        href: "/errors",
        label: "错题本",
        icon: <FigmaNavIcon name="errors" />,
      },
      {
        href: "/resources",
        label: "题目资料",
        icon: <FigmaNavIcon name="knowledge" />,
      },
    ],
  },
  {
    label: "规划与成长",
    items: [
      {
        href: "/plans",
        label: "学习计划",
        icon: <FigmaNavIcon name="plans" />,
      },
      {
        href: "/profile",
        label: "学习画像",
        icon: <FigmaNavIcon name="profile" />,
      },
      {
        href: "/reports",
        label: "学情报告",
        icon: <FigmaNavIcon name="reports" />,
      },
    ],
  },
];

const UTILITY_ITEMS: NavItem[] = [
  {
    href: "/orgs",
    label: "机构",
    icon: <FigmaNavIcon name="orgs" />,
  },
  {
    href: "/billing",
    label: "苏问 AI 额度",
    icon: <FigmaNavIcon name="billing" />,
  },
];

const TITLE_MAP: Record<string, string> = {
  "/today": "今日",
  "/chat": "AI 对话",
  "/tutor": "分级辅导",
  "/kb": "知识库",
  "/resources": "题目资料",
  "/quiz": "智能练习",
  "/reviews": "今日复习",
  "/errors": "错题本",
  "/plans": "学习计划",
  "/profile": "学习画像",
  "/reports": "学情报告",
  "/settings": "账户设置",
  "/orgs": "机构",
  "/billing": "苏问 AI 额度",
  "/teacher": "教师组卷",
  "/admin": "运营管理",
  "/evals": "评估看板",
};

function isActive(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function NavLink({ item, pathname, collapsed = false, onNavigate }: { item: NavItem; pathname: string; collapsed?: boolean; onNavigate?: () => void }) {
  const active = isActive(pathname, item.href);
  return (
    <Link
      href={item.href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      title={collapsed ? item.label : undefined}
      className={`group flex items-center rounded-[8px] text-[13px] font-medium transition-colors ${
        collapsed ? "mx-auto h-10 w-10 justify-center" : "h-8 gap-2.5 px-2.5"
      } ${
        active
          ? "bg-[#eeedea] text-ink"
          : "text-ink-secondary hover:bg-[#f4f3f0] hover:text-ink"
      }`}
    >
      <span className={active ? "text-ink" : "text-ink-muted group-hover:text-ink-secondary"}>{item.icon}</span>
      <span className={collapsed ? "sr-only" : "truncate"}>{item.label}</span>
    </Link>
  );
}

function Navigation({ pathname, collapsed = false, onNavigate }: { pathname: string; collapsed?: boolean; onNavigate?: () => void }) {
  return (
    <>
      <nav className={`min-h-0 flex-1 overflow-y-auto pb-3 ${collapsed ? "px-1.5" : "px-3"}`}>
        {NAV_GROUPS.map((group, index) => (
          <section key={group.label ?? "today"} className={index === 0 ? "pt-2" : collapsed ? "pt-2" : "pt-[18px]"}>
            {group.label && !collapsed && <h2 className="mb-1.5 px-2.5 text-caption font-medium tracking-[0.06em] text-[#a1a19d]">{group.label}</h2>}
            <div className="space-y-0.5">
              {group.items.map((item) => <NavLink key={item.href} item={item} pathname={pathname} collapsed={collapsed} onNavigate={onNavigate} />)}
            </div>
          </section>
        ))}
      </nav>
      <div className={`shrink-0 border-t border-line py-3 ${collapsed ? "px-1.5" : "px-3"}`}>
        <div className="space-y-0.5">
          {UTILITY_ITEMS.map((item) => <NavLink key={item.href} item={item} pathname={pathname} collapsed={collapsed} onNavigate={onNavigate} />)}
        </div>
      </div>
    </>
  );
}

function SidebarToggleIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="h-[19px] w-[19px]" aria-hidden>
      <rect x="2.5" y="3" width="15" height="14" rx="2.5" />
      <path d="M7 3v14" />
      {collapsed ? <path d="m11 7.5 2.5 2.5-2.5 2.5" /> : <path d="m13.5 7.5-2.5 2.5 2.5 2.5" />}
    </svg>
  );
}

function Brand({ collapsed = false, onToggle }: { collapsed?: boolean; onToggle?: () => void }) {
  return (
    <div className={`flex h-14 shrink-0 items-center ${collapsed ? "justify-center" : "justify-between px-3"}`}>
      {!collapsed && (
        <Link href="/today" className="flex items-center gap-2.5" aria-label="苏问今日">
          <span className="flex h-7 w-7 items-center justify-center rounded-[8px] bg-[#1e1e1c] text-[14px] font-semibold text-white">苏</span>
          <span className="flex flex-col leading-none">
            <span className="text-[14px] font-semibold tracking-[-0.02em] text-ink">苏问</span>
            <span className="mt-1 text-caption font-medium tracking-[0.08em] text-ink-muted">AI 课堂</span>
          </span>
        </Link>
      )}
      {onToggle && (
        <button
          type="button"
          onClick={onToggle}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[8px] text-ink-secondary transition-colors hover:bg-[#e9e8e5] hover:text-ink"
          aria-label={collapsed ? "展开功能导航" : "折叠功能导航"}
          title={collapsed ? "展开功能导航" : "折叠功能导航"}
        >
          <SidebarToggleIcon collapsed={collapsed} />
        </button>
      )}
    </div>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className={`h-3.5 w-3.5 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden>
      <path d="m4 6 4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function AccountMenu({
  displayName,
  email,
  isAdmin,
  isTeacher,
  onClose,
  onLogout,
}: {
  displayName: string;
  email: string;
  isAdmin: boolean;
  isTeacher: boolean;
  onClose: () => void;
  onLogout: () => void;
}) {
  const role = isAdmin ? "管理员" : isTeacher ? "教师" : "学生";
  const menuClass = "flex h-9 items-center rounded-[7px] px-3 text-[13px] text-ink-secondary transition-colors hover:bg-[#f5f4f1] hover:text-ink";

  return (
    <motion.div
      initial={{ opacity: 0, y: -5, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -4, scale: 0.98 }}
      transition={{ duration: 0.14, ease: "easeOut" }}
      className="absolute right-0 top-[calc(100%+8px)] z-50 w-[248px] origin-top-right rounded-[12px] border border-line bg-white p-1.5 shadow-floating"
    >
      <div className="px-3 pb-3 pt-2.5">
        <p className="truncate text-[13px] font-semibold text-ink">{displayName}</p>
        <p className="mt-0.5 truncate text-body-sm text-ink-muted">{email}</p>
        <span className="mt-2 inline-flex rounded-full bg-[#f3f2ef] px-2 py-0.5 text-caption font-medium text-ink-secondary">{role}</span>
      </div>
      <div className="border-t border-line pt-1.5">
        <Link href="/settings?section=profile" onClick={onClose} className={menuClass}>账户设置</Link>
      </div>
      {(isTeacher || isAdmin) && (
        <div className="mt-1 border-t border-line pt-1.5">
          {isTeacher && <Link href="/teacher" onClick={onClose} className={menuClass}>教师组卷</Link>}
          {isAdmin && <Link href="/admin" onClick={onClose} className={menuClass}>运营管理</Link>}
          {isAdmin && <Link href="/evals" onClick={onClose} className={menuClass}>评估看板</Link>}
        </div>
      )}
      <div className="mt-1 border-t border-line pt-1.5">
        <button type="button" onClick={onLogout} className="flex h-9 w-full items-center rounded-[7px] px-3 text-left text-[13px] text-danger-600 transition-colors hover:bg-danger-50">退出登录</button>
      </div>
    </motion.div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, status, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [navCollapsed, setNavCollapsed] = useHydratedLocalStorage(
    NAV_COLLAPSED_KEY,
    false,
    booleanStorageCodec,
  );
  const isPublic = PUBLIC_PATHS.includes(pathname);

  useEffect(() => {
    if (isPublic) {
      if (status === "authed") router.replace("/today");
      return;
    }
    if (status === "guest") router.replace("/login");
  }, [isPublic, status, router]);

  if (isPublic) return <>{children}</>;

  if (status !== "authed") {
    return (
      <div className="flex h-dvh items-center justify-center bg-canvas">
        <div className="flex flex-col items-center gap-4 text-ink-muted">
          <span className="animate-pulse-soft"><BrandMark size={40} /></span>
          <span className="h-6 w-6 animate-spin rounded-full border-2 border-brand-100 border-t-brand-700" />
          <span className="text-sm">{status === "loading" ? "正在恢复登录状态…" : "正在跳转到登录页…"}</span>
        </div>
      </div>
    );
  }

  if (BARE_AUTHED_PATHS.includes(pathname)) return <>{children}</>;

  const isAdmin = user?.role === "admin";
  const isTeacher = user?.role === "teacher" || isAdmin;
  const isChatRoute = isActive(pathname, "/chat");
  const title = Object.entries(TITLE_MAP).find(([path]) => isActive(pathname, path))?.[1] ?? "苏问";
  const displayName = user?.display_name?.trim() || "同学";
  const initial = displayName[0] ?? "学";
  const hour = Number(new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Shanghai", hour: "2-digit", hour12: false }).format(new Date()));
  const greeting = hour < 6 ? "夜深了" : hour < 12 ? "上午好" : hour < 18 ? "下午好" : "晚上好";
  const dateLabel = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date());
  const weekdayLabel = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    weekday: "long",
  }).format(new Date());
  const todayLabel = `${weekdayLabel} · ${dateLabel}`;

  const handleLogout = async () => {
    setMenuOpen(false);
    await logout();
    router.replace("/login");
  };

  return (
    <MotionConfig reducedMotion="user">
      <div className="flex h-dvh overflow-hidden bg-canvas">
        <aside className={`hidden shrink-0 flex-col border-r border-line bg-[#f4f3f1] transition-[width] duration-200 md:flex ${navCollapsed ? "w-16" : "w-[200px]"}`}>
          <Brand collapsed={navCollapsed} onToggle={() => setNavCollapsed((value) => !value)} />
          <Navigation pathname={pathname} collapsed={navCollapsed} />
        </aside>

        <AnimatePresence>
          {drawerOpen && (
            <motion.div className="fixed inset-0 z-40 md:hidden" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.16 }}>
              <button type="button" aria-label="关闭导航" className="absolute inset-0 bg-[#1c1917]/35" onClick={() => setDrawerOpen(false)} />
              <motion.aside className="absolute inset-y-0 left-0 flex w-[260px] flex-col border-r border-line bg-[#f4f3f1] shadow-floating" initial={{ x: -270 }} animate={{ x: 0 }} exit={{ x: -270 }} transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}>
                <Brand />
                <Navigation pathname={pathname} onNavigate={() => setDrawerOpen(false)} />
              </motion.aside>
            </motion.div>
          )}
        </AnimatePresence>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="relative z-30 h-14 shrink-0 border-b border-line bg-[#f7f6f4]">
            <div className="mx-auto flex h-full w-full max-w-[1480px] items-center justify-between px-4 sm:px-8 lg:px-12">
            <div className="flex min-w-0 items-center gap-3">
              <button type="button" onClick={() => setDrawerOpen(true)} className="-ml-1.5 rounded-[7px] p-1.5 text-ink-secondary hover:bg-[#f1f0ed] md:hidden" aria-label="打开导航">
                <Icon><path d="M4 6h16M4 12h16M4 18h16" /></Icon>
              </button>
              {pathname === "/today" ? (
                <div className="min-w-0">
                  <h1 className="truncate text-[14px] font-semibold leading-5 text-ink">{greeting}，{displayName}</h1>
                  <p className="truncate text-caption leading-4 text-ink-muted">{todayLabel}</p>
                </div>
              ) : (
                <h1 className="truncate text-[13px] font-semibold text-ink md:text-[14px]">{title}</h1>
              )}
            </div>

            <div className="relative">
              {menuOpen && <button type="button" aria-label="关闭账户菜单" className="fixed inset-0 z-40 cursor-default" onClick={() => setMenuOpen(false)} />}
              <button
                type="button"
                onClick={() => setMenuOpen((value) => !value)}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                className={`relative z-50 flex h-9 items-center gap-2 rounded-[8px] px-1.5 text-ink transition-colors hover:bg-[#eeedea] ${menuOpen ? "bg-[#eeedea]" : ""}`}
              >
                <span className="flex h-7 w-7 items-center justify-center rounded-full bg-[#2f2f2c] text-body-sm font-semibold text-white">{initial}</span>
                <span className="hidden max-w-40 truncate text-body-sm font-medium sm:block">{displayName}</span>
                <ChevronIcon open={menuOpen} />
              </button>
              <AnimatePresence>
                {menuOpen && <AccountMenu displayName={displayName} email={user?.email ?? ""} isAdmin={isAdmin} isTeacher={isTeacher} onClose={() => setMenuOpen(false)} onLogout={handleLogout} />}
              </AnimatePresence>
            </div>
            </div>
          </header>

          <main key={pathname} className={`min-h-0 flex-1 animate-fade-in ${isChatRoute ? "overflow-hidden" : "overflow-y-auto"}`}>{children}</main>
        </div>

        <CreditsToast />
      </div>
    </MotionConfig>
  );
}
