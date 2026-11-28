"""Bounded delegation of independent native DSH sessions through domain tools.

The host admits child tasks and stores their deliverables; it never interprets
model phases or runs a model/tool loop. Each child uses the existing DSH SDK.
"""

from __future__ import annotations

import asyncio
import copy
import re
import time
import uuid
from dataclasses import replace

from ..gateway.base import BudgetExceeded, ChatResult, GatewayError

ROLES = {"researcher", "planner", "reviewer"}
INPUT_KEYS = (
    "tenant_id", "user_id", "goal", "current_level", "target_outcome", "success_criteria",
    "total_days", "budget_hours_per_day", "plan_mode", "kb_id", "kb_ids", "owner_ids",
    "diagnosis", "kc_order", "weak_kcs", "profile_summary", "profile_weak_points",
)


class TeamBudgetGateway:
    def __init__(self, gateway, state, settings, checkpoint=None):
        self.gateway, self.settings = gateway, settings
        self.checkpoint = checkpoint
        self.budget = state.setdefault("team_budget", {"calls": 0, "cost_cny": 0.0, "tokens": 0})
        self.lock = asyncio.Lock()

    async def reserve(self):
        async with self.lock:
            if self.budget["calls"] >= self.settings.agent_collaboration_max_calls or self.budget["cost_cny"] >= self.settings.agent_collaboration_max_cost_cny:
                raise BudgetExceeded("本次协作任务的模型预算已用完")
            self.budget["calls"] += 1
            if self.checkpoint is not None:
                await self.checkpoint()

    async def account(self, result):
        async with self.lock:
            self.budget["cost_cny"] += result.cost_cny
            self.budget["tokens"] += result.prompt_tokens + result.completion_tokens
            if self.checkpoint is not None:
                await self.checkpoint()

    async def chat(self, request):
        await self.reserve()
        result = await self.gateway.chat(request)
        await self.account(result)
        return result

    async def stream_chat(self, request):
        await self.reserve()
        async for result in self.gateway.stream_chat(request):
            if isinstance(result, ChatResult):
                await self.account(result)
            yield result


class AgentCollaboration:
    def __init__(self, deps, store, record, thread_id, emit):
        self.store, self.record, self.thread_id, self.emit = store, record, thread_id, emit
        gateway = TeamBudgetGateway(deps.gateway, record.state, deps.settings,
                                    checkpoint=lambda: store.save(thread_id, record))
        self.deps = replace(deps, gateway=gateway)
        self.tasks = record.state.setdefault("team_tasks", {})
        self.events = record.state.setdefault("agent_events", [])
        self.running = {}
        self.slots = asyncio.Semaphore(deps.settings.agent_collaboration_max_parallel)
        self.shared_slots = getattr(store, "child_slots", None)

    def event(self, task, event, summary, **extra):
        payload = {
            "type": "agent", "node": "agent_collaboration", "event": event,
            "agent_name": task["name"], "agent_role": task["role"],
            "task_id": task["id"], "parent_task_id": self.thread_id,
            "summary": summary[:600], "sequence": len(self.events) + 1, **extra,
        }
        self.events.append(payload)
        del self.events[:-200]
        self.emit({"kind": "trace", "payload": payload})

    def resolve(self, task_id):
        task = self.tasks.get(task_id)
        if task is None or task["status"] != "completed":
            raise GatewayError("AGENT_RESULT_UNAVAILABLE", "指定子任务尚未完成或不属于本次任务")
        return task

    def public(self, task, *, artifact=False):
        result = {k: task.get(k) for k in ("id", "name", "role", "status", "input_task_ids", "summary", "error_code", "duration_ms", "native_session_id")}
        if artifact and task.get("artifact") is not None:
            result["artifact"] = copy.deepcopy(task["artifact"])
        return result

    async def delegate(self, args):
        specs = args["tasks"]
        if not specs:
            return {"accepted": False, "errors": ["至少委派一个子任务"]}
        # Validate the complete batch before admitting any child.
        validated, names = [], set()
        confirmed = bool(self.record.state.get("plan_confirmed"))
        for spec in specs:
            role, name = spec["role"], spec["name"]
            if role not in ROLES or (confirmed and role != "reviewer") or name in names or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or not spec["instructions"].strip():
                return {"accepted": False, "errors": ["子任务角色、名称或确认后的操作不合法"]}
            names.add(name)
            inputs = spec.get("input_task_ids") or []
            try:
                sources = [self.resolve(value) for value in inputs]
            except GatewayError as exc:
                return {"accepted": False, "errors": [exc.message]}
            if role == "planner" and not any(s["role"] == "researcher" for s in sources):
                return {"accepted": False, "errors": ["规划子任务必须接收已完成研究报告；修订时也应带上研究任务"]}
            if role == "reviewer" and len([s for s in sources if s["role"] in {"planner", "user"}]) != 1:
                return {"accepted": False, "errors": ["审查子任务必须指定一个已完成草案"]}
            identity = uuid.uuid5(uuid.NAMESPACE_URL, self.thread_id + ":" + name).hex
            task_id = f"{role}:{identity}"
            old = self.tasks.get(task_id)
            if old and (old["instructions"] != spec["instructions"] or old["input_task_ids"] != inputs):
                return {"accepted": False, "errors": ["同名子任务不能更换输入，请使用新的名称"]}
            validated.append((spec, task_id, sources))
        new_count = sum(task_id not in self.tasks for _, task_id, _ in validated)
        if len([v for v in self.tasks.values() if v["role"] != "user"]) + new_count > self.deps.settings.agent_collaboration_max_agents:
            return {"accepted": False, "errors": ["已达到本次任务的子 Agent 数量上限"]}
        planning = sum(t["role"] == "planner" for t in self.tasks.values())
        planning += sum(spec["role"] == "planner" and tid not in self.tasks for spec, tid, _ in validated)
        if planning > 1 + self.deps.settings.agent_collaboration_max_revisions:
            return {"accepted": False, "errors": ["已达到计划返工上限，请报告未解决的问题"]}
        for spec, task_id, sources in validated:
            task = self.tasks.get(task_id)
            if task is not None and (task["status"] == "completed" or task_id in self.running):
                continue
            task = task or {"id": task_id, "name": spec["name"], "role": spec["role"], "instructions": spec["instructions"], "input_task_ids": spec.get("input_task_ids") or []}
            task.update(status="queued", error_code="", summary="等待执行")
            self.tasks[task_id] = task
            self.event(task, "queued", task["summary"])
            self.running[task_id] = asyncio.create_task(self.run_child(task, sources))
        await self.store.save(self.thread_id, self.record)
        return {"accepted": True, "tasks": [self.public(self.tasks[tid]) for _, tid, _ in validated], "instruction": "使用 collect_agent_results 获取交付物"}

    async def run_child(self, task, sources):
        from .task import AgentTask

        started = time.monotonic()
        try:
            async with self.slots:
                if self.shared_slots is not None:
                    await self.shared_slots.acquire()
                try:
                    task["status"] = "running"
                    self.event(task, "started", "开始独立研究或核验")
                    payload = {k: copy.deepcopy(self.record.state[k]) for k in INPUT_KEYS if k in self.record.state}
                    payload.update(
                        request_id=task["id"], assignment=task["instructions"],
                        team_inputs=[self.public(source, artifact=True) for source in sources],
                    )
                    thread = f"{task['role']}:{payload.get('user_id', 'offline')}:{task['id'].split(':', 1)[1]}"
                    task["thread_id"] = thread
                    previous = await self.store.load(thread)
                    result = await AgentTask(
                        task["role"], self.deps, self.store,
                        parent_thread_id=self.thread_id, agent_name=task["name"],
                    ).run(thread, payload, retry=previous.status in {"failed", "cancelled"},
                          emit=lambda event: self.child_event(task, event))
                    if result.status != "completed" or not isinstance(result.state.get("artifact"), dict):
                        raise GatewayError("AGENT_ARTIFACT_REQUIRED", "子任务没有提交经过校验的交付物")
                    task.update(status="completed", artifact=copy.deepcopy(result.state["artifact"]), native_session_id=result.native_session_id,
                                summary=str(result.state["artifact"].get("summary") or "已完成交付")[:600])
                    self.event(task, "completed", task["summary"])
                finally:
                    if self.shared_slots is not None:
                        self.shared_slots.release()
        except asyncio.CancelledError:
            task.update(status="cancelled", summary="子任务已取消")
            self.event(task, "cancelled", task["summary"])
            raise
        except Exception as exc:
            task.update(status="failed", error_code=getattr(exc, "code", "AGENT_EXECUTION_FAILED"), summary="子任务执行失败，可查看错误类别后重试")
            self.event(task, "failed", task["summary"], error_code=task["error_code"])
        finally:
            task["duration_ms"] = round((time.monotonic() - started) * 1000)
            await self.store.save(self.thread_id, self.record)

    def child_event(self, task, event):
        if event.get("kind") == "tool":
            self.event(task, "tool_" + event["status"], "调用教学工具", tool=event["tool"])

    async def collect(self, args):
        ids = args["task_ids"]
        if not ids:
            return {"accepted": False, "errors": ["至少指定一个子任务"]}
        if any(value not in self.tasks for value in ids):
            return {"accepted": False, "errors": ["只能收集本次任务委派的子任务"]}
        # Re-admit previously admitted workers after process loss, using their
        # original identities and deliverables. Completed work is never rerun.
        for value in ids:
            task = self.tasks[value]
            if task["status"] in {"queued", "running"} and value not in self.running:
                sources = [self.resolve(source) for source in task["input_task_ids"]]
                self.running[value] = asyncio.create_task(self.run_child(task, sources))
        pending = [self.running[value] for value in ids if value in self.running]
        if pending:
            await asyncio.gather(*pending)
        return {"accepted": True, "tasks": [self.public(self.tasks[value], artifact=True) for value in ids]}

    async def close(self):
        for task in self.running.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.running.values(), return_exceptions=True)

    def confirmed_input(self):
        from ..education.plan_checks import plan_digest

        if not self.record.state.get("plan_confirmed"):
            return
        plan = self.record.state["plan"]
        digest = plan_digest(plan)
        task_id = "user:" + digest[:32]
        self.tasks[task_id] = {
            "id": task_id, "name": "confirmed-plan", "role": "user", "status": "completed",
            "instructions": "用户确认的计划", "input_task_ids": [], "summary": "用户确认版本",
            "artifact": {"plan": copy.deepcopy(plan), "digest": digest,
                         "resources": copy.deepcopy(self.record.state.get("resources") or []),
                         "evidences": copy.deepcopy(self.record.state.get("evidences") or []),
                         "web_results": copy.deepcopy(self.record.state.get("web_results") or [])},
        }
        self.record.state["confirmed_plan_task_id"] = task_id
