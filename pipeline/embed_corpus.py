"""
Rainmaker -- build the vector index.

Chunks and embeds BOTH text corpora into corpus_embeddings:
  * weather_events.narrative_text   -- real NWS hazard + safety prose
  * outreach_templates.message_text -- past campaign copy with booked rates

Same model as Homework 2 (all-MiniLM-L6-v2, 384 dims). Do not swap it:
embeddings from different models are not comparable, and the failure is
silent -- you get plausible-looking similarity scores that mean nothing.

Run:
    import os; os.environ["LAKEBASE_URL"] = "..."
    import embed_corpus; embed_corpus.run()
"""

from __future__ import annotations

import json
import os
import os as _os
import sys as _sys

# Resolve paths from THIS file, not the working directory -- the app, the
# notebook, and pytest all run from different cwds.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "db"), _os.path.join(_ROOT, "pipeline")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

# Serverless Databricks has a read-only HOME. Without this the model download
# fails with a permissions error that looks nothing like the real cause.
os.environ.setdefault("HF_HOME", "/tmp/hf")
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", "/tmp/hf")

from psycopg2.extras import execute_values

import lakebase

MODEL_NAME = "all-MiniLM-L6-v2"
EMBED_DIM = 384

# ~2-3 sentences per chunk. Small enough that a retrieved chunk is specific,
# large enough that it still carries context on its own.
CHUNK_CHARS = 450
CHUNK_OVERLAP = 80

_model = None


def get_model():
    """Lazy-load so importing this module (e.g. in tests) costs nothing."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        print(f"Loading {MODEL_NAME} (first run downloads ~90MB to /tmp/hf)...")
        _model = SentenceTransformer(MODEL_NAME)
    return _model


# ---------------------------------------------------------------------
# Chunking -- pure, no model, fully testable
# ---------------------------------------------------------------------
def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split on paragraph boundaries first, then hard-wrap anything still too
    long, with a small overlap so a sentence straddling a boundary is not
    lost to both chunks.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buffer = ""

    for para in paragraphs:
        if len(buffer) + len(para) + 2 <= size:
            buffer = f"{buffer}\n\n{para}".strip()
            continue
        if buffer:
            chunks.append(buffer)
            buffer = ""
        if len(para) <= size:
            buffer = para
        else:
            step = max(size - overlap, 1)
            for start in range(0, len(para), step):
                piece = para[start : start + size].strip()
                if piece:
                    chunks.append(piece)
    if buffer:
        chunks.append(buffer)
    return chunks


def embed(texts: list[str]) -> list[list[float]]:
    """Encode to unit-normalised vectors so cosine distance behaves."""
    model = get_model()
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vectors]


# ---------------------------------------------------------------------
# Load source text
# ---------------------------------------------------------------------
def _fetch_weather_rows() -> list[dict]:
    with lakebase.cursor(dict_rows=True) as cur:
        cur.execute("""
            SELECT event_id, event_type, headline, area_desc, state, severity, narrative_text
            FROM weather_events
            WHERE narrative_text IS NOT NULL AND length(narrative_text) > 40
        """)
        return [dict(r) for r in cur.fetchall()]


def _fetch_template_rows() -> list[dict]:
    with lakebase.cursor(dict_rows=True) as cur:
        cur.execute("""
            SELECT template_id, campaign_name, event_type, service_type,
                   message_text, past_booked_rate, sends
            FROM outreach_templates
        """)
        return [dict(r) for r in cur.fetchall()]


def build_records() -> list[tuple]:
    """Assemble (source_type, source_id, chunk_index, chunk_text, title, metadata)."""
    records: list[tuple] = []

    for row in _fetch_weather_rows():
        title = row["headline"] or f"{row['event_type']} — {row['area_desc']}"
        meta = {
            "event_type": row["event_type"],
            "severity": row["severity"],
            "state": row["state"],
            "area_desc": row["area_desc"],
        }
        for i, chunk in enumerate(chunk_text(row["narrative_text"])):
            records.append(("weather", row["event_id"], i, chunk, title, json.dumps(meta)))

    for row in _fetch_template_rows():
        # Booked rate goes in the chunk text, not just metadata: the analyst
        # asking "what messaging works best" is asking about that number, so
        # it has to be inside what gets embedded and retrieved.
        header = (
            f"Campaign: {row['campaign_name']}. "
            f"Event: {row['event_type']}. Service: {row['service_type']}. "
            f"Booked rate: {float(row['past_booked_rate']) * 100:.0f}% over {row['sends']} sends."
        )
        body = f"{header}\n\n{row['message_text']}"
        meta = {
            "campaign_name": row["campaign_name"],
            "event_type": row["event_type"],
            "service_type": row["service_type"],
            "past_booked_rate": float(row["past_booked_rate"]),
            "sends": row["sends"],
        }
        for i, chunk in enumerate(chunk_text(body)):
            records.append(
                ("template", row["template_id"], i, chunk, row["campaign_name"], json.dumps(meta))
            )

    return records


UPSERT_SQL = """
INSERT INTO corpus_embeddings
    (source_type, source_id, chunk_index, chunk_text, title, metadata, embedding)
VALUES %s
ON CONFLICT (source_type, source_id, chunk_index) DO UPDATE SET
    chunk_text = EXCLUDED.chunk_text,
    title      = EXCLUDED.title,
    metadata   = EXCLUDED.metadata,
    embedding  = EXCLUDED.embedding
"""


def run(batch_size: int = 64) -> int:
    records = build_records()
    if not records:
        print("Nothing to embed. Seed templates and poll weather first.")
        return 0

    print(f"Embedding {len(records)} chunks...")
    written = 0
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        vectors = embed([r[3] for r in batch])
        rows = [(*rec, str(vec)) for rec, vec in zip(batch, vectors)]
        with lakebase.cursor() as cur:
            execute_values(cur, UPSERT_SQL, rows)
        written += len(rows)
        print(f"  {written}/{len(records)}")

    with lakebase.cursor() as cur:
        cur.execute("SELECT source_type, count(*) FROM corpus_embeddings GROUP BY 1 ORDER BY 1")
        for source_type, n in cur.fetchall():
            print(f"  {source_type:<10} {n:>5} chunks")

    return written


if __name__ == "__main__":
    run()
