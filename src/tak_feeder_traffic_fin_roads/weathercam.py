# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Street3r
"""
Digitraffic weather cameras (kelikamerat) as mission items.

One sensor marker per camera direction (preset). The marker opens the
camera's latest image as a video stream (WEATHERCAMS_STREAM_URL, a MediaMTX
server that fetches the image when someone opens the stream), and its view
cone shows where the camera looks. No images go through TAK.

Station list (GeoJSON FeatureCollection; presets without names):
  https://tie.digitraffic.fi/api/weathercam/v1/stations
Station details (preset names, direction codes, road address):
  https://tie.digitraffic.fi/api/weathercam/v1/stations/<stationId>
Latest image of a preset: https://weathercam.digitraffic.fi/<presetId>.jpg

Direction: a preset's `direction` tells where the camera looks relative to
the road address: INCREASING_DIRECTION = towards growing road addresses,
DECREASING_DIRECTION = the opposite (checked 2026-10-19 on roads 1, 3, 4, 7
and 51, which start in Helsinki: "Helsinkiin" cameras are DECREASING). The
road's bearing at the station comes from two road-address points 50 m apart,
converted to coordinates by the Finnish Transport Infrastructure Agency's
Viitekehysmuunnin (open API, ETRS-TM35FIN). Cameras on a crossing road and
special or unknown directions get no view cone.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .common import cot_time, env, env_float, env_list, remarks_text
from .digitraffic import SOURCE_LINE

LOG = logging.getLogger(__name__)

STATIONS_URL = "https://tie.digitraffic.fi/api/weathercam/v1/stations"
STATION_URL = STATIONS_URL + "/{station}"
IMAGE_URL = "https://weathercam.digitraffic.fi/{preset}.jpg"
ROAD_ADDRESS_URL = "https://avoinapi.vaylapilvi.fi/viitekehysmuunnin/muunna"
UID_PREFIX = "digitraffic.weathercam."
# Points per Viitekehysmuunnin batch request (the API allows 1000)
ROAD_BATCH = 1000


@dataclass
class WeathercamSettings:
    """WEATHERCAMS_* settings."""

    bbox: Optional[Tuple[float, float, float, float]] = None  # west, south, east, north
    # Sensor point: camcorder icon, and ATAK draws the view cone of <sensor>
    cot_type: str = "b-m-p-s-p-loc"
    icon: str = ""
    # Mission items are re-sent only when something changes, so the stale
    # time is long; removal from the mission hides a camera.
    stale_days: float = 365.0
    # rtmps://<host>:<port>/wc; the preset id is appended. Empty = no video.
    stream_url: str = ""
    stream_user: str = ""
    stream_password: str = ""
    fov: float = 60.0      # horizontal view angle of the cone (degrees)
    range_m: float = 250.0  # length of the cone (m)
    # Markers of a station's directions are moved this far in the view
    # direction so they do not lie on top of each other
    offset_m: float = 20.0
    # Station details and road bearings are fetched again after this
    details_refresh_hours: float = 24.0
    # Gap between station detail requests (s)
    request_gap: float = 0.2

    @classmethod
    def from_env(cls) -> "WeathercamSettings":
        """Read WEATHERCAMS_* variables."""
        d = cls()
        for old in ("WEATHERCAMS_IMAGE_BBOX", "WEATHERCAMS_EMBED_BBOX", "WEATHERCAMS_THUMBNAILS"):
            if env(old, ""):
                LOG.warning("%s is no longer used: images are streamed (WEATHERCAMS_STREAM_URL)",
                            old)
        return cls(
            bbox=_bbox_env("WEATHERCAMS_BBOX"),
            cot_type=env("WEATHERCAMS_COT_TYPE", d.cot_type),
            icon=env("WEATHERCAMS_ICON", d.icon),
            stale_days=env_float("WEATHERCAMS_STALE_DAYS", d.stale_days),
            stream_url=env("WEATHERCAMS_STREAM_URL", d.stream_url).rstrip("/"),
            stream_user=env("WEATHERCAMS_STREAM_USER", d.stream_user),
            stream_password=env("WEATHERCAMS_STREAM_PASSWORD", d.stream_password),
            fov=env_float("WEATHERCAMS_FOV", d.fov),
            range_m=env_float("WEATHERCAMS_RANGE", d.range_m),
            offset_m=env_float("WEATHERCAMS_OFFSET", d.offset_m),
            details_refresh_hours=env_float("WEATHERCAMS_DETAILS_REFRESH_HOURS",
                                            d.details_refresh_hours),
        )


def _bbox_env(name: str) -> Optional[Tuple[float, float, float, float]]:
    """west,south,east,north from the environment, None if unset or invalid."""
    parts = env_list(name, "")
    if not parts:
        return None
    try:
        if len(parts) == 4:
            west, south, east, north = (float(p) for p in parts)
            return (west, south, east, north)
    except ValueError:
        pass
    LOG.warning("%s=%s is not west,south,east,north; ignored", name, ",".join(parts))
    return None


def _in_bbox(lat: float, lon: float, bbox: Optional[Tuple[float, float, float, float]]) -> bool:
    if bbox is None:
        return True
    west, south, east, north = bbox
    return west <= lon <= east and south <= lat <= north


def _station_name(props: Dict[str, Any], fallback: str) -> str:
    names = props.get("names") or {}
    name = props.get("name") or (names.get("fi") if isinstance(names, dict) else None)
    return str(name or fallback).strip()


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


# ------------------------------------------------------------------ geometry
# ETRS-TM35FIN (UTM zone 35N on GRS80), the Viitekehysmuunnin coordinates
_A = 6378137.0
_F = 1 / 298.257222101
_K0 = 0.9996
_LON0 = math.radians(27.0)


def to_tm35fin(lat_deg: float, lon_deg: float) -> Tuple[float, float]:
    """WGS84 latitude/longitude -> ETRS-TM35FIN easting, northing (m)."""
    e2 = _F * (2 - _F)
    ep2 = e2 / (1 - e2)
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = _A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    t = math.tan(lat) ** 2
    c = ep2 * math.cos(lat) ** 2
    a = math.cos(lat) * (lon - _LON0)
    m = _A * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * lat
              - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * lat)
              + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * lat)
              - (35 * e2 ** 3 / 3072) * math.sin(6 * lat))
    x = 500000.0 + _K0 * n * (a + (1 - t + c) * a ** 3 / 6
                              + (5 - 18 * t + t ** 2 + 72 * c - 58 * ep2) * a ** 5 / 120)
    y = _K0 * (m + n * math.tan(lat) * (a ** 2 / 2 + (5 - t + 9 * c + 4 * c ** 2) * a ** 4 / 24
                                        + (61 - 58 * t + t ** 2 + 600 * c - 330 * ep2)
                                        * a ** 6 / 720))
    return x, y


def grid_bearing(p1: Tuple[float, float], p2: Tuple[float, float],
                 lat: float, lon: float) -> float:
    """True bearing (degrees) from TM35FIN point p1 to p2 near lat/lon."""
    grid = math.degrees(math.atan2(p2[0] - p1[0], p2[1] - p1[1]))
    convergence = math.degrees((math.radians(lon) - _LON0) * math.sin(math.radians(lat)))
    return (grid + convergence) % 360


def offset(lat: float, lon: float, azimuth: float, dist: float) -> Tuple[float, float]:
    """The point dist metres from lat/lon towards azimuth."""
    r = 6371000.0
    d = dist / r
    az = math.radians(azimuth)
    lat1, lon1 = math.radians(lat), math.radians(lon)
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(az))
    lon2 = lon1 + math.atan2(math.sin(az) * math.sin(d) * math.cos(lat1),
                             math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


def direction_sign(preset: Mapping[str, Any]) -> Optional[int]:
    """+1 camera looks towards growing road addresses, -1 the other way,
    None crossing road, special or unknown direction."""
    direction = str(preset.get("direction") or "").upper()
    code = str(preset.get("directionCode") or "")
    if direction == "INCREASING_DIRECTION" or (not direction and code == "1"):
        return 1
    if direction == "DECREASING_DIRECTION" or (not direction and code == "2"):
        return -1
    return None


# ------------------------------------------------------------------ details
def _post_form(url: str, data: Mapping[str, str], headers: Mapping[str, str],
               timeout: float = 60.0) -> bytes:
    """POST a form (gzip accepted)."""
    body = urllib.parse.urlencode(data).encode()
    hdrs = {"Accept-Encoding": "gzip",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", **headers}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        out: bytes = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            out = gzip.decompress(out)
    return out


class StationDirections:
    """Camera names and directions of each station (Digitraffic station
    details) and the bearing of its road (Viitekehysmuunnin), kept for
    WEATHERCAMS_DETAILS_REFRESH_HOURS."""

    def __init__(self, get_json: Any = None, post: Any = _post_form,
                 sleep: Any = asyncio.sleep, clock: Any = time.monotonic) -> None:
        self._get_json, self._post, self._sleep, self._clock = get_json, post, sleep, clock
        self.presets: Dict[str, Dict[str, Dict[str, Any]]] = {}  # station -> preset id -> data
        self.road: Dict[str, Dict[str, Any]] = {}  # station -> roadAddress
        self.bearing: Dict[str, Optional[float]] = {}  # station -> bearing of growing address
        self._fetched: Dict[str, float] = {}

    async def update(self, stations: List[Dict[str, Any]], ws: WeathercamSettings,
                     headers: Mapping[str, str]) -> None:
        """Fetch details of stations not seen or older than the refresh time,
        then the road bearing of those stations."""
        now = self._clock()
        max_age = ws.details_refresh_hours * 3600
        due = [st for st in stations
               if now - self._fetched.get(st["id"], -math.inf) >= max_age]
        if not due:
            return
        LOG.info("weather cameras: fetching details of %d stations", len(due))
        fetched: List[Dict[str, Any]] = []
        for i, st in enumerate(due):
            if i:
                await self._sleep(ws.request_gap)
            try:
                feature = await self._get_json(STATION_URL.format(station=st["id"]))
            except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
                LOG.warning("weather camera station %s: %s", st["id"], exc)
                continue
            props = feature.get("properties", feature) if isinstance(feature, dict) else {}
            self.presets[st["id"]] = {str(p.get("id")): p for p in props.get("presets", []) or []
                                      if isinstance(p, dict) and p.get("id")}
            self.road[st["id"]] = props.get("roadAddress") or {}
            self._fetched[st["id"]] = now
            fetched.append(st)
        try:
            bearings = await self._road_bearings(fetched, headers)
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            LOG.warning("weather cameras: road bearings not available: %s", exc)
            bearings = {}
        for st in fetched:
            self.bearing[st["id"]] = bearings.get(st["id"])
        LOG.info("weather cameras: road bearing for %d of %d stations",
                 sum(1 for st in fetched if bearings.get(st["id"]) is not None), len(fetched))

    async def _road_bearings(self, stations: List[Dict[str, Any]],
                             headers: Mapping[str, str]) -> Dict[str, float]:
        """station id -> bearing of growing road addresses at the station."""
        wanted: Dict[str, Tuple[Dict[str, Any], int, int]] = {}
        for st in stations:
            ra = self.road.get(st["id"]) or {}
            try:
                road, part = int(ra["roadNumber"]), int(ra["roadSection"])
                dist = int(ra.get("distanceFromRoadSectionStart") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            start = max(0, dist - 25)
            wanted[st["id"]] = ({"tie": road, "osa": part,
                                 "ajorata": 0 if ra.get("carriageway") == "ONE_CARRIAGEWAY"
                                 else "1,2"}, start, start + 50)
        points = await self._points(wanted, headers)
        # A point past the end of the road part: use the 50 m before the station
        retry = {sid: (q, max(0, end - 100), end - 50) for sid, (q, _, end) in wanted.items()
                 if (sid, "b") not in points and (sid, "a") in points}
        points.update(await self._points(retry, headers))
        out: Dict[str, float] = {}
        by_id = {st["id"]: st for st in stations}
        for sid in wanted:
            a, b = points.get((sid, "a")), points.get((sid, "b"))
            if a and b and math.dist(a, b) > 5:
                st = by_id[sid]
                out[sid] = grid_bearing(a, b, st["lat"], st["lon"])
        return out

    async def _points(self, wanted: Mapping[str, Tuple[Dict[str, Any], int, int]],
                      headers: Mapping[str, str]) -> Dict[Tuple[str, str], Tuple[float, float]]:
        """(station id, "a"/"b") -> TM35FIN point of the two road addresses."""
        queries = []
        for sid, (q, first, second) in wanted.items():
            for tag, dist in (("a", first), ("b", second)):
                queries.append({**q, "etaisyys": dist, "palautusarvot": "1",
                                "tunniste": f"{sid}:{tag}"})
        points: Dict[Tuple[str, str], Tuple[float, float]] = {}
        for i in range(0, len(queries), ROAD_BATCH):
            chunk = json.dumps(queries[i:i + ROAD_BATCH], separators=(",", ":"))
            body = await asyncio.to_thread(self._post, ROAD_ADDRESS_URL, {"json": chunk},
                                           headers)
            data = json.loads(body)
            for feat in (data.get("features") or []) if isinstance(data, dict) else []:
                props = feat.get("properties") or {}
                sid, _, tag = str(props.get("tunniste", "")).rpartition(":")
                if not sid or props.get("virheet") or (sid, tag) in points:
                    continue
                try:
                    points[(sid, tag)] = (float(props["x"]), float(props["y"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return points

    def cameras(self, station: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The station's presets with name, direction and azimuth (None
        when the camera's direction or the road's bearing is not known)."""
        details = self.presets.get(station["id"], {})
        bearing = self.bearing.get(station["id"])
        out = []
        for preset in station["presets"]:
            info = {**preset, **details.get(str(preset["id"]), {})}
            sign = direction_sign(info)
            azimuth = None
            if sign is not None and bearing is not None:
                azimuth = bearing if sign > 0 else (bearing + 180) % 360
            info["azimuth"] = azimuth
            out.append(info)
        return out


# ------------------------------------------------------------------ CoT
def stream_url(ws: WeathercamSettings, preset_id: str) -> str:
    """Video URL of a preset, with the viewer account as query parameters."""
    url = f"{ws.stream_url}/{preset_id}"
    if ws.stream_user:
        url += "?" + urllib.parse.urlencode({"user": ws.stream_user,
                                             "pass": ws.stream_password})
    return url


def camera_remarks(station: Dict[str, Any], camera: Dict[str, Any]) -> str:
    """Station, direction, image link, source line, tags."""
    label = str(camera.get("presentationName") or "").strip()
    lines: List[Optional[str]] = [
        f"Weather camera {station['name']}" + (f", {label}" if label else ""),
        "The video shows the latest image, updated about every 10 minutes",
        f"Image: {IMAGE_URL.format(preset=camera['id'])}",
    ]
    if camera.get("azimuth") is None:
        lines.append("Direction of view not known")
    return remarks_text(lines, SOURCE_LINE, ["weather", "weathercam"])


def camera_cot(station: Dict[str, Any], camera: Dict[str, Any], ws: WeathercamSettings,
               mission_name: str, how: str = "h-e", now: Optional[datetime] = None) -> bytes:
    """Mission item for one camera direction."""
    t = now or datetime.now(timezone.utc)
    preset_id = str(camera["id"])
    uid = UID_PREFIX + preset_id
    lat, lon = station["lat"], station["lon"]
    azimuth = camera.get("azimuth")
    if azimuth is not None and ws.offset_m:
        lat, lon = offset(lat, lon, azimuth, ws.offset_m)
    root = ET.Element("event", {
        "version": "2.0", "type": ws.cot_type, "uid": uid,
        "how": how, "time": cot_time(t), "start": cot_time(t),
        "stale": cot_time(t + timedelta(days=ws.stale_days)),
    })
    ET.SubElement(root, "point", {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
                                  "hae": "0", "ce": "10", "le": "10"})
    detail = ET.SubElement(root, "detail")
    marti = ET.SubElement(detail, "marti")
    ET.SubElement(marti, "dest", {"mission": mission_name})
    label = str(camera.get("presentationName") or "").strip()
    callsign = f"CAM {station['name']} {label}".strip()
    ET.SubElement(detail, "contact", {"callsign": callsign})
    if ws.icon:
        ET.SubElement(detail, "usericon", {"iconsetpath": ws.icon})
    if ws.stream_url:
        url = stream_url(ws, preset_id)
        parts = urllib.parse.urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        # uid as ATAK writes it itself: links the marker to its video alias
        video = ET.SubElement(detail, "__video", {"uid": uid, "url": url})
        ET.SubElement(video, "ConnectionEntry", {
            "networkTimeout": "12000", "uid": uid, "path": path,
            "protocol": parts.scheme, "bufferTime": "-1",
            "address": parts.hostname or "", "port": str(parts.port or -1),
            "roverPort": "-1", "rtspReliable": "0", "ignoreEmbeddedKLV": "false",
            "alias": callsign})
    if azimuth is not None:
        # No hideFov attribute: ATAK hides the cone when it is present at all
        ET.SubElement(detail, "sensor", {
            "fov": f"{ws.fov:g}", "vfov": "40", "azimuth": f"{azimuth:.0f}",
            "range": f"{ws.range_m:g}", "fovAlpha": "0.3", "fovRed": "0.2",
            "fovGreen": "0.6", "fovBlue": "1", "strokeColor": "-16776961",
            "strokeWeight": "0.5", "displayMagneticReference": "0", "rangeLines": "100",
            "model": "Digitraffic weather camera"})
    ET.SubElement(detail, "remarks").text = camera_remarks(station, camera)
    return ET.tostring(root)


def build_cot_files(fc: Dict[str, Any], ws: WeathercamSettings, mission_name: str,
                    directions: Optional[StationDirections] = None) -> Dict[str, bytes | str]:
    """Mission items keyed by UID, one per camera direction."""
    directions = directions or StationDirections()
    files: Dict[str, bytes | str] = {}
    stations = extract_stations(fc, ws)
    cones = 0
    for station in stations:
        for camera in directions.cameras(station):
            files[UID_PREFIX + str(camera["id"])] = camera_cot(station, camera, ws, mission_name)
            cones += camera["azimuth"] is not None
    features = len(fc.get("features", []) or [])
    LOG.info("%s: %d of %d stations, %d cameras, %d with a view cone", mission_name,
             len(stations), features, len(files), cones)
    if features and not stations:
        LOG.warning("%s: no station has usable presets; check the property names", mission_name)
    if not ws.stream_url:
        LOG.warning("%s: WEATHERCAMS_STREAM_URL not set, the markers have no video",
                    mission_name)
    return files
