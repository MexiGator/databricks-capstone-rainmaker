"""relationship_v0.packs.base — the VerticalPack config object.

Everything domain-specific about a vertical lives here so the engine stays
generic. Defaults fall back to the v0.1 care corpus + event->service map, so a
weather vertical needs almost no config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from relationship_v0 import care_content


@dataclass
class VerticalPack:
    key: str                                   # "roofing", "<next_vertical>"
    service_lines: list[str] = field(default_factory=list)
    active_providers: list[str] = field(default_factory=lambda: ["event"])
    # cadence: {"default_interval_days": int, "service_line_intervals": {line: int}}
    cadence_config: dict = field(default_factory=dict)
    # optional overrides; default to the shared v0.1 assets
    event_service_map: Optional[dict] = None
    care_corpus: Optional[list] = None
    scoring_weights_override: Optional[dict] = None
    # Phase 2: {"weights": {...}, "threshold": float} for the soiling accumulator
    threshold_config: dict = field(default_factory=dict)
    notes: str = ""

    def get_event_service_map(self) -> dict:
        return self.event_service_map or care_content.EVENT_SERVICE_CARE

    def get_care_corpus(self) -> list:
        return self.care_corpus if self.care_corpus is not None else care_content.CARE_GUIDES

    def validate(self) -> None:
        from relationship_v0.triggers.base import TRIGGER_TYPES
        bad = [p for p in self.active_providers if p not in TRIGGER_TYPES]
        if bad:
            raise ValueError(f"pack {self.key}: unknown providers {bad}")
