"""
Rainmaker v0.1 -- Proactive Care analytics rollup (Delta Change Data Feed).

A direct parallel of gold_rollup.py, but for `care_sends` instead of
`opportunities`. Same honest architecture: the app writes care sends to Lakebase
(the operational store); this job mirrors that state into a Delta table with CDF
enabled, reads the change feed, and rolls up:

  1. the care funnel: queued -> approved -> sent -> opened -> clicked -> replied -> booked
  2. the HEADLINE metric: booking rate of care-touched vs. non-care-touched
     contacts -- the proof that relationship engagement drives inspections.

Isolation: v0.1-only. It uses its OWN Delta table + its OWN checkpoint/audit
tables (care_cdf_checkpoint / care_cdf_audit) and reads the graded opportunities
/customers tables WITHOUT writing them. Nothing here runs unless you call it.

Run after a care demo pass (needs the v0.1 schema applied + some care_sends):
    import os; os.environ["LAKEBASE_URL"] = "..."
    import care_rollup; care_rollup.run()
"""

from __future__ import annotations

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "db"), _os.path.join(_ROOT, "pipeline")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import pandas as pd
from psycopg2.extras import execute_values
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

import lakebase

# Its own Delta table (separate from rainmaker_opportunity_state), carrying CDF.
DELTA_TABLE = "workspace.default.rainmaker_care_state"

# Cumulative funnel: a replied care send has also been sent and opened. The
# stages nest, mirroring gold_rollup's opportunity funnel.
APPROVED_STATES = ("approved", "sent", "opened", "clicked", "replied", "booked")
SENT_STATES = ("sent", "opened", "clicked", "replied", "booked")
OPENED_STATES = ("opened", "clicked", "replied", "booked")
CLICKED_STATES = ("clicked", "replied", "booked")
REPLIED_STATES = ("replied", "booked")
BOOKED_STATES = ("booked",)
# "Reached the customer" for the lift metric = sent or further along.
REACHED_STATES = SENT_STATES
# An opportunity counts as booked at these graded statuses.
OPP_BOOKED_STATES = ("booked", "quoted", "won", "completed")


# ---------------------------------------------------------------------
# 1-2. Sync Lakebase care_sends into Delta so the change feed sees it
# ---------------------------------------------------------------------
def sync_state_to_delta(spark: SparkSession) -> None:
    import warnings

    with lakebase.connect() as conn, warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")
        current = pd.read_sql(
            """
            SELECT care_send_id, contact_id, tenant, event_id, event_type,
                   service_type, template_kind, cta_strength, status, updated_at
            FROM care_sends
            """,
            conn,
        )

    if current.empty:
        raise RuntimeError(
            "No care_sends in Lakebase. Queue some proactive care first "
            "(the 4th agent tool / the Proactive Care panel)."
        )

    updates = spark.createDataFrame(current)
    updates.createOrReplaceTempView("care_incoming")

    if not spark.catalog.tableExists(DELTA_TABLE):
        (
            updates.write.format("delta")
            .option("delta.enableChangeDataFeed", "true")
            .saveAsTable(DELTA_TABLE)
        )
        spark.sql(
            f"ALTER TABLE {DELTA_TABLE} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)"
        )
        return

    # MERGE (not overwrite): only real status transitions become change records,
    # so the funnel is computed from genuine movement, not a full-table rewrite.
    spark.sql(f"""
        MERGE INTO {DELTA_TABLE} AS t
        USING care_incoming AS s
        ON t.care_send_id = s.care_send_id
        WHEN MATCHED AND t.status <> s.status THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)


# ---------------------------------------------------------------------
# 3. Read the change feed (own checkpoint, never touches cdf_checkpoint)
# ---------------------------------------------------------------------
def _checkpoint() -> int:
    with lakebase.cursor() as cur:
        cur.execute("SELECT last_version FROM care_cdf_checkpoint WHERE table_name = %s",
                    (DELTA_TABLE,))
        row = cur.fetchone()
        return row[0] if row else 0


def _save_checkpoint(version: int) -> None:
    with lakebase.cursor() as cur:
        cur.execute(
            """
            INSERT INTO care_cdf_checkpoint (table_name, last_version, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (table_name) DO UPDATE SET
                last_version = EXCLUDED.last_version, updated_at = now()
            """,
            (DELTA_TABLE, version),
        )


def read_changes(spark: SparkSession):
    """Everything new since the last care rollup. Returns (DataFrame, latest)."""
    start = _checkpoint()
    latest = spark.sql(f"DESCRIBE HISTORY {DELTA_TABLE}").agg(F.max("version")).collect()[0][0]
    if start >= latest:
        return None, latest
    changes = (
        spark.read.format("delta")
        .option("readChangeFeed", "true")
        .option("startingVersion", start + 1)
        .table(DELTA_TABLE)
    )
    return changes, latest


def push_audit(changes) -> int:
    """Land raw care-send change rows in Lakebase (care_cdf_audit) so the Results
    tab can show the actual feed: care sent -> opened -> replied -> booked."""
    rows = (
        changes.filter(F.col("_change_type").isin("insert", "update_postimage"))
        .select(
            "care_send_id",
            F.col("_change_type").alias("change_type"),
            F.col("status").alias("new_status"),
            F.col("_commit_version").alias("commit_version"),
            F.col("_commit_timestamp").alias("commit_ts"),
        )
        .toPandas()
    )
    if rows.empty:
        return 0
    payload = [
        (int(r.care_send_id), r.change_type, r.new_status, int(r.commit_version), r.commit_ts)
        for r in rows.itertuples()
    ]
    with lakebase.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO care_cdf_audit
                (care_send_id, change_type, new_status, commit_version, commit_ts)
            VALUES %s
            ON CONFLICT (care_send_id, commit_version, change_type) DO NOTHING
            """,
            payload,
        )
    return len(payload)


# ---------------------------------------------------------------------
# 4. Roll up the care funnel from the Delta state
# ---------------------------------------------------------------------
def _funnel_exprs():
    return [
        F.count("*").alias("queued"),
        F.count(F.when(F.col("status").isin(*APPROVED_STATES), 1)).alias("approved"),
        F.count(F.when(F.col("status").isin(*SENT_STATES), 1)).alias("sent"),
        F.count(F.when(F.col("status").isin(*OPENED_STATES), 1)).alias("opened"),
        F.count(F.when(F.col("status").isin(*CLICKED_STATES), 1)).alias("clicked"),
        F.count(F.when(F.col("status").isin(*REPLIED_STATES), 1)).alias("replied"),
        F.count(F.when(F.col("status").isin(*BOOKED_STATES), 1)).alias("booked"),
    ]


def care_funnel(spark: SparkSession) -> list[tuple]:
    state = spark.table(DELTA_TABLE)
    overall = state.agg(*_funnel_exprs()).toPandas().iloc[0]
    rows: list[tuple] = [(
        "overall", int(overall.queued), int(overall.approved), int(overall.sent),
        int(overall.opened), int(overall.clicked), int(overall.replied), int(overall.booked),
    )]
    for r in state.groupBy("event_type").agg(*_funnel_exprs()).toPandas().itertuples():
        rows.append((
            r.event_type or "(unknown)", int(r.queued), int(r.approved), int(r.sent),
            int(r.opened), int(r.clicked), int(r.replied), int(r.booked),
        ))
    return rows


def publish_funnel(rows: list[tuple]) -> None:
    with lakebase.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO care_gold_funnel
                (grain_value, queued, approved, sent, opened, clicked, replied, booked)
            VALUES %s
            ON CONFLICT (grain_value) DO UPDATE SET
                queued=EXCLUDED.queued, approved=EXCLUDED.approved, sent=EXCLUDED.sent,
                opened=EXCLUDED.opened, clicked=EXCLUDED.clicked, replied=EXCLUDED.replied,
                booked=EXCLUDED.booked, computed_at=now()
            """,
            rows,
        )


# ---------------------------------------------------------------------
# 5. The headline metric: care-touched vs. non-care-touched booking rate
# ---------------------------------------------------------------------
def compute_and_publish_lift() -> dict:
    """Attribution join across care_sends (v0.1) + opportunities + customers
    (graded, READ-only). A contact is 'care-touched' if a care send reached them
    (status sent+); 'booked' if they have an opportunity at a booked+ status.
    Computed in Postgres because it's a relational join; no writes to graded
    tables. Publishes one row into care_gold_lift.
    """
    reached = ", ".join("%s" for _ in REACHED_STATES)
    oppbk = ", ".join("%s" for _ in OPP_BOOKED_STATES)
    sql = f"""
        WITH touched AS (
            SELECT DISTINCT contact_id AS cid FROM care_sends
            WHERE status IN ({reached})
        ),
        booked AS (
            SELECT DISTINCT customer_id AS cid FROM opportunities
            WHERE status IN ({oppbk})
        ),
        pop AS (
            SELECT customer_id AS cid FROM customers WHERE status IN ('active','lead')
        )
        SELECT
          (SELECT count(*) FROM touched)                                            AS care_contacts,
          (SELECT count(*) FROM touched t WHERE t.cid IN (SELECT cid FROM booked))  AS care_booked,
          (SELECT count(*) FROM pop p WHERE p.cid NOT IN (SELECT cid FROM touched)) AS noncare_contacts,
          (SELECT count(*) FROM pop p WHERE p.cid NOT IN (SELECT cid FROM touched)
                                        AND p.cid IN (SELECT cid FROM booked))      AS noncare_booked
    """
    with lakebase.cursor() as cur:
        cur.execute(sql, (*REACHED_STATES, *OPP_BOOKED_STATES))
        care_contacts, care_booked, noncare_contacts, noncare_booked = cur.fetchone()

    care_rate = (care_booked / care_contacts) if care_contacts else 0.0
    noncare_rate = (noncare_booked / noncare_contacts) if noncare_contacts else 0.0
    lift = care_rate - noncare_rate

    with lakebase.cursor() as cur:
        cur.execute(
            """
            INSERT INTO care_gold_lift
                (id, care_contacts, care_booked, care_rate,
                 noncare_contacts, noncare_booked, noncare_rate, lift, computed_at)
            VALUES (1, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                care_contacts=EXCLUDED.care_contacts, care_booked=EXCLUDED.care_booked,
                care_rate=EXCLUDED.care_rate, noncare_contacts=EXCLUDED.noncare_contacts,
                noncare_booked=EXCLUDED.noncare_booked, noncare_rate=EXCLUDED.noncare_rate,
                lift=EXCLUDED.lift, computed_at=now()
            """,
            (care_contacts, care_booked, round(care_rate, 4),
             noncare_contacts, noncare_booked, round(noncare_rate, 4), round(lift, 4)),
        )

    return {"care_contacts": care_contacts, "care_booked": care_booked,
            "care_rate": care_rate, "noncare_contacts": noncare_contacts,
            "noncare_booked": noncare_booked, "noncare_rate": noncare_rate, "lift": lift}


def run() -> dict:
    spark = SparkSession.builder.getOrCreate()

    sync_state_to_delta(spark)
    changes, latest = read_changes(spark)

    audited = 0
    if changes is not None:
        audited = push_audit(changes)
        print(f"Care change feed: {audited} new records through version {latest}.")
    else:
        print(f"No new care changes since version {latest}.")

    funnel = care_funnel(spark)
    publish_funnel(funnel)
    _save_checkpoint(latest)

    lift = compute_and_publish_lift()

    overall = funnel[0]
    print(
        f"\nCare funnel — queued {overall[1]} · approved {overall[2]} · sent {overall[3]} "
        f"· opened {overall[4]} · clicked {overall[5]} · replied {overall[6]} · booked {overall[7]}"
    )
    print(
        f"Care-lift — care-touched booking rate {lift['care_rate']*100:.1f}% "
        f"({lift['care_booked']}/{lift['care_contacts']}) vs "
        f"non-care {lift['noncare_rate']*100:.1f}% "
        f"({lift['noncare_booked']}/{lift['noncare_contacts']})  ->  "
        f"lift {lift['lift']*100:+.1f} pts"
    )

    return {"audited": audited, "grains": len(funnel), "version": latest, "lift": lift}


if __name__ == "__main__":
    run()
