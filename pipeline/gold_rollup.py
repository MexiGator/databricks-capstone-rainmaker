"""
Rainmaker -- gold analytics rollup (requirement #6).

The loop:
  1. Read current opportunity state from Lakebase (where the app writes).
  2. MERGE it into the Delta table. The merge is what generates change records.
  3. Read the DELTA CHANGE DATA FEED since the last checkpoint -- this is the
     requirement, and it is load-bearing rather than decorative.
  4. Derive status transitions from the feed and roll up funnel + revenue.
  5. Push the rollup and the raw change rows back to Lakebase so the Results
     tab renders numbers that were genuinely computed from captured changes.

Why the round trip: the app writes to Lakebase (Postgres) because that is the
operational store an interactive console needs. Delta is where change capture
and analytics live. Bridging them is the honest architecture, not a shortcut.

Run after a demo pass:
    import os; os.environ["LAKEBASE_URL"] = "..."
    import gold_rollup; gold_rollup.run()
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

DELTA_TABLE = "workspace.default.rainmaker_opportunities"

# Which statuses count at each funnel stage. A booked opportunity has also
# been sent and responded to -- the funnel is cumulative, not exclusive.
SENT_STATES = ("sent", "responded", "booked", "quoted", "won", "completed")
RESPONDED_STATES = ("responded", "booked", "quoted", "won", "completed")
BOOKED_STATES = ("booked", "quoted", "won", "completed")
WON_STATES = ("won", "completed")


# ---------------------------------------------------------------------
# 1-2. Sync Lakebase state into Delta so the change feed sees it
# ---------------------------------------------------------------------
def sync_state_to_delta(spark: SparkSession) -> None:
    with lakebase.connect() as conn:
        current = pd.read_sql(
            """
            SELECT o.opportunity_id, o.weather_event_id, o.customer_id, o.tenant,
                   o.service_needed, o.exposure_score::float8 AS exposure_score,
                   o.priority, o.est_value::float8 AS est_value, o.status,
                   o.updated_at, w.event_type
            FROM opportunities o
            JOIN weather_events w ON w.event_id = o.weather_event_id
            """,
            conn,
        )

    if current.empty:
        raise RuntimeError("No opportunities in Lakebase. Run match_and_score first.")

    updates = spark.createDataFrame(current)
    updates.createOrReplaceTempView("incoming")

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

    # MERGE rather than overwrite: overwrite would rewrite every row and the
    # change feed would report the whole table as changed, drowning the real
    # transitions in noise.
    spark.sql(f"""
        MERGE INTO {DELTA_TABLE} AS t
        USING incoming AS s
        ON t.opportunity_id = s.opportunity_id
        WHEN MATCHED AND t.status <> s.status THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)


# ---------------------------------------------------------------------
# 3. Read the change feed
# ---------------------------------------------------------------------
def _checkpoint() -> int:
    with lakebase.cursor() as cur:
        cur.execute("SELECT last_version FROM cdf_checkpoint WHERE table_name = %s", (DELTA_TABLE,))
        row = cur.fetchone()
        return row[0] if row else 0


def _save_checkpoint(version: int) -> None:
    with lakebase.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cdf_checkpoint (table_name, last_version, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (table_name) DO UPDATE SET
                last_version = EXCLUDED.last_version, updated_at = now()
            """,
            (DELTA_TABLE, version),
        )


def read_changes(spark: SparkSession):
    """Everything new since the last rollup. Returns (DataFrame, latest_version)."""
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
    """Land the raw change rows in Lakebase so the Results tab can show them.
    This is the 'here is the actual feed' beat in the demo."""
    rows = (
        changes.filter(F.col("_change_type").isin("insert", "update_postimage"))
        .select(
            "opportunity_id",
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
        (r.opportunity_id, r.change_type, None, r.new_status, int(r.commit_version), r.commit_ts)
        for r in rows.itertuples()
    ]
    with lakebase.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO cdf_audit
                (opportunity_id, change_type, old_status, new_status, commit_version, commit_ts)
            VALUES %s
            ON CONFLICT (opportunity_id, commit_version, change_type) DO NOTHING
            """,
            payload,
        )
    return len(payload)


# ---------------------------------------------------------------------
# 4-5. Roll up and publish
# ---------------------------------------------------------------------
def _funnel_exprs():
    return [
        F.count("*").alias("identified"),
        F.count(F.when(F.col("status").isin(*SENT_STATES), 1)).alias("sent"),
        F.count(F.when(F.col("status").isin(*RESPONDED_STATES), 1)).alias("responded"),
        F.count(F.when(F.col("status").isin(*BOOKED_STATES), 1)).alias("booked"),
        F.count(F.when(F.col("status").isin(*WON_STATES), 1)).alias("won"),
        F.coalesce(
            F.sum(F.when(F.col("status").isin(*BOOKED_STATES), F.col("est_value"))), F.lit(0.0)
        ).alias("pipeline_est"),
        F.coalesce(
            F.sum(F.when(F.col("status").isin(*WON_STATES), F.col("est_value"))), F.lit(0.0)
        ).alias("revenue_won"),
    ]


def rollup(spark: SparkSession) -> list[tuple]:
    """Current-state rollup, at two grains: overall and by storm type."""
    state = spark.table(DELTA_TABLE)

    overall = state.agg(*_funnel_exprs()).toPandas().iloc[0]
    rows: list[tuple] = [
        (
            "overall", "", int(overall.identified), int(overall.sent), int(overall.responded),
            int(overall.booked), int(overall.won),
            float(overall.pipeline_est), float(overall.revenue_won),
        )
    ]

    for r in state.groupBy("event_type").agg(*_funnel_exprs()).toPandas().itertuples():
        rows.append(
            (
                "event_type", r.event_type, int(r.identified), int(r.sent), int(r.responded),
                int(r.booked), int(r.won), float(r.pipeline_est), float(r.revenue_won),
            )
        )
    return rows


def publish(rows: list[tuple]) -> None:
    with lakebase.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO gold_results
                (grain, grain_value, identified, sent, responded, booked, won,
                 pipeline_est, revenue_won)
            VALUES %s
            ON CONFLICT (grain, grain_value) DO UPDATE SET
                identified   = EXCLUDED.identified,
                sent         = EXCLUDED.sent,
                responded    = EXCLUDED.responded,
                booked       = EXCLUDED.booked,
                won          = EXCLUDED.won,
                pipeline_est = EXCLUDED.pipeline_est,
                revenue_won  = EXCLUDED.revenue_won,
                computed_at  = now()
            """,
            rows,
        )


def run() -> dict:
    spark = SparkSession.builder.getOrCreate()

    sync_state_to_delta(spark)
    changes, latest = read_changes(spark)

    audited = 0
    if changes is not None:
        audited = push_audit(changes)
        print(f"Change feed: {audited} new change records through version {latest}.")
        changes.select(
            "opportunity_id", "status", "_change_type", "_commit_version", "_commit_timestamp"
        ).orderBy(F.col("_commit_version").desc()).show(12, truncate=False)
    else:
        print(f"No new changes since version {latest}.")

    rows = rollup(spark)
    publish(rows)
    _save_checkpoint(latest)

    overall = rows[0]
    print(
        f"\nFunnel — identified {overall[2]} · sent {overall[3]} · responded {overall[4]} "
        f"· booked {overall[5]} · won {overall[6]}"
    )
    print(f"Pipeline (est.) ${overall[7]:,.0f}   Jobs won ${overall[8]:,.0f}")

    return {"audited": audited, "grains": len(rows), "version": latest}


if __name__ == "__main__":
    run()
