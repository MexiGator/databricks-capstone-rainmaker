"""v0.2 tests — the trigger-provider abstraction.

The headline test is `test_event_provider_equivalent_to_v01_pipeline`: it proves
refactoring roofing/weather behind the provider interface changed NOTHING about
the decisions or drafts. The rest prove a second, non-weather trigger type
(cadence) flows through the identical shared engine + guardrails.
"""
import pytest

from relationship_v0.policy import (
    ContactFlags, Trigger, TemplateKind, CtaStrength, select_next_action,
)
from relationship_v0.pipeline import build_care_queue
from relationship_v0.triggers.base import Opportunity, TRIGGER_TYPES
from relationship_v0.triggers.event import EventProvider
from relationship_v0.triggers.cadence import CadenceProvider
from relationship_v0.triggers.cumulative_threshold import (
    CumulativeThresholdProvider, accumulate_soiling,
)
from relationship_v0.triggers.engine import (
    build_queue_from_opportunities, run_vertical,
)
from relationship_v0.packs.base import VerticalPack
from relationship_v0.packs.roofing import ROOFING_PACK


EVENTS = [
    {"event_type": "Hard Freeze Watch", "urgency": "future", "states": {"MN"},
     "headline": "a Hard Freeze Watch", "area": "Hennepin County, MN"},
    {"event_type": "Excessive Heat Watch", "urgency": "future", "states": {"AZ"},
     "headline": "an Excessive Heat Watch", "area": "Maricopa County, AZ"},
]
CONTACTS = [
    {"contact_id": 1, "name": "Maria Lopez", "state": "MN", "service_type": "plumbing",
     "days_since_last_touch": 20, "opens": 5, "clicks": 2, "positive_replies": 1,
     "avg_sentiment": 0.6, "tenure_years": 6, "completed_jobs": 3, "lifetime_value": 9000},
    {"contact_id": 2, "name": "Dev Patel", "state": "AZ", "service_type": "hvac",
     "is_prospect": True, "lifetime_value": 0},
    {"contact_id": 3, "name": "Bill Ray", "state": "MN", "service_type": "plumbing",
     "opted_out": True, "tenure_years": 8, "completed_jobs": 5, "lifetime_value": 12000},
]


# --- Opportunity contract ---------------------------------------------------

def test_opportunity_validates_and_clamps():
    o = Opportunity(contact_id=1, trigger_type="event", service_line="roofing",
                    signal_strength=5.0)
    assert o.signal_strength == 1.0          # clamped to [0,1]
    with pytest.raises(ValueError):
        Opportunity(contact_id=1, trigger_type="nonsense", service_line=None)
    assert set(TRIGGER_TYPES) == {"event", "cadence", "cumulative_threshold"}


# --- THE headline: refactor didn't change weather behavior ------------------

def _key(rows):
    out = {}
    for r in rows:
        out[r["contact_id"]] = (
            r["action"]["send"], r["action"]["template_kind"],
            r["action"]["cta_strength"], r["message"], r["tier"])
    return out


def test_event_provider_equivalent_to_v01_pipeline():
    # v0.1 path
    v01 = build_care_queue(CONTACTS, EVENTS, trigger=Trigger.FORECAST)
    # v0.2 path: same events, through the provider + shared engine
    pack = VerticalPack(key="t", service_lines=["plumbing", "hvac"],
                        active_providers=["event"])
    v02 = run_vertical(pack, CONTACTS, {"event": {"events": EVENTS}})
    assert _key(v01) == _key(v02)            # identical decisions AND drafts


# --- EventProvider ----------------------------------------------------------

def test_event_provider_emits_normalized_opportunities():
    opps = EventProvider().produce(CONTACTS, {"events": EVENTS})
    assert all(o.trigger_type == "event" for o in opps)
    ids = sorted(o.contact_id for o in opps)
    assert 1 in ids and 2 in ids             # Maria (freeze) + Dev (heat)
    assert all(0.0 <= o.signal_strength <= 1.0 for o in opps)


# --- CadenceProvider (the new trigger type) ---------------------------------

def test_cadence_emits_only_for_overdue_contacts():
    pack = VerticalPack(key="win", service_lines=["windows"],
                        active_providers=["cadence"],
                        cadence_config={"default_interval_days": 120})
    contacts = [
        {"contact_id": 10, "name": "A", "service_type": "windows", "days_since_service": 200},
        {"contact_id": 11, "name": "B", "service_type": "windows", "days_since_service": 30},
        {"contact_id": 12, "name": "C", "service_type": "windows"},  # no history
    ]
    opps = CadenceProvider(pack=pack).produce(contacts)
    assert [o.contact_id for o in opps] == [10]
    assert opps[0].trigger_type == "cadence"
    assert opps[0].signal_strength == pytest.approx((200 - 120) / 120, abs=1e-6)


def test_cadence_uses_per_service_line_interval():
    pack = VerticalPack(key="ext", service_lines=["gutters"], active_providers=["cadence"],
                        cadence_config={"default_interval_days": 120,
                                        "service_line_intervals": {"gutters": 180}})
    contacts = [{"contact_id": 1, "name": "G", "service_type": "gutters",
                 "days_since_service": 150}]  # due under 120 but NOT under 180
    assert CadenceProvider(pack=pack).produce(contacts) == []


# --- policy CADENCE branch (additive; v0.1 policy tests still pass) ----------

def test_policy_cadence_sends_reminder_and_respects_gate():
    a = select_next_action("cool", Trigger.CADENCE, ContactFlags())
    assert a.send and a.template_kind == TemplateKind.CADENCE_DUE
    held = select_next_action("hot", Trigger.CADENCE, ContactFlags(opted_out=True))
    assert held.send is False and held.template_kind == TemplateKind.SUPPRESS


# --- shared engine: cadence drafts a due message, timing gate holds ----------

def test_engine_drafts_due_message_for_cadence():
    contacts = [{"contact_id": 5, "name": "Sam Okafor", "service_type": "windows",
                 "days_since_service": 200, "opens": 3, "positive_replies": 1,
                 "avg_sentiment": 0.5, "tenure_years": 2, "completed_jobs": 2}]
    opp = Opportunity(5, "cadence", "windows", signal_strength=0.6,
                      context={"days_since_service": 200})
    rows = build_queue_from_opportunities([opp], {5: contacts[0]})
    assert rows[0]["action"]["send"] is True
    assert "Sam" in rows[0]["message"] and "due" in rows[0]["message"].lower()


def test_timing_gate_holds_a_ready_send():
    contact = {"contact_id": 7, "name": "Rae", "service_type": "windows",
               "days_since_service": 200}
    opp = Opportunity(7, "cadence", "windows", signal_strength=0.6, timing_ok=False,
                      context={"days_since_service": 200})
    rows = build_queue_from_opportunities([opp], {7: contact})
    assert rows[0]["action"]["send"] is False
    assert rows[0]["action"]["reason"] == "awaiting_timing_window"


# --- run_vertical: TWO trigger types, ONE engine ----------------------------

def test_roofing_pack_runs_event_and_cadence_through_one_engine():
    contacts = [
        # in a hail path AND overdue for the annual check -> both triggers fire
        {"contact_id": 100, "name": "Jo Kim", "state": "TX", "service_type": "roofing",
         "days_since_service": 400, "opens": 4, "positive_replies": 1,
         "avg_sentiment": 0.5, "tenure_years": 4, "completed_jobs": 2, "lifetime_value": 9000},
        # opted-out -> held on BOTH paths
        {"contact_id": 101, "name": "No Thanks", "state": "TX", "service_type": "roofing",
         "days_since_service": 500, "opted_out": True, "lifetime_value": 15000},
    ]
    events = [{"event_type": "Severe Thunderstorm Watch", "urgency": "future",
               "states": {"TX"}, "headline": "a Severe Thunderstorm Watch",
               "area": "Dallas County, TX"}]
    rows = run_vertical(ROOFING_PACK, contacts, {"event": {"events": events}})
    types = {r["trigger_type"] for r in rows}
    assert types == {"event", "cadence"}                 # both physics, one engine
    # opted-out contact is held on every row it appears in
    assert all(r["action"]["send"] is False
               for r in rows if r["contact_id"] == 101)
    # the engaged contact gets at least one real send
    assert any(r["action"]["send"] for r in rows if r["contact_id"] == 100)


# --- pack validation + Phase 2 interface ------------------------------------

def test_pack_rejects_unknown_provider():
    with pytest.raises(ValueError):
        VerticalPack(key="bad", active_providers=["telepathy"]).validate()


def test_cumulative_threshold_is_designed_but_deferred():
    with pytest.raises(NotImplementedError):
        CumulativeThresholdProvider().produce([], {})


def test_soiling_accumulator_is_bounded_and_monotonic():
    lo = accumulate_soiling(days_since_service=5, pollen_days=0)
    hi = accumulate_soiling(days_since_service=120, pollen_days=30,
                            dust_load=1.0, rain_after_pollen_events=3)
    assert 0.0 <= lo <= hi <= 1.0
    # more pollen -> more soiling, all else equal
    assert (accumulate_soiling(30, pollen_days=20)
            > accumulate_soiling(30, pollen_days=0))
