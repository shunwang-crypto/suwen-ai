import { describe, expect, it } from "vitest";

import {
  fieldIsHighConfidence,
  fieldNeedsReview,
  fieldsNeedingReview,
} from "./resource-confidence";
import type { ResourceQuestionOut } from "@/lib/types";

const question = {
  field_confidence: {
    schema_version: "v1",
    fields: {
      stem: { confidence: 0.95, source: "ocr_provider", warnings: [] },
      question_no: { confidence: 0.85, source: "parser", warnings: [] },
      answer: { confidence: null, source: "unknown", warnings: [] },
    },
  },
} as unknown as ResourceQuestionOut;

function highConfidenceQuestion(overrides: Record<string, unknown> = {}): ResourceQuestionOut {
  const entry = { confidence: 0.95, source: "ocr_provider", warnings: [] };
  return {
    field_confidence: {
      schema_version: "v1",
      fields: {
        question_no: entry,
        page_no: entry,
        type: entry,
        stem: entry,
        options: entry,
        detected_student_answer: entry,
        detected_reference_answer: entry,
        answer: entry,
        explanation: entry,
        kc_names: entry,
        difficulty: entry,
        points: entry,
        ...overrides,
      },
    },
  } as unknown as ResourceQuestionOut;
}

describe("server field confidence display", () => {
  it("keeps a high-confidence field as read-only body content", () => {
    expect(fieldIsHighConfidence(question, "stem")).toBe(true);
    expect(fieldNeedsReview(question, "stem")).toBe(false);
  });

  it("expands unknown critical fields for correction", () => {
    expect(fieldNeedsReview(question, "answer")).toBe(true);
  });

  it("does not treat legally inapplicable fields as low confidence", () => {
    const shortQuestion = highConfidenceQuestion({
      options: { confidence: null, source: "unknown", warnings: [] },
      page_no: { confidence: null, source: "unknown", warnings: [] },
    });
    shortQuestion.field_confidence = {
      ...shortQuestion.field_confidence!,
      applicable_fields: ["question_no", "type", "stem", "answer"],
    };
    expect(fieldIsHighConfidence(shortQuestion, "options")).toBe(true);
    expect(fieldIsHighConfidence(shortQuestion, "page_no")).toBe(true);
  });

  it("only expands the one low-confidence field in a twenty-question review", () => {
    const questions = Array.from({ length: 20 }, (_, index) =>
      highConfidenceQuestion(index === 7 ? { stem: { confidence: 0.7, source: "ocr_provider", warnings: [] } } : {}),
    );
    expect(fieldsNeedingReview(questions[7])).toEqual(["stem"]);
    expect(fieldsNeedingReview(questions[6])).toEqual([]);
  });
});
