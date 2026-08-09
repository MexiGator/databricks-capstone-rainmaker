"""Tests for care content selection + message composition."""
from relationship_v0 import care_content as cc
from relationship_v0.care_content import select_care_guide, compose_care_message


def test_every_guide_is_well_formed():
    ids = set()
    for g in cc.CARE_GUIDES:
        assert g["id"] not in ids, "duplicate guide id"
        ids.add(g["id"])
        assert g["service_type"]
        assert g["event_types"] and all(isinstance(e, str) for e in g["event_types"])
        assert len(g["tips"]) >= 2
        assert g["soft_cta"] and g["guide_url"].startswith("http")


def test_live_event_name_matches_by_substring():
    g = select_care_guide("Excessive Heat Warning")
    assert g is not None and g["service_type"] == "hvac"
    g2 = select_care_guide("Hard Freeze Warning")
    assert g2["service_type"] == "plumbing"
    g3 = select_care_guide("Hail / Severe Thunderstorm Watch")
    assert g3["service_type"] == "roofing"


def test_service_type_fallback():
    # An event name we don't recognize, but we know the customer is HVAC.
    g = select_care_guide("Air Quality Alert", service_type="hvac")
    assert g is not None and g["service_type"] == "hvac"


def test_no_match_returns_none():
    assert select_care_guide("Dense Fog Advisory") is None
    assert select_care_guide("") is None


def test_compose_personalizes_and_includes_tips():
    g = select_care_guide("Hard Freeze Warning")
    msg = compose_care_message(
        g,
        contact={"name": "Maria Lopez", "service_type": "plumbing"},
        event={"event_type": "Hard Freeze", "headline": "a Hard Freeze Warning", "area": "Travis County"},
        cta_strength="soft",
    )
    assert "Maria" in msg          # first-name personalization
    assert "Travis County" in msg  # area personalization
    assert g["tips"][0] in msg     # grounded in the retrieved guide
    assert g["guide_url"] in msg


def test_cta_strength_changes_the_ask():
    g = select_care_guide("Excessive Heat Warning")
    base = dict(contact={"name": "Sam"}, event={"event_type": "Excessive Heat"})
    none_msg = compose_care_message(g, cta_strength="none", **base)
    soft_msg = compose_care_message(g, cta_strength="soft", **base)
    strong_msg = compose_care_message(g, cta_strength="strong", **base)
    assert g["soft_cta"] not in none_msg          # suppressed CTA => no ask
    assert "No pressure" in soft_msg              # soft joiner softens it
    assert g["soft_cta"] in strong_msg            # strong => the ask as-is


def test_compose_handles_missing_name_gracefully():
    g = cc.CARE_GUIDES[0]
    msg = compose_care_message(g, contact={}, event={"event_type": "Hail"})
    assert "Hi there" in msg
