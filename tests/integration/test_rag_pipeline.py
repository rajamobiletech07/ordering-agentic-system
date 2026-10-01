"""RAG: idempotent ingestion, versioning, duplicate detection, tenant filtering, eval gate."""

from pathlib import Path

import pytest
from sqlalchemy import text

from ordering.common.db import admin_session
from ordering.rag.eval import evaluate
from ordering.rag.ingest import ingest_path
from ordering.rag.retriever import retrieve

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


async def test_ingestion_is_idempotent_with_versions_and_duplicates():
    results = await ingest_path(ROOT / "data" / "rag_docs", ["hashing"])
    statuses = {r.source_uri: r.status for r in results if r.source_uri != "policies/ordering-policy.md"}
    assert set(statuses.values()) <= {"unchanged", "duplicate", "created"}
    assert statuses["shared/duplicates/vendor-rules-copy.md"] == "duplicate"
    async with admin_session() as s:
        versions = (await s.execute(text(
            """SELECT v.declared_version, v.is_current FROM rag_document_versions v JOIN rag_documents d USING (document_id)
               WHERE d.source_uri = 'policies/ordering-policy.md' ORDER BY v.version_no"""))).all()
    assert [(v.declared_version, v.is_current) for v in versions] == [("1.0", False), ("2.0", True)]


async def test_only_current_version_is_retrieved():
    result = await retrieve("approval threshold manager signature 25,000", "acme-retail", profile="hashing")
    policy_chunks = [c for c in result.chunks if c.source_uri == "policies/ordering-policy.md"]
    assert policy_chunks and all(c.version == "2.0" for c in policy_chunks)


async def test_tenant_documents_never_leak():
    result = await retrieve("Globex organic sourcing dairy", "acme-retail", profile="hashing")
    assert all(c.tenant_id in (None, "acme-retail") for c in result.chunks)


async def test_retrieval_eval_meets_thresholds(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROFILE", "hashing")
    from ordering.common.config import get_settings
    get_settings.cache_clear()
    report = await evaluate(ROOT / "data" / "rag_eval" / "eval_set.jsonl", k=5)
    assert report.tenant_leaks == 0
    assert report.recall_at_k >= 0.9 and report.mrr >= 0.8
    assert report.refusal_accuracy >= 0.8, report.failures
