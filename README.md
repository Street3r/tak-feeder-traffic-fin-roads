# tak-feeder-traffic-fin-roads

TAK feeder for Finnish road traffic — Fintraffic Digitraffic GeoJSON Simple JSON format.

(C) 2026 Stefan Gofferje — Licensed under GPL-3.0-or-later.

Polls `https://tie.digitraffic.fi/api/traffic-message/v2/traffic-announcements` (`FeatureCollection` with embedded `Point`/`LineString`/`MultiLineString`) and publishes CoT to a TAK Server. Geometry is embedded inline (unlike DATEX II XML which needs separate `area-geometries` lookups), so GeoJSON is preferred.

## Configuration

All via environment variables / `.env`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `COT_URL` | `udp://239.2.3.1:6969` | TAK server COT URL (`ssl://host:8089` for TLS) |
| `CLIENT_CERT` |  | mTLS cert PEM path |
| `CLIENT_KEY` |  | mTLS key PEM path |
| `PYTAK_TLS_DONT_VERIFY` | `0` | Skip TLS verify |
| `DIGITRAFFIC_URL` | `https://tie.digitraffic.fi/api/traffic-message/v2/traffic-announcements` | Digitraffic GeoJSON endpoint |
| `UPDATE_INTERVAL` | `60` | Poll interval seconds |
| `COT_STALE` | `300` | CoT stale seconds |
| `COT_TYPE_POINT` | `a-f-G-E-V-C` | CoT type for Point incidents |
| `COT_TYPE_LINE` | `u-d-f` | CoT type for LineString/MultiLineString |
| `COT_HOW` | `h-e` | CoT how |
| `DEBUG` | `0` | Debug logging |

Other Digitraffic Simple JSON endpoints with identical shape: `/roadworks`, `/weight-restrictions`, `/exempted-transports` — set `DIGITRAFFIC_URL` accordingly.

## Certificates

Server-issued cert/key; remove password from key before use: `openssl rsa -in cert.key -out cert-nopw.key` (password typically `atakatak`).

## Usage

### Docker

```bash
docker run -d \
  --name tak-feeder-traffic-fin-roads \
  -v /path/to/certs:/data:ro \
  -e COT_URL=ssl://takserver:8089 \
  -e CLIENT_CERT=/data/cert.pem \
  -e CLIENT_KEY=/data/key.pem \
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
      - COT_URL=ssl://takserver:8089
      - CLIENT_CERT=/data/cert.pem
      - CLIENT_KEY=/data/key.pem
      - UPDATE_INTERVAL=60
```

## Development

```bash
poetry install
poetry run start
poetry run black . && poetry run mypy . && poetry run pylint src/
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
