"""
Rainmaker -- inbound reply simulator.

Stands in for real Twilio inbound SMS during the demo. Called explicitly from
the Send action, NOT from an always-on stream: Free Edition scales to zero and
auto-stops streams, so a background listener would be dead by demo time.

SEEDED, not random. The intent a given opportunity produces is derived from a
hash of its id, so the same opportunity always yields the same reply. Take
three of the demo behaves exactly like take one.

Mix: ~60% interested / 25% question / 15% not_now -- roughly what a
well-targeted post-storm campaign actually returns.
"""

from __future__ import annotations

import hashlib
import os as _os
import sys as _sys

# Resolve paths from THIS file, not the working directory -- the app, the
# notebook, and pytest all run from different cwds.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "db"), _os.path.join(_ROOT, "pipeline")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)


def _db():
    """Imported lazily so `reply_for` -- which is pure -- can be tested
    without psycopg2 installed."""
    import lakebase

    return lakebase

# Weighted buckets summing to 100. Order matters for the hash mapping.
INTENT_MIX: list[tuple[str, int]] = [
    ("interested", 60),
    ("question", 25),
    ("not_now", 15),
]

REPLY_POOL: dict[str, list[str]] = {
    "interested": [
        "Yes please, when can someone come out?",
        "Yeah we noticed a couple of shingles in the yard. Book us in.",
        "Sure, earliest slot works for us.",
        "Yes. There's a water stain on the upstairs ceiling I want looked at.",
        "Please schedule an inspection, we're home most afternoons.",
        "Sounds good, let's do it.",
        "Yes, and how soon can you get here?",
        "OK book it. We'd rather know now than in six months.",
    ],
    "question": [
        "How much does the inspection cost?",
        "Is this covered by insurance or do I pay out of pocket?",
        "What does the inspection actually involve?",
        "Who is this? I don't remember signing up for texts.",
        "How long does it take?",
        "Do I need to be home for it?",
    ],
    "not_now": [
        "No thanks, we're all set.",
        "Already had someone out last week.",
        "Not right now, maybe next spring.",
        "Not interested.",
        "We're travelling, try us next month.",
    ],
}


def _bucket(seed: str) -> str:
    """Map a stable hash into the weighted intent mix."""
    n = int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16) % 100
    running = 0
    for intent, weight in INTENT_MIX:
        running += weight
        if n < running:
            return intent
    return INTENT_MIX[-1][0]


def reply_for(opportunity_id: str) -> tuple[str, str]:
    """
    Return (intent, reply_text) for an opportunity. Pure and deterministic --
    same input, same output, forever. Tested directly.
    """
    intent = _bucket(opportunity_id)
    pool = REPLY_POOL[intent]
    idx = int(hashlib.sha1(f"{opportunity_id}|text".encode()).hexdigest()[:8], 16) % len(pool)
    return intent, pool[idx]


def simulate_for_opportunity(opportunity_id: str) -> dict | None:
    """
    Write one inbound reply for an opportunity that has been sent.

    Returns None if the opportunity isn't in 'sent' state or already has a
    reply -- so clicking Send twice cannot manufacture two customers.
    """
    with _db().cursor(dict_rows=True) as cur:
        cur.execute(
            """
            SELECT o.opportunity_id, o.status,
                   (SELECT count(*) FROM inbound_replies r
                     WHERE r.opportunity_id = o.opportunity_id) AS reply_count
            FROM opportunities o
            WHERE o.opportunity_id = %s
            """,
            (opportunity_id,),
        )
        row = cur.fetchone()

    if not row or row["status"] != "sent" or row["reply_count"] > 0:
        return None

    intent, text = reply_for(opportunity_id)

    with _db().cursor() as cur:
        cur.execute(
            """
            INSERT INTO inbound_replies (opportunity_id, reply_text, is_simulated)
            VALUES (%s, %s, TRUE)
            RETURNING reply_id
            """,
            (opportunity_id, text),
        )
        reply_id = cur.fetchone()[0]

    return {
        "reply_id": reply_id,
        "opportunity_id": opportunity_id,
        "reply_text": text,
        "seeded_intent": intent,
    }


def simulate_all_sent() -> list[dict]:
    """Generate replies for every sent opportunity that hasn't answered yet.
    Backs the 'Simulate replies' button."""
    with _db().cursor() as cur:
        cur.execute(
            """
            SELECT o.opportunity_id
            FROM opportunities o
            LEFT JOIN inbound_replies r ON r.opportunity_id = o.opportunity_id
            WHERE o.status = 'sent' AND r.reply_id IS NULL
            """
        )
        ids = [r[0] for r in cur.fetchall()]

    return [rep for rep in (simulate_for_opportunity(i) for i in ids) if rep]
