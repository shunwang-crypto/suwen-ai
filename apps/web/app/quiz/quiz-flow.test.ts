import { describe, expect, it } from "vitest";

import {
  hasPersistedQuizResult,
  quizOutcomeMessage,
  quizSseErrorMessage,
} from "./quiz-flow";

describe("quiz done outcome v2", () => {
  it.each([
    ["generation_failed", "没有生成出可用题目"],
    ["validation_failed", "题目质量检查未通过"],
    ["cancelled_by_user", "已取消本次出题"],
  ] as const)("maps %s to the real failure message", (outcome, message) => {
    expect(quizOutcomeMessage(outcome)).toBe(message);
  });

  it("requires a non-empty persisted question result for success", () => {
    expect(
      hasPersistedQuizResult({
        thread_id: "quiz:user:local",
        schema_version: 2,
        outcome: "succeeded",
        result: { quiz_id: "quiz-1", questions: [] },
        error: null,
      }),
    ).toBe(false);
    expect(
      hasPersistedQuizResult({
        thread_id: "quiz:user:local",
        schema_version: 2,
        outcome: "succeeded",
        result: {
          quiz_id: "quiz-1",
          questions: [
            {
              id: "question-1",
              type: "single",
              stem: "safe test payload",
              options: [],
              difficulty: "easy",
              kc_ids: [],
            },
          ],
        },
        error: null,
      }),
    ).toBe(true);
  });

  it("uses a stable service-error message with the correlation code", () => {
    expect(quizSseErrorMessage({ code: "QUIZ_FINALIZE_FAILED", message: "内部详情" })).toBe(
      "服务暂时不可用（错误码：QUIZ_FINALIZE_FAILED）",
    );
  });
});
