"""
Rainmaker v0.1 -- embed the Proactive Care corpus.

A direct clone of embed_corpus.py, pointed at the `care_content` table instead
of corpus_embeddings. Same model (all-MiniLM-L6-v2, 384 dims), same /tmp HF
cache + psycopg2 gotchas, so the care-guide embeddings are comparable with the
weather/template embeddings the Ask bar already searches. Do not swap the model.

This is v0.1 (relationship layer) and is INERT unless you run it -- it only
writes to the new care_content table, never to any graded table.

Run (once care_content is seeded via relationship_v0.db.seed_care_content()):
    import os; os.environ["LAKEBASE_URL"] = "..."
    import embed_care_content; embed_care_content.run()

run() will seed first if the table is empty, so a single call is enough.
"""

from __future__ import annotations

import os
import os as _os
import sys as _sys

# Resolve paths from THIS file, not the working directory -- the app, the
# notebook, and pytest all run from different cwds. _ROOT is the repo root,
# which holds db/ (lakebase.py) and relationship_v0/.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "db"), _os.path.join(_ROOT, "pipeline")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

# Serverless Databricks has a read-only HOME. Without this the model download
# fails with a permissions error that looks nothing like the real cause.
os.environ.setdefault("HF_HOME", "/tmp/hf")
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", "/tmp/hf")

import lakebase

MODEL_NAME = "all-MiniLM-L6-v2"
EMBED_DIM = 384

_model = None


def get_model():
    """Lazy-load so importing this module (e.g. in tests) costs nothing."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        print(f"Loading {MODEL_NAME} (first run downloads ~90MB to /tmp/hf)...")
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed(texts: list[str]) -> list[list[float]]:
    """Encode to unit-normalised vectors so cosine distance behaves -- exactly
    as embed_corpus.embed does, so the two corpora share a vector space."""
    model = get_model()
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vectors]


# ---------------------------------------------------------------------
# Corpus text -- pure, no model, easy to reason about
# ---------------------------------------------------------------------
def guide_text(row: dict) -> str:
    """The text we embed for one care guide: the title plus its tips. The guide
    is short and self-contained (unlike the weather narratives), so it embeds as
    a single chunk -- the Ask bar retrieves the whole guide, not a fragment."""
    tips = row.get("tips") or []
    if isinstance(tips, str):  # defensive: some drivers hand back raw JSON text
        import json
        tips = json.loads(tips)
    body = " ".join(str(t).strip() for t in tips if str(t).strip())
    return f"{row['title']}. {body}".strip()


def _fetch_care_rows() -> list[dict]:
    with lakebase.cursor(dict_rows=True) as cur:
        cur.execute("""
            SELECT id, service_type, event_types, title, tips
            FROM care_content
        """)
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------
def run(batch_size: int = 32) -> int:
    """Seed care_content if empty, then embed every guide into care_content.embedding.
    Returns the number of guides embedded."""
    # Seed first so a single call is enough (mirrors the "seed + embed" step in
    # the brief). seed_care_content is idempotent (ON CONFLICT DO UPDATE).
    from relationship_v0 import db as rel_db
    seeded = rel_db.seed_care_content()
    print(f"care_content seeded/confirmed: {seeded} guides.")

    rows = _fetch_care_rows()
    if not rows:
        print("Nothing to embed -- care_content is empty after seeding.")
        return 0

    print(f"Embedding {len(rows)} care guides...")
    written = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        vectors = embed([guide_text(r) for r in batch])
        with lakebase.cursor() as cur:
            for r, vec in zip(batch, vectors):
                cur.execute(
                    "UPDATE care_content SET embedding = %s WHERE id = %s",
                    (str(vec), r["id"]),
                )
        written += len(batch)
        print(f"  {written}/{len(rows)}")

    with lakebase.cursor() as cur:
        cur.execute("SELECT count(*) FROM care_content WHERE embedding IS NOT NULL")
        print(f"  care_content embedded: {cur.fetchone()[0]}/{len(rows)}")

    return written


if __name__ == "__main__":
    run()
