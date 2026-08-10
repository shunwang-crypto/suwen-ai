import type { ErrorEventData, GraphDoneData, QuizQuestion } from "@/lib/types";

type PersistedQuizResult = {
  quiz_id: string;
  questions: QuizQuestion[];
  [key: string]: unknown;
};

export function quizOutcomeMessage(outcome: GraphDoneData["outcome"]): string {
  switch (outcome) {
    case "generation_failed":
      return "没有生成出可用题目";
    case "validation_failed":
      return "题目质量检查未通过";
    case "cancelled_by_user":
      return "已取消本次出题";
    default:
      return "服务暂时不可用";
  }
}

export function quizSseErrorMessage(error: ErrorEventData): string {
  return `服务暂时不可用（错误码：${error.code || "GRAPH_FAILED"}）`;
}

export function hasPersistedQuizResult(
  done: GraphDoneData,
): done is GraphDoneData & {
  outcome: "succeeded";
  result: PersistedQuizResult;
} {
  const questions = done.result?.questions;
  return (
    done.outcome === "succeeded" &&
    Boolean(done.result?.quiz_id) &&
    Array.isArray(questions) &&
    questions.length > 0
  );
}
