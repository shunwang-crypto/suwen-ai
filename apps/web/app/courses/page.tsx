"use client";

// 课程列表（UI 重构阶段 5）：知识库 = 课程。每库一卡（名称/学科/机构徽章）+
// 「新建课程」；底部「未归类资料」文字链（?view=unclassified 承接原 materials 伪选项，
// 阶段 2 的 ?filter=unclassified 因后端无对应过滤参数而 SKIPPED，见实施报告）。

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore, type FormEvent } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  SkeletonList,
  Spinner,
} from "@/components/common";
import { ResourceLibrary } from "@/components/resources/resource-library";
import { ApiError, apiJson } from "@/lib/api";
import type { KbOut } from "@/lib/types";

const emptySubscribe = () => () => {};

export default function CoursesPage() {
  const router = useRouter();
  const [kbs, setKbs] = useState<KbOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newSubject, setNewSubject] = useState("");
  const [creating, setCreating] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  // SSR 快照为空串、客户端快照读真实 search（项目既有惯例，无需 Suspense）
  const search = useSyncExternalStore(
    emptySubscribe,
    () => window.location.search,
    () => "",
  );
  const showUnclassified = new URLSearchParams(search).get("view") === "unclassified";

  useEffect(() => {
    let alive = true;
    void apiJson<KbOut[]>("/api/kb")
      .then((list) => {
        if (!alive) return;
        setKbs(list);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setKbs((prev) => prev ?? []);
        setError(err instanceof ApiError ? err.message : "课程列表加载失败");
      });
    return () => {
      alive = false;
    };
  }, [reloadKey]);

  const createCourse = async (e: FormEvent) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name || creating) return;
    setCreating(true);
    setBanner(null);
    try {
      const kb = await apiJson<KbOut>("/api/kb", {
        body: { name, subject: newSubject.trim() || "general" },
      });
      setNewName("");
      setNewSubject("");
      setShowCreate(false);
      setReloadKey((k) => k + 1);
      // 新建后直达工作区（与原 materials 自动选中新库一致）
      router.push(`/courses/${kb.id}`);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "新建课程失败");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-[1100px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      {error && <ErrorBanner message={error} onRetry={() => setReloadKey((k) => k + 1)} />}
      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}

      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <p className="text-caption font-semibold uppercase tracking-[0.12em] text-ink-muted">课程</p>
          <h1 className="mt-1.5 text-[22px] font-semibold tracking-[-0.03em] text-ink">我的课程</h1>
          <p className="mt-0.5 text-body-sm text-ink-muted">每门课程对应一个资料空间：上传教材讲义，生成学习指南，安排学习计划。</p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreate((value) => !value)}
          className="h-9 rounded-[8px] bg-brand-600 px-4 text-body-sm font-medium text-white hover:bg-brand-800"
        >
          新建课程
        </button>
      </header>

      {showCreate && (
        <form onSubmit={createCourse} className="grid gap-2 rounded-[12px] border border-line bg-white p-4 sm:grid-cols-[minmax(0,1fr)_220px_auto]">
          <input value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={255} placeholder="课程名称（如：高数上）" className="h-9 rounded-[8px] border border-line-strong px-3 text-body-sm outline-none focus:border-brand-400" />
          <input value={newSubject} onChange={(e) => setNewSubject(e.target.value)} maxLength={64} placeholder="学科（可选）" className="h-9 rounded-[8px] border border-line-strong px-3 text-body-sm outline-none focus:border-brand-400" />
          <button type="submit" disabled={!newName.trim() || creating} className="flex h-9 items-center justify-center gap-1.5 rounded-[8px] bg-brand-600 px-4 text-body-sm font-medium text-white disabled:opacity-40">
            {creating && <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />}创建
          </button>
        </form>
      )}

      {showUnclassified ? (
        <ResourceLibrary scopeKbId="unclassified" />
      ) : kbs === null ? (
        <Card title="我的课程">
          <SkeletonList rows={4} />
        </Card>
      ) : kbs.length === 0 ? (
        <EmptyState
          title="建立第一门课程"
          description="新账号不会显示示例教材。创建课程后即可上传真实资料并生成学习指南。"
          illustration="docs"
        >
          <button type="button" onClick={() => setShowCreate(true)} className="mt-2 rounded-[8px] bg-brand-600 px-4 py-2 text-body-sm font-medium text-white">新建课程</button>
        </EmptyState>
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {kbs.map((kb) => (
            <li key={kb.id}>
              <Link
                href={`/courses/${kb.id}`}
                className="block h-full rounded-[12px] border border-line bg-white p-4 transition-colors hover:border-brand-200 hover:bg-brand-50/30"
              >
                <div className="flex items-center gap-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-success-50 text-[14px] font-semibold text-success-600">
                    {kb.name?.[0] ?? "课"}
                  </span>
                  <div className="min-w-0">
                    <p className="truncate text-[14px] font-semibold text-ink">{kb.name}</p>
                    <p className="mt-0.5 truncate text-caption text-ink-muted">
                      {kb.subject === "general" ? "未设学科" : kb.subject}
                      {!kb.writable && " · 只读"}
                    </p>
                  </div>
                </div>
                {kb.owner_type === "org" && (
                  <span className="mt-3 inline-flex items-center gap-1 rounded-full bg-warning-50 px-2 py-0.5 text-caption font-medium text-warning-700">
                    机构 · {kb.org_name ?? "共享课程"}
                  </span>
                )}
              </Link>
            </li>
          ))}
        </ul>
      )}

      <p className="pt-1 text-center text-caption text-ink-muted">
        还有未归类的题目资料？
        <Link
          href="/courses?view=unclassified"
          className="mx-0.5 text-brand-700 underline-offset-2 hover:underline"
        >
          查看未归类资料
        </Link>
      </p>
    </div>
  );
}
