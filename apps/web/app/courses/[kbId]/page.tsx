"use client";

// 课程工作区（UI 重构阶段 5）：/courses/[kbId] 三 tab（懒加载，非激活不挂载）——
// 资料（原 materials 选中库视图，除指南）/ 指南（StudyGuidePanel 独占）/ 计划
// （该 kb 关联计划详情，复用 PlanDetailPanel）。同一内容只渲染一处：指南只在
// 「指南」tab。顶栏动态标题显示课程名（lib/page-title）。

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";

import { CourseMaterialsTab } from "@/components/courses/course-materials-tab";
import { Card, EmptyState, ErrorBanner, SkeletonList } from "@/components/common";
import { KcGraphPanel } from "@/components/kb/kc-graph-panel";
import { StudyGuidePanel } from "@/components/kb/study-guide";
import { PlanDetailPanel } from "@/components/plans/plan-detail-panel";
import { ApiError, apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { setPageTitle } from "@/lib/page-title";
import type { KbOut, PlanListItem } from "@/lib/types";

const TABS = [
  { key: "materials", label: "资料" },
  { key: "guide", label: "指南" },
  { key: "graph", label: "知识图" },
  { key: "plan", label: "计划" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

/** 最近访问课程（侧栏「课程」项展开用）；与 last-knowledge-base 旧键并存读取。 */
function rememberRecentCourse(userId: string, kbId: string) {
  const key = `suwen:recent-courses:${userId}`;
  try {
    const raw = window.localStorage.getItem(key);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    const list = Array.isArray(parsed)
      ? parsed.filter((id): id is string => typeof id === "string")
      : [];
    const next = [kbId, ...list.filter((id) => id !== kbId)].slice(0, 3);
    window.localStorage.setItem(key, JSON.stringify(next));
    window.dispatchEvent(new Event(`eduagents:storage:${key}`));
  } catch {
    // 存储不可用时静默（侧栏展开退回最近 1 个旧键）
  }
}

/** 计划 tab：该 kb_id 关联计划（优先进行中）的详情；发起表单留在 /plans 聚合壳。 */
function CoursePlanTab({ kbId }: { kbId: string }) {
  const [plan, setPlan] = useState<PlanListItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void apiJson<PlanListItem[]>("/api/plans")
      .then((list) => {
        if (!alive) return;
        const scoped = list.filter((item) => item.kb_id === kbId);
        setPlan(
          scoped.find((item) => item.status === "active") ?? scoped[0] ?? null,
        );
        setError(null);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setError(err instanceof ApiError ? err.message : "计划加载失败");
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [kbId]);

  if (loading) {
    return (
      <Card title="学习计划">
        <SkeletonList rows={4} />
      </Card>
    );
  }
  if (error) {
    return <ErrorBanner message={error} />;
  }
  if (!plan) {
    return (
      <EmptyState
        title="这门课还没有学习计划"
        description="到「学习计划」页填写目标，选择这门课作为范围即可生成。"
        illustration="map"
      >
        <Link href="/plans" className="mt-2 inline-flex h-9 items-center rounded-[8px] bg-ink px-4 text-[13px] font-semibold text-white">
          去制定学习计划
        </Link>
      </EmptyState>
    );
  }
  return (
    <PlanDetailPanel
      planId={plan.id}
      goal={plan.goal}
      status={plan.status}
      budgetHours={plan.budget_hours}
    />
  );
}

function CourseWorkspaceView() {
  const params = useParams<{ kbId: string }>();
  const kbId = params.kbId;
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user } = useAuth();
  const rawTab = searchParams.get("tab");
  const tab: TabKey =
    rawTab === "guide" || rawTab === "plan" || rawTab === "graph" ? rawTab : "materials";

  const [kb, setKb] = useState<KbOut | null>(null);
  const [kbError, setKbError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void apiJson<KbOut[]>("/api/kb")
      .then((list) => {
        if (!alive) return;
        const found = list.find((item) => item.id === kbId) ?? null;
        if (found) {
          setKb(found);
          setKbError(null);
        } else {
          // 库不存在（已删除或无权限）→ 回课程列表
          router.replace("/courses");
        }
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setKbError(err instanceof Error ? err.message : "课程加载失败");
      });
    return () => {
      alive = false;
    };
  }, [kbId, router]);

  // 顶栏动态标题 + 最近访问课程记录
  useEffect(() => {
    if (!kb) return;
    setPageTitle(kb.name);
    if (user?.id) rememberRecentCourse(user.id, kb.id);
    return () => setPageTitle(null);
  }, [kb, user?.id]);

  return (
    <div className="mx-auto w-full max-w-[1100px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      {kbError && <ErrorBanner message={kbError} />}
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <p className="text-caption text-ink-muted">课程</p>
          <div className="mt-2 flex items-center gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-success-50 text-[15px] font-semibold text-success-600">
              {kb?.name?.[0] ?? "课"}
            </span>
            <div className="min-w-0">
              <h1 className="truncate text-[21px] font-semibold tracking-[-0.03em] text-ink">{kb?.name ?? "课程"}</h1>
              <p className="mt-0.5 text-body-sm text-ink-muted">
                {kb
                  ? `${kb.subject === "general" ? "未设学科" : kb.subject}${kb.owner_type === "org" ? ` · ${kb.org_name ?? "机构"}` : ""}${kb.writable ? "" : " · 只读"}`
                  : "正在加载课程…"}
              </p>
            </div>
          </div>
        </div>
        <Link
          href={`/chat?new=1&kb=${encodeURIComponent(kbId)}`}
          className="h-9 rounded-[8px] bg-brand-600 px-4 text-body-sm font-medium text-white hover:bg-brand-800"
        >
          问这门课
        </Link>
      </header>

      <div className="flex gap-1" role="tablist" aria-label="课程工作区视图">
        {TABS.map(({ key, label }) => {
          const active = tab === key;
          return (
            <Link
              key={key}
              href={key === "materials" ? `/courses/${kbId}` : `/courses/${kbId}?tab=${key}`}
              role="tab"
              aria-current={active ? "page" : undefined}
              className={`rounded-[8px] px-4 py-2 text-body-sm font-medium transition-colors ${
                active ? "bg-ink text-white" : "text-ink-secondary hover:bg-white"
              }`}
            >
              {label}
            </Link>
          );
        })}
      </div>

      {tab === "materials" &&
        (kb ? (
          <CourseMaterialsTab kb={kb} />
        ) : (
          <Card title="学习资料">
            <SkeletonList rows={3} />
          </Card>
        ))}
      {tab === "guide" &&
        (kb ? (
          <Card title="学习指南" extra={<span className="text-caption text-ink-muted">基于已就绪资料生成</span>}>
            <StudyGuidePanel key={kb.id} kbId={kb.id} kbName={kb.name} writable={kb.writable} />
          </Card>
        ) : (
          <Card title="学习指南">
            <SkeletonList rows={3} />
          </Card>
        ))}
      {tab === "graph" && <KcGraphPanel key={kbId} kbId={kbId} />}

      {tab === "plan" && <CoursePlanTab kbId={kbId} />}
    </div>
  );
}

export default function CourseWorkspacePage() {
  return (
    <Suspense fallback={null}>
      <CourseWorkspaceView />
    </Suspense>
  );
}
