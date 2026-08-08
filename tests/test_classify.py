"""
Tests for reply classification.

This function decides whether money-shaped rows get written to the database,
so the cases below are the ones that would actually go wrong: mixed signals,
opt-outs buried in polite text, and empty input.
"""

import pytest

from agent import classify


# ---------------------------------------------------------------------
# Clear interest -> should auto-book
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "reply",
    [
        "Yes",
        "yes please come out",
        "Yeah that sounds good, when can you get here?",
        "Sure, book me in",
        "Please schedule an inspection",
        "I'm in. Earliest slot works",
        "Absolutely, I noticed some water damage in the upstairs ceiling",
        "let's do it",
    ],
)
def test_clear_interest_is_classified_interested(reply):
    intent, conf = classify.classify(reply)
    assert intent == "interested", f"{reply!r} -> {intent}"
    assert classify.should_auto_book(intent, conf)


# ---------------------------------------------------------------------
# Questions -> human, never auto-booked
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "reply",
    [
        "How much does this cost?",
        "Is the inspection free?",
        "Who is this?",
        "Will my insurance cover it?",
        "What does the inspection involve?",
        "Can you explain what you found?",
    ],
)
def test_questions_are_classified_question(reply):
    intent, _ = classify.classify(reply)
    assert intent == "question", f"{reply!r} -> {intent}"


def test_questions_never_auto_book():
    intent, conf = classify.classify("How much does this cost?")
    assert not classify.should_auto_book(intent, conf)


# ---------------------------------------------------------------------
# Declines
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "reply",
    [
        "No thanks",
        "Not right now",
        "We're all set",
        "Already had someone out last week",
        "Maybe next spring",
        "Not interested",
        "pass",
    ],
)
def test_declines_are_classified_not_now(reply):
    intent, _ = classify.classify(reply)
    assert intent == "not_now", f"{reply!r} -> {intent}"


def test_declines_never_auto_book():
    for reply in ["No thanks", "Not right now", "We're all set"]:
        intent, conf = classify.classify(reply)
        assert not classify.should_auto_book(intent, conf)


# ---------------------------------------------------------------------
# Opt-out is a compliance hard stop, not a score
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "reply",
    [
        "STOP",
        "unsubscribe",
        "Please remove me from your list",
        "Yes I saw the storm but please stop texting me",
        "Sounds great, but stop contacting me",
    ],
)
def test_opt_out_always_wins_over_positive_language(reply):
    """The last two read as enthusiastic on a naive scorer. Texting them
    again is a TCPA problem, not a missed sale."""
    intent, conf = classify.classify(reply)
    assert intent == "not_now"
    assert conf == 1.0
    assert not classify.should_auto_book(intent, conf)


# ---------------------------------------------------------------------
# Mixed signals -- the interesting cases
# ---------------------------------------------------------------------
def test_yes_with_a_pricing_question_still_books():
    """'Yes, but what will it cost?' is a booking. The rep answers pricing
    on the call. Routing it to a human loses the moment."""
    intent, conf = classify.classify("Yes please, but how much will it cost?")
    assert intent == "interested"
    assert classify.should_auto_book(intent, conf)


def test_ambiguous_reply_goes_to_a_human():
    intent, conf = classify.classify("hmm")
    assert not classify.should_auto_book(intent, conf)


def test_empty_reply_does_not_crash_or_book():
    intent, conf = classify.classify("")
    assert intent == "question"
    assert conf == 0.0
    assert not classify.should_auto_book(intent, conf)


def test_none_reply_does_not_crash():
    intent, conf = classify.classify(None)
    assert not classify.should_auto_book(intent, conf)


def test_unrecognised_text_defaults_to_human_review():
    """Unknown input must reach a person -- never auto-book, never silently drop."""
    intent, conf = classify.classify("asdkjhasd qwe")
    assert intent == "question"
    assert not classify.should_auto_book(intent, conf)


# ---------------------------------------------------------------------
# Confidence behaviour
# ---------------------------------------------------------------------
def test_confidence_is_bounded():
    for reply in ["Yes", "How much?", "No thanks", "", "maybe later but yes"]:
        _, conf = classify.classify(reply)
        assert 0.0 <= conf <= 1.0


def test_unambiguous_scores_higher_than_mixed():
    _, clean = classify.classify("Yes please book me in")
    _, mixed = classify.classify("Yes maybe later I'm not sure")
    assert clean > mixed


def test_classification_is_deterministic():
    reply = "Yeah, when can you come out?"
    assert classify.classify(reply) == classify.classify(reply)


def test_case_insensitive():
    assert classify.classify("YES PLEASE")[0] == classify.classify("yes please")[0]


# ---------------------------------------------------------------------
# should_auto_book gate
# ---------------------------------------------------------------------
def test_auto_book_requires_interested_intent():
    assert not classify.should_auto_book("question", 0.99)
    assert not classify.should_auto_book("not_now", 0.99)


def test_auto_book_requires_confidence_above_floor():
    floor = classify.CONFIDENCE_FLOOR
    assert classify.should_auto_book("interested", floor)
    assert not classify.should_auto_book("interested", floor - 0.01)
