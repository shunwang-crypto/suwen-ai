"use client";

// 练习中心（UI 重构阶段 2）：练一练（原 /quiz 全量）/ 到期复习（原 /reviews 队列）/
// 错题本（原 ErrorLibrary）合并为顶部三 tab。tab 状态在 URL（?tab=quiz|review|errors，
// 默认 quiz），非激活 tab 不挂载——quiz 草稿轮询与复习队列请求互不干扰、首屏只发激活 tab 的请求。

import Link from "next/link";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";

import { QuizTab } from "@/components/practice/quiz-tab";
import { ReviewTab } from "@/components/practice/review-tab";
import { ErrorLibrary } from "@/components/reviews/error-library";

const TABS = [
  { key: "quiz", label: "练一练" },
  { key: "review", label: "到期复习" },
  { key: "errors", label: "错题本" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

function PracticeView() {
  const searchParams = useSearchParams();
  const raw = searchParams.get("tab");
  const tab: TabKey = raw === "review" || raw === "errors" ? raw : "quiz";

  return (
    <div>
      <div
        className="mx-auto flex w-full max-w-[1120px] gap-1 px-5 pt-5 sm:px-7 lg:px-8 lg:pt-6"
        role="tablist"
        aria-label="练习中心视图"
      >
        {TABS.map(({ key, label }) => {
          const active = tab === key;
          return (
            <Link
              key={key}
              href={key === "quiz" ? "/practice" : `/practice?tab=${key}`}
              role="tab"
              aria-current={active ? "page" : undefined}
              className={`rounded-control px-4 py-2 text-body-sm font-medium transition-colors ${
                active
                  ? "bg-ink text-white"
                  : "text-ink-secondary hover:bg-white"
              }`}
            >
              {label}
            </Link>
          );
        })}
      </div>
      {tab === "quiz" && <QuizTab />}
      {tab === "review" && <ReviewTab />}
      {tab === "errors" && <ErrorLibrary />}
    </div>
  );
}

export default function PracticePage() {
  return (
    <Suspense fallback={null}>
      <PracticeView />
    </Suspense>
  );
}
