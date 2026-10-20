"""Web UI and JSON API."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import __version__
from .auth import COOKIE, SESSION_TTL, AuthStore
from .core.engine import thumb_name
from .core.service import SyncService
from .core.settings import SECRET_KEYS, VERSION, Settings, SyncOverrides, template_fields
from .destinations import DestinationError, resolve_destination
from .destinations import gdrive

STATIC = Path(__file__).parent / "static"
MASK = "********"
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
    # Saved credentials to fall back on for fields the form leaves empty or masked.
    credentials: str | None = None


class DeviceUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    sync: SyncOverrides | None = None
    config: dict | None = None


class DeviceCreate(BaseModel):
    provider: str
    name: str = ""
    config: dict = {}
    sync: SyncOverrides = SyncOverrides()


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


def _invalid(exc: ValueError) -> HTTPException:
    """422 with a short message: 'defaults.folder_template: unknown placeholder {oops}; ...'."""
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors():
            where = ".".join(str(x) for x in err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            parts.append(f"{where}: {msg}" if where else msg)
        return HTTPException(422, "; ".join(parts))
    return HTTPException(422, str(exc))


def _mask(obj: dict) -> dict:
    for key in SECRET_KEYS & obj.keys():
        if obj[key]:
            obj[key] = MASK
    return obj


def _unmask(incoming: dict, current: dict | None) -> dict:
    """Puts back the stored secret wherever the client sent the mask (or nothing)."""
    for key in SECRET_KEYS:
        if incoming.get(key, MASK) == MASK:
            incoming[key] = (current or {}).get(key, "")
    return incoming


def _public(settings: Settings) -> dict:
    data = settings.model_dump()
    for cred in data["credentials"]:
        _mask(cred)
    for device in data["devices"]:
        _mask(device["config"])
    return data


def _merge(current: Settings, incoming: dict) -> Settings:
    incoming = dict(incoming)
    saved = {c.id: c.model_dump() for c in current.credentials}
    incoming["credentials"] = [_unmask(dict(c), saved.get(c.get("id"))) for c in incoming.get("credentials", [])]
    # Devices are managed through /api/devices, so a stale form cannot drop one a watcher just added.
    incoming["devices"] = [d.model_dump() for d in current.devices]
    incoming["version"] = VERSION
    return Settings.model_validate(incoming)


def create_app(service: SyncService, auth: AuthStore) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await service.start()
        yield
        await service.stop()

    app = FastAPI(title="Ferry", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

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
            raise _invalid(exc)
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
            raise _invalid(exc)
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
            raise _invalid(exc)
        service.settings_store.save(settings)
        service.log("info", "settings saved")
        return _public(settings)

    async def _test_destination(dest_id: str | None, credentials: str | None = None) -> dict:
        try:
            target = resolve_destination(service.settings, dest_id, credentials)
            await target.test()
        except Exception as exc:
            raise HTTPException(400, f"{type(exc).__name__}: {exc}")
        return {"ok": True, "target": target.describe()}

    @app.post("/api/target/test")
    async def test_target():
        return await _test_destination(service.settings.defaults.destination)

    @app.post("/api/destinations/{dest_id}/test")
    async def test_destination(dest_id: str, credentials: str | None = None):
        return await _test_destination(dest_id, credentials)

    @app.get("/api/providers")
    async def providers():
        return [p.describe() for p in service.providers.values()]

    @app.get("/api/providers/{kind}/discover")
    async def provider_discover(kind: str):
        provider = service.providers.get(kind)
        if provider is None:
            raise HTTPException(404, f"unknown provider {kind!r}")
        try:
            return await provider.discover()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/effective")
    async def effective(provider: str, device: str | None = None):
        settings = service.settings
        policy = settings.effective(provider, device)
        dev = settings.device(device)
        label = service.providers[provider].label if provider in service.providers else provider
        model = (dev.config.get("model") if dev else None) or label
        example = policy.key(template_fields(1790438400, label, (dev.name if dev else "") or model, model), "EXAMPLE.MP4")
        try:
            location = resolve_destination(settings, policy.destination, policy.credentials).describe(example)
        except DestinationError as exc:
            location = None
            example = f"{example} ({exc})"
        values = {k: getattr(policy, k) for k in policy.origin}
        return {"policy": values, "origin": policy.origin, "example": example, "location": location}

    # --- Google sign-in (device flow) -----------------------------------------

    pending_google: dict[str, dict] = {}

    def _google_credential(cred_id: str):
        cred = service.settings.credential(cred_id)
        if cred is None or cred.kind != "oauth_google":
            raise HTTPException(404, "unknown Google credentials")
        if not (cred.client_id and cred.client_secret):
            raise HTTPException(422, "enter the OAuth client ID and secret first")
        return cred

    @app.post("/api/credentials/{cred_id}/google/start")
    async def google_start(cred_id: str):
        cred = _google_credential(cred_id)
        try:
            flow = await asyncio.to_thread(gdrive.start_device_flow, cred.client_id)
        except DestinationError as exc:
            raise HTTPException(400, str(exc))
        pending_google[cred_id] = flow
        return {"user_code": flow["user_code"], "verification_url": flow.get("verification_url") or flow.get("verification_uri"),
                "interval": flow.get("interval", 5), "expires_in": flow.get("expires_in", 1800)}

    @app.post("/api/credentials/{cred_id}/google/poll")
    async def google_poll(cred_id: str):
        cred = _google_credential(cred_id)
        flow = pending_google.get(cred_id)
        if flow is None:
            raise HTTPException(409, "start the Google sign-in first")
        try:
            refresh = await asyncio.to_thread(gdrive.poll_device_flow, cred.client_id, cred.client_secret, flow["device_code"])
        except gdrive.OAuthPending:
            return {"status": "pending"}
        except DestinationError as exc:
            pending_google.pop(cred_id, None)
            raise HTTPException(400, str(exc))
        pending_google.pop(cred_id, None)
        connected = cred.model_copy(update={"refresh_token": refresh})
        try:
            account = await asyncio.to_thread(gdrive.account_email, connected)
        except DestinationError:
            account = ""

        def store(settings: Settings) -> None:
            c = settings.credential(cred_id)
            if c is not None:
                c.refresh_token, c.account = refresh, account or c.account

        service.settings_store.update(store)
        service.log("success", f"Google account connected{f': {account}' if account else ''}")
        return {"status": "ok", "account": account}

    # --- devices -----------------------------------------------------------

    def _save_devices(change) -> dict:
        try:
            settings = service.settings_store.update(change)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise _invalid(exc)
        service.publish_status()
        return _public(settings)

    @app.post("/api/devices")
    async def add_device(req: DeviceCreate):
        provider = service.providers.get(req.provider)
        if provider is None:
            raise HTTPException(404, f"unknown provider {req.provider!r}")
        try:
            device = await provider.new_device(req.name, req.config)
        except ValueError as exc:
            raise _invalid(exc)
        device.sync = req.sync

        def add(settings: Settings) -> None:
            if settings.device(device.id):
                raise ValueError(f"{device.id} is already set up")
            settings.devices.append(device)

        data = _save_devices(add)
        service.log("info", f"{provider.label} device added: {device.name or device.id}")
        return data

    @app.put("/api/devices/{device_id}")
    async def update_device(device_id: str, req: DeviceUpdate):
        def change(settings: Settings) -> None:
            device = settings.device(device_id)
            if device is None:
                raise LookupError(f"unknown device {device_id!r}")
            if req.name is not None:
                device.name = req.name
            if req.enabled is not None:
                device.enabled = req.enabled
            if req.sync is not None:
                device.sync = req.sync
            if req.config is not None:
                device.config = {**device.config, **_unmask(dict(req.config), device.config)}

        return _save_devices(change)

    @app.delete("/api/devices/{device_id}")
    async def delete_device(device_id: str):
        def remove(settings: Settings) -> None:
            if settings.device(device_id) is None:
                raise LookupError(f"unknown device {device_id!r}")
            settings.devices = [d for d in settings.devices if d.id != device_id]

        return _save_devices(remove)

    # --- network discovery ---------------------------------------------

    @app.get("/api/discover/hosts")
    async def discover_hosts():
        from . import discovery

        hosts = await discovery.discover_hosts()
        return [h.__dict__ for h in hosts]

    @app.post("/api/discover/shares")
    async def discover_shares(req: SharesRequest):
        from . import discovery

        cred = service.settings.credential(req.credentials)
        # Reuse the saved credentials unless the form sends new ones.
        username = (cred.username if cred else "") if req.username is None else req.username
        password = (cred.password if cred else "") if req.password in (None, MASK) else req.password
        try:
            return await discovery.list_smb_shares(req.host, username, password)
        except discovery.DiscoveryError as exc:
            raise HTTPException(400, str(exc))

    # --- cameras ---------------------------------------------------------

    def gopro():
        return service.provider("gopro")

    @app.get("/api/ble/scan")
    async def ble_scan():
        try:
            return await gopro().scan_ble()
        except Exception as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/ble/pair")
    async def ble_pair(req: PairRequest):
        try:
            await gopro().pair(req.address, req.name)
        except Exception as exc:
            service.log("error", f"pairing failed: {exc}")
            raise HTTPException(400, f"pairing failed: {exc}")
        return {"ok": True}

    @app.delete("/api/ble/cameras/{address}")
    async def ble_forget(address: str):
        gopro().forget(address)
        return {"ok": True}

    @app.post("/api/sync")
    async def sync(req: SyncRequest):
        return {"message": service.request_sync(req.mode, req.camera)}

    @app.post("/api/sync/cancel")
    async def cancel(req: CancelRequest | None = None):
        return {"cancelled": service.cancel(req.session if req else None)}

    # --- library -----------------------------------------------------------

    @app.get("/api/library")
    async def library(offset: int = 0, limit: int = 60, provider: str | None = None):
        items, total = service.store.library(max(offset, 0), min(max(limit, 1), 200), provider or None)
        for item in items:
            item["name"] = item["path"].rsplit("/", 1)[-1]
            item["thumb"] = (service.thumbs / thumb_name(item["dest"])).exists()
        return {"items": items, "total": total, "providers": service.store.library_providers()}

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
            settings = service.settings
            # Files synced before destinations existed live on the default one.
            destination = resolve_destination(settings, item["dest_id"] or settings.defaults.destination, item["cred_id"])
            remote = await destination.stat(item["dest"])
        except (DestinationError, OSError) as exc:
            raise HTTPException(502, f"cannot open {item['dest']} on the destination: {exc}") from exc
        if remote is None:
            raise HTTPException(502, f"{item['dest']} is no longer on the destination")
        if "presigned_url" in destination.capabilities:
            # Object stores serve the bytes themselves (with ranges); the Pi stays out of the way.
            url = destination.presigned_url(item["dest"])
            secure_page = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
            if url.startswith("https://") or not secure_page:
                return RedirectResponse(url, status_code=307)
        size = remote.size
        try:
            span = _byte_range(request.headers.get("range"), size)
        except (HTTPException, ValueError):
            raise HTTPException(416, "range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
        start, end = span or (0, size - 1)
        name = item["path"].rsplit("/", 1)[-1]
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
                   "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{name}"'}
        if span:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        async def body():
            async with aclosing(destination.open_range(item["dest"], start, end)) as chunks:
                async for chunk in chunks:
                    if await request.is_disconnected():
                        break
                    yield chunk

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
