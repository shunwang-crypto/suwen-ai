"""Small local Chinese memory embeddings; independent of knowledge-base vectors."""

from __future__ import annotations

import asyncio
from pathlib import Path


class LocalMemoryEmbedder:
    dim = 512
    model_name = "BAAI/bge-small-zh-v1.5"

    def __init__(self, cache_dir):
        from fastembed import TextEmbedding
        self.model = TextEmbedding(model_name=self.model_name, cache_dir=str(Path(cache_dir).expanduser()), threads=2)
        self.slots = asyncio.Semaphore(1)

    async def embed(self, texts):
        async with self.slots:
            return await asyncio.to_thread(lambda: [vector.tolist() for vector in self.model.embed(texts)])
