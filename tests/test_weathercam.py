# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the weather camera dataset."""

import asyncio
import hashlib
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


class AttachmentTest(unittest.TestCase):
    def test_cot(self) -> None:
        fc = {"features": [station("C1"), station("C2")]}
        files = weathercam.build_cot_files(fc, weathercam.WeathercamSettings(), "M",
                                           {"C1": "ab12"})
        ev = ET.fromstring(files["digitraffic.weathercam.C1"])
        self.assertEqual(ev.get("type"), "b-m-p-s-p-loc")
        self.assertEqual(ev.find("detail/attachment_list").get("hashes"), '["ab12"]')
        self.assertIsNone(ev.find("detail/image"))
        other = ET.fromstring(files["digitraffic.weathercam.C2"])
        self.assertIsNone(other.find("detail/attachment_list"))

    def test_signature_follows_attachment(self) -> None:
        ws = weathercam.WeathercamSettings()
        st = weathercam.extract_stations({"features": [station("C1")]}, ws)[0]
        sig = [feeder.cot_signature(ET.fromstring(weathercam.station_cot(st, ws, "M", attachment=h)))
               for h in ("ab12", "ab12", "cd34")]
        self.assertEqual(sig[0], sig[1])
        self.assertNotEqual(sig[0], sig[2])

    def test_cache(self) -> None:
        calls = []

        def get(url, headers):
            calls.append((url, headers.get("If-None-Match")))
            return (304, "", b"") if headers.get("If-None-Match") else (200, '"e1"', b"ABC")

        fc = {"features": [station("C1"), station("C2", lon=27.0, lat=65.0)]}
        ws = weathercam.WeathercamSettings(image_bbox=(19.0, 59.0, 26.0, 61.0))
        cache = weathercam.ThumbnailCache(get=get, sleep=mock.AsyncMock())
        self.assertEqual(asyncio.run(cache.refresh(fc, ws, {})), {"C1": b"ABC"})
        self.assertEqual(asyncio.run(cache.refresh(fc, ws, {})), {"C1": b"ABC"})
        self.assertEqual(calls, [
            ("https://weathercam.digitraffic.fi/C100.jpg?thumbnail=true", None),
            ("https://weathercam.digitraffic.fi/C100.jpg?thumbnail=true", '"e1"')])
        # no image area: nothing fetched
        self.assertEqual(asyncio.run(cache.refresh(fc, weathercam.WeathercamSettings(), {})), {})

    def test_image_max(self) -> None:
        fc = {"features": [station(f"C{i}") for i in range(5)]}
        ws = weathercam.WeathercamSettings(image_bbox=(19.0, 59.0, 26.0, 61.0), image_max=2)
        self.assertEqual(len(weathercam.ThumbnailCache().selected(fc, ws)), 2)

    def test_upload_and_remove(self) -> None:
        new = hashlib.sha256(b"NEW").hexdigest()
        same = hashlib.sha256(b"SAME").hexdigest()
        server = mock.Mock(api_base_url="https://tak:8443")
        server.connection.request = mock.AsyncMock(return_value=(200, json.dumps({"Hash": new})))
        mission = {"contents": [
            {"data": {"name": "weathercam_C2.jpg", "hash": same}},
            {"data": {"name": "weathercam_C1.jpg", "hash": "old1"}},
            {"data": {"name": "user-photo.jpg", "hash": "user1"}}]}
        sync = weathercam.AttachmentSync(server, "Weather Cams", "roads.0001")
        hashes, old = asyncio.run(sync.upload({"C1": b"NEW", "C2": b"SAME"}, mission, "tok"))
        self.assertEqual(hashes, {"C1": new, "C2": same})
        self.assertEqual(old, ["old1"])  # files of others are left alone
        upload, add = server.connection.request.call_args_list
        self.assertEqual(upload.args[0], "post")
        self.assertEqual(upload.args[1], "https://tak:8443/Marti/sync/upload"
                         "?name=weathercam_C1.jpg&creatorUid=roads.0001")
        self.assertEqual(upload.kwargs["data"], b"NEW")
        self.assertEqual(add.args[:2], ("put", "https://tak:8443/Marti/api/missions/"
                                        "Weather%20Cams/contents?creatorUid=roads.0001"))
        self.assertEqual(add.kwargs["json"], {"hashes": [new]})
        self.assertEqual(add.kwargs["headers"]["Authorization"], "Bearer tok")

        server.connection.request.reset_mock()
        asyncio.run(sync.remove(["old1"], "tok", delete_files=True))
        remove, delete = server.connection.request.call_args_list
        self.assertEqual(remove.args, ("delete", "https://tak:8443/Marti/api/missions/"
                                       "Weather%20Cams/contents?hash=old1&creatorUid=roads.0001"))
        self.assertEqual(delete.args, ("delete", "https://tak:8443/Marti/sync/delete?Hash=old1"))

    def test_failed_upload_is_skipped(self) -> None:
        server = mock.Mock(api_base_url="https://tak:8443")
        server.connection.request = mock.AsyncMock(return_value=(500, "error"))
        sync = weathercam.AttachmentSync(server, "M", "roads.0001")
        hashes, old = asyncio.run(sync.upload({"C1": b"NEW"}, {}, "tok"))
        self.assertEqual((hashes, old), ({}, []))


class SettingsTest(unittest.TestCase):
    def test_env(self) -> None:
        env = {"DATASETS": "traffic-announcements,weathercams",
               "WEATHERCAMS_MISSION_GROUP": "WEATHERCAM",
               "WEATHERCAMS_BBOX": "19,59,32,71", "WEATHERCAMS_THUMBNAILS": "1",
               "WEATHERCAMS_EMBED_BBOX": "24.5,60.1,25.3,60.4", "WEATHERCAMS_IMAGE_MAX": "900"}
        with mock.patch.dict("os.environ", env, clear=True):
            s = feeder.Settings.from_env()
        cams = s.datasets[1]
        self.assertEqual((cams.name, cams.mission_name, cams.groups, cams.url, cams.interval),
                         ("weathercams", "WeatherCamsFin", ["WEATHERCAM"],
                          weathercam.STATIONS_URL, 600.0))
        self.assertEqual(s.datasets[0].interval, 0.0)
        self.assertEqual(s.weathercam.bbox, (19.0, 59.0, 32.0, 71.0))
        self.assertTrue(s.weathercam.thumbnails)
        # the old WEATHERCAMS_EMBED_BBOX name still works
        self.assertEqual(s.weathercam.image_bbox, (24.5, 60.1, 25.3, 60.4))
        self.assertEqual(s.weathercam.image_max, 900)
        self.assertTrue(s.weathercam.delete_old_files)
        self.assertEqual(s.weathercam.cot_type, "b-m-p-s-p-loc")

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
                            ["WEATHERCAM"])
        sync = feeder.MissionSync(feeder.Settings(api_host="tak"), ds, srv, package)
        body = json.dumps({"features": [station("C1")]}).encode()
        with mock.patch.object(digitraffic, "fetch", mock.AsyncMock(return_value=body)):
            self.assertTrue(asyncio.run(sync.run_once()))
        self.assertEqual(set(package.call_args.kwargs["cot_files"]), {"digitraffic.weathercam.C1"})
        self.assertEqual(package.call_args.kwargs["groups"], ["WEATHERCAM"])


if __name__ == "__main__":
    unittest.main()
