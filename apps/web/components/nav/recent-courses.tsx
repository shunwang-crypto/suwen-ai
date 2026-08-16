"use client";

// 侧栏「课程」项下的最近课程展开（UI 重构阶段 5）：展示最近访问的 2-3 门课程，
// 数据来自 suwen:recent-courses:{userId}（课程工作区写入），兼容读取旧键
// suwen:last-knowledge-base:{userId}（阶段 5 之前的最近选中库）。折叠态不渲染。

import Link from "next/link";
import { useEffect, useState } from "react";

import { apiJson } from "@/lib/api";
import type { KbOut } from "@/lib/types";

let cachedKbs: KbOut[] | null = null;

function readRecentCourseIds(userId: string | undefined): string[] {
  if (typeof window === "undefined" || !userId) return [];
  const ids: string[] = [];
  try {
    const raw = window.localStorage.getItem(`suwen:recent-courses:${userId}`);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    if (Array.isArray(parsed)) {
      for (const id of parsed) {
        if (typeof id === "string") ids.push(id);
        if (ids.length >= 3) break;
      }
    }
  } catch {
    // 键损坏时回落旧键
  }
  if (ids.length === 0) {
    // 旧键：阶段 5 之前 materials 记录的最近选中库（单个 id）
    const legacy = window.localStorage.getItem(`suwen:last-knowledge-base:${userId}`);
    if (legacy) ids.push(legacy);
  }
  return ids;
}

export function RecentCourses({
  userId,
  pathname,
  onNavigate,
}: {
  userId: string | undefined;
  pathname: string;
  onNavigate?: () => void;
}) {
  // 初始化即读 localStorage（挂载时侧栏已在屏）；后续变化经同页事件同步
  const [ids, setIds] = useState<string[]>(() => readRecentCourseIds(userId));
  const [kbs, setKbs] = useState<KbOut[] | null>(cachedKbs);

  useEffect(() => {
    const key = `suwen:recent-courses:${userId}`;
    const onChange = () => setIds(readRecentCourseIds(userId));
    window.addEventListener(`eduagents:storage:${key}`, onChange);
    return () => window.removeEventListener(`eduagents:storage:${key}`, onChange);
  }, [userId]);

  useEffect(() => {
    if (cachedKbs) return;
    void apiJson<KbOut[]>("/api/kb")
      .then((list) => {
        cachedKbs = list;
        setKbs(list);
      })
      .catch(() => setKbs([]));
  }, []);

  if (!kbs || ids.length === 0) return null;
  const courses = ids
    .map((id) => kbs.find((kb) => kb.id === id))
    .filter((kb): kb is KbOut => Boolean(kb));
  if (courses.length === 0) return null;

  return (
    <ul className="mt-0.5 space-y-0.5">
      {courses.map((kb) => {
        const active = pathname === `/courses/${kb.id}` || pathname.startsWith(`/courses/${kb.id}/`);
        return (
          <li key={kb.id}>
            <Link
              href={`/courses/${kb.id}`}
              onClick={onNavigate}
              title={kb.name}
              className={`flex h-7 items-center gap-2 truncate rounded-[7px] py-1 pl-8 pr-2.5 text-[12.5px] transition-colors ${
                active
                  ? "bg-[#eeedea] font-medium text-ink"
                  : "text-ink-muted hover:bg-[#f4f3f0] hover:text-ink-secondary"
              }`}
            >
              <span className="truncate">{kb.name}</span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
