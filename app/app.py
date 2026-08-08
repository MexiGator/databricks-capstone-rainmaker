"""
Rainmaker -- Storm Response console (requirement #4).

ONE Databricks App, TWO tabs over the same data:
  Storm Response  -- the operate view: queue, drafts, send, replies
  Results         -- the outcome view: funnel, pipeline vs won, CDF trail

Flask, same pattern as the Day 1 ticketing app, same Lakebase secret binding.
"""

from __future__ import annotations

import sys
import traceback

import os as _os
import sys as _sys

# Resolve paths from THIS file, not the working directory -- the app, the
# notebook, and pytest all run from different cwds.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "db"), _os.path.join(_ROOT, "pipeline")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from flask import Flask, jsonify, render_template, request

import lakebase
from agent import retrieval, simulate, tools

app = Flask(__name__)


def _fail(exc: Exception, code: int = 500):
    """Return the actual error. A console that hides why an action failed is
    worse than one that never had the action."""
    traceback.print_exc()
    return jsonify({"ok": False, "error": str(exc)}), code


@app.get("/healthz")
def healthz():
    try:
        with lakebase.cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify({"ok": True, "database": "connected"})
    except Exception as exc:
        return jsonify({"ok": False, "database": str(exc)}), 503


@app.get("/")
def index():
    return render_template("index.html")


# =====================================================================
# STORM RESPONSE
# =====================================================================
@app.get("/api/active-events")
def active_events():
    """The bulletin strip. Real NWS alerts currently driving the queue."""
    try:
        with lakebase.cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT w.event_id, w.event_type, w.severity, w.headline,
                       w.area_desc, w.state, w.expires_at,
                       count(o.opportunity_id) AS exposed
                FROM weather_events w
                LEFT JOIN opportunities o ON o.weather_event_id = w.event_id
                WHERE w.expires_at IS NULL OR w.expires_at > now()
                GROUP BY w.event_id
                HAVING count(o.opportunity_id) > 0
                ORDER BY count(o.opportunity_id) DESC
                LIMIT 8
            """)
            return jsonify([dict(r) for r in cur.fetchall()])
    except Exception as exc:
        return _fail(exc)


@app.get("/api/stats")
def stats():
    try:
        with lakebase.cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT
                  count(*) FILTER (WHERE status NOT IN ('lost'))                AS opportunities,
                  count(*) FILTER (WHERE status IN ('sent','responded','booked',
                                                    'quoted','won','completed')) AS sent,
                  count(*) FILTER (WHERE status IN ('booked','quoted','won','completed')) AS booked,
                  COALESCE(sum(est_value) FILTER (
                      WHERE status IN ('booked','quoted','won','completed')), 0) AS pipeline_est
                FROM opportunities
            """)
            stats = dict(cur.fetchone())

            cur.execute(
                "SELECT count(*) AS safety_sent FROM outreach WHERE kind = 'safety' AND status = 'sent'"
            )
            stats.update(dict(cur.fetchone()))
            return jsonify(stats)
    except Exception as exc:
        return _fail(exc)


@app.get("/api/opportunities")
def opportunities():
    status = request.args.get("status")
    limit = min(int(request.args.get("limit", 60)), 200)
    try:
        sql = "SELECT * FROM v_opportunity_queue"
        params: list = []
        if status and status != "all":
            sql += " WHERE status = %s"
            params.append(status)
        sql += " LIMIT %s"
        params.append(limit)

        with lakebase.cursor(dict_rows=True) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]

        # Attach the latest draft so the panel can render without a second call.
        with lakebase.cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT DISTINCT ON (opportunity_id)
                       opportunity_id, outreach_id, message_text, template_id,
                       similarity, status AS outreach_status
                FROM outreach WHERE kind = 'commercial'
                ORDER BY opportunity_id, created_at DESC
            """)
            drafts = {r["opportunity_id"]: dict(r) for r in cur.fetchall()}

            cur.execute("""
                SELECT opportunity_id, message_text, sent_at
                FROM outreach WHERE kind = 'safety' AND status = 'sent'
            """)
            notices = {r["opportunity_id"]: dict(r) for r in cur.fetchall()}

        for row in rows:
            row["draft"] = drafts.get(row["opportunity_id"])
            row["safety"] = notices.get(row["opportunity_id"])
        return jsonify(rows)
    except Exception as exc:
        return _fail(exc)


@app.post("/api/opportunities/<opportunity_id>/safety")
def safety_notice(opportunity_id):
    """Agent Tool 0. Always available, never blocked, no model call."""
    try:
        return jsonify(tools.send_safety_notice(opportunity_id))
    except Exception as exc:
        return _fail(exc)


@app.get("/api/follow-ups")
def follow_ups():
    """Safety notices that promised a check-in and haven't had one.
    Kept visible because an unkept promise is worse than no promise."""
    try:
        return jsonify(tools.pending_follow_ups())
    except Exception as exc:
        return _fail(exc)


@app.post("/api/opportunities/<opportunity_id>/draft")
def draft(opportunity_id):
    """Agent Tool 1."""
    try:
        return jsonify(tools.draft_outreach(opportunity_id))
    except Exception as exc:
        return _fail(exc)


@app.post("/api/outreach/<int:outreach_id>/send")
def send(outreach_id):
    """
    Agent Tool 2, then the closed loop: the send triggers a seeded reply,
    and Tool 3 classifies it and books if the customer said yes.

    Chained server-side on purpose -- the whole point of the demo is that the
    analyst clicks once and the loop runs.
    """
    try:
        sent = tools.send_and_create_lead(outreach_id, approved_by="analyst")
        if not sent.get("ok"):
            return jsonify(sent)

        result = {"sent": sent, "reply": None, "booking": None}
        reply = simulate.simulate_for_opportunity(sent["opportunity_id"])
        if reply:
            result["reply"] = reply
            result["booking"] = tools.handle_reply_and_book(reply["reply_id"])
        return jsonify(result)
    except Exception as exc:
        return _fail(exc)


@app.post("/api/simulate-replies")
def simulate_replies():
    """Generate replies for anything already sent, then run Tool 3 on each."""
    try:
        replies = simulate.simulate_all_sent()
        processed = [tools.handle_reply_and_book(r["reply_id"]) for r in replies]
        return jsonify({"ok": True, "replies": len(replies), "processed": processed})
    except Exception as exc:
        return _fail(exc)


@app.post("/api/opportunities/<opportunity_id>/status")
def set_status(opportunity_id):
    """Manual override. Marking a job won is a human decision -- it happens
    after an on-site quote, which the agent has no way to observe."""
    new_status = (request.json or {}).get("status")
    allowed = {"identified", "drafted", "sent", "responded", "booked",
               "quoted", "won", "completed", "lost"}
    if new_status not in allowed:
        return jsonify({"ok": False, "error": f"Invalid status {new_status!r}"}), 400
    try:
        with lakebase.cursor() as cur:
            cur.execute(
                "UPDATE opportunities SET status = %s WHERE opportunity_id = %s",
                (new_status, opportunity_id),
            )
            if new_status in ("won", "completed"):
                cur.execute(
                    """UPDATE bookings SET status = 'won', won_value = est_value
                       WHERE opportunity_id = %s""",
                    (opportunity_id,),
                )
        return jsonify({"ok": True, "opportunity_id": opportunity_id, "status": new_status})
    except Exception as exc:
        return _fail(exc)


# =====================================================================
# ASK -- the RAG showcase
# =====================================================================
@app.post("/api/ask")
def ask():
    question = (request.json or {}).get("question", "").strip()
    if not question:
        return jsonify({"ok": False, "error": "Ask a question first."}), 400
    try:
        return jsonify(retrieval.ask(question))
    except Exception as exc:
        return _fail(exc)


# =====================================================================
# RESULTS
# =====================================================================
@app.get("/api/analytics")
def analytics():
    """
    Prefer the CDF-derived gold rollup. Fall back to a live query only if the
    rollup has never run, so the tab is never blank during a first demo -- but
    say which one you're looking at, because "computed from the change feed"
    and "queried just now" are different claims.
    """
    try:
        with lakebase.cursor(dict_rows=True) as cur:
            cur.execute("SELECT * FROM gold_results WHERE grain = 'overall'")
            gold = cur.fetchone()

            if gold:
                cur.execute("""
                    SELECT grain_value AS event_type, identified AS opportunities,
                           booked, pipeline_est, revenue_won
                    FROM gold_results WHERE grain = 'event_type'
                    ORDER BY pipeline_est DESC
                """)
                return jsonify({
                    "funnel": dict(gold),
                    "by_event": [dict(r) for r in cur.fetchall()],
                    "source": "cdf_rollup",
                    "computed_at": gold["computed_at"],
                })

            cur.execute("""
                SELECT
                  count(*)                                                          AS identified,
                  count(*) FILTER (WHERE status IN ('sent','responded','booked',
                                     'quoted','won','completed'))                   AS sent,
                  count(*) FILTER (WHERE status IN ('responded','booked',
                                     'quoted','won','completed'))                   AS responded,
                  count(*) FILTER (WHERE status IN ('booked','quoted','won','completed')) AS booked,
                  count(*) FILTER (WHERE status IN ('won','completed'))             AS won,
                  COALESCE(sum(est_value) FILTER (
                      WHERE status IN ('booked','quoted','won','completed')), 0)    AS pipeline_est,
                  COALESCE(sum(est_value) FILTER (
                      WHERE status IN ('won','completed')), 0)                      AS revenue_won
                FROM opportunities
            """)
            funnel = dict(cur.fetchone())

            cur.execute("""
                SELECT w.event_type,
                       count(*)                                                     AS opportunities,
                       count(*) FILTER (WHERE o.status IN ('booked','quoted','won','completed')) AS booked,
                       COALESCE(sum(o.est_value) FILTER (
                           WHERE o.status IN ('booked','quoted','won','completed')), 0) AS pipeline_est,
                       COALESCE(sum(o.est_value) FILTER (
                           WHERE o.status IN ('won','completed')), 0)               AS revenue_won
                FROM opportunities o
                JOIN weather_events w ON w.event_id = o.weather_event_id
                GROUP BY w.event_type
                ORDER BY pipeline_est DESC
            """)
            by_event = [dict(r) for r in cur.fetchall()]

        return jsonify({"funnel": funnel, "by_event": by_event, "source": "live_query"})
    except Exception as exc:
        return _fail(exc)


@app.get("/api/cdf-trail")
def cdf_trail():
    """
    Raw change records from the Delta Change Data Feed, landed by the rollup
    job. Falls back to the Postgres status-history trigger if the rollup
    hasn't run yet -- same transitions, different capture mechanism, and the
    response says which.
    """
    try:
        with lakebase.cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT a.audit_id, a.opportunity_id, a.change_type, a.new_status,
                       a.commit_version, a.commit_ts AS changed_at,
                       c.name, w.event_type
                FROM cdf_audit a
                JOIN opportunities  o ON o.opportunity_id = a.opportunity_id
                JOIN customers      c ON c.customer_id    = o.customer_id
                JOIN weather_events w ON w.event_id       = o.weather_event_id
                ORDER BY a.commit_ts DESC, a.audit_id DESC
                LIMIT 40
            """)
            rows = [dict(r) for r in cur.fetchall()]
            if rows:
                return jsonify({"source": "delta_cdf", "rows": rows})

            cur.execute("""
                SELECT h.history_id, h.opportunity_id, h.old_status, h.new_status,
                       h.changed_at, c.name, w.event_type
                FROM opportunity_status_history h
                JOIN opportunities  o ON o.opportunity_id = h.opportunity_id
                JOIN customers      c ON c.customer_id    = o.customer_id
                JOIN weather_events w ON w.event_id       = o.weather_event_id
                ORDER BY h.changed_at DESC
                LIMIT 40
            """)
            return jsonify({"source": "status_history", "rows": [dict(r) for r in cur.fetchall()]})
    except Exception as exc:
        return _fail(exc)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
