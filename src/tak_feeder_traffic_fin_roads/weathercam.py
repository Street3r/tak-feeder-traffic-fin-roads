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

import asyncio
import hashlib
import json
import logging
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .common import cot_time, env, env_bool, env_float, env_list, remarks_text
from .digitraffic import SOURCE_LINE

LOG = logging.getLogger(__name__)

STATIONS_URL = "https://tie.digitraffic.fi/api/weathercam/v1/stations"
IMAGE_URL = "https://weathercam.digitraffic.fi/{preset}.jpg"
# Name of a station's thumbnail in the mission; the feeder only touches files
# with this prefix
FILE_PREFIX = "weathercam_"


@dataclass
class WeathercamSettings:
    """WEATHERCAMS_* settings."""

    bbox: Optional[Tuple[float, float, float, float]] = None  # west, south, east, north
    thumbnails: bool = False
    # Sensor point; ATAK draws it with its own sensor (camcorder) icon.
    # Quick Pic (b-i-x-i) is left for photos taken by users.
    cot_type: str = "b-m-p-s-p-loc"
    # Stations in this area get the thumbnail of their first camera as a
    # mission file attached to the marker. None = no images.
    image_bbox: Optional[Tuple[float, float, float, float]] = None
    image_max: int = 100
    # Also delete replaced thumbnails from the server's file store, not only
    # from the mission
    delete_old_files: bool = True
    # Digitraffic allows 60 image requests a minute per IP
    image_request_gap: float = 1.1
    icon: str = ""
    # Mission items are re-sent only when the station set changes, so the
    # stale time is long; removal from the mission hides a station.
    stale_days: float = 365.0

    @classmethod
    def from_env(cls) -> "WeathercamSettings":
        """Read WEATHERCAMS_* variables."""
        d = cls()
        return cls(
            bbox=_bbox_env("WEATHERCAMS_BBOX"),
            # WEATHERCAMS_EMBED_* are the names of the earlier embedded-image test
            image_bbox=(_bbox_env("WEATHERCAMS_IMAGE_BBOX")
                        or _bbox_env("WEATHERCAMS_EMBED_BBOX")),
            image_max=int(env_float("WEATHERCAMS_IMAGE_MAX",
                                    env_float("WEATHERCAMS_EMBED_MAX", d.image_max))),
            delete_old_files=env_bool("WEATHERCAMS_DELETE_OLD_FILES", d.delete_old_files),
            thumbnails=env_bool("WEATHERCAMS_THUMBNAILS", d.thumbnails),
            cot_type=env("WEATHERCAMS_COT_TYPE", d.cot_type),
            icon=env("WEATHERCAMS_ICON", d.icon),
            stale_days=env_float("WEATHERCAMS_STALE_DAYS", d.stale_days),
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
                how: str = "h-e", now: Optional[datetime] = None,
                attachment: Optional[str] = None) -> bytes:
    """Mission item for one camera station; attachment = hash of the mission
    file shown as the marker's attachment."""
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
    if attachment:
        # TAK links mission files to a map item through this list (as ATAK
        # does for attachments a user publishes)
        ET.SubElement(detail, "attachment_list", {"hashes": json.dumps([attachment])})
    return ET.tostring(root)


def build_cot_files(fc: Dict[str, Any], ws: WeathercamSettings, mission_name: str,
                    attachments: Optional[Mapping[str, str]] = None) -> Dict[str, bytes | str]:
    """Mission items keyed by UID. attachments: station id -> file hash."""
    files: Dict[str, bytes | str] = {}
    stations = extract_stations(fc, ws)
    for station in stations:
        files[f"digitraffic.weathercam.{station['id']}"] = station_cot(
            station, ws, mission_name, attachment=(attachments or {}).get(station["id"]))
    features = len(fc.get("features", []) or [])
    LOG.info("%s: %d of %d stations, %d cameras", mission_name, len(stations), features,
             sum(len(s["presets"]) for s in stations))
    if features and not stations:
        LOG.warning("%s: no station has usable presets; check the property names", mission_name)
    return files


def _get_image(url: str, headers: Mapping[str, str], timeout: float = 30.0
               ) -> Tuple[int, str, bytes]:
    """GET an image: (status, ETag, body); 304 when If-None-Match matches."""
    req = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.headers.get("ETag", ""), resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, exc.headers.get("ETag", "") if exc.headers else "", b""
        raise


class ThumbnailCache:
    """Latest thumbnail of each selected station, refreshed with ETags."""

    def __init__(self, get: Any = _get_image, sleep: Any = asyncio.sleep) -> None:
        self._get, self._sleep = get, sleep
        self.images: Dict[str, bytes] = {}  # station id -> JPEG
        self._etags: Dict[str, str] = {}

    def selected(self, fc: Dict[str, Any], ws: WeathercamSettings) -> List[Dict[str, Any]]:
        """Stations that get a thumbnail."""
        if ws.image_bbox is None:
            return []
        stations = [s for s in extract_stations(fc, ws)
                    if _in_bbox(s["lat"], s["lon"], ws.image_bbox)]
        if len(stations) > ws.image_max:
            LOG.warning("%d stations in WEATHERCAMS_IMAGE_BBOX, using the first %d "
                        "(WEATHERCAMS_IMAGE_MAX)", len(stations), ws.image_max)
        return stations[:ws.image_max]

    async def refresh(self, fc: Dict[str, Any], ws: WeathercamSettings,
                      headers: Mapping[str, str]) -> Dict[str, bytes]:
        """Fetch changed thumbnails (first camera of each station), at most
        one request per image_request_gap seconds. Returns the images."""
        stations = self.selected(fc, ws)
        wanted = {s["id"] for s in stations}
        for sid in set(self.images) - wanted:
            self.images.pop(sid, None)
            self._etags.pop(sid, None)
        fetched = 0
        for i, station in enumerate(stations):
            if i:
                await self._sleep(ws.image_request_gap)
            url = IMAGE_URL.format(preset=station["presets"][0]["id"]) + "?thumbnail=true"
            hdrs = dict(headers)
            if station["id"] in self._etags:
                hdrs["If-None-Match"] = self._etags[station["id"]]
            try:
                status, etag, body = await asyncio.to_thread(self._get, url, hdrs)
            except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
                LOG.warning("thumbnail %s: %s", url, exc)
                continue
            if status == 200 and body:
                self.images[station["id"]] = body
                if etag:
                    self._etags[station["id"]] = etag
                fetched += 1
        if stations:
            LOG.info("thumbnails: %d stations, %d new", len(self.images), fetched)
        return dict(self.images)


def file_name(station_id: str) -> str:
    """Mission file name of a station's thumbnail (the same every round)."""
    return f"{FILE_PREFIX}{station_id}.jpg"


def _json(body: Any) -> Any:
    if isinstance(body, (bytes, str)):
        try:
            return json.loads(body)
        except ValueError:
            return None
    return body


def mission_files(mission: Mapping[str, Any]) -> Dict[str, str]:
    """hash -> name of this feeder's thumbnails in a mission (get_mission data)."""
    files: Dict[str, str] = {}
    for content in mission.get("contents", []) or []:
        data = content.get("data", content) if isinstance(content, dict) else {}
        name = str(data.get("name") or data.get("filename") or "")
        digest = data.get("hash")
        if digest and name.startswith(FILE_PREFIX):
            files[str(digest)] = name
    return files


class AttachmentSync:
    """Keeps one thumbnail file per station in the mission: uploads new
    pictures, adds them to the mission and removes the replaced ones."""

    def __init__(self, server: Any, mission_name: str, creator_uid: str) -> None:
        self.server, self.mission_name, self.creator_uid = server, mission_name, creator_uid

    async def _request(self, method: str, path: str, token: str = "", **kwargs: Any
                       ) -> Tuple[int, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return await self.server.connection.request(
            method, self.server.api_base_url + path, headers=headers, **kwargs)

    async def upload(self, images: Mapping[str, bytes], mission: Mapping[str, Any],
                     token: str) -> Tuple[Dict[str, str], List[str]]:
        """Upload the pictures the mission does not have yet. Returns
        station id -> hash for the markers and the hashes to remove once the
        markers point at the new files."""
        present = mission_files(mission)
        hashes: Dict[str, str] = {}
        new: Dict[str, str] = {}  # hash -> station id
        for sid, body in images.items():
            digest = hashlib.sha256(body).hexdigest()
            if digest not in present and digest not in new:
                query = urllib.parse.urlencode({"name": file_name(sid),
                                                "creatorUid": self.creator_uid})
                status, result = await self._request(
                    "post", f"/Marti/sync/upload?{query}",
                    headers={"Content-Type": "image/jpeg"}, data=body)
                if status != 200:
                    LOG.warning("%s: upload of %s failed: HTTP %s %s", self.mission_name,
                                file_name(sid), status, str(result)[:200])
                    continue
                meta = _json(result)
                stored = str(meta.get("Hash") or "") if isinstance(meta, dict) else ""
                if stored and stored.lower() != digest:
                    LOG.warning("%s: server hash %s differs from %s for %s",
                                self.mission_name, stored, digest, file_name(sid))
                    digest = stored.lower()
                new[digest] = sid
            hashes[sid] = digest
        if new:
            status, result = await self._request(
                "put", f"/Marti/api/missions/{urllib.parse.quote(self.mission_name)}/contents"
                f"?{urllib.parse.urlencode({'creatorUid': self.creator_uid})}",
                token=token, json={"hashes": sorted(new)})
            if status != 200:
                LOG.warning("%s: adding %d files failed: HTTP %s %s", self.mission_name,
                            len(new), status, str(result)[:200])
                for digest, sid in new.items():
                    hashes.pop(sid, None)
        in_use = set(hashes.values())
        old = [digest for digest in present if digest not in in_use]
        LOG.info("%s: %d thumbnails, %d new files, %d to remove", self.mission_name,
                 len(hashes), len(new), len(old))
        return hashes, old

    async def remove(self, digests: List[str], token: str, delete_files: bool) -> None:
        """Remove replaced thumbnails from the mission (and the file store)."""
        for digest in digests:
            query = urllib.parse.urlencode({"hash": digest, "creatorUid": self.creator_uid})
            status, result = await self._request(
                "delete",
                f"/Marti/api/missions/{urllib.parse.quote(self.mission_name)}/contents?{query}",
                token=token)
            if status != 200:
                LOG.warning("%s: removing file %s failed: HTTP %s %s", self.mission_name,
                            digest, status, str(result)[:200])
                continue
            if delete_files:
                status, result = await self._request(
                    "delete", f"/Marti/sync/delete?{urllib.parse.urlencode({'Hash': digest})}")
                if status != 200:
                    LOG.debug("%s: deleting stored file %s: HTTP %s %s", self.mission_name,
                              digest, status, str(result)[:200])
