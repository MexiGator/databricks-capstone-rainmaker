"""
Tests for the reply simulator.

The whole point of seeding is that the demo is repeatable. If these pass, the
third take behaves exactly like the first.
"""

from collections import Counter

from agent import simulate


def test_reply_is_deterministic():
    a = simulate.reply_for("opp_abc123")
    b = simulate.reply_for("opp_abc123")
    assert a == b


def test_different_opportunities_get_different_replies():
    replies = {simulate.reply_for(f"opp_{i:04d}")[1] for i in range(40)}
    assert len(replies) > 5, "seeding collapsed to too few distinct replies"


def test_intent_matches_the_reply_text_bucket():
    for i in range(60):
        intent, text = simulate.reply_for(f"opp_{i:04d}")
        assert text in simulate.REPLY_POOL[intent]


def test_mix_is_roughly_sixty_twenty_five_fifteen():
    """A demo where nobody says yes is not a demo. A demo where everyone says
    yes is not credible. Both failures are caught here."""
    counts = Counter(simulate.reply_for(f"opp_{i:05d}")[0] for i in range(1000))
    assert 0.52 <= counts["interested"] / 1000 <= 0.68
    assert 0.18 <= counts["question"] / 1000 <= 0.32
    assert 0.09 <= counts["not_now"] / 1000 <= 0.21


def test_every_intent_appears():
    counts = Counter(simulate.reply_for(f"opp_{i:04d}")[0] for i in range(200))
    assert set(counts) == {"interested", "question", "not_now"}


def test_mix_weights_sum_to_one_hundred():
    assert sum(w for _, w in simulate.INTENT_MIX) == 100


def test_pools_are_non_empty():
    for intent, _ in simulate.INTENT_MIX:
        assert simulate.REPLY_POOL[intent], f"{intent} pool is empty"


def test_simulated_replies_classify_to_their_seeded_intent():
    """End-to-end guard: the text the simulator picks must actually classify
    as the intent it was seeded for, or the demo's booked count is a lie."""
    from agent import classify

    mismatches = []
    for i in range(300):
        opp = f"opp_{i:05d}"
        seeded, text = simulate.reply_for(opp)
        actual, _ = classify.classify(text)
        if actual != seeded:
            mismatches.append((text, seeded, actual))
    assert not mismatches, f"simulator/classifier disagree: {mismatches[:5]}"
