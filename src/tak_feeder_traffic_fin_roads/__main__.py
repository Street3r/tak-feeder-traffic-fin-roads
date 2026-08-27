"""
Main entry point for the Finnish road traffic TAK feeder.
Polls Digitraffic GeoJSON Simple JSON format.
"""

import asyncio
import logging
import os
from configparser import ConfigParser

import pytak
from dotenv import load_dotenv

from .feeder import TrafficWorker, TrafficReceiver


async def main() -> None:
    """Initialize and run the road traffic feeder."""
    load_dotenv()

    cot_url = os.getenv("COT_URL")
    if not cot_url or cot_url.lower() == "none":
        cot_url = "udp://239.2.3.1:6969"

    cert = os.getenv("CLIENT_CERT")
    if not cert or cert.lower() == "none":
        cert = ""

    key = os.getenv("CLIENT_KEY")
    if not key or cert.lower() == "none":
        key = ""

    # Ensure absolute paths for certs when provided
    if cert and not os.path.isabs(cert):
        cert = os.path.abspath(cert)
    if key and not os.path.isabs(key):
        key = os.path.abspath(key)

    config_dict = {
        "COT_URL": cot_url,
        "PYTAK_TLS_CLIENT_CERT": cert,
        "PYTAK_TLS_CLIENT_KEY": key,
        "PYTAK_TLS_DONT_VERIFY": os.getenv("PYTAK_TLS_DONT_VERIFY", "0"),
        "DIGITRAFFIC_URL": os.getenv(
            "DIGITRAFFIC_URL",
            "https://tie.digitraffic.fi/api/traffic-message/v2/traffic-announcements",
        ),
        "UPDATE_INTERVAL": os.getenv("UPDATE_INTERVAL", "60"),
        "COT_STALE": os.getenv("COT_STALE", "300"),
        "COT_TYPE_POINT": os.getenv("COT_TYPE_POINT", "a-f-G-E-V-C"),
        "COT_TYPE_LINE": os.getenv("COT_TYPE_LINE", "u-d-f"),
        "COT_HOW": os.getenv("COT_HOW", "h-e"),
        "DEBUG": os.getenv("DEBUG", "0"),
    }

    config_parser = ConfigParser()
    config_parser["mycottool"] = config_dict
    config = config_parser["mycottool"]

    if config.get("DEBUG") == "1":
        logging.getLogger().setLevel(logging.DEBUG)

    clitool = pytak.CLITool(config)
    await clitool.setup()

    clitool.add_tasks(
        set(
            [
                TrafficWorker(clitool.tx_queue, config_dict),
                TrafficReceiver(clitool.rx_queue, config_dict),
            ]
        )
    )

    await clitool.run()


if __name__ == "__main__":
    asyncio.run(main())
