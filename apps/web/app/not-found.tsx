// 404 兜底（App Router `not-found.js` 约定）。在根布局内渲染，可用设计 token。

import Link from "next/link";

export default function NotFound() {
  return (
    <main
      className="flex min-h-[60vh] flex-col items-center justify-center px-6 py-16 text-center"
      aria-labelledby="not-found-title"
    >
      <p className="text-[52px] font-semibold leading-none tracking-[-0.04em] text-ink-muted">
        404
      </p>
      <h1 id="not-found-title" className="mt-4 text-[22px] font-semibold tracking-[-0.03em] text-ink">
        找不到这个页面
      </h1>
      <p className="mt-2 max-w-md text-body-sm leading-6 text-ink-secondary">
        页面可能已被移动或删除，链接也可能不正确。
      </p>
      <div className="mt-6 flex items-center gap-3">
        <Link
          href="/today"
          className="h-10 rounded-full bg-brand-600 px-5 text-body-sm font-medium leading-10 text-white transition-colors hover:bg-brand-700"
        >
          返回今日
        </Link>
        <Link
          href="/chat"
          className="h-10 rounded-full border border-line-strong bg-white px-5 text-body-sm font-medium leading-10 text-ink transition-colors hover:bg-surface-muted"
        >
          去问苏问
        </Link>
      </div>
    </main>
  );
}
