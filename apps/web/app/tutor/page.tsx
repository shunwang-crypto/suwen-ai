"use client";

// 旧路由重定向（UI 重构阶段 6）：辅导是对话页里的一种会话类型（kind='tutor'）。
// /tutor*（problem/error_id/kb/draft/plan_id/task_id 全参数）→ /chat?tutor=1&<原 query>，
// 由 chat 页的辅导工作区承接。全站「带题去辅导」类跳转无需逐个改写，经本 shim 等价直达。

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function TutorRedirect() {
  const router = useRouter();
  useEffect(() => {
    const incoming = new URLSearchParams(window.location.search);
    incoming.set("tutor", "1");
    router.replace(`/chat?${incoming.toString()}`);
  }, [router]);
  return null;
}
