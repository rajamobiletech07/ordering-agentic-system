from pathlib import Path

from ordering.rag.answerer import INSUFFICIENT, extractive_answer
from ordering.rag.chunker import chunk_text
from ordering.rag.embeddings.hashing import hashing_vector
from ordering.rag.loaders import load_document
from ordering.rag.retriever import RetrievedChunk, keyword_query

ROOT = Path(__file__).resolve().parents[2] / "data" / "rag_docs"


def test_loader_parses_front_matter_and_tenant():
    doc = load_document(ROOT / "acme-retail" / "acme-ordering-addendum.md", ROOT)
    assert doc.tenant_id == "acme-retail" and doc.doc_type == "ordering_policy" and doc.declared_version == "1.2"
    shared = load_document(ROOT / "shared" / "ordering-policy.md", ROOT)
    assert shared.tenant_id is None and shared.source_uri == "policies/ordering-policy.md"
    v1 = load_document(ROOT / "_archive" / "ordering-policy-v1.md", ROOT)
    assert v1.source_uri == shared.source_uri and v1.effective_date < shared.effective_date


def test_heading_aware_chunks_carry_section_paths():
    text = "# Policy\n\n## Payments\n\n" + "\n\n".join(f"- rule number {i} " + "word " * 40 for i in range(12))
    chunks = chunk_text(text, max_tokens=120)
    assert len(chunks) > 1
    assert all(c.section_path == "Policy > Payments" for c in chunks)
    assert all(c.token_count <= 200 for c in chunks)
    # overlap (~15%): with small blocks, the last block of a chunk is repeated at the start of the next
    small = chunk_text("# P\n\n" + "\n\n".join(f"- item {i} alpha beta" for i in range(40)), max_tokens=60)
    assert len(small) > 2
    assert small[0].content.split("\n\n")[-1] == small[1].content.split("\n\n")[0]


def test_hashing_embeddings_are_deterministic_and_lexical():
    a = hashing_vector("payment declined retry")
    assert a == hashing_vector("payment declined retry")

    def cos(x, y):
        return sum(i * j for i, j in zip(x, y, strict=True))

    assert cos(a, hashing_vector("what if a payment is declined")) > cos(a, hashing_vector("store receiving hours"))


def test_keyword_query_drops_stopwords():
    assert keyword_query("What is the approval threshold for orders?") == "approval | threshold | orders"
    assert keyword_query("what is the") is None


def test_extractive_answer_cites_or_refuses():
    chunk = RetrievedChunk("c1", "d1", "Supplier Payment Policy", "shared/payment-policy.md", "payment_policy", None, "1.3",
                           "Failed payments", "When a payment is declined, the order is marked payment failed and no "
                           "goods are released.")
    ans = extractive_answer("What happens when a payment is declined?", [chunk])
    assert "[1]" in ans.answer and ans.cited_refs == [1]
    assert extractive_answer("quantum chromodynamics", [chunk]).answer == INSUFFICIENT
