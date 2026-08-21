"use client";

// 侧栏「最近会话」区（UI 重构阶段 1）：一级导航下方展示最近 5 条对话会话。
// running/completed 指示沿用 chat 的会话状态键（同键 hook 自动同步）；
// 点击带 ?session= 进入 /chat。数据仅在组件挂载（= 侧栏可见且展开）时拉取，
// 模块级缓存避免移动端抽屉反复开关时重复请求；chat 页切换会话时通过
// rememberChatSession 派发的同页事件触发一次刷新。

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  CHAT_SESSION_STATUS_KEY,
  EMPTY_SESSION_INDICATORS,
  LAST_CHAT_SESSION_KEY,
  sessionIndicatorsCodec,
} from "@/app/chat/view-helpers";
import { apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import { useHydratedLocalStorage } from "@/lib/use-hydrated-local-storage";
import type { SessionOut } from "@/lib/types";

const RECENT_LIMIT = 5;

let cachedSessions: SessionOut[] | null = null;

export function RecentSessions({ onNavigate }: { onNavigate?: () => void }) {
  const [sessions, setSessions] = useState<SessionOut[] | null>(() => cachedSessions);
  const [indicators] = useHydratedLocalStorage(
    CHAT_SESSION_STATUS_KEY,
    EMPTY_SESSION_INDICATORS,
    sessionIndicatorsCodec,
  );

  const reload = useCallback(() => {
    void apiJson<SessionOut[]>("/api/chat/sessions")
      .then((list) => {
        cachedSessions = list.slice(0, RECENT_LIMIT);
        setSessions(cachedSessions);
      })
      .catch(() => {
        // 侧栏是辅助入口，失败静默（不占位、不打扰当前页面）。
        setSessions((prev) => prev ?? []);
      });
  }, []);

  useEffect(() => {
    if (!cachedSessions) reload();
    // chat 页每次切换/新建会话都会写 last-chat-session（rememberChatSession 派发同页事件）
    const onChange = () => {
      cachedSessions = null;
      reload();
    };
    window.addEventListener(`eduagents:storage:${LAST_CHAT_SESSION_KEY}`, onChange);
    return () => window.removeEventListener(`eduagents:storage:${LAST_CHAT_SESSION_KEY}`, onChange);
  }, [reload]);

  if (!sessions || sessions.length === 0) return null;

  return (
    <div className="mt-3 shrink-0 border-t border-line px-3 pb-1 pt-3">
      <h2 className="mb-1 px-2.5 text-caption font-medium tracking-[0.06em] text-[#a1a19d]">最近会话</h2>
      <ul className="space-y-0.5">
        {sessions.map((session) => {
          const indicator = indicators[session.id];
          return (
            <li key={session.id}>
              <Link
                href={`/chat?session=${session.id}`}
                onClick={onNavigate}
                title={session.title || "未命名会话"}
                className="flex h-8 items-center gap-2 rounded-control px-2.5 text-[13px] text-ink-secondary transition-colors hover:bg-surface-muted hover:text-ink"
              >
                <span className="flex min-w-0 flex-1 items-center gap-1">
                  <span className="min-w-0 truncate">
                    <MathText text={session.title || "未命名会话"} singleLine />
                  </span>
                  {session.kind === "tutor" && (
                    <span className="shrink-0 rounded-full bg-brand-50 px-1.5 py-0.5 text-caption font-medium text-brand-700">辅导</span>
                  )}
                </span>
                {indicator === "running" ? (
                  <span
                    className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-brand-100 border-t-brand-700"
                    role="img"
                    aria-label="正在生成回答"
                  />
                ) : indicator === "completed" ? (
                  <span className="h-2 w-2 shrink-0 rounded-full bg-indigo-500" aria-label="回答已完成" />
                ) : null}
              </Link>
            </li>
          );
        })}
      </ul>
      <Link
        href="/chat"
        onClick={onNavigate}
        className="mt-1 block rounded-control px-2.5 py-1 text-caption text-ink-muted transition-colors hover:text-ink-secondary"
      >
        查看全部 →
      </Link>
    </div>
  );
}
