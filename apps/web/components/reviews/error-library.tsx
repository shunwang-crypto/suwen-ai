"use client";

// 错题本（F6）：统计卡片（点击筛选）+ 错因分布条形（纯 CSS，同 profile 页风格，点击筛选）
// + 错题卡片列表（我的答案 vs 正确答案 / 解析 / 可改错因 / 变式重练 / 归档）+ 分页 + 空态。

import Link from "next/link";
import { useEffect, useState, useSyncExternalStore } from "react";

import { ModelMarkdown } from "@/components/chat/model-markdown";
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
import { ErrorImportDialog } from "@/components/errors/error-import-dialog";
import { ApiError, apiJson } from "@/lib/api";
import { buildErrorQuery, useKnowledgeBaseFilter } from "@/lib/error-filters";
import { MathText } from "@/lib/katex";
import type {
  ErrorItemOut,
  ErrorListResponse,
  ErrorStatsResponse,
  KbOut,
  ReviewVariantResult,
} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

/** 错因五分类中文化（models.py: ErrorItem.cause）。 */
const CAUSE_LABELS: Record<string, string> = {
  concept_confusion: "概念混淆",
  formula_error: "公式用错",
  calc_slip: "计算失误",
  misread: "审题偏差",
  knowledge_gap: "知识缺口",
};
const CAUSE_ORDER = Object.keys(CAUSE_LABELS);

/** 错因使用产品语义色；主色只承担概念类与主要操作。 */
const CAUSE_COLORS: Record<string, { badge: string; bar: string; active: string }> = {
  concept_confusion: {
    badge: "bg-brand-50 text-brand-700 ring-brand-100",
    bar: "bg-brand-400",
    active: "bg-brand-600",
  },
  formula_error: {
    badge: "bg-warning-50 text-warning-700 ring-warning-500/20",
    bar: "bg-warning-500",
    active: "bg-warning-600",
  },
  calc_slip: {
    badge: "bg-warning-50 text-warning-700 ring-warning-500/20",
    bar: "bg-warning-500",
    active: "bg-warning-600",
  },
  misread: {
    badge: "bg-warning-50 text-warning-700 ring-warning-500/20",
    bar: "bg-warning-500",
    active: "bg-warning-600",
  },
  knowledge_gap: {
    badge: "bg-danger-50 text-danger-700 ring-danger-500/20",
    bar: "bg-danger-500",
    active: "bg-danger-600",
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
  { key: "total", label: "在库错题", state: null },
  { key: "active", label: "待复习", state: "active" },
  { key: "mastered", label: "已攻克", state: "mastered" },
  { key: "archived", label: "已归档", state: "archived" },
];

function StateBadge({ state }: { state: string }) {
  const meta = STATE_LABELS[state] ?? {
    label: state,
    className: "bg-slate-100 text-slate-600",
  };
  return (
    <span className={`rounded-full px-2 py-0.5 text-caption font-medium ${meta.className}`}>
      {meta.label}
    </span>
  );
}

const emptySubscribe = () => () => {};

export function ErrorLibrary() {
  // ?filter=unclassified（裁决 2）：未归类错题 = 所属题目资料未关联知识库，
  // 服务端 linked=none 过滤，与分页/状态/错因/搜索按 AND 组合。
  const search = useSyncExternalStore(
    emptySubscribe,
    () => window.location.search,
    () => "",
  );
  const unclassified = new URLSearchParams(search).get("filter") === "unclassified";
  const [stats, setStats] = useState<ErrorStatsResponse | null>(null);
  const [list, setList] = useState<ErrorListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [stateFilter, setStateFilter] = useState<string | null>(null);
  const [causeFilter, setCauseFilter] = useState<string | null>(null);
  // B4 真实规模: 科目 tab 与全文搜索都走后端过滤，结果数和翻页保持一致。
  const [kbs, setKbs] = useState<KbOut[]>([]);
  const [kbListLoaded, setKbListLoaded] = useState(false);
  const { kbFilter, selectKb } = useKnowledgeBaseFilter(kbs, kbListLoaded);
  const [query, setQuery] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [page, setPage] = useState(1);
  const [reloadKey, setReloadKey] = useState(0);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [editingItem, setEditingItem] = useState<ErrorItemOut | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const next = query.trim();
      if (next !== searchQuery) {
        setSearchQuery(next);
        setPage(1);
      }
    }, 300);
    return () => window.clearTimeout(timer);
  }, [query, searchQuery]);

  // 列表 + 统计并行拉取（A5 收敛: 两块 useApiResource, 过滤条件走路径, reloadKey 走 deps）
  const listQuery = buildErrorQuery({
    page,
    pageSize: PAGE_SIZE,
    state: stateFilter,
    cause: causeFilter,
    kbId: kbFilter,
    query: searchQuery,
    ...(unclassified ? { linked: "none" } : {}),
  });
  const listRes = useApiResource<ErrorListResponse>(`/api/errors?${listQuery}`, {
    errorMessage: "错题本加载失败",
    deps: [reloadKey],
  });
  const statsRes = useApiResource<ErrorStatsResponse>("/api/errors/stats", {
    deps: [reloadKey],
  });
  const loading = listRes.loading || statsRes.loading;

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 列表/统计/错误的单向同步, 无纯派生替代 */
  useEffect(() => {
    if (listRes.data) {
      setList(listRes.data);
      setError(null);
    } else if (listRes.error) {
      setError(listRes.error);
    }
  }, [listRes.data, listRes.error]);

  useEffect(() => {
    setStats(statsRes.data);
  }, [statsRes.data]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // 知识库列表 (科目 tab 数据源); 失败静默降级为"全部"单 tab, 不拖垮错题本
  const kbListRes = useApiResource<KbOut[]>("/api/kb");
  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源落定 → 科目 tab 数据源的单向同步 */
  useEffect(() => {
    if (kbListRes.data) {
      setKbs(kbListRes.data);
      setKbListLoaded(true);
    } else if (kbListRes.error) {
      setKbs([]);
      setKbListLoaded(true);
    }
  }, [kbListRes.data, kbListRes.error]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const reload = () => setReloadKey((k) => k + 1);

  const pickState = (next: string | null) => {
    setStateFilter((prev) => (prev === next ? null : next));
    setPage(1);
  };

  const pickCause = (next: string) => {
    setCauseFilter((prev) => (prev === next ? null : next));
    setPage(1);
  };

  const pickKb = (next: string | null) => {
    selectKb(next);
    setPage(1);
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

  const restore = async (item: ErrorItemOut) => {
    if (busyId) return;
    setBusyId(item.id);
    setBanner(null);
    try {
      await apiJson<{ id: string; state: string }>(`/api/errors/${item.id}/restore`, {
        method: "POST",
        body: {},
      });
      setNotice("已恢复到复习队列，将按原有记忆进度重新安排。");
      setReloadKey((k) => k + 1);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "恢复失败，请重试");
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

  const filtered = stateFilter !== null || causeFilter !== null || unclassified;
  const clearUnclassified = () => {
    const url = new URL(window.location.href);
    url.searchParams.delete("filter");
    window.history.replaceState(window.history.state, "", url);
    setPage(1);
  };
  const selectedItem = list?.items.find((item) => item.id === selectedItemId) ?? null;

  return (
    <div className="mx-auto w-full max-w-[1100px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      <PageHeader
        title="错题管理"
        subtitle="查看、筛选与归档真实作答中沉淀的错题。"
        action={
          <>
            <button
              type="button"
              onClick={() => {
                setEditingItem(null);
                setImportOpen(true);
              }}
              className="rounded-[8px] bg-brand-600 px-3.5 py-1.5 text-body-sm font-medium text-white hover:bg-brand-800"
            >
              录入错题
            </button>
            <Link
              href="/materials"
              className="rounded-[8px] border border-brand-200 bg-brand-50 px-3.5 py-1.5 text-body-sm font-medium text-brand-700 hover:bg-brand-100"
            >
              导入整份题目资料
            </Link>
            <button
              type="button"
              onClick={reload}
              className="rounded-[8px] border border-line-strong bg-white px-3.5 py-1.5 text-body-sm font-medium text-ink-secondary hover:border-brand-200 hover:text-brand-700"
            >
              刷新
            </button>
          </>
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

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {STAT_CARDS.map((card) => {
          const active = stateFilter === card.state;
          return (
            <button
              key={card.key}
              type="button"
              onClick={() => pickState(card.state)}
              className={`rounded-[12px] border px-4 py-3 text-left transition-colors ${
                active
                  ? "border-brand-400 bg-brand-50"
                  : "border-line bg-white hover:border-brand-200"
              }`}
            >
              <p className="text-caption text-slate-500">{card.label}</p>
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

      <div className="flex flex-wrap items-center gap-2 rounded-[12px] border border-line bg-white p-2.5">
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索全部错题 · 题目、知识点、解析" aria-label="搜索全部错题" className="h-9 min-w-[220px] flex-1 rounded-[8px] border border-line-strong px-3 text-body-sm outline-none focus:border-brand-400" />
        {kbs.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            {[{ id: null, name: "全部科目" }, ...kbs.map((k) => ({ id: k.id, name: k.name }))].map(
              (tab) => {
                const on = kbFilter === tab.id;
                return (
                  <button
                    key={tab.id ?? "__all__"}
                    type="button"
                    onClick={() => {
                      pickKb(tab.id);
                    }}
                    className={`rounded-[7px] border px-2.5 py-1.5 text-body-sm font-medium transition-colors ${
                      on
                        ? "border-indigo-300 bg-indigo-50 text-indigo-700"
                        : "border-slate-200 bg-white text-slate-500 hover:border-slate-300"
                    }`}
                  >
                    {tab.name}
                  </button>
                );
              },
            )}
          </div>
        )}
      </div>

      <Card title="错题洞察" extra={<span className="text-caption text-ink-muted">包含已归档记录 · 点击分布快速筛选</span>}>
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <Card
          title="错因分布"
          className="border-0"
          extra={
            causeFilter && (
              <button
                type="button"
                onClick={() => pickCause(causeFilter)}
                className="text-caption text-indigo-600 hover:underline"
              >
                清除筛选
              </button>
            )
          }
        >
          {stats && stats.total === 0 ? (
            <p className="py-3 text-center text-caption text-slate-400">暂无数据</p>
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
                      className="flex w-full items-center gap-2 rounded-lg px-1 py-0.5 text-caption transition-colors hover:bg-slate-50"
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

        <Card title="高频知识点" className="border-0">
          {!stats || stats.by_kc.length === 0 ? (
            <p className="py-3 text-center text-caption text-slate-400">暂无数据</p>
          ) : (
            <ul className="space-y-1.5">
              {stats.by_kc.map((kc) => (
                <li
                  key={kc.kc_id}
                  className="flex items-center justify-between gap-2 text-caption"
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
      </Card>

      {filtered && (
        <p className="flex flex-wrap items-center gap-2 text-caption text-slate-500">
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
          {unclassified && (
            <span className="rounded-full bg-slate-100 px-2 py-0.5 font-medium text-slate-600">
              未归类
            </span>
          )}
          <button
            type="button"
            onClick={() => {
              setStateFilter(null);
              setCauseFilter(null);
              if (unclassified) clearUnclassified();
              setPage(1);
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
              : "练习中的错题会自动收录，也可以拍照、上传文件或手动录入"
          }
          illustration="notebook"
        />
      ) : (
        <>
          <div className="flex items-center justify-between text-caption text-ink-muted"><span>{filtered || searchQuery ? "已应用筛选条件" : "未使用筛选条件"}</span><span>{list.total} 条结果</span></div>
          <ul className="space-y-2">
            {list.items.map((item) => {
              const busy = busyId === item.id;
              return (
                <li key={item.id} className="rounded-[12px] border border-line bg-white p-4">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2"><h2 className="truncate text-[13px] font-semibold text-ink">{item.question?.stem ? <MathText text={item.question.stem} singleLine /> : "原题已删除"}</h2><StateBadge state={item.state} />{item.verification_status === "pending" && <span className="rounded-full bg-warning-50 px-2 py-0.5 text-caption font-medium text-warning-700">答案待核验</span>}</div>
                        <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <label
                        className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-caption ring-1 ring-inset ${
                          (CAUSE_COLORS[item.cause] ?? CAUSE_COLOR_FALLBACK).badge
                        }`}
                      >
                        <span>错因</span>
                        <select
                          value={item.cause}
                          onChange={(e) => void changeCause(item, e.target.value)}
                          aria-label="修改错因"
                          className="bg-transparent text-caption font-medium text-current outline-none"
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
                          className="max-w-32 truncate rounded-full bg-brand-50 px-2 py-0.5 text-caption text-brand-700"
                        >
                          {kc}
                        </span>
                      ))}
                      {item.is_variant && (
                        <span className="rounded-full bg-brand-50 px-2 py-0.5 text-caption font-medium text-brand-700">
                          变式题
                        </span>
                      )}
                      <span className="text-caption text-ink-muted">{item.question?.source_type && item.question.source_type !== "generated" ? "手动收录" : `错 ${item.error_count} 次`}{item.next_due && ` · 下次 ${fmtRelative(item.next_due)}`}</span>
                        </div>
                      </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <button
                        type="button"
                        disabled={busy || item.verification_status !== "verified"}
                        onClick={() => void makeVariant(item)}
                        className="flex items-center gap-1.5 rounded-[7px] bg-brand-50 px-3 py-1.5 text-body-sm font-medium text-brand-700 hover:bg-brand-100 disabled:opacity-50"
                      >
                        {busy && <Spinner className="h-3.5 w-3.5" />}
                        变式练习
                      </button>
                      <button type="button" onClick={() => setSelectedItemId(item.id)} className="rounded-[7px] px-3 py-1.5 text-body-sm font-medium text-ink-secondary hover:bg-surface-muted">查看详情</button>
                    </div>
                    </div>
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
                }}
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
              >
                上一页
              </button>
              <span className="text-caption text-slate-500">
                第 {list.page} / {list.pages} 页 · 共 {list.total} 题
              </span>
              <button
                type="button"
                disabled={list.page >= list.pages}
                onClick={() => {
                  setPage((p) => p + 1);
                }}
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
              >
                下一页
              </button>
            </div>
          )}
        </>
      )}

      {selectedItem && (
        <div className="fixed inset-0 z-50 flex justify-end">
          <button type="button" aria-label="关闭错题详情" className="absolute inset-0 bg-[#1c1917]/35" onClick={() => setSelectedItemId(null)} />
          <aside className="relative flex h-full w-full max-w-[580px] flex-col border-l border-line bg-white shadow-floating">
            <header className="flex items-start justify-between border-b border-line px-5 py-4"><div><div className="flex items-center gap-2"><StateBadge state={selectedItem.state} /><span className="text-body-sm text-ink-muted">{selectedItem.kc_names.join(" · ") || "未关联知识点"}</span></div><h2 className="mt-2 text-[18px] font-semibold text-ink">错题详情</h2></div><button type="button" onClick={() => setSelectedItemId(null)} className="rounded-[7px] p-2 text-ink-muted hover:bg-surface-muted" aria-label="关闭">✕</button></header>
            <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-5">
              {selectedItem.verification_status === "pending" && <section className="rounded-[10px] border border-warning-200 bg-warning-50 p-4"><p className="text-body-sm font-medium text-warning-700">这道题的正确答案尚未核验</p><p className="mt-1 text-caption leading-4 text-warning-700">核验前不会进入今日复习、生成变式题或影响学习画像。</p></section>}
              {selectedItem.question ? <><section><p className="text-caption font-semibold text-ink-muted">题目</p><div className="mt-2 text-[14px] leading-6 text-ink"><MathText text={selectedItem.question.stem} /></div></section><div className="grid gap-3 sm:grid-cols-2"><div className="rounded-[10px] border border-danger-200 bg-danger-50 p-4"><p className="text-caption font-semibold text-danger-600">我的答案</p><div className="mt-2 text-body-sm text-ink-secondary"><MathText text={selectedItem.my_answer || "（未作答）"} /></div></div><div className={`rounded-[10px] border p-4 ${selectedItem.verification_status === "verified" ? "border-success-200 bg-success-50" : "border-warning-200 bg-warning-50"}`}><p className={`text-caption font-semibold ${selectedItem.verification_status === "verified" ? "text-success-600" : "text-warning-700"}`}>{selectedItem.verification_status === "verified" ? "正确答案" : "待核验答案"}</p><div className="mt-2 text-body-sm text-ink-secondary">{selectedItem.question.answer ? <MathText text={selectedItem.question.answer} /> : "尚未填写"}</div></div></div>{selectedItem.question.explanation && <section><p className="text-caption font-semibold text-ink-muted">解析</p><div className="mt-2 text-body-sm leading-6 text-ink-secondary"><ModelMarkdown text={selectedItem.question.explanation} /></div></section>}</> : <p className="text-body-sm text-ink-muted">原题已删除，历史记录仍保留。</p>}
              <section className="rounded-[10px] border border-line p-4"><div className="flex items-center justify-between"><span className="text-body-sm text-ink-muted">错因判定</span><label className="rounded-full bg-brand-50 px-2 py-1 text-body-sm text-brand-700"><select value={selectedItem.cause} onChange={(e) => void changeCause(selectedItem, e.target.value)} className="bg-transparent outline-none">{CAUSE_ORDER.map((cause) => <option key={cause} value={cause}>{CAUSE_LABELS[cause]}</option>)}</select></label></div></section>
              <div className="grid grid-cols-3 gap-3 text-center"><div className="rounded-[10px] border border-line p-3"><p className="text-caption text-ink-muted">累计错误</p><strong className="mt-1 block text-[15px] text-ink">{selectedItem.error_count} 次</strong></div><div className="rounded-[10px] border border-line p-3"><p className="text-caption text-ink-muted">加入日期</p><strong className="mt-1 block text-body-sm text-ink">{fmtDateTime(selectedItem.created_at).slice(0, 10)}</strong></div><div className="rounded-[10px] border border-line p-3"><p className="text-caption text-ink-muted">下次复习</p><strong className="mt-1 block text-body-sm text-warning-600">{selectedItem.next_due ? fmtRelative(selectedItem.next_due) : "未安排"}</strong></div></div>
            </div>
            <footer className="grid gap-2 border-t border-line p-5 sm:grid-cols-[1fr_1fr_auto]">{selectedItem.resource_id && <Link href={`/materials?resource_id=${encodeURIComponent(selectedItem.resource_id)}${selectedItem.resource_question_id ? `&question_id=${encodeURIComponent(selectedItem.resource_question_id)}` : ""}`} className="rounded-[8px] border border-brand-200 bg-brand-50 px-4 py-2.5 text-center text-body-sm font-medium text-brand-700">查看原题资料</Link>}{selectedItem.question?.source_type && selectedItem.question.source_type !== "generated" ? <button type="button" onClick={() => { setEditingItem(selectedItem); setImportOpen(true); }} className="rounded-[8px] bg-brand-600 px-4 py-2.5 text-body-sm font-medium text-white">{selectedItem.verification_status === "verified" ? "编辑题目" : "编辑并核验"}</button> : <button type="button" disabled={busyId === selectedItem.id || selectedItem.verification_status !== "verified"} onClick={() => void makeVariant(selectedItem)} className="rounded-[8px] bg-brand-600 px-4 py-2.5 text-body-sm font-medium text-white disabled:opacity-40">生成变式题去练习</button>}<Link href={`/tutor?error_id=${encodeURIComponent(selectedItem.id)}`} className="rounded-[8px] border border-line-strong px-4 py-2.5 text-center text-body-sm font-medium text-ink-secondary">带着这道题去辅导</Link>{selectedItem.state === "archived" ? <button type="button" disabled={busyId === selectedItem.id} onClick={() => void restore(selectedItem)} className="rounded-[8px] border border-brand-200 bg-brand-50 px-4 py-2.5 text-body-sm font-medium text-brand-700">恢复到复习</button> : <button type="button" disabled={busyId === selectedItem.id} onClick={() => void archive(selectedItem)} className="rounded-[8px] border border-line-strong px-4 py-2.5 text-body-sm text-ink-secondary">归档</button>}</footer>
          </aside>
        </div>
      )}
      {importOpen && (
        <ErrorImportDialog
          editItem={editingItem}
          knowledgeBases={kbs}
          onClose={() => {
            setImportOpen(false);
            setEditingItem(null);
          }}
          onSaved={(message) => {
            setImportOpen(false);
            setEditingItem(null);
            setSelectedItemId(null);
            setNotice(message);
            setReloadKey((key) => key + 1);
          }}
        />
      )}
    </div>
  );
}
