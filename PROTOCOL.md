# PCM03-family BLE protocol (PCM03SMAX)

Derived from Bluetooth captures of the vendor app talking to one PCM03SMAX
(device info: model `PCM03SMAX`, version strings `0.0.3` and `0.0.9`).
Each field is marked **confirmed** (tied to a known user action) or
**inferred** (fits the data, not isolated). Nothing here is encrypted or
authenticated, and no pairing was needed.

## GATT

| Role | UUID (short) | Direction |
|---|---|---|
| Service | `FFF0` | |
| Notify | `FFF1` (`0000fff1-0000-1000-8000-00805f9b34fb`) | machine -> app |
| Write without response | `FFF2` (`0000fff2-…`) | app -> machine |

Enable notifications on `FFF1` first. Address characteristics by UUID, not by
the handles seen in captures (`0x000e`, `0x0011`). Replies arrive in 20-byte
notifications even though the MTU is negotiated to 247. The machine most
likely accepts a single connection, so close the vendor app first.

## Frame (confirmed)

```
[dir] [flags] [length] [checksum] [cmd] [payload ...]
```

- `dir`: `7F` app -> machine, `7E` machine -> app.
- `flags`: `00` single frame; `40` first, `80` middle, `C0` last fragment.
- `length`: total bytes of this frame, header included.
- `checksum`: (sum of all other bytes in the frame) mod 256.
- Fragments are frames of their own (own header and checksum, same `cmd`).
  The reply is the concatenation of their payloads.
- Most set commands are acknowledged by a reply with the same `cmd` and
  payload `01`. `0x16` echoes the value instead.
- If there is no reply within ~0.4 s the vendor app re-sends the command.
- Right after connecting, the machine sometimes ignores commands entirely.
  The vendor app hits this too: in one capture it re-sent the clock command
  for ~12 s and reconnected before getting a reply.

## Connect sequence used by the vendor app (confirmed)

enable notify -> `04` clock -> `15` -> `17` -> (`24`) -> `08` -> `06 00`
-> empty `00` (starts the ~1 Hz heartbeat).

## Commands

| cmd | Payload | Meaning | Confidence |
|---|---|---|---|
| `00` | empty | request status; machine then pushes `01 <b> 03 <temp C> <state> 00` about once per second | confirmed |
| `02` | `[mode, on]` | start (`on=1`) / stop (`on=0`) a brew mode | confirmed |
| `04` | `[year-2020, month, day, weekday, hour, min, sec]` | set clock; weekday Saturday = 6 (Sunday unverified) | confirmed |
| `06` | `00` | cup statistics: cups per day, newest first (below); 21 fragments | confirmed |
| `08` | empty | device info: `[?] len+"0.0.3" len+"0.0.9" len+"PCM03SMAX" 06 02 02 02 02 02` | confirmed |
| `15` | empty | reply `64 CC <F>`: last byte equals the current target in °F; other bytes maybe limits | inferred |
| `16` | `[target °F]` | set target water temperature; 90 °C = `C2`, 95 °C = `CB` | confirmed |
| `17` | empty | read settings block, 12 bytes (below) | confirmed |
| `18` | `[flow, presoak_s, standstill_s, extraction]` | write settings (4 bytes, differs from the readback layout) | confirmed |
| `23` | `[mode, hour, minute, alarm, beep, on]` | set (`on=1`) or cancel (`on=0`) the reservation; reply `01` | confirmed |
| `24` | empty | read the stored reservation (same 6 bytes) | confirmed |
| `19` | empty | seen when the vendor app's history page opens; reply `01`; meaning not established | unknown |

### Brew modes (`02`)

| mode | Meaning | Heartbeat state while running |
|---|---|---|
| `00` | heat only | `05` |
| `01` | heat and extract | `01` |
| `02` | extract only | `03` |

Stop = same mode with `on=0`. Whether a stop for a different mode also stops a
running one was not observed. Heartbeat state `00` is idle.

### Heartbeat (`00`)

`01 <b> 03 <temp> <state> 00` where `temp` is the water temperature in **°C**
(confirmed, rises while heating; reached 97 °C with a 95 °C target) and `<b>`
was `3C` while the app showed 3 of 4 battery bars (**inferred**: battery).

### Settings readback (`17`, 12 bytes) vs write (`18`, 4 bytes)

```
17 reply:  [flow] [A] [00] [0a] [B] [00] [0a] [ext] [1e] [fa] [01] [0f]
18 write:  [flow] [A] [B] [ext]
```

- `flow`: the app's flow slider, whole steps from `1` (min) to `15` (max);
  `8` is the middle and the default (confirmed: min 1, min+1 2, middle 8,
  max-1 14, max 15).
- `A` presoak and `B` standstill, in seconds (confirmed: presoak 0 -> 1 s changed `A`, standstill 0 -> 1 s changed `B`).
- `ext`: extraction time. The app showed 120 s -> 130 s while `ext` went `78` (120) -> `8C` (140), so `seconds = ext/2 + 60`
  (**inferred from two points**; needs a third value to confirm).
- Remaining bytes (`0a`, `1e fa 01 0f`, the `00`s) are unknown. A write must
  keep the others unchanged, so read first and modify one field. The vendor
  app also re-sends `16` (target) before every `18`.

### Cup statistics (`06`)

Reply payload: `01 39` (not decoded) followed by one byte per day, today
first, then yesterday, and so on (313 days on a PCM03SMAX, about 10 months).
"Today" follows the machine's clock, which the app sets on connect.

Confirmed against the vendor app's totals: week (Sunday to Saturday), month
and year are sums of these days. The count went up by one after a completed
brew.

### Reservation (`23` / `24`)

```
[mode] [hour] [minute] [alarm] [beep] [on]
mode:   0 = heat & extract, 1 = heating, 2 = extract only
        (numbered differently from the manual start command 02)
hour, minute: 24-hour clock, machine time
alarm:  0 = silent, 1 = alarm        stored only; the phone plays the sound
beep:   0 ding dong, 1 di di, 2 bu gu, 3 bi bi   stored only
on:     1 = scheduled, 0 = off
```

Confirmed: the machine starts **by itself** at that time on its own clock
(observed with the phone sending no start command), runs once, then switches
the reservation off (read back with `on=0`). Cancelling resends the same
values with `on=0`. The machine stores one reservation and remembers it
across connections. When a time already passed today is set, the run is
assumed to happen the next day (not yet observed).

## Temperature units

Target (`16`, `15`) is in °F: `raw = round(°C × 9/5 + 32)`. The heartbeat
temperature is in °C.
