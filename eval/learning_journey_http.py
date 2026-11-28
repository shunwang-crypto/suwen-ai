"""Case input and HTTP receipts for the live learner journey."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import defaultdict
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

import httpx
from pydantic import BaseModel, Field, model_validator


class DocumentCase(BaseModel):
    path: Path
    enrichment: Literal["semantic", "auto", "headings"] = "semantic"
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CourseCase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    subject: str = Field(default="general", max_length=64)
    documents: list[DocumentCase] = Field(default_factory=list)


class QaCase(BaseModel):
    question: str = Field(min_length=1)
    document: str = Field(min_length=1)
    heading_contains: str = Field(min_length=1)
    terms: list[list[str]] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)


class TutorCase(BaseModel):
    problem: str = Field(min_length=1, max_length=4000)
    turns: list[str] = Field(min_length=2)
    final_hint_concepts: list[list[str]] = Field(default_factory=list)
    premature_answers: list[str] = Field(default_factory=list)


class PlanCase(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    total_days: int = Field(ge=1, le=90)
    budget_hours_per_day: float = Field(gt=0, le=12)
    current_level: str | None = Field(default=None, max_length=200)
    target_outcome: str | None = Field(default=None, max_length=500)
    success_criteria: str | None = Field(default=None, max_length=500)


class QuestionCase(BaseModel):
    stem: str = Field(min_length=1, max_length=12000)
    type: Literal["single"] = "single"
    options: list[str] = Field(min_length=2, max_length=8)
    answer: str
    response: str
    explanation: str = Field(default="", max_length=12000)

    @model_validator(mode="after")
    def validate_choices(self):
        choices = "ABCDEFGH"[: len(self.options)]
        self.answer = self.answer.strip().upper()
        self.response = self.response.strip().upper()
        if self.answer not in set(choices) or self.response not in set(choices):
            raise ValueError("answer and response must each name an option")
        return self


class PracticeCase(BaseModel):
    kc: str = Field(min_length=1)
    kc_aliases: list[str] = Field(default_factory=list)
    questions: list[QuestionCase] = Field(min_length=3)

    @model_validator(mode="after")
    def independent_mistakes(self):
        if len({question.stem.strip() for question in self.questions}) != len(self.questions):
            raise ValueError("practice questions must have distinct stems")
        if sum(q.answer != q.response for q in self.questions) < 3:
            raise ValueError("the reinforcement case requires three pre-registered mistakes")
        return self


class JourneyCase(BaseModel):
    id: str = Field(min_length=1)
    course: CourseCase
    qa: QaCase
    tutor: TutorCase
    plan: PlanCase
    practice: PracticeCase


class CheckFailed(RuntimeError):
    """A failed check written into the evaluation receipt."""


def require(condition, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def _public_code(value, fallback: str) -> str:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", value)
        else fallback
    )


class JourneyApi:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.request_ids: list[str] = []

    def _headers(self, headers: dict | None) -> dict:
        request_id = uuid.uuid4().hex
        self.request_ids.append(request_id)
        return {**(headers or {}), "X-Request-ID": request_id}

    async def _check(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        await response.aread()
        fallback = f"HTTP_{response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            payload = None
        public = payload.get("code", payload.get("detail")) if isinstance(payload, dict) else None
        code = _public_code(public, fallback)
        raise CheckFailed(
            f"{response.request.method} {response.request.url.path}: {code} ({response.status_code})"
        )

    async def request(self, method: str, path: str, *, headers=None, **kwargs):
        response = await self.client.request(method, path, headers=self._headers(headers), **kwargs)
        await self._check(response)
        return response.json() if response.content else None

    async def stream(self, path: str, payload: dict) -> dict[str, list[dict]]:
        events: dict[str, list[dict]] = defaultdict(list)
        async with self.client.stream(
            "POST", path, headers=self._headers({"Accept": "text/event-stream"}), json=payload
        ) as response:
            await self._check(response)
            require(
                "text/event-stream" in response.headers.get("content-type", ""),
                "expected event stream",
            )
            kind, data = "message", []
            async for line in response.aiter_lines():
                if not line:
                    if data:
                        events[kind].append(json.loads("\n".join(data)))
                    kind, data = "message", []
                elif line.startswith("event:"):
                    kind = line.removeprefix("event:").strip()
                elif line.startswith("data:"):
                    data.append(line.removeprefix("data:").removeprefix(" "))
            if data:
                events[kind].append(json.loads("\n".join(data)))
        return events


def terminal(events: dict, kind: str) -> dict:
    errors = events.get("error", [])
    if errors:
        raise CheckFailed(
            f"event stream failed: {_public_code(errors[-1].get('code'), 'STREAM_ERROR')}"
        )
    candidates = events.get(kind, [])
    require(len(candidates) == 1, f"expected one {kind} event")
    return candidates[0]


def save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        staged = Path(temporary.name)
        try:
            json.dump(report, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
