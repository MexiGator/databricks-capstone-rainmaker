"""relationship_v0.packs — Vertical Packs.

A VerticalPack is the ONLY thing that changes between verticals: which providers
are active, the service lines, the cadence intervals, the care corpus, and any
scoring/threshold tuning. Instantiating a new vertical = writing one pack (and,
if it needs a new trigger physics, one provider). Proof of generalization.
"""
from relationship_v0.packs.base import VerticalPack
from relationship_v0.packs.roofing import ROOFING_PACK

__all__ = ["VerticalPack", "ROOFING_PACK"]
