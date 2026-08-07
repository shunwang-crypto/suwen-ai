import { describe, expect, it } from "vitest";

import { buildAttemptAnswers } from "@/components/quiz/submission";
import type { QuizQuestion } from "@/lib/types";

const questions: QuizQuestion[] = [
  { id: "q1", type: "single", stem: "1+1", options: ["1", "2"], difficulty: "easy", kc_ids: [] },
  { id: "q2", type: "blank", stem: "x", options: [], difficulty: "mid", kc_ids: [] },
];

describe("buildAttemptAnswers", () => {
  it("sends selected confidence and omits skipped predictions", () => {
    expect(buildAttemptAnswers(questions, { q1: "B" }, { q1: 4 })).toEqual([
      { question_id: "q1", response: "B", predicted_confidence: 4 },
      { question_id: "q2", response: "" },
    ]);
  });
});
