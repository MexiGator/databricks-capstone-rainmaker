"""
Rainmaker -- National Weather Service client.

Extends the Homework 2 NWS client. Two differences:
  * we pull ACTIVE ALERTS ONLY (forecasts do not signal damage), and
  * we extract a centroid + radius from the alert polygon so Match & Score
    can compute real distance to each customer.

NWS is free and needs no API key, but it WILL return 403 without a
descriptive User-Agent. That is not optional.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

import requests

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
USER_AGENT = "Rainmaker/1.0 (Analytic Gator; capstone project; contact@example.com)"
TIMEOUT = 30

# Fallback radius when an alert carries no polygon (zone-only alerts).
DEFAULT_RADIUS_KM = 60.0
# Cap so a statewide alert does not sweep in the entire country.
MAX_RADIUS_KM = 250.0


def _headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}


# ---------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------
def _iter_coords(geometry: dict[str, Any]) -> Iterable[tuple[float, float]]:
    """Yield (lon, lat) pairs from a Polygon or MultiPolygon."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        rings = coords
    elif gtype == "MultiPolygon":
        rings = [ring for poly in coords for ring in poly]
    else:
        return
    for ring in rings:
        for point in ring:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                yield float(point[0]), float(point[1])


def polygon_centroid_radius(
    geometry: dict[str, Any] | None,
) -> tuple[float | None, float | None, float]:
    """
    Reduce an alert polygon to (lat, lon, radius_km).

    A bounding circle is a deliberate simplification: point-in-polygon on
    every customer would be more precise, but the alert footprint is already
    an approximation of where damage occurred, and the exposure score treats
    distance as a gradient rather than a hard boundary. Documented so the
    tradeoff is visible rather than accidental.
    """
    if not geometry:
        return None, None, DEFAULT_RADIUS_KM

    points = list(_iter_coords(geometry))
    if not points:
        return None, None, DEFAULT_RADIUS_KM

    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    c_lon = sum(lons) / len(lons)
    c_lat = sum(lats) / len(lats)

    # Radius = furthest vertex from the centroid.
    radius = max(haversine_km(c_lat, c_lon, lat, lon) for lon, lat in points)
    radius = min(max(radius, 10.0), MAX_RADIUS_KM)
    return round(c_lat, 5), round(c_lon, 5), round(radius, 2)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km. Duplicated in scoring.py on purpose --
    this module must stay importable without Spark."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------
def normalize_alert(feature: dict[str, Any]) -> dict[str, Any] | None:
    """
    Turn one NWS GeoJSON feature into a weather_events row.

    Returns None for features missing an id or event type -- a malformed
    record should be skipped, not crash the poll.
    """
    props = feature.get("properties") or {}
    event_id = props.get("id") or feature.get("id")
    event_type = props.get("event")
    if not event_id or not event_type:
        return None

    lat, lon, radius = polygon_centroid_radius(feature.get("geometry"))

    # description + instruction are the genuinely unstructured text we embed.
    description = (props.get("description") or "").strip()
    instruction = (props.get("instruction") or "").strip()
    narrative = "\n\n".join(part for part in (description, instruction) if part)

    area_desc = props.get("areaDesc") or ""
    states = _states_from_geocode(props)
    state = states[0] if states else _infer_state(area_desc)

    return {
        "event_id": event_id,
        "event_type": event_type,
        "severity": props.get("severity"),
        "certainty": props.get("certainty"),
        "urgency": props.get("urgency"),
        "headline": props.get("headline"),
        "area_desc": area_desc,
        "state": state,
        # Every state the alert touches. `state` is the first for backwards
        # compatibility; zone gating should use this.
        "states": states or ([state] if state else []),
        "lat": lat,
        "lon": lon,
        "radius_km": radius,
        "effective_at": props.get("effective") or props.get("onset"),
        "expires_at": props.get("expires") or props.get("ends"),
        "narrative_text": narrative,
        "payload": props,
    }


def _states_from_geocode(props: dict[str, Any]) -> list[str]:
    """
    Pull states from the UGC codes, which is where NWS actually puts them.

    A UGC code is STATE + Z/C + zone number: NCZ051 is North Carolina zone 51,
    TXZ002 is Texas zone 2. Every alert carries them.

    This replaced parsing `areaDesc`, which works for county-format alerts
    ("Tarrant, TX") and fails silently for the zone-format ones that heat and
    Special Weather Statement alerts use ("Swain; Haywood", "Jewell"). Those
    are exactly the alerts with no polygon, so a null state left them unable
    to match any customer at all.

    Returns every state the alert touches. One Heat Advisory can span ARZ and
    LAZ zones, and dropping the second one loses real customers.
    """
    geocode = (props.get("geocode") or {})
    ugc = geocode.get("UGC") or []
    states = []
    for code in ugc:
        if isinstance(code, str) and len(code) >= 3 and code[:2].isalpha():
            st = code[:2].upper()
            if st not in states:
                states.append(st)
    return states


def _infer_state(area_desc: str) -> str | None:
    """
    Fallback for the county format, 'Tarrant, TX; Dallas, TX'.

    Only used when the geocode block is missing. Kept because it costs
    nothing and covers alerts that arrive without UGC codes.
    """
    for chunk in (area_desc or "").split(";"):
        parts = [p.strip() for p in chunk.split(",")]
        if len(parts) >= 2 and len(parts[-1]) == 2 and parts[-1].isalpha():
            return parts[-1].upper()
    return None


# ---------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------
def _raise_with_detail(resp) -> None:
    """
    Turn an NWS error into something that names the actual problem.

    weather.gov returns RFC-7807 problem details listing exactly which query
    parameter it rejected. resp.raise_for_status() throws that away and leaves
    you with "400 Bad Request" and a URL to squint at -- which cost a full
    debugging round trip on `limit`. Never again.
    """
    status = getattr(resp, "status_code", 200)
    if status < 400:
        return
    detail = ""
    try:
        body = resp.json() or {}
        errors = body.get("parameterErrors") or []
        if errors:
            detail = "; ".join(
                f"{e.get('parameter')}: {e.get('message')}" for e in errors
            )
        else:
            detail = body.get("detail") or body.get("title") or ""
    except Exception:  # noqa: BLE001 - a non-JSON body is not worth failing over
        detail = (getattr(resp, "text", "") or "")[:200]
    raise RuntimeError(f"NWS returned HTTP {status}. {detail}".strip())


def fetch_active_alerts(
    states: list[str] | None = None,
    event_types: list[str] | None = None,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch active alerts for the given states, then filter to the event types
    that create service demand.

    The event filter is applied CLIENT-SIDE, deliberately.

    Passing `event=` to the API returns HTTP 400 if any single value is not in
    the NWS enum, and that enum drifts -- "Extreme Heat Warning" replaced
    "Excessive Heat Warning" in 2025, and "Extreme Cold Warning" replaced "Wind
    Chill Warning". One stale name in the list kills the whole request, and the
    400 says nothing about which name was the problem.

    Filtering here costs a little more data over the wire and cannot break.
    Same reasoning as best_template using retrieval instead of an equality
    match: the upstream vocabulary is not ours to control.
    """
    # NOTE: no `limit` parameter. /alerts/active does not accept one and
    # returns 400 "Query parameter \"limit\" is not recognized" if you send it.
    # Active alerts are naturally bounded -- the whole US runs to a few hundred.
    params: dict[str, Any] = {"status": "actual", "message_type": "alert"}
    if states:
        params["area"] = ",".join(states)

    http = session or requests
    resp = http.get(NWS_ALERTS_URL, params=params, headers=_headers(), timeout=TIMEOUT)
    _raise_with_detail(resp)

    features = (resp.json() or {}).get("features") or []
    rows = [normalize_alert(f) for f in features]
    rows = [r for r in rows if r is not None]

    if event_types:
        wanted = {e.strip().lower() for e in event_types}
        rows = [r for r in rows if (r["event_type"] or "").strip().lower() in wanted]

    return rows
