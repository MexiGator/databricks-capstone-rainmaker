"""relationship_v0.triggers.registry — provider lookup + trigger->policy mapping.

Adding a new trigger type is exactly two lines here (register the provider + map
it to a policy Trigger) plus the provider class. That's the extension surface.
"""
from __future__ import annotations

from relationship_v0.policy import Trigger
from relationship_v0.triggers.event import EventProvider
from relationship_v0.triggers.cadence import CadenceProvider
from relationship_v0.triggers.cumulative_threshold import CumulativeThresholdProvider


# trigger_type -> provider class
PROVIDERS = {
    "event": EventProvider,
    "cadence": CadenceProvider,
    "cumulative_threshold": CumulativeThresholdProvider,
}

# trigger_type -> the shared policy Trigger it routes to.
# This is where a new trigger's "physics" connects to the shared decision logic.
PROVIDER_TO_POLICY_TRIGGER = {
    "event": Trigger.FORECAST,               # lead-time weather -> proactive care
    "cadence": Trigger.CADENCE,              # you're due -> reminder
    "cumulative_threshold": Trigger.FORECAST, # need crossed a line -> proactive care
}


def get_provider(trigger_type: str, pack=None):
    try:
        return PROVIDERS[trigger_type](pack=pack)
    except KeyError:
        raise ValueError(f"no provider registered for trigger_type={trigger_type!r}")
