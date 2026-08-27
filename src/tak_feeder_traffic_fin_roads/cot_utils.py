"""
CoT utilities for Fintraffic road traffic messages.

Converts normalized Digitraffic candidates (from digitraffic.py) to CoT XML.
Geometry handling discussion points are marked as TODO - see conversation.
"""

import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import pytak


def _color_for_announcement(announcement_type: str, features_str: str) -> str:
    """
    Map Digitraffic announcement type + features to ARGB color for ATAK.

    ARGB values from weather feeder:
      yellow 1090518784, orange 1090483968, red 1090453504, -1 white/default
    TODO: decide mapping for traffic - accident vs roadwork vs general.
    """
    low = (announcement_type + " " + features_str).lower()
    if "accident" in low or "onnettomuus" in low:
        return "1090453504"  # red
    if "suljettu" in low or "closed" in low:
        return "1090453504"  # red
    if "roadwork" in low or "tietyö" in low or "suljettu" in low:
        return "1090483968"  # orange
    if "general" in low:
        return "1090518784"  # yellow
    return "-1"


def generate_traffic_cot(
    candidate: Dict[str, Any],
    cot_type_point: str = "a-f-G-E-V-C",
    cot_type_line: str = "u-d-f",
    cot_how: str = "h-e",
    stale_secs: int = 300,
) -> Optional[bytes]:
    """
    Generate CoT XML for a single Digitraffic candidate.

    Decision points (for discussion):
      - Point vs LineString handling: icon at centroid vs polygon overlay?
      - Line drawing: use <link point="lat,lon"> repeated (like weather) or COT polygon via detail?
      - TAK type: u-d-f (friendly) vs a-*-G-E-V-C for vehicles? Incidents are typically u-d-p?
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

    # Choose CoT type based on geometry
    cot_type = cot_type_point if gtype == "Point" else cot_type_line

    root = ET.Element("event")
    root.set("version", "2.0")
    root.set("type", cot_type)
    root.set("uid", uid)
    root.set("how", cot_how)
    root.set("time", pytak.cot_time())
    root.set("start", pytak.cot_time())
    root.set("stale", pytak.cot_time(stale_secs))

    pt_attr = {
        "lat": str(lat),
        "lon": str(lon),
        "hae": "0",
        "ce": "10",
        "le": "10",
    }
    ET.SubElement(root, "point", attrib=pt_attr)

    detail = ET.Element("detail")

    contact = ET.Element("contact")
    contact.set("callsign", callsign)
    detail.append(contact)

    remarks_el = ET.Element("remarks")
    remarks_el.text = remarks
    detail.append(remarks_el)

    # Color mapping
    argb = _color_for_announcement(
        candidate.get("announcementType", ""),
        candidate.get("featuresStr", ""),
    )
    color = ET.Element("color")
    color.set("argb", argb)
    detail.append(color)

    # Add geometry as link points for polyline display (compatible with weather feeder pattern)
    # Weather feeder used: for point in cot["points"]: link point="lat,lon"
    # Here we expand LineString / MultiLineString into same.
    flat_coords: List[List[float]] = []
    if gtype == "Point" and coords:
        flat_coords = [coords]  # type: ignore[list-item]
    elif gtype == "LineString":
        flat_coords = coords  # type: ignore[assignment]
    elif gtype == "MultiLineString":
        for line in coords:  # type: ignore[union-attr]
            for pt in line:  # type: ignore[union-attr]
                flat_coords.append(pt)  # type: ignore[arg-type]

    # For ATAK, include all points as <link point="lat,lon"> ; limit to avoid huge CoT?
    # TODO: discuss decimation / simplification for long MultiLineStrings (e.g., 200+ points).
    max_points = 200  # safety cap
    if len(flat_coords) > max_points:
        # Simple decimation: take every Nth
        step = len(flat_coords) / max_points
        flat_coords = [flat_coords[int(i * step)] for i in range(max_points)]

    for lon_p, lat_p in flat_coords:  # type: ignore[misc]
        link = ET.Element("link")
        link.set("point", f"{lat_p},{lon_p}")
        detail.append(link)

    # Optional: add stroke styling for lines
    if gtype != "Point":
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
