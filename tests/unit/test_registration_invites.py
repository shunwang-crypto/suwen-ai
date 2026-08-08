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
    prod = Settings(_env_file=None, app_env="prod")
    dev = Settings(_env_file=None, app_env="dev")
    assert prod.registration_mode == "closed"
    assert dev.registration_mode == "open"
    assert prod.registration_mode_source == "default"
    assert dev.registration_mode_source == "default"


def test_registration_mode_records_canonical_source_without_logging_value():
    settings = Settings(_env_file=None, app_env="prod", registration_mode="invite")
    assert settings.registration_mode == "invite"
    assert settings.registration_mode_source == "REGISTRATION_MODE"


def test_legacy_public_registration_is_compatible_and_records_alias_source():
    settings = Settings(_env_file=None, app_env="dev", public_registration=True)
    assert settings.registration_mode == "open"
    assert settings.registration_mode_source == "PUBLIC_REGISTRATION"


def test_registration_mode_conflict_is_rejected():
    with pytest.raises(ValidationError, match="conflicts"):
        Settings(
            _env_file=None,
            app_env="prod",
            registration_mode="invite",
            public_registration=False,
        )
