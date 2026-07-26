from __future__ import annotations

import orjson
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..deps import AuthContext, api_rate_limited
from ..services import finalize_plan, finalize_quiz

router = APIRouter(prefix="/api/threads", tags=["threads"])
log = get_logger("threads")


class ResumeIn(BaseModel):
    value: dict  # interrupt 决策 payload, 如 {"approved": true, "blueprint": {...}}


def _sse(data: dict) -> str:
    return orjson.dumps(data).decode()


async def stream_graph_run(
    runtime,
    kind: str,
    thread_id: str,
    ctx: AuthContext,
    payload: dict | None,
    resume=None,
    trace_prelude: list[dict] | None = None,
):
    """图执行 → SSE 事件流 (§6 协议): node_update/trace_event/interrupt/done。
    quiz/plan 完成时在 done 前落库并回传结果。M3-D2 的 interrupt→resume 协议载体。

    trace_prelude: 入图前由路由层决策产生的 trace 事件 (如画像定向出题选中的薄弱点),
    原样先行下发, 让"这卷题为什么是这些知识点"在前端时间线里可核对。
    """

    async def gen():
        for tr in trace_prelude or []:
            yield {"event": "trace_event", "data": _sse(tr)}
        try:
            async for ev in runtime.astream_updates(kind, thread_id, payload, resume=resume):
                if ev["kind"] == "token":
                    # 图内真流式 token (目前 qa generate 写出; 其余图无 writer 即无此事件)
                    yield {
                        "event": "token",
                        "data": _sse(
                            {"text": ev["text"], "attempt": ev.get("attempt", 0)}
                        ),
                    }
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
                    result: dict = {}
                    if kind == "quiz" and state.get("questions"):
                        result = await finalize_quiz(ctx, local_id, state)
                    elif kind == "plan" and state.get("plan_confirmed"):
                        result = await finalize_plan(ctx, local_id, state)
                    elif kind == "qa":
                        result = {
                            "answer": state.get("answer", ""),
                            "refused": state.get("refused", False),
                            "route": state.get("route", ""),
                        }
                    yield {
                        "event": "done",
                        "data": _sse({"thread_id": thread_id, "result": result}),
                    }
        except Exception:
            log.exception("graph_stream_failed", kind=kind)
            yield {
                "event": "error",
                "data": _sse({"code": "GRAPH_FAILED", "message": "执行失败，请重试"}),
            }

    return EventSourceResponse(gen())


@router.post("/{thread_id}/resume")
async def resume_thread(
    thread_id: str,
    body: ResumeIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    runtime = getattr(request.app.state, "graphs", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="GRAPHS_UNAVAILABLE")
    try:
        kind, user_id, _local = runtime.parse_thread_id(thread_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="NOT_FOUND") from None
    if user_id != str(ctx.user_id):
        raise HTTPException(status_code=404, detail="NOT_FOUND")  # 不泄漏存在性
    return await stream_graph_run(runtime, kind, thread_id, ctx, None, resume=body.value)
