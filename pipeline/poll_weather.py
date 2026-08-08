"""
Rainmaker -- weather poller.

Fetches active NWS alerts over the states we have customers in, keeps only the
event types that create service demand, and UPSERTs them into weather_events.

Idempotent: event_id is the NWS alert id, so polling every 15 minutes updates
rows in place rather than piling up duplicates.

Run as a Databricks Job on a schedule, or by hand before the demo:
    import os; os.environ["LAKEBASE_URL"] = "..."
    import poll_weather; poll_weather.run()
"""

from __future__ import annotations

import json
import os as _os
import sys as _sys

# Resolve paths from THIS file, not the working directory -- the app, the
# notebook, and pytest all run from different cwds.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "db"), _os.path.join(_ROOT, "pipeline")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from psycopg2.extras import execute_values

import lakebase
import weather_client

UPSERT_SQL = """
INSERT INTO weather_events
    (event_id, event_type, severity, certainty, urgency, headline, area_desc,
     state, lat, lon, radius_km, effective_at, expires_at, narrative_text, payload)
VALUES %s
ON CONFLICT (event_id) DO UPDATE SET
    severity       = EXCLUDED.severity,
    certainty      = EXCLUDED.certainty,
    urgency        = EXCLUDED.urgency,
    headline       = EXCLUDED.headline,
    area_desc      = EXCLUDED.area_desc,
    expires_at     = EXCLUDED.expires_at,
    narrative_text = EXCLUDED.narrative_text,
    payload        = EXCLUDED.payload
"""


def covered_states() -> list[str]:
    """Only poll where we actually have customers -- no point ingesting alerts
    for a state with nobody in it."""
    with lakebase.cursor() as cur:
        cur.execute("SELECT DISTINCT state FROM customers ORDER BY state")
        return [r[0] for r in cur.fetchall()]


def demand_event_types() -> list[str]:
    """The event types event_service_map says create demand. Driving the API
    filter from the mapping table means adding a new hazard is a data change,
    not a code change."""
    with lakebase.cursor() as cur:
        cur.execute("SELECT DISTINCT event_type FROM event_service_map ORDER BY event_type")
        return [r[0] for r in cur.fetchall()]


def run(states: list[str] | None = None, event_types: list[str] | None = None) -> int:
    states = states or covered_states()
    event_types = event_types or demand_event_types()

    if not states:
        raise RuntimeError("No customers seeded -- run seed.run() first.")

    alerts = weather_client.fetch_active_alerts(states=states, event_types=event_types)
    print(f"NWS returned {len(alerts)} matching active alerts across {len(states)} states.")

    if not alerts:
        # Genuinely normal on a calm day. Say so plainly rather than looking broken.
        print("No active demand-driving alerts right now. Nothing to write.")
        return 0

    rows = [
        (
            a["event_id"], a["event_type"], a["severity"], a["certainty"], a["urgency"],
            a["headline"], a["area_desc"], a["state"], a["lat"], a["lon"], a["radius_km"],
            a["effective_at"], a["expires_at"], a["narrative_text"], json.dumps(a["payload"]),
        )
        for a in alerts
    ]

    with lakebase.cursor() as cur:
        execute_values(cur, UPSERT_SQL, rows)

    by_type: dict[str, int] = {}
    for a in alerts:
        by_type[a["event_type"]] = by_type.get(a["event_type"], 0) + 1
    for event_type, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>3}  {event_type}")

    return len(rows)


if __name__ == "__main__":
    n = run()
    print(f"Upserted {n} weather events.")
