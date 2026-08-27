"""
CoT utilities for Fintraffic road traffic messages.

Converts normalized Digitraffic candidates (from digitraffic.py) to CoT XML
for Mission API delivery. Implements:

- base COT type configurable via env COT_TYPE (default a-n-G)
- ATAK shape polyline when LineString/MultiLineString, pure point icon when Point
- stale = endTime when present, else start + 24h or far future for mission sync
- usericon via ICONSET_PATH (caution default, jam/restriction overrides, accident uses MIL a-u-G)
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional


def _parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    """Parse Digitraffic ISO8601 string to datetime (UTC)."""
    if not value:
        return None
    try:
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


def _cot_time_now() -> str:
    """Current time as COT string."""
    return _cot_time_from_dt(datetime.now(timezone.utc))


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


def _is_accident(announcement_type: str, features_str: str) -> bool:
    """Check if incident is accident / onnettomuus."""
    low = (announcement_type + " " + features_str).lower()
    return "onnettomuus" in low or "accident" in low


def _icon_for_announcement(
    announcement_type: str, features_str: str, default_icon: Optional[str]
) -> Optional[str]:
    """
    Map incident type to ATAK usericon.

    - default: warning triangle 6d781afb-89a6-4c07-b2b9-a89748b6a38f/Transport/caution.png
    - traffic jam / ruuhka -> traffic_jam.png (covers 'ruuhka', 'jonoutunut', 'jono')
    - road closed / tie suljettu -> restriction.png (covers 'suljettu')
    - accident / onnettomuus -> no usericon, uses MIL-STD a-u-G with milicon/milsym
    """
    if _is_accident(announcement_type, features_str):
        return None
    low = (announcement_type + " " + features_str).lower()
    # restriction (Tie suljettu) takes precedence over traffic_jam when both present
    if "suljettu" in low:
        return "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Transport/restriction.png"
    if "ruuhka" in low or "jonout" in low or "jono" in low:
        return "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Transport/traffic_jam.png"
    return default_icon


def _cot_type_for_announcement(
    announcement_type: str, features_str: str, default_type: str
) -> str:
    """Override COT type for accidents to a-u-G (unknown hazard accident)."""
    if _is_accident(announcement_type, features_str):
        return "a-u-G"
    return default_type


def generate_traffic_cot(
    candidate: Dict[str, Any],
    cot_type: str = "a-n-G",
    cot_how: str = "h-e",
    mission_name: Optional[str] = None,
    iconset_path: Optional[str] = None,
    cot_type_line: str = "u-d-f",
) -> Optional[bytes]:
    """
    Generate CoT XML for a single Digitraffic candidate.

    Mission API usage: include <marti><dest mission="..."/> if mission_name given.
    Geometry: Point -> pure point icon; LineString/MultiLineString -> ATAK shape polyline.
    Stale: endTime when present else startTime+24h else now+24h.
    Icon: usericon or MIL-STD for accidents.
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

    end_dt = _parse_iso8601(candidate.get("endTime"))
    start_dt = _parse_iso8601(candidate.get("startTime"))
    now = datetime.now(timezone.utc)
    if end_dt:
        stale_dt = end_dt
    elif start_dt:
        stale_dt = start_dt + timedelta(hours=24)
    else:
        stale_dt = now + timedelta(hours=24)

    start_for_cot = start_dt if start_dt else now

    # For polyline shapes try dedicated line type (u-d-f) so ATAK renders shape; accidents stay a-u-G
    is_polyline_for_type = geometry.get("type", "Point") in (
        "LineString",
        "MultiLineString",
    )
    if is_polyline_for_type and not _is_accident(
        candidate.get("announcementType", ""), candidate.get("featuresStr", "")
    ):
        actual_cot_type = cot_type_line
    else:
        actual_cot_type = _cot_type_for_announcement(
            candidate.get("announcementType", ""),
            candidate.get("featuresStr", ""),
            cot_type,
        )

    # Override callsign to Häiriö for generic triangle (default caution), except Nostotyö stays
    _default_caution = "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Transport/caution.png"
    _icon_preview = _icon_for_announcement(
        candidate.get("announcementType", ""),
        candidate.get("featuresStr", ""),
        iconset_path or _default_caution,
    )
    if _icon_preview == _default_caution and callsign != "Nostotyö":
        callsign = "Häiriö"

    root = ET.Element("event")
    root.set("version", "2.0")
    root.set("type", actual_cot_type)
    root.set("uid", uid)
    root.set("how", cot_how)
    root.set("time", _cot_time_now())
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

    argb = _color_for_announcement(
        candidate.get("announcementType", ""),
        candidate.get("featuresStr", ""),
    )

    if _is_accident(
        candidate.get("announcementType", ""),
        candidate.get("featuresStr", ""),
    ):
        # Accident: MIL-STD a-u-G EUI*HA + color (no usericon)
        color = ET.Element("color")
        color.set("argb", argb)
        detail.append(color)

        milicon = ET.Element("__milicon")
        milicon.set("id", "EUI*HA----*****")
        detail.append(milicon)
        milsym = ET.Element("__milsym")
        milsym.set("id", "EUI*HA----*****")
        detail.append(milsym)
    else:
        # Non-accident: usericon (caution/traffic_jam/restriction) — suppress <color> (icon already colored)
        # Stroke still uses argb for polyline via strokeColor below
        actual_icon = _icon_for_announcement(
            candidate.get("announcementType", ""),
            candidate.get("featuresStr", ""),
            iconset_path,
        )
        if actual_icon:
            usericon = ET.Element("usericon")
            usericon.set("iconsetpath", actual_icon)
            detail.append(usericon)

    is_polyline = gtype in ("LineString", "MultiLineString")

    if is_polyline:
        # Split into separate lines to avoid straight-line artifact
        # MultiLineString concatenating into one polyline draws a line back to start
        lines: List[List[List[float]]] = []
        if gtype == "LineString":
            lines = [coords]  # type: ignore[assignment]
        elif gtype == "MultiLineString":
            lines = coords  # type: ignore[assignment]

        # Decimate to keep total points <200, per line proportionally
        total_pts = sum(len(line) for line in lines)
        max_points = 200
        decimated_lines: List[List[List[float]]] = []
        for line in lines:
            if total_pts > max_points and len(line) > 1:
                keep = max(2, int(len(line) * max_points / total_pts))
                if len(line) > keep:
                    step = len(line) / keep
                    line = [line[int(i * step)] for i in range(keep)]  # type: ignore[arg-type]
            decimated_lines.append(line)  # type: ignore[arg-type]

        shape = ET.Element("shape")
        for line in decimated_lines:
            polyline = ET.Element("polyline")
            for lon_p, lat_p in line:  # type: ignore[misc]
                vertex = ET.Element("vertex")
                vertex.set("lat", str(lat_p))
                vertex.set("lon", str(lon_p))
                vertex.set("hae", "0")
                polyline.append(vertex)
            shape.append(polyline)
        detail.append(shape)

        for line in decimated_lines:
            for lon_p, lat_p in line:  # type: ignore[misc]
                link = ET.Element("link")
                link.set("point", f"{lat_p},{lon_p}")
                detail.append(link)

        stroke_color = ET.Element("strokeColor")
        stroke_color.set("value", argb)
        detail.append(stroke_color)

        stroke_weight = ET.Element("strokeWeight")
        stroke_weight.set("value", "3")
        detail.append(stroke_weight)

        stroke_style = ET.Element("strokeStyle")
        stroke_style.set("value", "solid")
        detail.append(stroke_style)

    root.append(detail)

    return ET.tostring(root)
