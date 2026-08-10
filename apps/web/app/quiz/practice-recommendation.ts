import type { PracticeRecommendation } from "@/lib/types";

type Adjustment = PracticeRecommendation["adjustments"][number];

const ADJUSTMENT_LABELS: Record<Adjustment, string> = {
  easier: "简单一点",
  harder: "难一点",
  switch_kc: "换个知识点",
  switch_kb: "换个科目",
};

export function adjustmentLabels(
  adjustments: readonly Adjustment[],
): Array<readonly [Adjustment, string]> {
  return adjustments.map((value) => [value, ADJUSTMENT_LABELS[value]] as const);
}

export function recommendationStatusMessage(
  recommendation: PracticeRecommendation,
): string {
  if (recommendation.status === "preparing") {
    return "正在理解资料并整理知识点";
  }
  if (recommendation.status === "needs_scope") {
    return "请选择这次要练的科目";
  }
  if (recommendation.status === "needs_upload") {
    return "请先上传一份正在学的资料";
  }
  return recommendation.reason;
}

export function recommendationScopeRequest(scopeKbId: string): Record<string, string> {
  return { scope_kb_id: scopeKbId };
}
