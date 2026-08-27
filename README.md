# tak-feeder-traffic-fin-roads

TAK feeder for Finnish road traffic — Fintraffic Digitraffic GeoJSON Simple JSON format.

(C) 2026 Stefan Gofferje — Licensed under GPL-3.0-or-later.

Polls `https://tie.digitraffic.fi/api/traffic-message/v2/traffic-announcements` (`FeatureCollection` with embedded `Point`/`LineString`/`MultiLineString`) and publishes to a TAK Server **mission** (DataSync) for reliable offline delivery. Geometry is embedded inline (unlike DATEX II XML which needs separate `area-geometries` lookups), so GeoJSON is preferred.

> Requires the ATAK DataSync plugin — mission sync handles stale removal (clients delete when item disappears), so new items reach clients even after offline. ATAK does not stale out polygons reliably.

## Configuration

All via environment variables / `.env`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLIENT_CERT` |  | mTLS cert PEM path |
| `CLIENT_KEY` |  | mTLS key PEM path |
| `API_HOST` |  | TAK Server API host (for mission) |
| `API_PORT` | `8443` | TAK Server API port |
| `MISSION_NAME` | `TrafficFinRoads` | Mission name |
| `MISSION_GROUP` |  | Mission group |
| `MY_UID` | `roads.0001` | Feeder UID |
| `DIGITRAFFIC_URL` | `https://tie.digitraffic.fi/api/traffic-message/v2/traffic-announcements` | Digitraffic GeoJSON endpoint |
| `UPDATE_INTERVAL` | `60` | Poll interval seconds |
| `COT_TYPE` | `a-n-G` | Base CoT type for point |
| `COT_TYPE_LINE` | `u-d-f` | CoT type for polyline shape (try `u-d-f` so ATAK draws line; fallback split icon+shape if needed) |
| `COT_HOW` | `h-e` | CoT how |
| `ICONSET_PATH` | `6d781afb-89a6-4c07-b2b9-a89748b6a38f/Transport/caution.png` | ATAK usericon `iconsetpath` (warning triangle, default for unknown) |
| `DEBUG` | `0` | Debug logging |

Details: UID `digitraffic.road.<situationId>`, callsign = incident type (`features[].name` or `trafficAnnouncementType`), remarks = description (`location.description` + `comment`), stale = `timeAndDuration.endTime` (or start+24h if missing — mission diff drives deletion).

Other Digitraffic Simple JSON endpoints with identical shape: `/roadworks`, `/weight-restrictions`, `/exempted-transports` — not polled in this version.

## Certificates

Server-issued cert/key; remove password from key before use: `openssl rsa -in cert.key -out cert-nopw.key` (password typically `atakatak`).

## Usage

### Docker

```bash
docker run -d \
  --name tak-feeder-traffic-fin-roads \
  -v /path/to/certs:/data:ro \
  -e CLIENT_CERT=/data/cert.pem \
  -e CLIENT_KEY=/data/key.pem \
  -e API_HOST=takserver \
  -e API_PORT=8443 \
  -e MISSION_NAME=TrafficFinRoads \
  -e DIGITRAFFIC_URL=https://tie.digitraffic.fi/api/traffic-message/v2/traffic-announcements \
  ghcr.io/sgofferj/tak-feeder-traffic-fin-roads:latest
```

### Docker Compose

```yaml
services:
  traffic-fin-roads:
    image: ghcr.io/sgofferj/tak-feeder-traffic-fin-roads:latest
    restart: always
    volumes:
      - ./certs:/data:ro
    environment:
      - CLIENT_CERT=/data/cert.pem
      - CLIENT_KEY=/data/key.pem
      - API_HOST=takserver
      - API_PORT=8443
      - MISSION_NAME=TrafficFinRoads
      - UPDATE_INTERVAL=60
```

## Development

```bash
poetry install
poetry run start
poetry run black . && poetry run mypy . && poetry run pylint src/
```

## License

Licensed under the GNU General Public License v3.0 or later (GPLv3). See [LICENSE](LICENSE).
