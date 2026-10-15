# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the road traffic feeder."""

import asyncio
import json
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from tak_feeder_traffic_fin_roads import digitraffic, feeder


def feature(sid, situation, features=(), phases=(), geometry=None, ann_type=None):
    props = {
        "situationId": sid, "situationType": situation,
        "announcements": [{
            "title": "Tie 1, Espoo", "location": {"description": "Tie 1 välillä A - B."},
            "features": list(features), "roadWorkPhases": list(phases),
            "timeAndDuration": {"startTime": "2026-10-13T08:00:00Z"},
            "comment": "Varaudu viivytyksiin.", "sender": "Fintraffic Tieliikennekeskus",
        }],
    }
    if ann_type:
        props["trafficAnnouncementType"] = ann_type
    return {"type": "Feature", "properties": props,
            "geometry": geometry or {"type": "Point", "coordinates": [24.8, 60.2]}}


LINE = {"type": "LineString", "coordinates": [[24.8, 60.2], [24.9, 60.25], [25.0, 60.3]]}


class ExtractTest(unittest.TestCase):
    def test_accident(self) -> None:
        fc = {"features": [feature("GUID1", "TRAFFIC_ANNOUNCEMENT", [{"name": "Onnettomuus"}],
                                   ann_type="accident report")]}
        (c,) = digitraffic.extract_cot_candidates(fc)
        self.assertEqual(c["uid"], "digitraffic.road.GUID1")
        self.assertEqual(c["callsign"], "Accident")
        self.assertIn("Sender: Fintraffic Tieliikennekeskus", c["remarks"])
        self.assertIn("Source: Fintraffic / digitraffic.fi, CC BY 4.0", c["remarks"])
        self.assertTrue(c["remarks"].endswith("#traffic #announcement #accident"))
        # 08:00Z is 11:00 in Finland in summer time
        self.assertIn("Valid: 13.10. 11:00 – until further notice", c["remarks"])

    def test_valid_local_time(self) -> None:
        f = feature("GUID2", "TRAFFIC_ANNOUNCEMENT", [{"name": "Onnettomuus"}])
        f["properties"]["announcements"][0]["timeAndDuration"]["endTime"] = "2026-12-01T22:30:00Z"
        (c,) = digitraffic.extract_cot_candidates({"features": [f]})
        self.assertIn("Valid: 13.10. 11:00 – 02.12. 00:30", c["remarks"])  # winter time +2
        (c,) = digitraffic.extract_cot_candidates({"features": [f]}, tz="UTC")
        self.assertIn("Valid: 13.10. 08:00 – 01.12. 22:30", c["remarks"])

    def test_valid_same_day_is_short(self) -> None:
        f = feature("GUID3", "TRAFFIC_ANNOUNCEMENT", [{"name": "Onnettomuus"}])
        tad = f["properties"]["announcements"][0]["timeAndDuration"]
        tad["startTime"], tad["endTime"] = "2026-10-15T17:00:00Z", "2026-10-15T20:30:00Z"
        (c,) = digitraffic.extract_cot_candidates({"features": [f]})
        self.assertIn("Valid: 15.10. 20:00–23:30\n", c["remarks"])
        # over midnight local time: both dates
        tad["endTime"] = "2026-10-15T23:00:00Z"
        (c,) = digitraffic.extract_cot_candidates({"features": [f]})
        self.assertIn("Valid: 15.10. 20:00 – 16.10. 02:00\n", c["remarks"])

    def test_roadwork(self) -> None:
        phase = {"workTypes": [{"type": "BRIDGE", "description": "Siltatyö"}],
                 "restrictions": [{"type": "SPEED_LIMIT", "restriction": {
                     "name": "Nopeusrajoitus", "quantity": 50.0, "unit": "km/h"}}]}
        fc = {"features": [feature("GUID2", "ROAD_WORK", phases=[phase], geometry=LINE)]}
        (c,) = digitraffic.extract_cot_candidates(fc, "roadworks")
        self.assertEqual(c["callsign"], "Roadwork")
        self.assertEqual(c["announcementType"], "roadwork")  # orange in cot_utils
        self.assertIn("Details: Siltatyö, Nopeusrajoitus 50 km/h", c["remarks"])
        self.assertTrue(c["remarks"].endswith("#traffic #roadwork"))

    def test_weight_restriction(self) -> None:
        fc = {"features": [feature("GUID3", "WEIGHT_RESTRICTION",
                                   [{"name": "Painorajoitus", "quantity": 12, "unit": "t"}])]}
        (c,) = digitraffic.extract_cot_candidates(fc, "weight-restrictions")
        self.assertEqual(c["callsign"], "Weight limit")
        self.assertIn("Details: Painorajoitus 12 t", c["remarks"])


class CotFilesTest(unittest.TestCase):
    def test_split_and_labels(self) -> None:
        s = feeder.Settings()
        fc = {"features": [
            feature("A", "ROAD_WORK", geometry=LINE),
            feature("B", "TRAFFIC_ANNOUNCEMENT", [{"name": "Jotain muuta"}], ann_type="general"),
        ]}
        roadwork = feeder.build_cot_files(digitraffic.extract_cot_candidates(fc, "roadworks")[:1],
                                          s, "Roadworks")
        self.assertEqual(set(roadwork), {"digitraffic.road.A", "digitraffic.road.A.line"})
        icon = ET.fromstring(roadwork["digitraffic.road.A"])
        # the generic "Disruption" label is only for traffic announcements
        self.assertEqual(icon.find("detail/contact").get("callsign"), "Roadwork")
        self.assertEqual(icon.find("detail/marti/dest").get("mission"), "Roadworks")
        ann = feeder.build_cot_files(digitraffic.extract_cot_candidates(fc)[1:], s, "M")
        label = ET.fromstring(ann["digitraffic.road.B"]).find("detail/contact").get("callsign")
        self.assertEqual(label, "Disruption")


def server(status=200, uids=()):
    srv = mock.Mock()
    srv.mission.create_mission_subscription = mock.AsyncMock(
        return_value=(201, {"data": {"token": "t"}}))
    srv.mission.get_mission = mock.AsyncMock(
        return_value=(status, {"data": [{"uids": [{"data": u} for u in uids]}]}))
    srv.mission.create_mission = mock.AsyncMock(return_value=(201, {"data": [{"token": "t"}]}))
    srv.mission.remove_mission_content = mock.AsyncMock(return_value=(200, {}))
    srv.mission.add_mission_package = mock.AsyncMock(return_value=(200, {}))
    return srv


class SettingsTest(unittest.TestCase):
    def test_datasets_and_groups(self) -> None:
        env = {"DATASETS": "traffic-announcements,roadworks,weight-restrictions,bogus",
               "MISSION_GROUP": "TRAFFIC", "ROADWORKS_MISSION_GROUP": "CONSTRUCTION,TRAFFIC",
               "WEIGHT_RESTRICTIONS_MISSION_GROUP": "BRIDGES,TRAFFIC",
               "CLIENT_CERT": "/c/cert.pem"}
        with mock.patch.dict("os.environ", env, clear=True):
            s = feeder.Settings.from_env()
        self.assertEqual([(d.name, d.mission_name, d.groups) for d in s.datasets], [
            ("traffic-announcements", "TrafficFinRoads", ["TRAFFIC"]),
            ("roadworks", "TrafficFinRoadworks", ["CONSTRUCTION", "TRAFFIC"]),
            ("weight-restrictions", "TrafficFinWeightRestrictions", ["BRIDGES", "TRAFFIC"]),
        ])
        self.assertEqual(s.cert, "/c/cert.pem")  # old variable name

    def test_default_is_upstream(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            s = feeder.Settings.from_env()
        self.assertEqual([d.name for d in s.datasets], ["traffic-announcements"])


class SyncTest(unittest.TestCase):
    def test_new_mission_gets_all_groups(self) -> None:
        srv = server(status=404)
        ds = feeder.Dataset("roadworks", "u", "TrafficFinRoadworks", ["CONSTRUCTION", "TRAFFIC"])
        sync = feeder.MissionSync(feeder.Settings(api_host="tak"), ds, srv, mock.Mock())
        asyncio.run(sync.run_once())
        self.assertEqual(srv.mission.create_mission.call_args.kwargs["group"],
                         ["CONSTRUCTION", "TRAFFIC"])

    def test_upload(self) -> None:
        srv = server(uids=["digitraffic.road.OLD"])
        ds = feeder.Dataset("roadworks", "u", "R", ["CONSTRUCTION"])
        package = mock.Mock(return_value=b"zip")
        sync = feeder.MissionSync(feeder.Settings(api_host="tak"), ds, srv, package)
        fc = json.dumps({"features": [feature("A", "ROAD_WORK")]}).encode()
        with mock.patch.object(digitraffic, "fetch", mock.AsyncMock(return_value=fc)), \
                mock.patch.object(feeder.asyncio, "sleep", mock.AsyncMock()):
            self.assertTrue(asyncio.run(sync.run_once()))
        srv.mission.remove_mission_content.assert_awaited_once()
        self.assertEqual(package.call_args.kwargs["groups"], ["CONSTRUCTION"])

    def _sync_with_stored(self, stored_xml, features):
        srv = server(uids=["digitraffic.road.A", "digitraffic.road.B"])
        srv.mission.get_mission_cot = mock.AsyncMock(return_value=(200, stored_xml))
        package = mock.Mock(return_value=b"zip")
        sync = feeder.MissionSync(feeder.Settings(api_host="tak"),
                                  feeder.Dataset("traffic-announcements", "u", "R"), srv, package)
        fc = json.dumps({"features": features}).encode()
        with mock.patch.object(digitraffic, "fetch", mock.AsyncMock(return_value=fc)), \
                mock.patch.object(feeder.asyncio, "sleep", mock.AsyncMock()):
            uploaded = asyncio.run(sync.run_once())
        return uploaded, package, sync

    def _stored(self, features):
        """The CoT TAK would return for these features, with other times."""
        cands = digitraffic.extract_cot_candidates({"features": features})
        files = feeder.build_cot_files(cands, feeder.Settings(), "R")
        events = "".join(f.decode().replace('time="', 'time="1999') for f in files.values())
        return f'<?xml version="1.0" encoding="UTF-8"?><events>{events}</events>'

    def test_unchanged_items_not_sent(self) -> None:
        feats = [feature("A", "TRAFFIC_ANNOUNCEMENT"), feature("B", "TRAFFIC_ANNOUNCEMENT")]
        uploaded, package, _ = self._sync_with_stored(self._stored(feats), feats)
        self.assertFalse(uploaded)
        package.assert_not_called()

    def test_only_changed_items_sent(self) -> None:
        old = [feature("A", "TRAFFIC_ANNOUNCEMENT"), feature("B", "TRAFFIC_ANNOUNCEMENT")]
        new = [feature("A", "TRAFFIC_ANNOUNCEMENT"), feature("B", "TRAFFIC_ANNOUNCEMENT")]
        new[1]["properties"]["announcements"][0]["comment"] = "Tie avattu."
        uploaded, package, _ = self._sync_with_stored(self._stored(old), new)
        self.assertTrue(uploaded)
        self.assertEqual(set(package.call_args.kwargs["cot_files"]), {"digitraffic.road.B"})

    def test_unreadable_store_falls_back(self) -> None:
        feats = [feature("A", "TRAFFIC_ANNOUNCEMENT"), feature("B", "TRAFFIC_ANNOUNCEMENT")]
        uploaded, package, _ = self._sync_with_stored("not xml", feats)
        self.assertFalse(uploaded)  # same items as in the mission: nothing sent, as before

    def test_error_raises(self) -> None:
        ds = feeder.Dataset("roadworks", "u", "R")
        sync = feeder.MissionSync(feeder.Settings(), ds, server(status=500), mock.Mock())
        with self.assertRaises(RuntimeError):
            asyncio.run(sync.run_once())


if __name__ == "__main__":
    unittest.main()
