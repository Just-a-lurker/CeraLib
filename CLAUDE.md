# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

CeraLib is a Python library that controls PCM03-family portable espresso machines (developed against a PCM03SMAX) over their local Bluetooth LE protocol. The protocol was reverse-engineered from captures; `PROTOCOL.md` is the byte-level spec, with every field marked confirmed or inferred.

## Commands

```
pip install -e .
python -m unittest discover -s tests -t .                              # all tests
python -m unittest tests.test_client.ClientTests.test_handshake        # one test
```

- `-t .` is required: tests import `tests.vectors`.
- No machine is needed. `test_codec.py` replays real captured frames. `test_client.py` drives `CeraClient` against a scripted fake `BleakClient` (it needs bleak installed, but no Bluetooth).
- CI runs Python 3.9 and 3.12, so 3.9 syntax and stdlib are the limit.
- `main` is protected: changes go through a PR with passing CI.

## Architecture

- `cera/codec.py` handles framing only: `[dir][flags][length][checksum][cmd][payload]`, the checksum, and fragment reassembly. It has no BLE code.
- `cera/messages.py` holds command bytes and typed payload parsers (`Status`, `Settings`, `Stats`, `Reservation`, `DeviceInfo`), plus the period buckets behind the charts. Each field's comment says whether it is confirmed or inferred; keep that in sync with `PROTOCOL.md` when a capture settles a question.
- `cera/client.py` is the bleak-based `CeraClient`: handshake, request/reply matching, retries.
- `cera/scan.py` holds `discover()`, which filters by advertised name because the machine does not advertise its service UUID.
- bleak is imported lazily inside `client.py` and `scan.py`, so `codec` and `messages` import without it. Keep it that way.

## Client invariants

These took hardware measurements to establish. Don't change them without new measurements.

- **Connect retries with a new BleakClient per attempt.** About 1 connection in 6 is bad (the link drops, or the machine answers nothing). Nothing sent on a silent connection wakes it, and reusing a client skips bleak's scan. A failed attempt is always closed, including on cancellation.
- **Error contract.** `connect()` raises only `CeraError`; parse errors during the handshake are wrapped too. bleak and OS errors from any call are wrapped in `CeraError`, and `CommandRejected` (a subclass) means the machine refused a set command. `ValueError` means a bad argument (target, flow, reservation fields) or, after the handshake, a reply that did not parse. CeraOpenApp relies on this split, mapping `ValueError` to HTTP 400.
- **`on_disconnect` fires once, and only for a connection that finished its handshake.** Failed attempts are not reported as drops. `on_status` is likewise silent until the handshake is done.
- **Replies are matched by command byte only.** Calls with the same command take turns through a per-command `asyncio.Lock`, created lazily inside the running loop (Python 3.9 needs that).
- **Timing knobs are module constants** (`SETTLE_AFTER_CONNECT`, `REPLY_TIMEOUT`, `RETRY_PAUSE`, …) read at call time, because the tests shrink them with `mock.patch.object`. That is why `request()` takes `timeout=None` instead of `timeout=REPLY_TIMEOUT`.
- **Writes are read-modify-write.** The settings readback (`0x17`, 12 bytes) and the write (`0x18`, 4 bytes) have different layouts, and unknown bytes are kept verbatim.
- **Two mode enums.** `BrewMode` is for `start()`. `ReservationMode` numbers modes differently, so never convert one to the other by value.

## Hardware and safety

- The machine accepts one BLE connection at a time; the vendor app must be closed.
- `start()` and `set_reservation()` make the machine heat water under pressure. A reservation starts it **by itself** later, even with nothing connected. Hardware checks should use the handshake and reads only, unless the user explicitly asks otherwise. `stop()` and `cancel_reservation()` are always safe.
- Commit only hex frame bytes (like `tests/vectors.py`). Never commit raw `btsnoop` logs or bug reports: they contain Bluetooth addresses (`.gitignore` blocks the common ones).

## Releases and dependants

- The version is in three places that must move together: `pyproject.toml`, `cera/__init__.py` (`__version__`), and the install line in `README.md`.
- [CeraOpenApp](https://github.com/Just-a-lurker/CeraOpenApp) pins CeraLib by **git tag** in its `pyproject.toml`. The tag must exist before the app's CI can pass, so merge here and tag first.
- CeraOpenApp's `android/` contains a **Kotlin port** of this protocol and client (`android/app/src/main/java/com/ceraopen/android/cera/`). Protocol or client-behaviour changes here usually need the same change there, as the 38 °C target change did.
- bleak is capped to 1.x (`<2`): only versions tested on hardware are allowed.
