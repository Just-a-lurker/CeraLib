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

    def test_status_parse(self):
        s = m.Status.parse(bytes.fromhex("013c032d0000"))
        self.assertEqual((s.battery_raw, s.water_temp_c, s.state), (0x3C, 0x2D, m.State.IDLE))


if __name__ == "__main__":
    unittest.main()
