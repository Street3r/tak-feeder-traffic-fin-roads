"""
Main entry point for the Finnish road traffic TAK feeder (Mission API only).

Polls Digitraffic GeoJSON Simple JSON for traffic announcements and
syncs to a TAK Server mission (DataSync). No COT stream / pytak needed;
uses REST put package only. stale = endTime, UID digitraffic.road.GUID.
"""

import asyncio
import logging
import os
from typing import Any

from dotenv import load_dotenv
from python_takserver_api import Server  # type: ignore[attr-defined]

import tak_feeder_traffic_fin_roads.feeder as feeder_mod


async def main() -> None:
    """Initialize and run the road traffic feeder (mission-only)."""
    load_dotenv()

    cert = os.getenv("CLIENT_CERT")
    if not cert or cert.lower() == "none":
        cert = ""
    key = os.getenv("CLIENT_KEY")
    if not key or cert.lower() == "none":
        key = ""

    if cert and not os.path.isabs(cert):
        cert = os.path.abspath(cert)
    if key and not os.path.isabs(key):
        key = os.path.abspath(key)

    update_interval = os.getenv("UPDATE_INTERVAL", "60")
    mission_name = os.getenv("MISSION_NAME", "TrafficFinRoads")
    mission_group = os.getenv("MISSION_GROUP", "")
    mission_token = os.getenv("MISSION_TOKEN", "")
    my_uid = os.getenv("MY_UID", "roads.0001")
    api_host = os.getenv("API_HOST", "")
    api_port = os.getenv("API_PORT", "8443")
    cot_type = os.getenv("COT_TYPE", "a-n-G")
    cot_how = os.getenv("COT_HOW", "h-e")
    iconset_path = os.getenv(
        "ICONSET_PATH", "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Transport/caution.png"
    )
    digitraffic_url = os.getenv(
        "DIGITRAFFIC_URL",
        "https://tie.digitraffic.fi/api/traffic-message/v2/traffic-announcements",
    )

    config_dict: dict[str, str] = {
        "DIGITRAFFIC_URL": digitraffic_url,
        "UPDATE_INTERVAL": update_interval,
        "MISSION_NAME": mission_name,
        "MISSION_GROUP": mission_group,
        "MISSION_TOKEN": mission_token,
        "MY_UID": my_uid,
        "API_HOST": api_host,
        "API_PORT": api_port,
        "COT_TYPE": cot_type,
        "COT_HOW": cot_how,
        "ICONSET_PATH": iconset_path,
        "VERSION": "0.1.0",
        "DEBUG": os.getenv("DEBUG", "0"),
    }

    if config_dict.get("DEBUG") == "1":
        logging.getLogger().setLevel(logging.DEBUG)

    if not api_host:
        print("API_HOST not set — cannot run mission sync. Exiting.")
        return

    takserver: Any = Server(api_host, cert, key)
    feeder_mod.takserver = takserver
    feeder_mod.token = mission_token

    if not mission_token:
        try:
            print("Trying to create subscription...")
            status, subscription = await takserver.mission.create_mission_subscription(
                mission_name, my_uid
            )
            if status == 201:
                tok = subscription["data"]["token"]
                role = subscription["data"]["role"]["type"]
                print(f"Subscription successful Role: {role} token: {tok}")
                feeder_mod.token = tok
                config_dict["MISSION_TOKEN"] = tok
            elif status == 404:
                print("Mission does not exist, creating...")
                status2, mssn = await takserver.mission.create_mission(
                    mission_name,
                    my_uid,
                    group=mission_group,
                    default_role="MISSION_READONLY_SUBSCRIBER",
                    classification="unclassified",
                )
                if status2 < 400:
                    tok = mssn["data"][0]["token"]
                    print(f"Mission created, token: {tok}")
                    feeder_mod.token = tok
                    config_dict["MISSION_TOKEN"] = tok
                else:
                    print(f"{status2} {mssn}")
                    print("Can neither subscribe to nor create mission. Exiting...")
                    await takserver.close()
                    return
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"Mission setup failed: {exc}")

    worker = feeder_mod.TrafficMissionWorker(config_dict)
    try:
        await worker.run()
    finally:
        await takserver.close()


if __name__ == "__main__":
    asyncio.run(main())
