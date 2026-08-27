"""Mission-API feeder for Digitraffic GeoJSON (traffic announcements)."""

import asyncio
import os
from typing import Any, Dict, Optional

import aiohttp
import pytak
from python_takserver_api import build_mission_package  # type: ignore[attr-defined]

from . import cot_utils, digitraffic

# Module globals set by __main__.main()
takserver: Any = None
token: str = ""

HEALTH_FILE = os.getenv("HEALTH_FILE", "/tmp/tak-feeder-healthy")
HEALTH_MAX_ERRORS = int(os.getenv("HEALTH_MAX_ERRORS", "3"))


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
        # Each entry is {"data": "<uid>", ...}
        uid = cot.get("data") if isinstance(cot, dict) else None
        if uid:
            tmp.append(uid)
    return tmp


class TrafficMissionWorker(pytak.QueueWorker):  # type: ignore[misc]
    """Polls Digitraffic GeoJSON and syncs to TAK Mission."""

    def __init__(self, queue: asyncio.Queue[bytes], config: Dict[str, Any]) -> None:
        super().__init__(queue, config)
        self.url: str = config.get("DIGITRAFFIC_URL", digitraffic.DEFAULT_URL)
        self.poll_interval: int = int(config.get("UPDATE_INTERVAL", "60"))
        self.cot_type: str = config.get("COT_TYPE", "a-n-G")
        self.cot_how: str = config.get("COT_HOW", "h-e")
        self.iconset_path: str = config.get("ICONSET_PATH", "")
        self.mission_name: str = config.get("MISSION_NAME", "TrafficFinRoads")
        self.mission_group: str = config.get("MISSION_GROUP", "")
        self.my_uid: str = config.get("MY_UID", "roads.0001")
        self.api_host: str = config.get("API_HOST", "")
        self.api_port: int = int(config.get("API_PORT", "8443"))
        # token is module global, also allow override via config
        global token
        cfg_token = config.get("MISSION_TOKEN", "")
        if cfg_token:
            token = cfg_token

    async def handle_data(self, data: bytes) -> None:
        """No-op: CoTs delivered via mission package, not CoT stream."""

    async def run(
        self, number_of_iterations: Optional[int] = None
    ) -> None:  # noqa: C901
        """Mission sync loop."""
        self._logger.setLevel("DEBUG")  # type: ignore[attr-defined]
        global token
        consecutive_errors = 0
        iterations = 0

        while number_of_iterations is None or iterations < number_of_iterations:
            if number_of_iterations is not None:
                iterations += 1

            if takserver is None:
                self._logger.error("takserver not initialized, waiting 5s")
                await asyncio.sleep(5)
                continue

            self._logger.info(
                "Getting mission %s from TAK server...", self.mission_name
            )
            try:
                m_status, mission = await takserver.mission.get_mission(
                    self.mission_name
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._logger.error("get_mission failed: %s", exc)
                consecutive_errors += 1
                if consecutive_errors >= HEALTH_MAX_ERRORS:
                    _report_health(False)
                await asyncio.sleep(self.poll_interval)
                continue

            if m_status == 404:
                self._logger.info("Mission not found, creating %s", self.mission_name)
                try:
                    status, new_mission = await takserver.mission.create_mission(
                        self.mission_name,
                        self.my_uid,
                        group=self.mission_group,
                        default_role="MISSION_READONLY_SUBSCRIBER",
                        classification="unclassified",
                    )
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    self._logger.error("create_mission exception: %s", exc)
                    consecutive_errors += 1
                    if consecutive_errors >= HEALTH_MAX_ERRORS:
                        _report_health(False)
                    await asyncio.sleep(self.poll_interval)
                    continue

                if status < 400:
                    # new_mission["data"][0]["token"]
                    try:
                        token = new_mission["data"][0]["token"]  # type: ignore[index]
                    except (KeyError, IndexError, TypeError):
                        token = new_mission.get("data", {}).get("token", "")  # type: ignore[union-attr]
                    self._logger.info("Mission recreated, new token obtained.")
                    consecutive_errors = 0
                    _report_health(True)
                else:
                    self._logger.error(
                        "Failed to recreate mission: %s %s", status, new_mission
                    )
                    consecutive_errors += 1
                await asyncio.sleep(5)
                if consecutive_errors >= HEALTH_MAX_ERRORS:
                    _report_health(False)
                continue

            if m_status != 200:
                self._logger.error(
                    "Could not get mission %s: %s", self.mission_name, m_status
                )
                consecutive_errors += 1
                if consecutive_errors >= HEALTH_MAX_ERRORS:
                    _report_health(False)
                await asyncio.sleep(self.poll_interval)
                continue

            # mission found - fetch Digitraffic
            self._logger.info("Mission found, fetching Digitraffic GeoJSON...")
            try:
                async with aiohttp.ClientSession() as session:
                    fc = await digitraffic.fetch_geojson(session, self.url)
                candidates = digitraffic.extract_cot_candidates(fc)
                cap_uids = [c["uid"] for c in candidates]
                self._logger.info(
                    "Fetched %d features -> %d candidates",
                    len(fc.get("features", [])),
                    len(candidates),
                )
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                ValueError,
                TypeError,
            ) as exc:
                self._logger.error("Digitraffic fetch error: %s", exc)
                consecutive_errors += 1
                if consecutive_errors >= HEALTH_MAX_ERRORS:
                    _report_health(False)
                await asyncio.sleep(self.poll_interval)
                continue

            # Compare with mission UIDs
            try:
                mission_data = mission["data"][0]  # type: ignore[index]
                mission_uids = set(_get_uids_in_mission(mission_data.get("uids", [])))
            except (KeyError, IndexError, TypeError) as exc:
                self._logger.error("Failed to parse mission UIDs: %s", exc)
                mission_uids = set()

            stale_uids = mission_uids - set(cap_uids)
            for stale_uid in stale_uids:
                self._logger.info("Removing stale content: %s", stale_uid)
                try:
                    s, r = await takserver.mission.remove_mission_content(
                        self.mission_name, stale_uid, self.my_uid, token
                    )
                    if s != 200:
                        self._logger.error(
                            "Failed to remove %s: %s %s", stale_uid, s, r
                        )
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    self._logger.error(
                        "remove_mission_content exception for %s: %s", stale_uid, exc
                    )
                await asyncio.sleep(3)

            if set(cap_uids) == mission_uids:
                self._logger.info("No changes detected, skipping package upload.")
                consecutive_errors = 0
                _report_health(True)
                await asyncio.sleep(30)
                await asyncio.sleep(self.poll_interval)
                continue

            # Changes detected - build mission package
            self._logger.info(
                "Changes detected, building mission package with %d CoTs...",
                len(candidates),
            )
            cot_files: Dict[str, bytes | str] = {}
            for cand in candidates:
                cot_data = cot_utils.generate_traffic_cot(
                    cand,
                    cot_type=self.cot_type,
                    cot_how=self.cot_how,
                    mission_name=self.mission_name,
                    iconset_path=self.iconset_path or None,
                )
                if cot_data:
                    cot_files[cand["uid"]] = cot_data

            if not cot_files:
                self._logger.info("No CoT files to upload (all filtered).")
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
            self._logger.info(
                "Uploading mission package with %d CoTs...", len(cot_files)
            )
            try:
                status, result = await takserver.mission.add_mission_package(
                    self.mission_name, self.my_uid, token, pkg
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._logger.error("add_mission_package exception: %s", exc)
                consecutive_errors += 1
                if consecutive_errors >= HEALTH_MAX_ERRORS:
                    _report_health(False)
                await asyncio.sleep(self.poll_interval)
                continue

            if status != 200:
                self._logger.error(
                    "Mission package upload failed: %s %s", status, result
                )
                consecutive_errors += 1
            else:
                self._logger.info("Mission package added successfully")
                consecutive_errors = 0
                _report_health(True)

            self._logger.info("Update done.")
            await asyncio.sleep(30)
            await asyncio.sleep(self.poll_interval)

            if consecutive_errors >= HEALTH_MAX_ERRORS:
                _report_health(False)


class KeepAliveWorker(pytak.QueueWorker):  # type: ignore[misc]
    """Sends periodic keepalive CoT to keep TAK stream alive."""

    def __init__(self, queue: asyncio.Queue[bytes], config: Dict[str, Any]) -> None:
        super().__init__(queue, config)
        self.my_uid: str = config.get("MY_UID", "roads.0001")
        self.version: str = config.get("VERSION", "0.1.0")

    async def handle_data(self, data: bytes) -> None:
        event = data
        await self.put_queue(event)

    async def run(self, number_of_iterations: Optional[int] = None) -> None:
        iterations = 0
        while number_of_iterations is None or iterations < number_of_iterations:
            if number_of_iterations is not None:
                iterations += 1
            data = cot_utils.keep_alive(self.my_uid, version=self.version)
            await self.handle_data(data)
            await asyncio.sleep(30)


class TrafficReceiver(pytak.QueueWorker):  # type: ignore[misc]
    """Drains RX queue."""

    async def handle_data(self, data: bytes) -> None:
        text = data.decode(errors="ignore")
        if "t-x" in text:
            self._logger.info("Received:\n%s\n", text)  # type: ignore[attr-defined]

    async def run(self, number_of_iterations: Optional[int] = None) -> None:
        iterations = 0
        while number_of_iterations is None or iterations < number_of_iterations:
            if number_of_iterations is not None:
                iterations += 1
            data = await self.queue.get()
            await self.handle_data(data)
