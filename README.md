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
| `weathercams` | weather cameras, links to the latest images | `WeatherCamsFin` | `CAM <station>` |

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

### Weather cameras (draft)

`weathercams` puts one marker per Digitraffic weather camera station in its
own mission, so it can have its own group, for example
`WEATHERCAMS_MISSION_GROUP=WEATHERCAM` (a comma-separated list shows the
cameras to several groups without nested groups). The remarks list each camera
direction with a link to its latest image
(`https://weathercam.digitraffic.fi/<presetId>.jpg`); the link always
serves the newest picture (updated about every 10 minutes), so the TAK
server carries no images and ATAK loads one only when the user opens it.
The device needs internet access for that.

Only stations and directions in collection are sent. The station list is
fetched every `WEATHERCAMS_UPDATE_INTERVAL` seconds, and the mission changes
only when stations come or go. UID `digitraffic.weathercam.<stationId>`,
tags `#weather #weathercam`.

Markers use CoT type `b-m-p-s-p-loc` (sensor point), which ATAK draws with
its own sensor icon; Quick Pic (`b-i-x-i`) is left for users' own photos.
Stations inside `WEATHERCAMS_IMAGE_BBOX` also get the thumbnail of their
first camera as a mission file, `weathercam_<stationId>.jpg`, linked to the
marker with `<attachment_list hashes='["<sha256>"]'/>` so ATAK lists it
under the marker's attachments. Thumbnails are fetched with ETags, at most
one request per 1.1 s (Digitraffic allows 60 image requests a minute). A new
picture is uploaded to the server's file store, added to the mission and the
marker re-sent with its hash; the station's previous file is then removed
from the mission (and from the file store, `WEATHERCAMS_DELETE_OLD_FILES`).
Only files named `weathercam_*` are touched. An unchanged picture sends
nothing. Experimental: an earlier test embedded the picture in the CoT
(`<image>`), which ATAK then offered to publish back to the mission.

Tested on a TAK 5.8 test server: 812 stations, 2266 cameras. Not yet
tested: opening the links in the remarks.

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
| `WEATHERCAMS_THUMBNAILS` | `0` | `1` = link the small images (`?thumbnail=true`) |
| `WEATHERCAMS_IMAGE_BBOX` | none | Area `west,south,east,north` whose stations get their thumbnail as a mission file; empty = none (`WEATHERCAMS_EMBED_BBOX` also works) |
| `WEATHERCAMS_IMAGE_MAX` | `100` | Most stations with a thumbnail (`WEATHERCAMS_EMBED_MAX` also works) |
| `WEATHERCAMS_DELETE_OLD_FILES` | `1` | Also delete replaced thumbnails from the server's file store |
| `WEATHERCAMS_COT_TYPE` | `b-m-p-s-p-loc` | CoT type of the camera markers |
| `WEATHERCAMS_ICON` | none | Optional ATAK iconset path instead of the type's icon |
| `WEATHERCAMS_STALE_DAYS` | `365` | Stale time; removal from the mission hides a station |
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
The source line is in every item's remarks.

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
  - `weathercams` dataset: one sensor marker per Digitraffic weather camera
    station in its own mission; optional thumbnails as mission files linked
    to the marker, the previous file removed when a new one arrives.

- 2026-10-15
  - Draft: Digitraffic weather cameras as the `weathercams` dataset in their
    own mission and group.

## License

GNU General Public License v3.0 or later, see [LICENSE](LICENSE).
