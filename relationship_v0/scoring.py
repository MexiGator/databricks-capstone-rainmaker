"""relationship_v0.scoring — the relationship_score engine (pure, dependency-free).

`relationship_score` (0-100) measures the WARMTH / HEALTH of the bond with one
contact. It is deliberately SEPARATE from a storm's exposure/priority score
(severity x proximity x value) that Rainmaker already computes:

    exposure  -> WHEN to reach out   (a hazard created a need right now)
    warmth    -> HOW  to reach out   (direct ask vs. warm-up value-first)

Keeping them separate is the whole design: a cold high-value prospect in a hail
path should get a *different* message than a loyal 8-year customer, even though
their exposure is identical. `policy.select_next_action` combines the two.

This module has NO third-party imports so it runs anywhere and is fully unit
tested. The DB/embedding/Flask adapters live in other modules and import this.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# --------------------------------------------------------------------------- #
# Tunable constants — every number a reviewer might question lives here and is
# covered by a test. "Engineered, not vibe-coded."
# --------------------------------------------------------------------------- #
WEIGHTS = {
    "recency": 0.25,      # have we been in touch recently? (decays)
    "engagement": 0.30,   # do they open / click / reply? (the strongest signal)
    "sentiment": 0.15,    # how do they feel about us?
    "tenure": 0.10,       # how long have they been with us?
    "loyalty": 0.10,      # repeat / completed jobs
    "care_health": 0.10,  # consent + not over-messaged + welcomes value content
}

RECENCY_HALF_LIFE_DAYS = 120.0     # a touch is "half as warm" after ~4 months
TENURE_SATURATION_YEARS = 5.0      # 5+ years of tenure counts as full loyalty on this axis
LOYALTY_SATURATION_JOBS = 3.0      # 3+ completed jobs = full loyalty on this axis
ENGAGEMENT_SCALE = 3.0             # larger => engagement saturates more slowly

# Engagement point values (a reply is worth far more than an open).
ENGAGEMENT_POINTS = {"open": 0.1, "click": 0.3, "positive_reply": 0.7}
ENGAGEMENT_NEG_PENALTY = 0.25      # per negative event (complaint, hard bounce), capped

# Warmth tier cutoffs (inclusive lower bound).
TIER_CUTOFFS = [("hot", 75.0), ("warm", 50.0), ("cool", 25.0), ("cold", 0.0)]


@dataclass
class ContactSignals:
    """Everything the score needs about one contact. All optional so the engine
    degrades gracefully on sparse CRM data (common in real client lists)."""
    days_since_last_touch: Optional[float] = None   # None => never touched
    opens: int = 0
    clicks: int = 0
    positive_replies: int = 0
    negative_events: int = 0                         # complaints, opt-out attempts, hard bounces
    avg_sentiment: Optional[float] = None            # [-1, 1]; None => unknown/neutral
    tenure_years: Optional[float] = None             # None or 0 for prospects
    completed_jobs: int = 0
    consent_ok: bool = True                          # do we have permission to contact?
    opted_out: bool = False
    recent_care_touches: int = 0                     # care sends in the frequency window
    is_prospect: bool = False


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def recency_score(days_since_last_touch: Optional[float]) -> float:
    """Exponential decay: fresh contact ~1.0, decaying by half every
    RECENCY_HALF_LIFE_DAYS. Never contacted => 0.0."""
    if days_since_last_touch is None:
        return 0.0
    d = max(0.0, float(days_since_last_touch))
    return _clamp(0.5 ** (d / RECENCY_HALF_LIFE_DAYS))


def engagement_score(opens: int, clicks: int, positive_replies: int,
                     negative_events: int = 0) -> float:
    """Saturating positive engagement minus a penalty for negative signals.
    A single positive reply moves this far more than a dozen opens."""
    pos = (ENGAGEMENT_POINTS["open"] * max(0, opens)
           + ENGAGEMENT_POINTS["click"] * max(0, clicks)
           + ENGAGEMENT_POINTS["positive_reply"] * max(0, positive_replies))
    base = 1.0 - math.exp(-pos / ENGAGEMENT_SCALE)
    penalty = min(base, ENGAGEMENT_NEG_PENALTY * max(0, negative_events))
    return _clamp(base - penalty)


def sentiment_score(avg_sentiment: Optional[float]) -> float:
    """Map [-1, 1] sentiment to [0, 1]. Unknown => 0.5 (neutral, no reward/penalty)."""
    if avg_sentiment is None:
        return 0.5
    return _clamp((_clamp(avg_sentiment, -1.0, 1.0) + 1.0) / 2.0)


def tenure_score(tenure_years: Optional[float], is_prospect: bool = False) -> float:
    if is_prospect or not tenure_years:
        return 0.0
    return _clamp(float(tenure_years) / TENURE_SATURATION_YEARS)


def loyalty_score(completed_jobs: int, is_prospect: bool = False) -> float:
    if is_prospect:
        return 0.0
    return _clamp(max(0, completed_jobs) / LOYALTY_SATURATION_JOBS)


def care_health_score(consent_ok: bool, opted_out: bool,
                      recent_care_touches: int) -> float:
    """Receptiveness to being contacted with value-first content.
    Opt-out zeroes it; missing consent caps it low; over-messaging erodes it."""
    if opted_out:
        return 0.0
    base = 1.0 if consent_ok else 0.4
    # Erode as recent care touches climb past a comfortable cadence (~1/window).
    over = max(0, recent_care_touches - 1)
    base -= min(0.6, 0.3 * over)
    return _clamp(base)


def tier_for(score: float) -> str:
    for name, cutoff in TIER_CUTOFFS:
        if score >= cutoff:
            return name
    return "cold"


def compute_relationship_score(s: ContactSignals) -> dict:
    """Return the components, the blended 0-100 score, and the warmth tier.

    The return is a plain dict so it serializes straight into Lakebase /
    JSON / the app without any framework coupling.
    """
    components = {
        "recency": recency_score(s.days_since_last_touch),
        "engagement": engagement_score(s.opens, s.clicks, s.positive_replies,
                                       s.negative_events),
        "sentiment": sentiment_score(s.avg_sentiment),
        "tenure": tenure_score(s.tenure_years, s.is_prospect),
        "loyalty": loyalty_score(s.completed_jobs, s.is_prospect),
        "care_health": care_health_score(s.consent_ok, s.opted_out,
                                         s.recent_care_touches),
    }
    blended = sum(WEIGHTS[k] * components[k] for k in WEIGHTS)
    score = round(100.0 * _clamp(blended), 1)
    return {
        "relationship_score": score,
        "tier": tier_for(score),
        "components": {k: round(v, 4) for k, v in components.items()},
        "is_prospect": s.is_prospect,
    }
