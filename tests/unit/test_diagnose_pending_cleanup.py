"""Read-only pending-cleanup diagnostic safety contracts."""

from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from scripts import diagnose_pending_cleanup as diagnostic

TENANT = "11111111-1111-1111-1111-111111111111"
KB = "22222222-2222-2222-2222-222222222222"
DOC = "33333333-3333-3333-3333-333333333333"


def _ref(*, doc_id: str | None = DOC) -> SimpleNamespace:
    return SimpleNamespace(
        point_id="44444444-4444-4444-4444-444444444444",
        tenant_id=TENANT,
        kb_id=KB,
        doc_id=doc_id,
    )


def _settings(collection: str = "eduagents_chunks") -> SimpleNamespace:
    return SimpleNamespace(qdrant_collection=collection)


@pytest.mark.asyncio
async def test_qdrant_cache_is_tenant_only_and_scan_budget_is_global(monkeypatch):
    calls: list[dict] = []

    class FakeStore:
        def __init__(self, _settings):
            pass

        async def scan_point_refs(self, **kwargs):
            calls.append(kwargs)
            if kwargs["collection"] == "eduagents_chunks":
                return [_ref()]
            return [_ref(doc_id="cache-doc")]

        def close(self):
            pass

    monkeypatch.setattr(diagnostic, "QdrantStore", FakeStore)
    index = await diagnostic._scan_qdrant(
        _settings(), page_size=100, max_pages=10, max_points=2
    )

    assert index.available
    assert index.scanned_points == 2
    assert index.tenant_points[TENANT] == 2
    assert index.kb_points[(TENANT, KB)] == 1
    assert index.doc_points[(TENANT, KB, DOC)] == 1
    assert (TENANT, KB, "cache-doc") not in index.doc_points
    assert [call["collection"] for call in calls] == ["eduagents_chunks", "answer_cache"]
    assert [call["max_points"] for call in calls] == [2, 1]
    assert [call["page_size"] for call in calls] == [2, 1]


@pytest.mark.asyncio
async def test_qdrant_duplicate_collection_is_scanned_once_and_limit_is_explicit(monkeypatch):
    calls: list[str] = []

    class FakeStore:
        def __init__(self, _settings):
            pass

        async def scan_point_refs(self, **kwargs):
            calls.append(kwargs["collection"])
            return [_ref()]

        def close(self):
            pass

    monkeypatch.setattr(diagnostic, "QdrantStore", FakeStore)
    index = await diagnostic._scan_qdrant(
        _settings("answer_cache"), page_size=10, max_pages=10, max_points=1
    )
    assert index.available
    assert calls == ["answer_cache"]
    assert index.tenant_points[TENANT] == 1
    assert not index.kb_points
    assert not index.doc_points

    limited = await diagnostic._scan_qdrant(
        _settings(), page_size=10, max_pages=10, max_points=1
    )
    assert not limited.available
    assert limited.error_code == "QDRANT_SCAN_LIMIT"
    assert calls == ["answer_cache", "eduagents_chunks"]


@pytest.mark.asyncio
async def test_qdrant_page_limit_and_timeout_are_safe_codes(monkeypatch):
    class FailingStore:
        def __init__(self, _settings):
            pass

        async def scan_point_refs(self, **_kwargs):
            raise RuntimeError("qdrant point scan page limit exceeded")

        def close(self):
            pass

    monkeypatch.setattr(diagnostic, "QdrantStore", FailingStore)
    limited = await diagnostic._scan_qdrant(
        _settings(), page_size=1, max_pages=1, max_points=10
    )
    assert limited.error_code == "QDRANT_SCAN_LIMIT"

    class TimeoutStore(FailingStore):
        async def scan_point_refs(self, **_kwargs):
            raise TimeoutError

    monkeypatch.setattr(diagnostic, "QdrantStore", TimeoutStore)
    timed_out = await diagnostic._scan_qdrant(
        _settings(), page_size=1, max_pages=1, max_points=10
    )
    assert timed_out.error_code == "QDRANT_READ_TIMEOUT"


@pytest.mark.asyncio
async def test_malformed_qdrant_payload_is_not_counted_as_cleaned(monkeypatch):
    class MalformedStore:
        def __init__(self, _settings):
            pass

        async def scan_point_refs(self, **kwargs):
            if kwargs["collection"] == "answer_cache":
                return []
            return [
                SimpleNamespace(
                    point_id="not-a-uuid",
                    tenant_id=TENANT,
                    kb_id=KB,
                    doc_id=DOC,
                )
            ]

        def close(self):
            pass

    monkeypatch.setattr(diagnostic, "QdrantStore", MalformedStore)
    index = await diagnostic._scan_qdrant(
        _settings(), page_size=10, max_pages=10, max_points=10
    )

    assert index.malformed_points == 1
    assert not index.available
    assert index.error_code == "QDRANT_MALFORMED_PAYLOAD"
    assert not index.doc_points

    row = diagnostic.PendingRow(
        row_id=str(uuid.uuid4()),
        kind="doc",
        payload={"tenant_id": TENANT, "kb_id": KB, "doc_id": DOC, "filename": "a.md"},
        created_at=datetime.now(UTC),
        attempts=1,
        done=False,
    )

    class EmptyStorage:
        async def count(self, _prefix):
            return 0, None

    report, snapshot = await diagnostic._row_report(
        row,
        now=datetime.now(UTC),
        qdrant=index,
        storage=EmptyStorage(),
        redis=object(),
        conn=object(),
    )
    assert report["classification"] == "external_unavailable"
    assert report["root_cause"] == "QDRANT_MALFORMED_PAYLOAD"
    assert snapshot.error_code == "QDRANT_MALFORMED_PAYLOAD"


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("doc", {"tenant_id": TENANT, "kb_id": KB, "doc_id": DOC, "filename": "a.md"}),
        ("kb", {"tenant_id": TENANT, "kb_id": KB}),
        ("tenant", {"tenant_id": TENANT, "session_ids": ["session-1"]}),
        ("object_prefix", {"prefix": f"{TENANT}/objects"}),
    ],
)
def test_payload_contract_is_checked_without_emitting_payload(kind, payload):
    row = diagnostic.PendingRow(
        row_id=str(uuid.uuid4()),
        kind=kind,
        payload=payload,
        created_at=datetime.now(UTC),
        attempts=0,
        done=False,
    )
    status, normalized, error = diagnostic._payload_error(row)
    assert status is None
    assert normalized is not None
    assert error is None

    bad = diagnostic.PendingRow(
        row_id=row.row_id,
        kind=kind,
        payload={**payload, "secret": "do-not-report"},
        created_at=row.created_at,
        attempts=0,
        done=False,
    )
    status, _normalized, error = diagnostic._payload_error(bad)
    assert status == "security_anomaly"
    assert error == {"code": "UNEXPECTED_PAYLOAD_FIELDS", "type": "payload_shape"}


def test_unknown_kind_and_error_codes_are_redacted():
    row = diagnostic.PendingRow(
        row_id="raw-row-id",
        kind="future_kind",
        payload={"token": "do-not-report"},
        created_at=datetime.now(UTC) - timedelta(hours=1),
        attempts=2,
        done=False,
        last_error_code="token=do-not-report",
    )
    status, _normalized, error = diagnostic._payload_error(row)
    assert status == "payload_error"
    assert error == {"code": "UNKNOWN_KIND", "type": "payload"}
    assert diagnostic._safe_code(row.last_error_code) == "REDACTED_CODE"


@pytest.mark.asyncio
async def test_progress_json_and_log_correlation_are_projected_without_secrets(tmp_path):
    row_id = str(uuid.uuid4())
    log_path = tmp_path / "cleanup.ndjson"
    log_path.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "event": "cleanup_outbox_retry",
                        "id": row_id,
                        "error_code": "token=super-secret",
                    }
                ),
                json.dumps(
                    {
                        "event": "unrelated",
                        "id": row_id,
                        "secret": "must-not-report",
                    }
                ),
            )
        ),
        encoding="utf-8",
    )
    correlation = diagnostic._load_log_correlation(log_path, {row_id})
    assert correlation.available
    assert correlation.event_counts == {"cleanup_outbox_retry": 1}
    assert correlation.per_row[diagnostic._hash_id(row_id)] == {"cleanup_outbox_retry": 1}
    assert row_id not in str(correlation)
    assert "super-secret" not in str(correlation)

    row = diagnostic.PendingRow(
        row_id=row_id,
        kind="doc",
        payload={"tenant_id": TENANT, "kb_id": KB, "doc_id": DOC, "filename": "a.md"},
        created_at=datetime.now(UTC),
        attempts=1,
        done=False,
        progress_json={
            "phase": "executing",
            "worker_id": "worker-secret",
            "claim_id": row_id,
            "current_step": "qdrant",
            "completed_steps": ["storage", "not-a-component"],
            "error_code": "token=super-secret",
            "secret": "must-not-report",
        },
    )

    class EmptyStorage:
        async def count(self, _prefix):
            return 0, None

    report, _snapshot = await diagnostic._row_report(
        row,
        now=datetime.now(UTC),
        qdrant=diagnostic.QdrantIndex(),
        storage=EmptyStorage(),
        redis=object(),
        conn=object(),
        log_correlation=correlation,
    )
    assert report["log_events"] == {"cleanup_outbox_retry": 1}
    assert report["progress_json"]["claim_id_matches_row"] is True
    assert report["progress_json"]["worker_id_hash"] != "worker-secret"
    assert "worker-secret" not in str(report)
    assert "super-secret" not in str(report)
    assert "must-not-report" not in str(report)


@pytest.mark.asyncio
async def test_row_report_timeout_is_incomplete_without_external_write():
    class SlowStorage:
        async def count(self, _prefix):
            await asyncio.sleep(0.05)
            return 1, None

    row = diagnostic.PendingRow(
        row_id=str(uuid.uuid4()),
        kind="doc",
        payload={"tenant_id": TENANT, "kb_id": KB, "doc_id": DOC, "filename": "a.md"},
        created_at=datetime.now(UTC),
        attempts=1,
        done=False,
    )
    report, snapshot = await diagnostic._row_report(
        row,
        now=datetime.now(UTC),
        qdrant=diagnostic.QdrantIndex(),
        storage=SlowStorage(),
        redis=object(),
        conn=object(),
        timeout_seconds=0.001,
    )
    assert report["classification"] == "external_unavailable"
    assert report["root_cause"] == "STORAGE_READ_TIMEOUT"
    assert snapshot.error_code == "STORAGE_READ_TIMEOUT"
    assert report["row_id_hash"] != row.row_id
    assert "do-not" not in str(report)


@pytest.mark.asyncio
async def test_redis_inspector_uses_only_exists_scan_and_ttl():
    calls: list[str] = []

    class FakeRedis:
        async def exists(self, key):
            calls.append(f"exists:{key}")
            return key == f"orgs:{TENANT}"

        async def ttl(self, key):
            calls.append(f"ttl:{key}")
            return 60

        async def scan_iter(self, **kwargs):
            calls.append(f"scan:{kwargs['match']}")
            if False:
                yield None

    count, error = await diagnostic.RedisInspector(FakeRedis()).tenant_count(TENANT, None)
    assert (count, error) == (1, None)
    assert all(call.startswith(("exists:", "ttl:", "scan:")) for call in calls)
    assert not any(call.startswith(("get:", "set:", "delete:")) for call in calls)


@pytest.mark.asyncio
async def test_object_inspector_uses_list_only_and_enforces_limit(monkeypatch):
    calls: list[str] = []

    class FakeMinio:
        def __init__(self, *_args, **_kwargs):
            pass

        def list_objects(self, _bucket, *, prefix, recursive):
            calls.append(f"list:{prefix}:{recursive}")
            return iter([object(), object()])

    monkeypatch.setitem(sys.modules, "minio", types.SimpleNamespace(Minio=FakeMinio))
    settings = SimpleNamespace(
        storage_backend="minio",
        minio_endpoint="objects.example:9000",
        minio_app_access_key="readonly",
        minio_app_secret_key="readonly-secret",
        minio_secure=False,
        minio_bucket="bucket",
    )
    inspector = diagnostic.ObjectInspector(settings, max_objects=1)
    count, error = await inspector.count(f"{TENANT}/objects")
    assert count is None
    assert error == "STORAGE_LIST_LIMIT"
    assert calls == [f"list:{TENANT}/objects:True"]


@pytest.mark.asyncio
async def test_checkpoint_inspector_uses_select_count_only():
    statements: list[str] = []

    class FakeConnection:
        async def scalar(self, statement, _params):
            sql = str(statement)
            statements.append(sql)
            return "public.checkpoints" if "to_regclass" in sql else 3

    count, error = await diagnostic._checkpoint_count(FakeConnection(), TENANT)
    assert (count, error) == (9, None)
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


def test_target_validation_and_exit_codes_are_explicit():
    args = SimpleNamespace(
        target_id="prod-eu-1",
        environment="prod",
        database_url="postgresql+asyncpg://readonly@db.example/eduagents",
        qdrant_url="https://qdrant.example",
        redis_url="rediss://redis.example/0",
        storage_backend="minio",
        minio_endpoint="objects.example:9000",
    )
    diagnostic._validate_target(args)
    args.qdrant_url = "http://127.0.0.1:6333"
    with pytest.raises(diagnostic.DiagnosticContractError):
        diagnostic._validate_target(args)

    assert diagnostic._status_exit_code(
        diagnostic.DiagnosticReport({}, {"pending_total": 0}, [])
    ) == 0
    assert diagnostic._status_exit_code(
        diagnostic.DiagnosticReport({}, {"pending_total": 1}, [])
    ) == 1
    assert diagnostic._status_exit_code(
        diagnostic.DiagnosticReport({}, {"pending_total": 0}, [], incomplete=True)
    ) == 2
    assert diagnostic._status_exit_code(
        diagnostic.DiagnosticReport(
            {}, {"pending_total": 0}, [], service_errors=["REDIS_PERMISSION_DENIED"]
        )
    ) == 3
