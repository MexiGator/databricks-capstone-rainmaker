"""
Local Spark smoke test for Match & Score.

Runs the REAL scoring function from match_and_score.py against fixture
DataFrames in local Spark. Catches join ambiguity, type mismatches, and
column-resolution bugs before they surface on Databricks -- where the
feedback loop is minutes instead of seconds.

Skipped automatically if pyspark is not installed, so the main suite still
runs anywhere.

    pytest tests/test_spark_scoring.py -q
"""

from __future__ import annotations

import pytest

pyspark = pytest.importorskip("pyspark", reason="pyspark not installed")

from pyspark.sql import SparkSession

from pipeline import match_and_score, scoring
from pipeline.match_and_score import CUSTOMER_SCHEMA, EVENT_SCHEMA, MAPPING_SCHEMA


@pytest.fixture(scope="module")
def spark():
    session = (
        SparkSession.builder.master("local[2]")
        .appName("rainmaker-smoke")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        .getOrCreate()
    )
    yield session
    session.stop()


# Fort Worth, and a customer 400 km away who must not survive scoring.
CUSTOMERS = [
    # customer_id, tenant, service_type, lat, lon, est_job_value, tier, assigned_rep
    ("cust_0001", "summit-exteriors", "roofing", 32.78, -96.80, 28000.0, "platinum", "Dana Ramirez"),
    ("cust_0002", "summit-exteriors", "roofing", 32.80, -96.85, 12000.0, "standard", "Kyle Whitfield"),
    ("cust_0003", "northline-plumbing", "plumbing", 44.98, -93.27, 3000.0, "gold", "Aoife Brennan"),
    ("cust_0004", "summit-exteriors", "roofing", 35.47, -97.52, 30000.0, "platinum", "Dana Ramirez"),
]
CUSTOMER_COLS = [
    "customer_id", "tenant", "service_type", "lat", "lon",
    "est_job_value", "tier", "assigned_rep",
]

EVENTS = [
    # event_id, event_type, severity, lat, lon, radius_km, state
    ("nws-001", "Severe Thunderstorm Warning", "Severe", 32.79, -96.82, 60.0, "TX"),
    ("nws-002", "Hard Freeze Warning", "Moderate", 44.98, -93.27, 80.0, "MN"),
]
EVENT_COLS = ["event_id", "event_type", "severity", "lat", "lon", "radius_km", "state"]

MAPPING = [
    ("Severe Thunderstorm Warning", "roofing", 0.85),
    ("Hard Freeze Warning", "plumbing", 0.95),
    ("Hard Freeze Warning", "roofing", 0.55),
]
MAPPING_COLS = ["event_type", "service_type", "urgency_weight"]


@pytest.fixture(scope="module")
def scored(spark):
    customers = spark.createDataFrame(CUSTOMERS, CUSTOMER_SCHEMA)
    events = spark.createDataFrame(EVENTS, EVENT_SCHEMA)
    mapping = spark.createDataFrame(MAPPING, MAPPING_SCHEMA)
    return match_and_score.score(customers, events, mapping).cache()


# ---------------------------------------------------------------------
# The job runs at all -- this is the class of bug local Spark exists to catch
# ---------------------------------------------------------------------
def test_job_runs_without_ambiguous_column_errors(scored):
    """customers and weather_events BOTH have lat, lon and service_type. If the
    aliasing is wrong Spark raises AMBIGUOUS_REFERENCE, and the only way to
    find out is to run it."""
    assert scored.count() >= 0


def test_output_schema_matches_the_lakebase_upsert(scored):
    """Column ORDER matters -- sync_to_lakebase passes tuples positionally, so
    a reordered select would silently write values into the wrong columns."""
    assert scored.columns == [
        "opportunity_id", "weather_event_id", "customer_id", "tenant",
        "service_needed", "distance_km", "exposure_score", "priority",
        "est_value", "assigned_rep",
    ]


def test_no_nulls_in_columns_the_database_requires(scored):
    required = ["opportunity_id", "weather_event_id", "customer_id", "tenant",
                "service_needed", "exposure_score", "priority", "est_value"]
    for col in required:
        assert scored.filter(scored[col].isNull()).count() == 0, f"{col} has nulls"


# ---------------------------------------------------------------------
# The scoring behaves the same in Spark as it does in the unit tests
# ---------------------------------------------------------------------
def test_nearby_customer_is_scored(scored):
    rows = {r["customer_id"]: r for r in scored.collect()}
    assert "cust_0001" in rows


def test_distant_customer_is_filtered_out(scored):
    """cust_0004 is 300 km from the Fort Worth storm with a $30k job. If the
    additive-vs-multiplicative decision ever regresses, this catches it."""
    ids = {r["customer_id"] for r in scored.collect()}
    assert "cust_0004" not in ids


def test_service_mismatch_never_produces_an_opportunity(scored):
    """A plumbing customer must not appear against a hail event."""
    for r in scored.collect():
        assert r["service_needed"] in ("roofing", "plumbing")
        if r["weather_event_id"] == "nws-001":
            assert r["service_needed"] == "roofing"


def test_platinum_outranks_standard_at_the_same_storm(scored):
    rows = {r["customer_id"]: r for r in scored.collect()}
    if "cust_0002" in rows:
        assert rows["cust_0001"]["exposure_score"] >= rows["cust_0002"]["exposure_score"]


def test_spark_scores_match_the_pure_python_oracle(scored):
    """Parity check. The pure functions are what 20 unit tests cover; this
    asserts the distributed path produces identical numbers."""
    events = {e[0]: e for e in EVENTS}
    customers = {c[0]: c for c in CUSTOMERS}
    urgency = {(m[0], m[1]): m[2] for m in MAPPING}

    for r in scored.collect():
        e = events[r["weather_event_id"]]
        c = customers[r["customer_id"]]
        expected_distance = scoring.haversine_km(e[3], e[4], c[3], c[4])
        expected = scoring.exposure_score(
            e[2], expected_distance, e[5], urgency[(e[1], c[2])], c[5], c[6]
        )
        assert abs(float(r["exposure_score"]) - expected) < 1e-6, r["customer_id"]


def test_opportunity_ids_are_stable_across_runs(spark, scored):
    """Re-running must UPSERT, not duplicate. Same inputs, same ids."""
    again = match_and_score.score(
        spark.createDataFrame(CUSTOMERS, CUSTOMER_SCHEMA),
        spark.createDataFrame(EVENTS, EVENT_SCHEMA),
        spark.createDataFrame(MAPPING, MAPPING_SCHEMA),
    )
    first = sorted(r["opportunity_id"] for r in scored.collect())
    second = sorted(r["opportunity_id"] for r in again.collect())
    assert first == second


def test_every_row_clears_the_queue_cutoff(scored):
    for r in scored.collect():
        assert float(r["exposure_score"]) >= scoring.QUEUE_CUTOFF


def test_priority_bands_are_populated(scored):
    for r in scored.collect():
        assert r["priority"] in {"critical", "high", "medium", "low"}


# ---------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------
def test_all_null_geometry_column_does_not_break_dataframe_creation(spark):
    """Zone-only NWS alerts carry no polygon. A batch where EVERY alert lacks
    coordinates is a real scenario, and type inference cannot handle an
    all-null column -- which is why the schemas are declared explicitly."""
    events = spark.createDataFrame(
        [("nws-003", "Severe Thunderstorm Warning", "Severe", None, None, 60.0, "TX")],
        EVENT_SCHEMA,
    )
    out = match_and_score.score(
        spark.createDataFrame(CUSTOMERS, CUSTOMER_SCHEMA),
        events,
        spark.createDataFrame(MAPPING, MAPPING_SCHEMA),
    )
    assert out.count() > 0
    for r in out.collect():
        assert r["distance_km"] is None


def test_no_matching_service_yields_empty_not_error(spark):
    events = spark.createDataFrame(
        [("nws-004", "Excessive Heat Warning", "Severe", 33.4, -112.0, 60.0, "AZ")],
        EVENT_SCHEMA,
    )
    out = match_and_score.score(
        spark.createDataFrame(CUSTOMERS, CUSTOMER_SCHEMA),
        events,
        spark.createDataFrame(MAPPING, MAPPING_SCHEMA),
    )
    assert out.count() == 0
