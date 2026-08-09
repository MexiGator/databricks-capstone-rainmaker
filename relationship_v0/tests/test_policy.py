"""Tests for the warmth x trigger -> action policy."""
from relationship_v0.policy import (
    Action, ContactFlags, CtaStrength, TemplateKind, Trigger,
    can_contact, select_next_action, MAX_CARE_TOUCHES_PER_WINDOW,
)


# --- the gate ---------------------------------------------------------------

def test_opt_out_always_suppresses():
    ok, why = can_contact(ContactFlags(opted_out=True))
    assert ok is False and why == "opted_out"


def test_missing_consent_blocks_marketing_but_not_transactional():
    flags = ContactFlags(consent_ok=False)
    assert can_contact(flags, is_transactional=False)[0] is False
    assert can_contact(flags, is_transactional=True)[0] is True


def test_frequency_cap_blocks_extra_marketing():
    flags = ContactFlags(care_touches_in_window=MAX_CARE_TOUCHES_PER_WINDOW)
    ok, why = can_contact(flags)
    assert ok is False and why == "frequency_cap"
    # a confirmation they asked for still goes through
    assert can_contact(flags, is_transactional=True)[0] is True


# --- active storm -----------------------------------------------------------

def test_warm_contact_in_active_storm_gets_direct_strong_ask():
    a = select_next_action("hot", Trigger.ACTIVE_STORM, ContactFlags(), exposure=0.9)
    assert a.send and a.template_kind == TemplateKind.DIRECT_INSPECTION
    assert a.cta_strength == CtaStrength.STRONG


def test_cold_contact_in_active_storm_gets_value_first_soft_ask():
    a = select_next_action("cold", Trigger.ACTIVE_STORM, ContactFlags(), exposure=0.3)
    assert a.send and a.template_kind == TemplateKind.DAMAGE_CHECK
    assert a.cta_strength == CtaStrength.SOFT


def test_cold_contact_high_exposure_escalates_cta_to_medium():
    a = select_next_action("cold", Trigger.ACTIVE_STORM, ContactFlags(), exposure=0.8)
    assert a.template_kind == TemplateKind.DAMAGE_CHECK
    assert a.cta_strength == CtaStrength.MEDIUM


# --- forecast (proactive care) ---------------------------------------------

def test_forecast_always_produces_care_tip_with_soft_or_medium_cta():
    for tier, expected in [("hot", CtaStrength.MEDIUM), ("warm", CtaStrength.SOFT),
                           ("cool", CtaStrength.SOFT), ("cold", CtaStrength.SOFT)]:
        a = select_next_action(tier, Trigger.FORECAST, ContactFlags())
        assert a.send and a.template_kind == TemplateKind.CARE_TIP
        assert a.cta_strength == expected
        assert a.tone == "advisor"


def test_forecast_respects_frequency_cap():
    flags = ContactFlags(care_touches_in_window=MAX_CARE_TOUCHES_PER_WINDOW)
    a = select_next_action("warm", Trigger.FORECAST, flags)
    assert a.send is False and a.template_kind == TemplateKind.SUPPRESS
    assert a.reason == "frequency_cap"


# --- dormant re-engagement --------------------------------------------------

def test_dormant_valuable_warm_contact_is_re_warmed():
    flags = ContactFlags(lifetime_value=8000)
    a = select_next_action("warm", Trigger.DORMANT, flags)
    assert a.send and a.template_kind == TemplateKind.REENGAGE
    assert a.cta_strength == CtaStrength.SOFT


def test_dormant_cold_or_low_value_is_suppressed():
    assert select_next_action("cold", Trigger.DORMANT, ContactFlags(lifetime_value=9000)).send is False
    assert select_next_action("warm", Trigger.DORMANT, ContactFlags(lifetime_value=100)).send is False


# --- shape ------------------------------------------------------------------

def test_action_serializes():
    a = select_next_action("hot", Trigger.FORECAST, ContactFlags())
    d = a.as_dict()
    import json
    json.dumps(d)
    assert d["template_kind"] == "care_tip"
    assert d["trigger"] == "forecast"


def test_every_path_returns_an_action():
    for tier in ("hot", "warm", "cool", "cold"):
        for trig in Trigger:
            a = select_next_action(tier, trig, ContactFlags(lifetime_value=5000))
            assert isinstance(a, Action)
            # suppressed actions never carry a real CTA
            if not a.send:
                assert a.cta_strength == CtaStrength.NONE
