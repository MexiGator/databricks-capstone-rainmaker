"""relationship_v0.policy — warmth x trigger -> next best action (pure).

This is the commercial heart of v0.1. It converts the relationship_score (warmth)
plus the reason we're reaching out (trigger) into ONE concrete decision whose
north star is always the same: get an inspection on the calendar without
spending relationship equity we can't get back.

The operating principle a home-services owner already lives by:
  - You *earn the right* to ask. A stranger in a hail path is not sold a roof by
    text; they're helped ("here's what to check"), and the ask comes warm.
  - A loyal customer doesn't need the warm-up; skipping it and getting them a
    slot fast is the respectful move.
  - You never message someone you're not allowed to, and you never wear out a
    good relationship with too many touches — deliverability and trust are the
    real assets.

So the matrix below is not arbitrary UX copy; it's how a good operator would
triage the list by hand, encoded so the agent can do it at scale.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Trigger(str, Enum):
    ACTIVE_STORM = "active_storm"   # a hazard is hitting now -> damage likely
    FORECAST = "forecast"           # a hazard is forecast (lead time) -> prep/care
    DORMANT = "dormant"             # no weather; a valuable contact has gone quiet


class TemplateKind(str, Enum):
    DIRECT_INSPECTION = "direct_inspection"  # "let's get your free inspection booked"
    DAMAGE_CHECK = "damage_check"            # value-first "here's what to check" + ask
    CARE_TIP = "care_tip"                    # pure prep guidance + soft ask (proactive care)
    REENGAGE = "reengage"                    # seasonal/maintenance re-warm for dormant value
    SUPPRESS = "suppress"                    # do not send (with a reason)


class CtaStrength(str, Enum):
    NONE = "none"
    SOFT = "soft"       # "reply if you'd ever like us to take a look"
    MEDIUM = "medium"   # "want us to pencil in a quick check?"
    STRONG = "strong"   # "we can hold a free inspection slot for you this week"


# Frequency guardrails (protect trust + deliverability).
FREQUENCY_WINDOW_DAYS = 30
MAX_CARE_TOUCHES_PER_WINDOW = 2


@dataclass
class ContactFlags:
    consent_ok: bool = True
    opted_out: bool = False
    care_touches_in_window: int = 0
    lifetime_value: float = 0.0     # used to decide if a dormant contact is worth a re-warm


@dataclass
class Action:
    send: bool
    template_kind: TemplateKind
    cta_strength: CtaStrength
    tone: str
    reason: str
    # convenience for callers/telemetry
    trigger: Optional[Trigger] = None
    tier: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "send": self.send,
            "template_kind": self.template_kind.value,
            "cta_strength": self.cta_strength.value,
            "tone": self.tone,
            "reason": self.reason,
            "trigger": self.trigger.value if self.trigger else None,
            "tier": self.tier,
        }


DORMANT_REENGAGE_MIN_LTV = 3000.0   # only re-warm dormant contacts worth the touch


def can_contact(flags: ContactFlags, is_transactional: bool = False) -> tuple[bool, str]:
    """Consent + frequency gate. `is_transactional` (e.g. a booking confirmation the
    customer asked for) bypasses the marketing frequency cap but never opt-out."""
    if flags.opted_out:
        return (False, "opted_out")
    if not flags.consent_ok and not is_transactional:
        return (False, "no_consent")
    if (not is_transactional
            and flags.care_touches_in_window >= MAX_CARE_TOUCHES_PER_WINDOW):
        return (False, "frequency_cap")
    return (True, "ok")


def select_next_action(tier: str, trigger: Trigger, flags: ContactFlags,
                       exposure: float = 0.0) -> Action:
    """The decision. `tier` is the warmth tier from scoring.tier_for();
    `exposure` (0..1) is the storm's existing priority score, used only to
    escalate the CTA for very high exposure active storms."""
    allowed, why = can_contact(flags)
    if not allowed:
        return Action(False, TemplateKind.SUPPRESS, CtaStrength.NONE,
                      tone="none", reason=why, trigger=trigger, tier=tier)

    hot_or_warm = tier in ("hot", "warm")

    if trigger == Trigger.ACTIVE_STORM:
        if hot_or_warm:
            # Trusted already -> get them a slot, don't make them read a tutorial.
            cta = CtaStrength.STRONG
            return Action(True, TemplateKind.DIRECT_INSPECTION, cta,
                          tone="urgent_helpful",
                          reason="active_storm+warm: direct inspection offer",
                          trigger=trigger, tier=tier)
        # cool/cold -> lead with help, then ask. Escalate CTA if exposure is severe.
        cta = CtaStrength.MEDIUM if exposure >= 0.75 else CtaStrength.SOFT
        return Action(True, TemplateKind.DAMAGE_CHECK, cta,
                      tone="helpful_reassuring",
                      reason="active_storm+cold: value-first damage check, warm-then-ask",
                      trigger=trigger, tier=tier)

    if trigger == Trigger.FORECAST:
        # Proactive care: pure prep value. The ask is always soft; warmth only
        # nudges it from soft->medium for people who already like hearing from us.
        cta = CtaStrength.MEDIUM if tier == "hot" else CtaStrength.SOFT
        return Action(True, TemplateKind.CARE_TIP, cta,
                      tone="advisor",
                      reason=f"forecast+{tier}: proactive prep tip, soft inspection nudge",
                      trigger=trigger, tier=tier)

    if trigger == Trigger.DORMANT:
        # No weather to justify contact -> only re-warm contacts worth it, gently.
        if hot_or_warm and flags.lifetime_value >= DORMANT_REENGAGE_MIN_LTV:
            return Action(True, TemplateKind.REENGAGE, CtaStrength.SOFT,
                          tone="friendly_checkin",
                          reason="dormant+valuable: seasonal maintenance re-warm",
                          trigger=trigger, tier=tier)
        return Action(False, TemplateKind.SUPPRESS, CtaStrength.NONE,
                      tone="none",
                      reason="dormant+low-value-or-cold: no weather reason to reach out",
                      trigger=trigger, tier=tier)

    # Unknown trigger -> safe default: do nothing.
    return Action(False, TemplateKind.SUPPRESS, CtaStrength.NONE,
                  tone="none", reason="unknown_trigger", trigger=trigger, tier=tier)
