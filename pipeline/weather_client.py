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
    state = _infer_state(area_desc)

    return {
        "event_id": event_id,
        "event_type": event_type,
        "severity": props.get("severity"),
        "certainty": props.get("certainty"),
        "urgency": props.get("urgency"),
        "headline": props.get("headline"),
        "area_desc": area_desc,
        "state": state,
        "lat": lat,
        "lon": lon,
        "radius_km": radius,
        "effective_at": props.get("effective") or props.get("onset"),
        "expires_at": props.get("expires") or props.get("ends"),
        "narrative_text": narrative,
        "payload": props,
    }


def _infer_state(area_desc: str) -> str | None:
    """
    NWS areaDesc looks like 'Tarrant, TX; Dallas, TX'. Take the first
    2-letter token after a comma. Cheap, and only used as a coarse fallback
    when an alert has no polygon.
    """
    for chunk in area_desc.split(";"):
        parts = [p.strip() for p in chunk.split(",")]
        if len(parts) >= 2 and len(parts[-1]) == 2 and parts[-1].isalpha():
            return parts[-1].upper()
    return None


# ---------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------
def _raise_for_nws_error(resp: Any) -> None:
    """
    On a 4xx/5xx, raise RuntimeError carrying NWS's problem detail.

    NWS answers a bad request with RFC-7807 problem+json whose `parameterErrors`
    array names exactly which query parameter it rejected and why (e.g.
    'Query parameter "limit" is not recognized'). requests' raise_for_status()
    throws that body away and reports only the status code -- turning a one-line
    fix into an hour of guessing. Parse it out instead.

    Falls back to `detail`, then `title`, then the raw response text when the
    body is not the expected shape (or is not JSON at all).
    """
    if resp.status_code < 400:
        return

    detail: str | None = None
    try:
        body = resp.json()
    except Exception:
        body = None

    if isinstance(body, dict):
        param_errors = body.get("parameterErrors")
        if isinstance(param_errors, list) and param_errors:
            parts = []
            for pe in param_errors:
                if not isinstance(pe, dict):
                    continue
                param = pe.get("parameter")
                message = pe.get("message")
                parts.append(f"{param}: {message}" if param else str(message))
            detail = "; ".join(p for p in parts if p) or None
        if not detail:
            detail = body.get("detail") or body.get("title")

    if not detail:
        detail = (getattr(resp, "text", "") or "").strip() or None

    raise RuntimeError(
        f"NWS request failed (HTTP {resp.status_code}): {detail or 'no detail provided'}"
    )


def fetch_active_alerts(
    states: list[str] | None = None,
    event_types: list[str] | None = None,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch active alerts, optionally filtered to the states we cover and the
    event types that actually create service demand.

    We filter by state server-side but by event type CLIENT-SIDE. The NWS
    `event` query param validates against a fixed enum and returns HTTP 400
    when any value is stale or unrecognised -- one bad name fails the whole
    poll. Fetching by state and matching event_type in Python is resilient to
    that: an event name that no longer exists simply matches nothing.

    No `limit` param: /alerts/active does not accept one (it 400s with
    'Query parameter "limit" is not recognized'). Active alerts are naturally
    bounded -- the whole US is a few hundred at a time.
    """
    params: dict[str, Any] = {"status": "actual", "message_type": "alert"}
    if states:
        params["area"] = ",".join(states)

    http = session or requests
    resp = http.get(NWS_ALERTS_URL, params=params, headers=_headers(), timeout=TIMEOUT)
    _raise_for_nws_error(resp)

    features = (resp.json() or {}).get("features") or []
    rows = [normalize_alert(f) for f in features]
    rows = [r for r in rows if r is not None]

    if event_types:
        wanted = {e.strip().lower() for e in event_types}
        rows = [r for r in rows if (r["event_type"] or "").strip().lower() in wanted]

    return rows
