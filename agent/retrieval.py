"""
Rainmaker -- retrieval (the RAG half of RAG).

Search the unified corpus index, return chunks with similarity scores, and
REFUSE when nothing clears the floor. The refusal is not a nicety: an
ungrounded answer that looks confident is the failure mode judges look for.

Similarity here is cosine similarity in 0..1 (1 = identical), derived from
pgvector's `<=>` cosine DISTANCE operator as 1 - distance.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

import os as _os
import sys as _sys

# Resolve paths from THIS file, not the working directory -- the app, the
# notebook, and pytest all run from different cwds.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "db"), _os.path.join(_ROOT, "pipeline")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import lakebase

# Below this, we say we don't know rather than grounding on noise.
# Tuned for all-MiniLM-L6-v2, where unrelated text typically lands under 0.25.
SIMILARITY_FLOOR = 0.35

DEFAULT_TOP_K = 5


@dataclass
class Chunk:
    """One retrieved passage plus everything the grounding panel renders."""

    source_type: str
    source_id: str
    title: str
    chunk_text: str
    similarity: float
    metadata: dict = field(default_factory=dict)

    def as_source(self) -> dict:
        """Shape the UI expects under each answer."""
        return {
            "title": self.title,
            "source_type": self.source_type,
            "similarity": round(self.similarity, 3),
            "chunk": self.chunk_text,
            "metadata": self.metadata,
        }


def search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    source_type: str | None = None,
    floor: float = SIMILARITY_FLOOR,
) -> list[Chunk]:
    """
    Embed the query with the SAME model used at ingestion, then cosine-rank.

    source_type=None searches both corpora at once -- that is what makes the
    headline Ask query ("who do I prioritise and what do I say") work.
    """
    import embed_corpus

    vector = embed_corpus.embed([query])[0]

    sql = """
        SELECT source_type, source_id, title, chunk_text, metadata,
               1 - (embedding <=> %s::vector) AS similarity
        FROM corpus_embeddings
        {where}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    where = "WHERE source_type = %s" if source_type else ""
    sql = sql.format(where=where)

    params: list = [str(vector)]
    if source_type:
        params.append(source_type)
    params += [str(vector), top_k]

    with lakebase.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    chunks = [
        Chunk(
            source_type=r[0],
            source_id=r[1],
            title=r[2] or r[1],
            chunk_text=r[3],
            metadata=r[4] if isinstance(r[4], dict) else json.loads(r[4] or "{}"),
            similarity=float(r[5]),
        )
        for r in rows
    ]
    return [c for c in chunks if c.similarity >= floor]


def best_template(event_type: str, service_type: str, floor: float = SIMILARITY_FLOOR):
    """
    Find the campaign most likely to book, for this hazard and service line.

    Retrieval, not a WHERE clause, on purpose: NWS event names drift
    ("Extreme Heat Warning" replaced "Excessive Heat Warning" in 2025), and
    an exact-match lookup would return nothing the day that happens.
    Semantic search degrades gracefully where equality does not.
    """
    query = f"{event_type} outreach campaign for {service_type} customers"
    hits = search(query, top_k=3, source_type="template", floor=floor)
    if not hits:
        return None

    # Among semantically close campaigns, prefer the one that actually booked.
    return max(
        hits,
        key=lambda c: (c.similarity * 0.6) + (float(c.metadata.get("past_booked_rate", 0)) * 0.4),
    )


def format_context(chunks: list[Chunk], max_chars: int = 4000) -> str:
    """Assemble retrieved chunks into the context block for the prompt."""
    parts: list[str] = []
    used = 0
    for i, c in enumerate(chunks, 1):
        block = f"[{i}] {c.title} (similarity {c.similarity:.2f})\n{c.chunk_text}"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


REFUSAL = (
    "I don't have a strong match in the indexed weather alerts or past campaigns "
    "for that question, so I'd rather not guess. Try rephrasing, or check whether "
    "the relevant storm has been polled yet."
)


def ask(query: str, top_k: int = DEFAULT_TOP_K, source_type: str | None = None) -> dict:
    """
    The /ask endpoint's core: retrieve, then generate grounded in what was
    retrieved. Returns {answer, sources, grounded}.

    When retrieval comes back empty we return the refusal WITHOUT calling the
    model at all. Handing an LLM an empty context and hoping it declines is
    not a guardrail.
    """
    chunks = search(query, top_k=top_k, source_type=source_type)
    if not chunks:
        return {"answer": REFUSAL, "sources": [], "grounded": False}

    context = format_context(chunks)
    prompt = (
        "You are an operations assistant for a home-services company.\n"
        "Answer the question using ONLY the retrieved context below. Cite the "
        "numbered sources you used, like [1] or [2]. If the context does not "
        "contain the answer, say so plainly rather than inferring.\n\n"
        f"RETRIEVED CONTEXT:\n{context}\n\n"
        f"QUESTION: {query}\n\nANSWER:"
    )

    from agent import llm

    answer = llm.complete(prompt)
    return {
        "answer": answer,
        "sources": [c.as_source() for c in chunks],
        "grounded": True,
    }
