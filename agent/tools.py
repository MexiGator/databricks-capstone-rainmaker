"""
Rainmaker -- the three agent tools (requirement #5).

  Tool 1  draft_outreach        -> writes `outreach`, advances -> drafted
  Tool 2  send_and_create_lead  -> writes the send, advances -> sent
  Tool 3  handle_reply_and_book -> writes `bookings`,  advances -> booked

All three have real database side-effects. None of them is "generate more
text and hand it back" -- that was the trap called out in the design doc.

Each returns a plain dict so Agent Bricks can serialise the result and the
app can render it directly.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import os as _os
import sys as _sys

# Resolve paths from THIS file, not the working directory -- the app, the
# notebook, and pytest all run from different cwds.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "db"), _os.path.join(_ROOT, "pipeline")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import lakebase
from agent import classify, llm, retrieval, safety

# Appointments are offered starting this far out -- a same-hour slot is not
# credible and a next-week slot loses the urgency the storm created.
FIRST_SLOT_HOURS = 20
SLOT_SPACING_HOURS = 3


# =====================================================================
# Shared helpers
# =====================================================================
def _load_opportunity(opportunity_id: str) -> dict | None:
    with lakebase.cursor(dict_rows=True) as cur:
        cur.execute(
            """
            SELECT o.opportunity_id, o.status, o.service_needed, o.est_value, o.tenant,
                   o.priority, o.exposure_score, o.assigned_rep, o.distance_km,
                   c.customer_id, c.name, c.city, c.state, c.phone, c.tier,
                   c.is_prospect, c.tenure_start,
                   w.event_id, w.event_type, w.headline, w.severity,
                   w.narrative_text, w.payload, w.expires_at,
                   EXISTS (SELECT 1 FROM outreach x
                            WHERE x.opportunity_id = o.opportunity_id
                              AND x.kind = 'safety' AND x.status = 'sent') AS safety_sent
            FROM opportunities o
            JOIN customers      c ON c.customer_id = o.customer_id
            JOIN weather_events w ON w.event_id    = o.weather_event_id
            WHERE o.opportunity_id = %s
            """,
            (opportunity_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _set_status(opportunity_id: str, status: str) -> None:
    """The status-history trigger records the transition automatically."""
    with lakebase.cursor() as cur:
        cur.execute(
            "UPDATE opportunities SET status = %s WHERE opportunity_id = %s",
            (status, opportunity_id),
        )


def _first_name(full_name: str) -> str:
    return (full_name or "there").split()[0]


def _distance_phrase(distance_km: float | None) -> str:
    """
    Describe distance for the draft prompt.

    Zone-based NWS alerts (heat, most Special Weather Statements) carry no
    polygon, so there is no centre to measure from and distance is genuinely
    null. A format string assuming a number crashes the draft on exactly
    those alerts -- which are the ones the state-gating path exists to serve.
    """
    if distance_km is None:
        return "not applicable — this is a zone-wide alert with no storm centre"
    return f"{distance_km:.0f} km"


def _templated_draft(opp: dict, channel: str) -> str:
    """Deterministic, safety-compliant outreach used ONLY when the model/SDK call
    fails mid-demo. Same discipline as the prompt's RULES: names the person, city
    and event, makes ONE ask, and never claims damage has occurred or invents a
    price or date. This degrades the draft path to a plain mail-merge instead of
    hard-failing the tool -- a model hiccup shouldn't be able to break draft->send->book."""
    first = _first_name(opp.get("name"))
    city = opp.get("city") or "your area"
    event = opp.get("event_type") or "recent severe weather"
    service = opp.get("service_needed") or "property"
    msg = (
        f"{first}, this is a note from your {service} team. With {event} moving "
        f"through {city}, it's worth having your {service} checked for "
        f"weather-related wear before any problem grows. Reply YES and we'll "
        f"schedule a quick inspection at no obligation."
    )
    return msg[:480]


# =====================================================================
# TOOL 0 -- the storm safety notice (always first, never generated)
# =====================================================================
def send_safety_notice(opportunity_id: str, channel: str = "sms") -> dict:
    """
    Send official NWS safety guidance to an exposed customer, with no ask
    attached.

    This is the first message Rainmaker ever sends anyone. It makes NO model
    call: the guidance is the NWS instruction field verbatim (public domain)
    and the property checks are a curated list. Nothing here can hallucinate,
    and it still works when the serving endpoint is down.

    Idempotent -- one safety notice per opportunity.
    """
    opp = _load_opportunity(opportunity_id)
    if not opp:
        return {"ok": False, "error": f"No opportunity {opportunity_id}"}

    if opp["safety_sent"]:
        return {"ok": True, "already_sent": True, "opportunity_id": opportunity_id}

    # The instruction block lives in the raw NWS payload; narrative_text is
    # description + instruction concatenated for embedding.
    payload = opp.get("payload") or {}
    instruction = payload.get("instruction") if isinstance(payload, dict) else None
    if not instruction:
        instruction = opp.get("narrative_text")

    notice = safety.build_safety_notice(
        name=opp["name"],
        city=opp["city"],
        event_type=opp["event_type"],
        instruction=instruction,
        service_type=opp["service_needed"],
        expires_at=opp.get("expires_at"),
        # Signed by the rep who would actually pick up the phone. Inventing a
        # persona would be warmer for one message and awkward the moment the
        # customer calls back and asks for someone who does not exist.
        rep_name=opp.get("assigned_rep"),
        tenant=opp.get("tenant"),
        severity=opp.get("severity"),
        state=opp.get("state"),
        is_prospect=opp.get("is_prospect", False),
        tenure_start=opp.get("tenure_start"),
    )

    # Quiet hours: a heat advisory at 3am destroys more trust than it protects.
    # Only the gentle tier is ever held -- a real hazard outranks a good night.
    if notice["hold_for_quiet_hours"]:
        return {
            "ok": False, "held": True, "reason": notice["hold_reason"],
            "opportunity_id": opportunity_id, "preview": notice["message_text"],
        }

    with lakebase.cursor() as cur:
        cur.execute(
            """
            INSERT INTO outreach
                (opportunity_id, kind, message_text, channel, status,
                 approved_by, sent_at, follow_up_due)
            VALUES (%s, 'safety', %s, %s, 'sent', 'system', now(), %s)
            RETURNING outreach_id
            """,
            (opportunity_id, notice["message_text"], channel, notice["follow_up_due"]),
        )
        outreach_id = cur.fetchone()[0]

    # Deliberately does NOT advance opportunity status. A safety notice is not
    # a step in the sales funnel and must not inflate the sent count.
    return {
        "ok": True,
        "outreach_id": outreach_id,
        "opportunity_id": opportunity_id,
        "customer": opp["name"],
        "kind": "safety",
        **notice,
    }


# =====================================================================
# TOOL 1 -- draft personalised outreach, grounded on the best past campaign
# =====================================================================
def draft_outreach(opportunity_id: str, channel: str = "sms") -> dict:
    """
    Draft a message for one opportunity.

    Grounding chain: the live NWS event + this customer's CRM profile + the
    retrieved best-performing past campaign for this hazard/service pair.
    The retrieval is what makes this RAG rather than a templated mail-merge --
    the model is told what wording actually booked jobs before.

    Side-effect: inserts into `outreach`, advances status -> drafted.
    """
    opp = _load_opportunity(opportunity_id)
    if not opp:
        return {"ok": False, "error": f"No opportunity {opportunity_id}"}

    # Safety first, and not as a slogan -- this is a hard gate.
    allowed, reason = safety.commercial_allowed(
        safety_sent=opp["safety_sent"],
        severity=opp["severity"],
        expires_at=opp.get("expires_at"),
    )
    if not allowed:
        return {"ok": False, "blocked": True, "reason": reason, "needs_safety_notice": not opp["safety_sent"]}

    template = retrieval.best_template(opp["event_type"], opp["service_needed"])

    if template:
        grounding = (
            f"BEST PAST CAMPAIGN (booked {float(template.metadata.get('past_booked_rate', 0)) * 100:.0f}% "
            f"of sends):\n{template.chunk_text}"
        )
        template_id = template.source_id
        similarity = round(template.similarity, 3)
    else:
        # Honest degradation: no strong match means write from the event
        # alone and say so, rather than silently pretending we grounded.
        grounding = "No strong past-campaign match was retrieved. Write from the weather event alone."
        template_id = None
        similarity = None

    relationship = (
        "This is a PROSPECT — they have never bought from us. Do not imply an existing relationship."
        if opp["is_prospect"]
        else f"This is an EXISTING customer ({opp['tier']} tier) since {opp['tenure_start']}."
    )

    prompt = f"""You are writing a single outreach {channel.upper()} for a home-services company.

CUSTOMER: {opp['name']} in {opp['city']}, {opp['state']}
RELATIONSHIP: {relationship}
SERVICE LINE: {opp['service_needed']}
LIVE WEATHER EVENT: {opp['event_type']} — {opp['headline']}
SEVERITY: {opp['severity']}
DISTANCE FROM STORM CENTRE: {_distance_phrase(opp['distance_km'])}

{grounding}

RULES:
- Under 480 characters. This is a text message, not an email.
- Open with their first name. Reference their actual city and the actual event.
- Name one specific, concrete risk to their property from THIS hazard.
- One clear ask: reply YES to book an inspection.
- No emojis. No exclamation marks. No invented statistics, prices, or dates.
- Do not claim damage has occurred — we have not inspected. Say it is worth checking.

Write only the message body."""

    # A model or SDK hiccup during the live demo must degrade to a templated
    # draft, not hard-fail the tool. The retrieval/grounding above already
    # happened, so the row still records what it was grounded on.
    try:
        message = llm.complete(prompt, max_tokens=300).strip()
        if not message:
            raise ValueError("empty completion")
        degraded = False
    except Exception as exc:  # noqa: BLE001 - deliberate: keep draft->send->book alive
        message = _templated_draft(opp, channel)
        degraded = True

    with lakebase.cursor() as cur:
        cur.execute(
            """
            INSERT INTO outreach
                (opportunity_id, template_id, similarity, message_text, channel, status)
            VALUES (%s, %s, %s, %s, %s, 'drafted')
            RETURNING outreach_id
            """,
            (opportunity_id, template_id, similarity, message, channel),
        )
        outreach_id = cur.fetchone()[0]

    _set_status(opportunity_id, "drafted")

    return {
        "ok": True,
        "outreach_id": outreach_id,
        "opportunity_id": opportunity_id,
        "customer": opp["name"],
        "message_text": message,
        "channel": channel,
        # True when the model/SDK call failed and we served the templated draft.
        "degraded": degraded,
        # The grounding panel renders these -- this is the visible proof of RAG.
        "grounded_on": template.title if template else None,
        "template_id": template_id,
        "similarity": similarity,
        "past_booked_rate": float(template.metadata.get("past_booked_rate", 0)) if template else None,
        "status": "drafted",
    }


# =====================================================================
# TOOL 2 -- send it and register the lead (the real side-effect)
# =====================================================================
def send_and_create_lead(
    outreach_id: int,
    approved_by: str = "analyst",
    send_sms: bool = False,
) -> dict:
    """
    Approve and send a drafted message, registering the lead.

    The Lakebase writes ARE the side-effect that satisfies "real action" --
    Twilio is optional polish. If TWILIO_* env vars are absent we record the
    send as queued rather than failing, so the demo never dies on a
    third-party outage.

    Side-effect: outreach -> sent, opportunity -> sent.
    """
    with lakebase.cursor(dict_rows=True) as cur:
        cur.execute(
            """
            SELECT o.outreach_id, o.opportunity_id, o.message_text, o.channel,
                   o.status, o.kind, c.phone, c.name
            FROM outreach o
            JOIN opportunities p ON p.opportunity_id = o.opportunity_id
            JOIN customers     c ON c.customer_id    = p.customer_id
            WHERE o.outreach_id = %s
            """,
            (outreach_id,),
        )
        row = cur.fetchone()

    if not row:
        return {"ok": False, "error": f"No outreach {outreach_id}"}
    if row["status"] == "sent":
        # Idempotent: double-clicking Send must not double-text a customer.
        return {"ok": True, "already_sent": True, "outreach_id": outreach_id}

    delivery = "queued"
    if send_sms:
        delivery = llm.try_send_sms(row["phone"], row["message_text"])

    with lakebase.cursor() as cur:
        cur.execute(
            """
            UPDATE outreach
            SET status = 'sent', approved_by = %s, sent_at = now()
            WHERE outreach_id = %s
            """,
            (approved_by, outreach_id),
        )

    _set_status(row["opportunity_id"], "sent")

    return {
        "ok": True,
        "outreach_id": outreach_id,
        "opportunity_id": row["opportunity_id"],
        "customer": row["name"],
        "channel": row["channel"],
        "delivery": delivery,
        "approved_by": approved_by,
        "status": "sent",
    }


# =====================================================================
# TOOL 3 -- read the reply, classify it, book the appointment
# =====================================================================
def _next_slot(seed_offset: int = 0) -> datetime:
    """Deterministic proposed slot so the demo is repeatable."""
    base = datetime.now(timezone.utc) + timedelta(hours=FIRST_SLOT_HOURS)
    slot = base + timedelta(hours=SLOT_SPACING_HOURS * seed_offset)
    return slot.replace(minute=0, second=0, microsecond=0)


def handle_reply_and_book(reply_id: int) -> dict:
    """
    Close the loop. Read an inbound reply, classify intent, and act:

      interested (confident) -> write a `bookings` row, opportunity -> booked
      interested (unsure)    -> opportunity -> responded, flagged for a human
      question               -> opportunity -> responded
      not_now                -> opportunity -> responded, flagged for nurture

    The booking is an APPOINTMENT, not a sold job. est_value feeds estimated
    pipeline; revenue is only recognised at status 'won'.
    """
    with lakebase.cursor(dict_rows=True) as cur:
        cur.execute(
            """
            SELECT r.reply_id, r.reply_text, r.processed_at,
                   p.opportunity_id, p.customer_id, p.service_needed,
                   p.est_value, p.assigned_rep
            FROM inbound_replies r
            JOIN opportunities   p ON p.opportunity_id = r.opportunity_id
            WHERE r.reply_id = %s
            """,
            (reply_id,),
        )
        row = cur.fetchone()

    if not row:
        return {"ok": False, "error": f"No reply {reply_id}"}
    if row["processed_at"]:
        return {"ok": True, "already_processed": True, "reply_id": reply_id}

    intent, confidence = classify.classify(row["reply_text"])
    auto_book = classify.should_auto_book(intent, confidence)

    with lakebase.cursor() as cur:
        cur.execute(
            "UPDATE inbound_replies SET intent = %s, intent_conf = %s, processed_at = now() "
            "WHERE reply_id = %s",
            (intent, confidence, reply_id),
        )

    result = {
        "ok": True,
        "reply_id": reply_id,
        "opportunity_id": row["opportunity_id"],
        "reply_text": row["reply_text"],
        "intent": intent,
        "confidence": confidence,
        "auto_booked": False,
        "booking_id": None,
        "needs_human": not auto_book,
    }

    if not auto_book:
        _set_status(row["opportunity_id"], "responded")
        result["status"] = "responded"
        result["reason"] = (
            f"intent={intent} confidence={confidence} below auto-book floor "
            f"{classify.CONFIDENCE_FLOOR}" if intent == "interested" else f"intent={intent}"
        )
        return result

    slot = _next_slot(reply_id % 6)
    with lakebase.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bookings
                (opportunity_id, customer_id, service_type, proposed_slot,
                 est_value, assigned_rep, status, booked_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'scheduled', 'agent')
            RETURNING booking_id
            """,
            (
                row["opportunity_id"], row["customer_id"], row["service_needed"],
                slot, row["est_value"], row["assigned_rep"],
            ),
        )
        booking_id = cur.fetchone()[0]

    _set_status(row["opportunity_id"], "booked")

    result.update(
        auto_booked=True,
        booking_id=booking_id,
        proposed_slot=slot.isoformat(),
        assigned_rep=row["assigned_rep"],
        est_value=float(row["est_value"]),
        status="booked",
        needs_human=False,
    )
    return result


def pending_follow_ups() -> list[dict]:
    """
    Safety notices that promised a check-in and have not had one.

    The urgent-tier notice says "I'll follow up once it's clear." This is how
    that promise stays visible instead of evaporating. Note it is fulfilled by
    a second SAFETY message, never by the sales draft -- using a pitch to
    satisfy a safety promise would be a bait and switch.
    """
    with lakebase.cursor(dict_rows=True) as cur:
        cur.execute("""
            SELECT o.outreach_id, o.opportunity_id, o.follow_up_due,
                   c.name, c.city, c.state, w.event_type
            FROM outreach o
            JOIN opportunities  p ON p.opportunity_id = o.opportunity_id
            JOIN customers      c ON c.customer_id    = p.customer_id
            JOIN weather_events w ON w.event_id       = p.weather_event_id
            WHERE o.kind = 'safety'
              AND o.follow_up_due IS NOT NULL
              AND o.follow_up_done IS NULL
              AND o.follow_up_due <= now()
            ORDER BY o.follow_up_due
        """)
        return [dict(r) for r in cur.fetchall()]


# Registry for Agent Bricks tool declaration.
TOOLS = {
    "send_safety_notice": send_safety_notice,
    "draft_outreach": draft_outreach,
    "send_and_create_lead": send_and_create_lead,
    "handle_reply_and_book": handle_reply_and_book,
}
