"""Frame codec for the PCM03 family BLE protocol (pure functions, no BLE).

Frame layout, both directions::

    [dir] [flags] [length] [checksum] [cmd] [payload ...]

* ``dir``      0x7F = app -> machine, 0x7E = machine -> app.
* ``flags``    0x00 single frame; 0x40 first, 0x80 middle, 0xC0 last fragment.
* ``length``   total bytes in this frame, header included.
* ``checksum`` (sum of every other byte in the frame) mod 256.
* Fragments are individual frames, each with its own header and checksum.
  A fragmented reply is the concatenation of the fragment payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

TO_MACHINE = 0x7F
FROM_MACHINE = 0x7E

FLAG_SINGLE = 0x00
FLAG_FIRST = 0x40
FLAG_MIDDLE = 0x80
FLAG_LAST = 0xC0

HEADER_LEN = 5
MAX_FRAME = 255


class FrameError(ValueError):
    """Raised for frames that fail length or checksum validation."""


@dataclass(frozen=True)
class Frame:
    direction: int
    flags: int
    cmd: int
    payload: bytes

    @property
    def is_fragment(self) -> bool:
        return self.flags != FLAG_SINGLE

    def encode(self) -> bytes:
        return encode(self.cmd, self.payload, self.direction, self.flags)


def checksum(frame: bytes) -> int:
    """Checksum of a frame, ignoring the checksum byte itself (index 3)."""
    return (sum(frame) - frame[3]) & 0xFF


def encode(
    cmd: int,
    payload: bytes = b"",
    direction: int = TO_MACHINE,
    flags: int = FLAG_SINGLE,
) -> bytes:
    total = HEADER_LEN + len(payload)
    if total > MAX_FRAME:
        raise FrameError(f"frame too long: {total}")
    raw = bytearray([direction, flags, total, 0, cmd]) + bytes(payload)
    raw[3] = checksum(raw)
    return bytes(raw)


def decode(raw: bytes) -> Frame:
    raw = bytes(raw)
    if len(raw) < HEADER_LEN:
        raise FrameError(f"frame too short: {len(raw)}")
    if raw[0] not in (TO_MACHINE, FROM_MACHINE):
        raise FrameError(f"bad direction byte: 0x{raw[0]:02x}")
    if raw[2] != len(raw):
        raise FrameError(f"length byte {raw[2]} != actual {len(raw)}")
    if raw[3] != checksum(raw):
        raise FrameError(
            f"bad checksum: got 0x{raw[3]:02x}, want 0x{checksum(raw):02x}"
        )
    return Frame(raw[0], raw[1], raw[4], raw[HEADER_LEN:])


class Reassembler:
    """Collects fragments of one multi-notification reply.

    Feed every decoded frame; ``feed`` returns a complete single-frame
    equivalent when one is ready, otherwise ``None``.
    """

    def __init__(self) -> None:
        self._parts: List[bytes] = []
        self._cmd: Optional[int] = None

    def feed(self, frame: Frame) -> Optional[Frame]:
        if not frame.is_fragment:
            return frame
        if frame.flags == FLAG_FIRST:
            self._parts = [frame.payload]
            self._cmd = frame.cmd
            return None
        if self._cmd is None or frame.cmd != self._cmd:
            self._parts, self._cmd = [], None
            raise FrameError("fragment without matching first fragment")
        self._parts.append(frame.payload)
        if frame.flags == FLAG_LAST:
            done = Frame(frame.direction, FLAG_SINGLE, self._cmd, b"".join(self._parts))
            self._parts, self._cmd = [], None
            return done
        return None
