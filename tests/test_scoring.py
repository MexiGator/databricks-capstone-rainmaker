"""
Tests for the scoring layer.

These cover the decisions that actually change who a business calls first.
Run: pytest -q
"""

import math

import pytest

from pipeline import scoring


# ---------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------
def test_haversine_zero_distance():
    assert scoring.haversine_km(32.7767, -96.7970, 32.7767, -96.7970) == 0.0


def test_haversine_known_distance_dallas_to_okc():
    # Dallas -> Oklahoma City is roughly 330 km.
    d = scoring.haversine_km(32.7767, -96.7970, 35.4676, -97.5164)
    assert 300 < d < 360


def test_haversine_is_symmetric():
    a = scoring.haversine_km(41.8781, -87.6298, 44.9778, -93.2650)
    b = scoring.haversine_km(44.9778, -93.2650, 41.8781, -87.6298)
    assert math.isclose(a, b, rel_tol=1e-9)


# ---------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------
def test_severity_ordering_is_monotonic():
    f = scoring.severity_factor
    assert f("Extreme") > f("Severe") > f("Moderate") > f("Minor")


def test_unknown_severity_falls_back_not_crashes():
    assert scoring.severity_factor(None) == scoring.DEFAULT_SEVERITY
    assert scoring.severity_factor("Bogus") == scoring.DEFAULT_SEVERITY


# ---------------------------------------------------------------------
# Proximity -- the gradient, not a hard boundary
# ---------------------------------------------------------------------
def test_proximity_is_max_at_footprint_centre():
    assert scoring.proximity_factor(0.0, 60.0) == 1.0


def test_proximity_is_half_at_footprint_edge():
    assert scoring.proximity_factor(60.0, 60.0) == pytest.approx(0.5)


def test_proximity_decays_outside_footprint_rather_than_cliff():
    just_outside = scoring.proximity_factor(70.0, 60.0)
    assert 0 < just_outside < 0.5


def test_proximity_is_zero_far_outside():
    assert scoring.proximity_factor(500.0, 60.0) == 0.0


def test_proximity_decreases_monotonically_with_distance():
    values = [scoring.proximity_factor(d, 60.0) for d in range(0, 200, 10)]
    assert all(a >= b for a, b in zip(values, values[1:]))


def test_zone_alert_in_the_same_state_scores_well():
    """No polygon, but the customer is confirmed inside the alert's state --
    an Extreme Heat Warning over Arizona means every AC in Tucson is under
    strain, not that we are 50% unsure."""
    assert scoring.proximity_factor(None, None, same_state=True) == scoring.SAME_STATE_PROXIMITY


def test_zone_alert_in_a_different_state_scores_zero():
    """The bug this fixes: with no polygon nothing constrained the match
    spatially, so a Tucson customer matched Florida's heat warning."""
    assert scoring.proximity_factor(None, None, same_state=False) == 0.0


def test_unknown_state_is_penalised_but_not_dropped():
    """Neither confirmed nor excluded. Lower than a state match, above zero --
    a parsing failure should not silently delete an opportunity."""
    p = scoring.proximity_factor(None, None)
    assert 0 < p < scoring.SAME_STATE_PROXIMITY
    assert p == scoring.UNKNOWN_STATE_PROXIMITY


# ---------------------------------------------------------------------
# Value -- prospects must not be priced at zero
# ---------------------------------------------------------------------
def test_value_factor_clamps_at_one():
    assert scoring.value_factor(500_000, "platinum") == 1.0


def test_tier_bonus_breaks_ties_between_equal_jobs():
    plat = scoring.value_factor(10_000, "platinum")
    std = scoring.value_factor(10_000, "standard")
    assert plat > std


def test_prospect_with_est_value_still_scores():
    """A prospect has contract_value 0 but a real est_job_value. If this
    returned 0 the whole prospect list would sink to the bottom of the queue."""
    assert scoring.value_factor(15_000, "standard") > 0.4


# ---------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------
def test_score_is_bounded():
    s = scoring.exposure_score("Extreme", 0.0, 60.0, 1.0, 999_999, "platinum")
    assert 0.0 <= s <= 1.0


def test_direct_hit_on_high_value_customer_scores_critical():
    s = scoring.exposure_score("Extreme", 2.0, 60.0, 1.0, 28_000, "platinum")
    assert scoring.priority_band(s) == "critical"


def test_distant_customer_is_filtered_out_despite_high_value():
    """The multiplicative form must let proximity veto value."""
    s = scoring.exposure_score("Extreme", 800.0, 60.0, 1.0, 30_000, "platinum")
    assert s == 0.0
    assert s < scoring.QUEUE_CUTOFF


def test_zero_urgency_kills_the_row():
    # Heat Advisory against a roofing customer: not a demand signal.
    s = scoring.exposure_score("Severe", 5.0, 60.0, 0.0, 20_000, "gold")
    assert s == 0.0


def test_closer_customer_outranks_farther_identical_customer():
    near = scoring.exposure_score("Severe", 10.0, 60.0, 0.85, 15_000, "gold")
    far = scoring.exposure_score("Severe", 55.0, 60.0, 0.85, 15_000, "gold")
    assert near > far


def test_higher_urgency_event_outranks_lower():
    tornado = scoring.exposure_score("Severe", 10.0, 60.0, 1.00, 15_000, "gold")
    wind = scoring.exposure_score("Severe", 10.0, 60.0, 0.75, 15_000, "gold")
    assert tornado > wind


# ---------------------------------------------------------------------
# Priority bands
# ---------------------------------------------------------------------
def test_priority_bands_cover_the_whole_range():
    assert scoring.priority_band(0.95) == "critical"
    assert scoring.priority_band(0.60) == "high"
    assert scoring.priority_band(0.35) == "medium"
    assert scoring.priority_band(0.01) == "low"


def test_priority_never_returns_none():
    for i in range(0, 101):
        assert scoring.priority_band(i / 100) in {"critical", "high", "medium", "low"}


# ---------------------------------------------------------------------
# Deterministic ids -- the guard against duplicate opportunities
# ---------------------------------------------------------------------
def test_opportunity_id_is_stable_across_calls():
    a = scoring.opportunity_id("nws-alert-123", "cust_0007")
    b = scoring.opportunity_id("nws-alert-123", "cust_0007")
    assert a == b


def test_opportunity_id_differs_per_customer():
    a = scoring.opportunity_id("nws-alert-123", "cust_0007")
    b = scoring.opportunity_id("nws-alert-123", "cust_0008")
    assert a != b


def test_opportunity_id_differs_per_event():
    a = scoring.opportunity_id("nws-alert-123", "cust_0007")
    b = scoring.opportunity_id("nws-alert-999", "cust_0007")
    assert a != b


def test_opportunity_id_is_not_order_confusable():
    """id(A,B) must not collide with id(B,A) -- the delimiter earns its keep."""
    assert scoring.opportunity_id("x", "y") != scoring.opportunity_id("y", "x")
