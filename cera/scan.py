"""Find nearby machines without importing bleak in the calling app."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

# Advertised names observed so far: the PCM03SMAX tested advertised as
# "Hi-t-<id>_<last 4 hex of address>". Please report other names.
KNOWN_NAME_PREFIXES = ("Hi-t-", "PCM03")


@dataclass(frozen=True)
class Found:
    address: str
    name: str
    rssi: int

    @property
    def likely_machine(self) -> bool:
        return self.name.startswith(KNOWN_NAME_PREFIXES)


async def discover(timeout: float = 8.0, only_likely: bool = False) -> List[Found]:
    """Scan for BLE devices, strongest signal first. The machine does not
    advertise its FFF0 service, so filtering is by name only."""
    from bleak import BleakScanner

    seen = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = [
        Found(addr, adv.local_name or dev.name or "", adv.rssi)
        for addr, (dev, adv) in seen.items()
    ]
    if only_likely:
        out = [f for f in out if f.likely_machine]
    return sorted(out, key=lambda f: -f.rssi)
