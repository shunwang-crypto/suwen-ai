"use client";

import { useMemo, useState, type FormEvent } from "react";

import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type {
  LearningResourceOut,
  ResourceConfirmOut,
  ResourceQuestionOut,
} from "@/lib/types";

import { SourcePreview } from "./source-preview";

const QUESTION_TYPES: Array<[string, string]> = [
  ["single", "单选题"],
  ["multi", "多选题"],
  ["blank", "填空题"],
  ["short", "简答题"],
];
const DIFFICULTIES: Array<[string, string]> = [
  ["easy", "简单"],
  ["mid", "中等"],
  ["hard", "困难"],
];
const fieldClass = "mt-1 w-full rounded-[8px] border border-line-strong bg-white px-3 py-2 text-[12px] leading-5 text-ink outline-none transition-colors focus:border-brand-500 disabled:bg-surface-muted disabled:text-ink-muted";

type QuestionDraft = {
  question_no: string;
  page_no: string;
  type: string;
  stem: string;
  options: string[];
  detected_student_answer: string;
  detected_reference_answer: string;
  answer: string;
  explanation: string;
  points: string;
  kc_names: string[];
  difficulty: string;
  extraction_confidence: string;
};

function draftFromQuestion(question: ResourceQuestionOut): QuestionDraft {
  return {
    question_no: question.question_no ?? "",
    page_no: question.page_no?.toString() ?? "",
    type: question.type,
    stem: question.stem,
    options: [...question.options],
    detected_student_answer: question.detected_student_answer ?? "",
    detected_reference_answer: question.detected_reference_answer ?? question.answer ?? "",
    answer: question.answer ?? "",
    explanation: question.explanation ?? "",
    points: question.points?.toString() ?? "",
    kc_names: [...question.kc_names],
    difficulty: question.difficulty || "mid",
    extraction_confidence: question.extraction_confidence == null ? "" : `${Math.round(question.extraction_confidence * 100)}%`,
  };
}

function statusLabel(status: ResourceQuestionOut["verification_status"]): string {
  return status === "verified" ? "已核验" : status === "rejected" ? "已拒绝" : "待校对";
}

function typeLabel(type: string): string {
  return QUESTION_TYPES.find(([value]) => value === type)?.[1] ?? type;
}

function friendlyError(reason: unknown, fallback: string): string {
  if (reason instanceof ApiError) {
    const messages: Record<string, string> = {
      RESOURCE_QUESTION_NOT_VERIFIED: "请先核验题目，再进行这项操作。",
      VERIFICATION_ROLLBACK_FORBIDDEN: "题目状态只能向前推进，不能退回。",
      ERROR_ITEM_LOCKED_AFTER_REVIEW: "这道题已经产生学习证据，判分语义字段已锁定。",
      RESOURCE_QUESTION_HAS_ERROR_ITEM: "这道题已经进入错题本，不能删除。",
      RESOURCE_QUESTION_HAS_ASSESSMENT: "这道题已有测评作答记录，不能删除。",
    };
    return messages[reason.code] ?? reason.message;
  }
  return reason instanceof Error ? reason.message : fallback;
}

function QuestionStatus({ status }: { status: ResourceQuestionOut["verification_status"] }) {
  const styles = status === "verified"
    ? "bg-success-50 text-success-700"
    : status === "rejected"
      ? "bg-surface-muted text-ink-muted"
      : "bg-warning-50 text-warning-700";
  return <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${styles}`}>{statusLabel(status)}</span>;
}

function QuestionList({
  questions,
  currentId,
  selectedErrors,
  onSelect,
  onToggleError,
}: {
  questions: ResourceQuestionOut[];
  currentId: string | null;
  selectedErrors: Set<string>;
  onSelect: (id: string) => void;
  onToggleError: (id: string, checked: boolean) => void;
}) {
  return (
    <div className="space-y-1.5">
      {questions.map((question, index) => {
        const selected = selectedErrors.has(question.id);
        return (
          <div key={question.id} className={`rounded-[9px] border transition-colors ${currentId === question.id ? "border-ink bg-[#f6f5f2]" : "border-transparent hover:border-line hover:bg-[#fafaf9]"}`}>
            <button type="button" onClick={() => onSelect(question.id)} className="flex w-full items-start gap-2.5 px-2.5 py-2.5 text-left">
              <span className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold ${currentId === question.id ? "bg-ink text-white" : "bg-surface-muted text-ink-muted"}`}>{question.question_no || index + 1}</span>
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-1.5"><span className="text-[10px] text-ink-secondary">{typeLabel(question.type)}</span><QuestionStatus status={question.verification_status} /></span>
                <span className="mt-1 block max-h-9 overflow-hidden text-[11px] leading-4 text-ink">{question.stem || "未填写题干"}</span>
              </span>
            </button>
            {question.verification_status === "verified" && (
              <label className="flex cursor-pointer items-center gap-2 border-t border-line/70 px-2.5 py-2 text-[10px] text-ink-secondary">
                <input type="checkbox" checked={selected} onChange={(event) => onToggleError(question.id, event.target.checked)} className="h-3.5 w-3.5 accent-brand-700" />
                这是我的错题，加入错题本
              </label>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function ResourceReview({
  resource,
  questions,
  onBack,
  onReload,
  onResourceChanged,
  onStartAssessment,
}: {
  resource: LearningResourceOut;
  questions: ResourceQuestionOut[];
  onBack: () => void;
  onReload: () => Promise<void>;
  onResourceChanged: (resource: LearningResourceOut) => void;
  onStartAssessment: () => void;
}) {
  const [currentId, setCurrentId] = useState<string | null>(questions[0]?.id ?? null);
  const [draft, setDraft] = useState<QuestionDraft | null>(questions[0] ? draftFromQuestion(questions[0]) : null);
  const [dirty, setDirty] = useState(false);
  const [selectedErrors, setSelectedErrors] = useState<Set<string>>(
    () => new Set(questions.filter((question) => question.is_error_selected).map((question) => question.id)),
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [mobilePane, setMobilePane] = useState<"source" | "questions">("questions");
  const [showManual, setShowManual] = useState(false);
  const [manualStem, setManualStem] = useState("");
  const [manualType, setManualType] = useState("short");

  const current = questions.find((question) => question.id === currentId) ?? null;
  const semanticLocked = current?.content_editable === false;
  const answerHidden = current != null && current.answer === undefined;
  const pendingCount = questions.filter((question) => question.verification_status === "pending").length;
  const verifiedCount = questions.filter((question) => question.verification_status === "verified").length;
  const selectedCount = useMemo(() => selectedErrors.size, [selectedErrors]);

  const updateDraft = <K extends keyof QuestionDraft>(key: K, value: QuestionDraft[K]) => {
    setDraft((previous) => previous ? { ...previous, [key]: value } : previous);
    setDirty(true);
  };

  const selectQuestion = (id: string) => {
    if (id === currentId) return;
    if (dirty && !window.confirm("当前题目还有未保存修改，确定切换吗？")) return;
    const question = questions.find((item) => item.id === id);
    if (!question) return;
    setCurrentId(id);
    setDraft(draftFromQuestion(question));
    setDirty(false);
    setMessage(null);
  };

  const saveQuestion = async (question = current): Promise<ResourceQuestionOut | null> => {
    if (!question || !draft) return null;
    setBusy("save");
    setMessage(null);
    const payload: Record<string, unknown> = {
      question_no: draft.question_no.trim() || null,
      page_no: draft.page_no ? Number(draft.page_no) : null,
      stem: draft.stem.trim(),
      detected_student_answer: draft.detected_student_answer.trim(),
      explanation: draft.explanation.trim(),
      points: draft.points ? Number(draft.points) : null,
      difficulty: draft.difficulty,
    };
    if (!semanticLocked && !answerHidden) {
      payload.type = draft.type;
      payload.options = draft.options.map((item) => item.trim()).filter(Boolean);
      payload.detected_reference_answer = draft.detected_reference_answer.trim();
      payload.answer = draft.answer.trim();
      payload.kc_names = draft.kc_names.map((item) => item.trim()).filter(Boolean).slice(0, 3);
    }
    try {
      if (!payload.stem) throw new Error("请先填写题干");
      const saved = await apiJson<ResourceQuestionOut>(`/api/resource-questions/${question.id}`, {
        method: "PATCH",
        body: payload,
      });
      setDraft(draftFromQuestion(saved));
      setDirty(false);
      await onReload();
      setMessage("题目修改已保存。");
      return saved;
    } catch (reason) {
      setMessage(friendlyError(reason, "题目保存失败，请稍后重试"));
      return null;
    } finally {
      setBusy(null);
    }
  };

  const verifyQuestion = async () => {
    if (!current || !draft) return;
    if (dirty && !(await saveQuestion())) return;
    setBusy("verify");
    setMessage(null);
    try {
      const saved = await apiJson<ResourceQuestionOut>(`/api/resource-questions/${current.id}/verify`, {
        method: "POST",
        body: {},
      });
      setDraft(draftFromQuestion(saved));
      setDirty(false);
      await onReload();
      setMessage("题目已核验。是否加入错题本，请在左侧明确勾选。 ");
    } catch (reason) {
      setMessage(friendlyError(reason, "核验失败，请先确认题目内容完整"));
    } finally {
      setBusy(null);
    }
  };

  const rejectQuestion = async () => {
    if (!current || current.verification_status !== "pending") return;
    if (!window.confirm("拒绝后这道题不会进入错题本或测评，确定继续吗？")) return;
    setBusy("reject");
    setMessage(null);
    try {
      await apiJson<ResourceQuestionOut>(`/api/resource-questions/${current.id}`, {
        method: "PATCH",
        body: { verification_status: "rejected" },
      });
      setSelectedErrors((previous) => {
        const next = new Set(previous);
        next.delete(current.id);
        return next;
      });
      await onReload();
      setMessage("题目已拒绝，不会进入错题本或测评。");
    } catch (reason) {
      setMessage(friendlyError(reason, "拒绝题目失败"));
    } finally {
      setBusy(null);
    }
  };

  const confirmErrors = async () => {
    if (dirty && !(await saveQuestion())) return;
    setBusy("confirm");
    setMessage(null);
    try {
      const result = await apiJson<ResourceConfirmOut>(`/api/resources/${resource.id}/confirm`, {
        body: { error_question_ids: [...selectedErrors], add_to_error_book: true },
      });
      onResourceChanged({ ...resource, status: result.status, pending_question_count: pendingCount });
      await onReload();
      setMessage(selectedCount > 0 ? `已将 ${selectedCount} 道明确选择的题加入错题本。` : "校对进度已保存；本次没有选择任何错题。 ");
    } catch (reason) {
      setMessage(friendlyError(reason, "保存校对结果失败"));
    } finally {
      setBusy(null);
    }
  };

  const addManualQuestion = async (event: FormEvent) => {
    event.preventDefault();
    if (!manualStem.trim()) return;
    setBusy("manual");
    setMessage(null);
    try {
      await apiJson<ResourceQuestionOut>(`/api/resources/${resource.id}/questions/manual`, {
        body: { stem: manualStem.trim(), type: manualType, answer: "" },
      });
      setManualStem("");
      setShowManual(false);
      await onReload();
      setMessage("已添加一题待校对题目。");
    } catch (reason) {
      setMessage(friendlyError(reason, "添加题目失败"));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mx-auto w-full max-w-[1480px] space-y-4 px-5 py-5 sm:px-8 lg:px-12 lg:py-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <button type="button" onClick={onBack} className="text-[11px] text-ink-muted hover:text-ink">← 返回学习资料</button>
          <h1 className="mt-2 truncate text-[22px] font-semibold tracking-[-0.03em] text-ink">{resource.title}</h1>
          <p className="mt-1 text-[11px] text-ink-secondary">{resource.subject || "通用"} · {verifiedCount} 道已核验 · {pendingCount} 道待校对 · {resource.source_filename ?? "手动创建"}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" onClick={onStartAssessment} disabled={verifiedCount === 0} className="rounded-[8px] border border-line-strong bg-white px-3.5 py-2 text-[11px] font-medium text-ink-secondary hover:border-ink-muted hover:text-ink disabled:cursor-not-allowed disabled:opacity-40">真实作答记录</button>
          <button type="button" onClick={() => void confirmErrors()} disabled={busy !== null || questions.length === 0} className="rounded-[8px] bg-ink px-3.5 py-2 text-[11px] font-medium text-white disabled:opacity-40">{busy === "confirm" ? "正在保存…" : selectedCount > 0 ? `确认并加入 ${selectedCount} 道错题` : "保存校对进度"}</button>
        </div>
      </header>

      {message && <div className="rounded-[9px] border border-line bg-white px-3.5 py-2.5 text-[11px] text-ink-secondary">{message}</div>}

      <div className="flex w-fit rounded-[8px] border border-line bg-white p-1 lg:hidden">
        <button type="button" onClick={() => setMobilePane("source")} className={`rounded-[6px] px-3 py-1.5 text-[11px] font-medium ${mobilePane === "source" ? "bg-ink text-white" : "text-ink-secondary"}`}>原件预览</button>
        <button type="button" onClick={() => setMobilePane("questions")} className={`rounded-[6px] px-3 py-1.5 text-[11px] font-medium ${mobilePane === "questions" ? "bg-ink text-white" : "text-ink-secondary"}`}>题目校对</button>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(280px,0.8fr)_minmax(0,1.4fr)]">
        <div className={mobilePane === "source" ? "block" : "hidden lg:block"}><SourcePreview resource={resource} /></div>
        <section className={mobilePane === "questions" ? "space-y-4" : "hidden lg:block lg:space-y-4"}>
          <div className="grid gap-4 lg:grid-cols-[220px_minmax(0,1fr)]">
            <aside className="rounded-[12px] border border-line bg-white p-3">
              <div className="flex items-center justify-between gap-2 border-b border-line pb-3">
                <div><h2 className="text-[12px] font-semibold text-ink">题目列表</h2><p className="mt-0.5 text-[10px] text-ink-muted">显式选择错题</p></div>
                <button type="button" onClick={() => setShowManual((value) => !value)} className="rounded-[7px] border border-line-strong px-2 py-1 text-[10px] text-ink-secondary hover:text-ink">+ 添加题目</button>
              </div>
              <div className="mt-3 max-h-[62dvh] overflow-y-auto"><QuestionList questions={questions} currentId={currentId} selectedErrors={selectedErrors} onSelect={selectQuestion} onToggleError={(id, checked) => setSelectedErrors((previous) => { const next = new Set(previous); if (checked) next.add(id); else next.delete(id); return next; })} /></div>
              {showManual && (
                <form onSubmit={addManualQuestion} className="mt-3 space-y-2 border-t border-line pt-3">
                  <textarea value={manualStem} onChange={(event) => setManualStem(event.target.value)} rows={3} placeholder="输入新题干" className={`${fieldClass} resize-y`} />
                  <select value={manualType} onChange={(event) => setManualType(event.target.value)} className={fieldClass}><option value="short">简答题</option><option value="single">单选题</option><option value="multi">多选题</option><option value="blank">填空题</option></select>
                  <button type="submit" disabled={busy === "manual" || !manualStem.trim()} className="w-full rounded-[7px] bg-ink px-3 py-2 text-[10px] font-medium text-white disabled:opacity-40">添加待校对题</button>
                </form>
              )}
            </aside>

            <div className="min-w-0 rounded-[12px] border border-line bg-white p-4 sm:p-5">
              {!current || !draft ? (
                <div className="flex min-h-64 items-center justify-center text-[12px] text-ink-muted">暂时没有题目。可以从左侧添加手动题目。</div>
              ) : (
                <>
                  <div className="flex flex-wrap items-start justify-between gap-2 border-b border-line pb-3">
                    <div><div className="flex flex-wrap items-center gap-2"><span className="text-[12px] font-semibold text-ink">第 {current.question_no || current.ordinal + 1} 题</span><QuestionStatus status={current.verification_status} /><span className="text-[10px] text-ink-muted">{typeLabel(current.type)}</span></div><p className="mt-1 text-[10px] text-ink-muted">识别置信度：{draft.extraction_confidence || "未提供"}</p></div>
                    <div className="flex flex-wrap gap-2">
                      <button type="button" onClick={() => void saveQuestion()} disabled={busy !== null || !dirty} className="rounded-[7px] border border-line-strong px-3 py-1.5 text-[10px] font-medium text-ink-secondary disabled:opacity-40">{busy === "save" ? "保存中…" : "保存修改"}</button>
                      {current.verification_status === "pending" && <><button type="button" onClick={() => void rejectQuestion()} disabled={busy !== null} className="rounded-[7px] border border-danger-200 px-3 py-1.5 text-[10px] text-danger-700 disabled:opacity-40">拒绝</button><button type="button" onClick={() => void verifyQuestion()} disabled={busy !== null || !draft.stem.trim() || !draft.answer.trim()} className="rounded-[7px] bg-ink px-3 py-1.5 text-[10px] font-medium text-white disabled:opacity-40">{busy === "verify" ? "核验中…" : "核验题目"}</button></>}
                    </div>
                  </div>

                  <div className="mt-4 grid gap-4 sm:grid-cols-3">
                    <label><span className="text-[10px] font-medium text-ink-secondary">题号</span><input value={draft.question_no} onChange={(event) => updateDraft("question_no", event.target.value)} className={fieldClass} /></label>
                    <label><span className="text-[10px] font-medium text-ink-secondary">页码</span><input type="number" min={1} value={draft.page_no} onChange={(event) => updateDraft("page_no", event.target.value)} className={fieldClass} /></label>
                    <label><span className="text-[10px] font-medium text-ink-secondary">题型</span><select disabled={semanticLocked || answerHidden} value={draft.type} onChange={(event) => updateDraft("type", event.target.value)} className={fieldClass}>{QUESTION_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                  </div>
                  <label className="mt-4 block"><span className="text-[10px] font-medium text-ink-secondary">题干 <em className="not-italic text-danger-600">*</em></span><textarea rows={5} value={draft.stem} onChange={(event) => updateDraft("stem", event.target.value)} className={`${fieldClass} resize-y`} /></label>
                  {(draft.type === "single" || draft.type === "multi") && <label className="mt-4 block"><span className="text-[10px] font-medium text-ink-secondary">选项（每行一个）</span><textarea disabled={semanticLocked || answerHidden} rows={4} value={draft.options.join("\n")} onChange={(event) => updateDraft("options", event.target.value.split("\n"))} className={`${fieldClass} resize-y font-mono`} /></label>}
                  <div className="mt-4 grid gap-4 sm:grid-cols-2"><label><span className="text-[10px] font-medium text-ink-secondary">学生原答案</span><textarea rows={3} value={draft.detected_student_answer} onChange={(event) => updateDraft("detected_student_answer", event.target.value)} className={`${fieldClass} resize-y`} /></label><label><span className="text-[10px] font-medium text-ink-secondary">参考答案</span><textarea disabled={semanticLocked || answerHidden} rows={3} value={draft.detected_reference_answer} onChange={(event) => { updateDraft("detected_reference_answer", event.target.value); updateDraft("answer", event.target.value); }} className={`${fieldClass} resize-y`} /></label></div>
                  <div className="mt-4 grid gap-4 sm:grid-cols-[minmax(0,1fr)_140px_160px]"><label><span className="text-[10px] font-medium text-ink-secondary">知识点（最多 3 个）</span><input disabled={semanticLocked || answerHidden} value={draft.kc_names.join("，")} onChange={(event) => updateDraft("kc_names", event.target.value.split(/[,，]/))} placeholder="例如：二次函数，最值" className={fieldClass} /></label><label><span className="text-[10px] font-medium text-ink-secondary">分值</span><input type="number" min={0} step="0.5" value={draft.points} onChange={(event) => updateDraft("points", event.target.value)} className={fieldClass} /></label><label><span className="text-[10px] font-medium text-ink-secondary">难度</span><select value={draft.difficulty} onChange={(event) => updateDraft("difficulty", event.target.value)} className={fieldClass}>{DIFFICULTIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div>
                  <label className="mt-4 block"><span className="text-[10px] font-medium text-ink-secondary">解析</span><textarea rows={4} value={draft.explanation} onChange={(event) => updateDraft("explanation", event.target.value)} className={`${fieldClass} resize-y`} /></label>
                  <div className="mt-4 rounded-[9px] bg-surface-muted px-3 py-2.5 text-[11px] leading-5 text-ink-secondary"><span className="font-medium text-ink">核验规则：</span>只有点击“核验题目”后，题目才能进入真实作答；是否属于错题必须单独勾选。待校对和已拒绝题不会影响学习数据。{semanticLocked && <span className="mt-1 block text-warning-700">这道题已经产生 FSRS 或掌握度证据，题型、选项、参考答案和知识点已锁定。</span>}{answerHidden && <span className="mt-1 block text-warning-700">当前存在未完成作答记录，参考答案暂时隐藏；请先完成或关闭该草稿记录。</span>}</div>
                  {current.verification_status === "verified" && <div className={`mt-3 flex items-center gap-2 rounded-[8px] px-3 py-2 text-[10px] ${selectedErrors.has(current.id) ? "bg-warning-50 text-warning-700" : "bg-success-50 text-success-700"}`}><input type="checkbox" checked={selectedErrors.has(current.id)} onChange={(event) => setSelectedErrors((previous) => { const next = new Set(previous); if (event.target.checked) next.add(current.id); else next.delete(current.id); return next; })} className="h-3.5 w-3.5 accent-brand-700" />{selectedErrors.has(current.id) ? "已选择：这是一道错题" : "这道题暂不加入错题本"}</div>}
                  <div className="mt-4 rounded-[9px] border border-line bg-[#fafaf9] p-3"><p className="text-[10px] font-semibold text-ink-muted">题干预览</p><div className="mt-2 text-[12px] leading-6 text-ink"><MathText text={draft.stem || "（尚未填写题干）"} /></div></div>
                </>
              )}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
