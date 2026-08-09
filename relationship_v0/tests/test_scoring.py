"""Tests for the relationship_score engine. Pure logic -> fast, deterministic."""
import math

import pytest

from relationship_v0 import scoring
from relationship_v0.scoring import ContactSignals, compute_relationship_score


def test_weights_sum_to_one():
    assert abs(sum(scoring.WEIGHTS.values()) - 1.0) < 1e-9


def test_score_is_bounded_0_100():
    # Maximal signals stay <= 100; empty/never-touched stays >= 0.
    hot = ContactSignals(days_since_last_touch=0, opens=50, clicks=50,
                         positive_replies=50, avg_sentiment=1.0,
                         tenure_years=20, completed_jobs=20)
    cold = ContactSignals()  # never touched, no data
    hi = compute_relationship_score(hot)["relationship_score"]
    lo = compute_relationship_score(cold)["relationship_score"]
    assert 0.0 <= lo <= hi <= 100.0
    assert hi > 90.0
    # A consented-but-unknown contact is "cold but contactable": neutral
    # sentiment + full care_health, nothing else. Lands in the cold tier.
    assert lo < 25.0
    assert compute_relationship_score(cold)["tier"] == "cold"


def test_recency_decays_by_half_at_half_life():
    at_0 = scoring.recency_score(0)
    at_hl = scoring.recency_score(scoring.RECENCY_HALF_LIFE_DAYS)
    assert at_0 == pytest.approx(1.0)
    assert at_hl == pytest.approx(0.5, abs=1e-6)
    assert scoring.recency_score(None) == 0.0


def test_recency_is_monotonic_decreasing():
    xs = [0, 30, 60, 120, 240, 480]
    ys = [scoring.recency_score(x) for x in xs]
    assert all(ys[i] >= ys[i + 1] for i in range(len(ys) - 1))


def test_a_reply_outweighs_many_opens():
    reply = scoring.engagement_score(opens=0, clicks=0, positive_replies=1)
    opens = scoring.engagement_score(opens=6, clicks=0, positive_replies=0)
    assert reply > opens


def test_negative_events_reduce_engagement():
    clean = scoring.engagement_score(2, 2, 1, negative_events=0)
    dirty = scoring.engagement_score(2, 2, 1, negative_events=2)
    assert dirty < clean
    assert dirty >= 0.0


def test_sentiment_mapping():
    assert scoring.sentiment_score(None) == 0.5
    assert scoring.sentiment_score(-1.0) == 0.0
    assert scoring.sentiment_score(1.0) == 1.0
    assert scoring.sentiment_score(0.0) == 0.5


def test_prospect_has_no_tenure_or_loyalty():
    assert scoring.tenure_score(10, is_prospect=True) == 0.0
    assert scoring.loyalty_score(10, is_prospect=True) == 0.0


def test_opt_out_zeroes_care_health():
    assert scoring.care_health_score(consent_ok=True, opted_out=True,
                                     recent_care_touches=0) == 0.0


def test_over_messaging_erodes_care_health():
    fresh = scoring.care_health_score(True, False, recent_care_touches=0)
    spammed = scoring.care_health_score(True, False, recent_care_touches=4)
    assert spammed < fresh


def test_tiers_map_to_cutoffs():
    assert scoring.tier_for(90) == "hot"
    assert scoring.tier_for(75) == "hot"
    assert scoring.tier_for(74.9) == "warm"
    assert scoring.tier_for(50) == "warm"
    assert scoring.tier_for(49.9) == "cool"
    assert scoring.tier_for(25) == "cool"
    assert scoring.tier_for(24.9) == "cold"
    assert scoring.tier_for(0) == "cold"


def test_loyal_customer_beats_cold_prospect_same_engagement():
    engaged = dict(days_since_last_touch=20, opens=3, clicks=1,
                   positive_replies=1, avg_sentiment=0.5)
    loyal = compute_relationship_score(
        ContactSignals(tenure_years=8, completed_jobs=4, **engaged))
    prospect = compute_relationship_score(
        ContactSignals(is_prospect=True, **engaged))
    assert loyal["relationship_score"] > prospect["relationship_score"]


def test_output_shape_is_serializable():
    out = compute_relationship_score(ContactSignals(days_since_last_touch=10))
    assert set(out) == {"relationship_score", "tier", "components", "is_prospect"}
    assert set(out["components"]) == set(scoring.WEIGHTS)
    # round-trips through json without error
    import json
    json.dumps(out)
