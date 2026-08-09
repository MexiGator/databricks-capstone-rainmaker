"""relationship_v0.db — Lakebase (Postgres) adapter for the v0.1 tables.

Thin, psycopg2-only persistence layer (NO Spark JDBC — serverless can't write to
Lakebase, per the Day 2 lecson). Reuses the SAME connection pattern as the
existing `lakebase.py`: a LAKEBASE_URL and a psycopg2 connection. Import the
repo's existing connection helper rather than re-implementing it.

>>> CLAUDE CODE: wire the two TODOs to the real repo's lakebase.py, then this
    module is complete. Everything else here is final.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from relationship_v0.scoring import ContactSignals, compute_relationship_score
from relationship_v0 import care_content


# --- connection (reuse the existing helper) --------------------------------- #
def _connect():
    """Reuse the repo's existing Lakebase helper -- same LAKEBASE_URL, same
    psycopg2 path, same commit/rollback/close semantics as the graded app.
    `lakebase.connect()` is a contextmanager yielding a psycopg2 connection, so
    the `with _connect() as conn` call sites below are unchanged."""
    import os as _os
    import sys as _sys

    # Resolve lakebase.py from THIS file, not cwd: relationship_v0/ sits at the
    # repo root, so db/ (where lakebase.py lives) is a sibling two levels up.
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    for _p in (_root, _os.path.join(_root, "db")):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)

    import lakebase
    return lakebase.connect()


def ensure_schema(sql_path: Optional[str] = None) -> None:
    """Run schema_relationship.sql (idempotent). Call on app boot, mirroring
    the existing lakebase.ensure_schema()."""
    sql_path = sql_path or os.path.join(os.path.dirname(__file__),
                                        "schema_relationship.sql")
    with open(sql_path) as f:
        ddl = f.read()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(ddl)
        conn.commit()


# --- relationship_score persistence ----------------------------------------- #
def upsert_relationship(contact_id: int, signals: ContactSignals,
                        tenant: Optional[str] = None) -> dict:
    """Score a contact and write/update its contact_relationship row."""
    result = compute_relationship_score(signals)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO contact_relationship
                (contact_id, tenant, relationship_score, tier, components,
                 consent_ok, opted_out, recent_care_touches, last_scored_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (contact_id) DO UPDATE SET
                relationship_score = EXCLUDED.relationship_score,
                tier               = EXCLUDED.tier,
                components         = EXCLUDED.components,
                consent_ok         = EXCLUDED.consent_ok,
                opted_out          = EXCLUDED.opted_out,
                recent_care_touches= EXCLUDED.recent_care_touches,
                last_scored_at     = now(),
                updated_at         = now();
            """,
            (contact_id, tenant, result["relationship_score"], result["tier"],
             json.dumps(result["components"]), signals.consent_ok, signals.opted_out,
             signals.recent_care_touches),
        )
        conn.commit()
    return result


def recompute_all(build_signals) -> int:
    """Recompute every contact's score. `build_signals(row)->ContactSignals`
    maps a joined CRM+engagement row to signals. Returns the count updated.

    The signal-gathering SQL is repo-specific (it joins customers +
    care_sends/outreach engagement), so it's injected rather than hard-coded.
    TODO(claude-code): provide the join query in the caller.
    """
    raise NotImplementedError(
        "Provide the customers-x-engagement join in the caller, then loop "
        "upsert_relationship(). See README_RELATIONSHIP.md 'Recompute'.")


def get_relationship(contact_id: int) -> Optional[dict]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT relationship_score, tier, components, consent_ok, opted_out, "
            "recent_care_touches FROM contact_relationship WHERE contact_id=%s",
            (contact_id,))
        row = cur.fetchone()
    if not row:
        return None
    return {"relationship_score": float(row[0]), "tier": row[1],
            "components": row[2], "consent_ok": row[3], "opted_out": row[4],
            "recent_care_touches": row[5]}


# --- care_content seeding + care_sends --------------------------------------- #
def seed_care_content() -> int:
    """Idempotently load CARE_GUIDES into care_content (embeddings added by the
    separate ingest step, mirroring ingest_weather_embeddings.py)."""
    with _connect() as conn, conn.cursor() as cur:
        for g in care_content.CARE_GUIDES:
            cur.execute(
                """
                INSERT INTO care_content (id, service_type, event_types, title,
                                          tips, guide_url, soft_cta)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    title=EXCLUDED.title, tips=EXCLUDED.tips,
                    guide_url=EXCLUDED.guide_url, soft_cta=EXCLUDED.soft_cta;
                """,
                (g["id"], g["service_type"], g["event_types"], g["title"],
                 json.dumps(g["tips"]), g["guide_url"], g["soft_cta"]),
            )
        conn.commit()
    return len(care_content.CARE_GUIDES)


def insert_care_send(row: dict) -> int:
    """Persist one queued care send; returns care_send_id."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO care_sends (contact_id, tenant, event_id, event_type,
                service_type, guide_id, template_kind, cta_strength, channel,
                message_text, status)
            VALUES (%(contact_id)s, %(tenant)s, %(event_id)s, %(event_type)s,
                %(service_type)s, %(guide_id)s, %(template_kind)s, %(cta_strength)s,
                %(channel)s, %(message_text)s, %(status)s)
            RETURNING care_send_id;
            """,
            {**{"tenant": None, "event_id": None, "channel": "sms",
                "status": "queued"}, **row},
        )
        new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


def advance_care_send(care_send_id: int, status: str,
                      **fields) -> None:
    """Advance a care_send's status (queued->approved->sent->replied->booked...).
    Every status write is what CDF captures for the Results funnel."""
    sets = ["status=%s", "updated_at=now()"]
    vals = [status]
    for k, v in fields.items():
        sets.append(f"{k}=%s")
        vals.append(v)
    vals.append(care_send_id)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE care_sends SET {', '.join(sets)} WHERE care_send_id=%s",
                    vals)
        conn.commit()
