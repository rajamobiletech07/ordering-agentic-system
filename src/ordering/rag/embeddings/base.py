"""Embedding profiles.

Adapted from mcp-core ``memory_server/embeddings/{base,openai}.py`` (``EmbeddingBase.embed``):
async, batched, and selected by *profile*. Each profile has a fixed dimension and its own
pgvector table because vector spaces are not interchangeable.

* ``openai``  - text-embedding-3-small (1536d) via the LLM Gateway (cost-tracked) - default
* ``gemini``  - gemini-embedding-2 (768d) via the LLM Gateway
* ``local``   - fastembed BAAI/bge-small-en-v1.5 (384d), in-process ONNX, no API key
* ``hashing`` - deterministic feature hashing (256d) for offline tests
"""

from __future__ import annotations

from abc import ABC, abstractmethod

PROFILE_DIMS = {"openai": 1536, "gemini": 768, "local": 384, "hashing": 256}


class Embedder(ABC):
    profile: str
    dims: int
    model: str

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder(Embedder):
    profile, dims, model = "hashing", 256, "feature-hashing-v1"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from ordering.rag.embeddings.hashing import hashing_vector

        return [hashing_vector(t, self.dims) for t in texts]


class LocalEmbedder(Embedder):
    profile, dims = "local", 384

    _models: dict[str, object] = {}

    def __init__(self, model: str | None = None):
        from ordering.common.config import get_settings

        self.model = model or get_settings().local_embedding_model

    def _load(self):
        if self.model not in self._models:
            from fastembed import TextEmbedding

            from ordering.common.config import get_settings

            self._models[self.model] = TextEmbedding(self.model, cache_dir=get_settings().fastembed_cache_dir)
        return self._models[self.model]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        model = self._load()
        return await asyncio.to_thread(lambda: [list(map(float, v)) for v in model.embed(texts, batch_size=32)])


class GatewayEmbedder(Embedder):
    """Remote embeddings through the LLM Gateway (auth, budget and cost ledger apply)."""

    def __init__(self, profile: str, model: str = ""):
        self.profile, self.dims, self.model = profile, PROFILE_DIMS[profile], model or profile

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from ordering.llm_client.client import get_llm_client

        vectors: list[list[float]] = []
        for start in range(0, len(texts), 64):
            vectors += await get_llm_client().embed(texts[start : start + 64], profile=self.profile)
        return vectors


def get_embedder(profile: str | None = None) -> Embedder:
    from ordering.common.config import get_settings

    profile = profile or get_settings().embedding_profile
    if profile == "hashing":
        return HashingEmbedder()
    if profile == "local":
        return LocalEmbedder()
    if profile in ("openai", "gemini"):
        return GatewayEmbedder(profile)
    raise ValueError(f"unknown embedding profile {profile}")
