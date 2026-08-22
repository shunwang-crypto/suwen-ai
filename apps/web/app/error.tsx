"use client";

// 路由级错误兜底（App Router `error.js` 约定，必须是客户端组件）。
// 注意：本版 Next 的恢复回调是 `unstable_retry`，不是老版的 `reset`——见
// node_modules/next/dist/docs/01-app/01-getting-started/10-error-handling.md。
// 防御式：回调不存在时退回硬刷新，保证按钮永远可用。

import { useEffect, useRef } from "react";
import Link from "next/link";

export default function RouteError({
  error,
  unstable_retry,
}: {
  error: Error & { digest?: string };
  unstable_retry?: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    console.error("[route-error]", error);
    headingRef.current?.focus();
  }, [error]);

  const retry = () => {
    if (typeof unstable_retry === "function") unstable_retry();
    else window.location.reload();
  };

  return (
    <div
      className="flex min-h-[60vh] flex-col items-center justify-center px-6 py-16 text-center"
      role="alert"
      aria-labelledby="route-error-title"
    >
      <div
        className="flex h-14 w-14 items-center justify-center rounded-card bg-surface-muted text-2xl"
        aria-hidden
      >
        ⚠️
      </div>
      <h1
        ref={headingRef}
        id="route-error-title"
        tabIndex={-1}
        className="mt-5 text-[22px] font-semibold tracking-[-0.03em] text-ink outline-none"
      >
        这个页面出了点问题
      </h1>
      <p className="mt-2 max-w-md text-body-sm leading-6 text-ink-secondary">
        刚才的操作触发了一个未预期的错误。如果你刚提交了内容或完成任务，请先确认结果，再决定是否重试；也可以返回今日继续。
      </p>
      <div className="mt-6 flex items-center gap-3">
        <button
          type="button"
          onClick={retry}
          className="h-10 rounded-full bg-brand-600 px-5 text-body-sm font-medium text-white transition-colors hover:bg-brand-700"
        >
          重试
        </button>
        <Link
          href="/today"
          className="h-10 rounded-full border border-line-strong bg-white px-5 text-body-sm font-medium leading-10 text-ink transition-colors hover:bg-surface-muted"
        >
          返回今日
        </Link>
      </div>
      {error.digest && (
        <p className="mt-6 text-caption text-ink-muted">错误编号：{error.digest}</p>
      )}
    </div>
  );
}
