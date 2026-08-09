"""relationship_v0.packs.roofing — Rainmaker's ORIGINAL vertical, expressed as a Pack.

The point of this file: prove the existing weather/roofing behavior is nothing
more than a configuration of the generic engine. It turns on the `event` provider
(the v0.1 forecast trigger) and adds an annual `cadence` (a roof-maintenance
check) so a single pack demonstrates TWO trigger types flowing through one engine.
"""
from relationship_v0.packs.base import VerticalPack


ROOFING_PACK = VerticalPack(
    key="roofing",
    service_lines=["roofing"],
    active_providers=["event", "cadence"],
    cadence_config={
        "default_interval_days": 365,          # annual roof / maintenance check
        "service_line_intervals": {"roofing": 365},
    },
    # event_service_map + care_corpus default to the shared v0.1 assets
    notes="The native Rainmaker vertical. `event` = storm/forecast (v0.1 behavior); "
          "`cadence` = annual roof check. Proves the existing product is just a pack.",
)
