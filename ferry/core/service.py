"""Orchestration: run every provider's watcher, run offloads in parallel, publish events."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from pathlib import Path

from ..destinations import resolve_destination
from ..providers import PROVIDERS, Provider
from .engine import Progress, SyncEngine
from .session import Flow, Session
from .settings import Settings, SettingsStore
from .store import Store

log = logging.getLogger("ferry")


class EventBus:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self.history: deque[dict] = deque(maxlen=300)

    def publish(self, event: dict) -> None:
        if event["type"] == "log":
            self.history.append(event)
        for queue in list(self._subscribers):
            if queue.qsize() < 500:
                queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)


class SyncService:
    def __init__(self, settings_store: SettingsStore, store: Store, data_dir: Path,
                 providers: list[type[Provider]] | None = None):
        self.settings_store = settings_store
        self.store = store
        self.data_dir = data_dir
        self.run_dir = data_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.staging = data_dir / "staging"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.thumbs = data_dir / "thumbs"
        self.events = EventBus()

        self.sessions: dict[str, Session] = {}
        # Resources shared by providers: Wi-Fi radios that join device access points
        # (one device per radio at a time) and the Bluetooth adapter.
        self.radios_in_use: set[str] = set()
        self.bluetooth_lock = asyncio.Lock()
        self.providers: dict[str, Provider] = {cls.kind: cls(self) for cls in (PROVIDERS if providers is None else providers)}
        self._tasks: list[asyncio.Task] = []

    @property
    def settings(self) -> Settings:
        return self.settings_store.load()

    def provider(self, kind: str) -> Provider:
        try:
            return self.providers[kind]
        except KeyError:
            raise RuntimeError(f"provider {kind!r} is not available") from None

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._watch(p)) for p in self.providers.values()]
        self.log("info", "service started")

    async def stop(self) -> None:
        tasks = [*self._tasks, *[s.task for s in self.sessions.values() if s.task]]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch(self, provider: Provider) -> None:
        try:
            await provider.watch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log("error", f"{provider.label} watcher stopped: {exc}")

    # --- events ----------------------------------------------------------

    def log(self, level: str, message: str) -> None:
        getattr(log, level if level != "success" else "info")(message)
        self.events.publish({"type": "log", "ts": time.time(), "level": level, "message": message})

    def publish_status(self) -> None:
        self.events.publish({"type": "status", "status": self.snapshot()})

    @property
    def pairing(self) -> str | None:
        return next((p.pairing for p in self.providers.values() if p.pairing), None)

    @property
    def state(self) -> str:
        if self.sessions:
            return "syncing"
        return "pairing" if self.pairing else "idle"

    def snapshot(self) -> dict:
        data = {
            "state": self.state,
            "sessions": [s.snapshot() for s in self.sessions.values()],
            "pairing": self.pairing,
            "providers": [p.describe() for p in self.providers.values()],
            "radios": {"configured": self.settings.wifi_interfaces, "in_use": sorted(self.radios_in_use)},
            "totals": self.store.totals(),
        }
        for provider in self.providers.values():
            data.update(provider.snapshot())
        return data

    # --- manual control --------------------------------------------------

    def request_sync(self, mode: str = "all", device: str | None = None) -> str:
        """Queue syncs; the watchers pick them up on their next pass."""
        requested = sum(p.request_sync(mode, device) for p in self.providers.values())
        if not requested:
            return "nothing to sync: no idle device is connected or paired"
        return f"sync requested for {requested} device(s)"

    def cancel(self, session_id: str | None = None) -> int:
        cancelled = 0
        for session in list(self.sessions.values()):
            if (session_id is None or session.id == session_id) and session.task and not session.task.done():
                session.task.cancel()
                cancelled += 1
        return cancelled

    # --- sessions (used by providers) ------------------------------------

    def has_capacity(self, settings: Settings) -> bool:
        return len(self.sessions) < max(1, settings.max_parallel_syncs)

    def start_session(self, session: Session, flow: Flow) -> None:
        self.sessions[session.id] = session
        session.task = asyncio.create_task(self._run_session(session, flow))
        self.publish_status()

    async def _run_session(self, session: Session, flow: Flow) -> None:
        run_id = self.store.start_run(session.mode, provider=session.provider or None)
        ok = False
        try:
            camera, result = await flow(session)
        except asyncio.CancelledError:
            self.store.finish_run(run_id, "cancelled", 0, 0, camera=session.camera)
            self.log("warning", f"{session.camera or session.name}: sync cancelled")
        except Exception as exc:
            self.store.finish_run(run_id, "failed", 0, 0, str(exc), camera=session.camera)
            self.log("error", f"{session.camera or session.name}: sync failed: {exc}")
        else:
            ok = True
            self.store.finish_run(run_id, "ok", result.files, result.bytes, result.problem, camera=camera)
            self.log("success", f"{camera}: {result.files} files, {result.bytes / 1e9:.2f} GB offloaded"
                                + (f", {result.deleted} deleted from the device" if result.deleted else ""))
            if result.problem:
                self.log("warning", f"{camera}: {result.problem}")
        finally:
            if session.radio:
                self.radios_in_use.discard(session.radio)
            provider = self.providers.get(session.provider)
            if provider is not None:
                provider.session_finished(session, ok)
            self.sessions.pop(session.id, None)
            self.publish_status()

    def engine(self, settings: Settings, session: Session) -> SyncEngine:
        """An engine for this session, with the sync rules of its provider and device (session.device)."""
        def on_progress(progress: Progress) -> None:
            session.progress = progress
            self.events.publish({"type": "progress", "session": session.id, "progress": progress.snapshot()})

        policy = settings.effective(session.rules or session.provider, session.device)
        destination = resolve_destination(settings, policy.destination, policy.credentials, self.remember_destination)
        mirrors = [resolve_destination(settings, m, on_learn=self.remember_destination)
                   for m in policy.mirrors if m != policy.destination]
        return SyncEngine(self.store, policy, destination, self.staging,
                          lambda level, msg: self.log(level, f"{session.camera or session.name}: {msg}"), on_progress,
                          self.thumbs, mirrors)

    def remember_destination(self, dest_id: str, updates: dict) -> None:
        """Stores what a destination pinned on first contact (SSH host key, TLS certificate)."""
        def apply(settings: Settings) -> None:
            cfg = settings.destination(dest_id)
            if cfg is not None:
                for key, value in updates.items():
                    setattr(cfg, key, value)

        self.settings_store.update(apply)
        self.log("info", f"destination {dest_id}: pinned {', '.join(f'{k} {v}' for k, v in updates.items())}")

    def identify(self, session: Session, device_id: str, label: str) -> tuple[str, str]:
        """Records who the device turned out to be, once connected."""
        session.camera = f"{label} {device_id}"
        session.state = "syncing"
        self.publish_status()
        return device_id, label
