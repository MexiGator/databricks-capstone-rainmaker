"""
Rainmaker -- storm safety notices.

The first message a customer ever receives from Rainmaker is help, not a pitch.

DESIGN RULE, non-negotiable: safety content is NEVER generated.

An LLM inventing storm safety advice is one hallucination away from telling
someone to go outside during a tornado warning. So this module composes the
notice from two non-generative sources:

  1. The National Weather Service `instruction` field, VERBATIM. NWS output is
     a work of the US federal government and is public domain, so quoting it
     in full is both legal and correct -- paraphrasing official safety guidance
     would be worse, not better.
  2. A curated post-storm property checklist, written once and reviewed, keyed
     by service line and hazard.

Zero LLM calls. That also means safety notices still send when the model
serving endpoint is slow or down.

Everything here is pure except `send_safety_notice` in tools.py, so the whole
module is unit-testable without a database.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Roughly two SMS segments of NWS guidance. Long enough to carry the real
# instruction, short enough that people read it.
MAX_INSTRUCTION_CHARS = 320

# Curated. Factual observations a homeowner can make from the ground, phrased
# as things worth checking -- never as damage that has occurred.
PROPERTY_CHECKS: dict[str, list[str]] = {
    "roofing": [
        "Shingle pieces or granules collecting in gutters or downspouts",
        "Dents on vents, flashing, or gutter faces — these show hail size",
        "Water stains on upstairs ceilings in the days after",
    ],
    "plumbing": [
        "Exterior hose bibs and any pipe running through an unheated space",
        "Reduced flow at a tap — often the first sign of a partial freeze",
        "Damp patches on walls near where supply lines run",
    ],
    "hvac": [
        "Debris packed against the outdoor condenser unit",
        "The system running continuously without reaching the set temperature",
        "Unusual noise or a burning smell at startup",
    ],
    "restoration": [
        "Standing water in crawlspaces, basements, or under appliances",
        "A musty smell, which can appear before any visible staining",
        "Baseboards or drywall that feel soft or look swollen",
    ],
}

# Hazard-specific overrides where the generic list would miss the point.
HAZARD_CHECKS: dict[tuple[str, str], list[str]] = {
    ("Ice Storm Warning", "roofing"): [
        "Ice building along the eaves — this is what forms an ice dam",
        "Gutters sagging or pulling away under ice weight",
        "Water stains appearing on upstairs ceilings during a thaw",
    ],
    ("Hurricane Warning", "roofing"): [
        "Lifted or missing ridge caps along the roof peak",
        "Flashing separated at the chimney or in valleys",
        "Soffit or fascia panels that have worked loose",
    ],
}

OPT_OUT_LINE = "Reply STOP to opt out."
# Deliberately does NOT say "no reply needed" -- the previous version did, two
# lines after inviting the customer to call. Mixed instruction, and the half
# that discourages contact reaches exactly the person who might need it.
DISCLAIMER = "Safety note, not a sales pitch — there's nothing to buy here."

# ---------------------------------------------------------------------
# TONE TIERS
#
# The single most important CX decision in this module: warmth has to scale
# DOWN as the hazard scales up.
#
# "Hope you and your family are doing well" is kind during a heat advisory and
# tone-deaf during an active tornado warning — at that moment the person may be
# in a hallway with their kids, and a chatty preamble delays the guidance that
# matters. So an Extreme active alert gets a short, calm, front-loaded message
# with no pleasantries and no property checklist; the checklist is promised for
# afterwards instead.
#
# Fixed friendly copy would read as automated exactly when trust matters most.
# ---------------------------------------------------------------------
TONE = {
    "urgent": {
        "defer_checks": True,               # not the moment for a to-do list
        "close": "Nothing needed from you right now. I'll follow up once it's clear "
                 "with what to check.",
        "promises_follow_up": True,
    },
    "standard": {
        "defer_checks": False,
        "close": None,                      # the help line closes it instead
        "promises_follow_up": False,
    },
    "gentle": {
        "defer_checks": False,
        "close": None,
        "promises_follow_up": False,
    },
}

# ---------------------------------------------------------------------
# WHY AM I GETTING THIS
#
# Question two after "who is this", and the version that shipped first did not
# answer it -- a nine-year customer and a stranger got identical copy.
#
# One line, earned differently depending on the relationship. This replaces
# "Hope you and your family are safe", which was filler occupying the most
# valuable line in the message.
# ---------------------------------------------------------------------
def relationship_line(
    *, is_prospect: bool, tenure_start=None, city: str | None = None, service_type: str = ""
) -> str:
    if is_prospect:
        where = f"around {city}" if city else "in your area"
        return f"We work {where} and you're in the path of this one, so I wanted you to have it."
    year = getattr(tenure_start, "year", None)
    if year:
        return f"You've been with us since {year}, so I wanted to make sure you saw this."
    return "You're one of our customers, so I wanted to make sure you saw this."


# ---------------------------------------------------------------------
# THE OPEN DOOR
#
# The first draft said "No need to reply" and then "we're around if you need
# us" -- mixed instruction, and the first half discourages contact from exactly
# the person who might need it.
#
# Offering help is not selling. This is the single most valuable line in the
# message and it costs the business nothing unless someone actually needs it.
# ---------------------------------------------------------------------
TROUBLE_SIGNS: dict[str, str] = {
    "roofing": "water coming in",
    "plumbing": "a pipe let go",
    "hvac": "the system quit",
    "restoration": "water where it shouldn't be",
}


def help_line(service_type: str, rep: str | None, phone: str | None) -> str | None:
    trouble = TROUBLE_SIGNS.get(service_type)
    if not trouble or not phone:
        return None
    who = f"ask for {rep}" if rep else "give us a call"
    return f"If you end up with {trouble}, call {phone} and {who} — day or night, and there's no charge to come look."


# Tenants carry a callback number as well as a display name. A safety notice
# that offers help without giving a way to reach anyone is a gesture, not help.
COMPANIES: dict[str, dict[str, str]] = {
    "summit-exteriors":      {"name": "Summit Exteriors",             "phone": "(817) 555-0142"},
    "heartland-roofing":     {"name": "Heartland Roofing",            "phone": "(402) 555-0119"},
    "northline-plumbing":    {"name": "Northline Plumbing",           "phone": "(612) 555-0168"},
    "gulfstate-restoration": {"name": "Gulf State Restoration",       "phone": "(504) 555-0133"},
    "desert-air-hvac":       {"name": "Desert Air Heating & Cooling", "phone": "(602) 555-0177"},
    "atlantic-exteriors":    {"name": "Atlantic Exteriors",           "phone": "(904) 555-0155"},
}


def company_name(tenant: str | None) -> str:
    if not tenant:
        return "your service team"
    if tenant in COMPANIES:
        return COMPANIES[tenant]["name"]
    return tenant.replace("-", " ").title()


def company_phone(tenant: str | None) -> str | None:
    return COMPANIES.get(tenant or "", {}).get("phone")


# ---------------------------------------------------------------------
# QUIET HOURS
#
# A heat advisory notice at 3am is a trust-destroying event, and it is also the
# kind of thing that gets a business a TCPA complaint. But an active tornado
# warning at 3am is exactly when someone needs to be woken up.
#
# So: quiet hours apply to the gentle tier only. Urgent and standard always
# send, because a hazard that qualifies for those tiers outranks a good night.
# ---------------------------------------------------------------------
QUIET_START_HOUR = 21   # 9pm
QUIET_END_HOUR = 8      # 8am

# Coarse state -> standard-time UTC offset. Coarse on purpose: we only need to
# know roughly whether it is the middle of the night, and a full tz database
# lookup for a quiet-hours check is precision nobody benefits from.
STATE_UTC_OFFSET: dict[str, int] = {
    "CT": -5, "MA": -5, "NY": -5, "VA": -5, "NC": -5, "SC": -5, "GA": -5, "FL": -5,
    "AL": -6, "TN": -6, "IL": -6, "WI": -6, "MN": -6, "IA": -6, "MO": -6, "LA": -6,
    "TX": -6, "OK": -6, "KS": -6, "NE": -6, "CO": -7, "NM": -7, "AZ": -7,
    "NV": -8, "CA": -8,
}


def local_hour(state: str | None, now: datetime | None = None) -> int | None:
    """Approximate local hour. Returns None when the state is unknown, which
    the caller treats as 'do not hold' -- failing open beats silently dropping
    a safety message."""
    offset = STATE_UTC_OFFSET.get((state or "").upper())
    if offset is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now.hour + offset) % 24


def hold_for_quiet_hours(tier: str, state: str | None, now: datetime | None = None) -> tuple[bool, str]:
    """Should this notice wait until morning? Returns (hold, reason)."""
    if tier != "gentle":
        return False, "Hazard outranks quiet hours."
    hour = local_hour(state, now)
    if hour is None:
        return False, "Local time unknown — sending."
    if hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR:
        return True, f"It's {hour}:00 locally. Holding until {QUIET_END_HOUR}:00 — this can wait."
    return False, "Within sending hours."


def tone_tier(severity: str | None, active: bool) -> str:
    """
    Which register to write in.

    Extreme AND still in progress is the only case that gets the stripped-back
    urgent voice. An expired Extreme alert is a cleanup conversation, not an
    emergency one, so it reads normally.
    """
    if severity == "Extreme" and active:
        return "urgent"
    if severity in ("Extreme", "Severe"):
        return "standard"
    return "gentle"


# ---------------------------------------------------------------------
# Instruction handling -- trim, never rewrite
# ---------------------------------------------------------------------
def clean_instruction(text: str | None) -> str:
    """
    Normalise NWS instruction text for display.

    NWS products are teletype: hard-wrapped at ~66 columns with newlines mid
    sentence. Collapsing that whitespace is formatting, not editing -- the
    words are untouched.
    """
    if not text:
        return ""
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed


def trim_to_sentences(text: str, max_chars: int = MAX_INSTRUCTION_CHARS) -> str:
    """
    Cut at a sentence boundary, never mid-sentence.

    Half a safety instruction is worse than a shorter complete one -- "move to
    an interior room on the lowest floor and" is a dangerous fragment.
    """
    text = clean_instruction(text)
    if not text or len(text) <= max_chars:
        return text

    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for sentence in sentences:
        candidate = f"{out} {sentence}".strip()
        if len(candidate) > max_chars:
            break
        out = candidate
    # If even the first sentence is too long, keep it whole rather than
    # truncating official guidance mid-clause.
    return out or sentences[0]


def property_checks(event_type: str, service_type: str) -> list[str]:
    return HAZARD_CHECKS.get(
        (event_type, service_type), PROPERTY_CHECKS.get(service_type, [])
    )


def _first_name(full_name: str | None) -> str:
    return (full_name or "there").split()[0]


def _until(expires_at) -> str:
    if not expires_at:
        return ""
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return ""
    return f", in effect until {expires_at:%-I:%M %p}" if hasattr(expires_at, "hour") else ""


# ---------------------------------------------------------------------
# The notice
# ---------------------------------------------------------------------
def build_safety_notice(
    *,
    name: str | None,
    city: str | None,
    event_type: str,
    instruction: str | None,
    service_type: str,
    expires_at=None,
    rep_name: str | None = None,
    tenant: str | None = None,
    severity: str | None = None,
    state: str | None = None,
    is_prospect: bool = False,
    tenure_start=None,
    now: datetime | None = None,
) -> dict:
    """
    Compose the notice. Pure -- no database, no model, no clock except what is
    passed in.

    Message order follows what someone actually needs from an unexpected text:

        1. Who is this
        2. Why am I getting it
        3. What is happening, and who says so
        4. What should I do
        5. What if it goes wrong  <- the open door
        6. Who to thank, how to stop

    Every line has to earn its place. There is no "hope this finds you well".
    """
    now = now or datetime.now(timezone.utc)
    active = _still_active(expires_at, now)
    tier = tone_tier(severity, active)
    voice = TONE[tier]

    guidance = trim_to_sentences(instruction)
    checks = property_checks(event_type, service_type)
    company = company_name(tenant)
    phone = company_phone(tenant)
    rep = _first_name(rep_name) if rep_name else None

    hold, hold_reason = hold_for_quiet_hours(tier, state, now)

    # 1. Who is this -- first six words, because that is the real question a
    #    text from an unknown number raises.
    lines = [
        f"Hi {_first_name(name)} — this is {rep} with {company}." if rep
        else f"Hi {_first_name(name)} — this is {company}."
    ]

    # 2. Why am I getting this. Skipped in the urgent tier: mid-tornado, the
    #    provenance of our mailing list is not what matters.
    if tier != "urgent":
        lines.append(
            relationship_line(
                is_prospect=is_prospect, tenure_start=tenure_start,
                city=city, service_type=service_type,
            )
        )

    # 3. What is happening -- attributed to NWS, not to us. We are the
    #    messenger, and saying so is both accurate and more trustworthy.
    window = _until(expires_at)
    lines += [
        "",
        f"The National Weather Service has issued a {event_type} for "
        f"{city or 'your area'}{window}:",
    ]
    if guidance:
        lines += ["", guidance]

    # 4. What to do -- deferred entirely while the event is still dangerous.
    if checks and not voice["defer_checks"]:
        lead = "Once this passes, worth a look:" if active else "Worth a look when you get a chance:"
        lines += ["", lead] + [f"• {c}" for c in checks[:3]]

    # 5. The open door. Costs nothing unless someone actually needs it.
    help_text = None if voice["defer_checks"] else help_line(service_type, rep, phone)
    if help_text:
        lines += ["", help_text]

    # 6. Sign off. A promise here is tracked (see follow_up_due), because an
    #    unkept promise is worse than none.
    if voice["close"]:
        lines += ["", voice["close"]]
    if rep:
        lines += ["", f"— {rep}, {company}"]
    lines += ["", DISCLAIMER, OPT_OUT_LINE]

    message = "\n".join(lines).strip()

    follow_up_due = None
    if voice["promises_follow_up"]:
        # We said we would check in. This is when that becomes due.
        follow_up_due = expires_at if _still_active(expires_at, now) else now

    return {
        "message_text": message,
        "tone": tier,
        "rep_name": rep,
        "company": company,
        "callback_phone": phone,
        "guidance_source": "National Weather Service (public domain)" if guidance else None,
        "guidance_verbatim": bool(guidance),
        "checks_used": [] if voice["defer_checks"] else checks[:3],
        "checks_deferred": bool(checks) and voice["defer_checks"],
        "offers_help": bool(help_text),
        "promises_follow_up": voice["promises_follow_up"],
        "follow_up_due": follow_up_due,
        "hold_for_quiet_hours": hold,
        "hold_reason": hold_reason,
        "generated": False,
        "chars": len(message),
    }


# ---------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------
# Selling to someone during an in-progress life-threatening event is the line.
# Extreme severity means tornado, hurricane, or equivalent -- while that is
# still active, safety notices only.
BLOCKING_SEVERITIES = {"Extreme"}


def commercial_allowed(
    *,
    safety_sent: bool,
    severity: str | None,
    expires_at=None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """
    May we send a commercial message for this opportunity yet?

    Two rules, in order:

      1. A safety notice must have gone first. The first thing we ever send is
         help. This is sequencing, not timing, so it is always demonstrable.
      2. No selling during an ACTIVE Extreme-severity event. Nobody wants a
         roofing quote while the tornado is still on the ground.

    Returns (allowed, reason). The reason is surfaced in the UI rather than
    failing silently.
    """
    now = now or datetime.now(timezone.utc)

    if not safety_sent:
        return False, "Send the storm safety notice first — that always goes before any pitch."

    if severity in BLOCKING_SEVERITIES and _still_active(expires_at, now):
        return False, (
            f"{severity} alert is still active. Commercial outreach is held until it expires."
        )

    return True, "Safety notice sent and the alert has passed its active window."


def _still_active(expires_at, now: datetime) -> bool:
    if not expires_at:
        return False
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now
