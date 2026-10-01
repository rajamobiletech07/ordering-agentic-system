"""Grounded answering with citations and explicit insufficient-evidence handling."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ordering.agents.guardrails import sanitize_untrusted
from ordering.common.logging import get_logger
from ordering.domain.schemas.chat import Citation
from ordering.prompts import registry
from ordering.rag.retriever import RetrievalResult, RetrievedChunk, keyword_query, retrieve

log = get_logger("ordering.rag.answerer")

INSUFFICIENT = (
    "I couldn't find enough evidence in the policy and operations documents available to you to answer that "
    "reliably, so I won't guess. Try rephrasing with specific terms (e.g. vendor, payment, approval threshold), "
    "or contact Procurement Operations."
)


class GroundedAnswer(BaseModel):
    answer: str = Field(..., description="answer text with [n] citations")
    cited_refs: list[int] = Field(default_factory=list)
    insufficient_evidence: bool = False


@dataclass
class AnswerResult:
    answer: str
    citations: list[Citation] = field(default_factory=list)
    sufficient: bool = True
    degraded: bool = False
    prompt_ref: str | None = None
    stripped_lines: list[str] = field(default_factory=list)
    retrieval: RetrievalResult | None = None


def _sentences(text: str) -> list[str]:
    text = re.sub(r"[*`#]", "", text)
    # unwrap hard-wrapped lines (keep list items and paragraph breaks)
    text = re.sub(r"(?<!\n)\n(?!\n|\s*[-*]\s|\s*\d+\.\s)", " ", text)
    parts = re.split(r"(?<=[.!?])\s+|\n+-\s+|\n+", text)
    return [p.strip(" -") for p in parts if len(p.strip()) > 20]


def extractive_answer(question: str, chunks: list[RetrievedChunk]) -> GroundedAnswer:
    """Deterministic grounded answer: best-matching sentences from the top sources, cited."""
    terms = set((keyword_query(question) or "").split(" | "))
    picked: list[tuple[int, str]] = []
    best = 0
    for ref, chunk in enumerate(chunks[:3], start=1):
        scored = sorted(((len(terms & set(re.findall(r"[a-z0-9]+", s.lower()))), s) for s in _sentences(chunk.content)),
                        key=lambda x: x[0], reverse=True)
        if not scored or scored[0][0] == 0:
            continue
        overlap, sentence = scored[0]
        # a supporting sentence must be about as relevant as the lead sentence
        if picked and overlap < max(2, 0.75 * best):
            continue
        best = max(best, overlap)
        picked.append((ref, sentence))
        if len(picked) == 2:
            break
    if not picked:
        return GroundedAnswer(answer=INSUFFICIENT, insufficient_evidence=True)
    lead = chunks[picked[0][0] - 1]
    body = " ".join(f"{s.rstrip('.')}. [{ref}]" for ref, s in picked)
    answer = f"According to {lead.title}{f' (v{lead.version})' if lead.version else ''}: {body}"
    return GroundedAnswer(answer=answer, cited_refs=[r for r, _ in picked])


def _citation(ref: int, chunk: RetrievedChunk) -> Citation:
    return Citation(ref=ref, document=chunk.title, version=chunk.version, section=chunk.section_path or None,
                    chunk_id=chunk.chunk_id, source_uri=chunk.source_uri, score=round(chunk.score, 4))


async def answer_question(question: str, tenant_id: str, cache=None, sticky_key: str | None = None) -> AnswerResult:
    from ordering.llm_client.client import get_llm_client

    retrieval = await retrieve(question, tenant_id)
    if not retrieval.sufficient:
        log.info("rag_insufficient_evidence", query=question[:120], candidates=len(retrieval.chunks))
        return AnswerResult(INSUFFICIENT, [], sufficient=False, degraded=retrieval.degraded, retrieval=retrieval)

    chunks = retrieval.chunks
    stripped: list[str] = []
    blocks = []
    for ref, chunk in enumerate(chunks, start=1):
        clean, removed = sanitize_untrusted(chunk.content)
        stripped += removed
        chunk.content = clean
        blocks.append(f"[{ref}] {chunk.title} (v{chunk.version or '?'}) - {chunk.section_path}\n{clean}")

    prompt = await registry.resolve("rag_answerer", sticky_key)
    system, user = prompt.render(question=question, sources="\n\n".join(blocks))
    fallback = extractive_answer(question, chunks)
    result = await get_llm_client().complete(prompt.route, system=system, user=user, schema=GroundedAnswer,
                                             fallback=fallback, prompt_id=prompt.prompt_id,
                                             prompt_version=prompt.version, cache=cache)
    answer: GroundedAnswer = result.parsed
    if answer.insufficient_evidence:
        return AnswerResult(INSUFFICIENT, [], sufficient=False, degraded=result.degraded, prompt_ref=prompt.ref,
                            stripped_lines=stripped, retrieval=retrieval)

    # Citation validation: keep only references that exist; refuse uncited policy claims.
    inline = {int(n) for n in re.findall(r"\[(\d+)\]", answer.answer)}
    valid = sorted(r for r in (set(answer.cited_refs) | inline) if 1 <= r <= len(chunks))
    if not valid:
        log.warning("rag_answer_uncited_falling_back", provider=result.provider)
        answer, valid = fallback, fallback.cited_refs
        if fallback.insufficient_evidence:
            return AnswerResult(INSUFFICIENT, [], sufficient=False, degraded=True, prompt_ref=prompt.ref,
                                retrieval=retrieval)
    text = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(chunks) else "", answer.answer)
    return AnswerResult(text, [_citation(r, chunks[r - 1]) for r in valid], True,
                        degraded=result.degraded or retrieval.degraded, prompt_ref=prompt.ref,
                        stripped_lines=stripped, retrieval=retrieval)
