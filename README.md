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
The full byte-level description is in [PROTOCOL.md](PROTOCOL.md), with each
field marked confirmed or inferred.

## What it can do

| Feature | API | Status |
|---|---|---|
| Find machines | `cera.discover()` | Verified on hardware |
| Connect, device info, live status (water °C, state, battery) | `CeraClient`, `.info`, `.status`, `on_status` | Verified on hardware |
| Target temperature (38–95 °C) | `set_target_temp_c()` | Verified on hardware |
| Recipe: flow 1–15, presoak, standstill, extraction time | `get_settings()`, `update_settings()` | Verified on hardware |
| Start and stop: heat, extract, heat & extract | `start()`, `stop()` | Verified on hardware |
| Cup history: per day, week, month and year | `get_stats()`, `Stats.week_series()` and friends | Verified on hardware, matches the vendor app |
| Schedule a start the machine runs by itself | `get_reservation()`, `set_reservation()`, `cancel_reservation()` | Format confirmed from captures; self-start, run-once and next-day behaviour confirmed on the machine |
| Firmware update | – | Not supported, on purpose |

Some values are inferred rather than confirmed: the battery scale (the raw
value looks like a percentage) and the extraction time in seconds
(`raw/2 + 60`, from two data points). See the open questions in
[PROTOCOL.md](PROTOCOL.md#open-questions).

**Known quirk:** right after connecting, the machine sometimes ignores every
command, or the link drops within a few seconds. The vendor app hits this
too. Measured on Windows: about 1 connection in 6 is bad, nothing sent on a
silent connection wakes it (re-sending for 8 s, re-subscribing), and bad
connections come in runs. So the client waits 4 s before subscribing and
makes up to four attempts (`connect(attempts=4)`), pausing 2 s, 4 s, then
6 s between them. A failed attempt's connection is closed and discarded, and
the next attempt builds a new bleak client: reusing one skips bleak's scan,
while a new one only connects once the machine is advertising again, so the
old link is really gone. A failed `connect()` can therefore take up to a
minute; it closes the connection before raising, and raises only `CeraError`.

Only `connect()` retries. A connection that drops later is reported through
`on_disconnect` and is not re-established: call `connect()` again.

## Safety and warranty

This machine heats water and builds pressure. Manual brewing and heating only
happen through explicit calls (`start()`), and `stop()` stops whichever mode is
running. A reservation (`set_reservation()`) makes the machine start **by
itself** at the scheduled time, even with no app connected: only set one when
water and a cup will be in place. Never leave it running unattended.

Use at your own risk. Controlling the machine with third-party software may
void its warranty, and this software comes with no warranty of any kind (see
[LICENSE](LICENSE)).

## Install

```
pip install git+https://github.com/Just-a-lurker/CeraLib.git@v0.4.1
```

Requires Python 3.9+ and [bleak](https://github.com/hbldh/bleak) 1.x, the
version tested on hardware. Newer bleak major versions are not allowed until
they have been tested. Pin a release tag as above; `main` may change.

## Use

Close the vendor app first: the machine accepts one connection at a time.

```python
import asyncio
import datetime as dt

from cera import BrewMode, ReservationMode, discover
from cera.client import CeraClient


async def main():
    machines = await discover(only_likely=True)
    async with CeraClient(machines[0].address) as c:
        print(c.info)                      # model and versions
        print(c.status)                    # water_temp_c, state, battery_raw

        # Recipe
        await c.set_target_temp_c(93)
        await c.update_settings(flow=8, presoak_s=1, standstill_s=0)

        # Cup history
        stats = await c.get_stats()
        today = dt.date.today()
        print(stats.today, stats.week(today), stats.month(today), stats.year(today))

        # Schedule: the machine starts BY ITSELF at 07:30, once
        await c.set_reservation(ReservationMode.HEAT_AND_EXTRACT, 7, 30)
        await c.cancel_reservation()

        # Manual brewing: always explicit, always stoppable
        # await c.start(BrewMode.EXTRACT)
        # await c.stop()


asyncio.run(main())
```

Reservations and manual starts number their modes differently: use
`ReservationMode` for schedules and `BrewMode` for `start()`.

## Development

```
pip install -e .
python -m unittest discover -s tests -t .
```

The tests run without a machine: they replay frames from real captures
(`tests/vectors.py` holds only the protocol bytes). `main` is protected;
changes go through a pull request with passing CI.

## Contributing

Captures from other models, and answers to the
[open questions](PROTOCOL.md#open-questions), are very welcome. Share only the
hex values of the frames, never raw `btsnoop` logs or bug reports: they
contain Bluetooth addresses and other private traffic.

## Apps using CeraLib

- [CeraOpenApp](https://github.com/Just-a-lurker/CeraOpenApp): local app with a browser UI.

## License

MIT, see [LICENSE](LICENSE).
