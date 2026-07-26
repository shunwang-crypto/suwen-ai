from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import orjson


class Cassette:
    """File-backed request->response store for record/replay.

    Key = sha256 over the canonicalized request payload, so identical requests
    replay identical responses (streaming rhythm is simulated by the gateway).
    """

    def __init__(self, directory: str | Path):
        self._dir = Path(directory)

    @staticmethod
    def key(payload: dict[str, Any]) -> str:
        canonical = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
        return hashlib.sha256(canonical).hexdigest()

    def _path(self, key: str) -> Path:
        return self._dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        p = self._path(key)
        if not p.exists():
            return None
        return orjson.loads(p.read_bytes())

    def put(self, key: str, value: dict[str, Any]) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(orjson.dumps(value, option=orjson.OPT_INDENT_2))
