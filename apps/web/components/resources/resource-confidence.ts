import type { ResourceQuestionOut } from "@/lib/types";

const CRITICAL = new Set(["stem", "detected_student_answer", "answer"]);
export const CONFIDENCE_REVIEW_FIELDS = [
  "question_no",
  "page_no",
  "type",
  "stem",
  "options",
  "detected_student_answer",
  "detected_reference_answer",
  "answer",
  "explanation",
  "kc_names",
  "difficulty",
  "points",
] as const;

export function fieldNeedsReview(question: ResourceQuestionOut, field: string): boolean {
  const applicable = question.field_confidence?.applicable_fields;
  if (Array.isArray(applicable) && !applicable.includes(field)) return false;
  const entry = question.field_confidence?.fields?.[field];
  if (!entry || entry.confidence == null) return true;
  if (entry.source === "model_reported" || entry.warnings.length > 0) return true;
  const threshold = CRITICAL.has(field) ? 0.9 : 0.85;
  return entry.confidence < threshold;
}

export function fieldIsHighConfidence(question: ResourceQuestionOut, field: string): boolean {
  return !fieldNeedsReview(question, field);
}

export function fieldsNeedingReview(question: ResourceQuestionOut): string[] {
  return CONFIDENCE_REVIEW_FIELDS.filter((field) => fieldNeedsReview(question, field));
}

export function eligibilityReasonLabel(reason: string): string {
  if (reason === "model_reported_not_calibrated") return "模型报告的分数尚未完成校准";
  if (reason.endsWith("_below_threshold")) return `${reason.replace(/_below_threshold$/, "")} 置信度不足`;
  if (reason.endsWith("_has_warnings")) return `${reason.replace(/_has_warnings$/, "")} 仍有识别提示`;
  return "仍有字段需要确认";
}
