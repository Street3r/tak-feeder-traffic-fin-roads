# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Street3r
"""
Digitraffic weather cameras (kelikamerat) as mission items.

One marker per camera station. The remarks link the latest image of each
camera direction (preset); the image URL always serves the newest picture,
so the items only change when stations enter or leave collection. ATAK
fetches an image only when the user opens its link.

Station list (GeoJSON FeatureCollection):
  https://tie.digitraffic.fi/api/weathercam/v1/stations
Latest image of a preset: https://weathercam.digitraffic.fi/<presetId>.jpg
(?thumbnail=true for a small one). Images update about every 10 minutes.

The property names below follow the Digitraffic documentation (id, name,
collectionStatus, presets[].id / inCollection). Not yet checked against a
live response; unknown fields are ignored.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .common import cot_time, env, env_bool, env_float, env_list, remarks_text
from .digitraffic import SOURCE_LINE

LOG = logging.getLogger(__name__)

STATIONS_URL = "https://tie.digitraffic.fi/api/weathercam/v1/stations"
IMAGE_URL = "https://weathercam.digitraffic.fi/{preset}.jpg"


@dataclass
class WeathercamSettings:
    """WEATHERCAMS_* settings."""

    bbox: Optional[Tuple[float, float, float, float]] = None  # west, south, east, north
    thumbnails: bool = False
    cot_type: str = "a-n-G"
    icon: str = ""
    # Mission items are re-sent only when the station set changes, so the
    # stale time is long; removal from the mission hides a station.
    stale_days: float = 365.0

    @classmethod
    def from_env(cls) -> "WeathercamSettings":
        """Read WEATHERCAMS_* variables."""
        d = cls()
        bbox: Optional[Tuple[float, float, float, float]] = None
        parts = env_list("WEATHERCAMS_BBOX", "")
        try:
            if len(parts) == 4:
                west, south, east, north = (float(p) for p in parts)
                bbox = (west, south, east, north)
        except ValueError:
            LOG.warning("WEATHERCAMS_BBOX=%s is not west,south,east,north; using all", parts)
        return cls(
            bbox=bbox,
            thumbnails=env_bool("WEATHERCAMS_THUMBNAILS", d.thumbnails),
            cot_type=env("WEATHERCAMS_COT_TYPE", d.cot_type),
            icon=env("WEATHERCAMS_ICON", d.icon),
            stale_days=env_float("WEATHERCAMS_STALE_DAYS", d.stale_days),
        )


def _in_bbox(lat: float, lon: float, bbox: Optional[Tuple[float, float, float, float]]) -> bool:
    if bbox is None:
        return True
    west, south, east, north = bbox
    return west <= lon <= east and south <= lat <= north


def _station_name(props: Dict[str, Any], fallback: str) -> str:
    names = props.get("names") or {}
    name = props.get("name") or (names.get("fi") if isinstance(names, dict) else None)
    return str(name or fallback).strip()


def _preset_label(preset: Dict[str, Any], index: int) -> str:
    label = preset.get("presentationName") or preset.get("direction")
    return str(label).strip() if label else f"Camera {index}"


def extract_stations(fc: Dict[str, Any], ws: WeathercamSettings) -> List[Dict[str, Any]]:
    """Stations in collection with their presets in collection."""
    stations: List[Dict[str, Any]] = []
    for feat in fc.get("features", []) or []:
        props: Dict[str, Any] = feat.get("properties", {}) or {}
        geometry: Dict[str, Any] = feat.get("geometry", {}) or {}
        coords = geometry.get("coordinates") or []
        station_id = props.get("id") or feat.get("id")
        if not station_id or len(coords) < 2:
            continue
        if props.get("collectionStatus", "GATHERING") != "GATHERING":
            continue
        lon, lat = float(coords[0]), float(coords[1])
        if not _in_bbox(lat, lon, ws.bbox):
            continue
        presets = [p for p in props.get("presets", []) or []
                   if isinstance(p, dict) and p.get("id") and p.get("inCollection", True)]
        if not presets:
            continue
        stations.append({
            "id": str(station_id),
            "name": _station_name(props, str(station_id)),
            "lat": lat,
            "lon": lon,
            "presets": presets,
        })
    return stations


def station_remarks(station: Dict[str, Any], ws: WeathercamSettings) -> str:
    """Station name, one image link per direction, source line, tags."""
    suffix = "?thumbnail=true" if ws.thumbnails else ""
    lines: List[Optional[str]] = [f"Weather camera {station['name']}"]
    for i, preset in enumerate(station["presets"], 1):
        url = IMAGE_URL.format(preset=preset["id"]) + suffix
        lines.append(f"{_preset_label(preset, i)}: {url}")
    lines.append("Latest image, updated about every 10 minutes")
    return remarks_text(lines, SOURCE_LINE, ["weather", "weathercam"])


def station_cot(station: Dict[str, Any], ws: WeathercamSettings, mission_name: str,
                how: str = "h-e", now: Optional[datetime] = None) -> bytes:
    """Mission item for one camera station."""
    t = now or datetime.now(timezone.utc)
    root = ET.Element("event", {
        "version": "2.0", "type": ws.cot_type, "uid": f"digitraffic.weathercam.{station['id']}",
        "how": how, "time": cot_time(t), "start": cot_time(t),
        "stale": cot_time(t + timedelta(days=ws.stale_days)),
    })
    ET.SubElement(root, "point", {"lat": f"{station['lat']:.6f}", "lon": f"{station['lon']:.6f}",
                                  "hae": "0", "ce": "10", "le": "10"})
    detail = ET.SubElement(root, "detail")
    marti = ET.SubElement(detail, "marti")
    ET.SubElement(marti, "dest", {"mission": mission_name})
    callsign = f"CAM {station['name']}"
    ET.SubElement(detail, "contact", {"callsign": callsign[:40]})
    if ws.icon:
        ET.SubElement(detail, "usericon", {"iconsetpath": ws.icon})
    ET.SubElement(detail, "remarks").text = station_remarks(station, ws)
    return ET.tostring(root)


def build_cot_files(fc: Dict[str, Any], ws: WeathercamSettings,
                    mission_name: str) -> Dict[str, bytes | str]:
    """Mission items keyed by UID."""
    files: Dict[str, bytes | str] = {}
    stations = extract_stations(fc, ws)
    for station in stations:
        files[f"digitraffic.weathercam.{station['id']}"] = station_cot(station, ws, mission_name)
    features = len(fc.get("features", []) or [])
    LOG.info("%s: %d of %d stations, %d cameras", mission_name, len(stations), features,
             sum(len(s["presets"]) for s in stations))
    if features and not stations:
        LOG.warning("%s: no station has usable presets; check the property names", mission_name)
    return files
