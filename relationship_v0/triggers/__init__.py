"""relationship_v0.triggers — v0.2 trigger-provider abstraction.

The v0.2 thesis: ONE engine, PLUGGABLE trigger types. Every provider turns some
signal (a weather event, a service-due date, a soiling threshold) into the SAME
normalized `Opportunity`, which the shared relationship engine (scoring + policy
+ care + guardrails) consumes without knowing how it was produced.

Adding a vertical becomes a config swap (a VerticalPack) + at most one new
provider — not a rebuild. That is what "the architecture generalizes across
trigger types" means, made concrete and testable.
"""
from relationship_v0.triggers.base import Opportunity, TriggerProvider
from relationship_v0.triggers.registry import (
    PROVIDERS, PROVIDER_TO_POLICY_TRIGGER, get_provider,
)

__all__ = [
    "Opportunity", "TriggerProvider", "PROVIDERS",
    "PROVIDER_TO_POLICY_TRIGGER", "get_provider",
]
