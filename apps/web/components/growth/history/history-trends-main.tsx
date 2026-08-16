"use client";

// 学情报告（F7）：掌握度雷达（章节聚合轴 + 知识库切换 tab；轴数<5 或移动端降级条形图，
// 防轴标签重叠）+ 30 天趋势（答题数/正确率双系列，纯 SVG、两联面板共享横轴——不做双 Y 轴）
// + 薄弱点卡片（按科目分节，「红线/待巩固」两口径分开计数，节内 p 升序 top5 + 展开全部）
// + 可执行建议（带跳转）+ narrative 三段式诊断（统一由 ModelMarkdown 渲染）。

import { RadarPanel } from "./radar";
import { StatTiles } from "./stat-tiles";
import { TrendChart } from "./trend-chart";
import { WeakPointCard, groupWeakPoints } from "./weak-points";
import Link from "next/link";
import { useEffect, useState } from "react";

import {Card, EmptyState, ErrorBanner, PageHeader, SkeletonPage, Spinner} from "@/components/common";
import { RADAR_MAX_DIMS } from "@/components/radar";
import { ModelMarkdown } from "@/components/chat/model-markdown";
import { ApiError, apiJson } from "@/lib/api";
import type {KbOut, LearningReportResponse, ReportRadarItem, ReportWeakPoint} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

// ---- 后端可选字段与空值降级 ----

/** 章节聚合轴的下钻明细项（?kb_id= 时每轴附带）。 */
interface RadarAxisKcDetail {
  kc_id?: string;
  name?: string;
  p_mastery?: number | null;
}

/** 雷达轴：章节聚合模式附带 kc_count/weak_count/kcs；KC 模式与旧契约一致。 */
type RadarAxis = ReportRadarItem & {
  kc_count?: number | null;
  weak_count?: number | null;
  kcs?: RadarAxisKcDetail[] | null;
};

/** 薄弱点：分科目契约附带 subject；缺失时降级为单节展示。 */
type SubjectWeakPoint = ReportWeakPoint & { subject?: string | null };

/** 报告响应扩展：axes_kind（"chapter"=章节聚合轴）与「待巩固」阈值。 */
type ScaledReportResponse = LearningReportResponse & {
  axes_kind?: string | null;
  consolidate_threshold?: number | null;
  /** 待巩固档（红线以上、未达定向出题线），与 weak_points 同结构 */
  consolidate_points?: SubjectWeakPoint[] | null;
  report_status?: "pending" | "generating" | "completed" | "unavailable" | "failed";
  report_updated_at?: string | null;
  cached?: boolean;
};

/** 每节薄弱点默认展示条数（p 升序 top5，其余「展开全部」）。 */
const WEAK_TOP_N = 5;

/** 30 天趋势：上联答题数（柱），下联正确率（折线，固定 0–100% 刻度），共享横轴。 */
export function HistoryTrends() {
  const [report, setReport] = useState<ScaledReportResponse | null>(null);
  const [kbs, setKbs] = useState<KbOut[]>([]);
  const [kbId, setKbId] = useState("");
  const [loading, setLoading] = useState(true);
  const [diagnosisLoading, setDiagnosisLoading] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({});

  // 知识库列表（tab 数据源，A5 收敛）：失败只隐藏 tab，不影响报告主体
  const kbListRes = useApiResource<KbOut[]>("/api/kb");
  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达 → 科目 tab 数据源的单向同步 */
  useEffect(() => {
    if (kbListRes.data) setKbs(kbListRes.data);
  }, [kbListRes.data]);
  /* eslint-enable react-hooks/set-state-in-effect */

  useEffect(() => {
    let alive = true;
    (async () => {
      let hasVisibleReport = false;
      try {
        setRefreshError(null);
        let current = await apiJson<ScaledReportResponse>(
          `/api/reports/learning${kbId ? `?kb_id=${encodeURIComponent(kbId)}` : ""}`,
        );
        if (!alive) return;
        setReport(current);
        hasVisibleReport = true;
        setError(null);
        setLoading(false);

        // 已在生成时只恢复轮询，不重复提交同一份模型任务。
        setDiagnosisLoading(true);
        if (current.report_status !== "generating") {
          current = await apiJson<ScaledReportResponse>(
            `/api/reports/learning/refresh${kbId ? `?kb_id=${encodeURIComponent(kbId)}` : ""}`,
            { method: "POST" },
          );
          if (!alive) return;
          setReport(current);
        }

        let polls = 0;
        while (alive && current.report_status === "generating" && polls < 200) {
          await new Promise((resolve) => setTimeout(resolve, 3000));
          if (!alive) return;
          current = await apiJson<ScaledReportResponse>(
            `/api/reports/learning${kbId ? `?kb_id=${encodeURIComponent(kbId)}` : ""}`,
          );
          polls += 1;
          if (alive) setReport(current);
        }
        if (alive && current.report_status === "generating") {
          setRefreshError("诊断仍在后台生成，稍后返回本页即可查看");
        }
      } catch (err) {
        if (alive) {
          if (hasVisibleReport) {
            setRefreshError(
              err instanceof ApiError
                ? err.message
                : "后台更新失败，当前仍显示最近一次报告",
            );
          } else {
            setError(err instanceof ApiError ? err.message : "学情报告加载失败");
          }
        }
      } finally {
        if (alive) {
          setLoading(false);
          setDiagnosisLoading(false);
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [reloadKey, kbId]);

  const reload = () => {
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  const switchKb = (next: string) => {
    if (next === kbId || loading) return;
    setKbId(next);
    setExpandedGroups({});
    setLoading(true);
  };

  if (loading && !report) {
    return (
      <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
        <PageHeader
          title="学情报告"
          subtitle="正在读取最近一次报告，AI 诊断会在页面展示后单独更新"
        />
        <SkeletonPage />
      </div>
    );
  }

  if (error || !report) {
    return (
      <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
        <PageHeader title="学情报告" subtitle="最近一次报告暂时无法读取" />
        <ErrorBanner message={error ?? "学情报告加载失败"} onRetry={reload} />
      </div>
    );
  }

  const { trend, narrative, narrative_note } = report;
  const radarAxes = report.radar as RadarAxis[];
  // 后端两档分开下发（weak_points=红线 / consolidate_points=待巩固），此处合并后
  // 由 groupWeakPoints 按科目分节并各自计数；旧后端无 consolidate_points 时自动降级
  const weakPoints = [
    ...(report.weak_points as SubjectWeakPoint[]),
    ...((report.consolidate_points ?? []) as SubjectWeakPoint[]),
  ];
  // 两口径阈值：红线（后端 weak_threshold 唯一来源）与待巩固（契约新增，缺省 0.6）
  const redThreshold = report.weak_threshold ?? 0.4;
  const consolidateThreshold = report.consolidate_threshold ?? report.weak_threshold ?? 0.5;
  const redPct = Math.round(redThreshold * 100);
  const consolidatePct = Math.round(consolidateThreshold * 100);

  // 章节聚合轴判定：axes_kind 显式声明，或任一轴带聚合字段。
  const isChapterAxes =
    report.axes_kind === "chapter" ||
    radarAxes.some((a) => a.kc_count != null || (a.kcs?.length ?? 0) > 0);

  const weakGroups = groupWeakPoints(weakPoints, redThreshold);
  const redTotal = weakGroups.reduce((s, g) => s + g.red, 0);
  const consolidateTotal = weakGroups.reduce((s, g) => s + g.consolidate, 0);
  // subject 全缺失时是单节降级形态，不渲染科目标签
  const hasSubjectSections = weakGroups.some((g) => g.key !== "");

  const hasAnyData =
    radarAxes.length > 0 || weakPoints.length > 0 || trend.some((t) => t.attempts > 0);
  const switching = loading && report != null;
  const updatedLabel = report.report_updated_at
    ? new Date(report.report_updated_at).toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

  return (
    <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      <PageHeader
        title="学情报告"
        subtitle={
          <>
            近 30 天作答证据聚合：掌握度雷达、趋势、薄弱点归因与可执行建议
            {updatedLabel && ` · 更新于 ${updatedLabel}`}
            {diagnosisLoading && " · 正在后台检查更新"}
          </>
        }
        action={
          <button
            type="button"
            onClick={reload}
            disabled={loading || diagnosisLoading}
            className="rounded-[8px] bg-brand-600 px-3.5 py-1.5 text-body-sm font-medium text-white transition-colors hover:bg-brand-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading || diagnosisLoading ? "更新中…" : "刷新报告"}
          </button>
        }
      />

      {/* 知识库切换 tab：全部=全站聚合；选中某库时后端按 ?kb_id= 下发章节聚合轴 */}
      {kbs.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5" role="tablist" aria-label="按知识库查看报告">
          <span className="mr-0.5 text-caption text-slate-500">范围：</span>
          {[{ id: "", name: "全部", subject: "" }, ...kbs].map((kb) => {
            const active = kbId === kb.id;
            return (
              <button
                key={kb.id || "all"}
                type="button"
                role="tab"
                aria-selected={active}
                title={kb.subject || undefined}
                onClick={() => switchKb(kb.id)}
                className={`rounded-full border px-3 py-1 text-caption font-medium transition-colors ${
                  active
                    ? "border-indigo-600 bg-indigo-600 text-white"
                    : "border-slate-300 bg-white text-slate-600 hover:border-indigo-300 hover:text-indigo-600"
                }`}
              >
                {kb.name}
              </button>
            );
          })}
          {switching && <Spinner className="h-3.5 w-3.5" />}
        </div>
      )}

      <div className="flex flex-col gap-4">
        {!hasAnyData && (
          <div className="order-1 space-y-3">
            <EmptyState
              title="还没有足够的学习数据"
              description="完成几次练习或辅导后，这里会生成你的学情报告"
              illustration="chart"
            />
            <div className="flex justify-center gap-2">
              <Link href="/practice/session?mode=quiz&from=%2Fgrowth&topic=%E6%91%B8%E5%BA%95%E7%BB%83%E4%B9%A0" className="text-body-sm font-medium text-brand-700 underline-offset-2 hover:underline">开始摸底练习</Link>
              <Link href="/plans" className="text-body-sm font-medium text-ink-secondary underline-offset-2 hover:text-brand-700 hover:underline">制定学习计划</Link>
            </div>
          </div>
        )}

        {hasAnyData && (
          <div className="order-1"><StatTiles
            trend={trend}
            metrics={report.metrics}
            redCount={redTotal}
            consolidateCount={consolidateTotal}
            redPct={redPct}
            consolidatePct={consolidatePct}
          /></div>
        )}

        {hasAnyData && (
          <div className="order-3 grid gap-4 lg:grid-cols-2">
            <Card
              title="掌握度雷达"
              extra={
                <span className="text-caption text-slate-400">
                  {isChapterAxes
                    ? `章节聚合 · ${radarAxes.length} 轴`
                    : `按证据活动量 Top ${Math.min(radarAxes.length, RADAR_MAX_DIMS)}`}
                </span>
              }
            >
              <RadarPanel axes={radarAxes} isChapter={isChapterAxes} weakThreshold={redThreshold} />
            </Card>

            <Card title="近 30 天趋势">
              <TrendChart trend={trend} />
            </Card>
          </div>
        )}

        {hasAnyData && (
          <Card
            title="薄弱知识点"
            className="order-4"
            extra={
              <span className="text-caption text-slate-400">
                红线 &lt;{redPct}% · 待巩固 &lt;{consolidatePct}% · 附归因下钻
              </span>
            }
          >
            {weakPoints.length === 0 ? (
              <p className="py-3 text-center text-caption text-slate-400">
                暂无薄弱点，继续保持
              </p>
            ) : (
              <div className="space-y-5">
                {weakGroups.map((g) => {
                  const groupKey = g.key || "__all__";
                  const expanded = Boolean(expandedGroups[groupKey]);
                  const shown = expanded ? g.items : g.items.slice(0, WEAK_TOP_N);
                  const hidden = g.items.length - shown.length;
                  return (
                    <section key={groupKey}>
                      <header className="mb-2 flex flex-wrap items-center gap-2">
                        {hasSubjectSections && (
                          <span className="rounded-lg bg-indigo-50 px-2 py-0.5 text-caption font-semibold text-indigo-700">
                            {g.key || "未分科"}
                          </span>
                        )}
                        <span className="text-caption text-slate-500">
                          红线{" "}
                          <b className="font-semibold text-rose-600">{g.red}</b> 个 ·
                          待巩固{" "}
                          <b className="font-semibold text-amber-600">
                            {g.consolidate}
                          </b>{" "}
                          个
                        </span>
                      </header>
                      <ul className="grid gap-2.5 md:grid-cols-2">
                        {shown.map((wp) => (
                          <WeakPointCard
                            key={wp.kc_id}
                            wp={wp}
                            isRedline={(wp.p_mastery ?? 1) < redThreshold}
                            redPct={redPct}
                            consolidatePct={consolidatePct}
                          />
                        ))}
                      </ul>
                      {(hidden > 0 || expanded) && g.items.length > WEAK_TOP_N && (
                        <div className="mt-2 flex justify-center">
                          <button
                            type="button"
                            onClick={() =>
                              setExpandedGroups((prev) => ({
                                ...prev,
                                [groupKey]: !expanded,
                              }))
                            }
                            className="rounded-lg border border-slate-300 bg-white px-3 py-1 text-caption font-medium text-slate-600 hover:border-indigo-300 hover:text-indigo-600"
                          >
                            {expanded
                              ? "收起"
                              : `展开全部 ${g.items.length} 个（还有 ${hidden} 个）`}
                          </button>
                        </div>
                      )}
                    </section>
                  );
                })}
              </div>
            )}
          </Card>
        )}

        {hasAnyData && <Card
          title="AI 诊断（三段式）"
          className="order-2"
          extra={
            diagnosisLoading ? (
              <span className="flex items-center gap-1.5 text-caption text-ink-muted">
                <Spinner className="h-3.5 w-3.5" />
                后台更新中
              </span>
            ) : report.report_status === "completed" ? (
              <span className="text-caption text-success-600">已更新</span>
            ) : null
          }
        >
          {refreshError && (
            <p className="mb-3 rounded-control bg-amber-50 px-3 py-2 text-caption text-amber-700">
              {refreshError}
            </p>
          )}
          {narrative && narrative.trim() ? (
            <div className={diagnosisLoading ? "opacity-75" : ""}>
              <ModelMarkdown text={narrative} />
              {diagnosisLoading && (
                <p className="mt-3 text-caption text-ink-muted">
                  当前先显示最近一次诊断，更新完成后会自动替换。
                </p>
              )}
            </div>
          ) : diagnosisLoading ? (
            <div className="flex items-center gap-3 rounded-control bg-surface-muted px-3.5 py-4">
              <Spinner className="h-5 w-5 shrink-0" />
              <div>
                <p className="text-sm font-medium text-ink">正在生成最新诊断</p>
                <p className="mt-0.5 text-caption text-ink-muted">
                  雷达、趋势和行动建议已经可以查看，你可以继续浏览其它内容。
                </p>
              </div>
            </div>
          ) : (
            <p className="rounded-xl bg-slate-50 px-3.5 py-3 text-caption text-slate-500">
              {narrative_note ?? "暂无诊断"}
              {report.report_status === "unavailable" &&
                "：当前网关模式下不生成诊断文本，以上聚合数据均来自真实作答记录。"}
            </p>
          )}
        </Card>}
      </div>
    </div>
  );
}
