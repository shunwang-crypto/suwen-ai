import type { MasteryChange } from "./quiz";

// 与 apps/api/src/eduagents_api/schemas.py 及 docs/api-contract.md 对齐的类型定义。

// ---- 结构化学习资料与真实测评（0012，对齐 routers/resources.py/assessments.py）----

export type LearningResourceType =
  | "textbook"
  | "lecture"
  | "notes"
  | "exam_paper"
  | "mock_paper"
  | "workbook"
  | "worksheet"
  | "homework"
  | "single_question"
  | "other";

export type LearningResourceStatus =
  | "uploaded"
  | "extracting"
  | "reviewing"
  | "ready"
  | "failed"
  | "archived";

export type LearningResourceRetrievalPolicy = "none" | "reference" | "questions_only";

export interface LearningResourceOut {
  id: string;
  tenant_id: string;
  owner_type: "personal" | "organization";
  owner_id: string;
  resource_type: LearningResourceType;
  title: string;
  subject: string;
  grade: string | null;
  term: string | null;
  source_filename: string | null;
  filename: string | null;
  mime_type: string | null;
  size_bytes: number;
  storage_key: string | null;
  source_url: string | null;
  sha256: string | null;
  page_count: number | null;
  metadata: Record<string, unknown>;
  status: LearningResourceStatus;
  retrieval_policy: LearningResourceRetrievalPolicy;
  linked_kb_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  question_count: number;
  pending_question_count: number;
}

export type ResourceQuestionVerificationStatus = "pending" | "verified" | "rejected";

export interface ResourceQuestionFieldConfidence {
  confidence: number | null;
  source: "parser" | "ocr_provider" | "rule" | "model_reported" | "unknown" | "user_correction";
  warnings: string[];
}

export interface ResourceQuestionEligibility {
  schema_version: string;
  eligible: boolean;
  reasons: string[];
}

export interface ResourceQuestionOut {
  id: string;
  resource_question_id: string;
  question_id: string;
  ordinal: number;
  question_no: string | null;
  section_path: string | null;
  page_no: number | null;
  source_region: Record<string, unknown>;
  points: number | null;
  extraction_confidence: number | null;
  field_confidence?: {
    schema_version: string;
    fields: Record<string, ResourceQuestionFieldConfidence>;
    applicable_fields?: string[];
  };
  eligibility?: ResourceQuestionEligibility;
  verification_status: ResourceQuestionVerificationStatus;
  type: string;
  stem: string;
  options: string[];
  answer?: string;
  detected_student_answer: string;
  detected_reference_answer?: string;
  explanation?: string;
  kc_ids: string[];
  kc_names: string[];
  difficulty: string;
  is_error_selected: boolean;
  /** 已有 FSRS 复习或掌握度证据后，题型/选项/答案/知识点不可编辑。 */
  content_editable: boolean;
}

export interface ResourceConfirmOut {
  resource_id: string;
  status: LearningResourceStatus;
  error_item_ids: string[];
  confirmed_question_count: number;
}

export type AssessmentAttemptType =
  | "formal_exam"
  | "mock_exam"
  | "homework"
  | "practice"
  | "imported_record";

export type AssessmentAttemptStatus = "draft" | "submitted" | "graded";

export interface AssessmentAttemptOut {
  id: string;
  resource_id: string | null;
  student_id: string;
  attempt_type: AssessmentAttemptType;
  title: string;
  taken_at: string | null;
  duration_minutes: number | null;
  score: number | null;
  total_score: number | null;
  status: AssessmentAttemptStatus;
  grader_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface AssessmentAnswerOut {
  id: string;
  resource_question_id: string;
  question_id: string;
  question: {
    type: string;
    stem: string;
    options: string[];
    answer?: string;
    explanation?: string;
  };
  response: string;
  score: number | null;
  max_score: number | null;
  is_correct: boolean | null;
  grading_status: "pending" | "graded" | "verified";
  teacher_comment: string | null;
  created_at: string;
  updated_at: string;
}

export interface AssessmentDetailOut extends AssessmentAttemptOut {
  answers: AssessmentAnswerOut[];
  answers_revealed: boolean;
}

export interface AssessmentGradeOut {
  attempt_id: string;
  status: AssessmentAttemptStatus;
  score: number | null;
  total_score: number | null;
  mastery_changes: MasteryChange[];
}
