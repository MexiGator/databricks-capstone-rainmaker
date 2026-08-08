"""
Rainmaker -- inbound reply classification.

interested | question | not_now

Rules, not an LLM call, and that is a deliberate choice. This function decides
whether a booking gets written to the database. It runs on every reply, needs
to be deterministic, needs to be unit-testable, and must not cost a network
round trip or fail during a demo. `classify_with_llm` is available as a
fallback for genuinely ambiguous text.

Pure Python, no database, no imports beyond stdlib -- so the test suite runs
in milliseconds.
"""

from __future__ import annotations

import re

# Order matters within a category, but categories are scored independently
# and the highest score wins.

INTERESTED_PATTERNS = [
    (r"\byes\b", 3.0),
    (r"\byep\b|\byeah\b|\bsure\b|\bok(ay)?\b", 2.0),
    (r"\bplease (do|come|send|schedule)\b", 3.0),
    (r"\b(book|schedule|set up|come out|send someone|swing by)\b", 2.5),
    (r"\b(sounds good|works for me|let'?s do it|i'?m in|count me in)\b", 3.0),
    (r"\b(when can you|how soon|earliest|asap|today|tomorrow)\b", 2.0),
    (r"\b(interested|definitely|absolutely)\b", 2.0),
    (r"\bi (do )?(have|noticed|found|saw)\b.*\b(damage|leak|water|crack)\b", 2.5),
]

QUESTION_PATTERNS = [
    (r"\?", 2.0),
    (r"\bhow much\b|\bwhat.{0,10}cost\b|\bprice\b|\bquote\b|\bfree\b", 2.5),
    (r"\bhow long\b|\bwhat does it involve\b|\bwhat happens\b", 2.0),
    (r"\bdo (you|i) need\b|\bis (it|this) covered\b|\binsurance\b|\bdeductible\b", 2.5),
    (r"\bwho (is|are) (this|you)\b|\bwhat company\b", 2.0),
    (r"\bcan you (explain|tell me)\b", 2.0),
]

NOT_NOW_PATTERNS = [
    (r"\bno\b(?!\w)", 2.0),
    (r"\bnot (right now|now|interested|at this time|this year)\b", 3.5),
    (r"\b(maybe )?later\b|\bnext (month|year|season|spring)\b", 2.5),
    (r"\b(all set|already (had|have|got)|taken care of|handled)\b", 3.0),
    (r"\b(stop|unsubscribe|remove me|don'?t contact)\b", 4.0),
    (r"\bbusy\b|\btravel(ing|ling)\b|\bout of town\b", 1.5),
    (r"\bno thanks?\b|\bpass\b", 3.0),
]

CATEGORIES = {
    "interested": INTERESTED_PATTERNS,
    "question": QUESTION_PATTERNS,
    "not_now": NOT_NOW_PATTERNS,
}

# Below this, escalate to a human rather than auto-booking.
CONFIDENCE_FLOOR = 0.45

# An opt-out must never be overridden by an enthusiastic-sounding word
# elsewhere in the message. This is a compliance issue, not a scoring one.
HARD_OPT_OUT = re.compile(r"\b(stop|unsubscribe|remove me|do ?n[o']?t contact)\b", re.I)

# An explicit "yes" is the single most decisive token a reply can contain.
# Without this boost, "Yes please, but how much will it cost?" scores as a
# question and gets routed to a human -- losing a customer who just said yes.
# A bonus rather than a hard override, so genuinely mixed replies still lose
# confidence and fall below the auto-book floor.
EXPLICIT_YES = re.compile(r"\b(yes|yeah|yep)\b|^\s*(sure|ok(ay)?)\b", re.I)
EXPLICIT_YES_BONUS = 3.0


def _score(text: str, patterns: list[tuple[str, float]]) -> float:
    return sum(weight for pattern, weight in patterns if re.search(pattern, text, re.I))


def classify(reply_text: str) -> tuple[str, float]:
    """
    Return (intent, confidence 0..1).

    Confidence is the winning category's share of total signal, so a message
    hitting one category cleanly scores high and a mixed message scores low --
    which is exactly when a human should look at it.
    """
    text = (reply_text or "").strip()
    if not text:
        return "question", 0.0

    if HARD_OPT_OUT.search(text):
        return "not_now", 1.0

    scores = {name: _score(text, pats) for name, pats in CATEGORIES.items()}
    if EXPLICIT_YES.search(text):
        scores["interested"] += EXPLICIT_YES_BONUS
    total = sum(scores.values())

    if total == 0:
        # No signal at all. Treat as a question so it reaches a human
        # rather than being auto-booked or silently dropped.
        return "question", 0.0

    intent = max(scores, key=lambda k: scores[k])
    confidence = scores[intent] / total

    # "Yes, but how much does it cost?" is interested AND a question.
    # Booking it is the right call -- the rep answers pricing on the call.
    if intent == "question" and scores["interested"] >= scores["question"] * 0.8:
        intent = "interested"
        confidence = scores["interested"] / total

    return intent, round(confidence, 3)


def should_auto_book(intent: str, confidence: float) -> bool:
    """Only a confident 'interested' books itself. Everything else waits for
    a human -- the defensible production posture."""
    return intent == "interested" and confidence >= CONFIDENCE_FLOOR
