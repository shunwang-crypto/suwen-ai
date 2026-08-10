import Link from "next/link";

import type { TutorEntrypointsOut } from "@/lib/types";

export function tutorTaskHref(
  task: Extract<TutorEntrypointsOut["today_task"], { status: "ready" }>,
): string {
  return `/tutor?plan_id=${encodeURIComponent(task.plan_id)}&task_key=${encodeURIComponent(task.task_key)}`;
}

export function tutorErrorHref(errorId: string): string {
  return `/tutor?error_id=${encodeURIComponent(errorId)}`;
}

export function TutorEntrypoints({
  data,
  loading = false,
  onChooseNew,
}: {
  data: TutorEntrypointsOut | null;
  loading?: boolean;
  onChooseNew: () => void;
}) {
  const todayReady = data?.today_task.status === "ready" ? data.today_task : null;
  const errorIds = data?.errors.status === "ready" ? data.errors.error_ids : [];
  return (
    <section className="mt-3 grid gap-2 sm:grid-cols-3" aria-label="辅导入口">
      <div className="rounded-[9px] border border-brand-200 bg-brand-50 px-3 py-3 text-left">
        {todayReady ? (
          <Link href={tutorTaskHref(todayReady)} aria-label="继续今天的任务" className="block hover:text-brand-800">
            <span className="block text-[13px] font-semibold text-brand-800">继续今天的任务</span>
            <span className="mt-1 block text-body-sm leading-5 text-brand-700">使用计划任务安全上下文进入辅导</span>
          </Link>
        ) : (
          <>
            <span className="block text-[13px] font-semibold text-brand-800">继续今天的任务</span>
            <span className="mt-1 block text-body-sm leading-5 text-brand-700">
              {loading ? "正在查找今日任务…" : "今天没有待完成的辅导任务"}
            </span>
          </>
        )}
      </div>
      <div className="rounded-[9px] border border-line-strong bg-white px-3 py-3 text-left">
        <span className="block text-[13px] font-semibold text-ink">挑一道我的错题</span>
        {errorIds.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {errorIds.map((errorId, index) => (
              <Link
                key={errorId}
                href={tutorErrorHref(errorId)}
                className="rounded-full border border-brand-100 bg-brand-50 px-2.5 py-1 text-body-sm font-medium text-brand-700 hover:border-brand-300"
              >
                错题 {index + 1}
              </Link>
            ))}
          </div>
        ) : (
          <span className="mt-1 block text-body-sm leading-5 text-ink-muted">
            {loading ? "正在查找可用错题…" : "还没有可用错题"}
          </span>
        )}
      </div>
      <button
        type="button"
        aria-label="粘贴或上传新题目"
        onClick={onChooseNew}
        className="rounded-[9px] border border-line-strong bg-white px-3 py-3 text-left hover:border-brand-300"
      >
        <span className="block text-[13px] font-semibold text-ink">粘贴或上传新题目</span>
        <span className="mt-1 block text-body-sm leading-5 text-ink-muted">支持文字、图片、PDF 和 Office 文件</span>
      </button>
    </section>
  );
}
