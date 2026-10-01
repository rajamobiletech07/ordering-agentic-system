"""Hybrid retrieval: pgvector (semantic) + Postgres full-text (lexical) → RRF fusion → rerank.

Tenant-aware: queries run in a tenant-scoped transaction, so RLS returns only the tenant's
documents plus shared ones (and the SQL repeats the filter as defence in depth). Only chunks
of the *current* document version are eligible.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from sqlalchemy import text

from ordering.common.config import get_settings
from ordering.common.db import tenant_session
from ordering.common.logging import get_logger
from ordering.rag import reranker
from ordering.rag.embeddings.base import PROFILE_DIMS, get_embedder

log = get_logger("ordering.rag.retriever")
RRF_K = 60
CANDIDATES = 20
_STOP = {"the", "and", "for", "are", "what", "how", "does", "can", "our", "with", "that", "this", "from", "have",
         "who", "when", "which", "about", "into", "your", "you", "is", "a", "an", "of", "to", "in", "on", "do", "i",
         "we", "it", "be", "or", "my", "me", "tell", "please", "policy", "rule", "rules"}


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    title: str
    source_uri: str
    doc_type: str
    tenant_id: str | None
    version: str | None
    section_path: str
    content: str
    vector_score: float = 0.0
    keyword_score: float = 0.0
    fused_score: float = 0.0
    rerank_score: float | None = None

    @property
    def score(self) -> float:
        return self.rerank_score if self.rerank_score is not None else self.fused_score

    def to_dict(self) -> dict:
        return {**asdict(self), "score": self.score}


@dataclass
class RetrievalResult:
    query: str
    chunks: list[RetrievedChunk]
    profile: str
    degraded: bool = False
    reranked: bool = False

    @property
    def sufficient(self) -> bool:
        settings = get_settings()
        if not self.chunks:
            return False
        top = self.chunks[0]
        if self.reranked:
            return (top.rerank_score or 0) >= settings.rag_min_rerank_score
        return top.fused_score >= settings.rag_min_fused_score and (top.keyword_score > 0 or top.vector_score >= 0.35)


def keyword_query(query: str) -> str | None:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", query.lower()) if len(w) > 2 and w not in _STOP]
    return " | ".join(dict.fromkeys(words)) or None


async def retrieve(query: str, tenant_id: str, top_k: int | None = None, profile: str | None = None) -> RetrievalResult:
    settings = get_settings()
    top_k = top_k or settings.rag_top_k
    profile = profile or settings.embedding_profile
    if profile not in PROFILE_DIMS:  # table-name whitelist (profile is interpolated into SQL)
        raise ValueError(f"unknown embedding profile {profile!r}")
    degraded = False
    qvec = None
    try:
        qvec = (await get_embedder(profile).embed([query]))[0]
    except Exception as exc:  # embedding provider down → keyword-only retrieval, flagged
        log.warning("rag_embedding_failed_keyword_only", profile=profile, error=str(exc)[:200])
        degraded = True

    tsq = keyword_query(query)
    ranks: dict[str, dict[str, float]] = {}
    async with tenant_session(tenant_id) as s:
        if qvec is not None:
            rows = await s.execute(text(
                f"""SELECT c.chunk_id::text AS id, 1 - (e.embedding <=> CAST(:q AS vector)) AS score
                    FROM rag_embeddings_{profile} e JOIN rag_chunks c ON c.chunk_id = e.chunk_id
                    WHERE c.is_current AND (c.tenant_id IS NULL OR c.tenant_id = :t)
                    ORDER BY e.embedding <=> CAST(:q AS vector) LIMIT :k"""),
                {"q": json.dumps(qvec), "t": tenant_id, "k": CANDIDATES})
            for rank, r in enumerate(rows, start=1):
                ranks.setdefault(r.id, {})["vec_rank"] = rank
                ranks[r.id]["vector_score"] = float(r.score)
        if tsq:
            rows = await s.execute(text(
                """SELECT c.chunk_id::text AS id, ts_rank_cd(c.tsv, q) AS score
                   FROM rag_chunks c, to_tsquery('english', :q) q
                   WHERE c.is_current AND (c.tenant_id IS NULL OR c.tenant_id = :t) AND c.tsv @@ q
                   ORDER BY score DESC LIMIT :k"""),
                {"q": tsq, "t": tenant_id, "k": CANDIDATES})
            for rank, r in enumerate(rows, start=1):
                ranks.setdefault(r.id, {})["kw_rank"] = rank
                ranks[r.id]["keyword_score"] = float(r.score)
        if not ranks:
            return RetrievalResult(query, [], profile, degraded)
        fused = {cid: sum(1.0 / (RRF_K + v[k]) for k in ("vec_rank", "kw_rank") if k in v) for cid, v in ranks.items()}
        candidate_ids = sorted(fused, key=fused.get, reverse=True)[:CANDIDATES]
        rows = await s.execute(text(
            """SELECT c.chunk_id::text AS chunk_id, c.document_id::text AS document_id, d.title, d.source_uri, d.doc_type,
                      d.tenant_id, v.declared_version AS version, c.section_path, c.content
               FROM rag_chunks c JOIN rag_documents d ON d.document_id = c.document_id
               JOIN rag_document_versions v ON v.version_id = c.version_id
               WHERE c.chunk_id = ANY(CAST(:ids AS uuid[]))"""),
            {"ids": candidate_ids})
        by_id = {r.chunk_id: RetrievedChunk(**dict(r._mapping)) for r in rows}

    chunks = []
    for cid in candidate_ids:
        chunk = by_id.get(cid)
        if chunk is None:
            continue
        chunk.vector_score = ranks[cid].get("vector_score", 0.0)
        chunk.keyword_score = ranks[cid].get("keyword_score", 0.0)
        chunk.fused_score = fused[cid]
        chunks.append(chunk)

    scores = await reranker.rerank(query, [f"{c.section_path}\n{c.content}" for c in chunks])
    if scores is not None:
        for chunk, score in zip(chunks, scores, strict=True):
            chunk.rerank_score = score
        chunks.sort(key=lambda c: c.rerank_score or 0, reverse=True)
    return RetrievalResult(query, chunks[:top_k], profile, degraded, reranked=scores is not None)
