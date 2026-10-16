# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright Stefan Gofferje (tak-feeder-traffic-fin-roads)
# Copyright (C) 2026 Street3r (datasets, mission groups, see README)
"""Mission sync of Digitraffic road traffic messages (one mission per dataset)."""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import __version__, cot_utils, digitraffic, weathercam
from .common import Health, digitraffic_headers, env, env_float, env_list

logger = logging.getLogger(__name__)

DEFAULT_MISSIONS = {
    "traffic-announcements": "TrafficFinRoads",  # as in upstream
    "roadworks": "TrafficFinRoadworks",
    "weight-restrictions": "TrafficFinWeightRestrictions",
    "exempted-transports": "TrafficFinExemptedTransports",
    "weathercams": "WeatherCamsFin",
}

# Datasets that are not Digitraffic traffic messages: URL and default interval (s)
OTHER_SOURCES = {
    "weathercams": (weathercam.STATIONS_URL, 600.0),
}


@dataclass
class Dataset:
    """One Digitraffic dataset synced to one mission."""

    name: str
    url: str
    mission_name: str
    groups: List[str] = field(default_factory=list)
    token: str = ""
    interval: float = 0.0  # 0 = UPDATE_INTERVAL


@dataclass
class Settings:
    """Feeder settings from the environment (see .env.example)."""

    api_host: str = ""
    cert: str = ""
    key: str = ""
    my_uid: str = "roads.0001"
    digitraffic_user: str = "tak-feeder-traffic-fin-roads"
    update_interval: float = 60.0
    retry_interval: float = 300.0
    cot_type: str = "a-n-G"
    cot_type_line: str = "u-d-f"
    cot_how: str = "h-e"
    iconset_path: str = "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Transport/caution.png"
    timezone: str = "Europe/Helsinki"
    datasets: List[Dataset] = field(default_factory=list)
    weathercam: weathercam.WeathercamSettings = field(
        default_factory=weathercam.WeathercamSettings)

    @classmethod
    def from_env(cls) -> "Settings":
        """Read settings. Each dataset has <PREFIX>_MISSION_NAME / _MISSION_GROUP;
        the traffic announcements keep the upstream names MISSION_NAME /
        MISSION_GROUP / MISSION_TOKEN / DIGITRAFFIC_URL."""
        d = cls()
        datasets = []
        for name in env_list("DATASETS", "traffic-announcements"):
            if name not in DEFAULT_MISSIONS:
                logger.warning("Unknown dataset %r (known: %s)", name, ", ".join(DEFAULT_MISSIONS))
                continue
            if name == "traffic-announcements":
                datasets.append(Dataset(
                    name, env("DIGITRAFFIC_URL", digitraffic.DEFAULT_URL),
                    env("MISSION_NAME", DEFAULT_MISSIONS[name]),
                    env_list("MISSION_GROUP", "", "MISSION_GROUPS"),
                    env("MISSION_TOKEN", "")))
            else:
                prefix = name.upper().replace("-", "_")
                url, interval = OTHER_SOURCES.get(name, (f"{digitraffic.BASE_URL}/{name}", 0.0))
                datasets.append(Dataset(
                    name, url,
                    env(f"{prefix}_MISSION_NAME", DEFAULT_MISSIONS[name]),
                    env_list(f"{prefix}_MISSION_GROUP", ""),
                    env(f"{prefix}_MISSION_TOKEN", ""),
                    env_float(f"{prefix}_UPDATE_INTERVAL", interval)))
        return cls(
            api_host=env("API_HOST", ""),
            cert=env("PYTAK_TLS_CLIENT_CERT", ""),
            key=env("PYTAK_TLS_CLIENT_KEY", ""),
            my_uid=env("MY_UID", d.my_uid),
            digitraffic_user=env("DIGITRAFFIC_USER", d.digitraffic_user),
            update_interval=env_float("UPDATE_INTERVAL", d.update_interval),
            retry_interval=env_float("RETRY_INTERVAL", d.retry_interval),
            cot_type=env("COT_TYPE", d.cot_type),
            cot_type_line=env("COT_TYPE_LINE", d.cot_type_line),
            cot_how=env("COT_HOW", d.cot_how),
            iconset_path=env("ICONSET_PATH", d.iconset_path),
            timezone=env("TIMEZONE", d.timezone),
            datasets=datasets,
            weathercam=weathercam.WeathercamSettings.from_env(),
        )


def build_cot_files(candidates: List[Dict[str, Any]], s: Settings,
                    mission_name: str) -> Dict[str, bytes | str]:
    """CoT per mission item. Non-accident lines are split into an icon CoT and
    one shape CoT per line so ATAK shows both without a closing line
    (upstream logic)."""
    files: Dict[str, bytes | str] = {}
    for cand in candidates:
        geometry = cand.get("geometry", {}) or {}
        gtype = geometry.get("type", "Point")
        is_acc = cot_utils._is_accident(  # pylint: disable=protected-access
            cand.get("announcementType", ""), cand.get("featuresStr", ""))
        icon = cot_utils.generate_traffic_cot(
            cand, cot_type=s.cot_type, cot_how=s.cot_how, mission_name=mission_name,
            iconset_path=s.iconset_path or None, cot_type_line=s.cot_type_line)
        if icon:
            files[cand["uid"]] = icon
        if gtype not in ("LineString", "MultiLineString") or is_acc:
            continue
        argb = cot_utils._color_for_announcement(  # pylint: disable=protected-access
            cand.get("announcementType", ""), cand.get("featuresStr", ""))
        coords = geometry.get("coordinates", []) or []
        lines = [(None, coords)] if gtype == "LineString" else list(enumerate(coords))
        for idx, line in lines:
            shape = cot_utils._generate_shape_cot_single(  # pylint: disable=protected-access
                cand, line_coords=line, line_index=idx, cot_type_line=s.cot_type_line,
                cot_how=s.cot_how, mission_name=mission_name, argb=argb,
                centroid=cand.get("centroid"))
            if shape:
                files[f"{cand['uid']}.line" + ("" if idx is None else f".{idx}")] = shape
    return files


def _points(values: List[Tuple[str, str]]) -> Tuple[Tuple[float, float], ...]:
    out = []
    for lat, lon in values:
        try:
            out.append((round(float(lat), 5), round(float(lon), 5)))
        except ValueError:
            continue
    return tuple(out)


def cot_signature(event: ET.Element) -> Tuple[Any, ...]:
    """What ATAK shows of a CoT item: type, position, label, remarks, icon,
    colour, line points and attached files. Times are left out, they change
    every round."""
    point = event.find("point")
    detail = event.find("detail")

    def attr(path: str, name: str) -> str:
        el = detail.find(path) if detail is not None else None
        return el.get(name, "") if el is not None else ""

    remarks = detail.find("remarks") if detail is not None else None
    links = detail.findall("link") if detail is not None else []
    return (
        event.get("type", ""),
        _points([(point.get("lat", ""), point.get("lon", ""))]) if point is not None else (),
        attr("contact", "callsign"),
        (remarks.text or "").strip() if remarks is not None else "",
        attr("usericon", "iconsetpath"),
        attr("color", "argb"),
        _points([tuple(l.get("point", ",").split(",", 1)) for l in links]),  # type: ignore[misc]
        _attachments(detail),
    )


def _attachments(detail: Optional[ET.Element]) -> str:
    """Hashes of the attached mission files, so a new picture counts as a change."""
    el = detail.find("attachment_list") if detail is not None else None
    hashes = el.get("hashes", "") if el is not None else ""
    return "".join(c for c in hashes if c.isalnum() or c == ",").lower()


def parse_signatures(xml: Any) -> Optional[Dict[str, Tuple[Any, ...]]]:
    """uid -> signature of the CoT items TAK stores for a mission, None if the
    response cannot be read."""
    if isinstance(xml, bytes):
        xml = xml.decode("utf-8", "replace")
    if not isinstance(xml, str) or not xml.strip():
        return None
    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError:
        return None
    return {e.get("uid", ""): cot_signature(e) for e in root.iter("event")}


class MissionSync:
    """Keeps one mission in sync with one dataset."""

    def __init__(self, s: Settings, ds: Dataset, server: Any, build_package: Any) -> None:
        self.s, self.ds, self.server, self.build_package = s, ds, server, build_package
        self.thumbnails = weathercam.ThumbnailCache()
        self.attachments = weathercam.AttachmentSync(server, ds.mission_name, s.my_uid)
        # station id -> thumbnail hash the marker points at
        self.hashes: Optional[Dict[str, str]] = None

    async def _create(self) -> None:
        group: Any = self.ds.groups or ""  # a list becomes repeated group=
        status, result = await self.server.mission.create_mission(
            self.ds.mission_name, self.s.my_uid, group=group,
            default_role="MISSION_READONLY_SUBSCRIBER", classification="unclassified")
        if status >= 400:
            raise RuntimeError(f"cannot create mission {self.ds.mission_name}: HTTP {status}")
        try:
            self.ds.token = result["data"][0]["token"]
        except (KeyError, IndexError, TypeError):
            self.ds.token = result.get("data", {}).get("token", "")
        logger.info("Mission %s created (groups: %s)", self.ds.mission_name,
                    ",".join(self.ds.groups) or "default")

    async def _subscribe(self) -> None:
        if self.ds.token:
            return
        status, result = await self.server.mission.create_mission_subscription(
            self.ds.mission_name, self.s.my_uid)
        if status == 201:
            self.ds.token = result["data"]["token"]
        elif status == 404:
            await self._create()
        else:
            raise RuntimeError(f"subscription to {self.ds.mission_name} failed: HTTP {status}")

    async def _changed(self, files: Dict[str, Any], uids_differ: bool) -> Dict[str, Any]:
        """Items that are new or whose content differs from what TAK stores.
        If the stored items cannot be read, everything is sent when the set of
        items changed (the old behaviour)."""
        if not files:
            return {}
        stored: Optional[Dict[str, Tuple[Any, ...]]] = None
        try:
            status, body = await self.server.mission.get_mission_cot(self.ds.mission_name)
            if status == 200:
                stored = parse_signatures(body)
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            logger.debug("%s: cannot read stored items: %s", self.ds.mission_name, exc)
        if stored is None:
            logger.debug("%s: stored items not readable", self.ds.mission_name)
            return dict(files) if uids_differ else {}
        changed = {}
        for uid, cot in files.items():
            try:
                sig = cot_signature(ET.fromstring(cot))
            except ET.ParseError:
                sig = None
            if stored.get(uid) != sig:
                changed[uid] = cot
        return changed

    async def run_once(self) -> bool:
        """One round. True if a package was uploaded."""
        await self._subscribe()
        status, mission = await self.server.mission.get_mission(self.ds.mission_name)
        if status == 404:
            await self._create()
            return False
        if status != 200:
            raise RuntimeError(f"cannot get mission {self.ds.mission_name}: HTTP {status}")

        fc = await digitraffic.fetch_geojson(self.ds.url, self.s.digitraffic_user)
        if self.ds.name == "weathercams":
            return await self._run_weathercams(mission["data"][0], fc)
        candidates = digitraffic.extract_cot_candidates(fc, self.ds.name, self.s.timezone)
        files = build_cot_files(candidates, self.s, self.ds.mission_name)
        return await self._sync_items(mission["data"][0], files)

    async def _sync_items(self, mission: Dict[str, Any], files: Dict[str, Any]) -> bool:
        """Remove items that left the dataset and upload new or changed ones.
        True if a package was uploaded."""
        wanted = set(files)
        present = {u.get("data") for u in mission.get("uids", []) if isinstance(u, dict)}
        for uid in sorted(present - wanted):
            logger.info("%s: removing %s", self.ds.mission_name, uid)
            s, r = await self.server.mission.remove_mission_content(
                self.ds.mission_name, uid, self.s.my_uid, self.ds.token)
            if s != 200:
                logger.warning("Could not remove %s: HTTP %s %s", uid, s, r)
            await asyncio.sleep(3)
        upload = await self._changed(files, wanted != present)
        if not upload:
            logger.info("%s: %d items, no changes", self.ds.mission_name, len(wanted))
            return False
        logger.info("%s: uploading %d of %d items", self.ds.mission_name,
                    len(upload), len(files))
        await self._upload_package(upload)
        return True

    async def _upload_package(self, cot_files: Dict[str, Any]) -> None:
        package = self.build_package(
            name=self.ds.mission_name, mission_name=self.ds.mission_name,
            mission_server=f"{self.s.api_host}:8443:ssl", creator_uid=self.s.my_uid,
            cot_files=cot_files, groups=self.ds.groups or None)
        s, r = await self.server.mission.add_mission_package(
            self.ds.mission_name, self.s.my_uid, self.ds.token, package)
        if s != 200:
            raise RuntimeError(f"package upload to {self.ds.mission_name} failed: HTTP {s} {r}")

    async def _run_weathercams(self, mission: Dict[str, Any], fc: Dict[str, Any]) -> bool:
        """Sync the station markers, then send the thumbnails in batches as
        they are fetched: each batch uploads its new pictures, points its
        markers at them and removes the pictures they replaced."""
        ws, name = self.s.weathercam, self.ds.mission_name
        present = weathercam.mission_files(mission)  # hash -> file name
        if self.hashes is None:  # after a restart: what the markers show now
            self.hashes = weathercam.marker_attachments(mission)
        stations = self.thumbnails.selected(fc, ws)
        ids = [st["id"] for st in stations]
        self.thumbnails.prune(ids)
        self.hashes = {sid: h for sid, h in self.hashes.items() if sid in ids and h in present}
        uploaded = await self._sync_items(
            mission, weathercam.build_cot_files(fc, ws, name, self.hashes))
        # files of stations that left the image area or of a failed round
        await self._remove_unused(present)
        headers = digitraffic_headers(self.s.digitraffic_user, "tak-feeder-traffic-fin-roads",
                                      __version__)
        batches = [stations[i:i + ws.image_batch]
                   for i in range(0, len(stations), ws.image_batch)]
        for number, batch in enumerate(batches, 1):
            images, fetched = await self.thumbnails.refresh(batch, ws, headers)
            hashes = await self.attachments.upload(images, present, self.ds.token)
            changed = {sid: h for sid, h in hashes.items() if self.hashes.get(sid) != h}
            if changed:
                await self._upload_package({
                    f"digitraffic.weathercam.{st['id']}": weathercam.station_cot(
                        st, ws, name, attachment=changed[st["id"]])
                    for st in batch if st["id"] in changed})
                self.hashes.update(changed)
                uploaded = True
            removed = await self._remove_unused(present)
            logger.info("%s: images %d/%d: %d fetched, %d markers updated, %d files removed",
                        name, number, len(batches), fetched, len(changed), len(removed))
        return uploaded

    async def _remove_unused(self, present: Dict[str, str]) -> List[str]:
        """Remove thumbnails no marker points at any more."""
        in_use = set((self.hashes or {}).values())
        unused = [digest for digest in present if digest not in in_use]
        if not unused:
            return []
        removed = await self.attachments.remove(unused, self.ds.token,
                                                self.s.weathercam.delete_old_files)
        for digest in removed:
            present.pop(digest, None)
        return removed


async def run(s: Settings, server: Any, build_package: Any,
              health: Optional[Health] = None) -> None:
    """Sync all datasets in turn. A failing dataset waits RETRY_INTERVAL and
    does not stop the others."""
    syncs = [MissionSync(s, ds, server, build_package) for ds in s.datasets]
    next_run = {sync.ds.name: 0.0 for sync in syncs}
    loop = asyncio.get_running_loop()
    while True:
        for sync in syncs:
            if loop.time() < next_run[sync.ds.name]:
                continue
            try:
                uploaded = await sync.run_once()
                if health is not None:
                    health.ok()
                if uploaded:
                    await asyncio.sleep(30)  # ATAK DataSync needs time (upstream note)
                next_run[sync.ds.name] = loop.time() + (sync.ds.interval or s.update_interval)
            except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
                logger.warning("%s: %s; retrying in %.0f s", sync.ds.mission_name, exc,
                               s.retry_interval)
                if health is not None:
                    health.error()
                next_run[sync.ds.name] = loop.time() + s.retry_interval
        await asyncio.sleep(max(1.0, min(next_run.values()) - loop.time()))
