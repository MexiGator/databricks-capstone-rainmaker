"""relationship_v0.triggers.cadence — the CadenceProvider (v0.2's new trigger type).

The cheapest possible proof that the architecture generalizes: a NON-weather
signal ("you're due for your recurring service") flowing through the exact same
scoring + policy + guardrails. Needs no external data — only the contact's
`days_since_service` and the vertical's cadence interval. It is the natural
maintenance-plan trigger for a recurring business (windows, gutters, HVAC tune-ups).

Pure: `days_since_service` is passed on the contact (like v0.1's
`days_since_last_touch`), so there is no hidden clock and the logic is testable.
"""
from __future__ import annotations

from typing import Optional

from relationship_v0.triggers.base import Opportunity, TriggerProvider


DEFAULT_INTERVAL_DAYS = 120  # a sane recurring cadence if a pack doesn't specify


class CadenceProvider(TriggerProvider):
    trigger_type = "cadence"

    def _interval_for(self, service_line: Optional[str]) -> int:
        cfg = {}
        if self.pack is not None:
            cfg = getattr(self.pack, "cadence_config", {}) or {}
        per_line = (cfg.get("service_line_intervals") or {})
        return int(per_line.get(service_line,
                                cfg.get("default_interval_days",
                                        DEFAULT_INTERVAL_DAYS)))

    def produce(self, contacts: list[dict], context: Optional[dict] = None
                ) -> list[Opportunity]:
        context = context or {}
        # allow a caller override of the interval (e.g. seasonal push)
        override = context.get("interval_days")
        opps = []
        for c in contacts:
            dsi = c.get("days_since_service")
            if dsi is None:
                continue  # no service history -> cadence can't speak
            service_line = c.get("service_type")
            interval = int(override) if override else self._interval_for(service_line)
            if dsi < interval:
                continue  # not due yet
            # overdue-ness as 0..1: at interval -> 0, at 2x interval -> ~1
            overdue = (dsi - interval) / max(1, interval)
            opps.append(Opportunity(
                contact_id=c.get("contact_id") or c.get("customer_id"),
                trigger_type="cadence",
                service_line=service_line,
                signal_strength=min(1.0, overdue),
                timing_ok=True,
                reason=f"cadence:{dsi}d since {service_line} service (interval {interval}d)",
                context={"days_since_service": dsi, "interval_days": interval,
                         "service_line": service_line},
            ))
        return opps
