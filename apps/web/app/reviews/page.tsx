"use client";

// 旧路由重定向（UI 重构阶段 2）：/reviews → /practice?tab=review；
// /reviews?view=errors → /practice?tab=errors。plan_id/task_id 归因参数原样转发。

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function ReviewsRedirect() {
  const router = useRouter();
  useEffect(() => {
    const incoming = new URLSearchParams(window.location.search);
    const tab = incoming.get("view") === "errors" ? "errors" : "review";
    incoming.delete("view");
    incoming.set("tab", tab);
    router.replace(`/practice?${incoming.toString()}`);
  }, [router]);
  return null;
}
