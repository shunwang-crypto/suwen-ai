from __future__ import annotations

from typing import Any

import orjson
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..deps import AuthContext, api_rate_limited
from ..services import QuizKCError, finalize_plan, finalize_quiz

router = APIRouter(prefix="/api/threads", tags=["threads"])
log = get_logger("threads")

_QUIZ_OUTCOMES = {
    "succeeded",
    "cancelled_by_user",
    "generation_failed",
    "validation_failed",
}


class QuizFinalizeError(RuntimeError):
    """Raised when a quiz cannot produce a persisted, non-empty result."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _quiz_outcome(state: dict) -> str:
    outcome = state.get("terminal_outcome")
    if outcome in _QUIZ_OUTCOMES:
        return outcome
    if (
        state.get("blueprint_confirmed")
        and isinstance(state.get("questions"), list)
        and state.get("questions")
        and not state.get("validation_errors")
    ):
        # Compatibility for a checkpoint created just before outcome v2. This
        # uses the graph's explicit confirmation flag, never question count,
        # to decide whether the blueprint was approved.
        return "succeeded"
    # Older checkpoints have no terminal_outcome. Never infer user
    # cancellation from an empty result.
    return "validation_failed" if state.get("ever_generated_nonempty") else "generation_failed"


def _quiz_failure(outcome: str, state: dict) -> dict:
    if outcome == "cancelled_by_user":
        return {
            "code": "QUIZ_CANCELLED_BY_USER",
            "message": "已取消本次出题",
            "stage": "confirm",
            "retryable": False,
            "attempts": 0,
        }
    if outcome == "validation_failed":
        return {
            "code": "QUIZ_VALIDATION_FAILED",
            "message": "题目质量检查未通过",
            "stage": "validate",
            "retryable": True,
            "attempts": max(0, int(state.get("retry_count") or 0)),
        }
    return {
        "code": "QUIZ_GENERATION_EMPTY",
        "message": "这次没有生成出可用题目，请重试。",
        "stage": "generate",
        "retryable": True,
        "attempts": max(0, int(state.get("retry_count") or 0)),
    }


def _is_persisted_quiz_result(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and bool(result.get("quiz_id"))
        and isinstance(result.get("questions"), list)
        and bool(result["questions"])
    )


class ResumeIn(BaseModel):
    value: dict[str, Any] = Field(max_length=20)  # interrupt 决策 payload


def _sse(data: dict) -> str:
    return orjson.dumps(data).decode()


async def stream_task_run(
    runtime,
    kind: str,
    thread_id: str,
    ctx: AuthContext,
    payload: dict | None,
    resume=None,
    trace_prelude: list[dict] | None = None,
):
    """原生任务 → SSE 事件流 (§6 协议): node_update/trace_event/interrupt/done。
    quiz/plan 完成时在 done 前落库并回传结果。M3-D2 的 interrupt→resume 协议载体。

    trace_prelude: 任务开始前由路由层决策产生的 trace 事件 (如画像定向出题选中的薄弱点),
    原样先行下发, 让"这卷题为什么是这些知识点"在前端时间线里可核对。
    """

    async def gen():
        for tr in trace_prelude or []:
            yield {"event": "trace_event", "data": _sse(tr)}
        try:
            async for ev in runtime.stream_task(kind, thread_id, payload, resume=resume):
                if ev["kind"] == "token":
                    # 图内真流式 token (目前 qa generate 写出; 其余图无 writer 即无此事件)
                    yield {
                        "event": "token",
                        "data": _sse(
                            {"text": ev["text"], "attempt": ev.get("attempt", 0)}
                        ),
                    }
                elif ev["kind"] == "tool":
                    yield {"event": "node_update", "data": _sse({"node": ev["tool"], "status": ev["status"]})}
                elif ev["kind"] == "trace":
                    yield {"event": "trace_event", "data": _sse(ev["payload"])}
                elif ev["kind"] == "custom":
                    continue  # 未知自定义流事件: 向前兼容忽略
                elif ev["kind"] == "node":
                    delta = ev["delta"]
                    yield {
                        "event": "node_update",
                        "data": _sse({"node": ev["node"], "status": "done"}),
                    }
                    for tr in delta.get("trace") or []:
                        yield {"event": "trace_event", "data": _sse(tr)}
                elif ev["kind"] == "interrupt":
                    yield {
                        "event": "interrupt",
                        "data": _sse(
                            {
                                **ev["payload"],
                                "thread_id": thread_id,
                                "resume_url": f"/api/threads/{thread_id}/resume",
                            }
                        ),
                    }
                    return
                else:  # end
                    state = ev["state"]
                    _kind, _uid, local_id = runtime.parse_thread_id(thread_id)
                    result: dict | None = None
                    outcome: str | None = None
                    error: dict | None = None
                    if kind == "quiz":
                        outcome = _quiz_outcome(state)
                        if outcome == "succeeded":
                            # Only a successful transaction with a non-empty
                            # persisted result is allowed to emit succeeded.
                            if not isinstance(state.get("questions"), list) or not state.get(
                                "questions"
                            ):
                                raise QuizFinalizeError(
                                    "QUIZ_FINALIZE_INVALID",
                                    "题目保存结果无效，请重试",
                                )
                            try:
                                result = await finalize_quiz(ctx, local_id, state)
                            except QuizKCError as exc:
                                # KC readiness is a safe, actionable business
                                # error. Preserve its stable code over the
                                # generic finalize-failed infrastructure code.
                                raise QuizFinalizeError(
                                    exc.code,
                                    "练习知识点尚未准备好，请稍后重试",
                                ) from exc
                            except Exception as exc:  # noqa: BLE001 - convert DB/UoW failures to SSE code
                                log.exception("quiz_finalize_failed", kind=kind)
                                raise QuizFinalizeError(
                                    "QUIZ_FINALIZE_FAILED",
                                    "题目保存失败，请重试",
                                ) from exc
                            if not _is_persisted_quiz_result(result):
                                log.error("quiz_finalize_invalid_result", kind=kind)
                                raise QuizFinalizeError(
                                    "QUIZ_FINALIZE_INVALID",
                                    "题目保存结果无效，请重试",
                                )
                        else:
                            error = _quiz_failure(outcome, state)
                    elif kind == "plan" and state.get("plan_confirmed"):
                        result = await finalize_plan(ctx, local_id, state)
                    elif kind == "qa":
                        result = {
                            "answer": state.get("answer", ""),
                            "refused": state.get("refused", False),
                            "route": state.get("route", ""),
                        }
                    done_data = {"thread_id": thread_id}
                    if kind == "quiz":
                        done_data.update(
                            {
                                "schema_version": 2,
                                "outcome": outcome,
                                "result": result,
                                "error": error,
                            }
                        )
                    else:
                        done_data["result"] = result or {}
                    yield {
                        "event": "done",
                        "data": _sse(done_data),
                    }
        except QuizFinalizeError as exc:
            yield {
                "event": "error",
                "data": _sse({"code": exc.code, "message": exc.message}),
            }
        except Exception as exc:
            log.exception("agent_task_stream_failed", kind=kind)
            # GatewayError/BudgetExceeded already carry a safe public code;
            # unknown exceptions stay behind the generic infrastructure code.
            code = getattr(exc, "code", None)
            message = getattr(exc, "message", None)
            yield {
                "event": "error",
                "data": _sse(
                    {
                        "code": code if isinstance(code, str) and code else "GRAPH_FAILED",
                        "message": message
                        if isinstance(message, str) and message
                        else "执行失败，请重试",
                    }
                ),
            }

    return EventSourceResponse(gen())


@router.post("/{thread_id}/resume")
async def resume_thread(
    thread_id: str,
    body: ResumeIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    runtime = getattr(request.app.state, "harness", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="GRAPHS_UNAVAILABLE")
    try:
        kind, user_id, _local = runtime.parse_thread_id(thread_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="NOT_FOUND") from None
    if user_id != str(ctx.user_id):
        raise HTTPException(status_code=404, detail="NOT_FOUND")  # 不泄漏存在性
    return await stream_task_run(runtime, kind, thread_id, ctx, None, resume=body.value)
