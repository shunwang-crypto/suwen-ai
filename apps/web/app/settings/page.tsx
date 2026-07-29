"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore, type ReactNode } from "react";

import { useAuth } from "@/lib/auth-context";
import { ApiError, apiJson } from "@/lib/api";

interface Preferences {
  explanation_style: string | null;
  verbosity: string | null;
  interests: string[];
  daily_minutes: number | null;
  goal: string | null;
  updated_at: string | null;
}

interface InsightItem {
  id: string;
  kc_name: string;
  source: string;
  kind: string;
  text: string;
  created_at: string;
}

interface InsightsResponse {
  insights: InsightItem[];
}

type SettingsSection = "profile" | "preferences" | "memory" | "privacy" | "security" | "org";

const SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: "profile", label: "个人资料" },
  { id: "preferences", label: "学习偏好" },
  { id: "memory", label: "AI 记住的你" },
  { id: "privacy", label: "隐私与数据" },
  { id: "security", label: "账户与安全" },
  { id: "org", label: "机构与权限" },
];

const STYLE_OPTIONS = [
  { value: "rigorous", label: "严谨", desc: "定义定理优先，推导严密" },
  { value: "colloquial", label: "口语", desc: "大白话讲解，轻松易懂" },
  { value: "analogy", label: "类比优先", desc: "先打比方，再进入概念" },
];
const VERBOSITY_OPTIONS = [
  { value: "concise", label: "简洁", desc: "要点式，点到为止" },
  { value: "detailed", label: "详尽", desc: "步骤完整，例子充分" },
];
const MINUTES_OPTIONS = [15, 30, 45, 60, 90];
const PRESET_INTERESTS = ["篮球", "足球", "动漫", "游戏", "音乐", "科幻", "编程", "历史", "美食", "旅行"];
const INTERESTS_MAX = 8;
const INTEREST_CHAR_MAX = 20;
const GOAL_MAX = 200;
const emptySubscribe = () => () => {};

function Chip({ active, onClick, children, title }: { active: boolean; onClick: () => void; children: ReactNode; title?: string }) {
  return (
    <button type="button" title={title} onClick={onClick} className={`rounded-control border px-3 py-1.5 text-xs font-medium transition-colors ${active ? "border-brand-600 bg-brand-50 text-brand-700" : "border-line-strong bg-white text-ink-secondary hover:border-ink-muted hover:text-ink"}`}>
      {children}
    </button>
  );
}

function SettingsHeader({ title, description }: { title: string; description: string }) {
  return (
    <header className="border-b border-line pb-5">
      <h2 className="text-xl font-semibold tracking-[-0.02em] text-ink">{title}</h2>
      <p className="mt-2 text-xs leading-5 text-ink-secondary">{description}</p>
    </header>
  );
}

function SettingRow({ label, value, note, action }: { label: string; value: ReactNode; note?: string; action?: ReactNode }) {
  return (
    <div className="grid gap-2 border-b border-line py-5 sm:grid-cols-[180px_minmax(0,1fr)_auto] sm:items-center sm:gap-6">
      <span className="text-xs text-ink-muted">{label}</span>
      <div className="min-w-0">
        <div className="text-[13px] font-medium text-ink">{value}</div>
        {note && <p className="mt-1 text-[11px] leading-5 text-ink-muted">{note}</p>}
      </div>
      {action && <div className="justify-self-start sm:justify-self-end">{action}</div>}
    </div>
  );
}

function InlineNotice({ children, tone = "success" }: { children: ReactNode; tone?: "success" | "error" }) {
  return <div className={`mb-5 border-l-2 px-3 py-2 text-xs ${tone === "error" ? "border-danger-500 bg-danger-50 text-danger-700" : "border-success-500 bg-success-50 text-success-700"}`}>{children}</div>;
}

export default function SettingsPage() {
  const router = useRouter();
  const { user, logout } = useAuth();
  const search = useSyncExternalStore(emptySubscribe, () => window.location.search, () => "");
  const requestedSection = new URLSearchParams(search).get("section");
  const querySection = SECTIONS.some((item) => item.id === requestedSection)
    ? requestedSection as SettingsSection
    : "profile";
  const [sectionOverride, setSection] = useState<SettingsSection | null>(null);
  const section = sectionOverride ?? querySection;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [goal, setGoal] = useState("");
  const [interests, setInterests] = useState<string[]>([]);
  const [customInterest, setCustomInterest] = useState("");
  const [minutes, setMinutes] = useState<number | null>(null);
  const [style, setStyle] = useState<string | null>(null);
  const [verbosity, setVerbosity] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [insights, setInsights] = useState<InsightItem[] | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      const [preferencesResult, insightsResult] = await Promise.allSettled([
        apiJson<Preferences>("/api/preferences"),
        apiJson<InsightsResponse>("/api/profile/insights"),
      ]);
      if (!alive) return;
      if (preferencesResult.status === "fulfilled") {
        const data = preferencesResult.value;
        setGoal(data.goal ?? "");
        setInterests(data.interests ?? []);
        setMinutes(data.daily_minutes);
        setStyle(data.explanation_style);
        setVerbosity(data.verbosity);
        setUpdatedAt(data.updated_at);
      } else {
        setError("学习偏好加载失败，可稍后重试");
      }
      setInsights(insightsResult.status === "fulfilled" ? insightsResult.value.insights : []);
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, []);

  const toggleInterest = (tag: string) => {
    setSaved(null);
    setInterests((current) => current.includes(tag) ? current.filter((item) => item !== tag) : current.length >= INTERESTS_MAX ? current : [...current, tag]);
  };

  const addCustomInterest = () => {
    const tag = customInterest.replace(/\s+/g, " ").trim();
    if (!tag) return;
    if (tag.length > INTEREST_CHAR_MAX) return setError(`单个兴趣不超过 ${INTEREST_CHAR_MAX} 字`);
    if (interests.length >= INTERESTS_MAX && !interests.includes(tag)) return setError(`最多选择 ${INTERESTS_MAX} 个兴趣`);
    setError(null);
    setSaved(null);
    if (!interests.includes(tag)) setInterests((current) => [...current, tag]);
    setCustomInterest("");
  };

  const save = async (payload?: { goal: string; interests: string[]; minutes: number | null; style: string | null; verbosity: string | null }) => {
    const next = payload ?? { goal, interests, minutes, style, verbosity };
    setError(null);
    setSaved(null);
    setSaving(true);
    try {
      const data = await apiJson<Preferences>("/api/preferences", {
        method: "PUT",
        body: {
          explanation_style: next.style,
          verbosity: next.verbosity,
          interests: next.interests,
          daily_minutes: next.minutes,
          goal: next.goal.trim() || null,
        },
      });
      setUpdatedAt(data.updated_at);
      setSaved(payload ? "已清空全部学习偏好" : "学习偏好已保存，将在下一次学习中生效");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "保存失败，请稍后重试");
    } finally {
      setSaving(false);
    }
  };

  const clearAll = async () => {
    setGoal("");
    setInterests([]);
    setMinutes(null);
    setStyle(null);
    setVerbosity(null);
    await save({ goal: "", interests: [], minutes: null, style: null, verbosity: null });
  };

  const deleteInsight = async (id: string) => {
    setDeletingId(id);
    setError(null);
    try {
      await apiJson<void>(`/api/profile/insights/${id}`, { method: "DELETE" });
      setInsights((current) => (current ?? []).filter((item) => item.id !== id));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "删除失败，请重试");
    } finally {
      setDeletingId(null);
    }
  };

  const roleLabel = user?.role === "admin" ? "管理员" : user?.role === "teacher" ? "教师" : "学习者";
  const initial = (user?.email?.[0] ?? "学").toUpperCase();
  const dateLabel = (value?: string | null) => value ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric" }).format(new Date(value)) : "—";

  return (
    <div className="mx-auto w-full max-w-[1100px] px-5 py-8 sm:px-8 lg:px-10 lg:py-10">
      <header>
        <h1 className="text-[28px] font-semibold tracking-[-0.04em] text-ink">个人中心</h1>
        <p className="mt-2 text-sm text-ink-secondary">管理账号与个人设置。学习画像和掌握度仍归在“成长”页面。</p>
      </header>

      <div className="mt-10 grid gap-9 lg:grid-cols-[220px_minmax(0,760px)] lg:gap-14">
        <nav aria-label="设置分类" className="flex gap-1 overflow-x-auto border-b border-line pb-3 lg:block lg:border-b-0 lg:pb-0">
          {SECTIONS.map((item) => (
            <button key={item.id} type="button" onClick={() => { setSection(item.id); setError(null); setSaved(null); }} className={`relative shrink-0 rounded-control px-3 py-2 text-left text-xs transition-colors lg:flex lg:h-11 lg:w-full lg:items-center lg:rounded-none lg:px-4 ${section === item.id ? "bg-surface-muted font-medium text-ink" : "text-ink-secondary hover:bg-surface-muted hover:text-ink"}`}>
              {section === item.id && <span className="absolute bottom-[-13px] left-3 right-3 h-[2px] bg-brand-600 lg:bottom-auto lg:left-0 lg:right-auto lg:h-7 lg:w-[3px]" aria-hidden />}
              <span>{item.label}</span>
              {item.id === "memory" && insights && insights.length > 0 && <span className="ml-auto rounded-full bg-white px-2 py-0.5 text-[10px] text-ink-muted">{insights.length}</span>}
            </button>
          ))}
        </nav>

        <main className="min-w-0">
          {error && <InlineNotice tone="error">{error}</InlineNotice>}
          {saved && <InlineNotice>{saved}</InlineNotice>}

          {section === "profile" && (
            <section>
              <SettingsHeader title="个人资料" description="这些资料用于账号识别，不参与学习者画像推断。" />
              <div className="flex items-center gap-5 border-b border-line py-8">
                <span className="flex h-16 w-16 shrink-0 items-center justify-center rounded-full bg-ink text-xl font-semibold text-white">{initial}</span>
                <div className="min-w-0">
                  <p className="truncate text-base font-semibold text-ink">{user?.email?.split("@")[0] || "学习者"}</p>
                  <p className="mt-1 truncate text-xs text-ink-secondary">{user?.email}</p>
                </div>
              </div>
              <SettingRow label="账号邮箱" value={user?.email ?? "—"} note="当前版本使用邮箱作为唯一登录标识。" />
              <SettingRow label="账号角色" value={roleLabel} note="角色决定可访问的教学和运营功能。" />
              <SettingRow label="学习画像" value="单独管理" note="知识掌握度、证据强度与最近变化不会混入账户资料。" action={<Link href="/profile" className="text-xs font-medium text-brand-600 hover:text-brand-700">前往成长 →</Link>} />
            </section>
          )}

          {section === "preferences" && (
            <section>
              <SettingsHeader title="学习偏好" description={updatedAt ? `上次更新：${dateLabel(updatedAt)}。保存后下一次对话、出题和辅导即生效。` : "尚未设置时，AI 助教会采用中性默认方式。"} />
              {loading ? <div className="mt-7 h-56 animate-pulse rounded-overlay bg-line/60" /> : (
                <div className="space-y-8 pt-7">
                  <div>
                    <label htmlFor="goal" className="text-xs font-medium text-ink">学习目标</label>
                    <input id="goal" value={goal} maxLength={GOAL_MAX} onChange={(event) => { setGoal(event.target.value); setSaved(null); }} placeholder="如：高数期末冲 90 分" className="mt-3 w-full rounded-control border border-line-strong bg-white px-3.5 py-2.5 text-sm text-ink outline-none placeholder:text-ink-muted focus:border-brand-600" />
                    <p className="mt-2 text-[11px] text-ink-muted">一句话即可，学习内容会围绕目标调整重点。</p>
                  </div>
                  <div>
                    <div className="flex items-center justify-between"><p className="text-xs font-medium text-ink">兴趣情境</p><span className="text-[10px] text-ink-muted">{interests.length}/{INTERESTS_MAX}</span></div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {PRESET_INTERESTS.map((tag) => <Chip key={tag} active={interests.includes(tag)} onClick={() => toggleInterest(tag)}>{tag}</Chip>)}
                      {interests.filter((tag) => !PRESET_INTERESTS.includes(tag)).map((tag) => <Chip key={tag} active title="点击移除" onClick={() => toggleInterest(tag)}>{tag} ×</Chip>)}
                    </div>
                    <div className="mt-3 flex gap-2">
                      <input value={customInterest} maxLength={INTEREST_CHAR_MAX} onChange={(event) => setCustomInterest(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addCustomInterest(); } }} placeholder="添加自己的兴趣" className="min-w-0 flex-1 rounded-control border border-line-strong px-3 py-2 text-xs text-ink outline-none placeholder:text-ink-muted focus:border-brand-600" />
                      <button type="button" onClick={addCustomInterest} className="rounded-control border border-line-strong px-4 text-xs font-medium text-ink-secondary hover:border-ink-muted hover:text-ink">添加</button>
                    </div>
                  </div>
                  <div>
                    <p className="text-xs font-medium text-ink">每日可用时长</p>
                    <div className="mt-3 flex flex-wrap gap-2">{MINUTES_OPTIONS.map((value) => <Chip key={value} active={minutes === value} onClick={() => { setMinutes(minutes === value ? null : value); setSaved(null); }}>{value} 分钟</Chip>)}</div>
                  </div>
                  <div>
                    <p className="text-xs font-medium text-ink">讲解风格</p>
                    <div className="mt-3 flex flex-wrap gap-2">{STYLE_OPTIONS.map((option) => <Chip key={option.value} active={style === option.value} title={option.desc} onClick={() => { setStyle(style === option.value ? null : option.value); setSaved(null); }}>{option.label}</Chip>)}</div>
                  </div>
                  <div>
                    <p className="text-xs font-medium text-ink">详略程度</p>
                    <div className="mt-3 flex flex-wrap gap-2">{VERBOSITY_OPTIONS.map((option) => <Chip key={option.value} active={verbosity === option.value} title={option.desc} onClick={() => { setVerbosity(verbosity === option.value ? null : option.value); setSaved(null); }}>{option.label}</Chip>)}</div>
                  </div>
                  <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-6">
                    <button type="button" disabled={saving} onClick={clearAll} className="text-xs font-medium text-danger-600 disabled:opacity-50">清空全部偏好</button>
                    <button type="button" disabled={saving} onClick={() => save()} className="rounded-control bg-ink px-5 py-2.5 text-xs font-medium text-white hover:bg-black disabled:opacity-50">{saving ? "保存中…" : "保存学习偏好"}</button>
                  </div>
                </div>
              )}
            </section>
          )}

          {section === "memory" && (
            <section>
              <SettingsHeader title="AI 记住的你" description="仅展示从学习对话和辅导中提取的长期洞察；你可以逐条删除。" />
              {insights === null ? <div className="mt-7 h-40 animate-pulse rounded-overlay bg-line/60" /> : insights.length === 0 ? <p className="border-b border-line py-10 text-sm text-ink-muted">目前没有长期记忆条目。</p> : (
                <div className="divide-y divide-line">{insights.map((item) => (
                  <article key={item.id} className="grid gap-3 py-5 sm:grid-cols-[minmax(0,1fr)_auto]">
                    <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className="text-xs font-medium text-ink">{item.kc_name || "学习洞察"}</span><span className="rounded-full bg-surface-muted px-2 py-0.5 text-[10px] text-ink-muted">{item.kind}</span></div><p className="mt-2 text-[13px] leading-6 text-ink-secondary">{item.text}</p><p className="mt-2 text-[10px] text-ink-muted">{dateLabel(item.created_at)} · {item.source === "tutor_summary" ? "辅导小结" : "对话洞察"}</p></div>
                    <button type="button" disabled={deletingId === item.id} onClick={() => deleteInsight(item.id)} className="self-start text-xs font-medium text-danger-600 disabled:opacity-50">{deletingId === item.id ? "删除中…" : "删除"}</button>
                  </article>
                ))}</div>
              )}
            </section>
          )}

          {section === "privacy" && (
            <section>
              <SettingsHeader title="隐私与数据" description="查看不同数据如何参与个性化。未接入的能力不会伪装成可操作开关。" />
              <SettingRow label="账户资料" value="仅用于认证与权限" note="邮箱和角色不会作为学习能力证据。" />
              <SettingRow label="学习证据" value="用于掌握度更新" note="练习、复习和辅导证据共同形成成长页画像，并保留来源。" action={<Link href="/profile" className="text-xs font-medium text-brand-600">查看画像</Link>} />
              <SettingRow label="长期记忆" value={`${insights?.length ?? 0} 条`} note="你可以在“AI 记住的你”中逐条查看和删除。" action={<button type="button" onClick={() => setSection("memory")} className="text-xs font-medium text-brand-600">管理</button>} />
            </section>
          )}

          {section === "security" && (
            <section>
              <SettingsHeader title="账户与安全" description="管理登录凭据和当前会话。" />
              <SettingRow label="登录邮箱" value={user?.email ?? "—"} />
              <SettingRow label="创建时间" value={dateLabel(user?.created_at)} />
              <SettingRow label="密码" value="已设置" note="重置密码会通过已验证邮箱完成。" action={<Link href="/forgot" className="text-xs font-medium text-brand-600">重置密码</Link>} />
              <SettingRow label="钱包与额度" value="单独管理" note="查看当前余额、套餐与消费记录。" action={<Link href="/billing" className="text-xs font-medium text-brand-600">前往钱包 →</Link>} />
              <div className="pt-8"><button type="button" onClick={async () => { await logout(); router.replace("/login"); }} className="rounded-control border border-danger-500 px-4 py-2 text-xs font-medium text-danger-600 hover:bg-danger-50">退出当前账号</button></div>
            </section>
          )}

          {section === "org" && (
            <section>
              <SettingsHeader title="机构与权限" description="机构成员关系与账号角色分开管理。" />
              <SettingRow label="当前角色" value={roleLabel} note="教师和管理员会看到额外的教学或运营入口。" />
              <SettingRow label="机构成员关系" value="在机构页面管理" note="加入机构需要机构码；普通学习者也可以使用此入口。" action={<Link href="/orgs" className="text-xs font-medium text-brand-600">前往机构 →</Link>} />
            </section>
          )}
        </main>
      </div>
    </div>
  );
}
