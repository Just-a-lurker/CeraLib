"""bleak-based client. Read-only use is verified on a PCM03SMAX; writes are not (see README).

Close the vendor app before connecting: the machine most likely accepts one
BLE connection at a time.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Callable, Dict, Optional

from . import messages as m
from .codec import FROM_MACHINE, Frame, FrameError, Reassembler, decode, encode

NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"  # machine -> app
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"   # app -> machine

SETTLE_AFTER_CONNECT = 4.0
REPLY_TIMEOUT = 0.5
STATS_TIMEOUT = 3.0   # the stats table arrives in ~21 fragments   # the vendor app re-sends after ~0.4 s without a reply
RETRIES = 2

# Observed range only: the app offered 90-95 C in captures. Not a machine limit.
MIN_TARGET_C = 60
MAX_TARGET_C = 95


class CeraError(RuntimeError):
    pass


class CommandRejected(CeraError):
    pass


class CeraClient:
    def __init__(
        self,
        address_or_device,
        *,
        on_status: Optional[Callable[[m.Status], None]] = None,
        on_frame: Optional[Callable[[str, bytes], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ):
        """``on_frame(direction, raw)`` sees every frame ("TX" or "RX"), for logging."""
        from bleak import BleakClient  # lazy: codec/tests need no bleak

        self._client = BleakClient(address_or_device, disconnected_callback=self._lost)
        self._on_status = on_status
        self._on_frame = on_frame
        self._on_disconnect = on_disconnect
        self._reasm = Reassembler()
        self._waiters: Dict[int, "asyncio.Future[Frame]"] = {}
        self.status: Optional[m.Status] = None
        self.info: Optional[m.DeviceInfo] = None
        self.settings: Optional[m.Settings] = None
        self.target_f: Optional[int] = None

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    @property
    def target_c(self) -> Optional[float]:
        return None if self.target_f is None else round(m.f_to_c(self.target_f), 1)

    def _lost(self, _client) -> None:
        if self._on_disconnect:
            self._on_disconnect()

    async def __aenter__(self) -> "CeraClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # -- connection ------------------------------------------------------
    async def connect(self, attempts: int = 3) -> None:
        """Connect and run the handshake, reconnecting if the machine stays silent.

        On Windows the machine sometimes ignores everything on a fresh
        connection (no replies at all); a new connection fixes it, resending
        does not.
        """
        from bleak.exc import BleakError

        for attempt in range(attempts):
            try:
                await self._connect_once()
                return
            except (CeraError, BleakError, OSError, asyncio.TimeoutError) as exc:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                if attempt == attempts - 1:
                    if isinstance(exc, CeraError):
                        raise
                    # Do not leak bleak/OS exceptions to apps.
                    raise CeraError(f"connect failed: {exc}") from exc
                await asyncio.sleep(2.0)

    async def _connect_once(self) -> None:
        await self._client.connect()
        # Measured on Windows: if commands follow the connection too quickly
        # the machine often stays silent (4 of 17 trials answered); after a
        # 4 s wait 7 of 8 did. Wait before subscribing.
        await asyncio.sleep(SETTLE_AFTER_CONNECT)
        await self._client.start_notify(NOTIFY_UUID, self._handle)
        await asyncio.sleep(1.0)  # the vendor app pauses ~0.7 s before its first command
        # Handshake as the vendor app does it: clock, limits, settings, info,
        # then an empty status request which starts the heartbeat.
        now = _dt.datetime.now()
        await self.request(m.CMD_SET_CLOCK, m.clock_payload(
            now.year, now.month, now.day, now.weekday() + 1 if now.weekday() < 6 else 0,
            now.hour, now.minute, now.second))
        limits = (await self.request(m.CMD_LIMITS)).payload
        if len(limits) >= 3:
            self.target_f = limits[-1]  # inferred: last byte tracks the target
        await self.get_settings()
        self.info = m.DeviceInfo.parse((await self.request(m.CMD_INFO)).payload)
        # The empty status request has no direct reply: it just starts the
        # ~1 Hz heartbeat. Send it, then wait for the first heartbeat.
        await self._write(encode(m.CMD_STATUS))
        for _ in range(30):
            if self.status is not None:
                break
            await asyncio.sleep(0.1)
        else:
            raise CeraError("no heartbeat after status request")

    async def disconnect(self) -> None:
        await self._client.disconnect()

    # -- low level -------------------------------------------------------
    def _handle(self, _sender, data: bytearray) -> None:
        if self._on_frame:
            self._on_frame("RX", bytes(data))
        try:
            frame = decode(bytes(data))
            if frame.direction != FROM_MACHINE:
                return
            frame = self._reasm.feed(frame)
        except FrameError:
            return  # drop corrupt frames; a retry will recover
        if frame is None:
            return
        if frame.cmd == m.CMD_STATUS and len(frame.payload) >= 6:
            self.status = m.Status.parse(frame.payload)
            if self._on_status:
                self._on_status(self.status)
        waiter = self._waiters.get(frame.cmd)
        if waiter and not waiter.done():
            waiter.set_result(frame)

    async def request(self, cmd: int, payload: bytes = b"", timeout: float = REPLY_TIMEOUT) -> Frame:
        """Send a command and return the reply with the same command byte."""
        data = encode(cmd, payload)
        for _ in range(RETRIES + 1):
            fut: "asyncio.Future[Frame]" = asyncio.get_running_loop().create_future()
            self._waiters[cmd] = fut
            try:
                await self._write(data)
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                continue
            finally:
                self._waiters.pop(cmd, None)
        raise CeraError(f"no reply to command 0x{cmd:02x}")

    async def _write(self, data: bytes) -> None:
        if self._on_frame:
            self._on_frame("TX", data)
        await self._client.write_gatt_char(WRITE_UUID, data, response=False)

    async def _set(self, cmd: int, payload: bytes) -> None:
        reply = await self.request(cmd, payload)
        ok = payload if cmd == m.CMD_TARGET_TEMP else m.STATUS_OK  # 0x16 echoes the value
        if reply.payload != ok:
            raise CommandRejected(f"0x{cmd:02x} -> {reply.payload.hex()}")

    # -- settings --------------------------------------------------------
    async def set_target_temp_c(self, celsius: float) -> None:
        if not MIN_TARGET_C <= celsius <= MAX_TARGET_C:
            raise ValueError(f"target must be {MIN_TARGET_C}-{MAX_TARGET_C} C")
        payload = m.target_payload(celsius)
        await self._set(m.CMD_TARGET_TEMP, payload)
        self.target_f = payload[0]

    async def get_settings(self) -> m.Settings:
        self.settings = m.Settings.parse((await self.request(m.CMD_GET_SETTINGS)).payload)
        return self.settings

    async def update_settings(self, *, flow=None, presoak_s=None, standstill_s=None,
                              extraction_raw=None) -> m.Settings:
        """Read-modify-write, like the vendor app (it also re-sends the target first)."""
        if flow is not None and not m.FLOW_MIN <= flow <= m.FLOW_MAX:
            raise ValueError(f"flow must be {m.FLOW_MIN}-{m.FLOW_MAX}")
        cur = await self.get_settings()
        new = m.Settings(
            cur.raw,
            cur.flow if flow is None else flow,
            cur.presoak_s if presoak_s is None else presoak_s,
            cur.standstill_s if standstill_s is None else standstill_s,
            cur.extraction_raw if extraction_raw is None else extraction_raw,
        )
        await self._set(m.CMD_SET_SETTINGS, new.write_payload())
        return await self.get_settings()

    # -- statistics --------------------------------------------------------
    async def get_stats(self) -> m.Stats:
        """Read the per-day cup counts. Read-only. The reply arrives in about
        21 notifications (~0.7 s), hence the longer timeout."""
        frame = await self.request(m.CMD_STATS, bytes([0x00]), timeout=STATS_TIMEOUT)
        return m.Stats.parse(frame.payload)

    # -- reservations ------------------------------------------------------
    async def get_reservation(self) -> m.Reservation:
        """Read the reservation stored on the machine (read-only)."""
        return m.Reservation.parse((await self.request(m.CMD_GET_RESERVATION)).payload)

    async def set_reservation(
        self,
        mode: m.ReservationMode,
        hour: int,
        minute: int,
        *,
        alarm: Optional[bool] = None,
        beep: Optional[int] = None,
    ) -> m.Reservation:
        """Schedule a start. WARNING: the machine will then heat and/or extract
        BY ITSELF at hour:minute (its own clock, set on connect), even with no
        app connected. It runs once.

        ``alarm`` and ``beep`` default to what is already stored, so the vendor
        app keeps the user's reminder choice.
        """
        current = await self.get_reservation()
        new = m.Reservation(
            int(m.ReservationMode(mode)), hour, minute,
            current.alarm if alarm is None else bool(alarm),
            current.beep if beep is None else int(m.Beep(beep)),
            True,
        )
        await self._set(m.CMD_SET_RESERVATION, new.payload())
        return await self.get_reservation()

    async def cancel_reservation(self) -> m.Reservation:
        """Switch the stored reservation off (the vendor app resends it with
        the last byte 0). Always safe to call."""
        current = await self.get_reservation()
        off = m.Reservation(current.mode if current.mode in tuple(m.ReservationMode) else 0,
                            current.hour, current.minute, current.alarm, current.beep, False)
        await self._set(m.CMD_SET_RESERVATION, off.payload())
        return await self.get_reservation()

    # -- brewing: manual starts are explicit calls (reservations above start
    #    the machine by itself at the scheduled time)
    async def start(self, mode: m.BrewMode) -> None:
        await self._set(m.CMD_BREW, m.brew_payload(mode, True))

    async def stop(self) -> None:
        """Stop whatever is running; if the state is unknown, stop every mode."""
        state = self.status.state if self.status else None
        try:
            modes = [m.STATE_TO_MODE[m.State(state)]]
        except (KeyError, ValueError):
            modes = list(m.BrewMode)
        for mode in modes:
            await self._set(m.CMD_BREW, m.brew_payload(mode, False))
