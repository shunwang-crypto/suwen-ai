"""Educational collaborators with separate contexts and checked handoffs."""

from __future__ import annotations

import copy
import json

from pydantic import BaseModel, ConfigDict, Field

from ..gateway.base import GatewayError, Tier
from .plan import PlanTools
from .plan_checks import CHECKS, plan_digest, plan_issues
from .toolkit import STRINGS, TEXT, EducationTools, schema


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=600)
    task_keys: list[str] = Field(default_factory=list, max_length=12)
    suggestion: str = Field(min_length=1, max_length=600)


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approved: bool
    checks: dict[str, str]
    issues: list[ReviewIssue] = Field(max_length=24)
    summary: str = Field(min_length=1, max_length=1200)


def team_policy(tools, role):
    bundle = tools.prompt_bundle.team_policies
    if bundle is None:
        raise GatewayError("AGENT_PROMPT_REQUIRED", "协作任务需要包含角色策略的提示词版本")
    return "\n".join(getattr(bundle, role)) + "\n" + EducationTools.policy(tools)


def assignment(tools):
    return {"assignment": tools.state.get("assignment"), "inputs": tools.state.get("team_inputs") or []}


class ResearchTools(EducationTools):
    feature = "agent_researcher"
    tier = Tier.standard

    def __init__(self, deps, record, emit):
        super().__init__(deps, record, emit)
        self.add_learning_context()
        course = self.state.get("plan_mode") == "course_bound"
        self.add_evidence_tools(kb=course, web=not course)
        self.add(schema("submit_research_report", "交付研究摘要和真实资料来源；不生成最终计划。",
                        {"summary": TEXT, "source_ids": STRINGS}, ("summary", "source_ids")), self.submit)

    def policy(self):
        return team_policy(self, "researcher")

    def input(self, messages):
        return {**super().input(messages), **assignment(self)}

    async def submit(self, args):
        evidences, web = self.state.get("evidences") or [], self.state.get("web_results") or []
        known = {v["chunk_id"] for v in evidences} | {v["url"] for v in web}
        if not args["summary"].strip() or set(args["source_ids"]) - known:
            return {"accepted": False, "errors": ["摘要不能为空，source_ids 必须来自实际检索结果"]}
        self.state["artifact"] = {
            "summary": args["summary"], "source_ids": args["source_ids"],
            "evidences": evidences, "web_results": web,
        }
        self.record.status = "completed"
        return {"accepted": True, "status": "completed"}


class PlannerTools(PlanTools):
    feature = "agent_planner"
    phased_draft = True

    def __init__(self, deps, record, emit):
        super().__init__(deps, record, emit)
        # Each worker sees only explicit deliverables, never another agent's transcript.
        evidences, web = [], []
        for task in self.state.get("team_inputs") or []:
            artifact = task.get("artifact") or {}
            evidences.extend(copy.deepcopy(artifact.get("evidences") or []))
            web.extend(copy.deepcopy(artifact.get("web_results") or []))
        self.state["evidences"] = list({e["chunk_id"]: e for e in evidences}.values())
        self.state["web_results"] = list({e["url"]: e for e in web}.values())
        self.add(schema("read_assignment", "读取研究交付物、已有草案和具体审查问题。", {}), self.read_assignment)

    def policy(self):
        return self.teaching_policy("plan") + team_policy(self, "planner") + (
            "\n按 submit_plan_draft 的结构提交。resource_ref 来自 read_assignment 返回的 resources，下标从 0 开始，无合适资料填 null。"
            "工具返回 issues 时，按 task_keys 定位步骤并处理 suggestion，再提交完整草案。"
        )

    def input(self, messages):
        return {**super().input(messages), "assignment": self.state.get("assignment")}

    async def read_assignment(self, _args):
        self.state["assignment_read"] = True
        return {
            **assignment(self), "resources": self.resources(),
            "total_days": self.state.get("total_days"),
            "daily_minutes": float(self.state.get("budget_hours_per_day") or 1) * 60,
            "kc_order": self.state.get("kc_order") or [],
        }

    async def submit(self, args):
        if not self.state.get("assignment_read"):
            return {"accepted": False, "errors": ["先 read_assignment 阅读研究与审查交付物"]}
        result = await super().submit(args)
        if result["accepted"]:
            self.record.pending, self.record.status = None, "completed"
            self.state["artifact"] = {
                "plan": self.state["plan"], "digest": plan_digest(self.state["plan"]),
                "summary": "草案已完成程序检查，等待独立教学审查",
                "resources": self.state.get("resources") or [],
                "evidences": self.state.get("evidences") or [], "web_results": self.state.get("web_results") or [],
                "goal_analysis": self.state.get("goal_analysis") or {},
                "goal_outline": args.get("goal_outline") or {},
            }
            return {"accepted": True, "status": "completed"}
        return result


class PlanReviewerTools(EducationTools):
    feature = "agent_reviewer"
    tier = Tier.strong
    max_tokens = 4000

    def __init__(self, deps, record, emit):
        super().__init__(deps, record, emit)
        drafts = [v for v in self.state.get("team_inputs") or [] if v.get("role") in {"planner", "user"}]
        if len(drafts) != 1:
            raise GatewayError("AGENT_REVIEW_INPUT_REQUIRED", "独立审查需要一个确定的草案")
        self.draft = drafts[0]["artifact"]
        self.state["evidences"] = copy.deepcopy(self.draft.get("evidences") or [])
        self.state["web_results"] = copy.deepcopy(self.draft.get("web_results") or [])
        self.add_learning_context()
        course = self.state.get("plan_mode") == "course_bound"
        self.add_evidence_tools(kb=course, web=not course)
        self.add(schema("read_review_context", "读取待审草案、原始目标、预算和程序检查问题。", {}), self.read)
        self.add(schema("submit_plan_review", "交付六项审查结论与可执行修订建议。", {
            "approved": {"type": "boolean"},
            "checks": {"type": "object", "additionalProperties": False, "required": list(CHECKS),
                       "properties": {key: {"type": "string", "enum": ["pass", "fail", "not_applicable"]} for key in CHECKS}},
            "issues": {"type": "array", "maxItems": 24, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"code": {"type": "string", "maxLength": 80},
                               "message": {"type": "string", "maxLength": 600},
                               "task_keys": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                               "suggestion": {"type": "string", "maxLength": 600}},
                "required": ["code", "message", "task_keys", "suggestion"],
            }}, "summary": {"type": "string", "maxLength": 1200},
        }, ("approved", "checks", "issues", "summary")), self.submit)

    def policy(self):
        return team_policy(self, "reviewer")

    def input(self, messages):
        return {**super().input(messages), "assignment": self.state.get("assignment")}

    async def read(self, _args):
        self.state["review_context_read"] = True
        return {
            "input": super().input([]), "diagnosis": self.state.get("diagnosis") or {},
            "kc_order": self.state.get("kc_order") or [], "draft": self.draft,
            "program_issues": self.program_issues(), "required_checks": CHECKS,
        }

    def program_issues(self):
        return plan_issues(self.draft["plan"], self.state, resources=self.draft.get("resources") or [])

    async def submit(self, args):
        if not self.state.get("review_context_read"):
            return {"accepted": False, "errors": ["先 read_review_context 独立核对原始目标与草案"]}
        try:
            review = ReviewResult.model_validate(args)
        except ValueError:
            return {"accepted": False, "errors": ["审查结果需要 approved、checks、issues、summary；每条问题需 code/message/task_keys/suggestion"]}
        if set(review.checks) != set(CHECKS) or any(v not in {"pass", "fail", "not_applicable"} for v in review.checks.values()):
            return {"accepted": False, "errors": ["必须逐项返回六项检查，结果为 pass/fail/not_applicable"]}
        if any(review.checks[key] == "not_applicable" for key in ("prerequisites", "baseline", "time_budget", "outcomes")):
            return {"accepted": False, "errors": ["先修、基线、时间与目标覆盖必须实际检查，不能标记为不适用"]}
        if self.draft.get("resources") and review.checks["resources"] == "not_applicable":
            return {"accepted": False, "errors": ["草案引用了资料，必须核验资源依据"]}
        program = self.program_issues()
        if review.approved and (review.issues or program or "fail" in review.checks.values()):
            return {"accepted": False, "errors": ["存在程序问题、审查问题或 fail 时不能批准"], "program_issues": program}
        if not review.approved and not review.issues:
            return {"accepted": False, "errors": ["拒绝草案时必须给出具体可修订的问题"]}
        if not review.approved and "fail" not in review.checks.values():
            return {"accepted": False, "errors": ["拒绝草案时应指出至少一项 fail 检查"]}
        self.state["artifact"] = {**review.model_dump(), "draft_digest": plan_digest(self.draft["plan"])}
        self.record.status = "completed"
        return {"accepted": True, "status": "completed"}


class PlanCoordinatorTools(PlanTools):
    feature = "agent_plan_coordinator"
    phased_draft = True

    def __init__(self, deps, record, emit, collaboration):
        EducationTools.__init__(self, deps, record, emit)
        self.collaboration = collaboration
        self.add_learning_context()
        self.add(schema("delegate_agents", "先委派并收集 researcher，再用研究任务的 name 委派 planner，收集草案后用草案的 name 委派 reviewer。同一批仅可并行无依赖子任务。", {
            "tasks": {"type": "array", "minItems": 1, "maxItems": 3, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"role": {"type": "string", "enum": ["researcher", "planner", "reviewer"]},
                               "name": {"type": "string", "maxLength": 64}, "instructions": TEXT,
                               "input_task_ids": {**STRINGS, "description": "已收集任务的简短 name（也支持 id）。规划引用研究，审查引用一个草案。有多份上游交付物或正在执行时，应明确指定。返工时指定使用的草案和审查。"}}, "required": ["role", "name", "instructions"]}},
        }, ("tasks",)), collaboration.delegate)
        self.add(schema("collect_agent_results", "等待指定子任务结束并读取交付物。", {
            "task_ids": {**STRINGS, "description": "子任务的简短 name 列表，也支持 id。"},
        }, ("task_ids",)), collaboration.collect)
        self.add(schema("report_team_failure", "有限返工仍不能得到合格成果时如实结束任务。", {"message": TEXT}, ("message",)), self.failure)
        if self.state.get("plan_confirmed"):
            collaboration.confirmed_input()
            self.add(schema("publish_plan", "发布用户确认且对应审查通过的版本。", {
                "review_task_id": {"type": "string", "description": "如有多份符合条件的审查，必填选定 reviewer 的 name 或 id。"},
            }), self.publish)
        else:
            self.add(schema("submit_reviewed_plan", "引用已完成规划和审查任务，提交同一版本合格草案等待用户确认。", {
                "draft_task_id": {"type": "string", "description": "选定 planner 的简短 name，也支持 id。"},
                "review_task_id": {"type": "string", "description": "审核该草案并批准的 reviewer 的简短 name，也支持 id。"},
            }, ("draft_task_id", "review_task_id")), self.submit_reviewed)

    def policy(self):
        roster = [self.collaboration.public(task) for task in self.collaboration.tasks.values()]
        return team_policy(self, "coordinator") + json.dumps({
            "tasks": roster, "confirmed": bool(self.state.get("plan_confirmed")),
            "confirmed_plan_task_id": self.state.get("confirmed_plan_task_id"),
            "instruction": "所有任务引用参数（包括名称含 id 的参数）优先用返回的短 name。确认后不能修改计划；若用户编辑使版本变化，委派 reviewer 审查 confirmed-plan，收集结果后再 publish_plan。",
        }, ensure_ascii=False)

    async def submit_reviewed(self, args):
        try:
            draft = self.collaboration.resolve(args["draft_task_id"])
            review = self.collaboration.resolve(args["review_task_id"])
        except GatewayError as exc:
            return self.collaboration.handoff_error(exc.message)
        artifact, verdict = draft.get("artifact") or {}, review.get("artifact") or {}
        if draft["role"] != "planner" or review["role"] != "reviewer" or not verdict.get("approved") or verdict.get("draft_digest") != artifact.get("digest"):
            return self.collaboration.handoff_error("必须引用同一版本已通过独立审查的规划草案")
        self.state.update({k: copy.deepcopy(artifact.get(k) or []) for k in ("evidences", "web_results", "resources")})
        result = await PlanTools.submit(self, {"plan": artifact["plan"], "goal_outline": artifact.get("goal_outline") or {}})
        if result["accepted"]:
            if plan_digest(self.state["plan"]) != verdict["draft_digest"]:
                self.record.pending, self.record.status = None, "running"
                return {"accepted": False, "errors": ["程序修正改变了已审查内容，需要重新规划和审查"]}
            self.state["approved_plan_digest"] = verdict["draft_digest"]
            self.state["plan_review"] = copy.deepcopy(verdict)
            self.record.pending["review"] = copy.deepcopy(verdict)
            self.record.pending["agents"] = [self.collaboration.public(t) for t in self.collaboration.tasks.values()]
        return result

    async def publish(self, args):
        digest = plan_digest(self.state["plan"])
        if digest != self.state.get("approved_plan_digest"):
            reviews = [task for task in self.collaboration.tasks.values()
                       if task["role"] == "reviewer" and task["status"] == "completed"
                       and (task.get("artifact") or {}).get("approved")
                       and (task.get("artifact") or {}).get("draft_digest") == digest]
            if not reviews:
                return {"accepted": False, "errors": ["用户编辑后的确认版本尚未通过独立审查，请先委派 reviewer"]}
            reference = args.get("review_task_id")
            if reference:
                try:
                    selected = self.collaboration.resolve(reference)
                except GatewayError as exc:
                    return self.collaboration.handoff_error(exc.message)
                if selected not in reviews:
                    return self.collaboration.handoff_error("选定的审查未批准当前确认版本")
            elif len(reviews) == 1:
                selected = reviews[0]
            else:
                return {"accepted": False, "errors": ["请通过 review_task_id 指定一份已通过的审查"],
                        "reviews": [self.collaboration.public(task) for task in reviews]}
            self.state["approved_plan_digest"], self.state["plan_review"] = digest, selected["artifact"]
        return await PlanTools.publish(self, args)

    async def failure(self, args):
        if self.state.get("plan_confirmed"):
            self.state["plan_confirmed"] = False
            self.record.status = "awaiting_confirmation"
            self.record.pending = {"kind": "plan_confirm", "plan": self.state["plan"],
                                   "resources": self.state.get("resources") or [],
                                   "plan_mode": self.state.get("plan_mode"),
                                   "review": {"approved": False, "summary": args["message"][:1200]}}
            return {"accepted": True, "status": "awaiting_confirmation"}
        self.record.status = "failed"
        self.state.update(failure_code="PLAN_COLLABORATION_FAILED", failure_message=args["message"][:1200])
        return {"accepted": True, "status": "failed"}
