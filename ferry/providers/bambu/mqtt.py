"""Print state from the printer's local MQTT broker (TLS on 8883, user bblp, the access code).

Ferry only reads: it subscribes to the report topic and asks for one full report ("pushall").
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable

PORT = 8883
# gcode_state values that mean a print is under way: never transfer files then.
PRINTING = {"PREPARE", "RUNNING", "PAUSE", "SLICING"}
FINISHED = {"FINISH", "FAILED", "IDLE"}


@dataclass
class PrinterState:
    connected: bool = False
    gcode_state: str = ""  # IDLE | PREPARE | RUNNING | PAUSE | FINISH | FAILED
    percent: int | None = None
    remaining_minutes: int | None = None
    job: str = ""
    error: str = ""
    updated: float = 0.0
    # Set when a print just ended (RUNNING -> FINISH/FAILED); the provider syncs after a delay.
    ended_at: float | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def printing(self) -> bool:
        return self.gcode_state in PRINTING

    def apply(self, report: dict) -> None:
        """Merges one report message; the printer sends partial updates after the first full one."""
        info = report.get("print")
        if not isinstance(info, dict):
            return
        before = self.gcode_state
        if "gcode_state" in info:
            self.gcode_state = str(info["gcode_state"]).upper()
        if "mc_percent" in info:
            self.percent = int(info["mc_percent"])
        if "mc_remaining_time" in info:
            self.remaining_minutes = int(info["mc_remaining_time"])
        if "subtask_name" in info:
            self.job = str(info["subtask_name"])
        self.updated = time.time()
        if before in PRINTING and self.gcode_state in FINISHED:
            self.ended_at = self.updated

    def snapshot(self) -> dict:
        return {"connected": self.connected, "state": self.gcode_state, "percent": self.percent,
                "remaining_minutes": self.remaining_minutes, "job": self.job, "error": self.error,
                "updated": self.updated}


class PrinterMonitor:
    """Keeps a paho-mqtt client connected (its own thread) and mirrors reports into a PrinterState."""

    def __init__(self, host: str, serial: str, access_code: str, on_change: Callable[[], None]):
        self.host, self.serial, self.access_code = host, serial, access_code
        self.state = PrinterState()
        self._loop = asyncio.get_running_loop()
        self._on_change = on_change
        self._client = None

    def start(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.state.error = "paho-mqtt is not installed; checking the printer on a timer instead"
            return
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"ferry-{self.serial[-6:]}-{int(time.time())}")
        client.username_pw_set("bblp", self.access_code)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # self-signed; the FTPS side pins the certificate
        client.tls_set_context(context)
        client.reconnect_delay_set(5, 300)
        client.on_connect = self._connected
        client.on_disconnect = self._disconnected
        client.on_message = self._message
        client.connect_async(self.host, PORT, keepalive=60)
        client.loop_start()
        self._client = client

    def stop(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()
            self._client = None

    # paho callbacks run on its network thread: hop back onto the event loop.
    def _connected(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code.is_failure:
            self._loop.call_soon_threadsafe(self._set_error, f"MQTT refused: {reason_code}")
            return
        client.subscribe(f"device/{self.serial}/report")
        client.publish(f"device/{self.serial}/request",
                       json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))
        self._loop.call_soon_threadsafe(self._set_connected, True)

    def _disconnected(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._loop.call_soon_threadsafe(self._set_connected, False)

    def _message(self, client, userdata, msg) -> None:
        try:
            report = json.loads(msg.payload)
        except ValueError:
            return
        self._loop.call_soon_threadsafe(self._apply, report)

    def _set_connected(self, value: bool) -> None:
        self.state.connected = value
        if value:
            self.state.error = ""
        self._on_change()

    def _set_error(self, message: str) -> None:
        self.state.connected, self.state.error = False, message
        self._on_change()

    def _apply(self, report: dict) -> None:
        before = (self.state.gcode_state, self.state.percent)
        self.state.apply(report)
        if (self.state.gcode_state, self.state.percent) != before:
            self._on_change()
