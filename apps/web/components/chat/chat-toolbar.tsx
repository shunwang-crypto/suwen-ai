"use client";

// 对话页顶部工具栏（A 瘦身自 page.tsx 拆出）：
// 展开历史（桌面折叠态）/ 打开会话列表（移动端）/ 当前会话标题 /
// 引用入口（移动与窄屏，桌面由右侧常驻面板承担）。

export function ChatToolbar({
  historyCollapsed,
  onExpandHistory,
  onOpenSessions,
  title,
  citationCount,
  onOpenCitations,
}: {
  historyCollapsed: boolean;
  onExpandHistory: () => void;
  onOpenSessions: () => void;
  title: string;
  citationCount: number;
  onOpenCitations: () => void;
}) {
  return (
    <div className="flex h-14 shrink-0 items-center gap-3 border-b border-line bg-canvas px-5">
      {historyCollapsed && (
        <button
          type="button"
          onClick={onExpandHistory}
          className="hidden h-8 w-8 items-center justify-center rounded-lg text-ink-secondary hover:bg-surface-muted hover:text-ink md:flex"
          aria-label="展开历史对话"
          title="展开历史对话"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className="h-[18px] w-[18px]" aria-hidden>
            <rect x="3" y="4" width="18" height="16" rx="3" />
            <path d="M9 4v16" />
          </svg>
        </button>
      )}
      <button
        type="button"
        onClick={onOpenSessions}
        className="rounded-lg p-1.5 text-ink-muted hover:bg-surface-muted md:hidden"
        aria-label="打开会话列表"
      >
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
          <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />
        </svg>
      </button>

      <h1 className="min-w-0 truncate text-sm font-semibold text-ink">
        {title}
      </h1>

      <span className="flex-1" />

      {citationCount > 0 && (
        <button
          type="button"
          onClick={onOpenCitations}
          className="relative rounded-control p-1.5 text-ink-muted hover:bg-surface-muted xl:hidden"
          aria-label="打开引用面板"
        >
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
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6" />
          </svg>
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-indigo-600 px-1 text-caption font-semibold text-white">
            {citationCount}
          </span>
        </button>
      )}
    </div>
  );
}
