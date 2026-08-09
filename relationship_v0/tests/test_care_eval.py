"""
Care-message eval (brief §8) -- the "quality tests" edge, mirroring the Ask-bar
RAG eval. Pure and dependency-free, so it runs in the same suite as the other
41 unit tests.

Three properties, on a curated set of (live NWS event name, service) cases:
  1. RETRIEVAL  -- the right care guide is selected (deterministic hit-rate).
  2. GROUNDING  -- the drafted message uses ONLY that guide's facts (its tips,
                   title, url, cta) and never leaks another guide's content.
  3. REFUSAL    -- when no guide matches, selection returns None (we refuse
                   rather than send a mismatched tip).
"""

import pytest

from relationship_v0.care_content import (
    CARE_GUIDES, select_care_guide, compose_care_message,
)

# (live NWS product name as it really appears, service_type, expected guide id)
RETRIEVAL_CASES = [
    ("Large Hail Warning",           "roofing",     "care_hail_roof"),
    ("Severe Thunderstorm Warning",  "roofing",     "care_hail_roof"),
    ("High Wind Watch",              "roofing",     "care_wind_roof"),
    ("Tornado Watch",                "roofing",     "care_wind_roof"),
    ("Hard Freeze Warning",          "plumbing",    "care_freeze_pipes"),
    ("Winter Storm Watch",           "plumbing",    "care_freeze_pipes"),
    ("Ice Storm Warning",            "plumbing",    "care_freeze_pipes"),
    ("Excessive Heat Watch",         "hvac",        "care_heat_hvac"),
    ("Heat Advisory",                "hvac",        "care_heat_hvac"),
    ("Flash Flood Watch",            "restoration", "care_flood_restoration"),
    ("Flood Watch",                  "restoration", "care_flood_restoration"),
    ("Hurricane Watch",              "roofing",     "care_hurricane_roof"),
]

# Events with no care guide -- selection must refuse (return None).
REFUSAL_CASES = [
    "Dense Fog Advisory",
    "Air Quality Alert",
    "Special Weather Statement",
    "",
]

_CONTACT = {"name": "Sam Rivera", "service_type": "roofing"}
_GUIDES_BY_ID = {g["id"]: g for g in CARE_GUIDES}


# --- 1. retrieval ----------------------------------------------------------- #
@pytest.mark.parametrize("event_name,service,expected", RETRIEVAL_CASES)
def test_retrieval_selects_the_right_guide(event_name, service, expected):
    guide = select_care_guide(event_name, service)
    assert guide is not None, f"no guide for {event_name!r}"
    assert guide["id"] == expected, f"{event_name!r} -> {guide['id']}, expected {expected}"


def test_retrieval_hit_rate_is_100pct_on_the_curated_set():
    hits = sum(
        1 for name, svc, exp in RETRIEVAL_CASES
        if (select_care_guide(name, svc) or {}).get("id") == exp
    )
    assert hits == len(RETRIEVAL_CASES), f"hit-rate {hits}/{len(RETRIEVAL_CASES)}"


# --- 2. grounding ----------------------------------------------------------- #
@pytest.mark.parametrize("event_name,service,expected", RETRIEVAL_CASES)
def test_message_is_grounded_only_in_the_selected_guide(event_name, service, expected):
    guide = _GUIDES_BY_ID[expected]
    event = {"event_type": event_name, "headline": event_name, "area": "Dallas, TX"}
    msg = compose_care_message(guide, _CONTACT, event, cta_strength="soft")

    # uses THIS guide's facts: title, url, and each rendered tip come from it
    assert guide["title"] in msg
    assert guide["guide_url"] in msg
    for tip in guide["tips"][:3]:
        assert tip in msg, "a rendered tip is not from the guide"

    # never leaks another guide's URL (unique per guide -> a clean leak probe)
    for other in CARE_GUIDES:
        if other["id"] != guide["id"]:
            assert other["guide_url"] not in msg, f"leaked {other['id']} url"


def test_refusal_via_none_cta_emits_no_call_to_action():
    guide = _GUIDES_BY_ID["care_freeze_pipes"]
    event = {"event_type": "Hard Freeze Warning", "headline": "Hard Freeze", "area": "Dallas, TX"}
    msg = compose_care_message(guide, _CONTACT, event, cta_strength="none")
    assert guide["soft_cta"] not in msg  # suppressed contact still gets no ask


# --- 3. refusal ------------------------------------------------------------- #
@pytest.mark.parametrize("event_name", REFUSAL_CASES)
def test_no_guide_match_refuses(event_name):
    # service_type=None so there's no fallback: a true no-match must return None.
    assert select_care_guide(event_name, None) is None
