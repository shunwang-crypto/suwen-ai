"use client";

// 设置页（P2-3）：编辑与 Onboarding 同一组学习偏好（目标/兴趣/时长/讲解偏好），
// 支持清空。保存走 PUT /api/preferences（后端保存后立即失效画像摘要缓存，
// 下一次问答/出题/辅导的画像段即体现新偏好）。
// 讲解偏好是内容组织维度（严谨/口语/类比优先），不给学习者贴任何感官类型标签（该类分类已被证伪，全仓禁用）。

import { useEffect, useState, type ReactNode } from "react";

import {
  Card,
  ErrorBanner,
  PageHeader,
  SkeletonPage,
  fmtDateTime,
} from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";

// 局部类型（P1 先例：不动 apps/web/lib/types.ts）
interface Preferences {
  explanation_style: string | null;
  verbosity: string | null;
  interests: string[];
  daily_minutes: number | null;
  goal: string | null;
  updated_at: string | null;
}

const STYLE_OPTIONS = [
  { value: "rigorous", label: "严谨", desc: "定义定理优先，推导严密" },
  { value: "colloquial", label: "口语", desc: "大白话讲解，轻松易懂" },
  { value: "analogy", label: "类比优先", desc: "先打比方，再进正题" },
];

const VERBOSITY_OPTIONS = [
  { value: "concise", label: "简洁", desc: "要点式，点到为止" },
  { value: "detailed", label: "详尽", desc: "步骤完整，例子充分" },
];

const MINUTES_OPTIONS = [15, 30, 45, 60, 90];

const PRESET_INTERESTS = [
  "篮球",
  "足球",
  "动漫",
  "游戏",
  "音乐",
  "科幻",
  "编程",
  "历史",
  "美食",
  "旅行",
];

const INTERESTS_MAX = 8; // 与后端 routers/preferences.py 同限
const INTEREST_CHAR_MAX = 20;
const GOAL_MAX = 200;

function Chip({
  active,
  onClick,
  children,
  title,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
  title?: string;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={`rounded-full border px-3.5 py-1.5 text-sm font-medium transition-colors ${
        active
          ? "border-indigo-500 bg-indigo-50 text-indigo-700"
          : "border-slate-300 bg-white text-slate-600 hover:border-indigo-300 hover:text-indigo-600"
      }`}
    >
      {children}
    </button>
  );
}

export default function SettingsPage() {
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

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await apiJson<Preferences>("/api/preferences");
        if (!alive) return;
        setGoal(data.goal ?? "");
        setInterests(data.interests ?? []);
        setMinutes(data.daily_minutes);
        setStyle(data.explanation_style);
        setVerbosity(data.verbosity);
        setUpdatedAt(data.updated_at);
      } catch (err) {
        if (alive)
          setError(
            err instanceof ApiError ? err.message : "加载偏好失败，请刷新重试",
          );
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const toggleInterest = (tag: string) => {
    setSaved(null);
    setInterests((prev) =>
      prev.includes(tag)
        ? prev.filter((t) => t !== tag)
        : prev.length >= INTERESTS_MAX
          ? prev
          : [...prev, tag],
    );
  };

  const addCustomInterest = () => {
    const tag = customInterest.replace(/\s+/g, " ").trim();
    if (!tag) return;
    if (tag.length > INTEREST_CHAR_MAX) {
      setError(`单个兴趣不超过 ${INTEREST_CHAR_MAX} 字`);
      return;
    }
    if (interests.length >= INTERESTS_MAX && !interests.includes(tag)) {
      setError(`最多选择 ${INTERESTS_MAX} 个兴趣`);
      return;
    }
    setError(null);
    setSaved(null);
    if (!interests.includes(tag)) setInterests((prev) => [...prev, tag]);
    setCustomInterest("");
  };

  const save = async (payload?: {
    goal: string;
    interests: string[];
    minutes: number | null;
    style: string | null;
    verbosity: string | null;
  }) => {
    const p = payload ?? { goal, interests, minutes, style, verbosity };
    setError(null);
    setSaved(null);
    setSaving(true);
    try {
      const data = await apiJson<Preferences>("/api/preferences", {
        method: "PUT",
        body: {
          explanation_style: p.style,
          verbosity: p.verbosity,
          interests: p.interests,
          daily_minutes: p.minutes,
          goal: p.goal.trim() || null,
        },
      });
      setUpdatedAt(data.updated_at);
      setSaved(payload ? "已清空全部偏好" : "偏好已保存，下次对话即生效");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "保存失败，请稍后重试");
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

  if (loading) return <SkeletonPage />;

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4 md:p-6">
      <PageHeader
        title="设置"
        subtitle={
          updatedAt
            ? `学习偏好 · 上次更新 ${fmtDateTime(updatedAt)}`
            : "学习偏好 · 尚未设置，AI 助教会按默认方式讲解"
        }
      />

      {error && <ErrorBanner message={error} onClose={() => setError(null)} />}
      {saved && (
        <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-700">
          {saved}
        </div>
      )}

      <Card title="学习目标">
        <input
          value={goal}
          maxLength={GOAL_MAX}
          onChange={(e) => {
            setGoal(e.target.value);
            setSaved(null);
          }}
          placeholder="如：高数期末冲 90 分"
          className="w-full rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm text-slate-900 outline-none transition-colors placeholder:text-slate-400 focus:border-indigo-500 focus:ring-4 focus:ring-indigo-500/10"
        />
        <p className="mt-2 text-xs text-slate-400">
          一句话即可，问答与出题都会围绕它调整重点。
        </p>
      </Card>

      <Card
        title="兴趣"
        extra={
          <span className="text-xs text-slate-400">
            {interests.length}/{INTERESTS_MAX}
          </span>
        }
      >
        <div className="flex flex-wrap gap-2">
          {PRESET_INTERESTS.map((tag) => (
            <Chip
              key={tag}
              active={interests.includes(tag)}
              onClick={() => toggleInterest(tag)}
            >
              {tag}
            </Chip>
          ))}
          {interests
            .filter((t) => !PRESET_INTERESTS.includes(t))
            .map((tag) => (
              <Chip key={tag} active title="点击移除" onClick={() => toggleInterest(tag)}>
                {tag} ×
              </Chip>
            ))}
        </div>
        <div className="mt-3 flex gap-2">
          <input
            value={customInterest}
            maxLength={INTEREST_CHAR_MAX}
            onChange={(e) => setCustomInterest(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addCustomInterest();
              }
            }}
            placeholder="自己填一个，如：天文"
            className="min-w-0 flex-1 rounded-xl border border-slate-300 px-3.5 py-2 text-sm text-slate-900 outline-none transition-colors placeholder:text-slate-400 focus:border-indigo-500 focus:ring-4 focus:ring-indigo-500/10"
          />
          <button
            type="button"
            onClick={addCustomInterest}
            className="shrink-0 rounded-xl border border-slate-300 px-4 text-sm font-medium text-slate-600 transition-colors hover:border-indigo-300 hover:text-indigo-600"
          >
            添加
          </button>
        </div>
        <p className="mt-2 text-xs text-slate-400">
          例题与讲解会自然贴合你的兴趣来举例，不会改变知识点本身。
        </p>
      </Card>

      <Card title="学习习惯">
        <div className="space-y-5">
          <div>
            <p className="mb-2 text-xs font-medium text-slate-500">每日可用时长</p>
            <div className="flex flex-wrap gap-2">
              {MINUTES_OPTIONS.map((m) => (
                <Chip
                  key={m}
                  active={minutes === m}
                  onClick={() => {
                    setMinutes(minutes === m ? null : m);
                    setSaved(null);
                  }}
                >
                  {m} 分钟
                </Chip>
              ))}
            </div>
          </div>
          <div>
            <p className="mb-2 text-xs font-medium text-slate-500">讲解风格</p>
            <div className="flex flex-wrap gap-2">
              {STYLE_OPTIONS.map((o) => (
                <Chip
                  key={o.value}
                  title={o.desc}
                  active={style === o.value}
                  onClick={() => {
                    setStyle(style === o.value ? null : o.value);
                    setSaved(null);
                  }}
                >
                  {o.label}
                </Chip>
              ))}
            </div>
          </div>
          <div>
            <p className="mb-2 text-xs font-medium text-slate-500">详略程度</p>
            <div className="flex flex-wrap gap-2">
              {VERBOSITY_OPTIONS.map((o) => (
                <Chip
                  key={o.value}
                  title={o.desc}
                  active={verbosity === o.value}
                  onClick={() => {
                    setVerbosity(verbosity === o.value ? null : o.value);
                    setSaved(null);
                  }}
                >
                  {o.label}
                </Chip>
              ))}
            </div>
          </div>
        </div>
      </Card>

      <div className="flex items-center justify-between pb-6">
        <button
          type="button"
          onClick={clearAll}
          disabled={saving}
          className="rounded-xl border border-slate-300 px-4 py-2 text-sm font-medium text-slate-500 transition-colors hover:border-red-300 hover:text-red-600 disabled:opacity-60"
        >
          清空全部偏好
        </button>
        <button
          type="button"
          onClick={() => save()}
          disabled={saving}
          className="rounded-xl bg-indigo-600 px-6 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {saving ? "保存中…" : "保存"}
        </button>
      </div>
    </div>
  );
}
