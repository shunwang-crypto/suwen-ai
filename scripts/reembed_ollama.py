"""Rebuild an offline knowledge index into the separate Ollama embedding space."""

from __future__ import annotations

import asyncio
import hashlib
import json

from eduagents_core.config import get_settings
from eduagents_core.embedding.providers import create_embedder
from eduagents_core.embedding.sparse import Bm25SparseEncoder
from eduagents_core.vectorstore.base import VectorPoint
from eduagents_core.vectorstore.qdrant_store import QdrantStore, _get_client


async def rebuild():
    settings = get_settings()
    if settings.embedding_mode != "ollama":
        raise ValueError("run with EMBEDDING_MODE=ollama while document writers are stopped")
    client = _get_client(settings)
    target = settings.qdrant_collection
    source = settings.retired_chunk_collection
    if source == target or not client.collection_exists(source):
        raise ValueError("source index missing")
    store = QdrantStore(settings)
    model = create_embedder(settings)
    sparse = Bm25SparseEncoder()

    async def scan(collection, write=False):
        digest = hashlib.sha256()
        count, offset = 0, None
        while True:
            rows, offset = await asyncio.to_thread(
                client.scroll,
                collection,
                limit=4,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            texts = []
            for row in rows:
                payload = row.payload
                if not all(payload.get(key) for key in ("text", "doc_id", "tenant_id", "kb_id")):
                    raise ValueError("incomplete indexed chunk")
                digest.update(json.dumps([str(row.id), payload], sort_keys=True).encode())
                texts.append(
                    "\n".join(
                        part
                        for part in (
                            payload.get("context_prefix"),
                            payload.get("heading_path"),
                            payload["text"],
                        )
                        if part
                    )
                )
            if write and rows:
                dense = await model.embed(texts)
                await store.upsert(
                    [
                        VectorPoint(str(row.id), vector, sparse.encode_document(text), row.payload)
                        for row, vector, text in zip(rows, dense, texts, strict=True)
                    ]
                )
            count += len(rows)
            if offset is None:
                return count, digest.hexdigest()

    try:
        await store.ensure_ready()
        original = await scan(source, write=True)
        if original != await scan(target) or original != await scan(source):
            raise RuntimeError(
                "source changed or destination differs; keep the original configuration"
            )
        print(f"Verified {original[0]} point IDs and payloads, collection={target}")
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(rebuild())
