"use client";

// AI 眼中的我（F8）：真实规模（3 库 × 152 KC）适配版。
// - 掌握度雷达：components/mastery-radar.tsx —— 库 tab 切换 + 章节聚合轴 + 少轴条形降级 + 轴下钻
//  （缺少 kb_id/kb_name/subject/chapter 时降级回 top12 行为）。
// - 掌握度明细：「科目 → 章节」两级折叠树（组头=章均掌握度+薄弱计数，默认只展开含薄弱的章），
//   名称/别名搜索 + 「只看薄弱」「只看到期」过滤 chip，行高压缩，动作收进桌面 hover /「…」菜单，
//   窄屏两行卡片布局，遗忘快照缩成「3 天后 N%」徽章（P2-7 全文进 tooltip）。
// - 最近掌握度变化：默认 10 条 +「展开更多」；非 1.0 权重徽章收进 hover tooltip；
//   零变化行后端已滤，前端再兜底一次。
// - 「AI 记住的你」记忆条目可见可删（P2-1）。

import { useEffect, useState } from "react";

import Link from "next/link";

import {
  Card,
  EmptyState,
  ErrorBanner,
  SkeletonList,
  SkeletonPage,
  Spinner,
  fmtDateTime,
  fmtPct,
} from "@/components/common";
import { MasteryRadarPanel } from "@/components/mastery-radar";
import { ProgressRing, masteryColor } from "@/components/ui/progress-ring";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type { InsightItem, MasteryItem, ProfileResponse } from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

const CAUSE_LABELS: Record<string, string> = {
  concept_confusion: "概念混淆",
  formula_error: "公式用错",
  calc_slip: "计算失误",
  misread: "审题偏差",
  knowledge_gap: "知识缺口",
};

const STATE_LABELS: Record<string, string> = {
  new: "新错题",
  reviewing: "复习中",
  mastered: "已掌握",
};

const EVIDENCE_LABELS: Record<string, string> = {
  quiz_attempt: "练习作答",
  review: "复习作答",
  assessment_answer: "核验作答",
  appeal_regrade: "申诉重判",
};

const EVIDENCE_STRENGTH_LABELS = {
  limited: "较少",
  moderate: "积累中",
  strong: "较充分",
} as const;

/** confidence 是内部证据累计分，不是校准后的统计概率；只向用户展示定性档位。
 *  档位判定的唯一出处在后端（eduagents_core.mastery.thresholds.evidence_strength），
 *  前端不复刻阈值：字段缺失（旧后端）时只标「未知」，不猜。 */
function evidenceStrengthLabel(item: MasteryWithMeta): string | null {
  const level = item.evidence_strength;
  return level ? EVIDENCE_STRENGTH_LABELS[level] : null;
}

/** 遗忘快照只在 3 天后保持率低于此值时渲染（P2-7：高于 90% 属噪声不打扰）。 */
const RETENTION_SHOW_BELOW = 0.9;

/** 最近掌握度变化默认显示条数（C1：超出收进「展开更多」）。 */
const CHANGES_PAGE_SIZE = 10;

function ActionIcon({ kind }: { kind: "consolidate" | "review" | "verify" }) {
  return (
    <span
      className={`flex h-8 w-8 items-center justify-center rounded-control ${
        kind === "verify"
          ? "bg-brand-50 text-brand-700"
          : "bg-warning-50 text-warning-600"
      }`}
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="h-4 w-4"
        aria-hidden
      >
        {kind === "consolidate" && <path d="m13 2-8 12h7l-1 8 8-12h-7l1-8Z" />}
        {kind === "review" && (
          <>
            <circle cx="12" cy="12" r="8.5" />
            <path d="M12 7.5V12l3 2" />
          </>
        )}
        {kind === "verify" && <path d="m5 12.5 4.2 4L19 7" />}
      </svg>
    </span>
  );
}

/**
 * 掌握度行 + 后端附带/并行契约字段（共享 types.ts 不改，页面局部扩展）：
 * retention_*（遗忘快照）；kb_id/kb_name/subject/chapter/aliases
 *（真实规模契约，后端并行开发中，缺失时本页各区块自动降级）。
 */
type MasteryWithMeta = MasteryItem & {
  retention_now?: number | null;
  retention_in_3d?: number | null;
  kb_id?: string | null;
  kb_name?: string | null;
  subject?: string | null;
  chapter?: string | null;
  aliases?: string[] | null;
};

interface InsightsResponse {
  insights: InsightItem[];
}

/** kind 中文标签（models.py CHECK 白名单：confused/understood/misconception）。 */
const KIND_LABELS: Record<string, { label: string; className: string }> = {
  confused: { label: "困惑", className: "bg-amber-50 text-amber-700 ring-amber-200/70" },
  understood: {
    label: "已理解",
    className: "bg-emerald-50 text-emerald-700 ring-emerald-200/70",
  },
  misconception: { label: "误解", className: "bg-rose-50 text-rose-700 ring-rose-200/70" },
};
const KIND_BADGE_FALLBACK = "bg-surface-muted text-ink-secondary ring-line/70";

const SOURCE_LABELS: Record<string, string> = {
  chat_reflection: "对话洞察",
  tutor_summary: "辅导小结",
};

/** 计数条形行（纯 div 宽度百分比）。 */
function CountBars({
  data,
  labels,
}: {
  data: Record<string, number>;
  labels: Record<string, string>;
}) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) {
    return <p className="py-3 text-center text-caption text-ink-muted">暂无数据</p>;
  }
  const max = Math.max(...entries.map(([, n]) => n), 1);
  return (
    <ul className="space-y-2">
      {entries.map(([key, n]) => (
        <li
          key={key}
          className="flex items-center gap-2 rounded-lg px-1 py-0.5 text-caption transition-colors hover:bg-surface-muted"
        >
          <span className="w-16 shrink-0 truncate text-ink-muted" title={key}>
            {labels[key] ?? key}
          </span>
          <span className="h-3.5 min-w-0 flex-1 rounded-full bg-surface-muted">
            <span
              className="block h-full rounded-full bg-indigo-500"
              style={{ width: `${Math.max((n / max) * 100, 4)}%` }}
              title={`${labels[key] ?? key}：${n}`}
            />
          </span>
          <span className="w-8 shrink-0 text-right font-mono text-ink-secondary">{n}</span>
        </li>
      ))}
    </ul>
  );
}

/** 折叠组头的展开箭头。 */
function Chevron({ open, className }: { open: boolean; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      className={`${className ?? "h-3.5 w-3.5"} shrink-0 text-ink-muted transition-transform motion-reduce:transition-none ${
        open ? "rotate-90" : ""
      }`}
    >
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}

/** 老值→新值展示：默认 2 位小数，变化小于展示精度时自动升位（避免「0.76 → 0.76」）。 */
function fmtPair(oldP: number, newP: number): [string, string] {
  for (const digits of [2, 3, 4]) {
    const a = oldP.toFixed(digits);
    const b = newP.toFixed(digits);
    if (a !== b) return [a, b];
  }
  return [oldP.toFixed(2), newP.toFixed(2)];
}

// ---- 明细折叠树（B1）：科目 → 章节 → KC ----

interface ChapterGroup {
  key: string;
  name: string;
  mean: number | null;
  weakCount: number;
  items: MasteryWithMeta[];
}

interface SubjectGroup {
  key: string;
  name: string;
  total: number;
  weakCount: number;
  chapters: ChapterGroup[];
}

const masteryValue = (m: MasteryWithMeta) => m.p_mastery ?? 0;
const isWeak = (m: MasteryWithMeta, weakThreshold: number) => m.evidence_state === "observed" && m.p_mastery != null && m.p_mastery < weakThreshold;
/** 「到期」口径：FSRS 3 天后保持率有值且低于 90%（与遗忘快照展示阈值一致）。 */
const isDue = (m: MasteryWithMeta) =>
  m.evidence_state === "observed" &&
  m.retention_in_3d != null &&
  m.retention_in_3d < RETENTION_SHOW_BELOW;

/** 名称/别名本地搜索（q 已 trim + 小写）。 */
function matchesQuery(m: MasteryWithMeta, q: string): boolean {
  if (m.name.toLowerCase().includes(q)) return true;
  return (m.aliases ?? []).some((a) => a.toLowerCase().includes(q));
}

/** 按 subject → chapter 聚合，保持首次出现顺序（章节序即后端下发序）。 */
function buildTree(items: MasteryWithMeta[], weakThreshold: number): SubjectGroup[] {
  const groups: SubjectGroup[] = [];
  const byKey = new Map<string, SubjectGroup>();
  for (const m of items) {
    const sName = m.subject ?? m.kb_name ?? "未分组";
    let g = byKey.get(sName);
    if (!g) {
      g = { key: sName, name: sName, total: 0, weakCount: 0, chapters: [] };
      byKey.set(sName, g);
      groups.push(g);
    }
    const cName = m.chapter ?? "未分章";
    let c = g.chapters.find((x) => x.name === cName);
    if (!c) {
      c = { key: `${sName}⟂${cName}`, name: cName, mean: null, weakCount: 0, items: [] };
      g.chapters.push(c);
    }
    c.items.push(m);
    g.total += 1;
    if (isWeak(m, weakThreshold)) {
      c.weakCount += 1;
      g.weakCount += 1;
    }
  }
  for (const g of groups) {
    for (const c of g.chapters) {
      const observed = c.items.filter((item) => item.evidence_state === "observed" && item.p_mastery != null);
      c.mean = observed.length
        ? observed.reduce((sum, item) => sum + masteryValue(item), 0) / observed.length
        : null;
    }
  }
  return groups;
}

/**
 * 掌握度明细行：压缩行高；桌面 hover/键盘聚焦浮出快捷动作，
 * 「…」菜单在所有尺寸可用（触屏兜底）；窄屏为两行卡片（名称行 + 元信息行）。
 */
function MasteryRow({ item, weakThreshold }: {
  item: MasteryWithMeta;
  weakThreshold: number;
}) {
  const weak = isWeak(item, weakThreshold);
  const observed = item.evidence_state === "observed" && item.p_mastery != null;
  const ret3d = item.retention_in_3d;
  const retentionTitle =
    ret3d != null
      ? `再不复习，3 天后预计保持率 ${Math.floor(ret3d * 100)}%${
          item.retention_now != null
            ? `（当前 ${Math.floor(item.retention_now * 100)}%，FSRS 遗忘曲线确定性预测）`
            : "（FSRS 遗忘曲线确定性预测）"
        }`
      : undefined;

  return (
    <li className="group relative rounded-lg transition-colors hover:bg-indigo-50/50">
      <div className="flex items-center gap-2.5 px-1.5 py-1">
        <ProgressRing
          value={observed ? masteryValue(item) : 0}
          color={observed ? undefined : "#cbd5e1"}
          showValue={observed}
          size={32}
          strokeWidth={3.5}
          title={`${item.name}：${item.evidence_state_label ?? (item.evidence_state === "unobserved" ? "尚无证据" : item.evidence_state === "estimated" ? "估计中" : `掌握度 ${fmtPct(masteryValue(item))}`)}`}
        />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="truncate text-sm font-medium text-ink-secondary" title={item.name}>
              {item.name}
            </span>
            {weak && (
              <span
                className="shrink-0 rounded-full bg-amber-50 px-1.5 py-px text-caption font-medium text-amber-700 ring-1 ring-inset ring-amber-200/70"
                title={`薄弱点：掌握度 ${fmtPct(masteryValue(item))} < ${fmtPct(weakThreshold)}`}
              >
                薄弱
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-body-sm text-ink-muted">
            {evidenceStrengthLabel(item) && (
              <span
                className="cursor-help"
                title="证据强度表示有效练习、复习或核验作答的积累程度，不是统计置信概率"
              >
                证据 {evidenceStrengthLabel(item)}
              </span>
            )}
            <span>{item.evidence_state_label ?? (item.evidence_state === "unobserved" ? "尚无证据" : item.evidence_state === "estimated" ? "估计中" : "已观测")}</span>
            {/* 遗忘快照（P2-7）缩成徽章，整句进 tooltip；<90% 才显示避免噪声 */}
            {observed && ret3d != null && ret3d < RETENTION_SHOW_BELOW && (
              <span
                className="inline-flex cursor-help items-center rounded-full bg-amber-50 px-1.5 py-px text-caption font-medium text-amber-700 ring-1 ring-inset ring-amber-200/70"
                title={retentionTitle}
              >
                3 天后 {Math.floor(ret3d * 100)}%
              </span>
            )}
          </div>
        </div>

      </div>
    </li>
  );
}

export function CurrentLearningState() {
  const [banner, setBanner] = useState<string | null>(null);

  // 记忆条目（P2-1）：null=加载中；主画像失败不牵连本区块，各自兜底
  const [insights, setInsights] = useState<InsightItem[] | null>(null);
  const [insightsError, setInsightsError] = useState<string | null>(null);
  const [deletingInsightId, setDeletingInsightId] = useState<string | null>(null);

  const [reloadKey, setReloadKey] = useState(0);

  // 明细树交互态（B1/B2）
  const [query, setQuery] = useState("");
  const [weakOnly, setWeakOnly] = useState(false);
  const [dueOnly, setDueOnly] = useState(false);
  // 折叠覆写：未覆写时科目默认展开、章节默认「含薄弱才展开」
  const [subjectOverrides, setSubjectOverrides] = useState<Record<string, boolean>>({});
  const [chapterOverrides, setChapterOverrides] = useState<Record<string, boolean>>({});

  // 最近变化展开态（C1）
  const [changesExpanded, setChangesExpanded] = useState(false);

  // 画像 + 记忆条目（A5 收敛: useApiResource; reloadKey 走 deps; insights 保留状态供删除后本地更新）
  const profileRes = useApiResource<ProfileResponse>("/api/profile", {
    errorMessage: "画像加载失败",
    deps: [reloadKey],
  });
  const insightsRes = useApiResource<InsightsResponse>("/api/profile/insights", {
    errorMessage: "记忆条目加载失败",
    deps: [reloadKey],
  });
  const profile = profileRes.data;
  const loading = profileRes.loading;
  const error = profileRes.error;

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 记忆条目的单向同步, 无纯派生替代 */
  useEffect(() => {
    if (insightsRes.data) setInsights(insightsRes.data.insights);
    else if (insightsRes.error) {
      setInsights([]);
      setInsightsError(insightsRes.error);
    }
  }, [insightsRes.data, insightsRes.error]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const deleteInsight = async (id: string) => {
    setDeletingInsightId(id);
    try {
      await apiJson<undefined>(`/api/profile/insights/${id}`, { method: "DELETE" });
      setInsights((prev) => (prev ?? []).filter((x) => x.id !== id));
    } catch (e) {
      setBanner(e instanceof ApiError ? e.message : "删除失败，请重试");
    } finally {
      setDeletingInsightId(null);
    }
  };

  if (loading)
    return (
      <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
        <SkeletonPage />
      </div>
    );
  if (error || !profile) {
    return (
      <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
        <h2 className="text-[20px] font-semibold text-ink">当前状态</h2>
        <ErrorBanner message={error ?? "画像为空"} />
        <button
          type="button"
          onClick={() => setReloadKey((k) => k + 1)}
          className="rounded-lg border border-line px-3 py-1.5 text-sm text-ink-secondary hover:border-indigo-300 hover:text-indigo-700"
        >
          重试
        </button>
      </div>
    );
  }

  const mastery = profile.mastery as MasteryWithMeta[];
  const weakThreshold = profile.weak_threshold;
  const q = query.trim().toLowerCase();
  const filtered = mastery.filter(
    (m) =>
      (!q || matchesQuery(m, q)) && (!weakOnly || isWeak(m, weakThreshold)) && (!dueOnly || isDue(m)),
  );
  const tree = buildTree(filtered, weakThreshold);
  const filterActive = Boolean(q) || weakOnly || dueOnly;
  // 零变化行后端已滤（?include_noop=1 保留审计口径），前端再兜底一次（C1）
  const changes = profile.recent_changes.filter((c) => Math.abs(c.new_p - c.old_p) >= 0.0001);
  const shownChanges = changesExpanded ? changes : changes.slice(0, CHANGES_PAGE_SIZE);
  const consolidateItems = [...mastery].filter((item) => isWeak(item, weakThreshold)).sort((a, b) => masteryValue(a) - masteryValue(b));
  const dueItems = [...mastery].filter((item) => isDue(item)).sort((a, b) => (a.retention_in_3d ?? 1) - (b.retention_in_3d ?? 1));
  const verifyItems = mastery.filter((item) => item.evidence_state !== "observed");
  const observed = mastery.filter((item) => item.evidence_state === "observed" && item.p_mastery != null);
  const strongest = [...observed].sort((a, b) => masteryValue(b) - masteryValue(a))[0];
  const weakest = [...observed].sort((a, b) => masteryValue(a) - masteryValue(b))[0];

  const chipCls = (on: boolean) =>
    `rounded-full border px-2.5 py-1 text-caption font-medium transition-colors ${
      on
        ? "border-indigo-300 bg-indigo-50 text-indigo-700"
        : "border-line bg-white text-ink-muted hover:border-line-strong"
    }`;

  return (
    <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      <div className="flex items-end justify-between gap-3"><div><h2 className="text-[20px] font-semibold tracking-[-0.03em] text-ink">当前状态</h2><p className="mt-1 text-body-sm text-ink-muted">系统基于真实学习证据形成的当前认识。</p></div><button type="button" onClick={() => setReloadKey((k) => k + 1)} className="rounded-control border border-line-strong bg-white px-3 py-1.5 text-body-sm font-medium text-ink-secondary">刷新</button></div>
      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}

      {/* 画像概述：陈述性信息，不是主操作 —— 用浅色面，近黑只留给本页唯一的黑按钮。
          原来这里是 bg-brand-700，与下方 bg-brand-600 的按钮同色值（见 globals.css
          --color-brand-600/700），四块同深度的黑并列，层级直接失效。 */}
      <section className="rounded-overlay border border-line bg-surface-muted p-5 sm:p-6">
        <p className="text-body-sm font-medium text-ink-secondary">系统此刻如何认识你</p>
        <p className="mt-2 max-w-4xl text-[15px] leading-7 text-ink">
          {mastery.length === 0
            ? "还没有足够的真实作答证据。完成第一次练习、复习或核验后的真实作答后，这里会逐步形成你的学习画像。"
            : <>当前共识别到 {mastery.length} 个知识点。{strongest ? `「${strongest.name}」表现相对稳定` : "尚未形成证据充分的掌握度"}{weakest ? `，「${weakest.name}」仍最值得优先巩固` : ""}。画像会随新的真实作答证据持续更新。</>}
        </p>
        {mastery.length === 0 && (
          <div className="mt-4 flex flex-wrap items-center gap-2.5">
            <Link
              href="/practice"
              className="rounded-control bg-brand-600 px-4 py-2 text-body-sm font-medium text-white transition-colors hover:bg-brand-700"
            >
              去练习，攒第一份证据
            </Link>
            <Link
              href="/courses"
              className="rounded-control border border-line-strong bg-white px-4 py-2 text-body-sm font-medium text-ink-secondary transition-colors hover:bg-surface-muted"
            >
              上传资料建知识库
            </Link>
          </div>
        )}
        {changes[0] && <p className="mt-3 text-caption text-ink-muted">最近更新 {fmtDateTime(changes[0].at)} · {EVIDENCE_LABELS[changes[0].evidence_type] ?? changes[0].evidence_type}</p>}
      </section>

      <div className="grid gap-3 md:grid-cols-3">
        <section className="rounded-overlay border border-line bg-white p-4"><div className="flex items-start justify-between"><ActionIcon kind="consolidate" /><strong className="text-[20px] text-ink">{consolidateItems.length}</strong></div><h2 className="mt-3 text-[14px] font-semibold text-ink">待巩固</h2><p className="mt-1 text-body-sm text-ink-muted">掌握较薄弱，最值得先动手</p><ul className="mt-3 space-y-2">{consolidateItems.slice(0, 3).map((item) => <li key={item.kc_id} className="truncate text-body-sm text-ink-secondary"><span className="mr-2 text-warning-500">•</span>{item.name}</li>)}</ul></section>
        <section className="rounded-overlay border border-line bg-white p-4"><div className="flex items-start justify-between"><ActionIcon kind="review" /><strong className="text-[20px] text-ink">{dueItems.length}</strong></div><h2 className="mt-3 text-[14px] font-semibold text-ink">待复习</h2><p className="mt-1 text-body-sm text-ink-muted">掌握过，但快要遗忘</p><ul className="mt-3 space-y-2">{dueItems.slice(0, 3).map((item) => <li key={item.kc_id} className="truncate text-body-sm text-ink-secondary"><span className="mr-2 text-warning-500">•</span>{item.name}</li>)}</ul></section>
        <section className="rounded-overlay border border-line bg-white p-4"><div className="flex items-start justify-between"><ActionIcon kind="verify" /><strong className="text-[20px] text-ink">{verifyItems.length}</strong></div><h2 className="mt-3 text-[14px] font-semibold text-ink">待验证</h2><p className="mt-1 text-body-sm text-ink-muted">记录太少，画像还不确定</p><ul className="mt-3 space-y-2">{verifyItems.slice(0, 3).map((item) => <li key={item.kc_id} className="truncate text-body-sm text-ink-secondary"><span className="mr-2 text-brand-500">•</span>{item.name}</li>)}</ul></section>
      </div>

      {/* 雷达（A1）：库 tab + 章节聚合轴 + 少轴条形降级 + 轴下钻（组件内实现） */}
      <MasteryRadarPanel items={mastery} weakThreshold={weakThreshold} />

      {/* 明细折叠树（B1/B2）：科目 → 章节 → KC */}
      <Card
        title="掌握度明细"
        extra={
          <span className="text-caption text-ink-muted">
            {filterActive ? `${filtered.length} / ${mastery.length}` : mastery.length} 个知识点
          </span>
        }
      >
        {mastery.length === 0 ? (
          <EmptyState
            title="还没有掌握度证据"
            description="练习、复习或核验后的真实作答会形成可追溯的掌握度证据"
          />
        ) : (
          <div className="space-y-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索知识点名称…"
                aria-label="搜索知识点"
                className="w-full max-w-56 rounded-lg border border-line px-2.5 py-1.5 text-caption outline-none transition-colors focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
              />
              <button type="button" className={chipCls(weakOnly)} onClick={() => setWeakOnly((v) => !v)}>
                只看薄弱
              </button>
              <button type="button" className={chipCls(dueOnly)} onClick={() => setDueOnly((v) => !v)}>
                只看待复习
              </button>
            </div>
            {tree.length === 0 ? (
              <p className="py-4 text-center text-caption text-ink-muted">没有匹配的知识点</p>
            ) : (
              <ul className="space-y-1.5">
                {tree.map((g) => {
                  const subjectOpen = subjectOverrides[g.key] ?? true;
                  return (
                    <li key={g.key} className="rounded-xl border border-line">
                      <button
                        type="button"
                        aria-expanded={subjectOpen}
                        onClick={() =>
                          setSubjectOverrides((prev) => ({ ...prev, [g.key]: !subjectOpen }))
                        }
                        className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left transition-colors hover:bg-surface-muted"
                      >
                        <Chevron open={subjectOpen} />
                        <span className="text-sm font-semibold text-ink-secondary">{g.name}</span>
                        <span className="text-body-sm text-ink-muted">
                          {g.total} 个知识点
                          {g.weakCount > 0 && (
                            <span className="ml-1 text-amber-600">· {g.weakCount} 个薄弱</span>
                          )}
                        </span>
                      </button>
                      {subjectOpen && (
                        <ul className="space-y-1 px-2 pb-2">
                          {g.chapters.map((c) => {
                            // 过滤/搜索时全展开（用户在找东西）；否则默认「含薄弱才展开」
                            const chapterOpen =
                              chapterOverrides[c.key] ?? (filterActive || c.weakCount > 0);
                            return (
                              <li key={c.key} className="rounded-lg bg-surface-muted/60">
                                <button
                                  type="button"
                                  aria-expanded={chapterOpen}
                                  onClick={() =>
                                    setChapterOverrides((prev) => ({
                                      ...prev,
                                      [c.key]: !chapterOpen,
                                    }))
                                  }
                                  className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-surface-muted/70"
                                >
                                  <Chevron open={chapterOpen} className="h-3 w-3" />
                                  <span className="min-w-0 flex-1 truncate text-caption font-medium text-ink-secondary">
                                    {c.name}
                                  </span>
                                  <span
                                    className="font-mono text-body-sm"
                                    style={{ color: c.mean == null ? undefined : masteryColor(c.mean) }}
                                    title={c.mean == null
                                      ? `本章尚无证据充分的掌握度（${c.items.length} 个知识点）`
                                      : `章平均掌握度 ${fmtPct(c.mean)}（${c.items.length} 个知识点）`}
                                  >
                                    {c.mean == null ? "待验证" : fmtPct(c.mean)}
                                  </span>
                                  {c.weakCount > 0 && (
                                    <span className="rounded-full bg-amber-50 px-1.5 py-px text-caption font-medium text-amber-700 ring-1 ring-inset ring-amber-200/70">
                                      {c.weakCount} 薄弱
                                    </span>
                                  )}
                                </button>
                                {chapterOpen && (
                                  <ul className="space-y-0.5 px-1 pb-1">
                                    {c.items.map((m) => (
                                      <MasteryRow key={m.kc_id} item={m} weakThreshold={weakThreshold} />
                                    ))}
                                  </ul>
                                )}
                              </li>
                            );
                          })}
                        </ul>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="错题分布 · 按错因">
          <CountBars data={profile.error_distribution.by_cause} labels={CAUSE_LABELS} />
        </Card>
        <Card title="错题分布 · 按状态">
          <CountBars data={profile.error_distribution.by_state} labels={STATE_LABELS} />
        </Card>
      </div>

      {/* 最近掌握度变化（C1）：默认 10 条；权重徽章收 tooltip */}
      <Card
        title="最近掌握度变化"
        extra={<span className="text-caption text-ink-muted">每次变化可核对到具体作答（F8-A3）</span>}
      >
        {changes.length === 0 ? (
          <p className="py-3 text-center text-caption text-ink-muted">暂无变化记录</p>
        ) : (
          <>
            <ul className="divide-y divide-line">
              {shownChanges.map((c) => {
                const [oldS, newS] = fmtPair(c.old_p, c.new_p);
                const up = c.new_p >= c.old_p;
                return (
                  <li key={c.id} className="flex items-center gap-2.5 py-1.5 text-caption">
                    <span
                      aria-hidden
                      className={`inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-caption font-bold text-white ${
                        up ? "bg-emerald-500" : "bg-orange-500"
                      }`}
                    >
                      {up ? "↑" : "↓"}
                    </span>
                    <span className="shrink-0 font-mono text-ink-muted">{fmtDateTime(c.at)}</span>
                    <span className="min-w-0 flex-1 truncate font-medium text-ink-secondary">
                      {c.kc_name}
                    </span>
                    <span className={`shrink-0 font-mono ${up ? "text-emerald-600" : "text-orange-600"}`}>
                      {oldS} → {newS}
                    </span>
                    <span
                      className="shrink-0 cursor-help rounded bg-surface-muted px-1.5 py-px text-caption text-ink-muted"
                      title={
                        c.weight < 1
                          ? `${EVIDENCE_LABELS[c.evidence_type] ?? c.evidence_type} · 证据权重 ×${c.weight}（不同证据来源按统一契约赋权）`
                          : EVIDENCE_LABELS[c.evidence_type] ?? c.evidence_type
                      }
                    >
                      {EVIDENCE_LABELS[c.evidence_type] ?? c.evidence_type}
                    </span>
                  </li>
                );
              })}
            </ul>
            {changes.length > CHANGES_PAGE_SIZE && (
              <button
                type="button"
                onClick={() => setChangesExpanded((v) => !v)}
                className="mt-2 w-full rounded-lg border border-line py-1.5 text-caption text-ink-muted transition-colors hover:border-line hover:text-ink-secondary"
              >
                {changesExpanded ? "收起" : `展开更多（共 ${changes.length} 条）`}
              </button>
            )}
          </>
        )}
      </Card>

      {/* AI 记住的你（P2-1）：长期记忆可见可删 */}
      <Card
        title="AI 记住的你"
        extra={<Link href="/settings?section=memory" className="text-caption text-brand-600">管理与纠正记忆</Link>}
      >
        {insights === null ? (
          <SkeletonList rows={3} />
        ) : insights.length === 0 ? (
          <p className="py-4 text-center text-caption text-ink-muted">
            {insightsError ?? "还没有记忆条目 —— 在对话或辅导中学习后，AI 会记下你在什么语境下困惑、又在哪里豁然开朗"}
          </p>
        ) : (
          <ul className="divide-y divide-line">
            {insights.map((it) => {
              const kind = KIND_LABELS[it.kind];
              return (
                <li key={it.id} className="flex items-start gap-2.5 py-2 text-caption">
                  <span
                    className={`mt-px shrink-0 rounded-full px-1.5 py-px text-caption font-medium ring-1 ring-inset ${
                      kind?.className ?? KIND_BADGE_FALLBACK
                    }`}
                  >
                    {kind?.label ?? it.kind}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="break-words text-ink-secondary"><MathText text={it.text} /></div>
                    <p className="mt-0.5 text-caption text-ink-muted">
                      {it.kc_name && <span className="mr-1.5">{it.kc_name}</span>}
                      {SOURCE_LABELS[it.source] ?? it.source} · {fmtDateTime(it.created_at)}
                      {it.user_corrected ? " · 已由你纠正" : it.effective_status !== "active" ? " · 暂不引用" : ""}
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={deletingInsightId === it.id}
                    onClick={() => deleteInsight(it.id)}
                    title="删除这条记忆（用户纠正权，删除即时生效）"
                    className="shrink-0 rounded-lg px-1.5 py-0.5 text-ink-muted transition-colors hover:bg-rose-50 hover:text-rose-600 disabled:opacity-40"
                  >
                    {deletingInsightId === it.id ? <Spinner className="h-3 w-3" /> : "删除"}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </div>
  );
}
