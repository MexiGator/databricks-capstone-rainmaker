"""
Rainmaker -- exposure scoring.

Deliberately PURE PYTHON with no Spark import. Match & Score wraps these in
UDFs, and the test suite calls them directly. Scoring logic that only exists
inside a Spark expression is logic you cannot unit-test.

exposure_score = severity x proximity x urgency x value   ->  0..1

Each factor answers a different question:
  severity   how bad was the weather?          (NWS severity field)
  proximity  how close to the damage footprint? (distance vs alert radius)
  urgency    does this event actually drive     (event_service_map weight)
             this service line?
  value      is this customer worth calling first? (job value + tier)
"""

from __future__ import annotations

import hashlib
import math

EARTH_RADIUS_KM = 6371.0088

# --- tunable constants -------------------------------------------------
# Exposed as module constants so tests can assert on them and so tuning
# never means editing logic.

SEVERITY_WEIGHT: dict[str, float] = {
    "Extreme": 1.00,
    "Severe": 0.85,
    "Moderate": 0.60,
    "Minor": 0.35,
    "Unknown": 0.40,
}
DEFAULT_SEVERITY = 0.40

# Beyond the alert radius the score decays instead of dropping to zero:
# hail footprints are approximations, not fences.
OUTSIDE_RADIUS_DECAY_KM = 40.0


# When an alert carries NO polygon -- true of most heat and cold products,
# which are issued by forecast zone -- we cannot compute a distance.
#
# The first version returned a neutral 0.5 in that case. Live data showed why
# that is wrong: a Tucson customer matched seven Extreme Heat Warnings at once,
# including ones issued for other states, because "unknown distance" was being
# read as "moderately close". Neutral is not neutral when the alternative is
# no match at all.
#
# So a zone-only alert falls back to STATE matching. Same state means the
# customer is plausibly inside the affected region; a different state means
# they are not, and no amount of unknown-ness should imply otherwise.
SAME_STATE_PROXIMITY = 0.60
UNKNOWN_STATE_PROXIMITY = 0.40

TIER_BONUS: dict[str, float] = {"platinum": 0.15, "gold": 0.07, "standard": 0.0}

# Job value is normalised PER SERVICE LINE.
#
# The first version used one $30k ceiling for everything, calibrated on a roof
# replacement. Live data showed the consequence: an HVAC job tops out near
# $13k, so the best possible HVAC customer scored 0.43 on value and the queue
# could never surface one. Plumbing was worse.
#
# The question the value factor should answer is "is this a big job FOR THIS
# TRADE", not "is this a big job compared to a roof".
VALUE_CEILING_BY_SERVICE: dict[str, float] = {
    "roofing": 30_000.0,
    "restoration": 22_000.0,
    "hvac": 12_000.0,
    "plumbing": 7_000.0,
}
VALUE_CEILING = 30_000.0  # fallback for an unknown service line

PRIORITY_BANDS: list[tuple[float, str]] = [
    (0.70, "critical"),
    (0.50, "high"),
    (0.30, "medium"),
    (0.00, "low"),
]

# Rows below this never reach the analyst queue.
QUEUE_CUTOFF = 0.25


# --- geometry ----------------------------------------------------------
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# --- factors -----------------------------------------------------------
def severity_factor(severity: str | None) -> float:
    return SEVERITY_WEIGHT.get(severity or "", DEFAULT_SEVERITY)


def proximity_factor(
    distance_km: float | None,
    radius_km: float | None,
    same_state: bool | None = None,
) -> float:
    """
    1.0 at the centre of the footprint, tapering to 0.5 at its edge, then
    decaying to 0 over the next OUTSIDE_RADIUS_DECAY_KM.

    Not a hard cutoff, because a customer 5 km outside a hail polygon is far
    more likely to have damage than one 200 km away, and a binary in/out test
    would rank them identically.

    When there is no polygon at all, fall back to state matching rather than a
    neutral guess -- see SAME_STATE_PROXIMITY for why that matters.
    """
    if distance_km is None or radius_km is None or radius_km <= 0:
        if same_state is True:
            return SAME_STATE_PROXIMITY
        if same_state is False:
            return 0.0
        return UNKNOWN_STATE_PROXIMITY
    if distance_km <= radius_km:
        return 1.0 - 0.5 * (distance_km / radius_km)
    overshoot = distance_km - radius_km
    if overshoot >= OUTSIDE_RADIUS_DECAY_KM:
        return 0.0
    return 0.5 * (1.0 - overshoot / OUTSIDE_RADIUS_DECAY_KM)


def value_ceiling(service_type: str | None) -> float:
    return VALUE_CEILING_BY_SERVICE.get(service_type or "", VALUE_CEILING)


def value_factor(
    est_job_value: float | None,
    tier: str | None,
    service_type: str | None = None,
) -> float:
    """Job value normalised against this trade's ceiling, plus a loyalty bump."""
    value = float(est_job_value or 0.0)
    base = min(value / value_ceiling(service_type), 1.0)
    return min(base + TIER_BONUS.get(tier or "standard", 0.0), 1.0)


# --- composite ---------------------------------------------------------
def exposure_score(
    severity: str | None,
    distance_km: float | None,
    radius_km: float | None,
    urgency_weight: float,
    est_job_value: float | None,
    tier: str | None,
    service_type: str | None = None,
    same_state: bool | None = None,
) -> float:
    """
    Multiplicative on purpose: any factor at zero should kill the row.
    A $30k platinum customer 400 km from the storm is not an opportunity,
    and an additive score would still rank them highly.
    """
    score = (
        severity_factor(severity)
        * proximity_factor(distance_km, radius_km, same_state)
        * float(urgency_weight)
        * value_factor(est_job_value, tier, service_type)
    )
    return round(min(max(score, 0.0), 1.0), 3)


def priority_band(score: float) -> str:
    for threshold, label in PRIORITY_BANDS:
        if score >= threshold:
            return label
    return "low"


def estimated_value(est_job_value: float | None, service_type: str | None = None) -> float:
    """Expected value of the job if it converts. Kept as a seam so a future
    version can vary by service line or damage mode."""
    return round(float(est_job_value or 0.0), 2)


def opportunity_id(weather_event_id: str, customer_id: str) -> str:
    """
    Deterministic id so re-running Match & Score UPSERTs the same row
    instead of creating a duplicate. Same input -> same id, forever.
    """
    digest = hashlib.sha1(f"{weather_event_id}|{customer_id}".encode()).hexdigest()
    return f"opp_{digest[:20]}"
