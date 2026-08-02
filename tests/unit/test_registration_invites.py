from __future__ import annotations

import pytest
from eduagents_api.registration_invites import (
    generate_invite_code,
    hash_invite_code,
)
from eduagents_core.config import Settings
from pydantic import ValidationError


def test_invite_codes_are_high_entropy_and_only_hash_is_storage_value():
    first = generate_invite_code()
    second = generate_invite_code()
    assert first.startswith("EDU-")
    assert len(first) == 28
    assert first != second
    assert first not in hash_invite_code(first)
    assert hash_invite_code(first) == hash_invite_code(f"  {first.lower()} ")


def test_registration_mode_defaults_closed_in_production_and_open_in_dev():
    assert Settings(_env_file=None, app_env="prod").registration_mode == "closed"
    assert Settings(_env_file=None, app_env="dev").registration_mode == "open"


def test_legacy_public_registration_is_compatible_but_conflicts_are_rejected():
    assert (
        Settings(_env_file=None, app_env="dev", public_registration=True).registration_mode
        == "open"
    )
    with pytest.raises(ValidationError, match="conflicts"):
        Settings(
            _env_file=None,
            app_env="prod",
            registration_mode="invite",
            public_registration=False,
        )
