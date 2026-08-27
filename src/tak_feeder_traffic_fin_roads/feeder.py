"""Polling worker for Digitraffic GeoJSON."""

import asyncio
from typing import Any, Dict, Optional

import aiohttp
import pytak

from . import cot_utils, digitraffic


class TrafficWorker(pytak.QueueWorker):  # type: ignore[misc]
    """Polls Digitraffic GeoJSON and enqueues CoT."""

    def __init__(self, queue: asyncio.Queue[bytes], config: Dict[str, Any]) -> None:
        super().__init__(queue, config)
        self.url: str = config.get(
            "DIGITRAFFIC_URL",
            digitraffic.DEFAULT_URL,
        )
        self.poll_interval: int = int(config.get("UPDATE_INTERVAL", "60"))
        self.stale_secs: int = int(config.get("COT_STALE", "300"))
        self.cot_type_point: str = config.get("COT_TYPE_POINT", "a-f-G-E-V-C")
        self.cot_type_line: str = config.get("COT_TYPE_LINE", "u-d-f")
        self.cot_how: str = config.get("COT_HOW", "h-e")

    async def handle_data(self, data: bytes) -> None:
        """Unused."""

    async def run(self, number_of_iterations: Optional[int] = None) -> None:
        """Poll loop."""
        self._logger.info(
            "TrafficWorker starting: url=%s interval=%d", self.url, self.poll_interval
        )

        async with aiohttp.ClientSession() as session:
            iterations = 0
            while number_of_iterations is None or iterations < number_of_iterations:
                if number_of_iterations is not None:
                    iterations += 1
                try:
                    self._logger.debug("Fetching Digitraffic GeoJSON...")
                    fc = await digitraffic.fetch_geojson(session, self.url)
                    candidates = digitraffic.extract_cot_candidates(fc)
                    self._logger.info(
                        "Fetched %d features, %d candidates",
                        len(fc.get("features", [])),
                        len(candidates),
                    )

                    for cand in candidates:
                        cot = cot_utils.generate_traffic_cot(
                            cand,
                            cot_type_point=self.cot_type_point,
                            cot_type_line=self.cot_type_line,
                            cot_how=self.cot_how,
                            stale_secs=self.stale_secs,
                        )
                        if cot:
                            await self.put_queue(cot)
                            self._logger.debug(
                                "Enqueued CoT %s (%s)",
                                cand.get("uid"),
                                cand.get("callsign"),
                            )

                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    ValueError,
                    TypeError,
                ) as exc:
                    self._logger.error("Poll error: %s", exc)

                await asyncio.sleep(self.poll_interval)


class TrafficReceiver(pytak.QueueWorker):  # type: ignore[misc]
    """Drains RX queue."""

    async def handle_data(self, data: bytes) -> None:
        text = data.decode(errors="ignore")
        if "takPing" not in text and text.strip():
            self._logger.debug("RX: %s", text[:500])

    async def run(self, number_of_iterations: Optional[int] = None) -> None:
        iterations = 0
        while number_of_iterations is None or iterations < number_of_iterations:
            if number_of_iterations is not None:
                iterations += 1
            data = await self.queue.get()
            await self.handle_data(data)
