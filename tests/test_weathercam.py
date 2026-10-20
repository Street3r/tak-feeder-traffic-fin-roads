# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the weather camera dataset."""

import asyncio
import json
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from unittest import mock

from tak_feeder_traffic_fin_roads import digitraffic, feeder, weathercam


def station(sid, lon=24.0, lat=60.05, status="GATHERING", presets=None, **props):
    p = {"id": sid, "name": f"kt51_{sid}", "collectionStatus": status,
         "presets": presets if presets is not None else [
             {"id": f"{sid}01", "inCollection": True},
             {"id": f"{sid}02", "inCollection": True},
             {"id": f"{sid}09", "inCollection": False}]}
    p.update(props)
    return {"type": "Feature", "id": sid, "properties": p,
            "geometry": {"type": "Point", "coordinates": [lon, lat, 0.0]}}


def details(sid, road=51, part=14, dist=422):
    """Station details as /stations/<id> returns them."""
    return {"type": "Feature", "id": sid, "properties": {
        "id": sid,
        "roadAddress": {"roadNumber": road, "roadSection": part,
                        "distanceFromRoadSectionStart": dist,
                        "carriageway": "ONE_CARRIAGEWAY"},
        "presets": [
            {"id": f"{sid}01", "presentationName": "Inkooseen", "directionCode": "1",
             "direction": "INCREASING_DIRECTION"},
            {"id": f"{sid}02", "presentationName": "Hankoon", "directionCode": "2",
             "direction": "DECREASING_DIRECTION"},
            {"id": f"{sid}09", "presentationName": "Tienpinta", "directionCode": "9",
             "direction": "SPECIAL_DIRECTION"}]}}


def road_api(points_west=True, missing=()):
    """Fake Viitekehysmuunnin: the road runs due west (x falls as the
    distance grows) or due north."""
    calls = []

    def post(url, data, headers):
        queries = json.loads(data["json"])
        calls.append(queries)
        feats = []
        for q in queries:
            if (q["tunniste"], q["etaisyys"]) in missing:
                continue
            d = q["etaisyys"]
            x, y = (330000.0 - d, 6660000.0) if points_west else (330000.0, 6660000.0 + d)
            feats.append({"type": "Feature", "properties": {
                "tunniste": q["tunniste"], "x": x, "y": y, "tie": q["tie"]}})
        return json.dumps({"type": "FeatureCollection", "features": feats}).encode()
    return post, calls


def directions_for(*sids, **kw):
    post, calls = road_api(**kw)

    async def get_json(url):
        return details(url.rsplit("/", 1)[-1])
    d = weathercam.StationDirections(get_json=get_json, post=post, sleep=mock.AsyncMock())
    return d, calls


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
        self.assertEqual([p["id"] for p in st["presets"]], ["C101", "C102"])
        self.assertEqual(len(weathercam.extract_stations(fc, weathercam.WeathercamSettings())), 2)


class GeometryTest(unittest.TestCase):
    def test_tm35fin(self) -> None:
        # Helsinki centre, about 385 600 / 6 672 100
        x, y = weathercam.to_tm35fin(60.1699, 24.9384)
        self.assertAlmostEqual(x, 385611, delta=5)
        self.assertAlmostEqual(y, 6672118, delta=5)
        self.assertAlmostEqual(weathercam.to_tm35fin(65.0, 27.0)[0], 500000.0, places=3)

    def test_bearing(self) -> None:
        # on the central meridian grid north is true north
        self.assertAlmostEqual(weathercam.grid_bearing((0, 0), (0, 10), 65.0, 27.0), 0.0)
        self.assertAlmostEqual(weathercam.grid_bearing((0, 0), (-10, 0), 65.0, 27.0), 270.0)
        # west of it grid north points east of true north: convergence is negative
        self.assertLess(weathercam.grid_bearing((0, 0), (0, 10), 60.0, 24.0), 360.0)
        self.assertGreater(weathercam.grid_bearing((0, 0), (0, 10), 60.0, 24.0), 357.0)

    def test_offset(self) -> None:
        lat, lon = weathercam.offset(60.0, 25.0, 90.0, 20.0)
        self.assertAlmostEqual(lat, 60.0, places=6)
        self.assertAlmostEqual(lon, 25.00036, places=5)

    def test_direction_sign(self) -> None:
        self.assertEqual(weathercam.direction_sign({"direction": "INCREASING_DIRECTION"}), 1)
        self.assertEqual(weathercam.direction_sign({"direction": "DECREASING_DIRECTION"}), -1)
        self.assertEqual(weathercam.direction_sign({"directionCode": "2"}), -1)
        for d in ("CROSSING_ROAD_INCREASING_DIRECTION", "SPECIAL_DIRECTION", "UNKNOWN"):
            self.assertIsNone(weathercam.direction_sign({"direction": d, "directionCode": "3"}))


class DirectionsTest(unittest.TestCase):
    def test_update(self) -> None:
        d, calls = directions_for("C1")
        ws = weathercam.WeathercamSettings()
        st = weathercam.extract_stations({"features": [station("C1")]}, ws)[0]
        asyncio.run(d.update([st], ws, {}))
        self.assertAlmostEqual(d.bearing["C1"], 270.0, delta=3)
        (batch,) = calls
        self.assertEqual(batch[0], {"tie": 51, "osa": 14, "ajorata": 0, "etaisyys": 397,
                                    "palautusarvot": "1", "tunniste": "C1:a"})
        self.assertEqual(batch[1]["etaisyys"], 447)
        cams = {c["id"]: c for c in d.cameras(st)}
        self.assertAlmostEqual(cams["C101"]["azimuth"], 270.0, delta=3)
        self.assertAlmostEqual(cams["C102"]["azimuth"], 90.0, delta=3)
        self.assertEqual(cams["C101"]["presentationName"], "Inkooseen")
        # cached: nothing fetched again before the refresh time
        asyncio.run(d.update([st], ws, {}))
        self.assertEqual(len(calls), 1)

    def test_end_of_road_part(self) -> None:
        d, calls = directions_for("C1", points_west=False, missing={("C1:b", 447)})
        ws = weathercam.WeathercamSettings()
        st = weathercam.extract_stations({"features": [station("C1")]}, ws)[0]
        asyncio.run(d.update([st], ws, {}))
        self.assertEqual([q["etaisyys"] for q in calls[1]], [347, 397])
        self.assertAlmostEqual((d.bearing["C1"] + 180) % 360, 180.0, delta=3)  # about north

    def test_no_road_address(self) -> None:
        async def get_json(url):
            return {"properties": {"presets": [{"id": "C101", "direction":
                                                "INCREASING_DIRECTION"}]}}
        post, calls = road_api()
        d = weathercam.StationDirections(get_json=get_json, post=post, sleep=mock.AsyncMock())
        ws = weathercam.WeathercamSettings()
        st = weathercam.extract_stations({"features": [station("C1")]}, ws)[0]
        asyncio.run(d.update([st], ws, {}))
        self.assertIsNone(d.bearing["C1"])
        self.assertTrue(all(c["azimuth"] is None for c in d.cameras(st)))


class CotTest(unittest.TestCase):
    def files(self, ws=None):
        ws = ws or weathercam.WeathercamSettings(
            stream_url="rtmps://tak.example:1936/wc", stream_user="kelikamera",
            stream_password="s3cret")
        d, _ = directions_for("C1")
        fc = {"features": [station("C1")]}
        asyncio.run(d.update(weathercam.extract_stations(fc, ws), ws, {}))
        return weathercam.build_cot_files(fc, ws, "WeatherCamsFin", d)

    def test_marker_per_direction(self) -> None:
        files = self.files()
        self.assertEqual(sorted(files), ["digitraffic.weathercam.C101",
                                         "digitraffic.weathercam.C102"])
        ev = ET.fromstring(files["digitraffic.weathercam.C101"])
        self.assertEqual(ev.get("type"), "b-m-p-s-p-loc")
        self.assertEqual(ev.find("detail/marti/dest").get("mission"), "WeatherCamsFin")
        self.assertEqual(ev.find("detail/contact").get("callsign"), "CAM kt51_C1 Inkooseen")
        # moved 20 m west, the way the camera looks
        self.assertLess(float(ev.find("point").get("lon")), 24.0)
        self.assertAlmostEqual(float(ev.find("point").get("lat")), 60.05, places=4)
        sensor = ev.find("detail/sensor")
        self.assertAlmostEqual(float(sensor.get("azimuth")), 270, delta=3)
        self.assertEqual((sensor.get("fov"), sensor.get("range")), ("60", "250"))
        self.assertNotIn("hideFov", sensor.attrib)
        text = ev.find("detail/remarks").text
        self.assertIn("Weather camera kt51_C1, Inkooseen", text)
        self.assertIn("https://weathercam.digitraffic.fi/C101.jpg", text)
        self.assertIn("Source: Fintraffic / digitraffic.fi, CC BY 4.0", text)
        self.assertTrue(text.endswith("#weather #weathercam"))

    def test_video(self) -> None:
        ev = ET.fromstring(self.files()["digitraffic.weathercam.C102"])
        video = ev.find("detail/__video")
        self.assertEqual(video.get("url"),
                         "rtmps://tak.example:1936/wc/C102?user=kelikamera&pass=s3cret")
        entry = video.find("ConnectionEntry").attrib
        self.assertEqual((entry["protocol"], entry["address"], entry["port"], entry["path"]),
                         ("rtmps", "tak.example", "1936",
                          "/wc/C102?user=kelikamera&pass=s3cret"))
        self.assertEqual(entry["uid"], "digitraffic.weathercam.C102")

    def test_without_stream_or_direction(self) -> None:
        ws = weathercam.WeathercamSettings()
        files = weathercam.build_cot_files({"features": [station("C1")]}, ws, "M")
        ev = ET.fromstring(files["digitraffic.weathercam.C101"])
        self.assertIsNone(ev.find("detail/__video"))
        self.assertIsNone(ev.find("detail/sensor"))
        self.assertEqual(ev.find("point").get("lon"), "24.000000")
        self.assertIn("Direction of view not known", ev.find("detail/remarks").text)

    def test_signature_follows_video_and_cone(self) -> None:
        ev = ET.fromstring(self.files()["digitraffic.weathercam.C101"])
        base = feeder.cot_signature(ev)
        ev.find("detail/sensor").set("azimuth", "10")
        self.assertNotEqual(feeder.cot_signature(ev), base)
        ev = ET.fromstring(self.files()["digitraffic.weathercam.C101"])
        ev.find("detail/__video").set("url", "rtmps://other/wc/C101")
        self.assertNotEqual(feeder.cot_signature(ev), base)


class SettingsTest(unittest.TestCase):
    def test_env(self) -> None:
        env = {"DATASETS": "traffic-announcements,weathercams",
               "WEATHERCAMS_MISSION_GROUP": "WEATHERCAM",
               "WEATHERCAMS_BBOX": "19,59,32,71",
               "WEATHERCAMS_STREAM_URL": "rtmps://tak:1936/wc/",
               "WEATHERCAMS_STREAM_USER": "u", "WEATHERCAMS_STREAM_PASSWORD": "p",
               "WEATHERCAMS_FOV": "45", "WEATHERCAMS_OFFSET": "0"}
        with mock.patch.dict("os.environ", env, clear=True):
            s = feeder.Settings.from_env()
        cams = s.datasets[1]
        self.assertEqual((cams.name, cams.mission_name, cams.groups, cams.url, cams.interval),
                         ("weathercams", "WeatherCamsFin", ["WEATHERCAM"],
                          weathercam.STATIONS_URL, 600.0))
        self.assertEqual(s.datasets[0].interval, 0.0)
        ws = s.weathercam
        self.assertEqual(ws.bbox, (19.0, 59.0, 32.0, 71.0))
        self.assertEqual((ws.stream_url, ws.stream_user, ws.stream_password),
                         ("rtmps://tak:1936/wc", "u", "p"))
        self.assertEqual((ws.fov, ws.offset_m, ws.cot_type), (45.0, 0.0, "b-m-p-s-p-loc"))

    def test_old_image_settings_warn(self) -> None:
        with mock.patch.dict("os.environ", {"WEATHERCAMS_IMAGE_BBOX": "19,59,32,71"},
                             clear=True), self.assertLogs(weathercam.LOG, "WARNING"):
            weathercam.WeathercamSettings.from_env()

    def test_bad_bbox(self) -> None:
        with mock.patch.dict("os.environ", {"WEATHERCAMS_BBOX": "a,b,c,d"}, clear=True):
            self.assertIsNone(weathercam.WeathercamSettings.from_env().bbox)


class SyncTest(unittest.TestCase):
    def test_round(self) -> None:
        srv = mock.Mock()
        srv.mission.create_mission_subscription = mock.AsyncMock(
            return_value=(201, {"data": {"token": "t"}}))
        srv.mission.get_mission = mock.AsyncMock(return_value=(200, {"data": [{"uids": []}]}))
        srv.mission.add_mission_package = mock.AsyncMock(return_value=(200, {}))
        package = mock.Mock(return_value=b"zip")
        ds = feeder.Dataset("weathercams", weathercam.STATIONS_URL, "WeatherCamsFin",
                            ["WEATHERCAM"])
        ws = weathercam.WeathercamSettings(stream_url="rtmps://tak:1936/wc")
        sync = feeder.MissionSync(feeder.Settings(api_host="tak", weathercam=ws), ds, srv,
                                  package)
        post, _ = road_api()
        sync.directions._post = post  # pylint: disable=protected-access
        sync.directions._sleep = mock.AsyncMock()  # pylint: disable=protected-access
        urls = []

        async def fetch(url, headers, timeout=30):
            urls.append(urllib.parse.urlsplit(url).path)
            body = details("C1") if url.endswith("/C1") else {"features": [station("C1")]}
            return json.dumps(body).encode()
        with mock.patch.object(digitraffic, "fetch", fetch):
            self.assertTrue(asyncio.run(sync.run_once()))
        self.assertEqual(urls, ["/api/weathercam/v1/stations",
                                "/api/weathercam/v1/stations/C1"])
        files = package.call_args.kwargs["cot_files"]
        self.assertEqual(set(files), {"digitraffic.weathercam.C101",
                                      "digitraffic.weathercam.C102"})
        self.assertIsNotNone(ET.fromstring(files["digitraffic.weathercam.C101"])
                             .find("detail/sensor"))
        self.assertEqual(package.call_args.kwargs["groups"], ["WEATHERCAM"])


if __name__ == "__main__":
    unittest.main()
