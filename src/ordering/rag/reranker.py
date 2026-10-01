"""Cross-encoder reranking (fastembed ONNX, runs locally, no API key).

Cross-encoders score (query, passage) pairs jointly, which is far more precise than the
bi-encoder similarity used for recall. Scores are mapped through a sigmoid to [0, 1] so a single
evidence threshold (``RAG_MIN_RERANK_SCORE``) can be applied.
"""

from __future__ import annotations

import asyncio
import math

from ordering.common.config import get_settings
from ordering.common.logging import get_logger

log = get_logger("ordering.rag.reranker")
_model = None
_unavailable = False


def _load():
    global _model, _unavailable
    if _model is None and not _unavailable:
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            settings = get_settings()
            _model = TextCrossEncoder(settings.reranker_model, cache_dir=settings.fastembed_cache_dir)
        except Exception as exc:  # model download blocked, etc. → degrade to fused scores
            log.warning("reranker_unavailable", error=str(exc)[:200])
            _unavailable = True
    return _model


def available() -> bool:
    return get_settings().reranker_enabled and _load() is not None


async def rerank(query: str, passages: list[str]) -> list[float] | None:
    if not passages or not available():
        return None
    model = _load()
    scores = await asyncio.to_thread(lambda: list(model.rerank(query, passages, batch_size=16)))
    return [1.0 / (1.0 + math.exp(-float(s))) for s in scores]
