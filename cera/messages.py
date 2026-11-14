"""Typed views of the commands seen in captures of a PCM03SMAX.

Confidence is marked per field: "confirmed" means it was tied to a known user
action in a capture, "inferred" means it fits the data but was not isolated.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import List, Tuple

# Command bytes.
CMD_STATUS = 0x00        # heartbeat (machine pushes ~1/s); app sends it empty on connect
CMD_BREW = 0x02          # payload [mode, on]
CMD_SET_CLOCK = 0x04     # payload [year-2020, month, day, weekday, hour, minute, second]
CMD_TABLE = 0x06         # multi-fragment table; request payload [0x00]; layout unknown
CMD_INFO = 0x08          # device info, fragmented
CMD_LIMITS = 0x15        # reply [0x64?, 0xCC, target_F]; layout partly inferred
CMD_TARGET_TEMP = 0x16   # payload [target in degrees Fahrenheit]; reply echoes it
CMD_GET_SETTINGS = 0x17  # request empty, reply 12-byte settings block
CMD_SET_SETTINGS = 0x18  # payload [flow, presoak_s, standstill_s, extraction_raw]
CMD_HISTORY = 0x19       # seen in captures, meaning unconfirmed; not exposed
CMD_RESERVATION = 0x23   # seen in captures, meaning unconfirmed; not exposed
CMD_QUERY_24 = 0x24      # read-only, meaning unconfirmed

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
