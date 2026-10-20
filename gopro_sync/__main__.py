"""Entry point: python -m gopro_sync"""

from __future__ import annotations

import logging
import os

import uvicorn

from .auth import AuthStore
from .config import SettingsStore, data_dir
from .service import SyncService
from .store import Store
from .web import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # smbprotocol and httpx log every SMB message / HTTP request (keep-alive every 3 s) at INFO.
    for name in ("smbprotocol", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)
    root = data_dir()
    service = SyncService(SettingsStore(root / "settings.json"), Store(root / "state.db"), root)
    uvicorn.run(
        create_app(service, AuthStore(root / "auth.json")),
        host=os.environ.get("GOPRO_SYNC_HOST", "0.0.0.0"),
        port=int(os.environ.get("GOPRO_SYNC_PORT", "8780")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
