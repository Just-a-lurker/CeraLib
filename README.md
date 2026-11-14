# CeraLib

Open Python library for controlling PCM03-family portable espresso machines
(developed against a PCM03SMAX) over the machine's local Bluetooth LE
protocol, without the vendor app or cloud.

> **Not affiliated with CERA+.** This is an independent community project. It
> is not made, endorsed or supported by CERA+ or the makers of the HappyGo
> app. "CERA+" and product names are used only to say which machines this
> works with and belong to their owners.

The protocol was worked out from Bluetooth captures of the author's own
machine talking to its app. No vendor code, firmware or assets are included.
See [PROTOCOL.md](PROTOCOL.md).

## Status

- `cera/codec.py`, `cera/messages.py`: tested against every frame in the
  captures (`python -m unittest discover -s tests -t .`).
- `cera/client.py` (bleak): read-only use (connect, device info, settings,
  status heartbeat) verified on a PCM03SMAX from Windows. Setters, `start()`
  and `stop()` are **not yet tested on hardware.** Known quirk: the machine
  often ignores commands sent right after connecting, so the client waits 4 s
  before subscribing and reconnects if it stays silent; even so about 1
  connect in 5 still failed in testing.
- Presoak and standstill are `presoak_s` / `standstill_s` (confirmed).
  Extraction time is `extraction_raw`; the seconds formula (`raw/2 + 60`) is
  inferred from two points only.
- `get_stats()` reads cups per day (about 10 months) with week, month and
  year totals; the format is confirmed against the vendor app.
- Not supported on purpose: firmware update, reservations.

## Safety and warranty

This machine heats water and builds pressure. Nothing starts automatically:
brewing and heating only happen through explicit calls (`start()`), and
`stop()` stops whichever mode is running. Never leave it running unattended.

Use at your own risk. Controlling the machine with third-party software may
void its warranty, and this software comes with no warranty of any kind (see
[LICENSE](LICENSE)).

## Install

```
pip install git+https://github.com/Just-a-lurker/CeraLib.git
```

Requires Python 3.9+ and [bleak](https://github.com/hbldh/bleak) 1.x, the
version tested on hardware. Newer bleak major versions are not allowed until
they have been tested.

To use a fixed release, install a tag, for example
`pip install git+https://github.com/Just-a-lurker/CeraLib.git@v0.1.0`.

## Use

```python
import asyncio
from cera import discover
from cera.client import CeraClient

async def main():
    machines = await discover(only_likely=True)
    async with CeraClient(machines[0].address) as c:
        print(c.info, c.status, await c.get_settings())

asyncio.run(main())
```

Close the vendor app first: the machine accepts one connection at a time.

## Contributing

Captures from other models are welcome. Share only the hex values of the
frames, never raw `btsnoop` logs or bug reports: they contain Bluetooth
addresses and other private traffic. Run the tests before opening a pull
request.

## Apps using CeraLib

- [CeraOpenApp](https://github.com/Just-a-lurker/CeraOpenApp): local test app with a browser UI.
