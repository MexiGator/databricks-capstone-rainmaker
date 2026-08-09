"""relationship_v0.triggers.event — the EventProvider (the EXISTING v0.1 trigger,
now expressed behind the provider interface).

This wraps `forecast_scan.match_contacts_to_events` WITHOUT modifying it, so the
roofing/weather behavior is byte-for-byte the v0.1 behavior — proven by the
equivalence test in tests/test_triggers_v02.py. This is the "we refactored the
seam, not the logic" guarantee.
"""
from __future__ import annotations

from typing import Optional

from relationship_v0.triggers.base import Opportunity, TriggerProvider
from relationship_v0.forecast_scan import match_contacts_to_events


class EventProvider(TriggerProvider):
    trigger_type = "event"

    def produce(self, contacts: list[dict], context: Optional[dict] = None
                ) -> list[Opportunity]:
        context = context or {}
        events = context.get("events", [])
        require_service = context.get("require_service_match", True)
        matches = match_contacts_to_events(contacts, events,
                                           require_service_match=require_service)
        opps = []
        for m in matches:
            c = m.get("contact", {})
            opps.append(Opportunity(
                contact_id=m["contact_id"],
                trigger_type="event",
                service_line=m.get("service_type"),
                # reuse the same exposure the v0.1 pipeline used (contact.exposure)
                signal_strength=c.get("exposure", 0.0),
                timing_ok=True,  # weather events are act-now; timing gate is trivial
                reason=f"event:{m.get('event_type')}",
                context={"event": m.get("event"), "event_type": m.get("event_type")},
            ))
        return opps
