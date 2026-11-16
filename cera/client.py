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
PRE_HANDSHAKE_PAUSE = 1.0  # the vendor app pauses ~0.7 s before its first command
RETRY_PAUSE = 2.0          # between connection attempts
REPLY_TIMEOUT = 0.5        # the vendor app re-sends after ~0.4 s without a reply
STATS_TIMEOUT = 3.0        # the stats table arrives in ~21 fragments
RETRIES = 2
HEARTBEAT_WAIT = 1.0       # per status request, before it is sent again
HEARTBEAT_REQUESTS = 3

# The vendor app's slider reaches 38 C, and the CMD_LIMITS reply agrees: it starts
# at 0x64 (100 F = 37.8 C) and tops out at 0xCC (204 F = 95.6 C).
MIN_TARGET_C = 38
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
        self._target = address_or_device
        self._client = None  # a new BleakClient per connection attempt
        self._ready = False  # True between a finished handshake and the disconnect
        self._on_status = on_status
        self._on_frame = on_frame
        self._on_disconnect = on_disconnect
        self._waiters: Dict[int, "asyncio.Future[Frame]"] = {}
        self._cmd_locks: Dict[int, asyncio.Lock] = {}
        self._reset()

    def _reset(self) -> None:
        """Forget everything learned from the previous connection."""
        self._reasm = Reassembler()
        self.status: Optional[m.Status] = None
        self.info: Optional[m.DeviceInfo] = None
        self.settings: Optional[m.Settings] = None
        self.target_f: Optional[int] = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def target_c(self) -> Optional[float]:
        return None if self.target_f is None else round(m.f_to_c(self.target_f), 1)

    def _lost(self, client) -> None:
        # Failed connection attempts and replaced clients are not a lost
        # session: only report the drop of a connection that was handed out.
        if client is not self._client or not self._ready:
            return
        self._ready = False
        if self._on_disconnect:
            self._on_disconnect()

    async def __aenter__(self) -> "CeraClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # -- connection ------------------------------------------------------
    async def connect(self, attempts: int = 4) -> None:
        """Connect and run the handshake, reconnecting if the machine stays silent.

        On Windows the machine sometimes ignores everything on a fresh
        connection (no replies at all), or the link drops while settling. A
        new connection fixes it, resending does not. Each attempt uses a new
        BleakClient: retrying on the same one skips bleak's scan and, in
        testing, stayed silent on every retry, while a new client recovered.

        Whatever goes wrong, the connection is closed before this raises, and
        only CeraError is raised (bleak and OS errors are wrapped).
        """
        from bleak import BleakClient  # lazy: codec/tests need no bleak
        from bleak.exc import BleakError

        if self._client is not None:
            await self._close()
        for attempt in range(attempts):
            # After a failure connect by address even if a BLEDevice was
            # given, so bleak scans again and only connects once the machine
            # is advertising, i.e. the old link is really gone.
            target = self._target if attempt == 0 else getattr(self._target, "address", self._target)
            self._reset()
            self._client = BleakClient(target, disconnected_callback=self._lost)
            ok = False
            try:
                await self._connect_once()
                ok = True
                self._ready = True
                return
            except (ValueError, IndexError) as exc:
                # A reply that does not parse (another model?) will not get
                # better by reconnecting.
                raise CeraError(f"unexpected reply during the handshake: {exc}") from exc
            except (CeraError, BleakError, OSError, asyncio.TimeoutError) as exc:
                if attempt == attempts - 1:
                    if isinstance(exc, CeraError):
                        raise
                    # Do not leak bleak/OS exceptions to apps.
                    raise CeraError(f"connect failed: {exc}") from exc
            finally:
                if not ok:  # also on cancellation: never leave the link open
                    await self._close()
            # Bad connections come in runs (two in a row were seen even 8 s
            # apart), so each further attempt waits a little longer.
            await asyncio.sleep(RETRY_PAUSE * (attempt + 1))

    async def _close(self) -> None:
        self._ready = False
        try:
            await self._client.disconnect()
        except Exception:
            pass

    async def _connect_once(self) -> None:
        await self._client.connect()
        # Measured on Windows: if commands follow the connection too quickly
        # the machine often stays silent (4 of 17 trials answered); after a
        # 4 s wait 7 of 8 did. Wait before subscribing. The link sometimes
        # drops during this wait (about 1 connection in 16): stop at once.
        waited = 0.0
        while waited < SETTLE_AFTER_CONNECT:
            step = min(0.2, SETTLE_AFTER_CONNECT - waited)
            await asyncio.sleep(step)
            waited += step
            if not self._client.is_connected:
                raise CeraError("the connection dropped right after connecting")
        await self._client.start_notify(NOTIFY_UUID, self._handle)
        await asyncio.sleep(PRE_HANDSHAKE_PAUSE)
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
        # ~1 Hz heartbeat. Send it and wait for the first heartbeat; frames
        # do get lost, so ask again rather than give up the whole connection.
        for _ in range(HEARTBEAT_REQUESTS):
            await self._write(encode(m.CMD_STATUS))
            for _ in range(max(1, round(HEARTBEAT_WAIT / 0.1))):
                await asyncio.sleep(0.1)
                if self.status is not None:
                    return
        raise CeraError("no heartbeat after status request")

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            # bleak's Windows backend asserts if asked to disconnect a client
            # that never finished connecting.
            if self._client.is_connected:
                await self._client.disconnect()
        finally:
            # bleak normally reports the drop itself; make sure it is
            # reported exactly once even if it does not.
            self._lost(self._client)

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
            # Heartbeats during the handshake are not reported: the
            # connection has not been handed to the caller yet.
            if self._on_status and self._ready:
                self._on_status(self.status)
        waiter = self._waiters.get(frame.cmd)
        if waiter and not waiter.done():
            waiter.set_result(frame)

    async def request(self, cmd: int, payload: bytes = b"", timeout: Optional[float] = None) -> Frame:
        """Send a command and return the reply with the same command byte.

        Replies are matched by command byte only, so calls with the same
        command take turns; different commands may overlap.
        """
        data = encode(cmd, payload)
        if timeout is None:
            timeout = REPLY_TIMEOUT
        lock = self._cmd_locks.get(cmd)
        if lock is None:  # created here, inside the running loop (Python 3.9)
            lock = self._cmd_locks[cmd] = asyncio.Lock()
        async with lock:
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
        from bleak.exc import BleakError

        if self._client is None:
            raise CeraError("not connected")
        if self._on_frame:
            self._on_frame("TX", data)
        try:
            await self._client.write_gatt_char(WRITE_UUID, data, response=False)
        except (BleakError, OSError) as exc:
            if isinstance(exc, asyncio.TimeoutError):
                raise
            # Do not leak bleak/OS exceptions to apps.
            raise CeraError(f"write failed: {exc}") from exc

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
        """Read-modify-write, like the vendor app. The vendor app also re-sends
        the target temperature before each write; this does not."""
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
