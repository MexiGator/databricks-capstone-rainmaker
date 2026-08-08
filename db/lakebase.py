"""
Rainmaker -- Lakebase connection helper.

Same pattern as Homework 1 and 2: the full Postgres URL (password included)
lives in a Databricks secret, never in code.

    scope: database   key: lakebase-url

Locally / in a notebook:
    import os; os.environ["LAKEBASE_URL"] = "postgresql://..."
"""

from __future__ import annotations

import os
import pathlib
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")


def _url() -> str:
    url = os.environ.get("LAKEBASE_URL")
    if not url:
        raise RuntimeError(
            "LAKEBASE_URL is not set. In the Databricks App, add a Secret "
            "resource with Resource key 'lakebase-url' matching valueFrom in "
            "app.yaml. In a notebook, set os.environ['LAKEBASE_URL'] first."
        )
    return url


@contextmanager
def connect(dict_rows: bool = False):
    """Yield a connection; commit on success, roll back on any exception."""
    conn = psycopg2.connect(_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def cursor(dict_rows: bool = False):
    """Yield a cursor. dict_rows=True returns dicts instead of tuples."""
    with connect() as conn:
        factory = RealDictCursor if dict_rows else None
        with conn.cursor(cursor_factory=factory) as cur:
            yield cur


def ensure_vector_extension() -> None:
    """
    Enable pgvector in its own transaction.

    Kept separate on purpose: if CREATE EXTENSION fails inside the main DDL,
    Postgres aborts the whole transaction and every table silently fails to
    appear. Failing here, loudly, with the fix in the message, is better.
    """
    try:
        with cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except psycopg2.Error as exc:
        raise RuntimeError(
            "Could not enable pgvector on this Lakebase instance. Turn on "
            "'Enable Lakebase Search' in the instance settings, then re-run. "
            f"Postgres said: {exc}"
        ) from exc


def ensure_schema() -> None:
    """Create every table/index/trigger. Idempotent -- safe to re-run."""
    ensure_vector_extension()
    ddl = SCHEMA_PATH.read_text()
    with cursor() as cur:
        cur.execute(ddl)
    print("Rainmaker schema ready.")


def table_counts() -> dict[str, int]:
    """Quick sanity check after seeding."""
    tables = [
        "customers",
        "event_service_map",
        "outreach_templates",
        "weather_events",
        "opportunities",
        "outreach",
        "inbound_replies",
        "bookings",
    ]
    counts: dict[str, int] = {}
    with cursor() as cur:
        for t in tables:
            cur.execute(f"SELECT count(*) FROM {t}")
            counts[t] = cur.fetchone()[0]
    return counts


if __name__ == "__main__":
    ensure_schema()
    for name, n in table_counts().items():
        print(f"  {name:<22} {n:>6}")
