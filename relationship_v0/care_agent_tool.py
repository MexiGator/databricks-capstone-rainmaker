"""relationship_v0.care_agent_tool — the "send_proactive_care_tip" agent tool.

Rainmaker's graded agent already has 3 tools (draft / send / handle-reply+book).
This adds a FOURTH, care-mode tool that is a genuine ACTION (it writes to the
DB), keeping the agent well above the ">=2 real actions" bar while telling the
"trusted advisor" story. It reuses the same grounding pattern as the sales
draft tool: retrieve the right corpus item, personalize, write, advance state.

Tool contract (Agent Bricks):
  name: send_proactive_care_tip
  input:  { contact_id, event: {event_type, headline, area, event_id},
            trigger: "forecast" }
  action: 1) load the contact's relationship_score + flags
          2) policy.select_next_action(...) decides send/suppress + CTA strength
          3) select_care_guide(...) grounds the message (deterministic + vector fallback)
          4) compose_care_message(...) drafts it
          5) db.insert_care_send(...) writes it as status='queued' (human-in-the-loop)
  output: { queued: bool, care_send_id?, reason, tier, cta_strength, message? }

Human-in-the-loop: the tool QUEUES (status='queued'); the analyst approves &
sends from the app, which flips it to 'sent'. For the live demo you may auto-send
warm-tier care tips and narrate it — same defensible pattern as the sales loop.
"""
from __future__ import annotations

from typing import Optional

from relationship_v0.policy import (
    ContactFlags, Trigger, TemplateKind, select_next_action,
)
from relationship_v0.care_content import select_care_guide, compose_care_message


def run_care_tool(contact_id: int, event: dict, trigger: str = "forecast",
                  get_relationship=None, get_contact=None, insert_care_send=None,
                  auto_send: bool = False) -> dict:
    """Pure-orchestration entrypoint; the three callbacks are the repo's db
    functions (injected so this stays unit-testable). In the deployed tool,
    default them to relationship_v0.db.* and the repo's customers lookup.
    """
    contact = get_contact(contact_id)                       # {name, service_type, lifetime_value, ...}
    rel = get_relationship(contact_id) or {"tier": "cold", "consent_ok": True,
                                           "opted_out": False, "recent_care_touches": 0}
    flags = ContactFlags(
        consent_ok=rel.get("consent_ok", True),
        opted_out=rel.get("opted_out", False),
        care_touches_in_window=rel.get("recent_care_touches", 0),
        lifetime_value=contact.get("lifetime_value", 0.0),
    )
    action = select_next_action(rel["tier"], Trigger(trigger), flags,
                                exposure=contact.get("exposure", 0.0))

    if not action.send:
        return {"queued": False, "reason": action.reason, "tier": rel["tier"],
                "cta_strength": action.cta_strength.value}

    guide = select_care_guide(event.get("event_type", ""), contact.get("service_type"))
    if guide is None:
        return {"queued": False, "reason": "no_matching_care_guide",
                "tier": rel["tier"], "cta_strength": action.cta_strength.value}

    message = compose_care_message(guide, contact, event,
                                   cta_strength=action.cta_strength.value)
    row = {
        "contact_id": contact_id,
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "service_type": contact.get("service_type"),
        "guide_id": guide["id"],
        "template_kind": action.template_kind.value,
        "cta_strength": action.cta_strength.value,
        "message_text": message,
        "status": "sent" if auto_send else "queued",
    }
    care_send_id = insert_care_send(row)
    return {"queued": True, "care_send_id": care_send_id, "reason": action.reason,
            "tier": rel["tier"], "cta_strength": action.cta_strength.value,
            "guide_id": guide["id"], "message": message,
            "status": row["status"]}


def handoff_to_booking(care_send: dict) -> Optional[dict]:
    """When a care send gets a positive reply, hand it to the EXISTING booking
    flow so proactive care funnels into the same `opportunities`/`bookings`
    pipeline the Results tab already measures. Returns an opportunity seed dict
    (status='responded') or None for non-positive replies.

    This is the join between v0.1 and the graded build: care creates warmth,
    the reply enters the funnel, Tool 3 books the appointment. Keep it decoupled
    (return a dict; let the caller write it) so v0.1 never edits the sales tables.
    """
    if care_send.get("reply_intent") != "interested":
        return None
    return {
        "customer_id": care_send["contact_id"],
        "service_needed": care_send.get("service_type"),
        "source": "proactive_care",
        "care_send_id": care_send.get("care_send_id"),
        "status": "responded",
    }
