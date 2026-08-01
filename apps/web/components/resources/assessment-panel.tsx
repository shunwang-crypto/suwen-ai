"use client";

import { useEffect, useMemo, useState } from "react";

import { ApiError, apiJson } from "@/lib/api";
import type {
  AssessmentAnswerOut,
  AssessmentAttemptOut,
  AssessmentDetailOut,
  AssessmentGradeOut,
  LearningResourceOut,
  ResourceQuestionOut,
} from "@/lib/types";

const fieldClass = "h-9 rounded-[8px] border border-line-strong bg-white px-3 text-[12px] text-ink outline-none focus:border-brand-500 disabled:bg-surface-muted disabled:text-ink-muted";

function formatDate(value: string | null): string {
  if (!value) return "未填写日期";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function statusLabel(status: AssessmentAttemptOut["status"]): string {
  return status === "graded" ? "已核验" : status === "submitted" ? "待最终核验" : "草稿";
}

function friendlyError(reason: unknown): string {
  if (reason instanceof ApiError) {
    const messages: Record<string, string> = {
      ASSESSMENT_NOT_EDITABLE: "这次作答已经提交，不能再修改答案。",
      ASSESSMENT_NOT_SUBMITTED: "请先提交作答，再进行评分。",
      ANSWER_CORRECTNESS_REQUIRED: "请为每道主观题选择正确或错误，再最终核验。",
      RESOURCE_QUESTION_NOT_VERIFIED: "只有已核验题目可以进入作答记录。",
      ASSESSMENT_ALREADY_GRADED: "这次记录已经核验，不能覆盖历史结果。",
      ASSESSMENT_ANSWER_LOCKED: "这道题的真实作答已经锁定。",
    };
    return messages[reason.code] ?? reason.message;
  }
  return reason instanceof Error ? reason.message : "测评记录操作失败，请稍后重试";
}

function answerFor(detail: AssessmentDetailOut | null, questionId: string): AssessmentAnswerOut | undefined {
  return detail?.answers.find((answer) => answer.resource_question_id === questionId);
}

export function AssessmentPanel({
  resource,
  questions,
  onClose,
}: {
  resource: LearningResourceOut;
  questions: ResourceQuestionOut[];
  onClose: () => void;
}) {
  const verifiedQuestions = useMemo(() => questions.filter((question) => question.verification_status === "verified"), [questions]);
  const [attempts, setAttempts] = useState<AssessmentAttemptOut[] | null>(null);
  const [detail, setDetail] = useState<AssessmentDetailOut | null>(null);
  const [selectedAttemptId, setSelectedAttemptId] = useState<string | null>(null);
  const [responses, setResponses] = useState<Record<string, string>>({});
  const [grades, setGrades] = useState<Record<string, boolean | null>>({});
  const [title, setTitle] = useState(`${resource.title} · 练习记录`);
  const [takenAt, setTakenAt] = useState(new Date().toISOString().slice(0, 10));
  const [duration, setDuration] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadAttempts = async (selectLatest = false) => {
    try {
      const all = await apiJson<AssessmentAttemptOut[]>("/api/assessment-attempts");
      const list = all.filter((attempt) => attempt.resource_id === resource.id);
      setAttempts(list);
      const id = selectLatest ? list[0]?.id : selectedAttemptId;
      if (id) await loadDetail(id);
    } catch (reason) {
      setError(friendlyError(reason));
      setAttempts([]);
    }
  };

  const loadDetail = async (attemptId: string) => {
    try {
      const next = await apiJson<AssessmentDetailOut>(`/api/assessment-attempts/${attemptId}`);
      setDetail(next);
      setSelectedAttemptId(attemptId);
      const nextResponses: Record<string, string> = {};
      const nextGrades: Record<string, boolean | null> = {};
      for (const question of verifiedQuestions) {
        const answer = answerFor(next, question.id);
        nextResponses[question.id] = answer?.response ?? "";
        nextGrades[question.id] = answer?.is_correct ?? null;
      }
      setResponses(nextResponses);
      setGrades(nextGrades);
    } catch (reason) {
      setError(friendlyError(reason));
    }
  };

  useEffect(() => {
    const timer = window.setTimeout(() => void loadAttempts(), 0);
    return () => window.clearTimeout(timer);
    // The panel is opened for one resource; avoid reloading history for each draft keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resource.id]);

  const createAttempt = async () => {
    setBusy("create");
    setError(null);
    try {
      const created = await apiJson<AssessmentAttemptOut>("/api/assessment-attempts", {
        body: {
          resource_id: resource.id,
          attempt_type: "practice",
          title: title.trim() || `${resource.title} · 练习记录`,
          taken_at: takenAt ? `${takenAt}T00:00:00` : null,
          duration_minutes: duration ? Number(duration) : null,
        },
      });
      setDetail({ ...created, answers: [], answers_revealed: false });
      setSelectedAttemptId(created.id);
      setResponses(Object.fromEntries(verifiedQuestions.map((question) => [question.id, ""])));
      setGrades(Object.fromEntries(verifiedQuestions.map((question) => [question.id, null])));
      await loadAttempts();
    } catch (reason) {
      setError(friendlyError(reason));
    } finally {
      setBusy(null);
    }
  };

  const saveAnswers = async (): Promise<boolean> => {
    if (!detail || detail.status !== "draft") return false;
    setBusy("save");
    setError(null);
    try {
      await apiJson(`/api/assessment-attempts/${detail.id}/answers`, {
        method: "PUT",
        body: {
          answers: verifiedQuestions.map((question) => ({
            resource_question_id: question.id,
            response: responses[question.id] ?? "",
          })),
        },
      });
      const next = await apiJson<AssessmentDetailOut>(`/api/assessment-attempts/${detail.id}`);
      setDetail(next);
      await loadAttempts();
      return true;
    } catch (reason) {
      setError(friendlyError(reason));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const submitAttempt = async () => {
    if (!detail) return;
    if (!(await saveAnswers())) return;
    setBusy("submit");
    setError(null);
    try {
      await apiJson(`/api/assessment-attempts/${detail.id}/submit`, { method: "POST", body: {} });
      await loadDetail(detail.id);
      await loadAttempts();
    } catch (reason) {
      setError(friendlyError(reason));
    } finally {
      setBusy(null);
    }
  };

  const gradeAttempt = async (verify: boolean) => {
    if (!detail || detail.status === "draft") return;
    setBusy(verify ? "verify" : "grade");
    setError(null);
    try {
      const result = await apiJson<AssessmentGradeOut>(`/api/assessment-attempts/${detail.id}/grade-or-verify`, {
        body: {
          verify,
          answers: verifiedQuestions.map((question) => {
            const value = grades[question.id];
            return {
              resource_question_id: question.id,
              response: responses[question.id] ?? "",
              ...(value !== null && value !== undefined ? { is_correct: value } : {}),
            };
          }),
        },
      });
      await loadDetail(detail.id);
      await loadAttempts();
      if (result.status === "graded") setError(null);
    } catch (reason) {
      setError(friendlyError(reason));
    } finally {
      setBusy(null);
    }
  };

  const allSubjectiveGraded = verifiedQuestions.every((question) => question.type !== "short" || grades[question.id] !== null && grades[question.id] !== undefined);

  return (
    <div className="fixed inset-0 z-[65] overflow-y-auto bg-[#1c1917]/40 p-3 sm:p-6">
      <section role="dialog" aria-modal="true" aria-labelledby="assessment-title" className="mx-auto flex min-h-[calc(100dvh-24px)] max-w-[1180px] flex-col overflow-hidden rounded-[14px] border border-line bg-canvas shadow-floating sm:min-h-[calc(100dvh-48px)]">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line bg-white px-5 py-4">
          <div><p className="text-[10px] font-medium uppercase tracking-[0.1em] text-ink-muted">真实考试 / 练习记录</p><h2 id="assessment-title" className="mt-1 text-[18px] font-semibold text-ink">{resource.title}</h2><p className="mt-1 text-[11px] text-ink-secondary">每次记录都保存真实作答；只有最终核验后才进入学习画像和错题本。</p></div>
          <button type="button" onClick={onClose} className="rounded-[7px] p-2 text-ink-muted hover:bg-surface-muted" aria-label="关闭作答记录">✕</button>
        </header>
        <div className="grid min-h-0 flex-1 lg:grid-cols-[280px_minmax(0,1fr)]">
          <aside className="border-b border-line bg-white p-4 lg:border-b-0 lg:border-r">
            <div className="flex items-center justify-between"><h3 className="text-[12px] font-semibold text-ink">历史记录</h3><span className="text-[10px] text-ink-muted">{attempts?.length ?? 0} 次</span></div>
            <div className="mt-3 space-y-1.5 lg:max-h-[calc(100dvh-190px)] lg:overflow-y-auto">
              {attempts === null && <p className="text-[11px] text-ink-muted">正在加载…</p>}
              {attempts?.length === 0 && <p className="rounded-[8px] bg-surface-muted px-3 py-3 text-[11px] leading-5 text-ink-muted">还没有记录。可以在下方创建第一次作答。</p>}
              {attempts?.map((attempt) => <button key={attempt.id} type="button" onClick={() => void loadDetail(attempt.id)} className={`w-full rounded-[8px] border px-3 py-2.5 text-left ${selectedAttemptId === attempt.id ? "border-ink bg-[#f6f5f2]" : "border-line hover:border-line-strong"}`}><span className="flex items-center justify-between gap-2"><span className="truncate text-[11px] font-medium text-ink">{attempt.title || "未命名记录"}</span><span className="shrink-0 text-[10px] text-ink-muted">{statusLabel(attempt.status)}</span></span><span className="mt-1 block text-[10px] text-ink-muted">{formatDate(attempt.taken_at ?? attempt.created_at)}{attempt.score != null && attempt.total_score != null ? ` · ${attempt.score}/${attempt.total_score}` : ""}</span></button>)}
            </div>
            {!detail && (
              <div className="mt-4 border-t border-line pt-4">
                <p className="text-[11px] font-medium text-ink">新建作答记录</p>
                <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="记录名称" className={`${fieldClass} mt-2 w-full`} />
                <div className="mt-2 grid grid-cols-2 gap-2"><label className="text-[10px] text-ink-muted">日期<input type="date" value={takenAt} onChange={(event) => setTakenAt(event.target.value)} className={`${fieldClass} mt-1 w-full`} /></label><label className="text-[10px] text-ink-muted">时长（分钟）<input type="number" min={0} value={duration} onChange={(event) => setDuration(event.target.value)} className={`${fieldClass} mt-1 w-full`} /></label></div>
                <button type="button" onClick={() => void createAttempt()} disabled={busy !== null || verifiedQuestions.length === 0} className="mt-3 w-full rounded-[8px] bg-ink px-3 py-2 text-[11px] font-medium text-white disabled:opacity-40">{busy === "create" ? "创建中…" : "开始一次作答"}</button>
              </div>
            )}
          </aside>

          <main className="min-h-0 overflow-y-auto p-4 sm:p-6">
            {error && <p className="mb-4 rounded-[8px] border border-danger-200 bg-danger-50 px-3 py-2 text-[11px] text-danger-700">{error}</p>}
            {!detail ? <div className="flex min-h-64 items-center justify-center rounded-[12px] border border-dashed border-line-strong bg-white px-5 text-center text-[12px] text-ink-muted">从左侧创建一次作答记录，或打开已有历史。</div> : (
              <>
                <div className="flex flex-wrap items-start justify-between gap-3 rounded-[11px] border border-line bg-white p-4"><div><h3 className="text-[14px] font-semibold text-ink">{detail.title || "未命名记录"}</h3><p className="mt-1 text-[10px] text-ink-muted">{formatDate(detail.taken_at ?? detail.created_at)} · {statusLabel(detail.status)}{detail.duration_minutes != null ? ` · ${detail.duration_minutes} 分钟` : ""}</p></div>{detail.score != null && detail.total_score != null && <div className="text-right"><p className="text-[20px] font-semibold text-ink">{detail.score}<span className="text-[12px] text-ink-muted"> / {detail.total_score}</span></p><p className="text-[10px] text-ink-muted">当前得分</p></div>}</div>
                <div className="mt-4 space-y-3">
                  {verifiedQuestions.map((question, index) => {
                    const savedAnswer = answerFor(detail, question.id);
                    const locked = detail.status !== "draft";
                    return <article key={question.id} className="rounded-[11px] border border-line bg-white p-4"><div className="flex flex-wrap items-start justify-between gap-2"><div className="flex gap-2"><span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface-muted text-[10px] font-semibold text-ink-secondary">{question.question_no || index + 1}</span><div><p className="text-[12px] leading-5 text-ink">{question.stem}</p>{question.options.length > 0 && <ul className="mt-2 space-y-1 text-[11px] text-ink-secondary">{question.options.map((option) => <li key={option}>{option}</li>)}</ul>}</div></div>{savedAnswer && <span className="shrink-0 rounded-full bg-surface-muted px-2 py-0.5 text-[10px] text-ink-muted">{savedAnswer.grading_status === "verified" ? "已核验" : savedAnswer.grading_status === "graded" ? "已评分" : "待评分"}</span>}</div><label className="mt-3 block"><span className="text-[10px] font-medium text-ink-secondary">我的作答</span><textarea rows={3} disabled={locked} value={responses[question.id] ?? ""} onChange={(event) => setResponses((previous) => ({ ...previous, [question.id]: event.target.value }))} placeholder="填写你当时的答案或解题过程" className="mt-1 w-full resize-y rounded-[8px] border border-line-strong px-3 py-2 text-[12px] leading-5 outline-none focus:border-brand-500 disabled:bg-surface-muted disabled:text-ink-muted" /></label>{locked && <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3"><span className="text-[10px] font-medium text-ink-secondary">核对结果</span><select disabled={detail.status === "graded"} value={grades[question.id] == null ? "" : grades[question.id] ? "correct" : "wrong"} onChange={(event) => setGrades((previous) => ({ ...previous, [question.id]: event.target.value === "" ? null : event.target.value === "correct" }))} className="h-8 rounded-[7px] border border-line-strong bg-white px-2 text-[11px] text-ink disabled:bg-surface-muted"><option value="">未选择</option><option value="correct">正确</option><option value="wrong">错误</option></select>{savedAnswer?.score != null && <span className="text-[10px] text-ink-muted">得分 {savedAnswer.score}{savedAnswer.max_score != null ? ` / ${savedAnswer.max_score}` : ""}</span>}</div>}</article>;
                  })}
                </div>
                <footer className="mt-4 flex flex-wrap items-center justify-end gap-2 rounded-[11px] border border-line bg-white p-4">
                  {detail.status === "draft" && <><button type="button" onClick={() => void saveAnswers()} disabled={busy !== null} className="rounded-[8px] border border-line-strong px-3.5 py-2 text-[11px] text-ink-secondary disabled:opacity-40">{busy === "save" ? "保存中…" : "保存作答"}</button><button type="button" onClick={() => void submitAttempt()} disabled={busy !== null} className="rounded-[8px] bg-ink px-3.5 py-2 text-[11px] font-medium text-white disabled:opacity-40">{busy === "submit" ? "提交中…" : "提交作答"}</button></>}
                  {detail.status === "submitted" && <><button type="button" onClick={() => void gradeAttempt(false)} disabled={busy !== null} className="rounded-[8px] border border-line-strong px-3.5 py-2 text-[11px] text-ink-secondary disabled:opacity-40">{busy === "grade" ? "评分中…" : "保存评分（未核验）"}</button><button type="button" onClick={() => void gradeAttempt(true)} disabled={busy !== null || !allSubjectiveGraded} className="rounded-[8px] bg-ink px-3.5 py-2 text-[11px] font-medium text-white disabled:opacity-40">{busy === "verify" ? "核验中…" : "最终核验并计入学习"}</button></>}
                  {detail.status === "graded" && <span className="text-[11px] text-success-700">这次作答已核验，结果已进入学习记录。</span>}
                </footer>
                {detail.status === "submitted" && !allSubjectiveGraded && <p className="mt-2 text-right text-[10px] text-warning-700">请为每道简答题选择正确或错误后，再进行最终核验。</p>}
              </>
            )}
          </main>
        </div>
      </section>
    </div>
  );
}
