"""Open GoPro BLE control: discovery, pairing, Wi-Fi AP control, keep-alive, sleep."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

GOPRO_SERVICE_UUID = "0000fea6-0000-1000-8000-00805f9b34fb"
GOPRO_COMPANY_ID = 0x02F2


def _gp_uuid(short: str) -> str:
    return f"b5f9{short}-aa8d-11e3-9046-0002a5d5c51b"


WIFI_AP_SSID = _gp_uuid("0002")
WIFI_AP_PASSWORD = _gp_uuid("0003")
COMMAND = _gp_uuid("0072")
COMMAND_RESPONSE = _gp_uuid("0073")
SETTINGS = _gp_uuid("0074")
SETTINGS_RESPONSE = _gp_uuid("0075")

CMD_SLEEP = 0x05
CMD_AP_CONTROL = 0x17
SETTING_KEEP_ALIVE = 0x5B


class BleError(Exception):
    pass


@dataclass
class Advert:
    address: str
    name: str
    rssi: int
    # From the manufacturer data; None when the camera does not advertise it.
    powered_on: bool | None


def parse_powered_on(manufacturer_data: dict[int, bytes]) -> bool | None:
    data = manufacturer_data.get(GOPRO_COMPANY_ID)
    if not data or len(data) < 2:
        return None
    # Byte 0 is the schema version, byte 1 the camera status bitfield (bit 0 = processor on).
    return bool(data[1] & 0x01)


def frame(payload: bytes) -> bytes:
    """Wrap a short message in a general 5-bit length header."""
    if len(payload) > 19:
        raise ValueError("only single-packet messages are supported")
    return bytes([len(payload)]) + payload


class PacketAssembler:
    """Reassembles (possibly fragmented) Open GoPro BLE response packets."""

    def __init__(self):
        self._buf = bytearray()
        self._expected = -1

    def feed(self, packet: bytes) -> bytes | None:
        if not packet:
            return None
        header = packet[0]
        if header & 0x80:
            if self._expected < 0:
                return None  # Continuation without a start packet.
            self._buf += packet[1:]
        else:
            kind = (header >> 5) & 0x03
            if kind == 0:
                self._expected, body = header & 0x1F, packet[1:]
            elif kind == 1:
                self._expected, body = ((header & 0x1F) << 8) | packet[1], packet[2:]
            elif kind == 2:
                self._expected, body = (packet[1] << 8) | packet[2], packet[3:]
            else:
                return None
            self._buf = bytearray(body)
        if len(self._buf) >= self._expected:
            message = bytes(self._buf[: self._expected])
            self._buf, self._expected = bytearray(), -1
            return message
        return None


async def scan(timeout: float = 8.0) -> list[Advert]:
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True, service_uuids=[GOPRO_SERVICE_UUID])
    adverts = []
    for device, adv in found.values():
        adverts.append(Advert(
            address=device.address,
            name=adv.local_name or device.name or "",
            rssi=adv.rssi,
            powered_on=parse_powered_on(adv.manufacturer_data),
        ))
    return sorted(adverts, key=lambda a: a.rssi, reverse=True)


class GoProBle:
    def __init__(self, address: str):
        self.address = address
        self._client = None
        self._queues = {COMMAND_RESPONSE: asyncio.Queue(), SETTINGS_RESPONSE: asyncio.Queue()}
        self._assemblers = {COMMAND_RESPONSE: PacketAssembler(), SETTINGS_RESPONSE: PacketAssembler()}

    async def connect(self, pair: bool = False, timeout: float = 20.0) -> None:
        from bleak import BleakClient, BleakScanner

        device = await BleakScanner.find_device_by_address(self.address, timeout=timeout)
        if device is None:
            raise BleError(f"camera {self.address} not found")
        self._client = BleakClient(device)
        await self._client.connect(timeout=timeout)
        if pair:
            await self._client.pair()
        for uuid in (COMMAND_RESPONSE, SETTINGS_RESPONSE):
            await self._client.start_notify(uuid, self._handler(uuid))

    def _handler(self, uuid: str):
        def on_notify(_char, data: bytearray) -> None:
            message = self._assemblers[uuid].feed(bytes(data))
            if message is not None:
                self._queues[uuid].put_nowait(message)
        return on_notify

    async def _request(self, char: str, response_char: str, payload: bytes, timeout: float = 8.0) -> bytes:
        if self._client is None:
            raise BleError("not connected")
        queue = self._queues[response_char]
        while not queue.empty():
            queue.get_nowait()
        await self._client.write_gatt_char(char, frame(payload), response=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise BleError(f"no response to 0x{payload[0]:02x}")
            try:
                message = await asyncio.wait_for(queue.get(), remaining)
            except asyncio.TimeoutError:
                raise BleError(f"no response to 0x{payload[0]:02x}") from None
            # Response layout: [id, status, ...]; ignore unrelated responses.
            if message and message[0] == payload[0]:
                if len(message) < 2 or message[1] != 0:
                    raise BleError(f"command 0x{payload[0]:02x} failed: {message.hex()}")
                return message

    async def set_wifi_ap(self, enabled: bool) -> None:
        await self._request(COMMAND, COMMAND_RESPONSE, bytes([CMD_AP_CONTROL, 0x01, 1 if enabled else 0]))

    async def wifi_credentials(self) -> tuple[str, str]:
        ssid = await self._client.read_gatt_char(WIFI_AP_SSID)
        password = await self._client.read_gatt_char(WIFI_AP_PASSWORD)
        return bytes(ssid).decode(), bytes(password).decode()

    async def keep_alive(self) -> None:
        await self._request(SETTINGS, SETTINGS_RESPONSE, bytes([SETTING_KEEP_ALIVE, 0x01, 0x42]))

    async def sleep(self) -> None:
        # The camera may drop the link before answering, so any failure here is fine.
        try:
            await self._request(COMMAND, COMMAND_RESPONSE, bytes([CMD_SLEEP]), timeout=3)
        except Exception:
            pass

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None
