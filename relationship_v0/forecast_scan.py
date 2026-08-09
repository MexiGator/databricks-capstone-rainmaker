"""relationship_v0.forecast_scan — turn forecast weather into care opportunities (pure core).

Rainmaker's sales side fires on active alerts (damage is happening). Proactive
Care fires one step EARLIER — on forecast / watch / advisory products that carry
lead time — so the helpful message lands before the hazard, not after.

The NWS client Rainmaker already ships (`weather_client.harvest`) returns both
alerts and forecasts, so no new data source is needed. This module:
  1. filters a normalized event stream down to *proactive* (lead-time) events,
  2. matches contacts to events by area + service_type,
so the agent only ever drafts for the right people about the right hazard.

The IO (calling NWS) is intentionally isolated in `fetch_forecast_events` and
injected, so the matching logic below is pure and fully unit tested. In the real
repo, pass `weather_client.harvest(...)`-shaped records straight in.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from relationship_v0.care_content import EVENT_SERVICE_CARE


# NWS `urgency` values that imply lead time (something is coming, not gone).
PROACTIVE_URGENCIES = {"future", "expected"}
# Product-name markers that imply a watch/advisory (forecast) rather than a warning.
PROACTIVE_MARKERS = ("watch", "advisory", "outlook")
# Product-name markers that mean it's already happening (exclude from *proactive*).
IMMINENT_MARKERS = ("warning",)


def normalize_event(raw: dict) -> dict:
    """Flatten an NWS alert/forecast feature (or a harvest() record) into the
    common shape the rest of v0.1 uses. Tolerant of missing keys."""
    props = raw.get("properties", raw)  # accept raw NWS features or pre-normalized
    event_type = (props.get("event") or props.get("event_type") or "").strip()
    return {
        "event_id": props.get("id") or raw.get("id") or props.get("event_id"),
        "event_type": event_type,
        "urgency": (props.get("urgency") or "").strip().lower(),
        "severity": (props.get("severity") or "").strip().lower(),
        "headline": props.get("headline") or event_type,
        "narrative_text": props.get("description") or props.get("narrative_text") or "",
        "area": props.get("areaDesc") or props.get("area") or "",
        # affected US state abbreviations, e.g. {"TX","OK"} — from geocode/UGC in prod.
        "states": set(s.upper() for s in (props.get("states") or [])),
        "effective_at": props.get("effective") or props.get("effective_at"),
        "onset": props.get("onset"),
    }


def is_proactive_event(event: dict) -> bool:
    """True if this event carries lead time we can act on BEFORE impact."""
    name = (event.get("event_type") or "").lower()
    urgency = (event.get("urgency") or "").lower()
    if urgency in PROACTIVE_URGENCIES:
        return True
    if any(m in name for m in PROACTIVE_MARKERS):
        return True
    # A plain "warning" with immediate urgency is the sales side's job, not care.
    if any(m in name for m in IMMINENT_MARKERS) and urgency in ("immediate", "expected"):
        return urgency == "expected"  # "expected" still has some lead time
    return False


def service_for_event(event_type: str) -> Optional[str]:
    """Map a live event name to the service_type its care guide targets."""
    if not event_type:
        return None
    ev = event_type.lower()
    for key, svc in EVENT_SERVICE_CARE.items():
        if key in ev:
            return svc
    return None


def contact_in_event_area(contact: dict, event: dict) -> bool:
    """Geographic match. Default: contact's state is in the event's affected
    states. In production you can swap a point-in-polygon / per-point NWS query
    here without touching the rest of the pipeline."""
    states = event.get("states") or set()
    cstate = (contact.get("state") or "").upper()
    if states and cstate:
        return cstate in states
    # If the event carries no structured geography, fall back to a substring
    # match of the contact's city/state in the free-text area description.
    area = (event.get("area") or "").lower()
    for field in ("city", "state"):
        val = (contact.get(field) or "").lower()
        if val and val in area:
            return True
    return False


def match_contacts_to_events(contacts: Iterable[dict], events: Iterable[dict],
                             require_service_match: bool = True) -> list[dict]:
    """Return care opportunities: one row per (contact, proactive event) where
    geography matches and (optionally) the contact's service_type matches the
    hazard's target service. Deterministically sorted for stable demos/tests.
    """
    proactive = [e for e in events if is_proactive_event(e)]
    opps = []
    for e in proactive:
        svc = service_for_event(e.get("event_type", ""))
        for c in contacts:
            if not contact_in_event_area(c, e):
                continue
            if require_service_match and svc and c.get("service_type") != svc:
                continue
            opps.append({
                "contact_id": c.get("contact_id") or c.get("customer_id"),
                "contact": c,
                "event": e,
                "service_type": svc or c.get("service_type"),
                "event_type": e.get("event_type"),
            })
    opps.sort(key=lambda o: (str(o.get("event_type")), str(o.get("contact_id"))))
    return opps


# --------------------------------------------------------------------------- #
# IO boundary — isolated so the logic above stays pure and testable.
# In the real repo, prefer reusing weather_client.harvest() over this.
# --------------------------------------------------------------------------- #
def fetch_forecast_events(states: list[str],
                          http_get: Optional[Callable] = None) -> list[dict]:  # pragma: no cover
    """Thin NWS fetch for forecast/watch products across the states where you
    have contacts. Reuse Rainmaker's `weather_client` in production; this exists
    so the module is runnable standalone.

    NWS requires a User-Agent header or returns 403 (the same gotcha baked into
    the Day 2 weather_client).
    """
    import requests  # lazy import: keeps the module importable without the dep
    http_get = http_get or requests.get
    headers = {"User-Agent": "Rainmaker/0.1 (proactive-care; contact@rainmaker.example)"}
    out: list[dict] = []
    for st in states:
        url = f"https://api.weather.gov/alerts/active?area={st}&urgency=Future,Expected"
        resp = http_get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        for feat in resp.json().get("features", []):
            ev = normalize_event(feat)
            if not ev["states"]:
                ev["states"] = {st.upper()}
            out.append(ev)
    return out
