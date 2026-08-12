"use client";

// 练习（F5）：发起表单 → SSE 流（过程时间线 + interrupt 蓝图确认）→ resume →
// 题目作答（四种控件，MathText 渲染）→ 逐题结果 + 掌握度变化；历史卷可重开。

import Link from "next/link";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { AppealEntry } from "@/components/appeal-entry";
import {
  Card,
  EmptyState,
  ErrorBanner,
  MasteryDelta,
  PageHeader,
  SkeletonList,
  Spinner,
  fmtDateTime,
  fmtPct,
} from "@/components/common";
import type { FlowItem } from "@/components/flow-timeline";
import { BlueprintCard } from "@/components/quiz/blueprint-card";
import { ExplainBackPanel } from "@/components/quiz/explain-back-panel";
import { PredictionSlider } from "@/components/quiz/prediction-slider";
import { QuizForm, type QuizFormValue } from "@/components/quiz/quiz-form";
import {
  QuestionAnswer,
  QuestionMeta,
} from "@/components/quiz/question-answer";
import { buildAttemptAnswers } from "@/components/quiz/submission";
import {
  hasPersistedQuizResult,
  quizOutcomeMessage,
  quizSseErrorMessage,
} from "@/app/quiz/quiz-flow";
import { adjustmentLabels } from "@/app/quiz/practice-recommendation";
import { ApiError, apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { MathText } from "@/lib/katex";
import { streamSse } from "@/lib/sse";
import type {
  AttemptResult,
  Blueprint,
  ErrorEventData,
  GraphDoneData,
  GraphInterruptData,
  GraphTraceData,
  KbOut,
  MetacognitionSummary,
  NodeUpdateData,
  PracticeRecommendation,
  QuizDetail,
  QuizListItem,
  QuizQuestion,
  WeakPointItem,
  WeakPointsResponse,
} from "@/lib/types";

type Phase = "idle" | "streaming" | "await_confirm" | "resuming";
const PENDING_QUIZ_KEY = "suwen:pending-quiz-draft";

interface PendingQuizDraft {
  status: "generating" | "await_confirm" | "resuming" | "failed";
  interrupt?: GraphInterruptData;
  topic: string;
  startedAt: number;
  requestBody?: Record<string, unknown>;
  requestPath?: string;
  failureOutcome?: GraphDoneData["outcome"];
}

function pendingQuizKey(userId: string): string {
  return `${PENDING_QUIZ_KEY}:${userId}`;
}

function readPendingQuiz(userId: string): PendingQuizDraft | null {
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

function clearPendingQuiz(userId: string): void {
  window.localStorage.removeItem(pendingQuizKey(userId));
}

function writePendingQuiz(userId: string, value: PendingQuizDraft): void {
  window.localStorage.setItem(pendingQuizKey(userId), JSON.stringify(value));
}

/** 后端下发的逐 KC 难度分档理由（P1-1 可解释出题：定性区间词，不含掌握度数值）。 */
interface DifficultyReason {
  kc_name: string;
  zone: string;
  difficulty: string;
  reason: string;
}

/** 薄弱点预览项（/api/profile/weak-points 已附带 zone/reason 定性理由字段）。 */
type WeakPointPreview = WeakPointItem & {
  zone?: string;
  difficulty?: string;
  reason?: string;
};

// useSyncExternalStore 订阅占位：URL search 在页面生命周期内不变化（跳转即重挂载）
const emptySubscribe = () => () => {};

interface ActiveQuiz {
  id: string;
  title: string;
  questions: QuizQuestion[];
}

function PracticeSteps({ current }: { current: number }) {
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

export default function QuizPage() {
  const { user } = useAuth();
  const userId = user?.id ?? "anonymous";
  // URL 预填（/quiz?topic=..&kb=..，来自对话页「考考我」chip）。
  // SSR 快照为空串、客户端快照读真实 search：无水合不匹配，也无需 Suspense。
  const search = useSyncExternalStore(
    emptySubscribe,
    () => window.location.search,
    () => "",
  );
  const urlParams = new URLSearchParams(search);
  const prefillTopic = urlParams.get("topic") ?? "";
  const prefillKb = urlParams.get("kb") ?? "";
  const rawQuestionCount = Number(urlParams.get("count") ?? "");
  const prefillQuestionCount = Number.isInteger(rawQuestionCount) && rawQuestionCount >= 1 && rawQuestionCount <= 20 ? rawQuestionCount : undefined;
  const prefillDifficulty = ["easy", "mid", "hard"].includes(urlParams.get("difficulty") ?? "")
    ? (urlParams.get("difficulty") as "easy" | "mid" | "hard")
    : undefined;
  const planId = urlParams.get("plan_id") ?? "";
  const taskKey = urlParams.get("task_key") ?? "";
  // /quiz?weak=1（画像页/学情报告页的「针对此点练习」）：高亮薄弱点定向出题卡片
  const fromWeakLink = urlParams.get("weak") === "1";
  // /quiz?kc=<KC名>&n=3（画像页「验证一下」，P2-6 可争辩画像）：
  // 该 KC 定向出 n 题，判分走既有 BKT 直写链路 = 掌握度重估，不走薄弱点通道
  const verifyKc = urlParams.get("kc") ?? "";
  const rawVerifyN = Number(urlParams.get("n") ?? "3");
  const verifyCount =
    Number.isInteger(rawVerifyN) && rawVerifyN >= 1 && rawVerifyN <= 20
      ? rawVerifyN
      : 3;

  const [kbs, setKbs] = useState<KbOut[]>([]);
  const [quizzes, setQuizzes] = useState<QuizListItem[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  // 画像联动出题（M4-D2）：预览与实际出题共用后端 weak_point_kcs，所见即所练
  const [weak, setWeak] = useState<WeakPointsResponse | null>(null);
  // 有无薄弱点决定文案与题量，但**不再决定按钮能否点击**：无薄弱点时后端走
  // Assessment Mode 出诊断题（证据不足是正常起点，不是错误状态）。
  const hasWeakPoints = (weak?.weak_points.length ?? 0) > 0;
  const [recommendation, setRecommendation] = useState<PracticeRecommendation | null>(null);
  const [recommendationError, setRecommendationError] = useState<string | null>(null);
  // A3 真实规模：多科目学生的薄弱点若不限库，会出成「高数+物理」混卷（"明天考高数"
  // 的场景不可用）。空串 = 全部科目；预览与出题共用同一个作用域，所见即所练。
  const [weakKbId, setWeakKbId] = useState("");
  const weakKbIdRef = useRef("");
  // 科目快速切换时，只允许最后一次请求更新预览，避免旧响应覆盖新 tab。
  const weakRequestSeqRef = useRef(0);

  // SSE 出题流
  const [phase, setPhase] = useState<Phase>("idle");
  const [, setTimeline] = useState<FlowItem[]>([]);
  const [interruptData, setInterruptData] = useState<GraphInterruptData | null>(null);
  const [flowError, setFlowError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const topicRef = useRef("");
  const abortRef = useRef<AbortController | null>(null);

  // 作答
  const [quiz, setQuiz] = useState<ActiveQuiz | null>(null);
  const [quizLoading, setQuizLoading] = useState(false);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [predictions, setPredictions] = useState<Record<string, number>>({});
  const [submitting, setSubmitting] = useState(false);
  const [attempt, setAttempt] = useState<AttemptResult | null>(null);
  const [calibration, setCalibration] = useState<MetacognitionSummary | null>(null);
  const [resultFilter, setResultFilter] = useState<"all" | "correct" | "wrong">("all");
  const [showResultDetails, setShowResultDetails] = useState(true);

  // 首个 setState 在 await 之后（effect 内不允许同步 setState）
  const loadQuizzes = useCallback(async () => {
    try {
      const list = await apiJson<QuizListItem[]>("/api/quiz");
      setQuizzes(list);
      setListError(null);
    } catch (err) {
      setQuizzes((prev) => prev ?? []);
      setListError(err instanceof ApiError ? err.message : "历史练习加载失败");
    }
  }, []);

  const weakPointsPath = useCallback(
    (kbId: string) =>
      `/api/profile/weak-points${kbId ? `?kb_id=${encodeURIComponent(kbId)}` : ""}`,
    [],
  );

  const loadRecommendation = useCallback(async () => {
    try {
      const next = await apiJson<PracticeRecommendation>("/api/practice/next");
      setRecommendation(next);
      setRecommendationError(null);
      return next;
    } catch (err) {
      setRecommendationError(err instanceof ApiError ? err.message : "下一步建议加载失败");
      return null;
    }
  }, []);

  // 首屏并行拉取历史练习 / 知识库 / 薄弱点预览（effect 内联 IIFE，同 chat 页模式）
  useEffect(() => {
    let alive = true;
    (async () => {
      const [quizzesRes, kbsRes, weakRes] = await Promise.allSettled([
        apiJson<QuizListItem[]>("/api/quiz"),
        apiJson<KbOut[]>("/api/kb"),
        apiJson<WeakPointsResponse>(weakPointsPath(weakKbId)),
      ]);
      if (!alive) return;
      if (quizzesRes.status === "fulfilled") setQuizzes(quizzesRes.value);
      else {
        setQuizzes([]);
        setListError("历史练习加载失败");
      }
      setKbs(kbsRes.status === "fulfilled" ? kbsRes.value : []);
      setWeak(
        weakRes.status === "fulfilled"
          ? weakRes.value
          : { threshold: 0.6, limit: 5, weak_points: [] },
      );
      void loadRecommendation();
      const pending = readPendingQuiz(userId);
      if (pending) {
        topicRef.current = pending.topic;
        if (pending.status === "failed") {
          setNotice(quizOutcomeMessage(pending.failureOutcome));
        }
        setInterruptData(pending.interrupt ?? null);
        setPhase(
          pending.status === "generating"
            ? "streaming"
            : pending.status === "resuming"
              ? "resuming"
              : pending.status === "await_confirm"
                ? "await_confirm"
                : "idle",
        );
      }
    })();
    return () => {
      alive = false;
    };
    // 只在首屏跑一次: weakKbId 初值恒为空串, 后续切换由 pickWeakKb 自己重拉,
    // 放进依赖会让每次切科目多打一轮首屏三连请求
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadRecommendation, userId]);

  useEffect(() => {
    if (phase !== "streaming" && phase !== "resuming") return;
    const timer = setInterval(() => {
      const pending = readPendingQuiz(userId);
      if (!pending) {
        setPhase("idle");
        void loadQuizzes();
        return;
      }
      if (pending.status === "await_confirm" && pending.interrupt) {
        topicRef.current = pending.topic;
        setInterruptData(pending.interrupt);
        setPhase("await_confirm");
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [phase, loadQuizzes, userId]);

  /** 切换薄弱点科目作用域：重拉预览，保证「所见即所练」。 */
  const pickWeakKb = useCallback(
    async (kbId: string) => {
      if (kbId === weakKbIdRef.current) return;
      weakKbIdRef.current = kbId;
      const requestSeq = ++weakRequestSeqRef.current;
      setWeakKbId(kbId);
      setWeak(null);
      try {
        const nextWeak = await apiJson<WeakPointsResponse>(weakPointsPath(kbId));
        if (requestSeq === weakRequestSeqRef.current && weakKbIdRef.current === kbId) {
          setWeak(nextWeak);
        }
      } catch {
        if (requestSeq === weakRequestSeqRef.current && weakKbIdRef.current === kbId) {
          setWeak({ threshold: 0.6, limit: 5, weak_points: [] });
        }
      }
    },
    [weakPointsPath],
  );

  /** 消费一段图执行 SSE（首次 POST /api/quiz 或 resume）。 */
  const consume = useCallback(
    async (path: string, body: unknown) => {
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        for await (const msg of streamSse(path, body, {
          signal: controller.signal,
        })) {
          switch (msg.event) {
            case "node_update":
              setTimeline((prev) => [
                ...prev,
                { kind: "node", data: msg.data as NodeUpdateData },
              ]);
              break;
            case "trace_event":
              setTimeline((prev) => [
                ...prev,
                { kind: "trace", data: msg.data as GraphTraceData },
              ]);
              break;
            case "interrupt": {
              const interrupt = msg.data as GraphInterruptData;
              const pending = readPendingQuiz(userId);
              writePendingQuiz(userId, {
                status: "await_confirm",
                interrupt,
                topic: topicRef.current,
                startedAt: pending?.startedAt ?? Date.now(),
                ...(pending?.requestBody ? { requestBody: pending.requestBody } : {}),
                ...(pending?.requestPath ? { requestPath: pending.requestPath } : {}),
              });
              setInterruptData(interrupt);
              setPhase("await_confirm");
              break;
            }
            case "done": {
              const d = msg.data as GraphDoneData;
              const succeeded = hasPersistedQuizResult(d);
              if (succeeded) {
                setQuiz({
                  id: d.result.quiz_id,
                  title: topicRef.current || "本次练习",
                  questions: d.result.questions,
                });
                setAnswers({});
                setPredictions({});
                setAttempt(null);
                setCalibration(null);
                setNotice(null);
              } else {
                setNotice(quizOutcomeMessage(d.outcome));
                const pending = readPendingQuiz(userId);
                const retryable = d.error?.retryable ?? d.outcome !== "cancelled_by_user";
                if (pending?.requestBody && retryable) {
                  writePendingQuiz(userId, {
                    ...pending,
                    status: "failed",
                    failureOutcome: d.outcome,
                  });
                } else if (!retryable) {
                  clearPendingQuiz(userId);
                }
              }
              if (succeeded) clearPendingQuiz(userId);
              setPhase("idle");
              setInterruptData(null);
              void loadQuizzes();
              void loadRecommendation();
              break;
            }
            case "error": {
              const d = msg.data as ErrorEventData;
              setFlowError(quizSseErrorMessage(d));
              const pending = readPendingQuiz(userId);
              if (path.includes("/api/threads/") && pending?.interrupt) {
                writePendingQuiz(userId, { ...pending, status: "await_confirm" });
                setPhase("await_confirm");
              } else if (pending?.requestBody) {
                writePendingQuiz(userId, { ...pending, status: "failed" });
                setPhase("idle");
              } else {
                clearPendingQuiz(userId);
                setPhase("idle");
              }
              break;
            }
            default:
              break; // 未知事件向前兼容：忽略
          }
        }
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") return;
        setFlowError(
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : "网络异常，请稍后重试",
        );
        const pending = readPendingQuiz(userId);
        if (path.includes("/api/threads/") && pending?.interrupt) {
          writePendingQuiz(userId, { ...pending, status: "await_confirm" });
          setPhase("await_confirm");
        } else if (pending?.requestBody) {
          writePendingQuiz(userId, { ...pending, status: "failed" });
          setPhase("idle");
        } else {
          clearPendingQuiz(userId);
          setPhase("idle");
        }
      } finally {
        abortRef.current = null;
        // 流意外结束（无 done/interrupt）时兜底回到空闲态
        setPhase((p) => (p === "await_confirm" ? p : "idle"));
      }
    },
    [loadQuizzes, loadRecommendation, userId],
  );

  const startQuiz = useCallback(
    (value: QuizFormValue) => {
      clearPendingQuiz(userId);
      topicRef.current = value.topic;
      const requestBody = {
        ...value,
        ...(planId && taskKey ? { plan_id: planId, task_key: taskKey } : {}),
      };
      writePendingQuiz(userId, {
        status: "generating",
        topic: value.topic,
        startedAt: Date.now(),
        requestBody,
        requestPath: "/api/quiz",
      });
      setTimeline([]);
      setInterruptData(null);
      setFlowError(null);
      setNotice(null);
      setQuiz(null);
      setAttempt(null);
      setPredictions({});
      setCalibration(null);
      setPhase("streaming");
      void consume("/api/quiz", requestBody);
    },
    [consume, planId, taskKey, userId],
  );

  const startRecommended = useCallback(
    (requestBody: Record<string, unknown>, title: string) => {
      clearPendingQuiz(userId);
      topicRef.current = title;
      writePendingQuiz(userId, {
        status: "generating",
        topic: title,
        startedAt: Date.now(),
        requestBody,
        requestPath: "/api/quiz/recommended",
      });
      setTimeline([]);
      setInterruptData(null);
      setFlowError(null);
      setNotice(null);
      setQuiz(null);
      setAttempt(null);
      setPredictions({});
      setCalibration(null);
      setPhase("streaming");
      void consume("/api/quiz/recommended", requestBody);
    },
    [consume, userId],
  );

  const startAdjustment = useCallback(
    (adjustment: "easier" | "harder" | "switch_kc" | "switch_kb") => {
      if (!quiz) return;
      startRecommended(
        { previous_quiz_id: quiz.id, adjustment },
        adjustment === "easier"
          ? "简单一点的练习"
          : adjustment === "harder"
            ? "难一点的练习"
            : adjustment === "switch_kc"
              ? "换个知识点练习"
              : "换个科目练习",
      );
    },
    [quiz, startRecommended],
  );

  const retryFailedQuiz = useCallback(() => {
    const pending = readPendingQuiz(userId);
    if (!pending?.requestBody) return;
    const retryContext = { ...pending };
    delete retryContext.interrupt;
    delete retryContext.failureOutcome;
    topicRef.current = pending.topic;
    writePendingQuiz(userId, {
      ...retryContext,
      status: "generating",
      startedAt: Date.now(),
    });
    setNotice(null);
    setFlowError(null);
    setPhase("streaming");
      void consume(pending.requestPath || "/api/quiz", pending.requestBody);
  }, [consume, userId]);

  /** 画像定向出题：kc_names 与难度分布都由后端按掌握度计算；前端只声明意图。 */
  const startWeakQuiz = useCallback(() => {
    const points = weak?.weak_points ?? [];
    const requestBody = {
      from_weak_points: true,
      // 无薄弱点时后端走 Assessment Mode 自选诊断 KC，这里给固定题量；
      // 早退会让冷启动用户点不动按钮（要有薄弱点才能出题，要出题才有薄弱点）。
      num_questions: points.length > 0 ? points.length : 5,
      // 与预览同一作用域：选了科目就只出该库的题，不跨学科混卷
      ...(weakKbId ? { kb_id: weakKbId } : {}),
    };
    clearPendingQuiz(userId);
    topicRef.current = points.length > 0 ? "薄弱点强化练习" : "水平诊断练习";
    writePendingQuiz(userId, {
      status: "generating",
      topic: topicRef.current,
      startedAt: Date.now(),
      requestBody,
    });
    setTimeline([]);
    setInterruptData(null);
    setFlowError(null);
    setNotice(null);
    setQuiz(null);
    setAttempt(null);
    setPredictions({});
    setCalibration(null);
    setPhase("streaming");
    void consume("/api/quiz", requestBody);
  }, [weak, weakKbId, consume, userId]);

  /** 掌握度验证出题（P2-6 可争辩画像）：主题=该 KC、kc_names=[该KC]、题数默认 3。
   *  难度三档均摊（重估需跨难度采样）；蓝图节点保证题数恰为 verifyCount。 */
  const startVerifyQuiz = useCallback(() => {
    if (!verifyKc) return;
    const base = Math.floor(verifyCount / 3);
    const rem = verifyCount % 3;
    const requestBody = {
      topic: verifyKc,
      kc_names: [verifyKc],
      num_questions: verifyCount,
      difficulty_mix: {
        easy: base + (rem > 0 ? 1 : 0),
        mid: base + (rem > 1 ? 1 : 0),
        hard: base,
      },
    };
    clearPendingQuiz(userId);
    topicRef.current = `验证：${verifyKc}`;
    writePendingQuiz(userId, {
      status: "generating",
      topic: topicRef.current,
      startedAt: Date.now(),
      requestBody,
    });
    setTimeline([]);
    setInterruptData(null);
    setFlowError(null);
    setNotice(null);
    setQuiz(null);
    setAttempt(null);
    setPredictions({});
    setCalibration(null);
    setPhase("streaming");
    void consume("/api/quiz", requestBody);
  }, [verifyKc, verifyCount, consume, userId]);

  const resume = useCallback(
    (approved: boolean, blueprint?: Blueprint) => {
      if (!interruptData) return;
      const pending = readPendingQuiz(userId);
      writePendingQuiz(userId, {
        ...(pending ?? {}),
        status: "resuming",
        interrupt: interruptData,
        topic: topicRef.current,
        startedAt: pending?.startedAt ?? Date.now(),
      });
      setPhase("resuming");
      void consume(interruptData.resume_url, {
        value: approved ? { approved: true, blueprint } : { approved: false },
      });
    },
    [interruptData, consume, userId],
  );

  const openQuiz = useCallback(async (id: string) => {
    setQuizLoading(true);
    setNotice(null);
    setFlowError(null);
    try {
      const detail = await apiJson<QuizDetail>(`/api/quiz/${id}`);
      setQuiz({
        id: detail.id,
        title: detail.title || "历史练习",
        questions: detail.questions,
      });
      setAnswers({});
      setPredictions({});
      setAttempt(null);
      setCalibration(null);
      setTimeline([]);
    } catch (err) {
      setFlowError(err instanceof ApiError ? err.message : "练习加载失败");
    } finally {
      setQuizLoading(false);
    }
  }, []);

  const submitAnswers = useCallback(async () => {
    if (!quiz || submitting) return;
    setSubmitting(true);
    setFlowError(null);
    try {
      const result = await apiJson<AttemptResult>(`/api/quiz/${quiz.id}/attempts`, {
        body: {
          answers: buildAttemptAnswers(quiz.questions, answers, predictions),
        },
      });
      setAttempt(result);
      setResultFilter("all");
      setShowResultDetails(true);
      void apiJson<MetacognitionSummary>("/api/metacognition/summary?window=week")
        .then(setCalibration)
        .catch(() => setCalibration(null));
      try {
        // 判分即写 BKT，薄弱点随之变化：立刻刷新预览（"上午答错，下午就在卷子上"）
        const currentKbId = weakKbIdRef.current;
        const requestSeq = ++weakRequestSeqRef.current;
        const nextWeak = await apiJson<WeakPointsResponse>(weakPointsPath(currentKbId));
        if (
          requestSeq === weakRequestSeqRef.current &&
          weakKbIdRef.current === currentKbId
        ) {
          setWeak(nextWeak);
        }
      } catch {
        // 薄弱点刷新失败不影响判分结果展示
      }
    } catch (err) {
      setFlowError(err instanceof ApiError ? err.message : "提交失败，请重试");
    } finally {
      setSubmitting(false);
    }
  }, [quiz, answers, predictions, submitting, weakPointsPath]);

  const busy = phase === "streaming" || phase === "resuming";
  // 定向出题的逐 KC 分档理由（interrupt 负载平铺，普通出题为空数组）
  const difficultyReasons =
    (interruptData?.difficulty_reasons as DifficultyReason[] | undefined) ?? [];
  const resultByQid = new Map(
    (attempt?.results ?? []).map((r) => [r.question_id, r]),
  );
  const visibleQuestions = (quiz?.questions ?? []).filter((question) => {
    if (!attempt || resultFilter === "all") return true;
    const result = resultByQid.get(question.id);
    return resultFilter === "correct" ? result?.correct : result && !result.correct;
  });
  const wrongQuestions = (quiz?.questions ?? []).filter(
    (question) => attempt && resultByQid.get(question.id)?.correct === false,
  );
  const answeredCount = quiz
    ? quiz.questions.filter((q) => (answers[q.id] ?? "").trim()).length
    : 0;
  const practiceStage = attempt ? 4 : quiz ? 3 : phase === "await_confirm" || phase === "resuming" ? 2 : 1;

  return (
    <div className="mx-auto w-full max-w-[1120px] space-y-5 px-5 py-8 sm:px-8 lg:px-10 lg:py-10">
      {practiceStage > 1 && <PracticeSteps current={practiceStage} />}
      {flowError && (
        <ErrorBanner message={flowError} onClose={() => setFlowError(null)} />
      )}
      {notice && (
        <div className="flex items-center justify-between rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-700">
          <span>{notice}</span>
          {readPendingQuiz(userId)?.status === "failed" && (
            <button type="button" className="ml-3 underline" onClick={retryFailedQuiz}>重试</button>
          )}
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="text-amber-400 hover:text-amber-600"
            aria-label="关闭提示"
          >
            ✕
          </button>
        </div>
      )}

      {quiz ? (
        /* ---- 作答 / 结果视图 ---- */
        <div className="space-y-4">
          <PageHeader
            title={<MathText text={quiz.title} />}
            subtitle={
              <>
                共 {quiz.questions.length} 题
                {attempt
                  ? ` · 答对 ${attempt.correct}/${attempt.total}`
                  : ` · 已作答 ${answeredCount}/${quiz.questions.length}`}
              </>
            }
            action={
              <button
                type="button"
                onClick={() => {
                  setQuiz(null);
                  setAttempt(null);
                  setPredictions({});
                  setCalibration(null);
                }}
                className="rounded-control border border-line-strong bg-white px-3.5 py-1.5 text-sm font-medium text-ink-secondary transition-colors hover:border-ink-muted hover:text-ink"
              >
                返回列表
              </button>
            }
          />

          {attempt && (
            <section className="rounded-[14px] border border-line bg-white p-5">
              <div className="flex flex-wrap items-center gap-5">
                <div className="flex h-16 w-16 shrink-0 flex-col items-center justify-center rounded-full border-[4px] border-brand-600"><strong className="text-[17px] leading-5 text-ink">{attempt.correct}/{attempt.total}</strong><span className="text-caption text-ink-muted">答对</span></div>
                <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h2 className="text-[14px] font-semibold text-ink">正确率 {Math.round((attempt.correct / Math.max(attempt.total, 1)) * 100)}%</h2><span className={`rounded-full px-2 py-0.5 text-caption font-medium ${attempt.correct / Math.max(attempt.total, 1) >= 0.6 ? "bg-success-50 text-success-700" : "bg-warning-50 text-warning-700"}`}>{attempt.correct / Math.max(attempt.total, 1) >= 0.8 ? "完成良好" : "建议巩固"}</span></div><p className="mt-1 text-body-sm leading-5 text-ink-muted">结果来自本次真实判分；知识点掌握变化已按证据写入学习画像。</p></div>
              </div>
              <div className="mt-5 grid gap-3 border-t border-line pt-4 sm:grid-cols-2"><div><p className="text-caption font-semibold text-success-700">● 表现较好</p><ul className="mt-2 space-y-1.5">{attempt.results.filter((result) => result.correct).slice(0, 3).map((result, index) => <li key={result.question_id} className="rounded-[7px] bg-[#f5f4f1] px-3 py-2 text-body-sm text-ink-secondary">第 {quiz.questions.findIndex((question) => question.id === result.question_id) + 1 || index + 1} 题 · 回答正确</li>)}</ul></div><div><p className="text-caption font-semibold text-warning-700">● 仍需巩固</p><ul className="mt-2 space-y-1.5">{attempt.results.filter((result) => !result.correct).slice(0, 3).map((result, index) => <li key={result.question_id} className="rounded-[7px] bg-[#f5f4f1] px-3 py-2 text-body-sm text-ink-secondary"><span>第 {quiz.questions.findIndex((question) => question.id === result.question_id) + 1 || index + 1} 题 · </span><MathText text={result.scored_reason || "查看解析后再次练习"} /></li>)}</ul></div></div>
              {attempt.plan_adjustment && <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-[9px] border border-warning-200 bg-warning-50 px-4 py-3"><div><p className="text-body-sm font-semibold text-warning-700">学习计划已更新</p><div className="mt-0.5 text-body-sm text-warning-600"><MathText text={attempt.plan_adjustment.reason} /></div></div><Link href={`/plans?id=${attempt.plan_adjustment.plan_id}`} className="rounded-[7px] border border-warning-200 bg-white px-3 py-1.5 text-body-sm font-medium text-warning-700">前往学习计划 →</Link></div>}
              {calibration && calibration.sample_n > 0 && (
                <div className="mt-4 rounded-[9px] border border-brand-100 bg-brand-50/45 px-4 py-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-body-sm font-semibold text-brand-800">近 7 天自我判断校准</p>
                    <span className="text-caption text-brand-700">{calibration.sample_n} 次有效预测</span>
                  </div>
                  <div className="mt-2 grid gap-2 sm:grid-cols-2">
                    <p className="rounded-[7px] bg-white/80 px-3 py-2 text-body-sm text-ink-secondary">
                      Brier 误差：<strong className="text-ink">{calibration.brier_score == null ? "—" : calibration.brier_score.toFixed(3)}</strong>
                      <span className="ml-1 text-caption text-ink-muted">越低表示预测越贴近结果</span>
                    </p>
                    <p className="rounded-[7px] bg-white/80 px-3 py-2 text-body-sm text-ink-secondary">
                      高信心答错率：<strong className="text-ink">{calibration.overconfidence_rate == null ? "样本不足" : fmtPct(calibration.overconfidence_rate)}</strong>
                    </p>
                  </div>
                  <p className="mt-2 text-caption leading-4 text-ink-muted">
                    {calibration.sample_n < 5
                      ? "当前样本较少，只展示记录，不对学习能力作评价。"
                      : "该指标只帮助校准信心，不参与正式评分、掌握度或学习画像。"}
                  </p>
                </div>
              )}
              <div className="mt-4 grid gap-2 border-t border-line pt-4 sm:grid-cols-2 lg:grid-cols-4">
                <button type="button" disabled={wrongQuestions.length === 0} onClick={() => { setQuiz({ ...quiz, title: `${quiz.title} · 错题重练`, questions: wrongQuestions }); setAnswers({}); setPredictions({}); setAttempt(null); setCalibration(null); setResultFilter("all"); setShowResultDetails(true); }} className="rounded-[8px] bg-brand-600 px-3 py-2 text-body-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-45">重练本次错题</button>
                <Link href="/tutor" className="rounded-[8px] border border-brand-200 bg-brand-50 px-3 py-2 text-center text-body-sm font-medium text-brand-700">进入分级辅导</Link>
                <Link href="/profile" className="rounded-[8px] border border-line-strong px-3 py-2 text-center text-body-sm font-medium text-ink-secondary">查看学习画像</Link>
                <button type="button" onClick={() => { setQuiz(null); setAttempt(null); setAnswers({}); setPredictions({}); setCalibration(null); }} className="rounded-[8px] border border-line-strong px-3 py-2 text-body-sm font-medium text-ink-secondary">返回智能练习</button>
              </div>
              <div className="mt-3 border-t border-line pt-3">
                <p className="text-body-sm font-medium text-ink-secondary">想继续练？可以事后调整</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {adjustmentLabels(recommendation?.adjustments ?? []).map(([value, label]) => (
                    <button key={value} type="button" onClick={() => startAdjustment(value)} className="rounded-[7px] border border-line-strong bg-white px-3 py-1.5 text-body-sm font-medium text-ink-secondary hover:border-brand-300 hover:text-brand-700">
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
                <div className="flex gap-1.5" aria-label="筛选答题结果">
                  {([['all', `全部 ${attempt.total}`], ['wrong', `错题 ${attempt.total - attempt.correct}`], ['correct', `正确 ${attempt.correct}`]] as const).map(([value, label]) => <button key={value} type="button" onClick={() => setResultFilter(value)} className={`rounded-[7px] px-2.5 py-1 text-body-sm font-medium ${resultFilter === value ? "bg-brand-50 text-brand-700" : "text-ink-muted hover:bg-surface-muted"}`}>{label}</button>)}
                </div>
                <button type="button" onClick={() => setShowResultDetails((value) => !value)} className="text-body-sm font-medium text-brand-700">{showResultDetails ? "收起逐题结果⌃" : "展开逐题结果⌄"}</button>
              </div>
            </section>
          )}

          {quiz.questions.length === 0 ? (
            <EmptyState
              title="这份练习没有题目"
              description="可能生成未完成，请重新发起"
              illustration="target"
            />
          ) : (!attempt || showResultDetails) ? (
            <ol className="space-y-4">
              {visibleQuestions.map((q) => {
                const i = quiz.questions.findIndex((question) => question.id === q.id);
                const result = resultByQid.get(q.id);
                return (
                  <li
                    key={q.id}
                    className="rounded-overlay border border-line bg-white p-5"
                  >
                    <div className="mb-2 flex items-start justify-between gap-3">
                      <p className="min-w-0 text-sm leading-relaxed text-slate-800">
                        <span className="mr-1.5 font-semibold text-indigo-600">
                          {i + 1}.
                        </span>
                        <MathText text={q.stem} />
                      </p>
                      <QuestionMeta question={q} />
                    </div>

                    <QuestionAnswer
                      question={q}
                      value={answers[q.id] ?? ""}
                      disabled={Boolean(attempt) || submitting}
                      onChange={(v) =>
                        setAnswers((prev) => ({ ...prev, [q.id]: v }))
                      }
                    />

                    {!attempt && (
                      <PredictionSlider
                        questionNumber={i + 1}
                        value={predictions[q.id]}
                        disabled={submitting}
                        onChange={(value) =>
                          setPredictions((current) => {
                            if (value == null) {
                              const next = { ...current };
                              delete next[q.id];
                              return next;
                            }
                            return { ...current, [q.id]: value };
                          })
                        }
                      />
                    )}

                    {result && (
                      <div
                        className={`edu-fade-up mt-3 rounded-xl border px-3.5 py-2.5 text-sm ${
                          result.correct
                            ? "border-emerald-200 bg-emerald-50/60"
                            : "border-red-200 bg-red-50/60"
                        }`}
                        style={{ animationDelay: `${Math.min(i, 12) * 90}ms` }}
                      >
                        <p
                          className={`flex items-center gap-1.5 font-semibold ${
                            result.correct ? "text-emerald-700" : "text-red-700"
                          }`}
                        >
                          {/* 对错图标 draw 入场动画（reduced-motion 直接呈现终态） */}
                          <svg
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2.6"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            className="h-4 w-4 shrink-0"
                            aria-hidden
                          >
                            {result.correct ? (
                              <path className="edu-draw" d="M20 6 9 17l-5-5" />
                            ) : (
                              <>
                                <path className="edu-draw" d="M18 6 6 18" />
                                <path
                                  className="edu-draw"
                                  style={{ animationDelay: "0.32s" }}
                                  d="m6 6 12 12"
                                />
                              </>
                            )}
                          </svg>
                          {result.correct ? "回答正确" : "回答错误"}
                          {result.llm_score != null && (
                            <span className="rounded-full bg-white px-2 py-0.5 text-caption font-medium text-slate-500">
                              AI 评分 {(result.llm_score * 100).toFixed(0)} 分
                            </span>
                          )}
                        </p>
                        {result.scored_reason && (
                          <p className="mt-1 text-caption text-slate-600">
                            <span className="font-medium text-slate-500">评分理由：</span>
                            <MathText text={result.scored_reason} />
                          </p>
                        )}
                        <p className="mt-1.5 text-caption text-slate-600">
                          <span className="font-medium text-slate-500">参考答案：</span>
                          <MathText text={result.answer} />
                        </p>
                        {result.explanation && (
                          <p className="mt-1 text-caption leading-relaxed text-slate-600">
                            <span className="font-medium text-slate-500">解析：</span>
                            <MathText text={result.explanation} />
                          </p>
                        )}
                        {result.mastery_changes.length > 0 && (
                          <p className="mt-2 flex flex-wrap gap-1.5">
                            {result.mastery_changes.map((c) => (
                              <MasteryDelta key={c.kc_id} change={c} />
                            ))}
                          </p>
                        )}
                        {/* 判分申诉（P2-3）：仅 LLM 评分的主观题可复核 */}
                        {q.type === "short" && result.llm_score != null && (
                          <AppealEntry questionId={q.id} />
                        )}
                        <ExplainBackPanel
                          kcId={q.kc_ids[0]}
                          promptSpanRef={`quiz:${quiz.id}:question:${q.id}`}
                        />
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
          ) : null}

          {!attempt && quiz.questions.length > 0 && (
            <div className="flex justify-end">
              <button
                type="button"
                disabled={submitting || answeredCount === 0}
                onClick={() => void submitAnswers()}
                className="flex items-center gap-2 rounded-control bg-ink px-6 py-2.5 text-sm font-semibold text-white hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
              >
                {submitting && (
                  <Spinner className="h-4 w-4 border-white/40 border-t-white" />
                )}
                {submitting ? "判分中…" : "提交答卷"}
              </button>
            </div>
          )}
        </div>
      ) : (
        /* ---- 发起 + 历史视图 ---- */
        <div className="space-y-4">
          <PageHeader
            title="智能练习"
            subtitle="根据你的学习状态选择个性化练习，或按主题和资料自定义一组题。"
          />
          <div className="space-y-4">
          <div className="min-w-0 flex-1 space-y-4">
            {/* 掌握度验证（P2-6 可争辩画像）：画像页「验证一下」跳转入口 */}
            {verifyKc && (
              <section className="rounded-2xl border border-indigo-400 bg-white p-4 shadow-[0_4px_20px_-6px_rgba(79,70,229,0.10)] ring-2 ring-indigo-100">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <h2 className="text-sm font-semibold text-slate-800">
                      验证「{verifyKc}」的掌握度
                    </h2>
                    <p className="mt-0.5 text-caption text-slate-500">
                      不同意画像？做 {verifyCount} 道该知识点的题，判分结果将作为
                      真实证据重估你的掌握度（与普通练习同一条链路）
                    </p>
                  </div>
                  <button
                    type="button"
                    autoFocus
                    disabled={busy}
                    onClick={startVerifyQuiz}
                    className="flex shrink-0 items-center gap-2 rounded-control bg-ink px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-black focus:ring-2 focus:ring-ink/20 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {busy && (
                      <Spinner className="h-4 w-4 border-white/40 border-t-white" />
                    )}
                    开始验证（{verifyCount} 题）
                  </button>
                </div>
              </section>
            )}

            {!fromWeakLink && recommendation && (
              <section className="rounded-[14px] border border-brand-200 bg-gradient-to-br from-brand-50/80 to-white p-5">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="min-w-0">
                    <h2 className="text-[16px] font-semibold text-ink">{recommendation.title}</h2>
                    <p className="mt-1 max-w-2xl text-sm leading-6 text-ink-secondary">{recommendation.reason}</p>
                  </div>
                  {recommendation.status === "ready" && recommendation.action === "quiz" && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => startRecommended({}, recommendation.title)}
                      className="flex shrink-0 items-center gap-2 rounded-[8px] bg-brand-600 px-5 py-2.5 text-sm font-medium text-white hover:bg-brand-800 focus:ring-2 focus:ring-brand-300 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {busy && <Spinner className="h-4 w-4 border-white/40 border-t-white" />}
                      开始练习{recommendation.question_count ? `（${recommendation.question_count} 题）` : ""}
                    </button>
                  )}
                  {recommendation.action === "review" && (
                    <Link href="/reviews" className="shrink-0 rounded-[8px] bg-brand-600 px-5 py-2.5 text-sm font-medium text-white">去复习</Link>
                  )}
                  {recommendation.action === "upload" && (
                    <Link href="/kb" className="shrink-0 rounded-[8px] bg-brand-600 px-5 py-2.5 text-sm font-medium text-white">上传资料</Link>
                  )}
                </div>
                {recommendation.status === "needs_scope" && (
                  <div className="mt-4 flex flex-wrap gap-2" aria-label="选择练习科目">
                    {recommendation.scope_options.map((option) => (
                      <button
                        key={option.id}
                        type="button"
                        disabled={busy}
                        onClick={() => startRecommended({ scope_kb_id: option.id }, recommendation.title)}
                        className="rounded-full border border-brand-200 bg-white px-3 py-1.5 text-sm text-brand-700 hover:bg-brand-50 disabled:opacity-50"
                      >
                        {option.name}
                      </button>
                    ))}
                  </div>
                )}
                {recommendation.status === "preparing" && (
                  <p className="mt-3 flex items-center gap-2 text-sm text-ink-muted"><Spinner className="h-4 w-4" />正在理解资料并整理知识点，完成后会自动更新建议。</p>
                )}
              </section>
            )}
            {recommendationError && !recommendation && (
              <ErrorBanner message={recommendationError} onRetry={() => void loadRecommendation()} />
            )}

            {/* 画像联动出题（M4-D2）：主按钮 + 将要练的 KC 预览 */}
            {(fromWeakLink || !recommendation) && <section
              className={`rounded-[14px] border bg-gradient-to-br from-brand-50/80 to-white p-5 ${
                fromWeakLink
                  ? "border-indigo-400 ring-2 ring-indigo-100"
                  : "border-line"
              }`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <h2 className="text-[16px] font-semibold text-ink">
                    为你推荐的练习
                  </h2>
                  <p className="mt-0.5 text-caption text-slate-500">
                    {hasWeakPoints
                      ? `根据真实学情画像自动选择知识点（掌握度 < ${weak ? fmtPct(weak.threshold) : "阈值"}，近 7 天错过的优先）`
                      : "还没测过你的水平，先做几道诊断题摸清底子"}
                  </p>
                </div>
                <button
                  type="button"
                  disabled={busy}
                  onClick={startWeakQuiz}
                  className="flex shrink-0 items-center gap-2 rounded-[8px] bg-brand-600 px-5 py-2.5 text-body-sm font-medium text-white transition-colors hover:bg-brand-800 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {busy && (
                    <Spinner className="h-4 w-4 border-white/40 border-t-white" />
                  )}
                  {hasWeakPoints ? "开始个性化练习" : "先做 5 道题，让苏问认识你"}
                </button>
              </div>

              {/* A3：多科目学生按科目圈定薄弱点，避免「高数+物理」混卷 */}
              {kbs.length > 1 && (
                <div className="mt-3 flex flex-wrap items-center gap-1.5">
                  <span className="text-caption text-slate-400">科目范围</span>
                  {[{ id: "", name: "全部" }, ...kbs.map((k) => ({ id: k.id, name: k.name }))].map(
                    (tab) => {
                      const on = weakKbId === tab.id;
                      return (
                        <button
                          key={tab.id || "__all__"}
                          type="button"
                          disabled={busy}
                          onClick={() => void pickWeakKb(tab.id)}
                          className={`rounded-full border px-2.5 py-1 text-caption font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                            on
                              ? "border-indigo-300 bg-indigo-50 text-indigo-700"
                              : "border-slate-200 bg-white text-slate-500 hover:border-slate-300"
                          }`}
                        >
                          {tab.name}
                        </button>
                      );
                    },
                  )}
                </div>
              )}

              {weak === null ? (
                <p className="mt-3 flex items-center gap-2 text-caption text-slate-400">
                  <Spinner className="h-3.5 w-3.5" /> 正在读取学情画像…
                </p>
              ) : !hasWeakPoints ? (
                <p className="mt-3 rounded-xl bg-info-50 px-3 py-2 text-caption text-info-700">
                  苏问会先出几道覆盖面广的诊断题量一下你的水平，判分后就能按掌握度定向出题了。
                </p>
              ) : (
                <div className="mt-3">
                  <p className="mb-1.5 text-caption text-slate-500">
                    将要练的 {weak.weak_points.length} 个知识点：
                  </p>
                  <ul className="flex flex-wrap gap-1.5">
                    {weak.weak_points.map((wp) => (
                      <li
                        key={wp.kc_id}
                        className="flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-caption text-amber-800"
                        title={`掌握度 ${fmtPct(wp.p_mastery)}`}
                      >
                        <span className="max-w-40 truncate font-medium">
                          {wp.name}
                        </span>
                        <span className="font-mono text-amber-600">
                          {fmtPct(wp.p_mastery)}
                        </span>
                        {wp.recent_error && (
                          <span className="rounded-full bg-red-100 px-1.5 py-0.5 text-caption font-medium text-red-700">
                            近期错过
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                  {/* 为什么练这些（P1-1）：后端逐 KC 分档理由，与实际难度分布同源 */}
                  {(weak.weak_points as WeakPointPreview[]).some((wp) => wp.reason) && (
                    <ul className="mt-2 space-y-1 border-t border-slate-100 pt-2">
                      {(weak.weak_points as WeakPointPreview[])
                        .filter((wp) => wp.reason)
                        .map((wp) => (
                          <li key={wp.kc_id} className="text-caption leading-relaxed text-slate-600">
                            <span className="font-medium text-slate-700">{wp.name}</span>
                            {wp.zone && (
                              <span className="mx-1 rounded-full bg-slate-100 px-1.5 py-0.5 text-caption font-medium text-slate-500">
                                {wp.zone}
                              </span>
                            )}
                            <MathText text={wp.reason ?? ""} />
                          </li>
                        ))}
                    </ul>
                  )}
                </div>
              )}
            </section>}

            <details className="group rounded-[14px] border border-line bg-white" open={Boolean(prefillTopic || prefillKb)}>
              <summary className="flex cursor-pointer list-none items-center justify-between px-5 py-4"><div><h2 className="text-[13px] font-semibold text-ink">自定义练习</h2><p className="mt-0.5 text-body-sm text-ink-muted">自选主题、资料范围和难度，生成一组专属练习</p></div><span className="text-body-sm font-medium text-brand-700 group-open:hidden">展开设置⌄</span><span className="hidden text-body-sm font-medium text-brand-700 group-open:inline">收起设置⌃</span></summary>
              <div className="border-t border-line p-5"><QuizForm key={`${prefillTopic}|${prefillKb}|${prefillQuestionCount ?? ""}|${prefillDifficulty ?? ""}`} kbs={kbs} busy={busy} onSubmit={startQuiz} initialTopic={prefillTopic} initialKbId={prefillKb} initialQuestionCount={prefillQuestionCount} initialDifficulty={prefillDifficulty} /></div>
            </details>

            {phase === "await_confirm" && interruptData?.blueprint && (
              <div className="space-y-2">
                {/* 为什么这套题（P1-1）：随 blueprint interrupt 下发的逐 KC 分档理由 */}
                {difficultyReasons.length > 0 && (
                  <div className="rounded-2xl border border-indigo-200 bg-indigo-50/50 p-4">
                    <h3 className="text-sm font-semibold text-slate-900">
                      为什么是这套题
                    </h3>
                    <p className="mt-0.5 text-caption text-slate-500">
                      难度由学情画像按掌握区间自动分档（可在下方蓝图中调整）
                    </p>
                    <ul className="mt-2 space-y-1">
                      {difficultyReasons.map((r) => (
                        <li
                          key={r.kc_name}
                          className="text-caption leading-relaxed text-slate-600"
                        >
                          <span className="font-medium text-slate-800">
                            {r.kc_name}
                          </span>
                          <span className="mx-1 rounded-full bg-indigo-100 px-1.5 py-0.5 text-caption font-medium text-indigo-700">
                            {r.zone}
                          </span>
                          <MathText text={r.reason} />
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                <BlueprintCard
                  key={interruptData.thread_id}
                  blueprint={interruptData.blueprint}
                  busy={false}
                  onConfirm={(bp) => resume(true, bp)}
                  onCancel={() => resume(false)}
                />
              </div>
            )}
            {phase === "resuming" && (
              <p className="flex items-center gap-2 text-sm text-slate-500">
                <Spinner /> 蓝图已提交，正在生成并校验题目…
              </p>
            )}
          </div>

          <section className="w-full">
            <Card title="最近练习">
              {listError && (
                <div className="mb-2">
                  <ErrorBanner message={listError} onRetry={() => void loadQuizzes()} />
                </div>
              )}
              {quizzes === null ? (
                <SkeletonList rows={4} />
              ) : quizzes.length === 0 ? (
                <p className="py-4 text-center text-caption text-slate-400">
                  还没有练习记录
                </p>
              ) : (
                <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {quizzes.map((qz) => (
                    <li key={qz.id}>
                      <button
                        type="button"
                        disabled={quizLoading}
                        onClick={() => void openQuiz(qz.id)}
                        className="w-full rounded-[10px] border border-line bg-[#fafaf9] px-4 py-3 text-left text-body-sm text-ink-secondary transition-colors hover:border-brand-200 hover:bg-brand-50/40 disabled:opacity-50"
                      >
                        <span className="block truncate font-medium">
                          <MathText text={qz.title || "未命名练习"} singleLine />
                        </span>
                        <span className="block text-caption text-slate-400">
                          {fmtDateTime(qz.created_at)}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </section>
          </div>
        </div>
      )}
    </div>
  );
}
