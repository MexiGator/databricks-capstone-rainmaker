"""relationship_v0.triggers.engine — the SHARED consumer (trigger-agnostic).

Takes normalized Opportunities from ANY provider and runs them through the exact
v0.1 relationship stack: relationship_score -> policy -> care draft, with the
consent/frequency guardrails intact. Nothing here knows or cares which provider
produced an opportunity. That indifference IS the generalization.

Reuses the v0.1 helpers (`_signals_from_contact`, `_flags_from_contact`) so there
is a single source of truth for how a contact becomes signals/flags.
"""
from __future__ import annotations

from typing import Iterable, Optional

from relationship_v0.scoring import compute_relationship_score
from relationship_v0.policy import (
    Action, TemplateKind, select_next_action,
)
from relationship_v0.care_content import select_care_guide, compose_care_message
from relationship_v0.pipeline import _signals_from_contact, _flags_from_contact
from relationship_v0.triggers.base import Opportunity
from relationship_v0.triggers.registry import PROVIDER_TO_POLICY_TRIGGER, get_provider


# template kinds that need a drafted message
_WEATHER_TEMPLATES = {TemplateKind.CARE_TIP, TemplateKind.DAMAGE_CHECK}
_REMINDER_TEMPLATES = {TemplateKind.CADENCE_DUE, TemplateKind.REENGAGE}


def _compose_due_message(contact: dict, service_line: Optional[str],
                         cta_strength: str, context: dict) -> str:
    """Deterministic 'you're due' reminder — no weather guide needed. Kept in the
    v0.2 layer so the v0.1 care_content stays untouched."""
    name = (contact.get("name") or "there").split()[0]
    dsi = context.get("days_since_service")
    line = service_line or "service"
    when = f" It's been about {dsi} days since your last visit." if dsi else ""
    if cta_strength == "none":
        cta = ""
    elif cta_strength in ("medium", "strong"):
        cta = "\n\nWant us to grab your regular spot on the schedule?"
    else:
        cta = "\n\nNo pressure — just reply whenever you'd like us to swing by."
    return (f"Hi {name} — you're due for your next {line} service.{when}{cta}").strip()


def build_queue_from_opportunities(opportunities: Iterable[Opportunity],
                                   contacts_by_id: dict,
                                   pack: Optional[object] = None,
                                   include_suppressed: bool = True) -> list[dict]:
    """The shared path: Opportunity[] -> ranked care queue rows."""
    rows: list[dict] = []
    for opp in opportunities:
        c = contacts_by_id.get(opp.contact_id)
        if c is None:
            continue
        score = compute_relationship_score(_signals_from_contact(c))
        policy_trigger = PROVIDER_TO_POLICY_TRIGGER.get(opp.trigger_type)
        action: Action = select_next_action(
            score["tier"], policy_trigger, _flags_from_contact(c),
            exposure=opp.signal_strength)

        row = {
            "contact_id": opp.contact_id,
            "name": c.get("name"),
            "trigger_type": opp.trigger_type,
            "service_line": opp.service_line,
            "signal_strength": round(opp.signal_strength, 3),
            "relationship_score": score["relationship_score"],
            "tier": score["tier"],
            "action": action.as_dict(),
            "message": None,
            "guide_id": None,
        }

        # timing gate: a provider can say "right contact, wrong moment"
        if action.send and not opp.timing_ok:
            row["action"]["send"] = False
            row["action"]["reason"] = "awaiting_timing_window"

        if row["action"]["send"]:
            if action.template_kind in _WEATHER_TEMPLATES:
                event = opp.context.get("event", {})
                guide = select_care_guide(
                    opp.context.get("event_type", ""), opp.service_line)
                if guide is None:
                    row["action"]["send"] = False
                    row["action"]["reason"] = "no_matching_care_guide"
                else:
                    row["guide_id"] = guide["id"]
                    row["message"] = compose_care_message(
                        guide, c, event, cta_strength=action.cta_strength.value)
            elif action.template_kind in _REMINDER_TEMPLATES:
                row["message"] = _compose_due_message(
                    c, opp.service_line, action.cta_strength.value, opp.context)

        if row["action"]["send"] or include_suppressed:
            rows.append(row)

    rows.sort(key=lambda r: (r["action"]["send"], r["relationship_score"]),
              reverse=True)
    return rows


def run_vertical(pack, contacts: list[dict],
                 provider_contexts: Optional[dict] = None,
                 include_suppressed: bool = True) -> list[dict]:
    """Run every active provider in `pack` and merge their Opportunities through
    the shared engine. `provider_contexts` maps trigger_type -> its context
    (e.g. {"event": {"events": [...]}}). One call = a whole vertical."""
    pack.validate()
    provider_contexts = provider_contexts or {}
    contacts_by_id = {(c.get("contact_id") or c.get("customer_id")): c
                      for c in contacts}

    all_opps: list[Opportunity] = []
    for trigger_type in pack.active_providers:
        provider = get_provider(trigger_type, pack=pack)
        ctx = provider_contexts.get(trigger_type, {})
        all_opps.extend(provider.produce(contacts, context=ctx))

    return build_queue_from_opportunities(
        all_opps, contacts_by_id, pack=pack, include_suppressed=include_suppressed)
