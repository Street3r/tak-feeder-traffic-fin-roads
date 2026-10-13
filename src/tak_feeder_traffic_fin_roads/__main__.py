# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright Stefan Gofferje (tak-feeder-traffic-fin-roads)
# Copyright (C) 2026 Street3r (changes, see README)
"""
Finnish road traffic TAK feeder (Mission API only): python -m tak_feeder_traffic_fin_roads

Polls Digitraffic Simple JSON (GeoJSON) traffic messages and keeps one TAK
mission (DataSync) per dataset in sync. No CoT stream.
"""

from __future__ import annotations

import asyncio
import logging

from . import __version__
from .common import Health, env_bool, setup_logging
from .feeder import Settings, run as run_sync

LOG = logging.getLogger("tak_feeder_traffic_fin_roads")


async def main() -> None:
    """Read settings and run."""
    setup_logging(env_bool("DEBUG"))
    settings = Settings.from_env()
    if not settings.api_host:
        LOG.error("API_HOST is not set; cannot sync missions")
        return
    LOG.info("tak-feeder-traffic-fin-roads %s -> %s: %s", __version__, settings.api_host,
             ", ".join(f"{d.name} -> {d.mission_name} ({','.join(d.groups) or 'default'})"
                       for d in settings.datasets))
    # pylint: disable=import-outside-toplevel
    from python_takserver_api import Server, build_mission_package  # type: ignore

    server = Server(settings.api_host, settings.cert, settings.key)
    try:
        await run_sync(settings, server, build_mission_package, Health())
    finally:
        await server.close()


def run() -> None:
    """Console script entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
