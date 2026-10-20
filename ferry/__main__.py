"""Entry point: python -m ferry"""

from __future__ import annotations

import logging

import uvicorn

from .auth import AuthStore
from .core.service import SyncService
from .core.settings import SettingsStore, data_dir, env
from .core.store import Store
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
        host=env("HOST", "0.0.0.0"),
        port=int(env("PORT", "8780")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
