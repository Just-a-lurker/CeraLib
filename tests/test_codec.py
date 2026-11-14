import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cera import messages as m  # noqa: E402
from cera.codec import FROM_MACHINE, TO_MACHINE, FrameError, Reassembler, decode, encode  # noqa: E402
from tests.vectors import FRAGMENT_RUNS, RX, TX  # noqa: E402


class CodecTests(unittest.TestCase):
    def test_every_captured_frame_validates(self):
        for h in TX + RX + [f for run in FRAGMENT_RUNS for f in run]:
            decode(bytes.fromhex(h))

    def test_tx_roundtrip_is_byte_identical(self):
        for h in TX:
            f = decode(bytes.fromhex(h))
            self.assertEqual(f.direction, TO_MACHINE)
            self.assertEqual(encode(f.cmd, f.payload).hex(), h)

    def test_rx_roundtrip_is_byte_identical(self):
        for h in RX:
            f = decode(bytes.fromhex(h))
            self.assertEqual(f.direction, FROM_MACHINE)
            self.assertEqual(encode(f.cmd, f.payload, f.direction, f.flags).hex(), h)

    def test_bad_checksum_and_length_rejected(self):
        good = bytearray(bytes.fromhex(TX[0]))
        bad = bytearray(good)
        bad[3] ^= 1
        with self.assertRaises(FrameError):
            decode(bytes(bad))
        with self.assertRaises(FrameError):
            decode(bytes(good[:-1]))

    def test_fragments_reassemble(self):
        for run in FRAGMENT_RUNS:
            r = Reassembler()
            out = None
            for h in run:
                out = r.feed(decode(bytes.fromhex(h)))
            self.assertIsNotNone(out)

    def test_device_info(self):
        run = next(r for r in FRAGMENT_RUNS if decode(bytes.fromhex(r[0])).cmd == m.CMD_INFO)
        r = Reassembler()
        out = None
        for h in run:
            out = r.feed(decode(bytes.fromhex(h)))
        info = m.DeviceInfo.parse(out.payload)
        self.assertEqual((info.version_a, info.version_b, info.model), ("0.0.3", "0.0.9", "PCM03SMAX"))


class ImportTests(unittest.TestCase):
    def test_client_module_compiles(self):
        # The client needs bleak only at runtime; compiling catches syntax errors.
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[1] / "cera" / "client.py"
        compile(src.read_bytes(), str(src), "exec")


class MessageTests(unittest.TestCase):
    def test_temperature_conversion(self):
        self.assertEqual(m.c_to_f(95), 0xCB)
        self.assertEqual(m.c_to_f(90), 0xC2)
        self.assertEqual(round(m.f_to_c(0xCB)), 95)
        self.assertEqual(m.target_payload(95), bytes([0xCB]))

    def test_target_frame_matches_capture(self):
        self.assertEqual(encode(m.CMD_TARGET_TEMP, m.target_payload(95)).hex(), "7f000666" "16cb")
        self.assertEqual(encode(m.CMD_TARGET_TEMP, m.target_payload(90)).hex(), "7f00065d" "16c2")

    def test_brew_frames_match_capture(self):
        cases = [
            (m.BrewMode.EXTRACT, True, "7f00078b020201"),
            (m.BrewMode.EXTRACT, False, "7f00078a020200"),
            (m.BrewMode.HEAT_AND_EXTRACT, True, "7f00078a020101"),
            (m.BrewMode.HEAT_AND_EXTRACT, False, "7f000789020100"),
            (m.BrewMode.HEAT, True, "7f00078902" "0001"),
            (m.BrewMode.HEAT, False, "7f00078802" "0000"),
        ]
        for mode, on, want in cases:
            self.assertEqual(encode(m.CMD_BREW, m.brew_payload(mode, on)).hex(), want)

    def test_settings_roundtrip(self):
        raw = bytes.fromhex("0800000a00000a781efa010f")
        self.assertEqual(len(raw), 12)
        s = m.Settings.parse(raw)
        self.assertEqual(s.write_payload().hex(), "08000078")
        self.assertEqual(s.extraction_seconds, 120)  # raw 0x78
        self.assertEqual(m.Settings.parse(bytes.fromhex("0800000a00000a8c1efa010f")).extraction_seconds, 130)

    def test_flow_range(self):
        self.assertEqual((m.FLOW_MIN, m.FLOW_DEFAULT, m.FLOW_MAX), (1, 8, 15))
        for flow, hexed in [(1, "01"), (2, "02"), (8, "08"), (14, "0e"), (15, "0f")]:
            s = m.Settings.parse(bytes.fromhex(hexed + "00000a00000a781efa010f"))
            self.assertEqual(s.flow, flow)
            self.assertEqual(s.write_payload().hex(), hexed + "000078")

    def test_stats(self):
        import datetime as dt
        run = next(r for r in FRAGMENT_RUNS if decode(bytes.fromhex(r[0])).cmd == m.CMD_STATS)
        r = Reassembler()
        for h in run:
            out = r.feed(decode(bytes.fromhex(h)))
        s = m.Stats.parse(out.payload)
        today = dt.date(2026, 11, 14)  # Saturday, the capture date
        self.assertEqual(len(s.daily), 313)
        self.assertEqual(s.today, 1)
        self.assertEqual(s.week(today), 2)                # default: Sunday start, like the app
        self.assertEqual(s.week(today, week_start=6), 2)  # Sunday start
        self.assertEqual(s.week(today, week_start=0), 1)  # Monday start
        self.assertEqual(s.month(today), 3)
        self.assertEqual(s.year(today), 208)
        self.assertEqual(s.by_date(today)[dt.date(2026, 11, 8)], 1)

    def test_stats_periods(self):
        import datetime as dt
        # today = Sat 14 Nov 2026; daily[0] is today, 20 days stored (back to 26 Oct).
        daily = (2, 0, 0, 0, 0, 0, 1, 1) + (0,) * 6 + (5,) + (1,) * 5
        s = m.Stats(b"9", daily)
        today = dt.date(2026, 11, 14)
        self.assertEqual(s.oldest(today), dt.date(2026, 10, 26))

        week = s.week_series(today)
        self.assertEqual(week[0].start, dt.date(2026, 11, 8))      # Sunday
        self.assertEqual([b.cups for b in week], [1, 0, 0, 0, 0, 0, 2])
        last_week = s.week_series(today, offset=-1)
        self.assertEqual(last_week[0].start, dt.date(2026, 11, 1))
        self.assertEqual(sum(b.cups for b in last_week), s.daily[7] + sum(s.daily[8:14]))

        nov = s.month_series(today)
        self.assertEqual(len(nov), 30)
        self.assertEqual(sum(b.cups or 0 for b in nov), s.month(today))
        self.assertIsNone(nov[20].cups)                             # 21 Nov: future
        self.assertTrue(nov[20].complete)
        octo = s.month_series(today, offset=-1)
        self.assertIsNone(octo[0].cups)                             # 1 Oct: not stored
        self.assertFalse(octo[0].complete)
        self.assertEqual(octo[25].cups, 1)                          # 26 Oct: oldest stored

        year = s.year_series(today)
        self.assertEqual(len(year), 12)
        self.assertEqual(year[10].cups, s.month(today))             # November
        self.assertEqual(year[9].cups, 5 + 1 * 5)                   # October, partial
        self.assertFalse(year[9].complete)
        self.assertIsNone(year[0].cups)                             # January: not stored
        self.assertIsNone(year[11].cups)                            # December: future
        self.assertTrue(all(b.cups is None for b in s.year_series(today, offset=-1)))

    def test_status_parse(self):
        s = m.Status.parse(bytes.fromhex("013c032d0000"))
        self.assertEqual((s.battery_raw, s.water_temp_c, s.state), (0x3C, 0x2D, m.State.IDLE))


if __name__ == "__main__":
    unittest.main()
