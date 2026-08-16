"use client";

// 本地检索评估看板（仅管理员）：把临时评测产物入库后集中展示。
// 上：导入 / 重跑（job 轮询）操作条；中：历史跑分表（可勾选 A/B 做 delta 对比）；
// 下：单次跑分详情展示 headline 指标卡、按 category 分桶柱和拒答正确率。
// 图表一律纯 SVG / 纯 CSS div，颜色只作辅助——每根柱都带文字数值与 title tooltip。
// 每张卡片底部固定标注 embedding/rerank、语料与黄金集版本。

import { useEffect, useState, type ReactNode } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  LoadingState,
  Spinner,
  fmtDateTime,
} from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type {
  EvalCompareResponse,
  EvalImportResponse,
  EvalJobInfo,
  EvalRunDetail,
  EvalRunJobStart,
  EvalRunListItem,
  EvalRunsResponse,
  RetrievalCategoryRow,
  RetrievalEvalMetrics,
  RetrievalRefusal,
} from "@/lib/types";
import * as Parts from "@/components/evals/evals-parts";

export default function EvalsPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  const [runs, setRuns] = useState<EvalRunListItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const [importing, setImporting] = useState(false);

  const [formOpen, setFormOpen] = useState(false);
  const [kbId, setKbId] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [golden, setGolden] = useState(Parts.DEFAULT_GOLDEN);
  const [label, setLabel] = useState("手动重跑");
  const [noSparse, setNoSparse] = useState(false);
  const [noRerank, setNoRerank] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<EvalJobInfo | null>(null);

  const [pickA, setPickA] = useState<string | null>(null);
  const [pickB, setPickB] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);

  // 列表加载
  useEffect(() => {
    if (!isAdmin) return;
    let alive = true;
    (async () => {
      try {
        const res = await apiJson<EvalRunsResponse>("/api/evals");
        if (!alive) return;
        setRuns(res.runs ?? []);
        setError(null);
      } catch (err) {
        if (!alive) return;
        setRuns([]);
        setError(err instanceof ApiError ? err.message : "评估列表加载失败");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [isAdmin, reloadKey]);

  // 重跑任务轮询：每 3s 一次，最多 200 次，complete/not_found 即停并刷新列表
  useEffect(() => {
    if (!jobId) return;
    let alive = true;
    let ticks = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      if (!alive) return;
      ticks += 1;
      try {
        const info = await apiJson<EvalJobInfo>(`/api/evals/jobs/${jobId}`);
        if (!alive) return;
        setJob(info);
        if (info.status === "complete" || info.status === "not_found") {
          setJobId(null);
          if (info.status === "not_found") {
            setBanner("评估任务已丢失（服务可能重启），请重新发起。");
          } else if (info.success === false) {
            setBanner(info.error || "评估任务执行失败。");
          } else {
            setOk("评估任务完成，已刷新历史跑分列表。");
          }
          setReloadKey((k) => k + 1);
          return;
        }
      } catch (err) {
        if (!alive) return;
        setJobId(null);
        setBanner(err instanceof ApiError ? err.message : "评估任务状态查询失败");
        return;
      }
      if (ticks >= Parts.POLL_MAX_TICKS) {
        setJobId(null);
        setBanner("评估任务轮询已达上限（200 次），请稍后手动刷新列表查看结果。");
        return;
      }
      timer = setTimeout(() => void poll(), Parts.POLL_INTERVAL_MS);
    };

    timer = setTimeout(() => void poll(), Parts.POLL_INTERVAL_MS);
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [jobId]);

  const doImport = async () => {
    if (importing) return;
    setImporting(true);
    setBanner(null);
    setOk(null);
    try {
      const res = await apiJson<EvalImportResponse>("/api/evals/import", {
        method: "POST",
      });
      const failed = (res.items ?? []).filter((i) => i.status === "error");
      setOk(
        `导入完成：新增 ${res.imported} 条，跳过 ${res.skipped} 条（内容哈希未变）` +
          (failed.length > 0 ? `，${failed.length} 条解析失败` : ""),
      );
      if (failed.length > 0) {
        setBanner(
          failed.map((f) => `${f.source}：${f.error ?? "解析失败"}`).join("；"),
        );
      }
      setReloadKey((k) => k + 1);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "导入评估产物失败");
    } finally {
      setImporting(false);
    }
  };

  const submitRun = async () => {
    if (submitting || !kbId.trim()) return;
    setSubmitting(true);
    setBanner(null);
    setOk(null);
    try {
      const started = await apiJson<EvalRunJobStart>("/api/evals/run", {
        body: {
          kb_id: kbId.trim(),
          ...(tenantId.trim() ? { tenant_id: tenantId.trim() } : {}),
          golden: golden.trim() || Parts.DEFAULT_GOLDEN,
          no_sparse: noSparse,
          no_rerank: noRerank,
          label: label.trim() || "手动重跑",
        },
      });
      setJob({
        job_id: started.job_id,
        status: (started.status as EvalJobInfo["status"]) ?? "queued",
        success: null,
        eval_run_id: null,
        error: null,
      });
      setJobId(started.job_id);
      setFormOpen(false);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "发起评估失败");
    } finally {
      setSubmitting(false);
    }
  };

  const togglePick = (slot: "A" | "B", id: string) => {
    if (slot === "A") {
      setPickA((prev) => (prev === id ? null : id));
      setPickB((prev) => (prev === id ? null : prev));
    } else {
      setPickB((prev) => (prev === id ? null : id));
      setPickA((prev) => (prev === id ? null : prev));
    }
  };

  if (!isAdmin) return <Parts.NoAccess />;

  return (
    <div className="mx-auto w-full max-w-6xl space-y-4 p-4">
      {/* 操作条 */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-slate-900">评估看板</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            展示的都是明确环境生成的评测产物，不做任何数字美化；没有新产物时不显示旧数字。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {job && <Parts.JobPill job={job} />}
          <button
            type="button"
            disabled={importing}
            onClick={() => void doImport()}
            className="flex items-center gap-2 rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {importing && <Spinner />}
            导入评估产物
          </button>
          <button
            type="button"
            onClick={() => setFormOpen((v) => !v)}
            className="rounded-xl bg-indigo-600 px-3.5 py-2 text-sm font-semibold text-white hover:bg-indigo-700"
          >
            {formOpen ? "收起" : "重新评估"}
          </button>
        </div>
      </div>

      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}
      {ok && <Parts.SuccessBanner message={ok} onClose={() => setOk(null)} />}
      {error && (
        <ErrorBanner
          message={error}
          onRetry={() => {
            setLoading(true);
            setError(null);
            setReloadKey((k) => k + 1);
          }}
        />
      )}

      {/* 内联重跑表单 */}
      {formOpen && (
        <Card title="重新跑一次检索评估">
          <div className="space-y-3">
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-xs text-slate-500">
                  kb_id（必填，被评估的知识库 UUID）
                </span>
                <input
                  value={kbId}
                  onChange={(e) => setKbId(e.target.value)}
                  placeholder="粘贴知识库 UUID"
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 font-mono text-sm text-slate-900 outline-none placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-slate-500">
                  黄金集路径（相对仓库根）
                </span>
                <input
                  value={golden}
                  onChange={(e) => setGolden(e.target.value)}
                  placeholder={Parts.DEFAULT_GOLDEN}
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 font-mono text-sm text-slate-900 outline-none placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-slate-500">
                  tenant_id（可选，跨租户评估时填）
                </span>
                <input
                  value={tenantId}
                  onChange={(e) => setTenantId(e.target.value)}
                  placeholder="留空则用当前租户"
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 font-mono text-sm text-slate-900 outline-none placeholder:font-sans placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-slate-500">本次跑分标签</span>
                <input
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder="手动重跑"
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-2 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={noSparse}
                  onChange={(e) => setNoSparse(e.target.checked)}
                  className="h-4 w-4 accent-indigo-600"
                />
                关闭稀疏检索（消融 BM25）
              </label>
              <label className="flex items-center gap-2 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={noRerank}
                  onChange={(e) => setNoRerank(e.target.checked)}
                  className="h-4 w-4 accent-indigo-600"
                />
                关闭重排（消融 rerank）
              </label>
              <div className="ml-auto flex items-center gap-2">
                {submitting && <Spinner />}
                <button
                  type="button"
                  disabled={submitting || !kbId.trim() || jobId !== null}
                  onClick={() => void submitRun()}
                  className="rounded-xl bg-indigo-600 px-3.5 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {jobId ? "已有任务在跑" : "发起评估"}
                </button>
              </div>
            </div>
            <p className="text-body-sm text-slate-400">
              发起后每 {Parts.POLL_INTERVAL_MS / 1000}s 轮询一次任务状态（最多{" "}
              {Parts.POLL_MAX_TICKS} 次），完成后自动刷新列表。
              消融开关会写进报告的 report_config，用于做 A/B 对比。
            </p>
          </div>
        </Card>
      )}

      {/* 历史跑分 */}
      <Card
        title="历史跑分"
        extra={
          <span className="text-xs text-slate-400">
            {runs ? `${runs.length} 次` : "—"} · 点击行查看详情
          </span>
        }
      >
        {loading ? (
          <LoadingState text="评估列表加载中…" />
        ) : !runs || runs.length === 0 ? (
          <EmptyState
            title="还没有任何评估记录"
            description="点击「导入评估产物」把临时评估目录中的新结果入库。"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-200 text-left text-sm">
              <thead>
                <tr className="border-b border-slate-100 text-xs text-slate-400">
                  <th className="py-2 pr-2 font-medium">对比</th>
                  <th className="py-2 pr-2 font-medium">时间</th>
                  <th className="py-2 pr-2 font-medium">类型</th>
                  <th className="py-2 pr-2 font-medium">标签</th>
                  <th className="py-2 pr-2 font-medium">黄金集版本</th>
                  <th className="py-2 pr-2 font-medium">关键指标</th>
                  <th className="py-2 font-medium">数据口径</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const active = detailId === r.id;
                  return (
                    <tr
                      key={r.id}
                      onClick={() => setDetailId(active ? null : r.id)}
                      className={`cursor-pointer border-b border-slate-50 align-top transition-colors ${
                        active ? "bg-indigo-50/60" : "hover:bg-slate-50"
                      }`}
                    >
                      <td className="py-2.5 pr-2" onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center gap-1">
                          {(["A", "B"] as const).map((slot) => {
                            const picked =
                              slot === "A" ? pickA === r.id : pickB === r.id;
                            return (
                              <button
                                key={slot}
                                type="button"
                                aria-pressed={picked}
                                onClick={() => togglePick(slot, r.id)}
                                title={`把该次跑分选为对比${slot}`}
                                className={`h-6 w-6 rounded-md border text-xs font-semibold transition-colors ${
                                  picked
                                    ? "border-indigo-600 bg-indigo-600 text-white"
                                    : "border-slate-300 bg-white text-slate-400 hover:bg-slate-100"
                                }`}
                              >
                                {slot}
                              </button>
                            );
                          })}
                        </div>
                      </td>
                      <td className="py-2.5 pr-2 text-xs whitespace-nowrap text-slate-600">
                        {fmtDateTime(r.at)}
                      </td>
                      <td className="py-2.5 pr-2">
                        <Parts.KindBadge />
                      </td>
                      <td className="py-2.5 pr-2">
                        <span className="text-sm font-medium text-slate-800">
                          {r.label}
                        </span>
                        <span
                          className="ml-1.5 font-mono text-body-sm text-slate-400"
                          title={r.source}
                        >
                          {r.source}
                        </span>
                      </td>
                      <td className="py-2.5 pr-2 font-mono text-xs whitespace-nowrap text-slate-500">
                        {r.golden_set_version ?? "—"}
                      </td>
                      <td className="py-2.5 pr-2">
                        {r.summary.length === 0 ? (
                          <span className="text-xs text-slate-400">—</span>
                        ) : (
                          <span className="flex flex-wrap gap-1">
                            {r.summary.map((s) => (
                              <span
                                key={s.key}
                                className="rounded-md bg-slate-100 px-1.5 py-0.5 font-mono text-body-sm text-slate-700"
                                title={`${s.key} = ${s.value}`}
                              >
                                {s.key} {s.display}
                              </span>
                            ))}
                          </span>
                        )}
                      </td>
                      <td className="py-2.5 text-body-sm text-slate-400">
                        {r.data_basis ?? "—"}
                        {r.corpus_version ? (
                          <>
                            <br />
                            {r.corpus_version}
                          </>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {(pickA || pickB) && (
          <p className="mt-3 text-xs text-slate-500">
            已选 A：
            <span className="font-mono">
              {pickA ? pickA.slice(0, 8) : "（未选）"}
            </span>
            　已选 B：
            <span className="font-mono">
              {pickB ? pickB.slice(0, 8) : "（未选）"}
            </span>
            {!(pickA && pickB) && "　—— 再选一个即可生成 delta 对比"}
            <button
              type="button"
              onClick={() => {
                setPickA(null);
                setPickB(null);
              }}
              className="ml-3 rounded-md border border-slate-300 px-2 py-0.5 text-body-sm text-slate-600 hover:bg-slate-100"
            >
              清空选择
            </button>
          </p>
        )}
      </Card>

      {pickA && pickB && (
        <Parts.CompareSection key={`${pickA}|${pickB}`} leftId={pickA} rightId={pickB} />
      )}

      {detailId && <Parts.RunDetailPanel key={detailId} id={detailId} />}
    </div>
  );
}
