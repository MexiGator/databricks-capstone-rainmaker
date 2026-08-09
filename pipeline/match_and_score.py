"""
Rainmaker -- Match & Score (the Spark pipeline, requirement #1).

    weather_events  x  event_service_map  x  customers
        -> haversine distance
        -> exposure score
        -> opportunities

Writes to a Delta table with Change Data Feed enabled (requirement #6), then
syncs into Lakebase so the app can read and write it live.

Why the round trip: serverless cannot do a Spark JDBC write to Lakebase
(Day 2 lecture). So Spark owns the computation and Delta, and a small
psycopg2 upsert moves the result into the operational store. This is also
the loop from Zach's Day 1 architecture slide -- Spark enriches the lake,
the lake feeds Lakebase, the app reads Lakebase.

Run:
    import os; os.environ["LAKEBASE_URL"] = "..."
    import match_and_score; match_and_score.run()
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Resolve paths from THIS file, not the working directory -- the app, the
# notebook, and pytest all run from different cwds.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "db"), _os.path.join(_ROOT, "pipeline")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from pyspark.sql import SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql.types import (
    BooleanType, DoubleType, StringType, StructField, StructType,
)

import scoring


def _db():
    """Database imports are lazy so `score()` -- which is pure Spark -- can be
    exercised in local Spark without psycopg2 or a Lakebase connection."""
    import lakebase

    return lakebase

DELTA_TABLE = "workspace.default.rainmaker_opportunities"


# ---------------------------------------------------------------------
# Load: Lakebase -> pandas -> Spark
# ---------------------------------------------------------------------
def _read_sql(query: str):
    """
    pandas warns that psycopg2 is not a SQLAlchemy connectable. It works fine
    for reads and pulling in SQLAlchemy just to silence a warning is not worth
    the dependency, so the warning is suppressed at the call site rather than
    left to clutter every pipeline run.
    """
    import warnings

    import pandas as pd

    with _db().connect() as conn:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")
            return pd.read_sql(query, conn)


# Explicit schemas, not inference.
#
# spark.createDataFrame raises CANNOT_DETERMINE_TYPE when a column is entirely
# null -- which happens for real: a batch of zone-only NWS alerts has no
# coordinates at all, and a CRM with no prospects has no null tenure dates.
# Inference works right up until the day the data is uniform, then fails the
# whole job. Declaring the schema costs six lines and removes the failure mode.
CUSTOMER_SCHEMA = StructType([
    StructField("customer_id", StringType()),
    StructField("tenant", StringType()),
    StructField("service_type", StringType()),
    StructField("lat", DoubleType()),
    StructField("lon", DoubleType()),
    StructField("est_job_value", DoubleType()),
    StructField("tier", StringType()),
    StructField("assigned_rep", StringType()),
    StructField("state", StringType()),
])

EVENT_SCHEMA = StructType([
    StructField("event_id", StringType()),
    StructField("event_type", StringType()),
    StructField("severity", StringType()),
    StructField("lat", DoubleType()),
    StructField("lon", DoubleType()),
    StructField("radius_km", DoubleType()),
    StructField("state", StringType()),
])

MAPPING_SCHEMA = StructType([
    StructField("event_type", StringType()),
    StructField("service_type", StringType()),
    StructField("urgency_weight", DoubleType()),
])


def load_frames(spark: SparkSession):
    """Pull the three inputs. At this scale (tens of thousands of pairs) the
    pandas hop is cheaper than standing up a JDBC read, and it keeps the job
    runnable on Free Edition serverless."""
    customers = _read_sql("""
        SELECT customer_id, tenant, service_type, lat, lon,
               est_job_value, tier, assigned_rep, state
        FROM customers
        WHERE status IN ('active', 'lead')
    """)
    events = _read_sql("""
        SELECT event_id, event_type, severity, lat, lon, radius_km, state
        FROM weather_events
        WHERE expires_at IS NULL OR expires_at > now()
    """)
    mapping = _read_sql("SELECT event_type, service_type, urgency_weight FROM event_service_map")

    if events.empty:
        raise RuntimeError(
            "No active weather events. Run poll_weather.run() first -- and note "
            "that on a calm day NWS may legitimately return nothing."
        )

    return (
        spark.createDataFrame(customers, schema=CUSTOMER_SCHEMA),
        spark.createDataFrame(events, schema=EVENT_SCHEMA),
        spark.createDataFrame(mapping, schema=MAPPING_SCHEMA),
    )


# ---------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------
# Implemented as Spark COLUMN EXPRESSIONS, not Python UDFs.
#
# The first version used UDFs wrapping the pure functions in scoring.py, for
# testability. It failed: a UDF ships a *reference* to `scoring`, and the Python
# worker cannot import a module that only exists on the driver --
#     ModuleNotFoundError: No module named 'scoring'
# You can work around that with addPyFile, but column expressions remove the
# dependency entirely and run natively rather than round-tripping every row
# through a Python process.
#
# scoring.py remains the readable definition of the logic and the target of 20
# unit tests. test_spark_scores_match_the_pure_python_oracle asserts this
# distributed path produces identical numbers, so the two cannot drift.
#
# Every expression below is built FROM the constants in scoring.py, so tuning a
# weight changes both paths at once.
# ---------------------------------------------------------------------
def _severity_expr(col):
    """Dict lookup with a default, as a Column."""
    pairs = []
    for label, weight in scoring.SEVERITY_WEIGHT.items():
        pairs += [F.lit(label), F.lit(float(weight))]
    return F.coalesce(
        F.element_at(F.create_map(*pairs), col), F.lit(scoring.DEFAULT_SEVERITY)
    )


def _proximity_expr(distance, radius):
    """Mirrors scoring.proximity_factor exactly, including the null case."""
    overshoot = distance - radius
    decay = F.lit(float(scoring.OUTSIDE_RADIUS_DECAY_KM))
    return (
        # Null distance means a zone-based alert with no polygon. The join
        # above already gated those to the customer's own state, so this branch
        # is always the same-state case -- hence the same constant the pure
        # Python path uses for same_state=True.
        #
        # This was hardcoded 0.5 while scoring.py had moved to 0.60, and the
        # parity test missed it because neither fixture event had null
        # geometry. Two implementations of one rule must read one constant.
        F.when(
            distance.isNull() | radius.isNull() | (radius <= 0),
            F.lit(float(scoring.SAME_STATE_PROXIMITY)),
        )
        .when(distance <= radius, F.lit(1.0) - F.lit(0.5) * (distance / radius))
        .when(overshoot >= decay, F.lit(0.0))
        .otherwise(F.lit(0.5) * (F.lit(1.0) - overshoot / decay))
    )


def _value_expr(job_value, tier, service):
    """Normalised against THIS TRADE's ceiling. A single roofing-scale ceiling
    capped the best possible HVAC customer at 0.43 and kept every non-roofing
    opportunity out of the queue."""
    tier_pairs = []
    for label, bonus in scoring.TIER_BONUS.items():
        tier_pairs += [F.lit(label), F.lit(float(bonus))]
    bonus = F.coalesce(F.element_at(F.create_map(*tier_pairs), tier), F.lit(0.0))

    ceiling_pairs = []
    for label, ceiling in scoring.VALUE_CEILING_BY_SERVICE.items():
        ceiling_pairs += [F.lit(label), F.lit(float(ceiling))]
    ceiling = F.coalesce(
        F.element_at(F.create_map(*ceiling_pairs), service),
        F.lit(float(scoring.VALUE_CEILING)),
    )

    base = F.least(F.coalesce(job_value, F.lit(0.0)) / ceiling, F.lit(1.0))
    return F.least(base + bonus, F.lit(1.0))


def _haversine_expr(lat1, lon1, lat2, lon2):
    p1, p2 = F.radians(lat1), F.radians(lat2)
    dp = p2 - p1
    dl = F.radians(lon2 - lon1)
    a = F.pow(F.sin(dp / 2), 2) + F.cos(p1) * F.cos(p2) * F.pow(F.sin(dl / 2), 2)
    return F.lit(2 * scoring.EARTH_RADIUS_KM) * F.asin(F.sqrt(a))


def _priority_expr(score_col):
    ordered = sorted(scoring.PRIORITY_BANDS, key=lambda b: -b[0])
    expr = F.when(score_col >= F.lit(ordered[0][0]), F.lit(ordered[0][1]))
    for threshold, label in ordered[1:]:
        expr = expr.when(score_col >= F.lit(threshold), F.lit(label))
    return expr.otherwise(F.lit("low"))


def _opportunity_id_expr(event_id, customer_id):
    """Must byte-for-byte match scoring.opportunity_id, or re-running the job
    would insert duplicates instead of upserting."""
    digest = F.sha1(F.concat_ws("|", event_id, customer_id))
    return F.concat(F.lit("opp_"), F.substring(digest, 1, 20))


def score(customers_df, events_df, mapping_df):
    """Join, measure, score, rank. Returns the opportunities DataFrame."""
    # event -> service line. This join is what stops a freeze alert from
    # generating roofing opportunities.
    events_mapped = events_df.join(mapping_df, on="event_type", how="inner")

    # Rename before the join. customers and weather_events both carry lat, lon
    # and service_type, and relying on alias-qualified names through a second
    # join is how you get AMBIGUOUS_REFERENCE at runtime.
    events_mapped = (
        events_mapped.withColumnRenamed("lat", "event_lat")
        .withColumnRenamed("lon", "event_lon")
        .withColumnRenamed("service_type", "service_needed")
    )
    customers = (
        customers_df.withColumnRenamed("lat", "cust_lat")
        .withColumnRenamed("lon", "cust_lon")
        .withColumnRenamed("service_type", "cust_service")
    )

    events_mapped = events_mapped.withColumnRenamed("state", "event_state")
    customers = customers.withColumnRenamed("state", "cust_state")

    # Service line must match. On top of that, an alert with NO POLYGON is
    # gated to its own state.
    #
    # Without this, a zone-based Extreme Heat Warning has no coordinates, so
    # distance is null, so nothing constrains it spatially -- and a Tucson
    # customer matched all seven heat warnings in the country including
    # Florida's. Alerts WITH a polygon need no gate; distance already handles
    # them, and storms cross state lines.
    same_service = events_mapped["service_needed"] == customers["cust_service"]
    has_geometry = events_mapped["event_lat"].isNotNull()
    same_state = events_mapped["event_state"] == customers["cust_state"]

    pairs = events_mapped.join(
        customers, same_service & (has_geometry | same_state), how="inner"
    )

    distance = F.when(
        F.col("event_lat").isNotNull() & F.col("event_lon").isNotNull(),
        _haversine_expr(
            F.col("event_lat"), F.col("event_lon"), F.col("cust_lat"), F.col("cust_lon")
        ),
    ).otherwise(F.lit(None).cast(DoubleType()))

    scored = pairs.withColumn("distance_km", distance)

    raw = (
        _severity_expr(F.col("severity"))
        * _proximity_expr(F.col("distance_km"), F.col("radius_km").cast(DoubleType()))
        * F.col("urgency_weight").cast(DoubleType())
        * _value_expr(
            F.col("est_job_value").cast(DoubleType()), F.col("tier"), F.col("service_needed")
        )
    )

    scored = (
        scored.withColumn(
            "exposure_score",
            F.round(F.least(F.greatest(raw, F.lit(0.0)), F.lit(1.0)), 3),
        )
        .withColumn("priority", _priority_expr(F.col("exposure_score")))
        .withColumn("opportunity_id", _opportunity_id_expr(F.col("event_id"), F.col("customer_id")))
        .withColumn("est_value", F.col("est_job_value").cast(DoubleType()))
    )

    # One opportunity per customer per hazard type.
    #
    # Seven concurrent Extreme Heat Warnings over Arizona are one heat event as
    # far as a homeowner is concerned. Without this the analyst sees the same
    # name seven times with identical scores, which reads as broken software
    # even though every row is technically correct.
    best_per_hazard = Window.partitionBy("customer_id", "event_type").orderBy(
        F.col("exposure_score").desc(), F.col("event_id")
    )
    scored = (
        scored.withColumn("_rank", F.row_number().over(best_per_hazard))
        .filter(F.col("_rank") == 1)
        .drop("_rank")
    )

    # Cutoff: everything below the floor is noise, and a queue full of noise is
    # a queue the analyst stops trusting.
    return scored.filter(F.col("exposure_score") >= F.lit(scoring.QUEUE_CUTOFF)).select(
        "opportunity_id",
        F.col("event_id").alias("weather_event_id"),
        "customer_id",
        "tenant",
        "service_needed",
        "distance_km",
        "exposure_score",
        "priority",
        "est_value",
        "assigned_rep",
    )


# ---------------------------------------------------------------------
# Write: Delta (with CDF) then Lakebase
# ---------------------------------------------------------------------
def write_delta(spark: SparkSession, df) -> None:
    """Requirement #6 lives here: Change Data Feed on the Delta table means
    every status transition is captured for the Results tab."""
    (
        df.write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .option("delta.enableChangeDataFeed", "true")
        .saveAsTable(DELTA_TABLE)
    )
    spark.sql(
        f"ALTER TABLE {DELTA_TABLE} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)"
    )


UPSERT_SQL = """
INSERT INTO opportunities
    (opportunity_id, weather_event_id, customer_id, tenant, service_needed,
     distance_km, exposure_score, priority, est_value, assigned_rep)
VALUES %s
ON CONFLICT (opportunity_id) DO UPDATE SET
    exposure_score = EXCLUDED.exposure_score,
    priority       = EXCLUDED.priority,
    distance_km    = EXCLUDED.distance_km,
    est_value      = EXCLUDED.est_value
"""


def sync_to_lakebase(df) -> int:
    """
    Upsert scored rows into the operational store.

    Note what the ON CONFLICT clause deliberately does NOT touch: `status`.
    Re-running the scoring job must never reset an opportunity the analyst
    has already sent or booked.
    """
    from psycopg2.extras import execute_values

    rows = [tuple(r) for r in df.toPandas().itertuples(index=False, name=None)]
    if not rows:
        return 0
    with _db().cursor() as cur:
        execute_values(cur, UPSERT_SQL, rows)
    return len(rows)


def run() -> int:
    spark = SparkSession.builder.getOrCreate()

    customers_df, events_df, mapping_df = load_frames(spark)
    opportunities = score(customers_df, events_df, mapping_df)

    # No .cache() here. Serverless compute rejects it:
    #   [NOT_SUPPORTED_WITH_SERVERLESS] PERSIST TABLE is not supported
    # The plan is re-evaluated for each action below, which at tens of
    # thousands of pairs costs milliseconds. Caching would be worth arguing
    # about at a hundred times this volume; here it buys nothing.
    n = opportunities.count()
    print(f"Scored {n} opportunities above the {scoring.QUEUE_CUTOFF} cutoff.")

    if n == 0:
        print("No opportunities cleared the cutoff -- weather is live but nobody is exposed.")
        return 0

    print("\nTop 10 by exposure:")
    opportunities.orderBy(F.col("exposure_score").desc()).show(10, truncate=False)

    opportunities.groupBy("priority").count().orderBy("priority").show()

    write_delta(spark, opportunities)
    synced = sync_to_lakebase(opportunities)
    print(f"Wrote {n} rows to {DELTA_TABLE} (CDF on) and synced {synced} to Lakebase.")
    return n


if __name__ == "__main__":
    run()
