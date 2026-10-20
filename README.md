# GoPro Sync

Automatic offload for GoPro cameras. Plug a camera in, or simply switch it on near
the server, and every new photo and video is copied to your NAS. The app verifies each
file on the destination and can then free up the SD card.

- **USB**: the camera shows up as a USB network device; any number of cameras in parallel.
- **Wireless**: the server notices a paired camera over Bluetooth, turns on its Wi-Fi,
  joins it and downloads the media. One camera per Wi-Fi radio at a time.
- **Destinations**: SMB shares (Synology, QNAP, TrueNAS, Windows, Samba) or a local folder.
  Hosts and shares on the network can be discovered from the UI.
- **Web UI** in English and Romanian, with an admin password, live progress per camera,
  history and logs.

Tested with a HERO9 Black (Open GoPro API 2.0). Other cameras that speak the
[Open GoPro](https://gopro.github.io/OpenGoPro/) API (HERO9 and newer) should work.

## Requirements

- A Linux host for wireless offload (a Raspberry Pi 4/5 is ideal) with Docker and BlueZ
  (`sudo apt install bluez`). USB offload works on any Docker host that sees USB devices.
- **For wireless offload, a Wi-Fi radio that is not the host's own network connection.**
  Joining a camera's access point takes the radio off your LAN. Connect the host over
  Ethernet and use the built-in Wi-Fi for cameras, or add a USB Wi-Fi adapter. The service
  refuses to use a radio that carries the default route.
- Speeds are limited by the camera: roughly 10-15 MB/s over Wi-Fi and USB alike on a HERO9.

## Install

```bash
mkdir gopro-sync && cd gopro-sync
curl -O https://raw.githubusercontent.com/iacob28/GoPro_Sync/main/docker-compose.yml
docker compose up -d
```

Open `http://<host>:8780`, create the admin password, pick the destination and pair your
cameras. Settings and state live in `./data` (override with `GOPRO_SYNC_DATA`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOPRO_SYNC_PORT` | `8780` | Web UI port |
| `GOPRO_SYNC_PASSWORD` | | Preset the admin password (otherwise set it in the browser) |
| `GOPRO_SYNC_DATA` | `./data` | Host folder for settings, state and staging |
| `GOPRO_SYNC_IMAGE` / `GOPRO_SYNC_TAG` | registry image / `latest` | Image to run |
| `TZ` | `UTC` | Time zone for logs |

### Behind Traefik

`docker-compose.traefik.yml` adds router labels for a Traefik instance on the same host.
Because the app uses host networking, Traefik reaches it through the gateway of its Docker
network:

```bash
GOPRO_SYNC_DOMAIN=gopro.example.lan TRAEFIK_UPSTREAM=http://172.19.0.1:8780 \
TRAEFIK_CERTRESOLVER=myresolver \
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d
```

Traefik only routes healthy containers, so the route appears once the health check passes.

### Pairing a camera

On the camera open **Preferences > Connections > Connect Device > GoPro Quik App**, then
click **Find cameras** in the UI and **Pair**. From then on, switching the camera on near
the server starts a sync. After a successful sync the camera is put to standby and is not
synced again for the configured pause.

### Deleting from the card

Off by default. When enabled, files are deleted from the camera only after every file of
the run exists on the destination with the exact same size.

## How files are organised

The folder layout is a template, `{year}/{date}` by default, which gives
`2026/2026-09-26/GX010474.MP4`. Available fields: `{year} {month} {day} {date} {camera}`.
Dates come from the camera's clock. If a different file with the same name already exists
(cameras reuse numbers after a card format), the new one is saved as `GX010474_<timestamp>.MP4`.
Files already present with the same size are not uploaded twice, so several servers can
share one destination.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest
GOPRO_SYNC_DATA=./.dev-data .venv/bin/python -m gopro_sync   # http://localhost:8780
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
scripts/release.sh   # multi-arch build and push (IMAGE=... to override)
```

On macOS the app runs natively for USB offload and UI work; Docker Desktop cannot reach
USB network devices or Bluetooth, so the container is for Linux hosts.

## Licenses

Bundled assets: [Geist](https://vercel.com/font) (SIL Open Font License) and
[Phosphor Icons](https://phosphoricons.com) (MIT), see `gopro_sync/static/vendor`.
GoPro is a trademark of GoPro, Inc.; this project is not affiliated with GoPro.
