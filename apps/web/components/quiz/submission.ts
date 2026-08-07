import type { QuizQuestion } from "@/lib/types";

export function buildAttemptAnswers(
  questions: QuizQuestion[],
  answers: Record<string, string>,
  predictions: Record<string, number>,
) {
  return questions.map((question) => ({
    question_id: question.id,
    response: answers[question.id] ?? "",
    ...(predictions[question.id] != null
      ? { predicted_confidence: predictions[question.id] }
      : {}),
  }));
}
