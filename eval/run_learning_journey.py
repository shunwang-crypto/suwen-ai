"""Run a pre-registered course through the live student APIs and save an audit receipt."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import mimetypes
import os
import re
import time
import unicodedata
import uuid
from datetime import UTC, datetime
from difflib import SequenceMatcher
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from eduagents_core.kc.types import normalize_kc_name
from pydantic import ValidationError

from .learning_journey_http import (
    CheckFailed,
    JourneyApi,
    JourneyCase,
    require,
    save_report,
    terminal,
)

_STEPS = (
    ("authenticate", ()),
    ("course_ingestion", ("authenticate",)),
    ("resolve_topic", ("course_ingestion",)),
    ("qa_citations", ("course_ingestion",)),
    ("tutor_hints", ("authenticate",)),
    ("plan_draft", ("course_ingestion",)),
    ("plan_confirm", ("plan_draft",)),
    ("complete_plan_task", ("plan_confirm",)),
    ("prepare_questions", ("resolve_topic",)),
    ("independent_attempts", ("prepare_questions",)),
    ("error_book", ("independent_attempts",)),
    ("review", ("error_book",)),
    ("targeted_quiz", ("plan_confirm", "independent_attempts")),
    ("reinforcement_proposal", ("targeted_quiz",)),
    ("reinforcement_confirm", ("reinforcement_proposal",)),
    ("repeat_confirm", ("reinforcement_confirm",)),
    ("evidence_audit", ("review", "reinforcement_proposal")),
    ("model_usage", ("authenticate",)),
)


def normalize_fact_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"\\(?:[dt]?frac)\s*\{(\d+)\}\s*\{(\d+)\}", r"\1/\2", text)
    text = text.translate(str.maketrans("", "", "$"))
    text = re.sub(r"(?<=\d)\s+(?=[^\W\d_])", "", text)

    def exact_number(match: re.Match) -> str:
        numeral = match.group()
        try:
            return str(Fraction(numeral))
        except (ValueError, ZeroDivisionError):
            return numeral

    return re.sub(r"\d{1,20}/\d{1,20}|\d{1,20}\.\d{1,20}", exact_number, text)


def fact_matches(term: str, normalized_answer: str) -> bool:
    needle = normalize_fact_text(term)
    if not needle:
        return False
    left = r"(?<![\d./])" if needle[0].isdigit() else ""
    right = r"(?![\d./])" if needle[-1].isdigit() else ""
    return re.search(left + re.escape(needle) + right, normalized_answer) is not None


def match_topic(requested: str, aliases: list[str], nodes: list[dict]) -> tuple[dict, str]:
    names = {normalize_kc_name(name) for name in [requested, *aliases]} - {""}
    candidates = [(node, normalize_kc_name(node["kc_name"])) for node in nodes]
    rules = {
        "exact": lambda n: n in names,
        "unique_containment": lambda n: any(
            min(len(n), len(name)) >= 3 and (n in name or name in n) for name in names
        ),
        "unique_similarity": lambda n: any(
            min(len(n), len(name)) >= 3 and SequenceMatcher(None, n, name).ratio() >= 0.75
            for name in names
        ),
    }
    for rule, matches in rules.items():
        hits = [node for node, name in candidates if matches(name)]
        if len(hits) == 1:
            return hits[0], rule
        if hits:
            labels = ", ".join(node["kc_name"] for node in hits[:12])
            raise CheckFailed(f"ambiguous KC for {requested}: {labels}")
    labels = ", ".join(node["kc_name"] for node, _ in candidates[:20])
    raise CheckFailed(f"no unique KC for {requested}; observed candidates: {labels}")


def plan_tasks(structure: dict) -> list[dict]:
    return [task for stage in structure["stages"] for day in stage["days"] for task in day["tasks"]]


class Journey:
    def __init__(
        self, api: JourneyApi, case: JourneyCase, args: argparse.Namespace, case_hash: str
    ):
        self.api = api
        self.case = case
        self.args = args
        self.started_at = datetime.now(UTC)
        self.started_clock = time.monotonic()
        self.run_id = uuid.uuid4().hex
        self.results: dict[str, dict] = {}
        self.details: dict = {}
        self.agent_request_ids: dict[str, list[str]] = {}
        self.report = {
            "schema_version": 2,
            "run_id": self.run_id,
            "case_id": case.id,
            "case_sha256": case_hash,
            "started_at": self.started_at.isoformat(),
            "status": "running",
            "steps": [],
            "completion_rate": 0.0,
            "model_usage": None,
            "error_updates": {"created": None, "reviewed": None},
            "plan_progress": None,
        }

    def save(self):
        passed = sum(row["status"] == "pass" for row in self.results.values())
        self.report["completion_rate"] = round(passed / len(_STEPS), 4)
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started_clock, 3)
        save_report(self.args.output, self.report)

    async def run(self) -> dict:
        self.save()
        for name, dependencies in _STEPS:
            blocked = [dep for dep in dependencies if self.results[dep]["status"] != "pass"]
            request_start = len(self.api.request_ids)
            began = time.monotonic()
            self.details = {}
            row = {"id": name, "status": "fail", "evidence": self.details}
            if blocked:
                row["blocked_by"] = blocked
            else:
                try:
                    await getattr(self, name)()
                except CheckFailed as exc:
                    row["error"] = str(exc)
                except Exception as exc:
                    # Keep authentication and database exceptions out of the receipt.
                    row["error"] = type(exc).__name__
                else:
                    row["status"] = "pass"
            row["elapsed_seconds"] = round(time.monotonic() - began, 3)
            row["request_ids"] = self.api.request_ids[request_start:]
            self.results[name] = row
            self.report["steps"].append(row)
            self.save()
        self.report["status"] = (
            "passed" if all(row["status"] == "pass" for row in self.results.values()) else "failed"
        )
        self.report["finished_at"] = datetime.now(UTC).isoformat()
        self.save()
        return self.report

    async def authenticate(self):
        from scripts.seed_demo import DEMO_EMAIL

        token = os.getenv("EDUAGENTS_JOURNEY_TOKEN")
        email = os.getenv("EDUAGENTS_JOURNEY_EMAIL")
        password = os.getenv("EDUAGENTS_JOURNEY_PASSWORD")
        if self.args.register or not token:
            require(
                email and password,
                "set the dedicated learner email and password in the environment",
            )
            require(
                email.strip().casefold() != DEMO_EMAIL.casefold(), "use a dedicated learner account"
            )
            credentials = {"email": email, "password": password}
            if self.args.register:
                user = {**credentials, "display_name": f"Learning journey {self.run_id[:8]}"}
                for field in ("invite_code", "email_code", "captcha_id", "captcha_answer"):
                    if value := os.getenv(f"EDUAGENTS_JOURNEY_{field.upper()}"):
                        user[field] = value
                await self.api.request("POST", "/api/auth/register", json=user)
            login = await self.api.request("POST", "/api/auth/login", json=credentials)
            token = login["access_token"]
        self.api.client.headers["Authorization"] = "Bearer " + token
        me = await self.api.request("GET", "/api/auth/me")
        require(
            me["role"] == "student" and me["email"].casefold() != DEMO_EMAIL.casefold(),
            "use a dedicated learner account",
        )
        plans = await self.api.request("GET", "/api/plans")
        require(
            not any(plan["status"] == "active" for plan in plans),
            "dedicated learner already has an active plan",
        )
        self.learner_id = str(uuid.UUID(me["id"]))
        self.report["learner_id"] = self.learner_id
        self.details["learner_id"] = self.learner_id

    async def course_ingestion(self):
        case = self.case.course
        existing = self.args.course_id
        sources = []
        if not existing:
            require(case.documents, "provide at least one textbook document or --course-id")
            for spec in case.documents:
                path = self.args.case.parent / spec.path
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                require(
                    not spec.sha256 or spec.sha256 == digest,
                    f"textbook SHA-256 mismatch: {path.name}",
                )
                sources.append((path.name, data, digest, spec.enrichment))
            require(
                len({name for name, *_ in sources}) == len(sources),
                "duplicates in textbook filenames",
            )
            require(
                any(name == self.case.qa.document for name, *_ in sources),
                "QA textbook missing from the upload list",
            )
            course = await self.api.request(
                "POST", "/api/kb", json={"name": case.name, "subject": case.subject}
            )
        else:
            courses = await self.api.request("GET", "/api/kb")
            course = next((item for item in courses if item["id"] == existing), None)
            require(
                course and course["owner_type"] == "user" and course["writable"],
                "select a course owned by the evaluation learner",
            )
        self.course_id = course["id"]
        self.kb_path = f"/api/kb/{self.course_id}"
        self.report["course_id"] = self.course_id
        documents_url = f"{self.kb_path}/documents"
        uploaded = {}
        receipts = []
        self.details.update(
            source="existing_course" if existing else "textbook_upload", documents=receipts
        )
        for name, data, digest, enrichment in sources:
            policy = "semantic" if enrichment == "auto" else enrichment
            doc = await self.api.request(
                "POST",
                documents_url,
                params={"enrichment": policy},
                files={
                    "file": (
                        name,
                        data,
                        mimetypes.guess_type(name)[0] or "application/octet-stream",
                    )
                },
            )
            uploaded[doc["id"]] = policy
            receipts.append({"id": doc["id"], "filename": name, "sha256": digest, "policy": policy})
        deadline = time.monotonic() + self.args.ingestion_timeout
        re_enriched = set()
        while True:
            documents = await self.api.request("GET", documents_url)
            require(documents, "the course has no source documents")
            self.details["document_statuses"] = {doc["id"]: doc["status"] for doc in documents}
            for doc in documents:
                status = doc["status"]
                require(status != "failed", f"textbook ingestion failed: {doc['filename']}")
                if status in {"ready", "partial_ready"} and doc["id"] not in re_enriched:
                    policy = doc.get("recognition_meta_json", {}).get("enrichment_policy")
                    error = doc.get("error_msg") or ""
                    require(
                        "知识增强暂时失败" not in error,
                        f"semantic enrichment failed: {doc['filename']}",
                    )
                    enqueue_failed = "任务提交失败" in error
                    if uploaded.get(doc["id"], policy) == "headings" or enqueue_failed:
                        await self.api.request("POST", f"{documents_url}/{doc['id']}/re-enrich")
                        re_enriched.add(doc["id"])
            if all(doc["status"] == "enriched" for doc in documents):
                break
            require(
                time.monotonic() < deadline,
                "semantic enrichment did not complete before the timeout",
            )
            await asyncio.sleep(self.args.poll_interval)
        if uploaded:
            require(
                {doc["id"] for doc in documents} == set(uploaded),
                "the uploaded document set changed",
            )
        else:
            receipts.extend({"id": doc["id"], "filename": doc["filename"]} for doc in documents)
        expected = [doc["id"] for doc in documents if doc["filename"] == self.case.qa.document]
        require(
            len(expected) == 1,
            "the pre-registered QA source filename must identify one imported book",
        )
        self.qa_document_id = expected[0]
        self.document_ids = {doc["id"] for doc in documents}
        self.child_chunk_ids = set()
        self.bound_kc_ids = set()
        for doc in documents:
            offset = 0
            while True:
                page = await self.api.request(
                    "GET",
                    f"{documents_url}/{doc['id']}/chunks",
                    params={"offset": offset, "limit": 200},
                )
                for chunk in page:
                    if not chunk["parent_id"]:
                        continue
                    require(
                        chunk["embedding_status"] == "done" and chunk["kc_ids"],
                        "textbook has an unindexed or unbound passage",
                    )
                    self.child_chunk_ids.add(chunk["id"])
                    self.bound_kc_ids.update(chunk["kc_ids"])
                if len(page) < 200:
                    break
                offset += len(page)
        require(self.child_chunk_ids, "textbook has no searchable passages")
        self.details.update(
            indexed_children=len(self.child_chunk_ids), bound_kcs=len(self.bound_kc_ids)
        )

    async def _topic_snapshot(self):
        graph = await self.api.request("GET", f"{self.kb_path}/kc-graph")
        require(graph["kb_id"] == self.course_id, "course KC graph scope mismatch")
        return graph["nodes"]

    async def resolve_topic(self):
        case = self.case.practice
        nodes = [
            node for node in await self._topic_snapshot() if node["kc_id"] in self.bound_kc_ids
        ]
        self.kc, rule = match_topic(case.kc, case.kc_aliases, nodes)
        self.details.update(requested=case.kc, match_rule=rule, observed_topic=self.kc)
        require(
            self.kc["evidence_state"] == "unobserved", "selected KC already has learning evidence"
        )

    async def _chunk_context(self, chunk_id: str) -> dict:
        context = await self.api.request(
            "GET", f"{self.kb_path}/chunks/{chunk_id}/context", params={"window": 0}
        )
        require(
            context["chunk"]["id"] == chunk_id and context["doc_id"] in self.document_ids,
            "passage does not resolve to the selected course",
        )
        require(context["chunk"]["text"].strip(), "source passage is empty")
        return context

    async def qa_citations(self):
        case = self.case.qa
        events = await self.api.stream(
            "/api/chat",
            {"mode": "fast", "kb_ids": [self.course_id], "message": case.question},
        )
        done = terminal(events, "done")
        self.details.update(
            question=case.question, session_id=done.get("session_id"), route=done.get("route")
        )
        answer, attempt = "", None
        for delta in events.get("token", []):
            if attempt != delta.get("attempt"):
                answer = ""
                attempt = delta.get("attempt")
            answer += delta.get("text", "")
        self.details["answer"] = answer
        require(answer.strip() and not done.get("refused"), "QA did not return an answer")
        selected = done.get("selected_citations")
        if selected is None and (done.get("usage") or {}).get("mode") == "cache":
            selected = events.get("citation", [])
        require(selected, "QA selected no citations")
        citations = []
        self.details["citations"] = citations
        for cite in selected:
            chunk_id = cite["chunk_id"]
            require(
                chunk_id in self.child_chunk_ids,
                "QA selected a passage outside the indexed source set",
            )
            context = await self._chunk_context(chunk_id)
            require(
                not cite.get("doc_id") or cite["doc_id"] == context["doc_id"],
                "citation document ID changed",
            )
            citations.append(
                {
                    "chunk_id": chunk_id,
                    "doc_id": context["doc_id"],
                    "filename": context["doc_name"],
                    "heading": context["chunk"]["heading_path"],
                }
            )
        require(
            any(
                cite["doc_id"] == self.qa_document_id and case.heading_contains in cite["heading"]
                for cite in citations
            ),
            "QA citations miss the pre-registered textbook section",
        )
        normalized = normalize_fact_text(answer)
        matched = [
            next((term for term in group if fact_matches(term, normalized)), None)
            for group in case.terms
        ]
        self.details["matched_terms"] = matched
        require(all(matched), "QA answer misses a pre-registered fact")
        require(
            not any(fact_matches(term, normalized) for term in case.contradictions),
            "QA answer contains a pre-registered contradiction",
        )

    async def tutor_hints(self):
        turns = []
        local_id = None
        self.details["turns"] = turns
        for message in self.case.tutor.turns:
            payload = {"problem": self.case.tutor.problem, "message": message}
            if local_id:
                payload["thread_local_id"] = local_id
            result = await self.api.request("POST", "/api/tutor", json=payload)
            require(
                not local_id or result["thread_local_id"] == local_id,
                "Tutor did not continue the same session",
            )
            local_id = result["thread_local_id"]
            self.details["thread_local_id"] = local_id
            require(result["reply"].strip(), "Tutor returned an empty reply")
            turns.append(
                {"message": message, "reply": result["reply"], "hint_level": result["hint_level"]}
            )
        levels = [turn["hint_level"] for turn in turns]
        require(
            all(isinstance(level, int) and 0 <= level <= 3 for level in levels),
            "Tutor hint level is outside the supported range",
        )
        require(
            levels == sorted(levels) and levels[0] < levels[-1],
            "Tutor hints did not progress through levels",
        )
        # The last hint must still address the student's requested self-check.
        final_hint = turns[-1]["reply"].casefold()
        concepts = self.case.tutor.final_hint_concepts
        hits = [
            next((word for word in group if word.casefold() in final_hint), None)
            for group in concepts
        ]
        self.details["matched_final_hint_concepts"] = hits
        require(all(hits), "the final hint missed a pre-registered concept in the student's task")
        for turn in turns:
            compact = "".join(turn["reply"].casefold().split())
            if turn["hint_level"] < 3:
                require(
                    not any(
                        "".join(term.casefold().split()) in compact
                        for term in self.case.tutor.premature_answers
                    ),
                    "a hint disclosed the pre-registered final answer early",
                )
        stored = await self.api.request("GET", f"/api/tutor/{local_id}")
        require(
            [m["hint_level"] for m in stored["messages"] if m["role"] == "assistant"] == levels,
            "Tutor checkpoint did not preserve the observed hint levels",
        )

    async def _record_agents(self, thread_id: str, stage: str):
        status = await self.api.request("GET", f"/api/threads/{thread_id}/agents")
        self.details["agents"] = [
            {key: agent.get(key) for key in ("id", "role", "name", "status", "error_code")}
            for agent in status["agents"]
        ]
        self.agent_request_ids.setdefault(stage, []).extend(
            agent["id"] for agent in status["agents"] if agent.get("id")
        )

    async def plan_draft(self):
        payload = {
            **self.case.plan.model_dump(exclude_none=True),
            "plan_mode": "course_bound",
            "kb_id": self.course_id,
        }
        events = await self.api.stream("/api/plans", payload)
        starts = events.get("task_started", [])
        if starts and starts[0].get("thread_id"):
            self.plan_thread = starts[0]["thread_id"]
            self.details["thread_id"] = self.plan_thread
            try:
                await self._record_agents(self.plan_thread, "plan_draft")
            except CheckFailed as exc:
                self.details["agent_status_error"] = str(exc)
        interrupt = terminal(events, "interrupt")
        require(
            interrupt["kind"] == "plan_confirm" and interrupt["thread_id"] == self.plan_thread,
            "plan did not stop for explicit confirmation",
        )
        tasks = plan_tasks(interrupt["plan"])
        require(tasks, "plan preview has no tasks")
        plans = await self.api.request("GET", "/api/plans")
        require(
            not any(plan["status"] == "active" for plan in plans),
            "plan became active before confirmation",
        )
        self.details["draft_task_count"] = len(tasks)

    async def plan_confirm(self):
        events = await self.api.stream(
            f"/api/threads/{self.plan_thread}/resume", {"value": {"approved": True}}
        )
        result = terminal(events, "done")["result"]
        self.plan_id = result["plan_id"]
        self.plans_path = f"/api/plans/{self.plan_id}"
        self.report["plan_id"] = self.plan_id
        plan = await self.api.request("GET", self.plans_path)
        self.report["plan_progress"] = plan["progress"]
        require(
            plan["id"] == self.plan_id
            and plan["kb_id"] == self.course_id
            and plan["status"] == "active",
            "confirmed plan did not persist in the selected course",
        )
        require(plan["plan_mode"] == "course_bound", "confirmed plan lost its course-bound mode")
        tasks = plan_tasks(plan["structure"])
        task_ids = [task["task_id"] for task in tasks]
        require(
            task_ids and len(task_ids) == len(set(task_ids)),
            "confirmed plan task identities are missing or duplicated",
        )
        days = [day for stage in plan["structure"]["stages"] for day in stage["days"]]
        budget_minutes = round(self.case.plan.budget_hours_per_day * 60)
        require(
            all(sum(task["minutes"] for task in day["tasks"]) <= budget_minutes for day in days),
            "confirmed plan exceeds the pre-registered daily budget",
        )
        self.details.update(
            plan_id=self.plan_id,
            task_ids=task_ids,
            version=plan["version"],
            grounding_status=plan["grounding_status"],
            progress=plan["progress"],
        )
        self.plan = plan

    async def complete_plan_task(self):
        task = next(
            (
                task
                for task in plan_tasks(self.plan["structure"])
                if task["status"] == "pending"
                and not task.get("locked")
                and task.get("action_type") in {"read", "mixed"}
                and (task.get("resource") or {}).get("kb_id") == self.course_id
                and task["resource"].get("chunk_id")
            ),
            None,
        )
        require(task, "confirmed plan has no pending reading task with a course source")
        source = task["resource"]
        context = await self._chunk_context(source["chunk_id"])
        require(context["doc_id"] == source["doc_id"], "plan task source document mismatch")
        result = await self.api.request(
            "POST", f"{self.plans_path}/tasks/{task['task_id']}/complete"
        )
        detail = await self.api.request("GET", self.plans_path)
        self.report["plan_progress"] = detail["progress"]
        require(
            result["completed"] and detail["progress"]["done"] == self.plan["progress"]["done"] + 1,
            "explicit reading completion did not advance plan progress once",
        )
        self.details.update(
            task_id=task["task_id"],
            chunk_id=source["chunk_id"],
            doc_id=source["doc_id"],
            progress=detail["progress"],
            version=detail["version"],
        )

    async def prepare_questions(self):
        kc_id, kc_name = self.kc["kc_id"], self.kc["kc_name"]
        listing = await self._errors()
        self.baseline_error_total = listing["total"]
        resource = await self.api.request(
            "POST",
            "/api/resources",
            json={
                "title": f"{self.case.course.name} · 已登记客观题",
                "subject": self.case.course.subject,
                "linked_kb_id": self.course_id,
                "resource_type": "worksheet",
            },
        )
        self.resource_id = resource["id"]
        self.details["resource_id"] = self.resource_id
        path = f"/api/resources/{self.resource_id}"
        question_ids = []
        self.details["resource_question_ids"] = question_ids
        for question in self.case.practice.questions:
            body = {**question.model_dump(exclude={"response"}), "kc_names": [kc_name]}
            saved = await self.api.request("POST", f"{path}/questions/manual", json=body)
            require(
                saved["kc_ids"] == [kc_id], "resource question did not bind the observed course KC"
            )
            question_id = saved["resource_question_id"]
            verified = await self.api.request(
                "POST", f"/api/resource-questions/{question_id}/verify"
            )
            require(
                verified["verification_status"] == "verified", "resource question was not verified"
            )
            question_ids.append(question_id)
        confirmation = await self.api.request(
            "POST",
            f"{path}/confirm",
            json={
                "resource_question_ids": question_ids,
                "add_to_error_book": False,
            },
        )
        require(
            confirmation["confirmed_question_count"] == len(question_ids)
            and not confirmation["error_item_ids"],
            "question verification produced unexpected error evidence",
        )
        questions = await self.api.request("GET", f"{path}/questions")
        require(
            [q["resource_question_id"] for q in questions] == question_ids,
            "verified resource question order changed",
        )
        for actual, expected in zip(questions, self.case.practice.questions, strict=True):
            require(
                actual["verification_status"] == "verified" and actual["answer"] == expected.answer,
                "the resource does not expose the verified objective reference answer",
            )
        self.questions = questions

    async def independent_attempts(self):
        attempts = []
        self.details["submissions"] = attempts
        for actual, preregistered in zip(self.questions, self.case.practice.questions, strict=True):
            draft = await self.api.request(
                "POST",
                "/api/assessment-attempts",
                json={
                    "resource_id": self.resource_id,
                    "title": f"独立作答 {len(attempts) + 1}",
                },
            )
            path = f"/api/assessment-attempts/{draft['id']}"
            answer = {
                "resource_question_id": actual["resource_question_id"],
                "response": preregistered.response,
            }
            await self.api.request("PUT", f"{path}/answers", json={"answers": [answer]})
            await self.api.request("POST", f"{path}/submit")
            grade = await self.api.request(
                "POST", f"{path}/grade-or-verify", json={"verify": True, "answers": [answer]}
            )
            record = await self.api.request("GET", path)
            require(
                grade["status"] == "graded" and len(record["answers"]) == 1,
                "objective attempt did not finish grading",
            )
            saved = record["answers"][0]
            expected_correct = answer["response"] == actual["answer"]
            require(
                saved["grading_status"] == "verified"
                and saved["response"] == answer["response"]
                and saved["is_correct"] is expected_correct,
                "graded response or correctness differs from the verified reference",
            )
            require(
                any(change["kc_id"] == self.kc["kc_id"] for change in grade["mastery_changes"]),
                "objective verification did not write mastery evidence for the target KC",
            )
            node = next(
                node for node in await self._topic_snapshot() if node["kc_id"] == self.kc["kc_id"]
            )
            attempts.append(
                {
                    "assessment_id": draft["id"],
                    "answer_id": saved["id"],
                    "resource_question_id": actual["resource_question_id"],
                    "correct": saved["is_correct"],
                    "evidence_state": node["evidence_state"],
                    "band": node["band"],
                }
            )
        require(
            len({a["assessment_id"] for a in attempts}) == len(attempts)
            and len({a["answer_id"] for a in attempts}) == len(attempts),
            "assessment evidence identities were reused",
        )
        require(
            [a["evidence_state"] for a in attempts[:3]] == ["estimated", "estimated", "observed"],
            "three independent graded responses did not advance the KC to observed",
        )
        self.assessments = attempts

    async def _errors(self):
        return await self.api.request(
            "GET", "/api/errors", params={"kb_id": self.course_id, "page_size": 100}
        )

    async def error_book(self):
        wrong = {a["resource_question_id"] for a in self.assessments if not a["correct"]}
        listing = await self._errors()
        items = [item for item in listing["items"] if item["resource_id"] == self.resource_id]
        self.details.update(
            error_ids=[item["id"] for item in items], wrong_resource_questions=sorted(wrong)
        )
        self.report["error_updates"]["created"] = listing["total"] - self.baseline_error_total
        require(
            len(items) == len(wrong) and {item["resource_question_id"] for item in items} == wrong,
            "verified wrong responses did not enter the error book exactly once",
        )
        require(
            all(
                item["verification_status"] == "verified"
                and item["state"] == "new"
                and self.kc["kc_id"] in item["kc_ids"]
                and item["next_due"]
                for item in items
            ),
            "new errors lack verified course evidence or a review due date",
        )
        queue = await self.api.request("GET", "/api/reviews/today")
        due_ids = {item["error_item_id"] for item in queue["items"]}
        self.review_item = next((item for item in items if item["id"] in due_ids), None)
        require(self.review_item, "a real wrong response did not enter today's review queue")

    async def review(self):
        error = self.review_item
        error_id = error["id"]
        answer = error["question"]["answer"]
        require(answer, "the verified error has no review reference answer")
        key = f"journey-{self.run_id}-review"
        result = await self.api.request(
            "POST",
            f"/api/reviews/{error_id}/answer",
            headers={"Idempotency-Key": key},
            json={"response": answer},
        )
        listing = await self._errors()
        updated = next(item for item in listing["items"] if item["id"] == error_id)
        self.details.update(
            error_id=error_id,
            submission_id=result["submission_id"],
            correct=result["correct"],
            previous_due=error["next_due"],
            next_due=updated["next_due"],
            state=updated["state"],
        )
        require(
            result["correct"] is True and result["grading_status"] == "scored",
            "objective review was not scored correctly",
        )
        require(
            updated["next_due"] == result["next_due"] and updated["next_due"] != error["next_due"],
            "review did not persist a new due date",
        )
        require(
            any(change["kc_id"] == self.kc["kc_id"] for change in result["mastery_changes"]),
            "review did not update mastery evidence",
        )
        self.report["error_updates"]["reviewed"] = 1

    async def targeted_quiz(self):
        kc_name = self.kc["kc_name"]
        events = await self.api.stream(
            "/api/quiz",
            {
                "kb_id": self.course_id,
                "topic": f"{kc_name}课后随测，请出一道单选题",
                "kc_names": [kc_name],
                "num_questions": 1,
                "difficulty_mix": {"easy": 1},
                "confirmation_mode": "required",
            },
        )
        interrupt = terminal(events, "interrupt")
        thread_id = interrupt["thread_id"]
        self.details["thread_id"] = thread_id
        require(
            interrupt["kind"] == "blueprint_confirm",
            "quiz did not provide a blueprint for confirmation",
        )
        blue = interrupt["blueprint"]
        require(len(blue["items"]) == 1, "requested a single-question quiz")
        # Use the existing blueprint editor to select one objective question on the resolved KC.
        edited = {**blue, "items": [{**blue["items"][0], "kc_name": kc_name, "type": "single"}]}
        resumed = await self.api.stream(
            f"/api/threads/{thread_id}/resume", {"value": {"approved": True, "blueprint": edited}}
        )
        done = terminal(resumed, "done")
        self.details["outcome"] = done.get("outcome")
        if done.get("outcome") != "succeeded":
            self.details["error_code"] = (done.get("error") or {}).get("code")
            raise CheckFailed("the targeted quiz did not persist successfully")
        self.quiz_id = done["result"]["quiz_id"]
        self.details["quiz_id"] = self.quiz_id
        self.quiz_path = f"/api/quiz/{self.quiz_id}"
        saved = await self.api.request("GET", self.quiz_path)
        require(
            saved["id"] == self.quiz_id and len(saved["questions"]) == 1,
            "quiz saved a different question set",
        )
        self.quiz_question = saved["questions"][0]
        require(
            self.quiz_question["type"] == "single" and len(self.quiz_question["options"]) >= 2,
            "quiz did not respect the confirmed objective format",
        )
        require(
            self.quiz_question["kc_ids"] == [self.kc["kc_id"]],
            "generated question did not bind the selected course KC",
        )
        self.details["question_id"] = self.quiz_question["id"]

    async def reinforcement_proposal(self):
        before = await self.api.request("GET", self.plans_path)
        # The student submits an unanswered generated item through the same objective grading path.
        response = "暂时不会作答"
        key = f"journey-{self.run_id}-quiz"
        result = await self.api.request(
            "POST",
            f"{self.quiz_path}/attempts",
            headers={"Idempotency-Key": key},
            json={"answers": [{"question_id": self.quiz_question["id"], "response": response}]},
        )
        graded = result["results"][0]
        self.details.update(
            submission_id=result["submission_id"],
            attempt_id=graded["attempt_id"],
            question_id=graded["question_id"],
            correct=graded["correct"],
            response=response,
            reference_answer=graded["answer"],
        )
        require(
            result["grading_status"] == "scored"
            and result["total"] == result["graded_total"] == 1
            and result["correct"] == 0
            and graded["correct"] is False
            and graded["question_id"] == self.quiz_question["id"]
            and graded["answer"],
            "generated objective attempt did not score the observed submission as wrong",
        )
        errors = await self._errors()
        added = [
            item
            for item in errors["items"]
            if (item.get("question") or {}).get("id") == graded["question_id"]
        ]
        self.report["error_updates"]["created"] = errors["total"] - self.baseline_error_total
        self.details["error_ids"] = [item["id"] for item in added]
        require(
            len(added) == 1 and graded["answer"] == added[0]["question"]["answer"],
            "objective quiz error and reference answer did not persist",
        )
        proposal = result.get("plan_adjustment") or {}
        self.details["proposal"] = {
            key: proposal.get(key) for key in ("status", "adjustment_id", "reason")
        }
        require(
            proposal.get("status") == "proposed"
            and proposal.get("plan_id") == self.plan_id
            and proposal.get("inserted"),
            "the true wrong answer produced no actionable automatic reinforcement",
        )
        self.adjustment_id = proposal["adjustment_id"]
        self.inserted = proposal["inserted"]
        self.details["inserted"] = [
            {
                key: task.get(key)
                for key in (
                    "task_id",
                    "kc_id",
                    "evidence",
                    "minutes",
                    "day",
                    "replaces",
                    "material",
                )
            }
            for task in self.inserted
        ]
        for task in self.inserted:
            require(
                task["kc_id"] == self.kc["kc_id"]
                and task["evidence"]
                == {
                    "type": "quiz_attempt",
                    "id": graded["attempt_id"],
                },
                "proposal cites evidence other than this wrong attempt and its mapped KC",
            )
            if material := task.get("material"):
                context = await self._chunk_context(material["chunk_id"])
                require(
                    context["doc_id"] == material["doc_id"],
                    "reinforcement material document mismatch",
                )
        history = await self.api.request("GET", f"{self.plans_path}/adjustments")
        pending = next((row for row in history if row["id"] == self.adjustment_id), None)
        require(
            pending
            and pending["trigger"] == "low_score_attempt"
            and pending["status"] == "proposed"
            and pending["inserted"] == self.inserted,
            "plan history did not confirm the evidence-bound proposal",
        )
        after = await self.api.request("GET", self.plans_path)
        before_tasks = {task["task_id"]: task["status"] for task in plan_tasks(before["structure"])}
        after_tasks = {task["task_id"]: task["status"] for task in plan_tasks(after["structure"])}
        require(
            after["version"] == before["version"]
            and after["progress"] == before["progress"]
            and after_tasks == before_tasks,
            "automatic proposal changed the plan before confirmation",
        )
        self.plan_before_adjustment = after

    async def reinforcement_confirm(self):
        self.confirm_path = f"{self.plans_path}/adjustments/{self.adjustment_id}/confirm"
        applied = await self.api.request("POST", self.confirm_path)
        plan = await self.api.request("GET", self.plans_path)
        before = self.plan_before_adjustment
        tasks = {task["task_id"]: task for task in plan_tasks(plan["structure"])}
        original_ids = {task["task_id"] for task in plan_tasks(before["structure"])}
        inserted_ids = {task["task_id"] for task in self.inserted}
        self.details.update(
            adjustment_id=self.adjustment_id,
            task_ids=sorted(inserted_ids),
            version=plan["version"],
            progress=plan["progress"],
        )
        self.report["plan_progress"] = plan["progress"]
        require(
            applied["status"] == "applied"
            and applied["adjustment_id"] == self.adjustment_id
            and applied["progress"] == plan["progress"]
            and plan["version"] == before["version"] + 1,
            "adjustment did not commit exactly one plan version",
        )
        require(
            len(inserted_ids) == len(self.inserted)
            and set(tasks) == original_ids | inserted_ids
            and plan["progress"]["total"] == before["progress"]["total"] + len(inserted_ids),
            "applied plan has missing or duplicate reinforcement tasks",
        )
        for offered in self.inserted:
            task = tasks[offered["task_id"]]
            require(
                task["status"] == "pending" and task["evidence"] == offered["evidence"],
                "applied task did not preserve its original attempt evidence",
            )
            if replaced := offered.get("replaces"):
                require(
                    tasks[replaced["id"]]["status"] == "skipped",
                    "scheduled replacement did not persist",
                )
        budget = round(self.case.plan.budget_hours_per_day * 60)
        for stage in plan["structure"]["stages"]:
            for day in stage["days"]:
                used = sum(task["minutes"] for task in day["tasks"] if task["status"] != "skipped")
                require(used <= budget, "reinforcement exceeded the daily budget")
        self.applied_plan = plan

    async def repeat_confirm(self):
        replayed = await self.api.request("POST", self.confirm_path)
        verified = await self.api.request("GET", self.plans_path)
        history = await self.api.request("GET", f"{self.plans_path}/adjustments")
        versions = [self.applied_plan["version"], verified["version"]]
        self.details.update(
            adjustment_id=self.adjustment_id, versions=versions, progress=verified["progress"]
        )
        require(
            replayed["status"] == "applied"
            and replayed["adjustment_id"] == self.adjustment_id
            and replayed["inserted"] == self.inserted
            and versions[0] == versions[1]
            and verified["progress"] == self.applied_plan["progress"]
            and plan_tasks(verified["structure"]) == plan_tasks(self.applied_plan["structure"]),
            "repeated confirmation changed the applied plan",
        )
        require(
            sum(row["id"] == self.adjustment_id and row["status"] == "applied" for row in history)
            == 1,
            "the plan history did not retain one applied adjustment",
        )

    async def evidence_audit(self):
        from .learning_journey_usage import read_mastery_audit

        quiz = self.results["reinforcement_proposal"]["evidence"]
        audit = await read_mastery_audit(
            learner_id=self.learner_id,
            kc_id=self.kc["kc_id"],
            assessments=self.assessments,
            quiz_attempt_id=quiz["attempt_id"],
            review_key=f"journey-{self.run_id}-review",
            review_card_id=self.review_item["id"],
            started_at=self.started_at,
            finished_at=datetime.now(UTC),
        )
        self.report["mastery_audit"] = audit
        self.details.update(audit)
        require(
            audit["missing_records"] == 0 and audit["unexpected_records"] == 0,
            "committed mastery evidence differs from this learner's verified submissions",
        )

    async def model_usage(self):
        from .learning_journey_usage import read_model_usage

        ids = set(self.api.request_ids)
        ids.update(agent for values in self.agent_request_ids.values() for agent in values)
        usage = await read_model_usage(
            tenant_id=self.learner_id,
            user_id=self.learner_id,
            request_ids=ids,
            started_at=self.started_at,
            finished_at=datetime.now(UTC),
        )
        matched = usage["matched"]
        unmatched = usage["unmatched"]
        counted = matched["by_request"]
        usage["stage_real_calls"] = {
            name: sum(
                counted.get(request_id, 0)
                for request_id in set(row["request_ids"] + self.agent_request_ids.get(name, []))
            )
            for name, row in self.results.items()
        }
        usage["real_calls"] = matched["real_calls"] + (unmatched or {}).get("real_calls", 0)
        self.report["model_usage"] = usage
        self.details["real_calls"] = usage["real_calls"]
        require(usage["real_calls"] > 0, "no committed live-model calls were observed")


async def run(args: argparse.Namespace, case: JourneyCase, case_hash: str) -> dict:
    timeout = httpx.Timeout(args.request_timeout, connect=min(10, args.request_timeout))
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=timeout, trust_env=False
    ) as client:
        journey = Journey(JourneyApi(client), case, args, case_hash)
        return await journey.run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True, help="pre-registered journey JSON")
    parser.add_argument("--output", type=Path, required=True, help="atomic JSON evidence receipt")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--course-id", type=uuid.UUID, help="existing course owned by the learner")
    parser.add_argument(
        "--register", action="store_true", help="register the dedicated learner through the API"
    )
    parser.add_argument("--request-timeout", type=float, default=360)
    parser.add_argument("--ingestion-timeout", type=float, default=1200)
    parser.add_argument("--poll-interval", type=float, default=3)
    args = parser.parse_args()
    args.case = args.case.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    args.course_id = str(args.course_id) if args.course_id else None
    if args.output == args.case:
        parser.error("the output and case paths must differ")
    url = urlsplit(args.base_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        parser.error("expected an HTTP(S) base URL without credentials, query or fragment")
    if not all(
        0 < value < float("inf")
        for value in (args.request_timeout, args.ingestion_timeout, args.poll_interval)
    ):
        parser.error("timeouts and polling interval must be positive and finite")
    try:
        case_bytes = args.case.read_bytes()
        case = JourneyCase.model_validate_json(case_bytes)
    except OSError:
        parser.error("could not read the journey case")
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
            for error in exc.errors()
        ]
        parser.error(f"invalid journey case ({'; '.join(errors)})")
    report = asyncio.run(run(args, case, hashlib.sha256(case_bytes).hexdigest()))
    print(f"{case.id} {report['status']} ({report['completion_rate']:.1%}) — {args.output}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
