"""
Tests for the null-distance path.

Zone-based NWS alerts carry no polygon, so distance_km is genuinely null for
them. That path did not exist until state gating let zone alerts through, and
when it opened, three bugs were waiting in it:

  1. Spark's null became NaN crossing into pandas, and Postgres accepts NaN
     as a valid double.
  2. The JSON serializer emitted a bare `NaN`, which is not valid JSON, so
     the browser's JSON.parse threw and the queue never rendered.
  3. The draft prompt formatted distance with `:.0f`, which raises on None.

Each layer is now defended independently, because any one of them can be
reached without the others.
"""

from __future__ import annotations

import math

import pytest


# ---------------------------------------------------------------------
# Layer 1 · pandas conversion
# ---------------------------------------------------------------------
def test_pandas_nan_becomes_none_before_the_database_write():
    """pandas float64 has no separate missing value, so a Spark null arrives
    as NaN. Writing that to Postgres stores a real NaN, not a null."""
    pd = pytest.importorskip("pandas")
    import numpy as np

    pdf = pd.DataFrame({"distance_km": [12.5, np.nan], "priority": ["high", "low"]})
    cleaned = pdf.astype(object).where(pdf.notna(), None)
    rows = [tuple(r) for r in cleaned.itertuples(index=False, name=None)]

    assert rows[0][0] == 12.5
    assert rows[1][0] is None
    assert not any(isinstance(v, float) and math.isnan(v) for row in rows for v in row)


def test_conversion_preserves_non_null_values():
    pd = pytest.importorskip("pandas")
    import numpy as np

    pdf = pd.DataFrame({"a": [1.0, 2.5, np.nan], "b": ["x", "y", "z"]})
    cleaned = pdf.astype(object).where(pdf.notna(), None)
    assert [r[0] for r in cleaned.itertuples(index=False, name=None)] == [1.0, 2.5, None]
    assert [r[1] for r in cleaned.itertuples(index=False, name=None)] == ["x", "y", "z"]


# ---------------------------------------------------------------------
# Layer 2 · JSON serialisation
# ---------------------------------------------------------------------
def _json_safe(value):
    """Mirror of the helper in app.py, so this can be tested without Flask."""
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def test_nan_is_replaced_with_null():
    assert _json_safe(float("nan")) is None


def test_infinity_is_replaced_with_null():
    assert _json_safe(float("inf")) is None
    assert _json_safe(float("-inf")) is None


def test_nested_structures_are_cleaned():
    payload = [{"distance_km": float("nan"), "score": 0.42, "name": "Desmond"}]
    assert _json_safe(payload) == [{"distance_km": None, "score": 0.42, "name": "Desmond"}]


def test_ordinary_values_pass_through_untouched():
    payload = {"a": 3.14, "b": "text", "c": None, "d": 0, "e": [1, 2, 3]}
    assert _json_safe(payload) == payload


def test_cleaned_payload_is_parseable_json():
    """The actual failure: json.dumps writes bare NaN, which is valid Python
    and invalid JSON. JSON.parse throws and the fetch never resolves."""
    import json

    payload = [{"distance_km": float("nan")}]
    with pytest.raises(ValueError):
        json.dumps(payload, allow_nan=False)

    text = json.dumps(_json_safe(payload), allow_nan=False)
    assert json.loads(text) == [{"distance_km": None}]


# ---------------------------------------------------------------------
# Layer 3 · the draft prompt
# ---------------------------------------------------------------------
def _distance_phrase(distance_km):
    """Mirror of the helper in tools.py, testable without a database."""
    if distance_km is None:
        return "not applicable — this is a zone-wide alert with no storm centre"
    return f"{distance_km:.0f} km"


def test_null_distance_does_not_crash_the_prompt():
    """`{d:.0f}` raises TypeError on None. That crashed the draft on exactly
    the alerts state gating exists to serve."""
    assert "zone-wide" in _distance_phrase(None)


def test_real_distance_is_formatted_as_kilometres():
    assert _distance_phrase(52.3) == "52 km"
    assert _distance_phrase(0.0) == "0 km"


def test_the_old_format_string_would_have_raised():
    """Documents the original bug so the regression is unmistakable."""
    with pytest.raises(TypeError):
        "{:.0f} km".format(None)
