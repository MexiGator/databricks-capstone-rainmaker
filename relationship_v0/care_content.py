"""relationship_v0.care_content — the Proactive Care knowledge corpus + composer.

This is the RAG corpus for the *care* side of Rainmaker (parallel to the sales
`outreach_templates`). Each guide is real, on-brand prep advice a homeowner
actually wants BEFORE a hazard — the thing that repositions the company from
storm-chaser to trusted advisor.

Selection is deterministic-first (exact event_type/service_type lookup) with a
service_type fallback, so the demo is reliable; the embedding column on
`care_content` (see schema) gives a semantic fallback + powers the "Ask" bar
("how do I protect my pipes?"). Determinism for reliability, vectors for reach —
the same call a careful engineer makes.

Message composition is deterministic here (slot-filled) so it is fully testable
and never hallucinates. In the real repo an optional LLM polish step can smooth
the copy; keep the slot contract identical so tests still hold.
"""
from __future__ import annotations

from typing import Optional


# event_type (as it appears in NWS product names, normalized to lowercase) ->
# service_type. Extends the sales-side event_service_map with care intent.
EVENT_SERVICE_CARE = {
    "hail": "roofing",
    "severe thunderstorm": "roofing",
    "tornado": "roofing",
    "high wind": "roofing",
    "hurricane": "roofing",
    "tropical storm": "restoration",
    "flood": "restoration",
    "flash flood": "restoration",
    "heavy rain": "restoration",
    "winter storm": "plumbing",
    "ice storm": "plumbing",
    "hard freeze": "plumbing",
    "freeze": "plumbing",
    "excessive heat": "hvac",
    "heat": "hvac",
}


# One guide per hazard family. `event_types` are matched case-insensitively as
# substrings of the live NWS event name (e.g. "Excessive Heat Warning").
CARE_GUIDES = [
    {
        "id": "care_hail_roof",
        "service_type": "roofing",
        "event_types": ["hail", "severe thunderstorm"],
        "title": "Before the hail: protect your roof",
        "tips": [
            "Clear gutters and downspouts so post-storm water drains fast.",
            "Move vehicles under cover — hail dents metal and cracks skylights.",
            "After it passes, photograph your roofline and yard for your records; "
            "granules in the gutter or dented vents are early signs of roof damage.",
        ],
        "guide_url": "https://rainmaker.example/guides/hail-roof-prep",
        "soft_cta": "If you'd like peace of mind, we can do a no-cost roof check once it clears.",
    },
    {
        "id": "care_wind_roof",
        "service_type": "roofing",
        "event_types": ["high wind", "tornado", "wind"],
        "title": "High winds incoming: secure your roof and property",
        "tips": [
            "Secure or store loose outdoor items — patio furniture, trampolines, trash bins.",
            "Trim branches that overhang the roofline if you safely can.",
            "After the wind, look for lifted or missing shingles and loose flashing from the ground.",
        ],
        "guide_url": "https://rainmaker.example/guides/wind-roof-prep",
        "soft_cta": "Want us to pencil in a quick post-wind roof inspection?",
    },
    {
        "id": "care_freeze_pipes",
        "service_type": "plumbing",
        "event_types": ["hard freeze", "freeze", "winter storm", "ice storm"],
        "title": "Hard freeze coming: keep your pipes from bursting",
        "tips": [
            "Let a pencil-thin stream drip from faucets on exterior walls overnight.",
            "Open cabinet doors under sinks so warm air reaches the pipes.",
            "Disconnect garden hoses and cover outdoor spigots; know where your main shutoff is.",
        ],
        "guide_url": "https://rainmaker.example/guides/freeze-pipe-prep",
        "soft_cta": "If you'd like, we can do a quick winterization check before the cold hits.",
    },
    {
        "id": "care_heat_hvac",
        "service_type": "hvac",
        "event_types": ["excessive heat", "heat"],
        "title": "Heat wave ahead: help your AC survive the load",
        "tips": [
            "Replace your air filter — a clogged filter is the #1 cause of heat-wave AC failure.",
            "Clear leaves and debris from the outdoor condenser unit for airflow.",
            "Set the thermostat a few degrees higher when out; a system running flat-out all day is the one that quits.",
        ],
        "guide_url": "https://rainmaker.example/guides/heat-ac-prep",
        "soft_cta": "Want a pre-summer AC tune-up so it doesn't quit on the hottest day?",
    },
    {
        "id": "care_flood_restoration",
        "service_type": "restoration",
        "event_types": ["flood", "flash flood", "heavy rain", "tropical storm"],
        "title": "Heavy rain / flood risk: protect against water damage",
        "tips": [
            "Clear gutters and yard drains; make sure downspouts carry water away from the foundation.",
            "Test your sump pump and check for a battery backup before the rain arrives.",
            "Move valuables off basement floors; know how to shut off power to lower levels if water enters.",
        ],
        "guide_url": "https://rainmaker.example/guides/flood-water-prep",
        "soft_cta": "If water does get in, reply here — we can be out fast for a moisture check.",
    },
    {
        "id": "care_hurricane_roof",
        "service_type": "roofing",
        "event_types": ["hurricane"],
        "title": "Hurricane watch: get your home storm-ready",
        "tips": [
            "Photograph your roof, siding, and property now for insurance documentation.",
            "Clear gutters, secure loose items, and trim overhanging branches.",
            "Have your roofer's number handy; the fastest inspections go to homeowners who call before the rush.",
        ],
        "guide_url": "https://rainmaker.example/guides/hurricane-roof-prep",
        "soft_cta": "Want us to hold a priority inspection slot for you for after the storm?",
    },
]

_GUIDES_BY_ID = {g["id"]: g for g in CARE_GUIDES}

# Minimum similarity for a semantic (vector) fallback to be trusted; below this,
# refuse rather than send a mismatched guide (mirrors the Ask-bar guardrail).
SEMANTIC_FLOOR = 0.55


def select_care_guide(event_type: str,
                      service_type: Optional[str] = None) -> Optional[dict]:
    """Deterministic guide selection. Match the live event name against each
    guide's event_types (case-insensitive substring); fall back to service_type;
    return None if nothing matches (caller should refuse, not guess)."""
    if not event_type:
        return None
    ev = event_type.lower()
    for g in CARE_GUIDES:
        if any(et in ev for et in g["event_types"]):
            return g
    if service_type:
        for g in CARE_GUIDES:
            if g["service_type"] == service_type:
                return g
    return None


_CTA_JOINERS = {
    "soft": lambda cta: f"No pressure at all — {cta[0].lower()}{cta[1:]}",
    "medium": lambda cta: cta,
    "strong": lambda cta: cta,
}


def compose_care_message(guide: dict, contact: dict, event: dict,
                         cta_strength: str = "soft", max_tips: int = 3) -> str:
    """Slot-fill a personalized care message. Deterministic and dependency-free.

    contact: {"name": ..., "service_type": ...}
    event:   {"event_type": ..., "headline": ..., "area": ...}
    """
    first_name = (contact.get("name") or "there").split()[0]
    hazard = event.get("headline") or f"a {event.get('event_type', 'weather')} event"
    area = event.get("area")
    where = f" for {area}" if area else ""

    tips = guide["tips"][:max_tips]
    tip_lines = "\n".join(f"- {t}" for t in tips)

    if cta_strength == "none":
        cta_line = ""
    else:
        joiner = _CTA_JOINERS.get(cta_strength, _CTA_JOINERS["soft"])
        cta_line = "\n\n" + joiner(guide["soft_cta"])

    return (
        f"Hi {first_name} — there's {hazard}{where}, and we wanted to help you get ahead of it.\n\n"
        f"{guide['title']}:\n{tip_lines}\n\n"
        f"Full guide: {guide['guide_url']}"
        f"{cta_line}"
    ).strip()
