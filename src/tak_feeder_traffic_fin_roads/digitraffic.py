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

from typing import Any, Dict, List, Tuple, Optional
import aiohttp

DEFAULT_URL = "https://tie.digitraffic.fi/api/traffic-message/v2/traffic-announcements"


async def fetch_geojson(
    session: aiohttp.ClientSession, url: str = DEFAULT_URL
) -> Dict[str, Any]:
    """Fetch Digitraffic GeoJSON FeatureCollection."""
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"User-Agent": "tak-feeder-traffic-fin-roads/0.1.0"}

    async with session.get(url, headers=headers, timeout=timeout) as resp:
        resp.raise_for_status()
        data: Dict[str, Any] = await resp.json(content_type=None)
        return data


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


def extract_cot_candidates(
    feature_collection: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Extract normalized candidates for CoT conversion from FeatureCollection.
    Each dict contains: uid, geometry, properties, centroid, callsign, remarks, stale hints.
    """
    candidates: List[Dict[str, Any]] = []
    features = feature_collection.get("features", [])

    for feat in features:
        props: Dict[str, Any] = feat.get("properties", {}) or {}
        geometry: Dict[str, Any] = feat.get("geometry", {}) or {}

        situation_id: str = props.get("situationId", "unknown")
        situation_type: str = props.get("situationType", "traffic announcement")
        ann_type: str = props.get("trafficAnnouncementType", "general")

        announcements: List[Dict[str, Any]] = props.get("announcements", []) or []
        # Use first announcement for human-readable fields (all announcements are fi/fi mostly)
        title = ""
        location_desc = ""
        features_str = ""
        start_time: Optional[str] = None
        end_time: Optional[str] = None
        comment: Optional[str] = None
        sender: Optional[str] = None
        road_name: Optional[str] = None

        if announcements:
            ann = announcements[0]
            title = ann.get("title", "") or ""
            loc = ann.get("location", {}) or {}
            location_desc = loc.get("description", "") or ""
            # Human-readable feature names like "Onnettomuus", "Ajokaista suljettu"
            feat_names = [f.get("name", "") for f in ann.get("features", []) or []]
            features_str = ", ".join([n for n in feat_names if n])
            tad = ann.get("timeAndDuration", {}) or {}
            start_time = tad.get("startTime")
            end_time = tad.get("endTime")
            comment = ann.get("comment")
            sender = ann.get("sender")
            # Try to extract roadName from locationDetails
            ldetails = ann.get("locationDetails", {}) or {}
            ral = ldetails.get("roadAddressLocation", {}) or {}
            primary = ral.get("primaryPoint", {}) or {}
            road_name = primary.get("roadName")

        centroid = _centroid_of_coords(geometry)

        # Build callsign: short form for ATAK - title first line or situationId
        callsign = title.strip() if title else f"{situation_type} {situation_id[:8]}"
        # Truncate callsign to ~40 chars for ATAK label
        if len(callsign) > 40:
            callsign = callsign[:37] + "..."

        # Remarks: richer detail
        remarks_lines = []
        if title:
            remarks_lines.append(title.strip())
        if features_str:
            remarks_lines.append(f"Features: {features_str}")
        if location_desc:
            remarks_lines.append(location_desc.strip())
        if comment:
            remarks_lines.append(comment.strip())
        if road_name:
            remarks_lines.append(f"Road: {road_name}")
        if ann_type:
            remarks_lines.append(f"Type: {ann_type}")
        remarks = "\n".join(remarks_lines) if remarks_lines else callsign

        # UID: stable per situationId + geometry hash fallback
        # Digitraffic situationId is stable across updates; include geometry type to allow multiples
        gtype = geometry.get("type", "unknown")
        uid = f"digitraffic.{situation_id}.{gtype.lower()}"

        candidates.append(
            {
                "uid": uid,
                "situationId": situation_id,
                "situationType": situation_type,
                "announcementType": ann_type,
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
            }
        )

    return candidates
