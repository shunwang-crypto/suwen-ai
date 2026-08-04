"use client";

// F8-A2 主动开场卡：新会话首屏展示可解释的「下一步」推荐（GET /api/next-actions）。
// 每条卡片 = 图标 + 标题 + 「为什么推它」理由，点击跳转对应功能页；
// 有 resume_insight 时置顶为「上次你在『X』卡住」高亮样式（主动关联历史卡点）。
// 加载失败/无数据一律静默不渲染 —— 开场卡绝不能拖垮对话页。

import { useRouter } from "next/navigation";
import { useEffect, useState, type CSSProperties } from "react";

import { apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";

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
  const ordered = (resume ? [resume, ...rest] : rest).slice(0, 3);

  return (
    <div className="fx-fade-up w-full max-w-[640px] text-left">
      <p className="mb-3 text-center text-xs text-slate-400">建议下一步</p>
      <div className="overflow-hidden rounded-overlay border border-slate-200 bg-white">
        {ordered.map((item, index) => (
          <button
            key={`${item.type}-${item.title}`}
            type="button"
            onClick={() => router.push(item.href)}
            style={{ "--fx-delay": `${120 + index * 80}ms` } as CSSProperties}
            className="fx-chip-in flex h-16 w-full items-center gap-3 border-t border-slate-200 px-3.5 text-left first:border-t-0 hover:bg-slate-50"
          >
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-control bg-brand-50 text-[13px] font-medium text-brand-700">
              {index + 1}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] font-medium text-slate-900">
                <MathText text={item.title} singleLine />
              </span>
              <span className="mt-0.5 block truncate text-xs text-slate-400">
                <MathText text={item.reason} singleLine />
              </span>
            </span>
            <span className="text-sm text-slate-400" aria-hidden>
              →
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
