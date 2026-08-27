"""
CoT utilities for Fintraffic road traffic messages.

Converts normalized Digitraffic candidates (from digitraffic.py) to CoT XML
for Mission API delivery. Implements:

- base COT type configurable via env COT_TYPE (default a-n-G)
- ATAK shape polyline when LineString/MultiLineString, pure point icon when Point
- stale = endTime when present, else start + 24h or far future for mission sync
- usericon placeholder via ICONSET_PATH (to be looked up)
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import pytak


def _parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    """Parse Digitraffic ISO8601 string to datetime (UTC)."""
    if not value:
        return None
    try:
        # Normalize Z -> +00:00 for fromisoformat, handle millis
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _cot_time_from_dt(dt: datetime) -> str:
    """Format datetime as COT time string YYYY-MM-DDTHH:MM:SS.000Z."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _color_for_announcement(announcement_type: str, features_str: str) -> str:
    """
    Map Digitraffic announcement type + features to ARGB color for ATAK.
    yellow 1090518784, orange 1090483968, red 1090453504, -1 white/default
    """
    low = (announcement_type + " " + features_str).lower()
    if "accident" in low or "onnettomuus" in low:
        return "1090453504"  # red
    if "suljettu" in low or "closed" in low:
        return "1090453504"  # red
    if "roadwork" in low or "tietyö" in low:
        return "1090483968"  # orange
    if "general" in low:
        return "1090518784"  # yellow
    return "-1"


def generate_traffic_cot(
    candidate: Dict[str, Any],
    cot_type: str = "a-n-G",
    cot_how: str = "h-e",
    mission_name: Optional[str] = None,
    iconset_path: Optional[str] = None,
) -> Optional[bytes]:
    """
    Generate CoT XML for a single Digitraffic candidate.

    Mission API usage: include <marti><dest mission="..."/> if mission_name given.
    Geometry: Point -> pure point icon; LineString/MultiLineString -> ATAK shape polyline.
    Stale: endTime when present else startTime+24h else now+24h.
    Icon: usericon placeholder when ICONSET_PATH provided (e.g. 5af2af2c-...).
    """
    uid: str = candidate.get("uid", "unknown")
    callsign: str = candidate.get("callsign", uid)
    remarks: str = candidate.get("remarks", "")
    geometry: Dict[str, Any] = candidate.get("geometry", {}) or {}
    centroid = candidate.get("centroid")

    if not centroid:
        return None

    lat, lon = centroid
    gtype = geometry.get("type", "Point")
    coords = geometry.get("coordinates", [])

    # Resolve stale from endTime per spec (user decision 1)
    end_dt = _parse_iso8601(candidate.get("endTime"))
    start_dt = _parse_iso8601(candidate.get("startTime"))
    now = datetime.now(timezone.utc)
    if end_dt:
        stale_dt = end_dt
    elif start_dt:
        stale_dt = start_dt + timedelta(hours=24)
    else:
        # No time info: far future so mission sync controls lifecycle
        stale_dt = now + timedelta(hours=24)

    # Use start as event start when available else now
    start_for_cot = start_dt if start_dt else now

    root = ET.Element("event")
    root.set("version", "2.0")
    root.set("type", cot_type)
    root.set("uid", uid)
    root.set("how", cot_how)
    root.set("time", pytak.cot_time())
    root.set("start", _cot_time_from_dt(start_for_cot))
    root.set("stale", _cot_time_from_dt(stale_dt))

    pt_attr = {
        "lat": str(lat),
        "lon": str(lon),
        "hae": "0",
        "ce": "10",
        "le": "10",
    }
    ET.SubElement(root, "point", attrib=pt_attr)

    detail = ET.Element("detail")

    # Mission routing for DataSync (user decision 6)
    if mission_name:
        marti = ET.Element("marti")
        dest = ET.Element("dest")
        dest.set("mission", mission_name)
        marti.append(dest)
        detail.append(marti)

    contact = ET.Element("contact")
    contact.set("callsign", callsign)
    detail.append(contact)

    remarks_el = ET.Element("remarks")
    remarks_el.text = remarks
    detail.append(remarks_el)

    # Color
    argb = _color_for_announcement(
        candidate.get("announcementType", ""),
        candidate.get("featuresStr", ""),
    )
    color = ET.Element("color")
    color.set("argb", argb)
    detail.append(color)

    # Usericon placeholder (user decision 2) - TODO look up actual iconset path
    if iconset_path:
        usericon = ET.Element("usericon")
        usericon.set("iconsetpath", iconset_path)
        detail.append(usericon)

    is_polyline = gtype in ("LineString", "MultiLineString")

    if is_polyline:
        # Build flat coords for shape polyline (user decision 3)
        flat_coords: List[List[float]] = []
        if gtype == "LineString":
            flat_coords = coords  # type: ignore[assignment]
        elif gtype == "MultiLineString":
            # Keep each line separate for shape? ATAK wants continuous polyline
            # For MultiLineString we concatenate with gap handling
            for line in coords:  # type: ignore[union-attr]
                for pt in line:  # type: ignore[union-attr]
                    flat_coords.append(pt)  # type: ignore[arg-type]

        # Decimate if huge to avoid oversize CoT / mission payload
        max_points = 200
        if len(flat_coords) > max_points:
            step = len(flat_coords) / max_points
            flat_coords = [flat_coords[int(i * step)] for i in range(max_points)]

        # Add ATAK shape polyline
        shape = ET.Element("shape")
        polyline = ET.Element("polyline")
        for lon_p, lat_p in flat_coords:
            vertex = ET.Element("vertex")
            vertex.set("lat", str(lat_p))
            vertex.set("lon", str(lon_p))
            vertex.set("hae", "0")
            polyline.append(vertex)
        shape.append(polyline)
        detail.append(shape)

        # Also add link points for backward compat (weather pattern)
        for lon_p, lat_p in flat_coords:
            link = ET.Element("link")
            link.set("point", f"{lat_p},{lon_p}")
            detail.append(link)

        # Stroke styling for line
        stroke_color = ET.Element("strokeColor")
        stroke_color.set("value", argb)
        detail.append(stroke_color)

        stroke_weight = ET.Element("strokeWeight")
        stroke_weight.set("value", "3")
        detail.append(stroke_weight)

        stroke_style = ET.Element("strokeStyle")
        stroke_style.set("value", "solid")
        detail.append(stroke_style)
    else:
        # Pure point icon - no shape, no link points
        pass

    root.append(detail)

    return ET.tostring(root)


def keep_alive(
    uid: str, mission_name: Optional[str] = None, version: str = "0.1.0"
) -> bytes:
    """Keepalive CoT for the feeder itself (similar to weather feeder)."""
    root = ET.Element("event")
    root.set("version", "2.0")
    root.set("type", "a-f-G-U")
    root.set("uid", uid)
    root.set("how", "h-e")
    root.set("time", pytak.cot_time())
    root.set("start", pytak.cot_time())
    root.set("stale", pytak.cot_time(60))
    pt_attr = {
        "lat": "0.0",
        "lon": "0.0",
        "hae": "9999999.0",
        "ce": "9999999",
        "le": "9999999",
    }
    ET.SubElement(root, "point", attrib=pt_attr)

    callsign = "ROADS-FI"
    contact = ET.Element("contact")
    contact.set("callsign", callsign)
    contact.set("endpoint", "*:-1:stcp")

    takv = ET.Element("takv")
    takv.set("device", "Docker")
    takv.set("os", "Linux")
    takv.set("platform", "Road traffic feeder")
    takv.set("version", version)

    remarks = ET.Element("remarks")
    remarks.text = "#roads-fi"

    e_uid = ET.Element("uid")
    e_uid.set("Droid", callsign)

    dgroup = ET.Element("__group")
    dgroup.set("name", "White")
    dgroup.set("role", "HQ")

    track = ET.Element("track")
    track.set("course", "0")
    track.set("speed", "0")

    status = ET.Element("status")
    status.set("battery", "99")

    detail = ET.Element("detail")
    if mission_name:
        # Keepalive not mission-specific but include if needed
        pass
    detail.append(takv)
    detail.append(contact)
    detail.append(e_uid)
    detail.append(dgroup)
    detail.append(status)
    detail.append(track)
    detail.append(remarks)

    root.append(detail)
    return ET.tostring(root)
