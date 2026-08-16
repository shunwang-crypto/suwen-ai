"use client";

// 旧路由重定向（UI 重构阶段 2）：/quiz* → /practice?tab=quiz，原 query（topic/kb/count/
// difficulty/plan_id/task_id/weak/kc/n 八类参数）原样转发。组件已迁 components/practice/quiz-tab.tsx。

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function QuizRedirect() {
  const router = useRouter();
  useEffect(() => {
    const incoming = new URLSearchParams(window.location.search);
    incoming.set("tab", "quiz");
    router.replace(`/practice?${incoming.toString()}`);
  }, [router]);
  return null;
}
