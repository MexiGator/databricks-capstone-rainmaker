"""
Rainmaker v0.1 -- the 4th agent tool: `send_proactive_care_tip`.

The graded agent already has three real-action tools (safety notice, draft,
send+book). This adds a FOURTH that is also a genuine WRITE: it drafts a
forecast-triggered Proactive Care message and QUEUES it (human-in-the-loop) in
the v0.1 `care_sends` table. It keeps the agent well above the ">=2 real
actions" bar and tells the "trusted advisor" story.

Isolation: this module lives in agent/ but only ever writes to v0.1 tables
(care_sends) via relationship_v0.db. It is imported nowhere unless the
relationship layer is used, so with ENABLE_RELATIONSHIP_V0 unset the graded
agent is unchanged. handoff_to_booking stays decoupled -- it RETURNS an
opportunity seed dict; v0.1 never writes the graded sales tables itself.

The orchestration is the tested-pure `relationship_v0.care_agent_tool.run_care_tool`;
this file only injects the repo's real db callbacks.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Resolve lakebase.py and relationship_v0 from THIS file regardless of cwd.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "db")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from relationship_v0 import care_agent_tool, db as rel_db


# --- repo-specific callbacks ------------------------------------------------ #
def get_contact(contact_id: str) -> dict:
    """Load the CRM fields the care tool personalizes on. Proactive Care fires
    on a forecast (no active storm), so exposure defaults to 0.0."""
    import lakebase
    with lakebase.cursor(dict_rows=True) as cur:
        cur.execute(
            "SELECT customer_id, name, service_type, lifetime_value "
            "FROM customers WHERE customer_id = %s",
            (contact_id,),
        )
        row = cur.fetchone()
    if not row:
        return {}
    r = dict(row)
    return {
        "name": r["name"],
        "service_type": r["service_type"],
        "lifetime_value": float(r["lifetime_value"] or 0.0),
        "exposure": 0.0,
    }


# --- the tool itself -------------------------------------------------------- #
def send_proactive_care_tip(contact_id: str, event: dict,
                            trigger: str = "forecast",
                            auto_send: bool = False) -> dict:
    """Agent Bricks Tool #4. Decides send/suppress via the tested policy, grounds
    the draft in the right care guide, and writes a `care_sends` row (queued, so
    the analyst approves & sends). Returns the tool result dict."""
    return care_agent_tool.run_care_tool(
        contact_id, event, trigger=trigger,
        get_relationship=rel_db.get_relationship,
        get_contact=get_contact,
        insert_care_send=rel_db.insert_care_send,
        auto_send=auto_send,
    )


# --- reply -> booking hand-off ---------------------------------------------- #
def record_care_reply(care_send_id: int, reply_text: str,
                      reply_intent: str) -> dict:
    """Log a customer's reply on the care send, and -- if they're interested --
    produce the opportunity seed that funnels into the EXISTING booking pipeline.

    Decoupled by design: this advances the v0.1 care_sends row and returns the
    seed. The analyst pushes the seed into the graded booking flow from the app
    (Tool 3), so v0.1 never writes the opportunities/bookings tables itself.
    """
    import lakebase
    rel_db.advance_care_send(care_send_id, status="replied",
                             reply_text=reply_text, reply_intent=reply_intent)

    # Pull the fields handoff_to_booking needs to build the seed.
    with lakebase.cursor(dict_rows=True) as cur:
        cur.execute(
            "SELECT contact_id, service_type FROM care_sends WHERE care_send_id = %s",
            (care_send_id,),
        )
        row = cur.fetchone()

    care_send = {
        "care_send_id": care_send_id,
        "reply_intent": reply_intent,
        "contact_id": (row or {}).get("contact_id"),
        "service_type": (row or {}).get("service_type"),
    }
    seed = care_agent_tool.handoff_to_booking(care_send)
    return {"care_send_id": care_send_id, "reply_intent": reply_intent,
            "opportunity_seed": seed}


# Agent Bricks tool contract (name/description/params) for registration.
# Register this alongside the existing three tools; see AGENT_BRICKS_TOOL.md.
TOOL_SPEC = {
    "name": "send_proactive_care_tip",
    "description": (
        "Send a forecast-triggered Proactive Care tip to a contact before a "
        "weather hazard. Uses the contact's relationship warmth to pick the "
        "CTA strength, grounds the message in an approved care guide, and "
        "queues it for analyst approval. A real write action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string",
                           "description": "customers.customer_id, e.g. 'cust_0001'"},
            "event": {
                "type": "object",
                "description": "The forecast event.",
                "properties": {
                    "event_type": {"type": "string"},
                    "headline": {"type": "string"},
                    "area": {"type": "string"},
                    "event_id": {"type": "string"},
                },
                "required": ["event_type"],
            },
            "trigger": {"type": "string", "enum": ["forecast", "dormant"],
                        "default": "forecast"},
        },
        "required": ["contact_id", "event"],
    },
}
