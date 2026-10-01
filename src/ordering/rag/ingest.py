"""Ingestion: parse → chunk → dedup/version → embed → store.

* **Versioning**: a document is identified by ``(tenant, source_uri)``. Changed content creates
  a new version; the version with the latest ``effective_date`` is current and only current
  chunks are retrievable. Older versions are kept for audit.
* **Duplicate detection**: identical content (SHA-256 of normalized text) already current under
  another document in the same tenant scope is skipped and reported as a duplicate.
* **Re-runs are idempotent**: unchanged documents are skipped; missing embeddings for the
  requested profiles are back-filled (so ``--profile all`` enables instant profile switching).
Runs with the owner DB role (RAG tables are read-only for the application role).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ordering.common.context import Principal, RequestContext, reset_context, set_context
from ordering.common.db import admin_session
from ordering.common.ids import sha256_hex
from ordering.common.logging import get_logger
from ordering.rag.chunker import chunk_text
from ordering.rag.embeddings.base import PROFILE_DIMS, get_embedder
from ordering.rag.loaders import LoadedDocument, discover, load_document

log = get_logger("ordering.rag.ingest")


@dataclass
class IngestResult:
    source_uri: str
    tenant_id: str | None
    status: str  # created | new_version | historical_version | unchanged | duplicate
    version_no: int | None = None
    chunks: int = 0
    embedded: dict[str, int] = field(default_factory=dict)
    duplicate_of: str | None = None


async def _embed_missing(session: AsyncSession, version_id: UUID, profiles: list[str]) -> dict[str, int]:
    counts = {}
    for profile in profiles:
        if profile not in PROFILE_DIMS:  # table-name whitelist (profile is interpolated into SQL)
            raise ValueError(f"unknown embedding profile {profile!r}")
        rows = (await session.execute(
            text(f"""SELECT c.chunk_id, c.section_path, c.content FROM rag_chunks c
                     LEFT JOIN rag_embeddings_{profile} e ON e.chunk_id = c.chunk_id
                     WHERE c.version_id = :v AND e.chunk_id IS NULL ORDER BY c.chunk_index"""),
            {"v": version_id},
        )).all()
        if not rows:
            counts[profile] = 0
            continue
        embedder = get_embedder(profile)
        # Section path is prepended so headings contribute to the semantic signal.
        vectors = await embedder.embed([f"{r.section_path}\n{r.content}" for r in rows])
        await session.execute(
            text(f"INSERT INTO rag_embeddings_{profile} (chunk_id, model, embedding) VALUES (:c, :m, CAST(:e AS vector)) "
                 "ON CONFLICT (chunk_id) DO NOTHING"),
            [{"c": r.chunk_id, "m": embedder.model, "e": json.dumps(v)} for r, v in zip(rows, vectors, strict=True)],
        )
        counts[profile] = len(rows)
    return counts


async def ingest_document(doc: LoadedDocument, profiles: list[str]) -> IngestResult:
    content_hash = sha256_hex(doc.text)
    scope = doc.tenant_id or "__shared__"
    # Embedding calls go through the LLM gateway on behalf of a system principal for this tenant.
    token = set_context(RequestContext(principal=Principal(user_id="system:rag-ingest", tenant_id=doc.tenant_id or "system",
                                                           roles=frozenset({"admin"})), agent="rag_ingest"))
    try:
        async with admin_session() as s:
            dup = (await s.execute(text(
                """SELECT d.source_uri FROM rag_document_versions v JOIN rag_documents d ON d.document_id = v.document_id
                   WHERE v.content_sha256 = :h AND v.is_current AND COALESCE(d.tenant_id, '__shared__') = :scope
                     AND d.source_uri <> :uri LIMIT 1"""),
                {"h": content_hash, "scope": scope, "uri": doc.source_uri})).first()
            if dup:
                return IngestResult(doc.source_uri, doc.tenant_id, "duplicate", duplicate_of=dup.source_uri)

            row = (await s.execute(text(
                "SELECT document_id FROM rag_documents WHERE COALESCE(tenant_id, '__shared__') = :scope AND source_uri = :uri"),
                {"scope": scope, "uri": doc.source_uri})).first()
            if row is None:
                document_id = uuid4()
                await s.execute(text(
                    """INSERT INTO rag_documents (document_id, tenant_id, source_uri, doc_type, title)
                       VALUES (:id, :t, :uri, :dt, :title)"""),
                    {"id": document_id, "t": doc.tenant_id, "uri": doc.source_uri, "dt": doc.doc_type, "title": doc.title})
                status = "created"
            else:
                document_id = row.document_id
                await s.execute(text("UPDATE rag_documents SET title = :title, doc_type = :dt, updated_at = now() "
                                     "WHERE document_id = :id"), {"title": doc.title, "dt": doc.doc_type, "id": document_id})
                status = "new_version"

            existing = (await s.execute(text(
                "SELECT version_id, version_no FROM rag_document_versions WHERE document_id = :d AND content_sha256 = :h"),
                {"d": document_id, "h": content_hash})).first()
            if existing:
                embedded = await _embed_missing(s, existing.version_id, profiles)
                return IngestResult(doc.source_uri, doc.tenant_id, "unchanged", existing.version_no, 0, embedded)

            current = (await s.execute(text(
                "SELECT version_id, effective_date FROM rag_document_versions WHERE document_id = :d AND is_current"),
                {"d": document_id})).first()
            becomes_current = current is None or (doc.effective_date or date.max) >= (current.effective_date or date.min)
            if current is not None and not becomes_current:
                status = "historical_version"
            version_no = (await s.execute(text(
                "SELECT COALESCE(max(version_no), 0) + 1 FROM rag_document_versions WHERE document_id = :d"),
                {"d": document_id})).scalar_one()
            if becomes_current and current is not None:
                await s.execute(text("UPDATE rag_document_versions SET is_current = false WHERE version_id = :v"),
                                {"v": current.version_id})
                await s.execute(text("UPDATE rag_chunks SET is_current = false WHERE version_id = :v"),
                                {"v": current.version_id})
            version_id = uuid4()
            await s.execute(text(
                """INSERT INTO rag_document_versions (version_id, document_id, version_no, declared_version, content_sha256,
                       effective_date, metadata, is_current)
                   VALUES (:v, :d, :n, :dv, :h, :ed, CAST(:m AS JSONB), :cur)"""),
                {"v": version_id, "d": document_id, "n": version_no, "dv": doc.declared_version, "h": content_hash,
                 "ed": doc.effective_date, "m": json.dumps({**doc.metadata, "path": doc.path.name}),
                 "cur": becomes_current})
            chunks = chunk_text(doc.text)
            await s.execute(text(
                """INSERT INTO rag_chunks (chunk_id, version_id, document_id, tenant_id, chunk_index, section_path, content,
                       token_count, chunk_sha256, is_current, metadata)
                   VALUES (:id, :v, :d, :t, :i, :sp, :c, :tc, :h, :cur, CAST(:m AS JSONB))"""),
                [{"id": uuid4(), "v": version_id, "d": document_id, "t": doc.tenant_id, "i": ch.index,
                  "sp": ch.section_path, "c": ch.content, "tc": ch.token_count, "h": sha256_hex(ch.content),
                  "cur": becomes_current,
                  "m": json.dumps({"doc_type": doc.doc_type, "title": doc.title, "version": doc.declared_version})}
                 for ch in chunks],
            )
            embedded = await _embed_missing(s, version_id, profiles)
            return IngestResult(doc.source_uri, doc.tenant_id, status, version_no, len(chunks), embedded)
    finally:
        reset_context(token)


async def ingest_path(root: Path, profiles: list[str]) -> list[IngestResult]:
    docs = [load_document(p, root) for p in discover(root)]
    # Shallower paths first (so a canonical file wins over a copy in a sub-folder when duplicate
    # content is detected); within a document, oldest effective date first so the newest version
    # ends up current.
    docs.sort(key=lambda d: (len(d.path.relative_to(root).parts), d.tenant_id or "", d.source_uri,
                             d.effective_date or date.min, str(d.path)))
    results = []
    for doc in docs:
        result = await ingest_document(doc, profiles)
        log.info("rag_ingested", source=doc.source_uri, tenant=doc.tenant_id, status=result.status,
                 chunks=result.chunks, embedded=result.embedded)
        results.append(result)
    return results
