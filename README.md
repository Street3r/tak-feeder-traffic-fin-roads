# tak-feeder-traffic-fin-roads

> [!WARNING]
> **Work in progress. Not recommended for use.** This fork is being improved
> and has not been tested against a live TAK server yet. Things may change or
> break without notice.

TAK feeder for Finnish road traffic messages from Fintraffic's Digitraffic
Simple JSON (GeoJSON) API. Each dataset is kept in sync with its own TAK
server **mission** (DataSync), so items reach clients even after they were
offline and disappear when the message ends.

Fork of [sgofferj/tak-feeder-traffic-fin-roads](https://github.com/sgofferj/tak-feeder-traffic-fin-roads)
by Stefan Gofferje.

> Requires the ATAK DataSync plugin. Mission sync handles removal; ATAK does
> not stale out polygons reliably (upstream note).

## Datasets and missions

| `DATASETS` entry | Content | Default mission | Callsign |
|---|---|---|---|
| `traffic-announcements` (default) | accidents, closures, congestion, detours… | `TrafficFinRoads` | short type, e.g. `Accident`, `Road closed`, `Disruption` |
| `roadworks` | road works with work types and restrictions | `TrafficFinRoadworks` | `Roadwork` |
| `weight-restrictions` | weight limits (e.g. bridges) | `TrafficFinWeightRestrictions` | `Weight limit` |
| `exempted-transports` | oversize transports | `TrafficFinExemptedTransports` | `Exempted transport` |
| `weathercams` | weather cameras: one marker per camera direction with video and view cone | `WeatherCamsFin` | `CAM <station> <direction>` |

### Groups

Each mission can belong to several TAK groups, set with
`<DATASET>_MISSION_GROUP` (comma-separated; `MISSION_GROUP` for the traffic
announcements). Example for a TRAFFIC hierarchy without nested groups:

```
DATASETS=traffic-announcements,roadworks,weight-restrictions
MISSION_GROUP=TRAFFIC
ROADWORKS_MISSION_GROUP=CONSTRUCTION,TRAFFIC
WEIGHT_RESTRICTIONS_MISSION_GROUP=BRIDGES,TRAFFIC
```

A user who activates only `CONSTRUCTION` in Channels sees the road works; a
member of `TRAFFIC` sees all three missions. The groups apply when a mission
is created. For an existing mission change them in the TAK admin UI, or
delete the mission so the feeder recreates it.

### Weather cameras

`weathercams` puts one sensor marker (`b-m-p-s-p-loc`, camcorder icon) per
Digitraffic weather camera direction in its own mission, so it can have its
own group, for example `WEATHERCAMS_MISSION_GROUP=WEATHERCAM` (a
comma-separated list shows the cameras to several groups without nested
groups). Only stations and directions in collection are sent. UID
`digitraffic.weathercam.<presetId>`, tags `#weather #weathercam`.

- **Video:** with `WEATHERCAMS_STREAM_URL` (e.g. `rtmps://tak.example:1936/wc`)
  the marker opens `<url>/<presetId>?user=…&pass=…` in ATAK's video player.
  The stream server fetches the camera's latest image when someone opens it;
  [docker-tak-feeder-compose](https://github.com/Street3r/docker-tak-feeder-compose)
  runs MediaMTX for this. No images go through TAK. Everyone who sees the
  markers sees the stream account (`WEATHERCAMS_STREAM_USER/PASSWORD`).
- **View cone:** a camera's `direction` tells whether it looks towards
  growing road addresses (`INCREASING_DIRECTION`) or the other way
  (`DECREASING_DIRECTION`); checked on roads that start in Helsinki, where
  "Helsinkiin" cameras are decreasing. The road's bearing at the station
  comes from two points of the station's road address 50 m apart, converted
  to coordinates by the Finnish Transport Infrastructure Agency's
  [Viitekehysmuunnin](https://avoinapi.vaylapilvi.fi/viitekehysmuunnin/)
  (one batch request). The marker gets `<sensor azimuth fov range>` and is
  moved `WEATHERCAMS_OFFSET` metres that way, so a station's directions do
  not lie on top of each other. Cameras on a crossing road, road-surface
  cameras and unknown directions get no cone and stay at the station.
  Digitraffic's direction codes are not always right, so some cones may
  point the wrong way.
- Station details (camera names, directions, road address) are fetched once
  per station, one request every 0.2 s, and again after
  `WEATHERCAMS_DETAILS_REFRESH_HOURS`. The station list is read every
  `WEATHERCAMS_UPDATE_INTERVAL` seconds; the mission changes only when
  something a user sees changes.

Upgrading from the thumbnail version (markers per station, image files in
the mission): delete the `WeatherCamsFin` mission in the TAK admin UI before
starting this version, so the feeder recreates it without the old markers and
files. The old files stay in the server's file store until removed there.

## What it sends

- Points get an icon (caution, traffic jam, closure); accidents are a hazard
  symbol in red. Lines are split into an icon and one shape per line so ATAK
  draws them without a closing segment (upstream logic).
- UID `digitraffic.road.<situationId>` (as upstream); stale at the end time
  of the message.
- Remarks: title, location, road, details (feature names with quantities,
  road work types and restrictions), comment, validity, sender, source line,
  `#traffic #<dataset>` (+ `#accident`). The message texts are in Finnish as
  published by Fintraffic; labels and callsigns are in English.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PYTAK_TLS_CLIENT_CERT` / `PYTAK_TLS_CLIENT_KEY` | | Client certificate and unencrypted key (PEM). Old names `CLIENT_CERT` / `CLIENT_KEY` work |
| `API_HOST` | | TAK server for the Mission API (port 8443) |
| `DATASETS` | `traffic-announcements` | Datasets to sync, comma-separated |
| `MISSION_NAME` / `MISSION_GROUP` / `MISSION_TOKEN` | `TrafficFinRoads` | Traffic announcement mission |
| `<DATASET>_MISSION_NAME` / `_MISSION_GROUP` / `_MISSION_TOKEN` | see table | Other missions, e.g. `ROADWORKS_MISSION_GROUP` |
| `DIGITRAFFIC_URL` | traffic announcements URL | Override for the announcements endpoint |
| `DIGITRAFFIC_USER` | `tak-feeder-traffic-fin-roads` | `Digitraffic-User` header |
| `MY_UID` | `roads.0001` | Creator UID |
| `COT_TYPE` / `COT_TYPE_LINE` / `COT_HOW` | `a-n-G` / `u-d-f` / `h-e` | CoT types |
| `ICONSET_PATH` | caution icon | Default icon |
| `WEATHERCAMS_UPDATE_INTERVAL` | `600` | Seconds between weather camera syncs (`<DATASET>_UPDATE_INTERVAL` works for every dataset) |
| `WEATHERCAMS_BBOX` | all of Finland | Area `west,south,east,north`, e.g. `22,60,26,62` |
| `WEATHERCAMS_STREAM_URL` | none | Video stream base URL, the preset id is appended; empty = no video |
| `WEATHERCAMS_STREAM_USER` / `WEATHERCAMS_STREAM_PASSWORD` | none | Stream account, added as `?user=&pass=` |
| `WEATHERCAMS_FOV` / `WEATHERCAMS_RANGE` | `60` / `250` | View cone angle (degrees) and length (m) |
| `WEATHERCAMS_OFFSET` | `20` | Metres a marker is moved in its view direction; `0` = at the station |
| `WEATHERCAMS_DETAILS_REFRESH_HOURS` | `24` | How often station details and road bearings are fetched again |
| `WEATHERCAMS_COT_TYPE` | `b-m-p-s-p-loc` | CoT type of the camera markers |
| `WEATHERCAMS_ICON` | none | Optional ATAK iconset path instead of the type's icon |
| `WEATHERCAMS_STALE_DAYS` | `365` | Stale time; removal from the mission hides a camera |
| `TIMEZONE` | `Europe/Helsinki` | Time zone of the validity times in the remarks |
| `UPDATE_INTERVAL` | `60` | Seconds between syncs of one dataset |
| `RETRY_INTERVAL` | `300` | Wait after a failed sync |
| `HEALTH_MAX_ERRORS` | `3` | Failed syncs in a row before the container is unhealthy |
| `DEBUG` | `0` | Debug logging |

## Running

Image for amd64 and arm64: `ghcr.io/street3r/tak-feeder-traffic-fin-roads:latest`.

```yaml
services:
  feeder-traffic:
    image: ghcr.io/street3r/tak-feeder-traffic-fin-roads:latest
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./certs:/data:ro
```

The container runs as uid 10001; the key must be readable by it.

## Data

Fintraffic / digitraffic.fi, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
The source line is in every item's remarks. Road bearings of the weather
cameras: Väylävirasto, Viitekehysmuunnin (CC BY 4.0).

## Credits and changes

Original work © Stefan Gofferje,
[tak-feeder-traffic-fin-roads](https://github.com/sgofferj/tak-feeder-traffic-fin-roads),
used with his permission. Uses his
[python-takserver-api](https://github.com/sgofferj/python-takserver-api).

Changes in this fork (Street3r):

- 2026-10-13
  - Road works, weight restrictions and exempted transports as optional
    datasets, each in its own mission; several groups per mission.
  - `Digitraffic-User` header and gzip as Fintraffic asks.
  - English callsigns and remarks labels (`Accident`, `Road closed`,
    `Disruption`…); the generic label is used only for traffic
    announcements. Remarks with road, details, validity, source line and
    hashtags.
  - A failing dataset waits `RETRY_INTERVAL` without stopping the others;
    mission setup failures are retried instead of ending the feeder.
  - Old variable names still work; python-takserver-api pinned to a commit;
    health check; unprivileged container; tests and CI.
- 2026-10-15
  - Validity in local time (`TIMEZONE`), shorter within one day:
    `15.10. 20:00–23:30`, otherwise `15.10. 23:00 – 16.10. 05:00`.
  - Each round compares the items with what the mission already stores and
    uploads only new or changed ones, so an updated message (new text or
    times, same id) reaches ATAK. Unchanged items are not sent again.
  - Every event of a send round reaches TAK. pytak's send queue holds 100
    events and dropped the oldest when full, so rounds of more than 100
    lost the rest.
- 2026-10-16 (draft)
  - `weathercams` dataset: one Quick Pic marker per Digitraffic weather camera
    station in its own mission; optional thumbnails as mission files linked
    to the marker, the previous file removed when a new one arrives;
    thumbnails sent in batches of 50 stations as they are fetched, replaced
    ones removed 5 at a time.
- 2026-10-20
  - `weathercams`: one sensor marker per camera direction with an on-demand
    video stream (`WEATHERCAMS_STREAM_URL`) and a view cone from the road
    bearing; thumbnails and mission files removed.

- 2026-10-15
  - Draft: Digitraffic weather cameras as the `weathercams` dataset in their
    own mission and group.

## License

GNU General Public License v3.0 or later, see [LICENSE](LICENSE).
