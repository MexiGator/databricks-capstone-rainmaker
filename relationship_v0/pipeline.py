"""relationship_v0.pipeline — the one function that ties v0.1 together (pure).

Given contacts (with CRM signals) + a normalized weather-event stream + the
trigger context, produce the ranked Proactive Care queue: for each matched
contact, the warmth score, the chosen action, and the drafted message —
suppressions included, with reasons, so the analyst sees what was held back
and why. This is the object the Flask route and the Agent tool both build on.
"""
from __future__ import annotations

from typing import Iterable, Optional

from relationship_v0.scoring import ContactSignals, compute_relationship_score
from relationship_v0.policy import (
    Action, ContactFlags, Trigger, TemplateKind, select_next_action,
)
from relationship_v0.care_content import select_care_guide, compose_care_message
from relationship_v0.forecast_scan import match_contacts_to_events


def _signals_from_contact(c: dict) -> ContactSignals:
    return ContactSignals(
        days_since_last_touch=c.get("days_since_last_touch"),
        opens=c.get("opens", 0),
        clicks=c.get("clicks", 0),
        positive_replies=c.get("positive_replies", 0),
        negative_events=c.get("negative_events", 0),
        avg_sentiment=c.get("avg_sentiment"),
        tenure_years=c.get("tenure_years"),
        completed_jobs=c.get("completed_jobs", 0),
        consent_ok=c.get("consent_ok", True),
        opted_out=c.get("opted_out", False),
        recent_care_touches=c.get("recent_care_touches", 0),
        is_prospect=c.get("is_prospect", False),
    )


def _flags_from_contact(c: dict) -> ContactFlags:
    return ContactFlags(
        consent_ok=c.get("consent_ok", True),
        opted_out=c.get("opted_out", False),
        care_touches_in_window=c.get("recent_care_touches", 0),
        lifetime_value=c.get("lifetime_value", 0.0),
    )


def build_care_queue(contacts: Iterable[dict], events: Iterable[dict],
                     trigger: Trigger = Trigger.FORECAST,
                     include_suppressed: bool = True) -> list[dict]:
    """Return the Proactive Care queue rows, ranked by warmth (hottest first,
    since a warm contact is the most likely to convert to a booked inspection)."""
    contacts = list(contacts)
    by_id = {(c.get("contact_id") or c.get("customer_id")): c for c in contacts}
    opps = match_contacts_to_events(contacts, events)

    rows: list[dict] = []
    for opp in opps:
        c = by_id.get(opp["contact_id"], opp["contact"])
        score = compute_relationship_score(_signals_from_contact(c))
        action: Action = select_next_action(
            score["tier"], trigger, _flags_from_contact(c),
            exposure=c.get("exposure", 0.0))

        row = {
            "contact_id": opp["contact_id"],
            "name": c.get("name"),
            "service_type": opp["service_type"],
            "event_type": opp["event_type"],
            "relationship_score": score["relationship_score"],
            "tier": score["tier"],
            "action": action.as_dict(),
            "message": None,
            "guide_id": None,
        }

        if action.send and action.template_kind in (
                TemplateKind.CARE_TIP, TemplateKind.DAMAGE_CHECK):
            guide = select_care_guide(opp["event_type"], opp["service_type"])
            if guide is None:
                row["action"]["send"] = False
                row["action"]["reason"] = "no_matching_care_guide"
            else:
                row["guide_id"] = guide["id"]
                row["message"] = compose_care_message(
                    guide, c, opp["event"], cta_strength=action.cta_strength.value)

        if row["action"]["send"] or include_suppressed:
            rows.append(row)

    # hottest first; suppressed rows sink to the bottom
    rows.sort(key=lambda r: (r["action"]["send"], r["relationship_score"]),
              reverse=True)
    return rows
