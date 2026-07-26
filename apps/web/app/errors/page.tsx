"use client";

// 错题本（F6）：统计卡片（点击筛选）+ 错因分布条形（纯 CSS，同 profile 页风格，点击筛选）
// + 错题卡片列表（我的答案 vs 正确答案 / 解析 / 可改错因 / 变式重练 / 归档）+ 分页 + 空态。

import { useEffect, useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  Skeleton,
  SkeletonList,
  Spinner,
  fmtDateTime,
  fmtRelative,
} from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type {
  ErrorItemOut,
  ErrorListResponse,
  ErrorStatsResponse,
  ReviewVariantResult,
} from "@/lib/types";

/** 错因五分类中文化（models.py: ErrorItem.cause）。 */
const CAUSE_LABELS: Record<string, string> = {
  concept_confusion: "概念混淆",
  formula_error: "公式用错",
  calc_slip: "计算失误",
  misread: "审题偏差",
  knowledge_gap: "知识缺口",
};
const CAUSE_ORDER = Object.keys(CAUSE_LABELS);

/** 错因五色语义化（徽章底色 + 分布条颜色，与复习页同一套色）。 */
const CAUSE_COLORS: Record<string, { badge: string; bar: string; active: string }> = {
  concept_confusion: {
    badge: "bg-violet-50 text-violet-700 ring-violet-200/70",
    bar: "bg-violet-400",
    active: "bg-violet-600",
  },
  formula_error: {
    badge: "bg-sky-50 text-sky-700 ring-sky-200/70",
    bar: "bg-sky-400",
    active: "bg-sky-600",
  },
  calc_slip: {
    badge: "bg-amber-50 text-amber-700 ring-amber-200/70",
    bar: "bg-amber-400",
    active: "bg-amber-600",
  },
  misread: {
    badge: "bg-orange-50 text-orange-700 ring-orange-200/70",
    bar: "bg-orange-400",
    active: "bg-orange-600",
  },
  knowledge_gap: {
    badge: "bg-rose-50 text-rose-700 ring-rose-200/70",
    bar: "bg-rose-400",
    active: "bg-rose-600",
  },
};
const CAUSE_COLOR_FALLBACK = {
  badge: "bg-slate-100 text-slate-600 ring-slate-200/70",
  bar: "bg-indigo-400",
  active: "bg-indigo-600",
};

const STATE_LABELS: Record<string, { label: string; className: string }> = {
  new: { label: "新错题", className: "bg-red-50 text-red-600" },
  reviewing: { label: "复习中", className: "bg-amber-50 text-amber-700" },
  mastered: { label: "已攻克", className: "bg-emerald-50 text-emerald-700" },
  archived: { label: "已归档", className: "bg-slate-100 text-slate-500" },
};

const PAGE_SIZE = 10;

/** 顶部统计卡片：state=null 表示"全部"。 */
const STAT_CARDS: { key: string; label: string; state: string | null }[] = [
  { key: "total", label: "总错题数", state: null },
  { key: "active", label: "待复习", state: "active" },
  { key: "mastered", label: "已攻克", state: "mastered" },
  { key: "archived", label: "归档", state: "archived" },
];

function StateBadge({ state }: { state: string }) {
  const meta = STATE_LABELS[state] ?? {
    label: state,
    className: "bg-slate-100 text-slate-600",
  };
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${meta.className}`}>
      {meta.label}
    </span>
  );
}

export default function ErrorsPage() {
  const [stats, setStats] = useState<ErrorStatsResponse | null>(null);
  const [list, setList] = useState<ErrorListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [stateFilter, setStateFilter] = useState<string | null>(null);
  const [causeFilter, setCauseFilter] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [reloadKey, setReloadKey] = useState(0);
  const [busyId, setBusyId] = useState<string | null>(null);

  // 列表 + 统计并行拉取（首个 setState 在 await 之后）
  useEffect(() => {
    let alive = true;
    (async () => {
      const params = new URLSearchParams({
        page: String(page),
        page_size: String(PAGE_SIZE),
      });
      if (stateFilter) params.set("state", stateFilter);
      if (causeFilter) params.set("cause", causeFilter);
      const [listRes, statsRes] = await Promise.allSettled([
        apiJson<ErrorListResponse>(`/api/errors?${params.toString()}`),
        apiJson<ErrorStatsResponse>("/api/errors/stats"),
      ]);
      if (!alive) return;
      if (listRes.status === "fulfilled") {
        setList(listRes.value);
        setError(null);
      } else {
        setError(
          listRes.reason instanceof ApiError
            ? listRes.reason.message
            : "错题本加载失败",
        );
      }
      if (statsRes.status === "fulfilled") setStats(statsRes.value);
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, [stateFilter, causeFilter, page, reloadKey]);

  const reload = () => {
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  const pickState = (next: string | null) => {
    setStateFilter((prev) => (prev === next ? null : next));
    setPage(1);
    setLoading(true);
  };

  const pickCause = (next: string) => {
    setCauseFilter((prev) => (prev === next ? null : next));
    setPage(1);
    setLoading(true);
  };

  const patchItem = (id: string, patch: Partial<ErrorItemOut>) => {
    setList((prev) =>
      prev
        ? { ...prev, items: prev.items.map((it) => (it.id === id ? { ...it, ...patch } : it)) }
        : prev,
    );
  };

  const changeCause = async (item: ErrorItemOut, cause: string) => {
    if (cause === item.cause) return;
    const previous = item.cause;
    patchItem(item.id, { cause }); // 乐观更新
    setBanner(null);
    try {
      await apiJson<{ id: string; cause: string }>(`/api/errors/${item.id}/cause`, {
        body: { cause },
      });
      setStats(await apiJson<ErrorStatsResponse>("/api/errors/stats"));
    } catch (err) {
      patchItem(item.id, { cause: previous }); // 失败回滚
      setBanner(err instanceof ApiError ? err.message : "错因修改失败，请重试");
    }
  };

  const archive = async (item: ErrorItemOut) => {
    if (busyId) return;
    setBusyId(item.id);
    setBanner(null);
    try {
      await apiJson<{ id: string; state: string }>(`/api/errors/${item.id}/archive`, {
        method: "POST",
        body: {},
      });
      setNotice("已归档，该题不再进入今日复习队列。");
      setReloadKey((k) => k + 1);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "归档失败，请重试");
    } finally {
      setBusyId(null);
    }
  };

  const makeVariant = async (item: ErrorItemOut) => {
    if (busyId) return;
    setBusyId(item.id);
    setBanner(null);
    try {
      const res = await apiJson<ReviewVariantResult>(
        `/api/reviews/${item.id}/variant`,
        { method: "POST", body: {} },
      );
      setNotice(`已生成同考点变式题（${res.question.stem.slice(0, 24)}…），去「复习」作答。`);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "变式生成失败，请重试");
    } finally {
      setBusyId(null);
    }
  };

  const causeEntries = CAUSE_ORDER.map((key) => ({
    key,
    count: stats?.by_cause[key] ?? 0,
  }));
  const causeMax = Math.max(...causeEntries.map((e) => e.count), 1);
  const statValue = (key: string): number => {
    if (!stats) return 0;
    if (key === "total") return stats.total;
    if (key === "active")
      return (stats.by_state.new ?? 0) + (stats.by_state.reviewing ?? 0);
    return stats.by_state[key] ?? 0;
  };

  const filtered = stateFilter !== null || causeFilter !== null;

  return (
    <div className="mx-auto w-full max-w-4xl space-y-4 p-4">
      <PageHeader
        title="错题本"
        subtitle="测评与复习中的错题自动归集；错因五分类可随时修正，攻克后自动退出复习队列"
        action={
          <button
            type="button"
            onClick={reload}
            className="rounded-xl bg-indigo-600 px-3.5 py-1.5 text-sm font-semibold text-white transition-colors hover:bg-indigo-700"
          >
            刷新
          </button>
        }
      />

      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}
      {error && <ErrorBanner message={error} onRetry={reload} />}
      {notice && (
        <div className="flex items-center justify-between rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-700">
          <span>{notice}</span>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="text-emerald-400 hover:text-emerald-600"
            aria-label="关闭提示"
          >
            ✕
          </button>
        </div>
      )}

      {/* 统计卡片：点击筛选 */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {STAT_CARDS.map((card) => {
          const active = stateFilter === card.state;
          return (
            <button
              key={card.key}
              type="button"
              onClick={() => pickState(card.state)}
              className={`rounded-2xl border px-4 py-3 text-left shadow-[0_4px_20px_-6px_rgba(79,70,229,0.10)] transition-all duration-200 hover:-translate-y-0.5 hover:shadow-[0_8px_26px_-6px_rgba(79,70,229,0.18)] motion-reduce:hover:translate-y-0 ${
                active
                  ? "border-indigo-300 bg-indigo-50"
                  : "border-slate-200 bg-white hover:border-indigo-200"
              }`}
            >
              <p className="text-xs text-slate-500">{card.label}</p>
              <p
                className={`mt-0.5 font-mono text-2xl font-semibold ${
                  active ? "text-indigo-700" : "text-slate-800"
                }`}
              >
                {statValue(card.key)}
              </p>
            </button>
          );
        })}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="错因分布"
          extra={
            causeFilter && (
              <button
                type="button"
                onClick={() => pickCause(causeFilter)}
                className="text-xs text-indigo-600 hover:underline"
              >
                清除筛选
              </button>
            )
          }
        >
          {stats && stats.total === 0 ? (
            <p className="py-3 text-center text-xs text-slate-400">暂无数据</p>
          ) : (
            <ul className="space-y-2">
              {causeEntries.map((entry) => {
                const active = causeFilter === entry.key;
                const color = CAUSE_COLORS[entry.key] ?? CAUSE_COLOR_FALLBACK;
                return (
                  <li key={entry.key}>
                    <button
                      type="button"
                      onClick={() => pickCause(entry.key)}
                      className="flex w-full items-center gap-2 rounded-lg px-1 py-0.5 text-xs transition-colors hover:bg-slate-50"
                    >
                      <span
                        className={`w-16 shrink-0 truncate text-left ${
                          active ? "font-semibold text-indigo-700" : "text-slate-500"
                        }`}
                      >
                        {CAUSE_LABELS[entry.key]}
                      </span>
                      <span className="h-3.5 min-w-0 flex-1 rounded-full bg-slate-100">
                        <span
                          className={`block h-full rounded-full transition-[width] duration-300 ${
                            active ? color.active : color.bar
                          }`}
                          style={{
                            width: `${Math.max((entry.count / causeMax) * 100, 4)}%`,
                          }}
                        />
                      </span>
                      <span className="w-8 shrink-0 text-right font-mono text-slate-600">
                        {entry.count}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>

        <Card title="高频知识点 Top 10">
          {!stats || stats.by_kc.length === 0 ? (
            <p className="py-3 text-center text-xs text-slate-400">暂无数据</p>
          ) : (
            <ul className="space-y-1.5">
              {stats.by_kc.map((kc) => (
                <li
                  key={kc.kc_id}
                  className="flex items-center justify-between gap-2 text-xs"
                >
                  <span className="min-w-0 truncate text-slate-600" title={kc.name}>
                    {kc.name || `KC ${kc.kc_id.slice(0, 8)}`}
                  </span>
                  <span className="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 font-mono text-slate-600">
                    {kc.count}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {filtered && (
        <p className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
          当前筛选：
          {stateFilter && (
            <span className="rounded-full bg-indigo-50 px-2 py-0.5 font-medium text-indigo-700">
              {stateFilter === "active"
                ? "待复习"
                : (STATE_LABELS[stateFilter]?.label ?? stateFilter)}
            </span>
          )}
          {causeFilter && (
            <span className="rounded-full bg-indigo-50 px-2 py-0.5 font-medium text-indigo-700">
              {CAUSE_LABELS[causeFilter] ?? causeFilter}
            </span>
          )}
          <button
            type="button"
            onClick={() => {
              setStateFilter(null);
              setCauseFilter(null);
              setPage(1);
              setLoading(true);
            }}
            className="text-indigo-600 hover:underline"
          >
            全部清除
          </button>
        </p>
      )}

      {loading ? (
        <div className="space-y-4">
          {[0, 1].map((i) => (
            <div
              key={i}
              className="rounded-2xl border border-slate-200 bg-white p-4"
            >
              <div className="mb-3 flex gap-2">
                <Skeleton className="h-5 w-14 rounded-full" />
                <Skeleton className="h-5 w-24 rounded-full" />
                <Skeleton className="h-5 w-16 rounded-full" />
              </div>
              <Skeleton className="mb-3 h-4 w-3/4" />
              <SkeletonList rows={2} />
            </div>
          ))}
          <span className="sr-only">错题加载中…</span>
        </div>
      ) : !list || list.items.length === 0 ? (
        <EmptyState
          title={filtered ? "没有符合筛选条件的错题" : "错题本还是空的"}
          description={
            filtered
              ? "换个状态或错因看看，或清除筛选查看全部"
              : "做一次练习吧——答错的题会 100% 自动收进这里，并按遗忘曲线安排复习"
          }
          illustration="notebook"
        />
      ) : (
        <>
          <ul className="space-y-4">
            {list.items.map((item) => {
              const busy = busyId === item.id;
              return (
                <li key={item.id}>
                  <Card className="transition-all duration-200 hover:-translate-y-0.5 motion-reduce:hover:translate-y-0">
                    <div className="mb-2 flex flex-wrap items-center gap-1.5">
                      <StateBadge state={item.state} />
                      <label
                        className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ring-1 ring-inset ${
                          (CAUSE_COLORS[item.cause] ?? CAUSE_COLOR_FALLBACK).badge
                        }`}
                      >
                        <span>错因</span>
                        <select
                          value={item.cause}
                          onChange={(e) => void changeCause(item, e.target.value)}
                          aria-label="修改错因"
                          className="bg-transparent text-xs font-medium text-current outline-none"
                        >
                          {CAUSE_ORDER.map((c) => (
                            <option key={c} value={c}>
                              {CAUSE_LABELS[c]}
                            </option>
                          ))}
                        </select>
                      </label>
                      {item.kc_names.map((kc) => (
                        <span
                          key={kc}
                          className="max-w-32 truncate rounded-full bg-violet-50 px-2 py-0.5 text-xs text-violet-700"
                        >
                          {kc}
                        </span>
                      ))}
                      {item.is_variant && (
                        <span className="rounded-full bg-violet-50 px-2 py-0.5 text-xs font-medium text-violet-600">
                          变式题
                        </span>
                      )}
                      <span className="flex-1" />
                      <span
                        className="text-xs text-slate-400"
                        title={item.next_due ? fmtDateTime(item.next_due) : undefined}
                      >
                        错 {item.error_count} 次
                        {item.next_due && ` · 下次复习 ${fmtRelative(item.next_due)}`}
                      </span>
                    </div>

                    {item.question ? (
                      <>
                        <p className="mb-2 text-sm leading-relaxed text-slate-800">
                          <MathText text={item.question.stem} />
                        </p>
                        {item.question.options.length > 0 && (
                          <ol className="mb-2 space-y-0.5 text-xs text-slate-500">
                            {item.question.options.map((opt, i) => (
                              <li key={i}>
                                <span className="mr-1 font-medium text-slate-400">
                                  {String.fromCharCode(65 + i)}.
                                </span>
                                <MathText text={opt} />
                              </li>
                            ))}
                          </ol>
                        )}
                        <div className="flex flex-wrap gap-2 text-xs">
                          <span className="rounded-lg border border-red-200 bg-red-50/60 px-2.5 py-1 text-red-700">
                            我的答案：{item.my_answer || "（未作答）"}
                          </span>
                          <span className="rounded-lg border border-emerald-200 bg-emerald-50/60 px-2.5 py-1 text-emerald-700">
                            正确答案：<MathText text={item.question.answer} />
                          </span>
                        </div>
                        {item.question.explanation && (
                          <details className="mt-2 rounded-xl bg-slate-50 px-3 py-2">
                            <summary className="cursor-pointer text-xs font-medium text-slate-600">
                              查看解析
                            </summary>
                            <p className="mt-1 text-xs leading-relaxed text-slate-600">
                              <MathText text={item.question.explanation} />
                            </p>
                          </details>
                        )}
                      </>
                    ) : (
                      <p className="text-sm text-slate-400">原题已被删除</p>
                    )}

                    <div className="mt-3 flex items-center justify-end gap-2">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void makeVariant(item)}
                        className="flex items-center gap-1.5 rounded-xl border border-violet-200 bg-violet-50 px-3.5 py-1.5 text-sm font-medium text-violet-700 hover:bg-violet-100 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {busy && <Spinner className="h-3.5 w-3.5" />}
                        变式重练
                      </button>
                      {item.state !== "archived" && (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void archive(item)}
                          className="rounded-xl border border-slate-300 bg-white px-3.5 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          归档
                        </button>
                      )}
                    </div>
                  </Card>
                </li>
              );
            })}
          </ul>

          {list.pages > 1 && (
            <div className="flex items-center justify-center gap-3 text-sm">
              <button
                type="button"
                disabled={list.page <= 1}
                onClick={() => {
                  setPage((p) => Math.max(1, p - 1));
                  setLoading(true);
                }}
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
              >
                上一页
              </button>
              <span className="text-xs text-slate-500">
                第 {list.page} / {list.pages} 页 · 共 {list.total} 题
              </span>
              <button
                type="button"
                disabled={list.page >= list.pages}
                onClick={() => {
                  setPage((p) => p + 1);
                  setLoading(true);
                }}
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
              >
                下一页
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
