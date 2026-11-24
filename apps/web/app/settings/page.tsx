"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { MemoryCorrectionForm } from "@/components/settings/memory-correction-form";

import { useAuth } from "@/lib/auth-context";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type { InsightItem } from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

interface Preferences {
  explanation_style: string | null;
  verbosity: string | null;
  interests: string[];
  daily_minutes: number | null;
  goal: string | null;
  personalization_enabled: boolean;
  updated_at: string | null;
}

interface MemoryPreview {
  summary: string;
  weak_points: string[];
  mastered: string[];
}

interface InsightsResponse {
  insights: InsightItem[];
}

interface ModelCredential {
  configured: boolean;
  server_enabled: boolean;
  provider: "deepseek";
  enabled: boolean;
  fallback_to_platform: boolean;
  key_last_four: string | null;
  normal_model: string | null;
  reasoning_model: string | null;
  last_validated_at: string | null;
  last_validation_status: string | null;
  allowed_models: string[];
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
const formatSettingsDate = (value?: string | null) => value ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric" }).format(new Date(value)) : "—";

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
        {note && <p className="mt-1 text-body-sm leading-5 text-ink-muted">{note}</p>}
      </div>
      {action && <div className="justify-self-start sm:justify-self-end">{action}</div>}
    </div>
  );
}

function InlineNotice({ children, tone = "success" }: { children: ReactNode; tone?: "success" | "error" }) {
  return <div className={`mb-5 border-l-2 px-3 py-2 text-xs ${tone === "error" ? "border-danger-500 bg-danger-50 text-danger-700" : "border-success-500 bg-success-50 text-success-700"}`}>{children}</div>;
}

function ModelCredentialSection() {
  const credentialRes = useApiResource<ModelCredential>("/api/settings/model-credential", {
    errorMessage: "模型服务配置加载失败",
  });
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [normalModel, setNormalModel] = useState("deepseek-chat");
  const [reasoningModel, setReasoningModel] = useState("deepseek-reasoner");
  const [fallback, setFallback] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const deleteButtonRef = useRef<HTMLButtonElement>(null);

  const credential = credentialRes.data;

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 本地表单或横幅的单向同步, 无纯派生替代 */
  // 到达后回填表单 (原加载 effect 的联动部分; setData 更新时同样重放, 值与表单一致无感)
  useEffect(() => {
    const data = credentialRes.data;
    if (!data) return;
    if (data.normal_model) setNormalModel(data.normal_model);
    if (data.reasoning_model) setReasoningModel(data.reasoning_model);
    setFallback(data.fallback_to_platform);
  }, [credentialRes.data]);
  /* eslint-enable react-hooks/set-state-in-effect */

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 本地表单或横幅的单向同步, 无纯派生替代 */
  useEffect(() => {
    if (credentialRes.error) setError(credentialRes.error);
  }, [credentialRes.error]);
  /* eslint-enable react-hooks/set-state-in-effect */

  useEffect(() => {
    if (!confirmDelete) return;
    deleteButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setConfirmDelete(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [confirmDelete]);

  const save = async () => {
    if (!apiKey.trim()) {
      setError("请输入 DeepSeek API Key");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const data = await apiJson<ModelCredential>("/api/settings/model-credential", {
        method: "PUT",
        body: { provider: "deepseek", api_key: apiKey, normal_model: normalModel, reasoning_model: reasoningModel, fallback_to_platform: fallback },
      });
      credentialRes.setData(data);
      setApiKey("");
      setShowKey(false);
      setNotice("DeepSeek 配置已验证并保存");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "保存失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const patchCredential = async (body: { enabled?: boolean; fallback_to_platform?: boolean }) => {
    setBusy(true);
    setError(null);
    try {
      const data = await apiJson<ModelCredential>("/api/settings/model-credential", { method: "PATCH", body });
      credentialRes.setData(data);
      setFallback(data.fallback_to_platform);
      setNotice(data.enabled ? "已恢复使用自带 Key" : "已暂停使用自带 Key");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "更新失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    setError(null);
    try {
      await apiJson<void>("/api/settings/model-credential", { method: "DELETE" });
      credentialRes.setData(null);
      setApiKey("");
      setConfirmDelete(false);
      setNotice("已删除自带模型配置");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "删除失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const enabled = credential?.enabled === true;
  const models = credential?.allowed_models?.length ? credential.allowed_models : ["deepseek-chat", "deepseek-reasoner"];
  return (
    <div className="mt-8 border-t border-line pt-8">
      <SettingsHeader title="我的模型服务" description="启用后，普通对话、深度思考、辅导、出题、批改、学习计划和报告等文本生成使用你的 DeepSeek 账户。图片理解、知识库检索、重排、网页搜索和存储仍由苏问提供。" />
      {error && <InlineNotice tone="error">{error}</InlineNotice>}
      {notice && <InlineNotice>{notice}</InlineNotice>}
      {credential?.server_enabled === false ? (
        <p className="mt-5 text-xs text-ink-muted">当前环境暂未开放自带模型服务。</p>
      ) : (
        <div className="mt-5 space-y-5">
          <div className="flex flex-wrap items-center justify-between gap-3 text-xs">
            <span className="text-ink-secondary">Provider：DeepSeek</span>
            <span className={`rounded-full px-2.5 py-1 ${enabled ? "bg-success-50 text-success-700" : credential?.configured ? "bg-surface-muted text-ink-secondary" : "bg-surface-muted text-ink-muted"}`}>
              {!credential?.configured ? "未配置" : enabled ? "已启用" : "已暂停"}
            </span>
          </div>
          {credential?.configured && <p className="text-xs text-ink-secondary">当前 Key：••••••••{credential.key_last_four} · 最近验证：{formatSettingsDate(credential.last_validated_at)}</p>}
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="text-xs text-ink-secondary">普通模型
              <select value={normalModel} onChange={(event) => setNormalModel(event.target.value)} className="mt-2 w-full rounded-control border border-line-strong bg-white px-3 py-2 text-sm text-ink">
                {models.map((model) => <option key={model}>{model}</option>)}
              </select>
            </label>
            <label className="text-xs text-ink-secondary">深度思考模型
              <select value={reasoningModel} onChange={(event) => setReasoningModel(event.target.value)} className="mt-2 w-full rounded-control border border-line-strong bg-white px-3 py-2 text-sm text-ink">
                {models.map((model) => <option key={model}>{model}</option>)}
              </select>
            </label>
          </div>
          <label className="block text-xs text-ink-secondary">API Key
            <div className="mt-2 flex gap-2">
              <input type={showKey ? "text" : "password"} value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="new-password" maxLength={260} placeholder={credential?.configured ? "输入新 Key 以替换" : "sk-…"} className="min-w-0 flex-1 rounded-control border border-line-strong px-3 py-2 text-sm text-ink" />
              <button type="button" onClick={() => setShowKey((value) => !value)} className="rounded-control border border-line-strong px-3 text-xs text-ink-secondary">{showKey ? "隐藏" : "显示"}</button>
            </div>
          </label>
          <label className="flex items-start gap-2 text-xs leading-5 text-ink-secondary">
            <input type="checkbox" checked={fallback} onChange={(event) => { setFallback(event.target.checked); if (credential?.configured) void patchCredential({ fallback_to_platform: event.target.checked }); }} className="mt-1" />
            <span>自己的 API 不可用时，允许使用苏问额度继续（发生回退时会按平台服务计费并提示）。</span>
          </label>
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={busy || !apiKey.trim()} onClick={() => void save()} className="rounded-control bg-ink px-4 py-2 text-xs font-medium text-white disabled:opacity-40">{busy ? "处理中…" : credential?.configured ? "测试连接并替换" : "测试连接并保存"}</button>
            {credential?.configured && <>
              <button type="button" disabled={busy} onClick={() => void patchCredential({ enabled: !enabled })} className="rounded-control border border-line-strong px-4 py-2 text-xs text-ink-secondary disabled:opacity-40">{enabled ? "暂停使用" : "恢复使用"}</button>
              <button type="button" disabled={busy} onClick={() => setConfirmDelete(true)} className="rounded-control border border-danger-500 px-4 py-2 text-xs text-danger-600 disabled:opacity-40">删除配置</button>
            </>}
          </div>
          {confirmDelete && <div role="dialog" aria-modal="true" aria-labelledby="delete-model-credential-title" className="rounded-overlay border border-danger-200 bg-danger-50 p-4 text-xs text-danger-700">
            <p id="delete-model-credential-title" className="font-medium">删除自带模型配置？</p>
            <p className="mt-1">删除后文本生成恢复使用苏问平台模型和额度，已保存的 Key 不可恢复。</p>
            <div className="mt-3 flex gap-2"><button ref={deleteButtonRef} type="button" onClick={() => void remove()} className="rounded-control bg-danger-600 px-3 py-1.5 text-white">确认删除</button><button type="button" onClick={() => setConfirmDelete(false)} className="rounded-control border border-danger-300 px-3 py-1.5">取消</button></div>
          </div>}
          <p className="text-body-sm leading-5 text-ink-muted">安全说明：Key 只在服务端加密保存，页面不会写入 URL、localStorage 或 sessionStorage；管理员只能看到 provider、状态和后四位。</p>
        </div>
      )}
    </div>
  );
}

export default function SettingsPage() {
  const router = useRouter();
  const { user, logout, refreshMe } = useAuth();
  const search = useSyncExternalStore(emptySubscribe, () => window.location.search, () => "");
  const requestedSection = new URLSearchParams(search).get("section");
  const querySection = SECTIONS.some((item) => item.id === requestedSection)
    ? requestedSection as SettingsSection
    : "profile";
  const [sectionOverride, setSection] = useState<SettingsSection | null>(null);
  const section = sectionOverride ?? querySection;
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
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [memoryEnabled, setMemoryEnabled] = useState(true);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [displayName, setDisplayName] = useState(() => user?.display_name ?? "");
  const [savingProfile, setSavingProfile] = useState(false);

  const prefsRes = useApiResource<Preferences>("/api/preferences", {
    errorMessage: "学习偏好加载失败，可稍后重试",
    toErrorMessage: () => "学习偏好加载失败，可稍后重试",
  });
  const insightsRes = useApiResource<InsightsResponse>("/api/profile/insights", {
    errorMessage: "画像洞察加载失败",
  });
  const previewRes = useApiResource<MemoryPreview>(
    prefsRes.data && prefsRes.data.personalization_enabled !== false
      ? "/api/profile/memory/preview"
      : null,
  );
  // 原语义: 洞察失败呈现空态 (非骨架), 仅加载中为 null
  const insights = insightsRes.data
    ? insightsRes.data.insights
    : prefsRes.loading || insightsRes.loading
      ? null
      : [];
  const preview = previewRes.data;
  const loading = prefsRes.loading || insightsRes.loading;

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 本地表单或横幅的单向同步, 无纯派生替代 */
  // 偏好到达后回填表单 (原加载 effect 的联动部分)
  useEffect(() => {
    const data = prefsRes.data;
    if (!data) return;
    setGoal(data.goal ?? "");
    setInterests(data.interests ?? []);
    setMinutes(data.daily_minutes);
    setStyle(data.explanation_style);
    setVerbosity(data.verbosity);
    setUpdatedAt(data.updated_at);
    setMemoryEnabled(data.personalization_enabled !== false);
  }, [prefsRes.data]);
  /* eslint-enable react-hooks/set-state-in-effect */

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 本地表单或横幅的单向同步, 无纯派生替代 */
  useEffect(() => {
    if (prefsRes.error) setError(prefsRes.error);
  }, [prefsRes.error]);
  /* eslint-enable react-hooks/set-state-in-effect */

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
      insightsRes.setData((current) =>
        current ? { insights: current.insights.filter((item) => item.id !== id) } : current,
      );
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "删除失败，请重试");
    } finally {
      setDeletingId(null);
    }
  };

  const toggleMemory = async (enabled: boolean) => {
    setMemoryBusy(true);
    setError(null);
    setSaved(null);
    try {
      await apiJson("/api/preferences/personalization", { method: "POST", body: { enabled } });
      setMemoryEnabled(enabled);
      setSaved(enabled ? "记忆已开启：讲解与练习将继续参考你的学情画像" : "记忆已关闭：讲解和出题不再参考你的画像；历史与错题照常保存");
      if (!enabled) {
        previewRes.setData(null);
      } else {
        previewRes.reload();
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "切换失败，请重试");
    } finally {
      setMemoryBusy(false);
    }
  };

  const toggleSuppress = async (item: InsightItem) => {
    setDeletingId(item.id);
    setError(null);
    try {
      await apiJson(`/api/profile/insights/${item.id}`, {
        method: "PATCH",
        body: { suppressed: !item.suppressed, expected_revision: item.revision },
      });
      insightsRes.setData((current) =>
        current
          ? { insights: current.insights.map((row) => (row.id === item.id ? { ...row, suppressed: !row.suppressed } : row)) }
          : current,
      );
      setSaved(item.suppressed ? "已恢复该条记忆的引用" : "已暂停引用该条记忆（原文保留，可随时恢复）");
      insightsRes.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "操作失败，请重试");
    } finally {
      setDeletingId(null);
    }
  };

  const updateMemory = async (item: InsightItem, patch: { text?: string; kind?: string; status?: string }) => {
    setDeletingId(item.id);
    setError(null);
    try {
      await apiJson(`/api/profile/insights/${item.id}`, {
        method: "PATCH", body: { ...patch, expected_revision: item.revision },
      });
      setEditingId(null);
      insightsRes.reload();
      setSaved(patch.text ? "已保存你的纠正，后续讲解会优先参考" : patch.status === "obsolete" ? "已标记过时，后续不再引用" : "已恢复这条记忆");
    } catch (caught) {
      setError(caught instanceof ApiError && caught.status === 409 ? "这条记忆已更新，请刷新后再修改" : "保存失败，请重试");
      insightsRes.reload();
    } finally {
      setDeletingId(null);
    }
  };

  const acceptSuggestion = async (item: InsightItem) => {
    // 建议文本由后端确定性生成（"建议尝试「严谨」讲解"），据此映射回风格枚举；
    // 解析失败则退化为引导到偏好区手动确认 — 推断永不直接改偏好
    const labelMatch = item.text.match(/建议尝试「([^」]+)」/);
    const target = STYLE_OPTIONS.find((option) => option.label === labelMatch?.[1]);
    if (!target) {
      setSection("preferences");
      setSaved("请在「学习偏好」中调整讲解风格");
      return;
    }
    setDeletingId(item.id);
    setError(null);
    try {
      await apiJson<Preferences>("/api/preferences", {
        method: "PUT",
        body: {
          explanation_style: target.value,
          verbosity,
          interests,
          daily_minutes: minutes,
          goal: goal.trim() || null,
        },
      });
      setStyle(target.value);
      await apiJson<void>(`/api/profile/insights/${item.id}`, { method: "DELETE" });
      insightsRes.setData((current) =>
        current ? { insights: current.insights.filter((row) => row.id !== item.id) } : current,
      );
      setSaved(`已把讲解风格切换为「${target.label}」，可在学习偏好中随时改回`);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "采纳失败，请手动在学习偏好中调整");
    } finally {
      setDeletingId(null);
    }
  };

  const saveProfile = async () => {
    const normalized = displayName.replace(/\s+/g, " ").trim();
    if (!normalized) {
      setError("姓名或昵称不能为空");
      return;
    }
    setError(null);
    setSaved(null);
    setSavingProfile(true);
    try {
      await apiJson("/api/auth/me", {
        method: "PATCH",
        body: { display_name: normalized },
      });
      await refreshMe();
      setDisplayName(normalized);
      setSaved("个人资料已保存");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "保存失败，请稍后重试");
    } finally {
      setSavingProfile(false);
    }
  };

  const roleLabel = user?.role === "admin" ? "管理员" : user?.role === "teacher" ? "教师" : "学习者";
  const accountName = user?.display_name?.trim() || "同学";
  const initial = accountName[0] ?? "学";
  const dateLabel = formatSettingsDate;

  return (
    <div className="mx-auto w-full max-w-[1100px] px-5 py-8 sm:px-8 lg:px-10 lg:py-10">
      <header>
        <h1 className="text-[28px] font-semibold tracking-[-0.04em] text-ink">账户设置</h1>
        <p className="mt-2 text-sm text-ink-secondary">管理账号与个人设置。学习画像和掌握度仍归在“成长”页面。</p>
      </header>

      <div className="mt-10 grid gap-9 lg:grid-cols-[220px_minmax(0,760px)] lg:gap-14">
        <nav aria-label="设置分类" className="flex gap-1 overflow-x-auto border-b border-line pb-3 lg:block lg:border-b-0 lg:pb-0">
          {SECTIONS.map((item) => (
            <button key={item.id} type="button" onClick={() => { setSection(item.id); setError(null); setSaved(null); }} className={`relative shrink-0 rounded-control px-3 py-2 text-left text-xs transition-colors lg:flex lg:h-11 lg:w-full lg:items-center lg:rounded-none lg:px-4 ${section === item.id ? "bg-surface-muted font-medium text-ink" : "text-ink-secondary hover:bg-surface-muted hover:text-ink"}`}>
              {section === item.id && <span className="absolute bottom-[-13px] left-3 right-3 h-[2px] bg-brand-600 lg:bottom-auto lg:left-0 lg:right-auto lg:h-7 lg:w-[3px]" aria-hidden />}
              <span>{item.label}</span>
              {item.id === "memory" && insights && insights.length > 0 && <span className="ml-auto rounded-full bg-white px-2 py-0.5 text-caption text-ink-muted">{insights.length}</span>}
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
                  <p className="truncate text-base font-semibold text-ink">{accountName}</p>
                  <p className="mt-1 truncate text-xs text-ink-secondary">{user?.email}</p>
                </div>
              </div>
              <div className="border-b border-line py-5">
                <label htmlFor="display-name" className="text-xs text-ink-muted">姓名或昵称</label>
                <div className="mt-2 flex max-w-md gap-2">
                  <input
                    id="display-name"
                    value={displayName}
                    maxLength={80}
                    onChange={(event) => {
                      setDisplayName(event.target.value);
                      setSaved(null);
                    }}
                    className="min-w-0 flex-1 rounded-control border border-line-strong bg-white px-3 py-2 text-[13px] text-ink outline-none focus:border-ink"
                    placeholder="例如：林小溪"
                  />
                  <button
                    type="button"
                    disabled={savingProfile || !displayName.trim()}
                    onClick={() => void saveProfile()}
                    className="rounded-control bg-ink px-4 text-xs font-medium text-white hover:bg-black disabled:opacity-40"
                  >
                    {savingProfile ? "保存中…" : "保存"}
                  </button>
                </div>
              </div>
              <SettingRow label="账号邮箱" value={user?.email ?? "—"} note="当前版本使用邮箱作为唯一登录标识。" />
              <SettingRow label="账号角色" value={roleLabel} note="角色决定可访问的教学和运营功能。" />
              <SettingRow label="学习画像" value="单独管理" note="知识掌握度、证据强度与最近变化不会混入账户资料。" action={<Link href="/growth" className="text-xs font-medium text-brand-600 hover:text-brand-700">前往成长 →</Link>} />
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
                    <p className="mt-2 text-body-sm text-ink-muted">一句话即可，学习内容会围绕目标调整重点。</p>
                  </div>
                  <div>
                    <div className="flex items-center justify-between"><p className="text-xs font-medium text-ink">兴趣情境</p><span className="text-caption text-ink-muted">{interests.length}/{INTERESTS_MAX}</span></div>
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
                  <div className="border-t border-line pt-6">
                    <p className="text-xs font-medium text-ink">重新开始引导</p>
                    <p className="mt-1 text-body-sm text-ink-muted">想重新选择「上传资料」或「目标起步计划」的起点？可随时回到新手引导，不会影响已有数据。</p>
                    <Link href="/onboarding" className="mt-3 inline-flex items-center gap-1 rounded-control border border-line-strong bg-white px-4 py-2 text-xs font-medium text-ink-secondary transition-colors hover:bg-surface-muted">重新查看新手引导 →</Link>
                  </div>
                </div>
              )}
            </section>
          )}

          {section === "memory" && (
            <section>
              <SettingsHeader title="AI 记住的你" description="按当前问题参考历史观察；你可以纠正、暂停引用或删除。删除记忆后，原会话记录仍保留。" />
              <SettingRow
                label="启用记忆"
                value={memoryEnabled ? "开启" : "关闭"}
                note="关闭后，讲解与出题不再参考你的学情画像；对话历史和错题照常保存，随时可重新开启。关闭不会删除任何数据。"
                action={
                  <button
                    type="button"
                    role="switch"
                    aria-checked={memoryEnabled}
                    disabled={memoryBusy}
                    onClick={() => void toggleMemory(!memoryEnabled)}
                    className={`relative h-6 w-11 rounded-full transition-colors disabled:opacity-50 ${memoryEnabled ? "bg-brand-600" : "bg-line-strong"}`}
                  >
                    <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${memoryEnabled ? "left-[22px]" : "left-0.5"}`} />
                  </button>
                }
              />
              {memoryEnabled && (
                <div className="border-b border-line py-5">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-xs font-medium text-ink">AI 眼中的你（摘要预览）</p>
                    <span className="text-caption text-ink-muted">与问答注入同源 · 约 60 秒刷新</span>
                  </div>
                  {preview === null ? (
                    <p className="mt-2 text-body-sm leading-5 text-ink-muted">暂无画像摘要：还没有足够的学习记录，或摘要生成中。</p>
                  ) : (
                    <div className="mt-2 rounded-control border border-line bg-surface-muted px-3.5 py-3">
                      <p className="text-[13px] leading-6 text-ink-secondary">{preview.summary || "（当前为空摘要）"}</p>
                      {preview.weak_points.length > 0 && (
                        <p className="mt-2 text-caption text-ink-muted">薄弱点关注：{preview.weak_points.join("、")}</p>
                      )}
                    </div>
                  )}
                </div>
              )}
              {(() => {
                const suggestions = (insights ?? []).filter((item) => item.kind === "preference_signal");
                if (suggestions.length === 0) return null;
                return (
                  <div className="border-b border-line py-5">
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-xs font-medium text-ink">AI 的建议</p>
                      <span className="text-caption text-ink-muted">根据你对讲解的评价推断，需你确认后生效</span>
                    </div>
                    <div className="mt-3 space-y-3">
                      {suggestions.map((item) => (
                        <div key={item.id} className="rounded-control border border-brand-100 bg-brand-50/50 px-3.5 py-3">
                          <p className="text-[13px] leading-6 text-ink-secondary">{item.text}</p>
                          <div className="mt-2 flex gap-3">
                            <button type="button" disabled={deletingId === item.id} onClick={() => void acceptSuggestion(item)} className="rounded-control bg-ink px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">{deletingId === item.id ? "处理中…" : "采纳"}</button>
                            <button type="button" disabled={deletingId === item.id} onClick={() => void deleteInsight(item.id)} className="rounded-control border border-line-strong px-3 py-1.5 text-xs text-ink-secondary disabled:opacity-50">忽略</button>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })()}
              {insights === null ? <div className="mt-7 h-40 animate-pulse rounded-overlay bg-line/60" /> : insights.filter((item) => item.kind !== "preference_signal").length === 0 ? <p className="border-b border-line py-10 text-sm text-ink-muted">目前没有长期记忆条目。</p> : (
                <div className="divide-y divide-line">{insights.filter((item) => item.kind !== "preference_signal").map((item) => (
                  <article key={item.id} className={`grid gap-3 py-5 sm:grid-cols-[minmax(0,1fr)_auto] ${item.suppressed ? "opacity-55" : ""}`}>
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-xs font-medium text-ink">{item.kc_name || "学习洞察"}</span>
                        <span className="rounded-full bg-surface-muted px-2 py-0.5 text-caption text-ink-muted">{{active: "按需参考", suppressed: "已暂停引用", obsolete: "已过时", expired: "已到期", historical: "已有新记录"}[item.effective_status] ?? "按需参考"}</span>
                        {item.user_corrected && <span className="text-caption text-brand-600">已由你纠正</span>}
                      </div>
                      {editingId === item.id ? (
                        <MemoryCorrectionForm key={item.id} id={item.id} text={item.text} kind={item.kind}
                          busy={deletingId === item.id} onSave={(patch) => void updateMemory(item, patch)}
                          onCancel={() => setEditingId(null)} />
                      ) : <div className="mt-2 text-[13px] leading-6 text-ink-secondary"><MathText text={item.text} /></div>}
                      <p className="mt-2 text-caption text-ink-muted">{dateLabel(item.observed_at ?? item.created_at)} · {item.source === "tutor_summary" ? "辅导小结（AI 观察）" : "对话中的学生原话"}{item.source_message_id ? " · 已核对原话" : ""}</p>
                      {item.session_id && <Link href={`/chat?session=${item.session_id}`} className="mt-1 inline-block text-xs text-brand-600">查看来源会话</Link>}
                    </div>
                    <div className="flex shrink-0 flex-col items-start gap-2 sm:items-end">
                      <button type="button" disabled={deletingId === item.id} onClick={() => setEditingId(item.id)} className="text-xs font-medium text-brand-600 disabled:opacity-50">纠正</button>
                      <button type="button" disabled={deletingId === item.id} onClick={() => void updateMemory(item, { status: item.status === "obsolete" ? "active" : "obsolete" })} className="text-xs font-medium text-ink-secondary disabled:opacity-50">{item.status === "obsolete" ? "恢复这条记忆" : "已过时"}</button>
                      <button type="button" disabled={deletingId === item.id} onClick={() => void toggleSuppress(item)} className="text-xs font-medium text-ink-secondary hover:text-ink disabled:opacity-50">{item.suppressed ? "恢复引用" : "不要再提及"}</button>
                      <button type="button" disabled={deletingId === item.id} onClick={() => deleteInsight(item.id)} className="text-xs font-medium text-danger-600 disabled:opacity-50">{deletingId === item.id ? "删除中…" : "删除"}</button>
                    </div>
                  </article>
                ))}</div>
              )}
            </section>
          )}

          {section === "privacy" && (
            <section>
              <SettingsHeader title="隐私与数据" description="查看不同数据如何参与个性化。未接入的能力不会伪装成可操作开关。" />
              <SettingRow label="账户资料" value="仅用于认证与权限" note="邮箱和角色不会作为学习能力证据。" />
              <SettingRow label="学习证据" value="用于掌握度更新" note="练习、复习和核验后的真实作答共同形成成长页画像；反讲反馈只保存为表达观察，不会更新掌握度。" action={<Link href="/growth" className="text-xs font-medium text-brand-600">查看成长</Link>} />
              <SettingRow label="长期记忆" value={memoryEnabled ? `${insights?.length ?? 0} 条 · 引用中` : "已暂停引用"} note="你可以在“AI 记住的你”中开关记忆、逐条暂停引用或删除。" action={<button type="button" onClick={() => setSection("memory")} className="text-xs font-medium text-brand-600">管理</button>} />
              <div className="mt-5 rounded-overlay border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-900">免费 AI 通道由第三方提供，请求可能被用于服务改进；不要上传身份证、成绩单姓名页或其他敏感资料。平台不承诺零保留或完全私密。</div>
            </section>
          )}

          {section === "security" && (
            <section>
              <SettingsHeader title="账户与安全" description="管理登录凭据和当前会话。" />
              <SettingRow label="登录邮箱" value={user?.email ?? "—"} />
              <SettingRow label="创建时间" value={dateLabel(user?.created_at)} />
              <SettingRow label="密码" value="已设置" note="重置密码会通过已验证邮箱完成。" action={<Link href="/forgot" className="text-xs font-medium text-brand-600">重置密码</Link>} />
              <SettingRow label="苏问 AI 额度" value="单独管理" note="查看当前额度、套餐与消费记录。" action={<Link href="/billing" className="text-xs font-medium text-brand-600">查看额度 →</Link>} />
              <div className="pt-8"><button type="button" onClick={async () => { await logout(); router.replace("/login"); }} className="rounded-control border border-danger-500 px-4 py-2 text-xs font-medium text-danger-600 hover:bg-danger-50">退出当前账号</button></div>
              <ModelCredentialSection />
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
