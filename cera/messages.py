"""Typed views of the commands seen in captures of a PCM03SMAX.

Confidence is marked per field: "confirmed" means it was tied to a known user
action in a capture, "inferred" means it fits the data but was not isolated.
"""

from __future__ import annotations

import datetime as _dt
import enum
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Command bytes.
CMD_STATUS = 0x00        # heartbeat (machine pushes ~1/s); app sends it empty on connect
CMD_BREW = 0x02          # payload [mode, on]
CMD_SET_CLOCK = 0x04     # payload [year-2020, month, day, weekday, hour, minute, second]
CMD_STATS = 0x06         # cup counts per day; request payload [0x00]; reply in ~21 fragments
CMD_TABLE = CMD_STATS    # kept for compatibility
CMD_INFO = 0x08          # device info, fragmented
CMD_LIMITS = 0x15        # reply [0x64?, 0xCC, target_F]; layout partly inferred
CMD_TARGET_TEMP = 0x16   # payload [target in degrees Fahrenheit]; reply echoes it
CMD_GET_SETTINGS = 0x17  # request empty, reply 12-byte settings block
CMD_SET_SETTINGS = 0x18  # payload [flow, presoak_s, standstill_s, extraction_raw]
CMD_HISTORY = 0x19       # seen in captures, meaning unconfirmed; not exposed
CMD_SET_RESERVATION = 0x23  # payload = Reservation (6 bytes); reply 01
CMD_GET_RESERVATION = 0x24  # request empty; reply = Reservation (6 bytes)
CMD_RESERVATION = CMD_SET_RESERVATION  # kept for compatibility
CMD_QUERY_24 = CMD_GET_RESERVATION     # kept for compatibility

STATUS_OK = b"\x01"      # reply payload for accepted set commands (confirmed)


class State(enum.IntEnum):
    """Heartbeat state byte (confirmed from brew-mode captures)."""

    IDLE = 0
    HEAT_AND_EXTRACT = 1
    EXTRACT = 3
    HEAT = 5


class BrewMode(enum.IntEnum):
    """First payload byte of CMD_BREW (confirmed)."""

    HEAT = 0
    HEAT_AND_EXTRACT = 1
    EXTRACT = 2


class ReservationMode(enum.IntEnum):
    """Mode byte of a reservation (confirmed). Numbered differently from
    BrewMode: here 0 is heat & extract and 1 is heating."""

    HEAT_AND_EXTRACT = 0
    HEAT = 1
    EXTRACT = 2


class Beep(enum.IntEnum):
    """Reminder sound chosen in the vendor app (confirmed values). The phone
    plays it, not the machine; the machine only stores the choice."""

    DING_DONG = 0
    DI_DI = 1
    BU_GU = 2
    BI_BI = 3


@dataclass(frozen=True)
class Reservation:
    """A scheduled start stored on the machine (CMD_SET/GET_RESERVATION).

    Layout: ``[mode, hour, minute, alarm, beep, enabled]`` (all confirmed).
    The machine starts by itself at hour:minute on its own clock, runs once,
    then switches the reservation off. ``alarm`` and ``beep`` are only stored
    for phone apps, which play the reminder; the machine does not use them.
    """

    mode: int        # ReservationMode
    hour: int
    minute: int
    alarm: bool      # False = silent reminder
    beep: int        # Beep, ignored when alarm is False
    enabled: bool

    @classmethod
    def parse(cls, payload: bytes) -> "Reservation":
        if len(payload) != 6:
            raise ValueError(f"reservation payload must be 6 bytes, got {len(payload)}")
        return cls(payload[0], payload[1], payload[2], bool(payload[3]), payload[4], bool(payload[5]))

    def payload(self) -> bytes:
        if not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59:
            raise ValueError("time must be 00:00-23:59")
        if self.mode not in tuple(ReservationMode):
            raise ValueError(f"unknown reservation mode {self.mode}")
        if not 0 <= self.beep <= 255:
            raise ValueError("beep out of range")
        return bytes([self.mode, self.hour, self.minute, int(self.alarm), self.beep, int(self.enabled)])

    @property
    def time(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def next_run(self, now: _dt.datetime) -> _dt.datetime:
        """When it will run: today at hour:minute if still ahead, otherwise
        tomorrow (both confirmed on a PCM03SMAX: a time already passed today
        runs the next day)."""
        at = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        return at if at > now else at + _dt.timedelta(days=1)


#: Heartbeat state while running -> the CMD_BREW mode that stops it (inferred
#: from the matching start/stop pairs; stop for a mode other than the one
#: started has not been observed).
STATE_TO_MODE = {
    State.HEAT: BrewMode.HEAT,
    State.HEAT_AND_EXTRACT: BrewMode.HEAT_AND_EXTRACT,
    State.EXTRACT: BrewMode.EXTRACT,
}

# Flow values seen: 0x08 = "normal", 0x0F = "high" (confirmed), 0x09 (capture 1).
# Flow is the vendor app's slider: whole steps from 1 to 15, middle 8 is the
# default (confirmed: min=1, min+1=2, middle=8, max-1=14, max=15).
FLOW_MIN = 1
FLOW_MAX = 15
FLOW_DEFAULT = 8
FLOW_NORMAL = FLOW_DEFAULT  # kept for compatibility
FLOW_HIGH = FLOW_MAX        # kept for compatibility


def c_to_f(celsius: float) -> int:
    return round(celsius * 9 / 5 + 32)


def f_to_c(fahrenheit: int) -> float:
    return (fahrenheit - 32) * 5 / 9


@dataclass(frozen=True)
class Status:
    """CMD_STATUS heartbeat payload: ``01 <b> 03 <temp> <state> 00``."""

    raw: bytes
    battery_raw: int      # inferred: 0x3C (60) while the app showed 3 of 4 bars
    water_temp_c: int     # confirmed (rises while heating); degrees Celsius
    state: int            # see State; unknown values are kept as ints

    @classmethod
    def parse(cls, payload: bytes) -> "Status":
        if len(payload) < 6:
            raise ValueError(f"status payload too short: {payload.hex()}")
        return cls(bytes(payload), payload[1], payload[3], payload[4])


@dataclass(frozen=True)
class Settings:
    """CMD_GET_SETTINGS reply (12 bytes).

    The write layout (CMD_SET_SETTINGS, 4 bytes) differs from this readback.
    Unknown bytes are kept verbatim in ``raw`` so a write can round-trip them.
    """

    raw: bytes
    flow: int             # confirmed: slider step FLOW_MIN..FLOW_MAX (1-15), default 8
    presoak_s: int        # confirmed: presoak seconds (0 -> 1 s)
    standstill_s: int     # confirmed: standstill seconds (0 -> 1 s)
    extraction_raw: int   # confirmed to change with extraction time; see extraction_seconds

    @classmethod
    def parse(cls, payload: bytes) -> "Settings":
        if len(payload) != 12:
            raise ValueError(f"settings payload must be 12 bytes, got {len(payload)}")
        return cls(bytes(payload), payload[0], payload[1], payload[4], payload[7])

    @property
    def extraction_seconds(self) -> float:
        """INFERRED from two points only: raw 120 -> 120 s, raw 140 -> 130 s."""
        return self.extraction_raw / 2 + 60

    def write_payload(self) -> bytes:
        return bytes([self.flow, self.presoak_s, self.standstill_s, self.extraction_raw])


@dataclass(frozen=True)
class DeviceInfo:
    version_a: str        # "0.0.3" in the capture (hardware or firmware; unresolved)
    version_b: str        # "0.0.9"
    model: str            # "PCM03SMAX"
    tail: bytes

    @classmethod
    def parse(cls, payload: bytes) -> "DeviceInfo":
        pos = 1  # first byte meaning unknown (0x18 in the capture)
        strings: List[str] = []
        for _ in range(3):
            n = payload[pos]
            strings.append(payload[pos + 1 : pos + 1 + n].decode("ascii"))
            pos += 1 + n
        return cls(strings[0], strings[1], strings[2], bytes(payload[pos:]))


@dataclass(frozen=True)
class Bucket:
    """One bar of a stats chart: a day (week/month view) or a month (year view).

    ``cups`` is None when the machine has no data for the whole bucket (before
    its oldest stored day, or in the future). ``complete`` is False when part
    of the bucket is older than the stored history, so the count may be low.
    """

    start: _dt.date
    cups: Optional[int]
    complete: bool


def _add_months(year: int, month: int, delta: int) -> Tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _month_days(year: int, month: int) -> int:
    ny, nm = _add_months(year, month, 1)
    return (_dt.date(ny, nm, 1) - _dt.date(year, month, 1)).days


@dataclass(frozen=True)
class Stats:
    """CMD_STATS reply: cups brewed per day, newest first.

    Confirmed against the vendor app (week, month and year totals match):
    payload[2] is the machine's current day, payload[3] the day before, and so
    on (313 days on a PCM03SMAX). payload[0:2] (``01 39``) is not decoded.
    "Today" is the machine's clock, which the client sets on connect.
    """

    header: bytes
    daily: Tuple[int, ...]

    @classmethod
    def parse(cls, payload: bytes) -> "Stats":
        if len(payload) < 3:
            raise ValueError(f"stats payload too short: {len(payload)}")
        return cls(bytes(payload[:2]), tuple(payload[2:]))

    @property
    def today(self) -> int:
        return self.daily[0]

    @property
    def total(self) -> int:
        """All cups in the stored days (not a lifetime total)."""
        return sum(self.daily)

    def by_date(self, today: _dt.date) -> Dict[_dt.date, int]:
        return {today - _dt.timedelta(days=i): n for i, n in enumerate(self.daily)}

    def _last(self, days: int) -> int:
        return sum(self.daily[:days])

    def week(self, today: _dt.date, week_start: int = 6) -> int:
        """Cups since the start of this week. ``week_start`` uses Python's
        weekday numbers: 6 = Sunday (default, like the vendor app, whose week
        runs Sunday to Saturday), 0 = Monday."""
        return self._last((today.weekday() - week_start) % 7 + 1)

    def month(self, today: _dt.date) -> int:
        return self._last(today.day)

    def year(self, today: _dt.date) -> int:
        """Cups since 1 January, limited to the days the machine stores."""
        return self._last(today.timetuple().tm_yday)

    # -- periods for charts ---------------------------------------------
    def oldest(self, today: _dt.date) -> _dt.date:
        return today - _dt.timedelta(days=len(self.daily) - 1)

    def day(self, today: _dt.date, date: _dt.date) -> Optional[int]:
        """Cups on ``date``; None if it is in the future or not stored."""
        i = (today - date).days
        if i < 0 or i >= len(self.daily):
            return None
        return self.daily[i]

    def _bucket(self, today: _dt.date, start: _dt.date, days: int) -> Bucket:
        counts, missing = [], False
        for k in range(days):
            d = start + _dt.timedelta(days=k)
            if d > today:
                continue  # not happened yet: neither data nor missing
            n = self.day(today, d)
            if n is None:
                missing = True
            else:
                counts.append(n)
        return Bucket(start, sum(counts) if counts else None, not missing)

    def week_series(self, today: _dt.date, offset: int = 0, week_start: int = 6) -> List[Bucket]:
        """Seven day buckets for the week ``offset`` weeks from this one
        (0 = this week, -1 = last week). Sunday start by default."""
        start = today - _dt.timedelta(days=(today.weekday() - week_start) % 7 - 7 * offset)
        return [self._bucket(today, start + _dt.timedelta(days=k), 1) for k in range(7)]

    def month_series(self, today: _dt.date, offset: int = 0) -> List[Bucket]:
        """One bucket per date of the month ``offset`` months from this one."""
        y, mo = _add_months(today.year, today.month, offset)
        first = _dt.date(y, mo, 1)
        return [self._bucket(today, first + _dt.timedelta(days=k), 1)
                for k in range(_month_days(y, mo))]

    def year_series(self, today: _dt.date, offset: int = 0) -> List[Bucket]:
        """Twelve month buckets for the year ``offset`` years from this one."""
        y = today.year + offset
        return [self._bucket(today, _dt.date(y, mo, 1), _month_days(y, mo)) for mo in range(1, 13)]


def clock_payload(year: int, month: int, day: int, weekday: int,
                  hour: int, minute: int, second: int) -> bytes:
    """``weekday`` follows the capture: Saturday 2026-11-14 was sent as 6."""
    return bytes([year - 2020, month, day, weekday, hour, minute, second])


def brew_payload(mode: BrewMode, on: bool) -> bytes:
    return bytes([int(mode), 1 if on else 0])


def target_payload(celsius: float) -> bytes:
    f = c_to_f(celsius)
    if not 0 <= f <= 255:
        raise ValueError("target out of range")
    return bytes([f])
