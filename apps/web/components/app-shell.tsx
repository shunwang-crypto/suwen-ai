"use client";

import { AnimatePresence, MotionConfig, motion } from "framer-motion";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { BrandMark } from "@/components/ui/brand";
import { CreditsToast } from "@/components/ui/credits-toast";
import { useAuth } from "@/lib/auth-context";

const PUBLIC_PATHS = ["/", "/login", "/register", "/forgot", "/reset"];
const BARE_AUTHED_PATHS = ["/onboarding"];

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

const NAV_GROUPS: NavGroup[] = [
  {
    items: [
      {
        href: "/today",
        label: "今日",
        icon: <Icon><path d="M5 4.5h14v15H5zM8 2.8v3.4m8-3.4v3.4M5 9h14m-9 4h4m-4 3h6" /></Icon>,
      },
    ],
  },
  {
    label: "学习与资料",
    items: [
      {
        href: "/chat",
        label: "AI 对话",
        icon: <Icon><path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5H6l-3 2v-9.5A7.5 7.5 0 0 1 10.5 4h2A7.5 7.5 0 0 1 20 11.5Z" /></Icon>,
      },
      {
        href: "/tutor",
        label: "分级辅导",
        icon: <Icon><path d="M4 5.5 12 3l8 2.5-8 2.5-8-2.5Zm2 2.2V15c2 2.4 4 3.5 6 3.5s4-1.1 6-3.5V7.7M20 6v7" /></Icon>,
      },
      {
        href: "/kb",
        label: "知识库",
        icon: <Icon><path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3V4Zm0 12.8A3.2 3.2 0 0 1 8.2 14H19M9 8h6" /></Icon>,
      },
    ],
  },
  {
    label: "练习巩固",
    items: [
      {
        href: "/quiz",
        label: "智能练习",
        icon: <Icon><path d="M7 4h10a2 2 0 0 1 2 2v14H5V6a2 2 0 0 1 2-2Zm2 5h6m-6 4h6m-6 4h3" /></Icon>,
      },
      {
        href: "/reviews",
        label: "今日复习",
        icon: <Icon><path d="M4 12a8 8 0 1 0 2.3-5.7L4 8.6M4 4v4.6h4.6M12 8v4l2.8 1.8" /></Icon>,
      },
      {
        href: "/errors",
        label: "错题本",
        icon: <Icon><path d="M6 3.5h12v17H6zM9 8l6 6m0-6-6 6m0 3h6" /></Icon>,
      },
    ],
  },
  {
    label: "规划与成长",
    items: [
      {
        href: "/plans",
        label: "学习计划",
        icon: <Icon><path d="M5 4.5h14v15H5zM8 2.8v3.4m8-3.4v3.4M5 9h14m-9 4h4m-4 3h6" /></Icon>,
      },
      {
        href: "/profile",
        label: "学习画像",
        icon: <Icon><circle cx="12" cy="8" r="3.5" /><path d="M5.5 20a6.5 6.5 0 0 1 13 0M18 5l1 1 2-2" /></Icon>,
      },
      {
        href: "/reports",
        label: "学情报告",
        icon: <Icon><path d="M5 20V9m4 11V4m5 16v-7m5 7V7M3 20.5h18" /></Icon>,
      },
    ],
  },
];

const UTILITY_ITEMS: NavItem[] = [
  {
    href: "/orgs",
    label: "机构",
    icon: <Icon><path d="M4 20h16M6 20V8l6-4 6 4v12M9 11h2m2 0h2m-6 4h2m2 0h2" /></Icon>,
  },
  {
    href: "/billing",
    label: "钱包",
    icon: <Icon><path d="M4 6.5h14a2 2 0 0 1 2 2v9H6a2 2 0 0 1-2-2v-9Zm0 2h14V5H6a2 2 0 0 0-2 2m11 6h2" /></Icon>,
  },
];

const TITLE_MAP: Record<string, string> = {
  "/today": "今日",
  "/chat": "AI 对话",
  "/tutor": "分级辅导",
  "/kb": "知识库",
  "/quiz": "智能练习",
  "/reviews": "今日复习",
  "/errors": "错题本",
  "/plans": "学习计划",
  "/profile": "学习画像",
  "/reports": "学情报告",
  "/settings": "账户设置",
  "/orgs": "机构",
  "/billing": "钱包",
  "/teacher": "教师组卷",
  "/admin": "运营管理",
  "/evals": "评估看板",
};

function isActive(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function NavLink({ item, pathname, onNavigate }: { item: NavItem; pathname: string; onNavigate?: () => void }) {
  const active = isActive(pathname, item.href);
  return (
    <Link
      href={item.href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={`group flex h-8 items-center gap-2.5 rounded-[6px] px-2.5 text-[13px] font-medium transition-colors ${
        active
          ? "bg-brand-50 text-brand-700"
          : "text-ink-secondary hover:bg-[#f4f3f0] hover:text-ink"
      }`}
    >
      <span className={active ? "text-brand-700" : "text-ink-muted group-hover:text-ink-secondary"}>{item.icon}</span>
      <span className="truncate">{item.label}</span>
    </Link>
  );
}

function Navigation({ pathname, onNavigate }: { pathname: string; onNavigate?: () => void }) {
  return (
    <>
      <nav className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {NAV_GROUPS.map((group, index) => (
          <section key={group.label ?? "today"} className={index === 0 ? "pt-2" : "pt-[18px]"}>
            {group.label && <h2 className="mb-1.5 px-2.5 text-[10px] font-medium tracking-[0.06em] text-[#a1a19d]">{group.label}</h2>}
            <div className="space-y-0.5">
              {group.items.map((item) => <NavLink key={item.href} item={item} pathname={pathname} onNavigate={onNavigate} />)}
            </div>
          </section>
        ))}
      </nav>
      <div className="shrink-0 border-t border-line px-3 py-3">
        <div className="space-y-0.5">
          {UTILITY_ITEMS.map((item) => <NavLink key={item.href} item={item} pathname={pathname} onNavigate={onNavigate} />)}
        </div>
      </div>
    </>
  );
}

function Brand() {
  return (
    <div className="flex h-14 shrink-0 items-center px-4">
      <Link href="/today" className="flex items-center gap-2.5" aria-label="EduAgents 今日">
        <span className="flex h-[26px] w-[26px] items-center justify-center rounded-[7px] bg-brand-700 text-[11px] font-semibold text-white">E</span>
        <span className="text-[14px] font-semibold tracking-[-0.02em] text-ink">EduAgents</span>
      </Link>
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
  email,
  isAdmin,
  isTeacher,
  onClose,
  onLogout,
}: {
  email: string;
  isAdmin: boolean;
  isTeacher: boolean;
  onClose: () => void;
  onLogout: () => void;
}) {
  const name = email.split("@")[0] || "学习者";
  const role = isAdmin ? "管理员" : isTeacher ? "教师" : "学习者";
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
        <p className="truncate text-[13px] font-semibold text-ink">{name}</p>
        <p className="mt-0.5 truncate text-[11px] text-ink-muted">{email}</p>
        <span className="mt-2 inline-flex rounded-full bg-[#f3f2ef] px-2 py-0.5 text-[10px] font-medium text-ink-secondary">{role}</span>
      </div>
      <div className="border-t border-line pt-1.5">
        <Link href="/settings?section=profile" onClick={onClose} className={menuClass}>账户设置</Link>
        <Link href="/settings?section=preferences" onClick={onClose} className={menuClass}>学习偏好</Link>
        <Link href="/settings?section=privacy" onClick={onClose} className={menuClass}>隐私与数据</Link>
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
  const title = Object.entries(TITLE_MAP).find(([path]) => isActive(pathname, path))?.[1] ?? "EduAgents";
  const initial = (user?.email?.[0] ?? "学").toUpperCase();

  const handleLogout = async () => {
    setMenuOpen(false);
    await logout();
    router.replace("/login");
  };

  return (
    <MotionConfig reducedMotion="user">
      <div className="flex h-dvh overflow-hidden bg-canvas">
        <aside className="hidden w-[200px] shrink-0 flex-col border-r border-line bg-[#fafaf9] md:flex">
          <Brand />
          <Navigation pathname={pathname} />
        </aside>

        <AnimatePresence>
          {drawerOpen && (
            <motion.div className="fixed inset-0 z-40 md:hidden" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.16 }}>
              <button type="button" aria-label="关闭导航" className="absolute inset-0 bg-[#1c1917]/35" onClick={() => setDrawerOpen(false)} />
              <motion.aside className="absolute inset-y-0 left-0 flex w-[260px] flex-col border-r border-line bg-[#fafaf9] shadow-floating" initial={{ x: -270 }} animate={{ x: 0 }} exit={{ x: -270 }} transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}>
                <Brand />
                <Navigation pathname={pathname} onNavigate={() => setDrawerOpen(false)} />
              </motion.aside>
            </motion.div>
          )}
        </AnimatePresence>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="relative z-30 flex h-14 shrink-0 items-center justify-between border-b border-line bg-[#fafaf9] px-4 sm:px-5">
            <div className="flex min-w-0 items-center gap-3">
              <button type="button" onClick={() => setDrawerOpen(true)} className="-ml-1.5 rounded-[7px] p-1.5 text-ink-secondary hover:bg-[#f1f0ed] md:hidden" aria-label="打开导航">
                <Icon><path d="M4 6h16M4 12h16M4 18h16" /></Icon>
              </button>
              <h1 className="truncate text-[13px] font-semibold text-ink md:text-[14px]">{title}</h1>
            </div>

            <div className="relative">
              {menuOpen && <button type="button" aria-label="关闭账户菜单" className="fixed inset-0 z-40 cursor-default" onClick={() => setMenuOpen(false)} />}
              <button
                type="button"
                onClick={() => setMenuOpen((value) => !value)}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                className="relative z-50 flex h-9 items-center gap-2 rounded-[8px] px-1.5 text-ink-secondary transition-colors hover:bg-[#f1f0ed]"
              >
                <span className="flex h-7 w-7 items-center justify-center rounded-full bg-brand-700 text-[11px] font-semibold text-white">{initial}</span>
                <span className="hidden max-w-40 truncate text-[12px] sm:block">{user?.email}</span>
                <ChevronIcon open={menuOpen} />
              </button>
              <AnimatePresence>
                {menuOpen && <AccountMenu email={user?.email ?? ""} isAdmin={isAdmin} isTeacher={isTeacher} onClose={() => setMenuOpen(false)} onLogout={handleLogout} />}
              </AnimatePresence>
            </div>
          </header>

          <main key={pathname} className={`min-h-0 flex-1 animate-fade-in ${isChatRoute ? "overflow-hidden" : "overflow-y-auto"}`}>{children}</main>
        </div>

        <CreditsToast />
      </div>
    </MotionConfig>
  );
}
