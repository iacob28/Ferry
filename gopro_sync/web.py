"""Web UI and JSON API."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .auth import COOKIE, SESSION_TTL, AuthStore
from .config import Settings
from .service import SyncService
from .engine import thumb_name
from .targets import TargetError, make_target

STATIC = Path(__file__).parent / "static"
MASK = "********"
STREAM_CHUNK = 1 << 20
MEDIA_TYPES = {"mp4": "video/mp4", "mov": "video/quicktime", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "gpr": "application/octet-stream", "wav": "audio/wav"}
# Reachable without a session: the page shell and the sign-in/setup endpoints.
PUBLIC_PATHS = {"/", "/api/auth", "/api/auth/login", "/api/auth/setup", "/healthz"}


class PairRequest(BaseModel):
    address: str
    name: str = ""


class SyncRequest(BaseModel):
    mode: str = "all"
    camera: str | None = None


class CancelRequest(BaseModel):
    session: str | None = None


class PasswordRequest(BaseModel):
    password: str
    current: str | None = None


class SharesRequest(BaseModel):
    host: str
    username: str | None = None
    password: str | None = None


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parses a single `bytes=start-end` range; returns an inclusive (start, end) or None for the full file."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    start_s, _, end_s = header[6:].strip().partition("-")
    if start_s:
        start = int(start_s)
        end = min(int(end_s), size - 1) if end_s else size - 1
    else:  # suffix range: the last N bytes
        start, end = max(size - int(end_s), 0), size - 1
    if start > end or start >= size:
        raise HTTPException(416, "range not satisfiable")
    return start, end


def _public(settings: Settings) -> dict:
    data = settings.model_dump()
    data["smb"]["password"] = MASK if settings.smb.password else ""
    return data


def _merge(current: Settings, incoming: dict) -> Settings:
    password = incoming.get("smb", {}).get("password")
    if password in (None, MASK):
        incoming.setdefault("smb", {})["password"] = current.smb.password
    # Paired cameras are managed by the pairing endpoints only.
    incoming["cameras"] = [c.model_dump() for c in current.cameras]
    return Settings.model_validate(incoming)


def create_app(service: SyncService, auth: AuthStore) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await service.start()
        yield
        await service.stop()

    app = FastAPI(title="GoPro Sync", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or not path.startswith("/api/"):
            return await call_next(request)
        if not auth.configured:
            return JSONResponse({"detail": "setup required"}, status_code=401)
        if not auth.valid(request.cookies.get(COOKIE)):
            return JSONResponse({"detail": "sign in required"}, status_code=401)
        return await call_next(request)

    def _sign_in(request: Request, response: Response) -> None:
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(COOKIE, auth.issue(), max_age=SESSION_TTL, httponly=True, samesite="lax", secure=secure)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "version": __version__}

    # --- auth ------------------------------------------------------------

    @app.get("/api/auth")
    async def auth_state(request: Request):
        return {"configured": auth.configured, "authenticated": auth.valid(request.cookies.get(COOKIE)),
                "version": __version__}

    @app.post("/api/auth/setup")
    async def auth_setup(req: PasswordRequest, request: Request, response: Response):
        if auth.configured:
            raise HTTPException(409, "a password is already set")
        try:
            auth.set_password(req.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        _sign_in(request, response)
        service.log("info", "admin password created")
        return {"ok": True}

    @app.post("/api/auth/login")
    async def auth_login(req: PasswordRequest, request: Request, response: Response):
        if not auth.check_password(req.password):
            await asyncio.sleep(1)  # Slow down guessing.
            raise HTTPException(401, "wrong password")
        _sign_in(request, response)
        return {"ok": True}

    @app.post("/api/auth/logout")
    async def auth_logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.post("/api/auth/password")
    async def auth_change(req: PasswordRequest, request: Request, response: Response):
        if not auth.check_password(req.current or ""):
            raise HTTPException(401, "current password is wrong")
        try:
            auth.set_password(req.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        _sign_in(request, response)
        return {"ok": True}

    # --- status & settings -------------------------------------------------

    @app.get("/api/status")
    async def status():
        return service.snapshot()

    @app.get("/api/settings")
    async def get_settings():
        return _public(service.settings)

    @app.put("/api/settings")
    async def put_settings(payload: dict):
        try:
            settings = _merge(service.settings, payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        service.settings_store.save(settings)
        service.log("info", "settings saved")
        return _public(settings)

    @app.post("/api/target/test")
    async def test_target():
        try:
            target = make_target(service.settings)
            await target.test()
        except Exception as exc:
            raise HTTPException(400, f"{type(exc).__name__}: {exc}")
        return {"ok": True, "target": target.describe()}

    # --- network discovery ---------------------------------------------

    @app.get("/api/discover/hosts")
    async def discover_hosts():
        from . import discovery

        hosts = await discovery.discover_hosts()
        return [h.__dict__ for h in hosts]

    @app.post("/api/discover/shares")
    async def discover_shares(req: SharesRequest):
        from . import discovery

        smb = service.settings.smb
        # Reuse the saved credentials unless the form sends new ones.
        username = smb.username if req.username is None else req.username
        password = smb.password if req.password in (None, MASK) else req.password
        try:
            return await discovery.list_smb_shares(req.host, username, password)
        except discovery.DiscoveryError as exc:
            raise HTTPException(400, str(exc))

    # --- cameras ---------------------------------------------------------

    @app.get("/api/ble/scan")
    async def ble_scan():
        try:
            return await service.scan_ble()
        except Exception as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/ble/pair")
    async def ble_pair(req: PairRequest):
        try:
            await service.pair(req.address, req.name)
        except Exception as exc:
            service.log("error", f"pairing failed: {exc}")
            raise HTTPException(400, f"pairing failed: {exc}")
        return {"ok": True}

    @app.delete("/api/ble/cameras/{address}")
    async def ble_forget(address: str):
        service.forget(address)
        return {"ok": True}

    @app.post("/api/sync")
    async def sync(req: SyncRequest):
        return {"message": service.request_sync(req.mode, req.camera)}

    @app.post("/api/sync/cancel")
    async def cancel(req: CancelRequest | None = None):
        return {"cancelled": service.cancel(req.session if req else None)}

    # --- library -----------------------------------------------------------

    @app.get("/api/library")
    async def library(offset: int = 0, limit: int = 60):
        items, total = service.store.library(max(offset, 0), min(max(limit, 1), 200))
        for item in items:
            item["name"] = item["path"].rsplit("/", 1)[-1]
            item["thumb"] = (service.thumbs / thumb_name(item["dest"])).exists()
        return {"items": items, "total": total}

    def _item(item_id: int) -> dict:
        item = service.store.library_item(item_id)
        if item is None:
            raise HTTPException(404, "not found")
        return item

    @app.get("/api/library/{item_id}/thumb")
    async def library_thumb(item_id: int):
        path = service.thumbs / thumb_name(_item(item_id)["dest"])
        if not path.exists():
            raise HTTPException(404, "no thumbnail")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/library/{item_id}/media")
    async def library_media(item_id: int, request: Request, download: bool = False):
        item = _item(item_id)
        try:
            target = make_target(service.settings)
            fh = await asyncio.to_thread(target.open_read, item["dest"])
        except (TargetError, OSError) as exc:
            raise HTTPException(502, f"cannot open {item['dest']} on the destination: {exc}") from exc
        size = item["size"] if item["size"] >= 0 else await asyncio.to_thread(lambda: fh.seek(0, 2))
        try:
            span = _byte_range(request.headers.get("range"), size)
        except (HTTPException, ValueError):
            fh.close()
            raise HTTPException(416, "range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
        start, end = span or (0, size - 1)
        name = item["path"].rsplit("/", 1)[-1]
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
                   "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{name}"'}
        if span:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        async def body():
            try:
                await asyncio.to_thread(fh.seek, start)
                left = end - start + 1
                while left > 0 and not await request.is_disconnected():
                    chunk = await asyncio.to_thread(fh.read, min(STREAM_CHUNK, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    yield chunk
            finally:
                await asyncio.to_thread(fh.close)

        media_type = MEDIA_TYPES.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
        return StreamingResponse(body(), status_code=206 if span else 200, media_type=media_type, headers=headers)

    @app.get("/api/runs")
    async def runs():
        return service.store.recent_runs()

    @app.get("/api/logs")
    async def logs():
        return list(service.events.history)

    @app.get("/api/events")
    async def events(request: Request):
        queue = service.events.subscribe()

        async def stream():
            try:
                yield f"data: {json.dumps({'type': 'status', 'status': service.snapshot()})}\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), 15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                service.events.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
