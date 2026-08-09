"""relationship_v0.triggers.base — the normalized Opportunity + provider contract.

`Opportunity` is the ONE shape every trigger provider emits and the shared engine
consumes. If a new trigger type can be expressed as an Opportunity, it works with
the entire existing relationship stack for free — that is the generalization.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional


# The trigger "physics" a provider represents. New physics => new value here +
# a mapping in registry.PROVIDER_TO_POLICY_TRIGGER (that's the whole extension).
TRIGGER_TYPES = ("event", "cadence", "cumulative_threshold")


@dataclass
class Opportunity:
    """A normalized 'someone may need service now' signal, produced by ANY
    provider and consumed by the shared engine.

    signal_strength (0..1) is the provider's own confidence/urgency:
      - event               -> storm exposure (severity x proximity)
      - cadence             -> how overdue the service is
      - cumulative_threshold-> how far past the soiling threshold
    timing_ok gates the send NOW vs. wait (e.g. don't clean before more rain).
    context carries whatever the care composer + grounding need (the event, the
    due interval, the accumulator breakdown).
    """
    contact_id: object
    trigger_type: str            # one of TRIGGER_TYPES
    service_line: Optional[str]
    signal_strength: float = 0.0
    timing_ok: bool = True
    reason: str = ""
    context: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.trigger_type not in TRIGGER_TYPES:
            raise ValueError(f"unknown trigger_type: {self.trigger_type!r}")
        self.signal_strength = max(0.0, min(1.0, float(self.signal_strength)))

    def as_dict(self) -> dict:
        return {
            "contact_id": self.contact_id,
            "trigger_type": self.trigger_type,
            "service_line": self.service_line,
            "signal_strength": round(self.signal_strength, 4),
            "timing_ok": self.timing_ok,
            "reason": self.reason,
            "context": self.context,
        }


class TriggerProvider(abc.ABC):
    """A source of Opportunities. Implement `produce`; everything downstream is
    shared. Providers are pure (no DB/network in the core) — IO is injected via
    `context`, exactly like forecast_scan isolates the NWS call.
    """

    #: the trigger_type this provider emits (must be in TRIGGER_TYPES)
    trigger_type: str = ""

    def __init__(self, pack: Optional[object] = None):
        self.pack = pack

    @abc.abstractmethod
    def produce(self, contacts: list[dict], context: Optional[dict] = None
                ) -> list[Opportunity]:
        """Turn contacts + a provider-specific context into Opportunities."""
        raise NotImplementedError
