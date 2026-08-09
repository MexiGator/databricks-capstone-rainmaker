"""relationship_v0.triggers.cumulative_threshold — Phase 2 provider (DESIGNED, STUBBED).

This is the "soiling accumulator" trigger for recurring-exteriors verticals
(e.g. window/pressure washing): integrate environmental load (pollen-days,
dust/AQI, rain-after-pollen spotting) since the last service, fire when it
crosses a "needs service" threshold AND a clear-weather window is forecast so the
clean lasts (timing_ok).

It is intentionally NOT built in Phase 1 — its job here is to PROVE the interface
is future-proof: the moment it's implemented, it emits the same `Opportunity` and
flows through the unchanged engine. The pure accumulator sketch below is included
so the contract is concrete, but `produce` raises until Phase 2 is greenlit and a
real environmental feed + per-market calibration exist.

Design note (keep it honest): weather -> "looks dirty" is a NOISIER correlation
than weather -> "roof damage". Ship an EXPLAINABLE proxy (days-since + pollen-days
+ rain-after-pollen + dust, visible weights), framed as timing SUGGESTIONS a human
confirms, and calibrate weights per market from real response data. Do not ship a
black-box "dirtiness predictor."
"""
from __future__ import annotations

from typing import Optional

from relationship_v0.triggers.base import Opportunity, TriggerProvider


def accumulate_soiling(days_since_service: float, pollen_days: float = 0.0,
                       dust_load: float = 0.0, rain_after_pollen_events: int = 0,
                       weights: Optional[dict] = None) -> float:
    """Explainable 0..1 soiling proxy. Reference implementation for Phase 2 so the
    interface is concrete; weights are the tunable, per-market knobs."""
    w = {"time": 0.35, "pollen": 0.25, "dust": 0.15, "spotting": 0.25}
    if weights:
        w.update(weights)
    # each term saturates; spotting (pollen then rain) is the strongest visible cue
    time_term = min(1.0, days_since_service / 90.0)
    pollen_term = min(1.0, pollen_days / 30.0)
    dust_term = min(1.0, dust_load)  # dust_load already normalized 0..1
    spot_term = min(1.0, rain_after_pollen_events / 2.0)
    score = (w["time"] * time_term + w["pollen"] * pollen_term
             + w["dust"] * dust_term + w["spotting"] * spot_term)
    return max(0.0, min(1.0, score))


class CumulativeThresholdProvider(TriggerProvider):
    trigger_type = "cumulative_threshold"

    def produce(self, contacts: list[dict], context: Optional[dict] = None
                ) -> list[Opportunity]:  # pragma: no cover - Phase 2
        raise NotImplementedError(
            "cumulative_threshold is Phase 2. The interface is proven; wire an "
            "environmental feed + per-market calibration, then emit Opportunity("
            "trigger_type='cumulative_threshold', signal_strength=accumulate_soiling(...), "
            "timing_ok=<clear-window forecast>). No engine changes required."
        )
