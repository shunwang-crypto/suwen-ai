"use client";

// 旧路由重定向（UI 重构阶段 5）：/materials* → /courses。知识库=课程，选中库视图
// 已迁 /courses/[kbId]。query 转发规则：
//   ?kb=X            → /courses/X
//   ?view=unclassified → /courses?view=unclassified
//   ?resource_id=R   → 需查资源归属库（数据依赖），client-side 解析后 replace：
//     linked_kb_id 有值 → /courses/{kb}?resource_id=&question_id=；无值 → 未归类视图
//   其余             → /courses
// （/plans?id=X 的同类解析在 /plans 页内做，实施指令 5.5 明确不做服务端重定向。）

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { apiJson } from "@/lib/api";

export default function MaterialsRedirect() {
  const router = useRouter();

  useEffect(() => {
    const incoming = new URLSearchParams(window.location.search);
    const kbId = incoming.get("kb");
    const view = incoming.get("view");
    const resourceId = incoming.get("resource_id");

    const dropHandled = (params: URLSearchParams) => {
      params.delete("kb");
      params.delete("view");
      return params;
    };

    if (kbId) {
      dropHandled(incoming);
      router.replace(`/courses/${kbId}${incoming.toString() ? `?${incoming.toString()}` : ""}`);
      return;
    }
    if (view === "unclassified") {
      dropHandled(incoming);
      router.replace(`/courses?view=unclassified${incoming.toString() ? `&${incoming.toString()}` : ""}`);
      return;
    }
    if (resourceId) {
      void (async () => {
        let linkedKb: string | null = null;
        try {
          const resource = await apiJson<{ linked_kb_id: string | null }>(
            `/api/resources/${resourceId}`,
          );
          linkedKb = resource.linked_kb_id;
        } catch {
          linkedKb = null;
        }
        dropHandled(incoming);
        const rest = incoming.toString();
        if (linkedKb) {
          router.replace(`/courses/${linkedKb}${rest ? `?${rest}` : ""}`);
        } else {
          const params = new URLSearchParams(rest);
          params.set("view", "unclassified");
          router.replace(`/courses?${params.toString()}`);
        }
      })();
      return;
    }
    router.replace("/courses");
  }, [router]);

  return null;
}
