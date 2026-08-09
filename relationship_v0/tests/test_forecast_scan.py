"""Tests for forecast-event filtering + contact matching."""
from relationship_v0 import forecast_scan as fs
from relationship_v0.forecast_scan import (
    is_proactive_event, service_for_event, contact_in_event_area,
    match_contacts_to_events, normalize_event,
)


def _ev(**kw):
    base = {"event_type": "", "urgency": "", "states": set(), "area": ""}
    base.update(kw)
    return base


def test_normalize_accepts_raw_nws_feature():
    raw = {"properties": {"event": "Hard Freeze Watch", "urgency": "Future",
                          "severity": "Moderate", "areaDesc": "Travis, TX",
                          "id": "abc"}}
    ev = normalize_event(raw)
    assert ev["event_type"] == "Hard Freeze Watch"
    assert ev["urgency"] == "future"
    assert ev["event_id"] == "abc"


def test_future_urgency_is_proactive():
    assert is_proactive_event(_ev(event_type="Hard Freeze Warning", urgency="future"))


def test_watch_is_proactive_even_without_urgency():
    assert is_proactive_event(_ev(event_type="Severe Thunderstorm Watch"))
    assert is_proactive_event(_ev(event_type="Winter Storm Watch"))


def test_immediate_warning_is_not_proactive():
    # already-happening -> the sales side handles it, not care
    assert not is_proactive_event(_ev(event_type="Tornado Warning", urgency="immediate"))


def test_service_mapping():
    assert service_for_event("Excessive Heat Warning") == "hvac"
    assert service_for_event("Hard Freeze Watch") == "plumbing"
    assert service_for_event("Hail Storm") == "roofing"
    assert service_for_event("Dense Fog Advisory") is None


def test_area_match_by_state():
    ev = _ev(states={"TX", "OK"})
    assert contact_in_event_area({"state": "TX"}, ev)
    assert not contact_in_event_area({"state": "FL"}, ev)


def test_area_match_by_text_fallback():
    ev = _ev(area="Travis County, TX")
    assert contact_in_event_area({"city": "Travis"}, ev)
    assert not contact_in_event_area({"city": "Miami"}, ev)


def test_match_pairs_right_contacts_with_right_hazard():
    events = [
        _ev(event_type="Excessive Heat Watch", urgency="future", states={"AZ"}),
        _ev(event_type="Hard Freeze Warning", urgency="future", states={"MN"}),
        _ev(event_type="Tornado Warning", urgency="immediate", states={"OK"}),  # not proactive
    ]
    contacts = [
        {"contact_id": 1, "state": "AZ", "service_type": "hvac"},      # -> heat
        {"contact_id": 2, "state": "MN", "service_type": "plumbing"},  # -> freeze
        {"contact_id": 3, "state": "AZ", "service_type": "roofing"},   # wrong service, dropped
        {"contact_id": 4, "state": "OK", "service_type": "roofing"},   # only imminent event, dropped
    ]
    opps = match_contacts_to_events(contacts, events)
    ids = sorted(o["contact_id"] for o in opps)
    assert ids == [1, 2]
    by_id = {o["contact_id"]: o for o in opps}
    assert by_id[1]["service_type"] == "hvac"
    assert by_id[2]["service_type"] == "plumbing"


def test_match_is_deterministic():
    events = [_ev(event_type="Excessive Heat Watch", urgency="future", states={"AZ"})]
    contacts = [{"contact_id": 9, "state": "AZ", "service_type": "hvac"},
                {"contact_id": 2, "state": "AZ", "service_type": "hvac"}]
    a = match_contacts_to_events(contacts, events)
    b = match_contacts_to_events(list(reversed(contacts)), events)
    assert [o["contact_id"] for o in a] == [o["contact_id"] for o in b]
