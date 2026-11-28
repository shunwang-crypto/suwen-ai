"""Versioned teaching instructions; authorization and tool contracts stay in code."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field

from ..gateway.base import GatewayError

DEFAULT_PROMPT_VERSION = "edu-v1"


class TeachingPolicies(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    qa: tuple[str, ...]
    quiz: tuple[str, ...]
    plan: tuple[str, ...]
    tutor: tuple[str, ...]
    tutor_hint0: tuple[str, ...]
    tutor_hint1: tuple[str, ...]
    tutor_hint2: tuple[str, ...]
    tutor_hint3: tuple[str, ...]


class TeamTeachingPolicies(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    coordinator: tuple[str, ...]
    researcher: tuple[str, ...]
    planner: tuple[str, ...]
    reviewer: tuple[str, ...]


class PromptBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    parent_version: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    policies: TeachingPolicies
    team_policies: TeamTeachingPolicies | None = None

    @property
    def identity(self) -> dict[str, str]:
        payload = self.model_dump(mode="json")
        # Preserve the byte-level identity of pre-team bundles and their checkpoints.
        if self.team_policies is None:
            payload.pop("team_policies")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return {"version": self.version, "sha256": hashlib.sha256(encoded).hexdigest()}

    def text(self, role: str) -> str:
        instructions = getattr(self.policies, role)
        if not instructions or len(instructions) > 24:
            raise ValueError(f"invalid instruction count: {role}")
        if any(not item.strip() or len(item) > 2000 for item in instructions):
            raise ValueError(f"invalid teaching instruction: {role}")
        return "\n".join(instructions)

    def validate_instructions(self) -> PromptBundle:
        texts = [self.text(role) for role in TeachingPolicies.model_fields]
        if self.team_policies is not None:
            for role in TeamTeachingPolicies.model_fields:
                instructions = getattr(self.team_policies, role)
                if not 1 <= len(instructions) <= 24 or any(not t.strip() or len(t) > 2000 for t in instructions):
                    raise ValueError(f"invalid team teaching instructions: {role}")
                texts.append("\n".join(instructions))
        if sum(len(text) for text in texts) > 12000:
            raise ValueError("teaching bundle exceeds its context budget")
        return self


@lru_cache(maxsize=32)
def load_prompt_bundle(version: str = DEFAULT_PROMPT_VERSION) -> PromptBundle:
    # Validate before resolving the resource: configuration is never a path.
    if not isinstance(version, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", version):
        raise GatewayError("HARNESS_PROMPT_VERSION_INVALID", "教学提示词版本无效")
    try:
        text = files(__package__).joinpath("prompt_versions", f"{version}.json").read_text("utf-8")
        bundle = PromptBundle.model_validate_json(text).validate_instructions()
        if bundle.version != version:
            raise ValueError("prompt resource version mismatch")
        return bundle
    except (FileNotFoundError, ValueError) as exc:
        raise GatewayError("HARNESS_PROMPT_VERSION_UNAVAILABLE", "教学提示词版本不可用") from exc


def pin_prompt_bundle(record, settings, override: PromptBundle | None = None) -> PromptBundle:
    pinned = record.state.get("_prompt_bundle")
    version = pinned.get("version") if isinstance(pinned, dict) else settings.education_prompt_version
    bundle = (override or load_prompt_bundle(version)).validate_instructions()
    if pinned is not None and pinned != bundle.identity:
        raise GatewayError("HARNESS_PROMPT_VERSION_MISMATCH", "任务的教学提示词版本或内容已改变")
    record.state["_prompt_bundle"] = bundle.identity
    return bundle


@lru_cache(maxsize=2)
def load_review_prompt(role: str) -> str:
    if role not in {"qa", "quiz"}:
        raise ValueError("unknown independent reviewer")
    prompts = json.loads(files(__package__).joinpath("review_prompts.json").read_text("utf-8"))
    return prompts[role]
