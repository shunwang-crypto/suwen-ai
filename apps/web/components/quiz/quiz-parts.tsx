"use client";

// 练习（F5）：发起表单 → SSE 流（过程时间线 + interrupt 蓝图确认）→ resume →
// 题目作答（四种控件，MathText 渲染）→ 逐题结果 + 掌握度变化；历史卷可重开。





import type {GraphDoneData, GraphInterruptData, QuizQuestion, WeakPointItem} from "@/lib/types";

export type Phase = "idle" | "streaming" | "await_confirm" | "resuming";
export const PENDING_QUIZ_KEY = "suwen:pending-quiz-draft";

export interface PendingQuizDraft {
  status: "generating" | "await_confirm" | "resuming" | "failed";
  interrupt?: GraphInterruptData;
  topic: string;
  startedAt: number;
  requestBody?: Record<string, unknown>;
  requestPath?: string;
  failureOutcome?: GraphDoneData["outcome"];
}

export function pendingQuizKey(userId: string): string {
  return `${PENDING_QUIZ_KEY}:${userId}`;
}

export function readPendingQuiz(userId: string): PendingQuizDraft | null {
  try {
    const key = pendingQuizKey(userId);
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<PendingQuizDraft>;
    const validStatus = ["generating", "await_confirm", "resuming", "failed"].includes(
      value.status ?? "",
    );
    const stale = !value.startedAt || Date.now() - value.startedAt > 30 * 60 * 1000;
    const invalidInterrupt =
      ["await_confirm", "resuming"].includes(value.status ?? "") &&
      (!value.interrupt?.thread_id?.startsWith("quiz:") ||
        !value.interrupt.resume_url ||
        !value.interrupt.blueprint);
    if (!validStatus || !value.topic || stale || invalidInterrupt) {
      window.localStorage.removeItem(key);
      return null;
    }
    return value as PendingQuizDraft;
  } catch {
    window.localStorage.removeItem(pendingQuizKey(userId));
    return null;
  }
}

export function clearPendingQuiz(userId: string): void {
  window.localStorage.removeItem(pendingQuizKey(userId));
}

export function writePendingQuiz(userId: string, value: PendingQuizDraft): void {
  window.localStorage.setItem(pendingQuizKey(userId), JSON.stringify(value));
}

/** 后端下发的逐 KC 难度分档理由（P1-1 可解释出题：定性区间词，不含掌握度数值）。 */
export interface DifficultyReason {
  kc_name: string;
  zone: string;
  difficulty: string;
  reason: string;
}

/** 薄弱点预览项（/api/profile/weak-points 已附带 zone/reason 定性理由字段）。 */
export type WeakPointPreview = WeakPointItem & {
  zone?: string;
  difficulty?: string;
  reason?: string;
};

// useSyncExternalStore 订阅占位：URL search 在页面生命周期内不变化（跳转即重挂载）
export const emptySubscribe = () => () => {};

export interface ActiveQuiz {
  id: string;
  title: string;
  questions: QuizQuestion[];
}

export function PracticeSteps({ current }: { current: number }) {
  const labels = ["设置", "蓝图确认", "作答", "查看结果"];
  return (
    <ol className="flex flex-wrap items-center gap-2 border-b border-line pb-4 text-body-sm">
      {labels.map((label, index) => {
        const step = index + 1;
        const done = step < current;
        const active = step === current;
        return (
          <li key={label} className="flex items-center gap-2">
            <span className={`flex h-5 w-5 items-center justify-center rounded-full text-caption font-semibold ${done ? "bg-success-50 text-success-600" : active ? "bg-brand-600 text-white" : "bg-[#efede9] text-ink-muted"}`}>{done ? "✓" : step}</span>
            <span className={active ? "font-semibold text-ink" : done ? "text-success-600" : "text-ink-muted"}>{label}</span>
            {index < labels.length - 1 && <span className="h-px w-7 bg-line sm:w-12" aria-hidden />}
          </li>
        );
      })}
    </ol>
  );
}
