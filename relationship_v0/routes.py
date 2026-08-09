"""relationship_v0.routes — Flask blueprint for the v0.1 endpoints.

Registered behind a feature flag so v0.1 is DARK by default and cannot affect
the graded app:

    # in the existing app.py, ADD (do not modify existing routes):
    import os
    if os.getenv("ENABLE_RELATIONSHIP_V0") == "1":
        from relationship_v0.routes import bp as relationship_bp
        app.register_blueprint(relationship_bp)

Endpoints:
    GET  /care/health                      -> liveness + flag check
    POST /care/forecast-scan               -> build the care queue for a states list
    GET  /contacts/<id>/relationship       -> a contact's warmth score + components
    POST /relationship/recompute           -> rescore contacts
    POST /care/approve-send                -> analyst approves a queued care send

The heavy lifting is done by the pure modules + db.py; routes are thin.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from relationship_v0.policy import Trigger
from relationship_v0.pipeline import build_care_queue

bp = Blueprint("relationship_v0", __name__, template_folder="templates")


@bp.get("/care/health")
def health():
    return jsonify({"module": "relationship_v0", "status": "ok"})


@bp.get("/care")
def care_console():
    """The Proactive Care console (warmth badges + forecast-care queue). Served
    only under the flag, so the graded app UI is untouched."""
    return render_template("care_console.html")


@bp.post("/care/forecast-scan")
def forecast_scan_route():
    """Body: {"states": ["TX","MN"], "trigger": "forecast"}.
    Fetches forecast events, joins to contacts, returns the ranked care queue."""
    body = request.get_json(silent=True) or {}
    trigger = Trigger(body.get("trigger", "forecast"))

    contacts = _load_contacts(body.get("states"))
    events = _load_forecast_events(body.get("states"))

    queue = build_care_queue(contacts, events, trigger=trigger)

    # Attach a minimal event per row (route-level; the pure builder stays
    # untouched) so the console can queue a specific send.
    by_type = {}
    for e in events:
        by_type.setdefault(e.get("event_type"), e)
    for row in queue:
        e = by_type.get(row["event_type"]) or {}
        row["event"] = {"event_id": e.get("event_id"),
                        "event_type": e.get("event_type"),
                        "headline": e.get("headline"),
                        "area": e.get("area")}

    return jsonify({"count": len(queue),
                    "to_send": sum(1 for r in queue if r["action"]["send"]),
                    "queue": queue})


@bp.post("/care/queue")
def queue_route():
    """Body: {"contact_id": "cust_0001", "event": {...}, "trigger": "forecast"}.
    Runs the 4th agent tool to WRITE a queued care_sends row (human-in-the-loop),
    returning care_send_id so the analyst can then Approve & Send."""
    from agent.care_tool import send_proactive_care_tip
    body = request.get_json(silent=True) or {}
    contact_id = body.get("contact_id")
    event = body.get("event") or {}
    if not contact_id or not event.get("event_type"):
        return jsonify({"error": "contact_id and event.event_type required"}), 400
    result = send_proactive_care_tip(
        contact_id, event, trigger=body.get("trigger", "forecast"))
    return jsonify(result)


@bp.get("/contacts/<contact_id>/relationship")
def get_relationship_route(contact_id: str):
    # contact_id is the repo's TEXT customers.customer_id (e.g. 'cust_0001').
    from relationship_v0 import db
    rel = db.get_relationship(contact_id)
    if rel is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(rel)


@bp.post("/relationship/recompute")
def recompute_route():
    """Rescore contacts from the CRM and write contact_relationship rows.
    Body (optional): {"states": ["TX","MN"]}. Returns the count updated."""
    from relationship_v0 import db
    from relationship_v0.pipeline import _signals_from_contact
    body = request.get_json(silent=True) or {}
    contacts = _load_contacts(body.get("states"))
    updated = 0
    for c in contacts:
        db.upsert_relationship(c["contact_id"], _signals_from_contact(c),
                               tenant=c.get("tenant"))
        updated += 1
    return jsonify({"updated": updated})


@bp.post("/care/approve-send")
def approve_send_route():
    """Body: {"care_send_id": 123}. Flips queued -> sent (+ optional Twilio)."""
    from relationship_v0 import db
    body = request.get_json(silent=True) or {}
    csid = body.get("care_send_id")
    if not csid:
        return jsonify({"error": "care_send_id required"}), 400
    db.advance_care_send(csid, status="sent")
    return jsonify({"care_send_id": csid, "status": "sent"})


# --- repo-specific loaders (wired to the real repo) ------------------------- #
def _tenure_years(tenure_start):
    if not tenure_start:
        return None
    from datetime import date
    today = date.today()
    return max(0.0, (today - tenure_start).days / 365.25)


def _days_since(ts):
    if not ts:
        return None
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0, (now - ts).days)


def _load_contacts(states):
    """Load contacts from the graded `customers` table and shape them into the
    dicts build_care_queue / scoring expect.

    Base signals come from the CRM (tenure, LTV, service, geography). Consent /
    opt-out / recent-touch state and last-touch recency are overlaid from the
    v0.1 `contact_relationship` table when it exists, so the policy gates
    (opt-out, frequency cap) reflect reality. The overlay is best-effort: before
    the schema is applied or scores computed, contacts still load with safe
    defaults (consent True, not opted out, 0 touches). Engagement counters
    (opens/clicks/replies) are populated by /relationship/recompute as
    care_sends history accrues; unknown here, they default to 0.
    """
    import lakebase

    sql = """
        SELECT customer_id, tenant, name, service_type, city, state,
               lat, lon, lifetime_value, is_prospect, tenure_start
        FROM customers
        WHERE status IN ('active', 'lead')
    """
    params: list = []
    if states:
        sql += " AND state = ANY(%s)"
        params.append([s.upper() for s in states])

    with lakebase.cursor(dict_rows=True) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]

    # Best-effort relationship overlay (consent/opt-out/touches/last_touch).
    overlay: dict = {}
    try:
        with lakebase.cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT contact_id, consent_ok, opted_out, recent_care_touches, "
                "last_touch_at FROM contact_relationship"
            )
            overlay = {r["contact_id"]: dict(r) for r in cur.fetchall()}
    except Exception:
        overlay = {}  # table not created yet -> safe defaults below

    contacts = []
    for r in rows:
        rel = overlay.get(r["customer_id"], {})
        contacts.append({
            "contact_id": r["customer_id"],
            "tenant": r["tenant"],
            "name": r["name"],
            "service_type": r["service_type"],
            "city": r["city"],
            "state": r["state"],
            "lat": r["lat"],
            "lon": r["lon"],
            "lifetime_value": float(r["lifetime_value"] or 0.0),
            "is_prospect": bool(r["is_prospect"]),
            "tenure_years": _tenure_years(r["tenure_start"]),
            "days_since_last_touch": _days_since(rel.get("last_touch_at")),
            "consent_ok": rel.get("consent_ok", True),
            "opted_out": rel.get("opted_out", False),
            "recent_care_touches": rel.get("recent_care_touches", 0) or 0,
            # engagement counters accrue via recompute; unknown -> neutral zeros
            "opens": 0, "clicks": 0, "positive_replies": 0,
            "negative_events": 0, "completed_jobs": 0,
        })
    return contacts


def _load_forecast_events(states):
    """Forecast/watch events with lead time. Reuses the repo's NWS client
    (weather_client.fetch_active_alerts) and normalizes into the v0.1 event
    shape, then forecast_scan keeps only proactive (lead-time) events."""
    import lakebase  # noqa: F401  (ensures db/ is import-resolvable in-app)
    from relationship_v0.forecast_scan import normalize_event, is_proactive_event
    try:
        from pipeline import weather_client
    except Exception:
        weather_client = None

    if weather_client is None:
        # Standalone fallback: hit NWS directly for forecast products.
        from relationship_v0.forecast_scan import fetch_forecast_events
        return fetch_forecast_events(states or [])

    raw = weather_client.fetch_active_alerts(states=states or None)
    events = []
    for r in raw:
        ev = normalize_event(r)
        # weather_client already gives us a state; make sure the event carries it
        if not ev["states"] and r.get("state"):
            ev["states"] = {str(r["state"]).upper()}
        if is_proactive_event(ev):
            events.append(ev)
    return events
