"""
Tests for the NWS client.

No network calls: every test runs against fixture GeoJSON shaped like a real
NWS response. A test suite that needs the internet is a test suite that fails
during the demo.
"""

import pytest

from pipeline import weather_client as wc


# ---------------------------------------------------------------------
# Fixtures shaped like real api.weather.gov output
# ---------------------------------------------------------------------
def _feature(**overrides):
    props = {
        "id": "urn:oid:2.49.0.1.840.0.abc123",
        "event": "Severe Thunderstorm Warning",
        "severity": "Severe",
        "certainty": "Observed",
        "urgency": "Immediate",
        "headline": "Severe Thunderstorm Warning issued for Tarrant County",
        "areaDesc": "Tarrant, TX; Dallas, TX",
        "description": "Golf ball size hail and 60 mph wind gusts reported.",
        "instruction": "Move to an interior room on the lowest floor.",
        "effective": "2026-08-07T18:05:00-05:00",
        "expires": "2026-08-07T19:00:00-05:00",
    }
    props.update(overrides.pop("properties", {}))
    feature = {
        "id": props["id"],
        "properties": props,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-97.4, 32.6], [-97.0, 32.6], [-97.0, 33.0], [-97.4, 33.0], [-97.4, 32.6],
            ]],
        },
    }
    feature.update(overrides)
    return feature


# ---------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------
def test_normalize_extracts_core_fields():
    row = wc.normalize_alert(_feature())
    assert row["event_type"] == "Severe Thunderstorm Warning"
    assert row["severity"] == "Severe"
    assert row["event_id"].startswith("urn:oid:")


def test_narrative_combines_description_and_instruction():
    """Both blocks are the unstructured text we embed. Dropping instruction
    would lose the safety guidance the Ask feature answers from."""
    row = wc.normalize_alert(_feature())
    assert "hail" in row["narrative_text"]
    assert "interior room" in row["narrative_text"]


def test_state_inferred_from_area_desc():
    row = wc.normalize_alert(_feature())
    assert row["state"] == "TX"


def test_state_is_none_when_area_desc_unparseable():
    row = wc.normalize_alert(_feature(properties={"areaDesc": "Coastal waters"}))
    assert row["state"] is None


# ---------------------------------------------------------------------
# Malformed input must be skipped, never raise
# ---------------------------------------------------------------------
def test_missing_id_is_skipped():
    bad = _feature()
    bad["id"] = None
    bad["properties"]["id"] = None
    assert wc.normalize_alert(bad) is None


def test_missing_event_type_is_skipped():
    assert wc.normalize_alert(_feature(properties={"event": None})) is None


def test_empty_properties_do_not_crash():
    assert wc.normalize_alert({"properties": {}, "geometry": None}) is None


def test_missing_description_yields_empty_narrative_not_none():
    row = wc.normalize_alert(
        _feature(properties={"description": None, "instruction": None})
    )
    assert row["narrative_text"] == ""


# ---------------------------------------------------------------------
# Geometry -> centroid + radius
# ---------------------------------------------------------------------
def test_centroid_lands_inside_the_polygon():
    lat, lon, radius = wc.polygon_centroid_radius(_feature()["geometry"])
    assert 32.6 <= lat <= 33.0
    assert -97.4 <= lon <= -97.0
    assert radius > 0


def test_null_geometry_falls_back_to_default_radius():
    lat, lon, radius = wc.polygon_centroid_radius(None)
    assert lat is None and lon is None
    assert radius == wc.DEFAULT_RADIUS_KM


def test_multipolygon_is_handled():
    geom = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[-97.4, 32.6], [-97.0, 32.6], [-97.0, 33.0], [-97.4, 32.6]]],
            [[[-96.4, 32.6], [-96.0, 32.6], [-96.0, 33.0], [-96.4, 32.6]]],
        ],
    }
    lat, lon, radius = wc.polygon_centroid_radius(geom)
    assert lat is not None and lon is not None
    assert radius > 0


def test_radius_is_capped_for_statewide_alerts():
    """An unbounded radius would sweep the whole customer list into one storm."""
    geom = {
        "type": "Polygon",
        "coordinates": [[[-125.0, 25.0], [-67.0, 25.0], [-67.0, 49.0], [-125.0, 49.0], [-125.0, 25.0]]],
    }
    _, _, radius = wc.polygon_centroid_radius(geom)
    assert radius == wc.MAX_RADIUS_KM


def test_degenerate_polygon_gets_minimum_radius():
    geom = {"type": "Polygon", "coordinates": [[[-97.0, 32.0], [-97.0, 32.0], [-97.0, 32.0]]]}
    _, _, radius = wc.polygon_centroid_radius(geom)
    assert radius >= 10.0


# ---------------------------------------------------------------------
# Fetch layer -- fake transport, no network
# ---------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status
        self.last_kwargs = None

    def get(self, url, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResponse(self._payload, self._status)


def test_fetch_filters_out_malformed_features():
    payload = {"features": [_feature(), {"properties": {}}, _feature()]}
    rows = wc.fetch_active_alerts(session=_FakeSession(payload))
    assert len(rows) == 2


def test_fetch_sends_user_agent():
    """NWS returns 403 without one. This test exists because that failure is
    silent-looking and costs an hour to diagnose."""
    session = _FakeSession({"features": []})
    wc.fetch_active_alerts(session=session)
    assert "User-Agent" in session.last_kwargs["headers"]


def test_fetch_passes_state_filter():
    session = _FakeSession({"features": []})
    wc.fetch_active_alerts(states=["TX", "OK"], session=session)
    assert session.last_kwargs["params"]["area"] == "TX,OK"


def test_event_filter_is_never_sent_to_the_api():
    """Passing event= 400s the whole request if one name is stale, and NWS
    renames events. We filter client-side instead."""
    session = _FakeSession({"features": []})
    wc.fetch_active_alerts(event_types=["Tornado Warning"], session=session)
    assert "event" not in session.last_kwargs["params"]


def test_event_filter_is_applied_client_side():
    payload = {"features": [
        _feature(),  # Severe Thunderstorm Warning
        _feature(properties={"event": "Air Quality Alert", "id": "urn:oid:other"}),
    ]}
    rows = wc.fetch_active_alerts(
        event_types=["Severe Thunderstorm Warning"], session=_FakeSession(payload)
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "Severe Thunderstorm Warning"


def test_event_filter_is_case_insensitive():
    rows = wc.fetch_active_alerts(
        event_types=["severe thunderstorm warning"], session=_FakeSession({"features": [_feature()]})
    )
    assert len(rows) == 1


def test_no_event_filter_returns_everything():
    payload = {"features": [_feature(), _feature(properties={"event": "Air Quality Alert",
                                                             "id": "urn:oid:other"})]}
    rows = wc.fetch_active_alerts(session=_FakeSession(payload))
    assert len(rows) == 2


def test_empty_response_returns_empty_list_not_none():
    rows = wc.fetch_active_alerts(session=_FakeSession({"features": []}))
    assert rows == []


def test_missing_features_key_does_not_crash():
    rows = wc.fetch_active_alerts(session=_FakeSession({}))
    assert rows == []


# ---------------------------------------------------------------------
# Error reporting -- NWS tells you what it rejected; don't throw that away
# ---------------------------------------------------------------------
def test_limit_is_never_sent():
    """/alerts/active rejects `limit` outright: 400 'Query parameter "limit"
    is not recognized'. Cost one debugging round trip."""
    session = _FakeSession({"features": []})
    wc.fetch_active_alerts(states=["TX"], session=session)
    assert "limit" not in session.last_kwargs["params"]


def test_parameter_errors_are_surfaced_in_the_message():
    """Bare '400 Bad Request' means another round trip. The problem detail
    names the offending parameter, so put it in the exception."""
    problem = {
        "title": "Bad Request",
        "parameterErrors": [
            {"parameter": "query.limit", "message": 'Query parameter "limit" is not recognized'}
        ],
    }
    with pytest.raises(RuntimeError) as err:
        wc.fetch_active_alerts(session=_FakeSession(problem, status=400))
    assert "query.limit" in str(err.value)
    assert "not recognized" in str(err.value)


def test_non_json_error_body_still_raises_cleanly():
    class _Bad:
        status_code = 500
        text = "upstream exploded"

        def json(self):
            raise ValueError("not json")

    class _S:
        last_kwargs = None

        def get(self, url, **kwargs):
            return _Bad()

    with pytest.raises(RuntimeError) as err:
        wc.fetch_active_alerts(session=_S())
    assert "500" in str(err.value)


def test_successful_response_does_not_raise():
    assert wc.fetch_active_alerts(session=_FakeSession({"features": []})) == []


# ---------------------------------------------------------------------
# State comes from UGC codes, not from prose
# ---------------------------------------------------------------------
def _with_geocode(ugc, area_desc="Swain; Haywood"):
    f = _feature(properties={"areaDesc": area_desc, "geocode": {"UGC": ugc}})
    return f


def test_state_comes_from_ugc_codes():
    """The real fix: areaDesc for zone-based alerts is bare county names with
    no state ('Swain; Haywood'). The UGC code carries it — NCZ051 is NC."""
    row = wc.normalize_alert(_with_geocode(["NCZ051", "NCZ052"]))
    assert row["state"] == "NC"


def test_zone_format_alerts_are_no_longer_stateless():
    """These are exactly the alerts with no polygon, so a null state left them
    unable to match any customer at all."""
    for ugc, expected in [(["TXZ002"], "TX"), (["AZZ502"], "AZ"), (["KSZ007"], "KS")]:
        assert wc.normalize_alert(_with_geocode(ugc))["state"] == expected


def test_multi_state_alerts_keep_every_state():
    """One Heat Advisory can span ARZ and LAZ zones. Dropping the second
    loses real customers."""
    row = wc.normalize_alert(_with_geocode(["ARZ050", "LAZ001", "LAZ002"]))
    assert row["states"] == ["AR", "LA"]
    assert row["state"] == "AR"


def test_county_format_still_works_without_geocode():
    """The old areaDesc path is kept as a fallback for alerts that arrive
    with no UGC codes."""
    f = _feature(properties={"areaDesc": "Tarrant, TX; Dallas, TX"})
    f["properties"].pop("geocode", None)
    assert wc.normalize_alert(f)["state"] == "TX"


def test_no_geocode_and_unparseable_area_desc_yields_none():
    f = _feature(properties={"areaDesc": "Coastal waters"})
    f["properties"].pop("geocode", None)
    row = wc.normalize_alert(f)
    assert row["state"] is None
    assert row["states"] == []


def test_malformed_ugc_entries_are_skipped():
    """Nulls, empties, non-strings and too-short codes are all discarded.
    A real UGC code is at least three characters: state + Z/C + number."""
    row = wc.normalize_alert(_with_geocode(["NCZ051", None, "", 42, "XX", "TXZ002"]))
    assert row["states"] == ["NC", "TX"]


def test_duplicate_zones_in_one_state_yield_one_entry():
    row = wc.normalize_alert(_with_geocode(["TXZ002", "TXZ007", "TXZ013"]))
    assert row["states"] == ["TX"]
