import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getPdfRecognitionSummary,
  ResourceReview,
} from "@/components/resources/resource-review";
import type { LearningResourceOut, ResourceQuestionOut } from "@/lib/types";

const { apiJson, apiFetch } = vi.hoisted(() => ({ apiJson: vi.fn(), apiFetch: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiJson, apiFetch };
});

function resource(overrides: Partial<LearningResourceOut> = {}): LearningResourceOut {
  return {
    id: "resource-1",
    tenant_id: "tenant-1",
    owner_type: "personal",
    owner_id: "user-1",
    resource_type: "exam_paper",
    title: "长文件名-试卷-2026.pdf",
    subject: "数学",
    grade: null,
    term: null,
    source_filename: "长文件名-试卷-2026.pdf",
    filename: "paper.pdf",
    mime_type: "application/pdf",
    size_bytes: 10,
    storage_key: null,
    source_url: null,
    sha256: null,
    page_count: 3,
    metadata: {
      failed_pages: [3],
      recognition: {
        page_count: 3,
        pages: [
          { page_number: 1, recognition_status: "ready" },
          { page_number: 2, recognition_status: "ready" },
          { page_number: 3, recognition_status: "ready" },
        ],
      },
    },
    status: "reviewing",
    retrieval_policy: "questions_only",
    linked_kb_id: null,
    created_by: "user-1",
    created_at: "2026-08-03T00:00:00Z",
    updated_at: "2026-08-03T00:00:00Z",
    question_count: 2,
    pending_question_count: 1,
    ...overrides,
  };
}

function question(id: string, page_no: number, verification_status: "pending" | "verified"): ResourceQuestionOut {
  return {
    id,
    resource_question_id: id,
    question_id: id,
    ordinal: page_no,
    question_no: String(page_no),
    section_path: null,
    page_no,
    source_region: {},
    points: null,
    extraction_confidence: 0.9,
    verification_status,
    type: "short",
    stem: "\\lim_{n \\to \\infty} \\frac{1}{n}",
    options: [],
    answer: "1",
    detected_student_answer: "",
    detected_reference_answer: "1",
    explanation: "\\sqrt{x}",
    kc_ids: [],
    kc_names: [],
    difficulty: "mid",
    is_error_selected: false,
    content_editable: true,
  };
}

describe("resource PDF summary and review display", () => {
  beforeEach(() => {
    apiJson.mockResolvedValue([]);
    apiFetch.mockRejectedValue(new Error("test source"));
  });

  it("counts pages, questions, verification state and failed pages", () => {
    const summary = getPdfRecognitionSummary(resource(), [question("q1", 1, "pending"), question("q2", 2, "verified")]);
    expect(summary).toMatchObject({
      isPdf: true,
      totalPages: 3,
      successfulPages: 2,
      recognizedQuestions: 2,
      pendingQuestions: 1,
      verifiedQuestions: 1,
      failedPages: [3],
    });
    expect(summary.questionsByPage).toEqual([{ page: 1, count: 1 }, { page: 2, count: 1 }]);
    expect(summary.pagesWithoutQuestions).toEqual([]);
  });

  it("does not produce PDF statistics for non-PDF resources", () => {
    expect(getPdfRecognitionSummary(resource({ mime_type: "text/plain", source_filename: "notes.txt" }), []).isPdf).toBe(false);
  });

  it("renders MathText previews and one compact failure card", () => {
    render(<ResourceReview resource={resource({ source_url: "/api/source" })} questions={[question("q1", 1, "pending"), question("q2", 2, "verified")]} onBack={() => {}} onReload={async () => {}} onResourceChanged={() => {}} onStartAssessment={() => {}} />);
    expect(screen.getByText("PDF 共 3 页")).toBeInTheDocument();
    expect(screen.getByText("失败页：3")).toBeInTheDocument();
    expect(screen.getByText("重新识别第 3 页")).toBeInTheDocument();
    expect(screen.getByText(/部分页面未识别.*已保留成功题目/)).toBeInTheDocument();
    expect(document.querySelectorAll(".katex").length).toBeGreaterThan(0);
  });

  it("does not render a failure card when there are no failed pages", () => {
    render(<ResourceReview resource={resource({ metadata: { recognition: { page_count: 3, pages: [] } } })} questions={[question("q1", 1, "pending")]} onBack={() => {}} onReload={async () => {}} onResourceChanged={() => {}} onStartAssessment={() => {}} />);
    expect(screen.queryByText(/失败页：/)).not.toBeInTheDocument();
    expect(screen.queryByText("部分页面未识别")).not.toBeInTheDocument();
  });
});
