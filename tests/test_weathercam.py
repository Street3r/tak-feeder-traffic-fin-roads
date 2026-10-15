# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the weather camera dataset."""

import asyncio
import json
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from tak_feeder_traffic_fin_roads import digitraffic, feeder, weathercam


def station(sid, lon=24.8, lat=60.2, status="GATHERING", presets=None, **props):
    p = {"id": sid, "name": f"Tie 1 {sid}", "collectionStatus": status,
         "presets": presets if presets is not None else [
             {"id": f"{sid}00", "inCollection": True},
             {"id": f"{sid}01", "inCollection": False}]}
    p.update(props)
    return {"type": "Feature", "id": sid, "properties": p,
            "geometry": {"type": "Point", "coordinates": [lon, lat, 0.0]}}


class ExtractTest(unittest.TestCase):
    def test_filters(self) -> None:
        fc = {"features": [
            station("C1"),
            station("C2", status="REMOVED_TEMPORARILY"),
            station("C3", presets=[{"id": "C300", "inCollection": False}]),
            station("C4", lon=27.0, lat=65.0),
        ]}
        ws = weathercam.WeathercamSettings(bbox=(19.0, 59.0, 26.0, 61.0))
        (st,) = weathercam.extract_stations(fc, ws)
        self.assertEqual(st["id"], "C1")
        self.assertEqual([p["id"] for p in st["presets"]], ["C100"])
        self.assertEqual(len(weathercam.extract_stations(fc, weathercam.WeathercamSettings())), 2)

    def test_cot(self) -> None:
        fc = {"features": [station("C1", presets=[
            {"id": "C100", "presentationName": "Helsinkiin"}, {"id": "C101"}])]}
        files = weathercam.build_cot_files(fc, weathercam.WeathercamSettings(), "WeatherCamsFin")
        ev = ET.fromstring(files["digitraffic.weathercam.C1"])
        self.assertEqual(ev.find("detail/marti/dest").get("mission"), "WeatherCamsFin")
        self.assertEqual(ev.find("detail/contact").get("callsign"), "CAM Tie 1 C1")
        self.assertEqual(ev.find("point").get("lat"), "60.200000")
        text = ev.find("detail/remarks").text
        self.assertIn("Helsinkiin: https://weathercam.digitraffic.fi/C100.jpg", text)
        self.assertIn("Camera 2: https://weathercam.digitraffic.fi/C101.jpg", text)
        self.assertIn("Source: Fintraffic / digitraffic.fi, CC BY 4.0", text)
        self.assertTrue(text.endswith("#weather #weathercam"))

    def test_thumbnails(self) -> None:
        ws = weathercam.WeathercamSettings(thumbnails=True)
        st = weathercam.extract_stations({"features": [station("C1")]}, ws)[0]
        self.assertIn("C100.jpg?thumbnail=true", weathercam.station_remarks(st, ws))


class SettingsTest(unittest.TestCase):
    def test_env(self) -> None:
        env = {"DATASETS": "traffic-announcements,weathercams",
               "WEATHERCAMS_MISSION_GROUP": "WEATHERCAM,WEATHER,TRAFFIC",
               "WEATHERCAMS_BBOX": "19,59,32,71", "WEATHERCAMS_THUMBNAILS": "1"}
        with mock.patch.dict("os.environ", env, clear=True):
            s = feeder.Settings.from_env()
        cams = s.datasets[1]
        self.assertEqual((cams.name, cams.mission_name, cams.groups, cams.url, cams.interval),
                         ("weathercams", "WeatherCamsFin", ["WEATHERCAM", "WEATHER", "TRAFFIC"],
                          weathercam.STATIONS_URL, 600.0))
        self.assertEqual(s.datasets[0].interval, 0.0)
        self.assertEqual(s.weathercam.bbox, (19.0, 59.0, 32.0, 71.0))
        self.assertTrue(s.weathercam.thumbnails)

    def test_bad_bbox(self) -> None:
        with mock.patch.dict("os.environ", {"WEATHERCAMS_BBOX": "a,b,c,d"}, clear=True):
            self.assertIsNone(weathercam.WeathercamSettings.from_env().bbox)


class SyncTest(unittest.TestCase):
    def test_upload(self) -> None:
        srv = mock.Mock()
        srv.mission.create_mission_subscription = mock.AsyncMock(
            return_value=(201, {"data": {"token": "t"}}))
        srv.mission.get_mission = mock.AsyncMock(return_value=(200, {"data": [{"uids": []}]}))
        srv.mission.add_mission_package = mock.AsyncMock(return_value=(200, {}))
        package = mock.Mock(return_value=b"zip")
        ds = feeder.Dataset("weathercams", weathercam.STATIONS_URL, "WeatherCamsFin",
                            ["WEATHERCAM", "WEATHER", "TRAFFIC"])
        sync = feeder.MissionSync(feeder.Settings(api_host="tak"), ds, srv, package)
        body = json.dumps({"features": [station("C1")]}).encode()
        with mock.patch.object(digitraffic, "fetch", mock.AsyncMock(return_value=body)):
            self.assertTrue(asyncio.run(sync.run_once()))
        self.assertEqual(set(package.call_args.kwargs["cot_files"]), {"digitraffic.weathercam.C1"})
        self.assertEqual(package.call_args.kwargs["groups"], ["WEATHERCAM", "WEATHER", "TRAFFIC"])


if __name__ == "__main__":
    unittest.main()
