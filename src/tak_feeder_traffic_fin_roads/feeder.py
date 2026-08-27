"""Mission-API feeder for Digitraffic GeoJSON (traffic announcements only)."""

import asyncio
import logging
import os
from typing import Any, Dict

import aiohttp
from python_takserver_api import build_mission_package  # type: ignore[attr-defined]

from . import cot_utils, digitraffic

# Module globals set by __main__.main()
takserver: Any = None
token: str = ""

HEALTH_FILE = os.getenv("HEALTH_FILE", "/tmp/tak-feeder-healthy")
HEALTH_MAX_ERRORS = int(os.getenv("HEALTH_MAX_ERRORS", "3"))

logger = logging.getLogger(__name__)


def _report_health(ok: bool) -> None:
    """Write health status file. No file = healthy."""
    if ok:
        try:
            os.unlink(HEALTH_FILE)
        except FileNotFoundError:
            pass
    else:
        with open(HEALTH_FILE, "w", encoding="utf-8") as f:
            f.write("unhealthy")


def _get_uids_in_mission(cots: Any) -> list[str]:
    """Extract UIDs from mission uids list (same as weather feeder util)."""
    tmp: list[str] = []
    for cot in cots:
        uid = cot.get("data") if isinstance(cot, dict) else None
        if uid:
            tmp.append(uid)
    return tmp


class TrafficMissionWorker:
    """Polls Digitraffic GeoJSON and syncs to TAK Mission (no COT stream)."""

    def __init__(self, config: Dict[str, str]) -> None:
        self.url: str = config.get("DIGITRAFFIC_URL", digitraffic.DEFAULT_URL)
        self.poll_interval: int = int(config.get("UPDATE_INTERVAL", "60"))
        self.cot_type: str = config.get("COT_TYPE", "a-n-G")
        self.cot_type_line: str = config.get("COT_TYPE_LINE", "u-d-f")
        self.cot_how: str = config.get("COT_HOW", "h-e")
        self.iconset_path: str = config.get("ICONSET_PATH", "")
        self.mission_name: str = config.get("MISSION_NAME", "TrafficFinRoads")
        self.mission_group: str = config.get("MISSION_GROUP", "")
        self.my_uid: str = config.get("MY_UID", "roads.0001")
        self.api_host: str = config.get("API_HOST", "")
        self.api_port: int = int(config.get("API_PORT", "8443"))
        global token
        cfg_token = config.get("MISSION_TOKEN", "")
        if cfg_token:
            token = cfg_token

    async def run(self) -> None:
        """Mission sync loop (no iteration limit)."""
        global token
        consecutive_errors = 0

        while True:
            if takserver is None:
                logger.error("takserver not initialized, waiting 5s")
                await asyncio.sleep(5)
                continue

            logger.info("Getting mission %s from TAK server...", self.mission_name)
            try:
                m_status, mission = await takserver.mission.get_mission(
                    self.mission_name
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.error("get_mission failed: %s", exc)
                consecutive_errors += 1
                if consecutive_errors >= HEALTH_MAX_ERRORS:
                    _report_health(False)
                await asyncio.sleep(self.poll_interval)
                continue

            if m_status == 404:
                logger.info("Mission not found, creating %s", self.mission_name)
                try:
                    status, new_mission = await takserver.mission.create_mission(
                        self.mission_name,
                        self.my_uid,
                        group=self.mission_group,
                        default_role="MISSION_READONLY_SUBSCRIBER",
                        classification="unclassified",
                    )
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.error("create_mission exception: %s", exc)
                    consecutive_errors += 1
                    if consecutive_errors >= HEALTH_MAX_ERRORS:
                        _report_health(False)
                    await asyncio.sleep(self.poll_interval)
                    continue

                if status < 400:
                    try:
                        token = new_mission["data"][0]["token"]  # type: ignore[index]
                    except (KeyError, IndexError, TypeError):
                        token = new_mission.get("data", {}).get("token", "")  # type: ignore[union-attr]
                    logger.info("Mission recreated, new token obtained.")
                    consecutive_errors = 0
                    _report_health(True)
                else:
                    logger.error(
                        "Failed to recreate mission: %s %s", status, new_mission
                    )
                    consecutive_errors += 1
                await asyncio.sleep(5)
                if consecutive_errors >= HEALTH_MAX_ERRORS:
                    _report_health(False)
                continue

            if m_status != 200:
                logger.error(
                    "Could not get mission %s: %s", self.mission_name, m_status
                )
                consecutive_errors += 1
                if consecutive_errors >= HEALTH_MAX_ERRORS:
                    _report_health(False)
                await asyncio.sleep(self.poll_interval)
                continue

            logger.info("Mission found, fetching Digitraffic GeoJSON...")
            try:
                async with aiohttp.ClientSession() as session:
                    fc = await digitraffic.fetch_geojson(session, self.url)
                candidates = digitraffic.extract_cot_candidates(fc)
                # For polyline non-accidents we split into icon + per-line shape(s)
                cap_uids: list[str] = []
                for c in candidates:
                    cap_uids.append(c["uid"])
                    gtype = c.get("geometry", {}).get("type", "Point")
                    is_acc = cot_utils._is_accident(  # type: ignore[attr-defined]
                        c.get("announcementType", ""), c.get("featuresStr", "")
                    )
                    if gtype in ("LineString", "MultiLineString") and not is_acc:
                        coords = c.get("geometry", {}).get("coordinates", [])
                        if gtype == "MultiLineString":
                            for idx in range(len(coords)):  # type: ignore[arg-type]
                                cap_uids.append(f"{c['uid']}.line.{idx}")
                        else:
                            cap_uids.append(f"{c['uid']}.line")
                logger.info(
                    "Fetched %d features -> %d candidates (%d CoTs with split)",
                    len(fc.get("features", [])),
                    len(candidates),
                    len(cap_uids),
                )
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                ValueError,
                TypeError,
            ) as exc:
                logger.error("Digitraffic fetch error: %s", exc)
                consecutive_errors += 1
                if consecutive_errors >= HEALTH_MAX_ERRORS:
                    _report_health(False)
                await asyncio.sleep(self.poll_interval)
                continue

            try:
                mission_data = mission["data"][0]  # type: ignore[index]
                mission_uids = set(_get_uids_in_mission(mission_data.get("uids", [])))
            except (KeyError, IndexError, TypeError) as exc:
                logger.error("Failed to parse mission UIDs: %s", exc)
                mission_uids = set()

            stale_uids = mission_uids - set(cap_uids)
            for stale_uid in stale_uids:
                logger.info("Removing stale content: %s", stale_uid)
                try:
                    s, r = await takserver.mission.remove_mission_content(
                        self.mission_name, stale_uid, self.my_uid, token
                    )
                    if s != 200:
                        logger.error("Failed to remove %s: %s %s", stale_uid, s, r)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.error(
                        "remove_mission_content exception for %s: %s", stale_uid, exc
                    )
                await asyncio.sleep(3)

            if set(cap_uids) == mission_uids:
                logger.info("No changes detected, skipping package upload.")
                consecutive_errors = 0
                _report_health(True)
                await asyncio.sleep(30)
                await asyncio.sleep(self.poll_interval)
                continue

            logger.info(
                "Changes detected, building mission package with %d CoTs...",
                len(cap_uids),
            )
            cot_files: Dict[str, bytes | str] = {}
            for cand in candidates:
                gtype = cand.get("geometry", {}).get("type", "Point")
                is_acc = cot_utils._is_accident(  # type: ignore[attr-defined]
                    cand.get("announcementType", ""), cand.get("featuresStr", "")
                )
                is_poly = gtype in ("LineString", "MultiLineString")
                if is_poly and not is_acc:
                    # Split into icon (a-n-G) + per-line shape(s) (u-d-f) so ATAK shows both without closing line
                    icon_cot = cot_utils.generate_traffic_cot(
                        cand,
                        cot_type=self.cot_type,
                        cot_how=self.cot_how,
                        mission_name=self.mission_name,
                        iconset_path=self.iconset_path or None,
                        cot_type_line=self.cot_type_line,
                    )
                    if icon_cot:
                        cot_files[cand["uid"]] = icon_cot
                    argb = cot_utils._color_for_announcement(  # type: ignore[attr-defined]
                        cand.get("announcementType", ""), cand.get("featuresStr", "")
                    )
                    gtype2 = cand.get("geometry", {}).get("type", "Point")
                    coords2 = cand.get("geometry", {}).get("coordinates", [])
                    if gtype2 == "LineString":
                        shape_cot = cot_utils._generate_shape_cot_single(  # type: ignore[attr-defined]
                            cand,
                            line_coords=coords2,  # type: ignore[arg-type]
                            line_index=None,
                            cot_type_line=self.cot_type_line,
                            cot_how=self.cot_how,
                            mission_name=self.mission_name,
                            argb=argb,
                            centroid=cand.get("centroid"),
                        )
                        if shape_cot:
                            cot_files[f"{cand['uid']}.line"] = shape_cot
                    elif gtype2 == "MultiLineString":
                        for idx, line in enumerate(coords2):  # type: ignore[union-attr]
                            # Decimate per line inside helper, but we pass single line
                            shape_cot = cot_utils._generate_shape_cot_single(  # type: ignore[attr-defined]
                                cand,
                                line_coords=line,  # type: ignore[arg-type]
                                line_index=idx,
                                cot_type_line=self.cot_type_line,
                                cot_how=self.cot_how,
                                mission_name=self.mission_name,
                                argb=argb,
                                centroid=cand.get("centroid"),
                            )
                            if shape_cot:
                                cot_files[f"{cand['uid']}.line.{idx}"] = shape_cot
                else:
                    # Point or accident polyline (single CoT with icon+shape)
                    cot_data = cot_utils.generate_traffic_cot(
                        cand,
                        cot_type=self.cot_type,
                        cot_how=self.cot_how,
                        mission_name=self.mission_name,
                        iconset_path=self.iconset_path or None,
                        cot_type_line=self.cot_type_line,
                    )
                    if cot_data:
                        cot_files[cand["uid"]] = cot_data

            if not cot_files:
                logger.info("No CoT files to upload (all filtered).")
                consecutive_errors = 0
                _report_health(True)
                await asyncio.sleep(30)
                await asyncio.sleep(self.poll_interval)
                continue

            mission_server = f"{self.api_host}:{self.api_port}:ssl"
            pkg = build_mission_package(
                name=self.mission_name,
                mission_name=self.mission_name,
                mission_server=mission_server,
                creator_uid=self.my_uid,
                cot_files=cot_files,
            )
            logger.info("Uploading mission package with %d CoTs...", len(cot_files))
            try:
                status, result = await takserver.mission.add_mission_package(
                    self.mission_name, self.my_uid, token, pkg
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.error("add_mission_package exception: %s", exc)
                consecutive_errors += 1
                if consecutive_errors >= HEALTH_MAX_ERRORS:
                    _report_health(False)
                await asyncio.sleep(self.poll_interval)
                continue

            if status != 200:
                logger.error("Mission package upload failed: %s %s", status, result)
                consecutive_errors += 1
            else:
                logger.info("Mission package added successfully")
                consecutive_errors = 0
                _report_health(True)

            logger.info("Update done.")
            await asyncio.sleep(30)
            await asyncio.sleep(self.poll_interval)

            if consecutive_errors >= HEALTH_MAX_ERRORS:
                _report_health(False)
