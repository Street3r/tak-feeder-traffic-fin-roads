"""
Fintraffic Digitraffic GeoJSON fetch and parsing helpers.

Simple JSON / GeoJSON format from:
  https://tie.digitraffic.fi/api/traffic-message/v2/traffic-announcements

Response is a GeoJSON FeatureCollection:
  {
    "type": "FeatureCollection",
    "dataUpdatedTime": "2026-08-27T07:16:21.020Z",
    "features": [
      {
        "type": "Feature",
        "geometry": { "type": "Point"|"LineString"|"MultiLineString", "coordinates": [...] },
        "properties": {
          "situationId": "GUID...",
          "situationType": "...",
          "trafficAnnouncementType": "general"|"accident report"|...,
          "announcements": [{ "title", "location", "features": [{name}], "timeAndDuration", ... }],
          "releaseTime": "...",
          "versionTime": "..."
        }
      }
    ]
  }
"""

# Copyright Stefan Gofferje; Copyright (C) 2026 Street3r (datasets, English labels)

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from . import __version__
from .common import digitraffic_headers, fetch, remarks_text

BASE_URL = "https://tie.digitraffic.fi/api/traffic-message/v2"
DEFAULT_URL = f"{BASE_URL}/traffic-announcements"
SOURCE_LINE = "Source: Fintraffic / digitraffic.fi, CC BY 4.0"

# Datasets of the Simple JSON API: path -> (fallback label, hashtag)
DATASETS: Dict[str, Tuple[str, str]] = {
    "traffic-announcements": ("Announcement", "announcement"),
    "roadworks": ("Roadwork", "roadwork"),
    "weight-restrictions": ("Weight limit", "weightlimit"),
    "exempted-transports": ("Exempted transport", "exemptedtransport"),
}

# Short English callsigns for common Digitraffic feature names
SHORT_LABELS = {
    "Tie on suljettu liikenteeltä": "Road closed",
    "Tapahtumapaikalla on käytössä kiertotie": "Detour",
    "Lautan kantavuus on muuttunut": "Ferry",
    "Liikennejärjestelyt ovat muuttuneet": "Arrangements",
    "Raskaan ajoneuvon nostotyö": "Heavy lift",
    "Onnettomuus": "Accident",
    "Tapahtumapaikalla liikenteenohjaus": "Traffic control",
    "Liikenne saattaa ruuhkautua": "Congestion",
    "Paikalla on kiertotieopastus": "Detour signs",
    "Ajokaista suljettu liikenteeltä": "Lane closed",
    "Liikenne on palautumassa normaaliksi": "Clearing",
    "Painorajoitus": "Weight limit",
    "Tietyö": "Roadwork",
}


async def fetch_geojson(url: str = DEFAULT_URL,
                        user: str = "tak-feeder-traffic-fin-roads") -> Dict[str, Any]:
    """Fetch a Digitraffic GeoJSON FeatureCollection (gzip, Digitraffic-User)."""
    body = await fetch(url, digitraffic_headers(user, "tak-feeder-traffic-fin-roads",
                                                __version__), timeout=30)
    data: Dict[str, Any] = json.loads(body)
    return data


def _feature_text(feature: Dict[str, Any]) -> str:
    """Feature name with its quantity, e.g. "Painorajoitus 12 t"."""
    name = str(feature.get("name") or feature.get("description") or "").strip()
    quantity, unit = feature.get("quantity"), feature.get("unit")
    if name and quantity not in (None, ""):
        name += f" {quantity:g}" if isinstance(quantity, (int, float)) else f" {quantity}"
        if unit:
            name += f" {unit}"
    return name


def local_time(value: Optional[str], tz: ZoneInfo) -> Optional[str]:
    """Digitraffic ISO-8601 time (UTC) as local "dd.mm. HH:MM"; unparsable as is."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%d.%m. %H:%M")


def _roadwork_names(ann: Dict[str, Any]) -> List[str]:
    """Work types and restrictions of road work phases (names vary by phase)."""
    names: List[str] = []
    for phase in ann.get("roadWorkPhases", []) or []:
        for work in phase.get("workTypes", []) or []:
            text = str(work.get("description") or work.get("type") or "").strip()
            if text and text not in names:
                names.append(text)
        for restr in phase.get("restrictions", []) or []:
            inner = restr.get("restriction") if isinstance(restr.get("restriction"), dict) else restr
            text = _feature_text(inner or {})
            if text and text not in names:
                names.append(text)
    return names


def extract_cot_candidates(
    feature_collection: Dict[str, Any], dataset: str = "traffic-announcements",
    tz: str = "Europe/Helsinki",
) -> List[Dict[str, Any]]:
    """
    Normalized candidates for CoT conversion from a FeatureCollection.
    Each dict holds: uid, geometry, centroid, callsign, remarks, times etc.
    """
    fallback_label, dataset_tag = DATASETS.get(dataset, ("Announcement", "traffic"))
    zone = ZoneInfo(tz)
    candidates: List[Dict[str, Any]] = []
    for feat in feature_collection.get("features", []) or []:
        props: Dict[str, Any] = feat.get("properties", {}) or {}
        geometry: Dict[str, Any] = feat.get("geometry", {}) or {}
        situation_id: str = props.get("situationId", "unknown")
        situation_type: str = props.get("situationType", "TRAFFIC_ANNOUNCEMENT")
        # Used for colours and icons; road works etc. have no announcement type
        ann_type: str = props.get("trafficAnnouncementType") or (
            situation_type.lower().replace("_", "") if situation_type else "general")
        announcements: List[Dict[str, Any]] = props.get("announcements", []) or []

        title = location_desc = features_str = ""
        feat_names: List[str] = []
        start_time = end_time = comment = sender = road_name = None
        if announcements:
            ann = announcements[0]
            title = ann.get("title", "") or ""
            loc = ann.get("location", {}) or {}
            location_desc = loc.get("description", "") or ""
            feat_names = [_feature_text(f) for f in ann.get("features", []) or []]
            feat_names = [n for n in feat_names if n] + _roadwork_names(ann)
            features_str = ", ".join(feat_names)
            tad = ann.get("timeAndDuration", {}) or {}
            start_time = tad.get("startTime")
            end_time = tad.get("endTime")
            comment = ann.get("comment")
            sender = ann.get("sender")
            ldetails = ann.get("locationDetails", {}) or {}
            ral = ldetails.get("roadAddressLocation", {}) or {}
            primary = ral.get("primaryPoint", {}) or {}
            road_name = primary.get("roadName")

        centroid = _centroid_of_coords(geometry)

        # Callsign: short incident type
        if feat_names and dataset == "traffic-announcements":
            first = feat_names[0]
            callsign = SHORT_LABELS.get(first, first)
            if len(callsign) > 20:
                callsign = callsign[:17] + "..."
        else:
            callsign = fallback_label

        lines: List[Optional[str]] = [
            title.strip() or None,
            location_desc.strip() or None,
            f"Road: {road_name}" if road_name else None,
            f"Details: {features_str}" if features_str else None,
            comment.strip() if comment else None,
            f"Valid: {local_time(start_time, zone) or '?'} – "
            f"{local_time(end_time, zone) or 'until further notice'}"
            if start_time or end_time else None,
            f"Sender: {sender}" if sender else None,
        ]
        tags = ["traffic", dataset_tag]
        if "onnettomuus" in features_str.lower() or "accident" in ann_type.lower():
            tags.append("accident")
        remarks = remarks_text(lines, SOURCE_LINE, tags) if any(lines) else callsign

        candidates.append({
            "uid": f"digitraffic.road.{situation_id}",  # as in upstream
            "situationId": situation_id,
            "situationType": situation_type,
            "announcementType": ann_type,
            "dataset": dataset,
            "geometry": geometry,
            "properties": props,
            "centroid": centroid,
            "callsign": callsign,
            "remarks": remarks,
            "locationDesc": location_desc,
            "startTime": start_time,
            "endTime": end_time,
            "sender": sender,
            "title": title,
            "featuresStr": features_str,
            "roadName": road_name,
            "releaseTime": props.get("releaseTime"),
            "versionTime": props.get("versionTime"),
        })
    return candidates


def _centroid_of_coords(
    geometry: Dict[str, Any],
) -> Optional[Tuple[float, float]]:
    """Compute a display centroid for ATAK icon placement.

    Uses simple average for Point/LineString; for MultiLineString uses
    the middle coordinate of the longest line.
    Returns (lat, lon) or None.
    """
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None

    if gtype == "Point":
        lon, lat = coords  # type: ignore[misc]
        return float(lat), float(lon)

    if gtype == "LineString":
        # coords: [[lon,lat], ...]
        lons = [c[0] for c in coords]  # type: ignore[index]
        lats = [c[1] for c in coords]  # type: ignore[index]
        if not lats:
            return None
        # Use midpoint for stability rather than centroid of line
        mid = len(coords) // 2
        return float(coords[mid][1]), float(coords[mid][0])  # type: ignore[index]

    if gtype == "MultiLineString":
        # coords: [[[lon,lat],...], [[lon,lat],...]]
        # Pick longest line, then midpoint
        longest = max(coords, key=len)  # type: ignore[arg-type]
        if not longest:
            return None
        mid = len(longest) // 2
        return float(longest[mid][1]), float(longest[mid][0])  # type: ignore[index]

    return None


