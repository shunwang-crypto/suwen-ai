"use client";

// F8-A2 主动开场卡：新会话首屏展示可解释的「下一步」推荐（GET /api/next-actions）。
// 每条卡片 = 图标 + 标题 + 「为什么推它」理由，点击跳转对应功能页；
// 有 resume_insight 时置顶为「上次你在『X』卡住」高亮样式（主动关联历史卡点）。
// 加载失败/无数据一律静默不渲染 —— 开场卡绝不能拖垮对话页。

import { useRouter } from "next/navigation";
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";

import { apiJson } from "@/lib/api";

/** 局部类型：共享 lib/types.ts 冻结不动（P1 先例），组件内自定义响应形状。 */
interface NextActionItem {
  type: string;
  title: string;
  reason: string;
  href: string;
}

interface NextActionsResponse {
  items: NextActionItem[];
}

function greeting(): string {
  const h = new Date().getHours();
  if (h < 6) return "夜深了";
  if (h < 12) return "早上好";
  if (h < 18) return "下午好";
  return "晚上好";
}

/** 各推荐类型的图标（stroke 线条风格，与全站 SVG 图标一致）与底色。 */
const TYPE_META: Record<string, { chip: string; icon: ReactNode }> = {
  review: {
    chip: "bg-amber-50 text-amber-600",
    icon: (
      // 循环箭头：间隔复习
      <path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" />
    ),
  },
  weak_practice: {
    chip: "bg-rose-50 text-rose-600",
    icon: (
      // 靶心：定向练薄弱点
      <>
        <circle cx="12" cy="12" r="9" />
        <circle cx="12" cy="12" r="4.5" />
        <circle cx="12" cy="12" r="0.5" />
      </>
    ),
  },
  plan_task: {
    chip: "bg-emerald-50 text-emerald-600",
    icon: (
      // 日历勾选：今日计划任务
      <>
        <rect x="3" y="5" width="18" height="16" rx="2" />
        <path d="M8 3v4M16 3v4M3 10h18M9 15l2 2 4-4" />
      </>
    ),
  },
  quiz: {
    chip: "bg-indigo-50 text-indigo-600",
    icon: (
      // 铅笔：摸底测评
      <path d="m17 3 4 4L8 20l-5 1 1-5L17 3Z" />
    ),
  },
  plan: {
    chip: "bg-emerald-50 text-emerald-600",
    icon: (
      // 路线图：制定学习计划
      <path d="M9 20 3 17V4l6 3 6-3 6 3v13l-6-3-6 3ZM9 7v13M15 4v13" />
    ),
  },
};

const FALLBACK_META = TYPE_META.quiz;

/**
 * 主动开场卡（F8-A2）。只在新会话（消息为空）时由 /chat 首屏渲染，
 * 会话一旦有消息即由父组件卸载隐藏。
 */
export function OpeningCard() {
  const router = useRouter();
  const [items, setItems] = useState<NextActionItem[] | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await apiJson<NextActionsResponse>("/api/next-actions");
        if (alive && Array.isArray(data?.items) && data.items.length > 0) {
          setItems(data.items);
        }
      } catch {
        // 静默：推荐加载失败不渲染，不影响正常对话
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (!items) return null;

  // 有 resume_insight 时首条置顶为「上次你在『X』卡住」高亮卡
  const resume = items.find((it) => it.type === "resume_insight") ?? null;
  const rest = items.filter((it) => it !== resume);

  return (
    <div className="fx-fade-up w-full max-w-xl text-left">
      <p className="text-sm font-medium text-slate-600">
        {greeting()}！为你找了几件现在就能做的事：
      </p>

      {resume && (
        <button
          type="button"
          onClick={() => router.push(resume.href)}
          className="mt-3 flex w-full items-start gap-3 rounded-2xl border border-indigo-200 bg-gradient-to-r from-indigo-50 to-violet-50 p-4 text-left shadow-sm transition-colors hover:border-indigo-300 hover:from-indigo-100/70"
        >
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-white text-indigo-600 shadow-sm">
            {/* 对话气泡 + 问号：接着上次的卡点继续 */}
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-5 w-5"
              aria-hidden
            >
              <path d="M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z" />
              <path d="M10.5 10a2 2 0 1 1 2.5 1.9c-.6.2-1 .6-1 1.1v.3M12 16.5h.01" />
            </svg>
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-sm font-semibold text-indigo-900">
              {resume.title}
            </span>
            <span className="mt-1 block text-xs leading-relaxed text-indigo-600/90">
              {resume.reason}
            </span>
          </span>
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="mt-0.5 h-4 w-4 shrink-0 text-indigo-400"
            aria-hidden
          >
            <path d="m9 18 6-6-6-6" />
          </svg>
        </button>
      )}

      {rest.length > 0 && (
        <div className="mt-3 grid gap-2">
          {rest.map((it, i) => {
            const meta = TYPE_META[it.type] ?? FALLBACK_META;
            return (
              <button
                key={`${it.type}-${it.title}`}
                type="button"
                onClick={() => router.push(it.href)}
                style={{ "--fx-delay": `${120 + i * 80}ms` } as CSSProperties}
                className="fx-chip-in flex w-full items-start gap-3 rounded-xl border border-slate-200 bg-white p-3 text-left transition-colors hover:border-indigo-300 hover:bg-indigo-50/40"
              >
                <span
                  className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${meta.chip}`}
                >
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="h-4.5 w-4.5"
                    aria-hidden
                  >
                    {meta.icon}
                  </svg>
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-slate-800">
                    {it.title}
                  </span>
                  <span className="mt-0.5 block text-xs leading-relaxed text-slate-500">
                    {it.reason}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
