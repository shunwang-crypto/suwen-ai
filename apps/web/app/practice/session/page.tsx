"use client";

// 专注会话入口（UI 重构阶段 3）：/practice/session?mode=review|quiz&from=<站内路径>&…业务参数。
// topic/kb/count/difficulty/plan_id/task_id/kc/n/weak 原样透传（QuizTab/ReviewTab 从
// window.location.search 读取，与路由无关）。from 经 safeInternalPath 校验（0.1-7）。

import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { FocusShell } from "@/components/focus/focus-shell";
import { QuizTab } from "@/components/practice/quiz-tab";
import { ReviewTab } from "@/components/practice/review-tab";
import { safeInternalPath } from "@/lib/safe-internal-path";

function FocusSessionView() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const mode = searchParams.get("mode");
  const from = safeInternalPath(searchParams.get("from"));

  useEffect(() => {
    if (mode !== "review" && mode !== "quiz") router.replace("/practice");
  }, [mode, router]);

  if (mode !== "review" && mode !== "quiz") return null;

  return (
    <FocusShell from={from}>
      {mode === "review" ? (
        <ReviewTab focus={{ from }} />
      ) : (
        <QuizTab focus={{ from }} />
      )}
    </FocusShell>
  );
}

export default function FocusSessionPage() {
  return (
    <Suspense fallback={null}>
      <FocusSessionView />
    </Suspense>
  );
}
