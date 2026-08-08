"""
Tests for storm safety notices and the commercial gate.

Two things are being protected here:
  1. Safety guidance is never invented, never truncated mid-sentence.
  2. A pitch cannot go out before help does.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent import safety

NWS_INSTRUCTION = (
    "Move to an interior room on the lowest floor of a sturdy building.\n"
    "Avoid windows. If you are outdoors, in a mobile home, or in a vehicle,\n"
    "move to the closest substantial shelter and protect yourself from\n"
    "flying debris. Do not attempt to outrun the storm in a vehicle."
)

NOW = datetime(2026, 8, 8, 20, 0, tzinfo=timezone.utc)  # 3pm in Texas


# ---------------------------------------------------------------------
# Instruction text is trimmed, never rewritten
# ---------------------------------------------------------------------
def test_teletype_newlines_are_collapsed_not_words_changed():
    out = safety.clean_instruction(NWS_INSTRUCTION)
    assert "\n" not in out
    assert "Move to an interior room on the lowest floor" in out
    assert "flying debris" in out


def test_none_instruction_returns_empty_not_crash():
    assert safety.clean_instruction(None) == ""


def test_trim_never_cuts_mid_sentence():
    """'Move to an interior room and' is a dangerous fragment. Every trimmed
    notice must end on a complete sentence."""
    out = safety.trim_to_sentences(NWS_INSTRUCTION, max_chars=90)
    assert out.rstrip().endswith((".", "!", "?"))


def test_trim_respects_the_budget():
    out = safety.trim_to_sentences(NWS_INSTRUCTION, max_chars=90)
    assert len(out) <= 120  # one sentence may slightly exceed; never doubles it


def test_short_instruction_passes_through_whole():
    short = "Avoid travel. Stay indoors."
    assert safety.trim_to_sentences(short) == short


def test_single_overlong_sentence_is_kept_whole():
    """Better a long complete instruction than a truncated one."""
    long_one = "Move immediately to the lowest floor of a sturdy building and stay away from all windows until the warning expires."
    out = safety.trim_to_sentences(long_one, max_chars=40)
    assert out == long_one


# ---------------------------------------------------------------------
# The notice itself
# ---------------------------------------------------------------------
def _notice(**kw):
    base = dict(
        name="Marcus Alvarez", city="Fort Worth", event_type="Severe Thunderstorm Warning",
        instruction=NWS_INSTRUCTION, service_type="roofing",
        expires_at=NOW + timedelta(hours=1),
        rep_name="Dana Ramirez", tenant="summit-exteriors",
        severity="Severe", state="TX", now=NOW,
        is_prospect=False, tenure_start=datetime(2019, 4, 2),
    )
    base.update(kw)
    return safety.build_safety_notice(**base)


def test_notice_is_never_generated():
    n = _notice()
    assert n["generated"] is False
    assert n["guidance_verbatim"] is True


def test_notice_quotes_nws_verbatim():
    n = _notice()
    assert "Move to an interior room on the lowest floor" in n["message_text"]
    assert "National Weather Service" in n["message_text"]


def test_notice_answers_who_is_this_in_the_first_line():
    """The number one anxiety with a text from an unknown number."""
    first_line = _notice()["message_text"].splitlines()[0]
    assert "Marcus" in first_line
    assert "Dana" in first_line
    assert "Summit Exteriors" in first_line


def test_notice_names_the_customers_city():
    assert "Fort Worth" in _notice()["message_text"]


def test_notice_is_signed_by_the_assigned_rep():
    """Signed by the person who would actually pick up the phone -- not a
    persona, which becomes awkward the moment the customer calls back."""
    assert "— Dana, Summit Exteriors" in _notice()["message_text"]


def test_notice_carries_no_ask():
    """The whole point. No booking, no pitch, no call to action."""
    text = _notice()["message_text"].lower()
    for word in ["inspection", "book", "schedule", "reply yes", "quote", "estimate", "free"]:
        assert word not in text, f"safety notice contains a sales cue: {word!r}"


def test_notice_always_carries_opt_out():
    assert "STOP" in _notice()["message_text"]


def test_notice_says_it_is_not_a_sales_message():
    assert "nothing to buy" in _notice()["message_text"].lower()


def test_notice_never_claims_damage_occurred():
    text = _notice()["message_text"].lower()
    for phrase in ["your roof was damaged", "damage to your", "you have damage"]:
        assert phrase not in text


def test_missing_instruction_still_produces_a_usable_notice():
    """Zone-only alerts sometimes carry no instruction. The notice degrades to
    the property checklist rather than failing."""
    n = _notice(instruction=None)
    assert n["guidance_verbatim"] is False
    assert "worth a look" in n["message_text"].lower()
    assert "STOP" in n["message_text"]


def test_missing_name_does_not_produce_none():
    assert "None" not in _notice(name=None)["message_text"]


# ---------------------------------------------------------------------
# Property checks
# ---------------------------------------------------------------------
def test_checks_match_the_service_line():
    assert "granules" in " ".join(safety.property_checks("Severe Thunderstorm Warning", "roofing"))
    assert "hose bibs" in " ".join(safety.property_checks("Hard Freeze Warning", "plumbing"))


def test_hazard_override_beats_the_generic_list():
    ice = safety.property_checks("Ice Storm Warning", "roofing")
    generic = safety.property_checks("Severe Thunderstorm Warning", "roofing")
    assert ice != generic
    assert "ice dam" in " ".join(ice).lower()


def test_unknown_service_returns_empty_not_crash():
    assert safety.property_checks("Tornado Warning", "landscaping") == []


def test_every_service_line_has_checks():
    for service in ("roofing", "plumbing", "hvac", "restoration"):
        assert len(safety.PROPERTY_CHECKS[service]) >= 2


def test_checks_are_observations_not_diagnoses():
    """Checks describe what to look for, never assert damage happened."""
    for checks in safety.PROPERTY_CHECKS.values():
        for c in checks:
            assert not c.lower().startswith("your ")


# ---------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------
def test_no_pitch_before_a_safety_notice():
    allowed, reason = safety.commercial_allowed(
        safety_sent=False, severity="Severe", expires_at=None, now=NOW
    )
    assert not allowed
    assert "safety notice first" in reason


def test_no_pitch_during_an_active_extreme_event():
    """Nobody wants a roofing quote while the tornado is still on the ground."""
    allowed, reason = safety.commercial_allowed(
        safety_sent=True, severity="Extreme", expires_at=NOW + timedelta(hours=2), now=NOW
    )
    assert not allowed
    assert "still active" in reason


def test_pitch_allowed_once_the_extreme_event_expires():
    allowed, _ = safety.commercial_allowed(
        safety_sent=True, severity="Extreme", expires_at=NOW - timedelta(hours=1), now=NOW
    )
    assert allowed


def test_severe_events_do_not_block_once_safety_is_sent():
    """Only Extreme blocks. A severe thunderstorm warning is not a
    life-threatening in-progress event in the same sense."""
    allowed, _ = safety.commercial_allowed(
        safety_sent=True, severity="Severe", expires_at=NOW + timedelta(hours=2), now=NOW
    )
    assert allowed


def test_gate_reason_is_always_human_readable():
    for sent in (True, False):
        _, reason = safety.commercial_allowed(
            safety_sent=sent, severity="Extreme", expires_at=NOW + timedelta(hours=1), now=NOW
        )
        assert len(reason) > 20 and reason[0].isupper()


def test_missing_expiry_does_not_block():
    allowed, _ = safety.commercial_allowed(
        safety_sent=True, severity="Extreme", expires_at=None, now=NOW
    )
    assert allowed


def test_naive_timestamp_does_not_crash_the_gate():
    allowed, _ = safety.commercial_allowed(
        safety_sent=True, severity="Extreme",
        expires_at=datetime(2026, 8, 8, 22, 0), now=NOW,
    )
    assert not allowed


def test_string_timestamp_is_parsed():
    allowed, _ = safety.commercial_allowed(
        safety_sent=True, severity="Extreme", expires_at="2026-08-08T22:00:00+00:00", now=NOW
    )
    assert not allowed


def test_unparseable_timestamp_fails_open_rather_than_blocking_forever():
    allowed, _ = safety.commercial_allowed(
        safety_sent=True, severity="Extreme", expires_at="not a date", now=NOW
    )
    assert allowed


# ---------------------------------------------------------------------
# Tone tiers -- warmth scales DOWN as the hazard scales up
# ---------------------------------------------------------------------
def test_active_extreme_event_gets_the_urgent_voice():
    assert safety.tone_tier("Extreme", active=True) == "urgent"


def test_expired_extreme_event_reads_normally():
    """An expired tornado warning is a cleanup conversation, not an emergency."""
    assert safety.tone_tier("Extreme", active=False) == "standard"


def test_moderate_events_get_the_gentle_voice():
    assert safety.tone_tier("Moderate", active=True) == "gentle"
    assert safety.tone_tier(None, active=True) == "gentle"


def test_urgent_tier_skips_the_relationship_line():
    """Mid-tornado, the provenance of our mailing list is not what matters."""
    n = _notice(event_type="Tornado Warning", severity="Extreme",
                expires_at=NOW + timedelta(minutes=20))
    assert n["tone"] == "urgent"
    assert "been with us since" not in n["message_text"]
    assert "Hope you" not in n["message_text"]


def test_property_checklist_is_deferred_during_an_urgent_event():
    """Not the moment to hand someone a to-do list about their gutters."""
    n = _notice(event_type="Tornado Warning", severity="Extreme",
                expires_at=NOW + timedelta(minutes=20))
    assert n["checks_deferred"] is True
    assert n["checks_used"] == []
    assert "follow up once it's clear" in n["message_text"]


def test_warmth_returns_for_lower_severity():
    n = _notice(event_type="Heat Advisory", severity="Moderate", service_type="hvac")
    assert n["tone"] == "gentle"
    assert n["checks_used"]
    assert n["offers_help"]


def test_urgent_notice_is_meaningfully_shorter():
    urgent = _notice(event_type="Tornado Warning", severity="Extreme",
                     expires_at=NOW + timedelta(minutes=20))
    gentle = _notice(event_type="Heat Advisory", severity="Moderate", service_type="hvac")
    assert urgent["chars"] < gentle["chars"]


def test_every_tier_still_carries_opt_out_and_disclaimer():
    for sev, exp in [("Extreme", NOW + timedelta(minutes=20)), ("Severe", NOW + timedelta(hours=1)), ("Minor", None)]:
        text = _notice(severity=sev, expires_at=exp)["message_text"]
        assert "STOP" in text
        assert "not a sales pitch" in text


def test_no_exclamation_marks_are_added_by_us():
    """NWS text may contain them (TAKE COVER NOW!). Our own copy must not --
    exclamation marks in a storm message read as chirpy."""
    n = _notice()
    ours = n["message_text"].replace(safety.trim_to_sentences(NWS_INSTRUCTION), "")
    assert "!" not in ours


# ---------------------------------------------------------------------
# Company naming
# ---------------------------------------------------------------------
def test_known_tenant_gets_its_display_name():
    assert safety.company_name("desert-air-hvac") == "Desert Air Heating & Cooling"


def test_unknown_tenant_falls_back_to_a_readable_name():
    """A new tenant must work without a code change."""
    assert safety.company_name("lakeshore-roofing") == "Lakeshore Roofing"


def test_missing_tenant_does_not_render_none():
    assert "None" not in _notice(tenant=None)["message_text"]


def test_missing_rep_drops_the_signature_cleanly():
    n = _notice(rep_name=None)
    assert "None" not in n["message_text"]
    assert "—" not in n["message_text"].split("Reply STOP")[0].splitlines()[-2]


# ---------------------------------------------------------------------
# "Why am I getting this" -- earned differently per relationship
# ---------------------------------------------------------------------
def test_long_standing_customer_hears_their_tenure():
    assert "since 2019" in _notice()["message_text"]


def test_prospect_is_not_told_they_are_a_customer():
    """Claiming a relationship that does not exist is the fastest way to lose
    a prospect's trust."""
    n = _notice(is_prospect=True, tenure_start=None)
    text = n["message_text"]
    assert "been with us" not in text
    assert "one of our customers" not in text
    assert "We work around Fort Worth" in text


def test_customer_without_tenure_still_gets_a_reason():
    n = _notice(is_prospect=False, tenure_start=None)
    assert "one of our customers" in n["message_text"]


def test_no_filler_pleasantries_anywhere():
    """'Hope this finds you well' occupies the most valuable line in the
    message and does no work. Every line has to earn its place."""
    for kw in [dict(), dict(is_prospect=True), dict(severity="Moderate")]:
        text = _notice(**kw)["message_text"].lower()
        for filler in ["hope this", "hope you and your family", "just wanted to reach out",
                       "trust you are well", "hope all is well"]:
            assert filler not in text


# ---------------------------------------------------------------------
# The open door
# ---------------------------------------------------------------------
def test_notice_gives_a_way_to_reach_a_human():
    """A safety notice that offers help without a phone number is a gesture,
    not help."""
    n = _notice()
    assert n["offers_help"]
    assert "(817) 555-0142" in n["message_text"]
    assert "ask for Dana" in n["message_text"]


def test_help_line_is_service_specific():
    assert "a pipe let go" in _notice(service_type="plumbing")["message_text"]
    assert "the system quit" in _notice(service_type="hvac")["message_text"]


def test_disclaimer_no_longer_discourages_replying():
    """The first version said 'no reply needed' two lines after inviting a
    call. Mixed instruction, and the wrong half reaches the person in trouble."""
    assert "no reply needed" not in _notice()["message_text"].lower()


def test_urgent_tier_does_not_offer_the_help_line():
    n = _notice(event_type="Tornado Warning", severity="Extreme",
                expires_at=NOW + timedelta(minutes=20))
    assert n["offers_help"] is False


def test_unknown_tenant_has_no_phone_and_degrades_quietly():
    n = _notice(tenant="lakeshore-roofing")
    assert n["callback_phone"] is None
    assert n["offers_help"] is False
    assert "None" not in n["message_text"]


# ---------------------------------------------------------------------
# Promises get tracked
# ---------------------------------------------------------------------
def test_urgent_notice_records_when_the_follow_up_is_due():
    """It says 'I'll follow up once it's clear'. An unkept promise is worse
    than no promise, so the due time is captured rather than remembered."""
    expiry = NOW + timedelta(minutes=25)
    n = _notice(event_type="Tornado Warning", severity="Extreme", expires_at=expiry)
    assert n["promises_follow_up"] is True
    assert n["follow_up_due"] == expiry


def test_tiers_that_promise_nothing_schedule_nothing():
    n = _notice()
    assert n["promises_follow_up"] is False
    assert n["follow_up_due"] is None


# ---------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------
def test_gentle_notice_is_held_overnight():
    """A heat advisory notice at 3am destroys more trust than it protects."""
    three_am_texas = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)
    hold, reason = safety.hold_for_quiet_hours("gentle", "TX", three_am_texas)
    assert hold
    assert "Holding until" in reason


def test_urgent_notice_is_never_held():
    """An active tornado at 3am is exactly when someone needs waking up."""
    three_am_texas = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)
    hold, _ = safety.hold_for_quiet_hours("urgent", "TX", three_am_texas)
    assert not hold


def test_standard_notice_is_never_held():
    three_am_texas = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)
    hold, _ = safety.hold_for_quiet_hours("standard", "TX", three_am_texas)
    assert not hold


def test_daytime_gentle_notice_sends():
    hold, _ = safety.hold_for_quiet_hours("gentle", "TX", NOW)
    assert not hold


def test_unknown_state_fails_open_rather_than_dropping_the_message():
    """Failing open beats silently withholding a safety message."""
    hold, reason = safety.hold_for_quiet_hours("gentle", "ZZ", NOW)
    assert not hold
    assert "unknown" in reason.lower()


def test_local_hour_respects_the_timezone_offset():
    noon_utc = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)
    assert safety.local_hour("TX", noon_utc) == 12   # UTC-6
    assert safety.local_hour("CA", noon_utc) == 10   # UTC-8
    assert safety.local_hour("NY", noon_utc) == 13   # UTC-5


def test_every_seeded_state_has_a_timezone():
    """A state in the CRM with no offset would silently never be quiet-hour
    protected."""
    seeded = {"TX","OK","CO","KS","NE","IA","MO","MN","WI","IL","NY","MA","CT",
              "LA","AL","FL","SC","TN","AZ","NV","NM","CA","GA","NC","VA"}
    missing = seeded - set(safety.STATE_UTC_OFFSET)
    assert not missing, f"no timezone for {missing}"
