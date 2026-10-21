# Contributing

Thanks for helping. Bug reports, device reports ("works with my HERO12 / Mini 4 / A1") and
pull requests are all welcome.

## Setup

```bash
uv venv && uv pip install -e ".[dev,media]"
.venv/bin/pytest
FERRY_DATA=./.dev-data .venv/bin/python -m ferry   # http://localhost:8780
```

The app runs natively on macOS and Linux for UI work and USB offload. Wireless GoPro, BLE and
mounting drives need a Linux host, usually through Docker:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

## Where things go

- **A new device family:** a package under `ferry/providers/` with a `Provider`
  (`ferry/providers/base.py`) that opens devices as a `MediaSource` (`ferry/core/media.py`).
  `usbstorage` and `bambu` are small, complete examples.
- **A new storage backend:** a module under `ferry/destinations/` that implements
  `ferry/destinations/base.py`. See `tests/test_cloud.py` and `tests/test_sftp_ftp.py` for
  how the existing ones are tested against fakes or local servers.
- **The UI** is `ferry/static/index.html`: one file, no build step, no framework.

## Pull requests

- Keep each pull request to one change, with tests where the code can be tested without
  hardware.
- Match the style of the code around your change.
- Say in the description which real device or server you tried it on.

## Releases

Maintainers bump `version` in `pyproject.toml` and `ferry/__init__.py`, merge, and push a
`v<version>` tag. GitHub Actions builds the arm64 and amd64 image, pushes it to
`ghcr.io/iacob28/ferry` and creates the release notes.
