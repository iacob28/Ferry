# Ferry

Automatic offload for the devices that fill up with media: cameras, drones, 3D printers.
Plug a device in, or simply switch it on near the server, and every new photo and video is
copied to your NAS. The app verifies each file on the destination and can then free up the
device's storage.

Supported sources:

- **GoPro** cameras over USB or Bluetooth + Wi-Fi.
- **Drones and cameras that connect as USB storage**: DJI (Neo and others), Insta360, any
  camera with a `DCIM` folder, and SD card readers.

- **Bambu Lab 3D printers** (P1S, P1P, X1, A1): print timelapses from the SD card.

Destinations: SMB shares (Synology, QNAP, TrueNAS, Windows, Samba), local folders, S3-compatible
object storage (Amazon S3, Cloudflare R2, Backblaze B2, MinIO, Wasabi) and Google Drive. Ferry was
called GoPro Sync until 0.2.0.

- **USB**: the camera shows up as a USB network device; any number of cameras in parallel.
- **Wireless**: the server notices a paired camera over Bluetooth, turns on its Wi-Fi,
  joins it and downloads the media. One camera per Wi-Fi radio at a time.
- **Destinations**: SMB shares, local folders, S3 / R2 and Google Drive, set up once and shared
  by every device. Hosts and shares on the network can be discovered from the UI.
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
mkdir ferry && cd ferry
curl -O https://raw.githubusercontent.com/iacob28/GoPro_Sync/main/docker-compose.yml
docker compose up -d
```

Open `http://<host>:8780`, create the admin password, pick the destination and pair your
cameras. Settings and state live in `./data` (override with `FERRY_DATA`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `FERRY_PORT` | `8780` | Web UI port |
| `FERRY_PASSWORD` | | Preset the admin password (otherwise set it in the browser) |
| `FERRY_DATA` | `./data` | Host folder for settings, state and staging |
| `FERRY_IMAGE` / `FERRY_TAG` | registry image / `latest` | Image to run |
| `TZ` | `UTC` | Time zone for logs |

### Upgrading from GoPro Sync

The data folder is compatible: point `FERRY_DATA` at the old one and nothing is synced twice.
Inside the container the app still reads the old `GOPRO_SYNC_*` variables when the
`FERRY_*` ones are not set. Everyone is signed out once, because the session cookie was renamed.

### Behind Traefik

`docker-compose.traefik.yml` adds router labels for a Traefik instance on the same host.
Because the app uses host networking, Traefik reaches it through the gateway of its Docker
network:

```bash
FERRY_DOMAIN=ferry.example.lan TRAEFIK_UPSTREAM=http://172.19.0.1:8780 \
TRAEFIK_CERTRESOLVER=myresolver \
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d
```

Traefik only routes healthy containers, so the route appears once the health check passes.

### Pairing a camera

On the camera open **Preferences > Connections > Connect Device > GoPro Quik App**, then
click **Find cameras** in the UI and **Pair**. From then on, switching the camera on near
the server starts a sync. After a successful sync the camera is put to standby and is not
synced again for the configured pause.

### Drones and cameras over USB (DJI, Insta360, card readers)

Connect the device with its USB cable (the DJI Neo over USB-C) and switch it on if it needs
to be. Ferry mounts it **read-only**, looks for a `DCIM` folder and shows it under Devices.
The first time, choose **Adopt** (set its folder and rules, then press Sync) or **Adopt and
sync**. From then on it syncs whenever it is plugged in, and is unmounted when done, so it can
be unplugged safely. Drives without `DCIM`, and file systems other than FAT/exFAT/NTFS, are
never touched.

The container needs `SYS_ADMIN`, the `b 8:* rmw` device rule and `/dev` (all in
`docker-compose.yml`). If you would rather not grant that, let the host automount drives (for
example under `/media`), bind-mount that folder into the container and add it as a *watch
folder* in the provider settings.

Capture dates come from the file name where the camera encodes it (`DJI_20261020143012_...`,
`VID_20261020_143012_...`), otherwise from the file time; set `TZ` to the camera's time zone.
Low-resolution proxies (`.LRF`, `.LRV`) and thumbnails stay on the device; the list of file
types is configurable.

### Bambu Lab printers

Under Devices, **Add printer**: *Find printers on the network* fills in the IP address and serial
number from the printer's own announcements (UDP 2021), and you add the **LAN access code** from
the printer screen (Settings, Network). Ferry checks the code over FTPS (implicit TLS, port 990)
and pins the printer's certificate.

It follows the print state over the printer's local MQTT broker (TLS, port 8883, read only) and
copies new timelapses from `/timelapse` a couple of minutes after a print ends, and on a timer
while the printer is idle. It never copies while a print is running. Firmware that restricts
third-party MQTT access only loses the live status; the timer keeps working. Turn on timelapse
recording in Bambu Studio for the prints you want.

### Deleting from the card

Off by default. When enabled, files are deleted from the camera only after every file of
the run exists on the destination with the exact same size.

## Destinations and sync rules

Destinations (SMB shares, local folders) and credentials are set up once and referenced by
the sync rules. The rules are layered, and each layer only overrides what it sets:

1. **Global rules**: destination, folder in the destination, folder layout, delete after sync.
2. **Provider settings** (GoPro, ...): overrides for every device of that kind, plus the
   provider's own options (automatic USB / wireless sync, standby, wireless pause).
3. **Device settings**: overrides for one camera, for example a different folder or a different
   login on the same server.

Every field in the provider and device editors shows the value it inherits until you switch on
*Override*, and a preview shows exactly where a file would land.

**Also copy to** sends every file to more destinations as well, for example the NAS plus an R2
bucket as an off-site copy. A file only counts as synced, and is only deleted from the device,
once every destination has it.

### Cloud destinations

- **S3 / R2**: endpoint (`https://<account id>.r2.cloudflarestorage.com` for R2, empty for
  Amazon S3), bucket, optional prefix, and an access key. Every request is signed over its
  content's SHA-256, so the provider rejects damaged parts; files over 16 MB go up as multipart
  uploads that only appear once complete. The library streams straight from the bucket through
  presigned links.
- **Google Drive**: create an OAuth client of type *TVs and Limited Input devices* in Google
  Cloud Console (with the Drive API enabled), add it under Credentials, Google account, and
  press *Connect*: Ferry shows a code to enter at google.com/device. Ferry uses the `drive.file`
  scope, so it only sees what it created, starting with its folder in My Drive (`Ferry` by
  default). Uploads are resumable and checked against Drive's MD5.

## How files are organised

A file lands at `<folder in the destination>/<folder layout>/<file name>`. The folder in the
destination defaults to `{provider}` and the layout to `{year}/{date}`, which gives
`GoPro/2026/2026-09-26/GX010474.MP4`. Available fields in both:
`{year} {month} {day} {date} {provider} {device} {model}` (`{camera}` is an alias of `{model}`).
Settings migrated from GoPro Sync keep an empty folder, so existing files stay where they are.
Dates come from the camera's clock. If a different file with the same name already exists
(cameras reuse numbers after a card format), the new one is saved as `GX010474_<timestamp>.MP4`.
Files already present with the same size are not uploaded twice, so several servers can
share one destination.

## Architecture

- `ferry/core`: the offload engine (download to staging, upload, verify, optional delete),
  sessions, settings and the SQLite bookkeeping. It only knows two contracts:
- `ferry/providers`: one package per device family. A provider watches for its devices and
  opens them as a `MediaSource` (`list_media`, `download`, `delete`, `thumbnail`).
- `ferry/destinations`: where media goes (`stat`, atomic `put`, `open_range`). Keys are
  relative POSIX paths, so object stores and cloud drives fit the same contract.

## Development

```bash
uv venv && uv pip install -e ".[dev,media]"
.venv/bin/pytest
FERRY_DATA=./.dev-data .venv/bin/python -m ferry   # http://localhost:8780
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
scripts/release.sh   # multi-arch build and push (IMAGE=... to override)
```

On macOS the app runs natively for USB offload and UI work; Docker Desktop cannot reach
USB network devices or Bluetooth, so the container is for Linux hosts.

## Licenses

Bundled assets: [Geist](https://vercel.com/font) (SIL Open Font License) and
[Phosphor Icons](https://phosphoricons.com) (MIT), see `ferry/static/vendor`.
GoPro is a trademark of GoPro, Inc.; this project is not affiliated with GoPro.
