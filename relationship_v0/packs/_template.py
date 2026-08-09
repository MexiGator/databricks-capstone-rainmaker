"""relationship_v0.packs._template — fill-in template for the NEXT vertical.

Do NOT hardcode the second vertical yet. Project Clearview (the ResiBrands/Pink's
research dossier) decides which vertical to instantiate first. When it returns:
  1. copy this file to packs/<vertical>.py,
  2. set service_lines + cadence intervals,
  3. add a care corpus for that vertical (or reuse shared guides),
  4. turn on the providers it needs (`cadence` works today; `cumulative_threshold`
     is Phase 2 and needs an environmental feed + calibration),
  5. register the pack and run it through the SAME engine — no engine changes.

The presence of `cumulative_threshold` in active_providers is what will exercise
the Phase 2 provider once it's built; until then, leave it out or expect
NotImplementedError from that provider only.
"""
from relationship_v0.packs.base import VerticalPack


NEXT_VERTICAL_PACK = VerticalPack(
    key="TODO_vertical",                       # e.g. "exteriors" (windows/pressure-wash)
    service_lines=[],                          # e.g. ["windows", "gutters", "pressure_wash"]
    active_providers=["cadence"],              # add "cumulative_threshold" in Phase 2
    cadence_config={
        "default_interval_days": 120,          # e.g. quarterly window cleaning
        "service_line_intervals": {},          # e.g. {"gutters": 180, "windows": 120}
    },
    care_corpus=None,                          # supply vertical-specific guides here
    threshold_config={                         # Phase 2 soiling accumulator knobs
        "threshold": 0.6,
        "weights": {"time": 0.35, "pollen": 0.25, "dust": 0.15, "spotting": 0.25},
    },
    notes="TEMPLATE — populate from the Project Clearview dossier's chosen vertical.",
)
