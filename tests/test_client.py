"""CeraClient against a scripted fake of bleak's BleakClient: no Bluetooth."""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bleak.exc import BleakError  # noqa: E402

from cera import client as client_mod  # noqa: E402
from cera import messages as m  # noqa: E402
from cera.client import CeraClient, CeraError  # noqa: E402
from cera.codec import FLAG_FIRST, FLAG_LAST, FLAG_MIDDLE, FROM_MACHINE, decode, encode  # noqa: E402

SETTINGS = bytes.fromhex("0800000a00000a781efa010f")
INFO = b"\x18" + b"\x050.0.3" + b"\x050.0.9" + b"\x09PCM03SMAX" + bytes.fromhex("060202020202")
HEARTBEAT = bytes.fromhex("013c031e0000")
HEARTBEAT_HEATING = bytes.fromhex("013c031e0500")


def reply(cmd, payload, flags=0):
    return encode(cmd, payload, FROM_MACHINE, flags)


class FakeMachine:
    """Shared script for every FakeBleak built during one test."""

    def __init__(self):
        self.clients = []
        self.silent_connections = 0   # this many first connections never answer
        self.drop_connections = 0     # this many first connections drop while settling
        self.settings = SETTINGS
        self.write_error = None
        self.lose_status_requests = 0
        self.replies = {
            m.CMD_SET_CLOCK: lambda p: [reply(m.CMD_SET_CLOCK, m.STATUS_OK)],
            m.CMD_LIMITS: lambda p: [reply(m.CMD_LIMITS, bytes.fromhex("64cccb"))],
            m.CMD_GET_SETTINGS: lambda p: [reply(m.CMD_GET_SETTINGS, self.settings)],
            m.CMD_INFO: lambda p: [reply(m.CMD_INFO, INFO[:10], FLAG_FIRST),
                                   reply(m.CMD_INFO, INFO[10:20], FLAG_MIDDLE),
                                   reply(m.CMD_INFO, INFO[20:], FLAG_LAST)],
            m.CMD_STATUS: self._status,
            m.CMD_BREW: lambda p: [reply(m.CMD_BREW, m.STATUS_OK)],
            m.CMD_TARGET_TEMP: lambda p: [reply(m.CMD_TARGET_TEMP, p)],
        }

    def _status(self, _payload):
        if self.lose_status_requests:
            self.lose_status_requests -= 1
            return []
        return [reply(m.CMD_STATUS, HEARTBEAT)]

    def build(self, target, disconnected_callback=None, **_kw):
        fake = FakeBleak(self, target, disconnected_callback)
        self.clients.append(fake)
        return fake


class FakeBleak:
    def __init__(self, machine, target, disconnected_callback):
        self.machine = machine
        self.target = target
        self.is_connected = False
        self._lost = disconnected_callback
        self._notify = None
        self.written = []
        index = len(machine.clients)
        self.silent = index < machine.silent_connections
        self.drops = index < machine.drop_connections

    async def connect(self, **_kw):
        self.is_connected = True
        if self.drops:
            self.is_connected = False
            self._lost(self)

    async def disconnect(self):
        if self.is_connected:
            self.is_connected = False
            self._lost(self)

    async def start_notify(self, _uuid, callback):
        if not self.is_connected:
            raise BleakError("Not connected")
        self._notify = callback

    async def write_gatt_char(self, _uuid, data, response=False):
        if not self.is_connected:
            raise BleakError("Not connected")
        if self.machine.write_error:
            raise self.machine.write_error
        self.written.append(bytes(data))
        if self.silent:
            return
        frame = decode(bytes(data))
        handler = self.machine.replies.get(frame.cmd)
        loop = asyncio.get_running_loop()
        for raw in handler(frame.payload) if handler else []:
            loop.call_soon(self._notify, None, bytearray(raw))

    def push(self, raw):
        self._notify(None, bytearray(raw))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.machine = FakeMachine()
        patches = [
            mock.patch("bleak.BleakClient", self.machine.build),
            mock.patch.object(client_mod, "SETTLE_AFTER_CONNECT", 0),
            mock.patch.object(client_mod, "PRE_HANDSHAKE_PAUSE", 0),
            mock.patch.object(client_mod, "RETRY_PAUSE", 0),
            mock.patch.object(client_mod, "REPLY_TIMEOUT", 0.02),
            mock.patch.object(client_mod, "HEARTBEAT_WAIT", 0.1),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.lost = []
        self.statuses = []

    def make(self, target="AA:BB"):
        return CeraClient(target, on_disconnect=lambda: self.lost.append(1),
                          on_status=self.statuses.append)

    async def test_handshake(self):
        c = self.make()
        await c.connect()
        self.assertTrue(c.is_connected)
        self.assertEqual(c.info.model, "PCM03SMAX")
        self.assertEqual(c.settings.flow, 8)
        self.assertEqual(c.target_c, 95.0)
        self.assertEqual(c.status.state, m.State.IDLE)
        sent = [decode(w).cmd for w in self.machine.clients[0].written]
        self.assertEqual(sent, [m.CMD_SET_CLOCK, m.CMD_LIMITS, m.CMD_GET_SETTINGS, m.CMD_INFO, m.CMD_STATUS])
        self.assertEqual(self.lost, [])

    async def test_silent_connection_retries_with_a_new_client(self):
        self.machine.silent_connections = 2
        c = self.make()
        await c.connect()
        self.assertEqual(len(self.machine.clients), 3)      # one BleakClient per attempt
        self.assertFalse(self.machine.clients[0].is_connected)
        self.assertFalse(self.machine.clients[1].is_connected)
        self.assertTrue(c.is_connected)
        self.assertEqual(self.lost, [])                      # failed attempts are not a lost session

    async def test_link_dropping_while_settling_is_retried(self):
        self.machine.drop_connections = 1
        c = self.make()
        await c.connect()
        self.assertEqual(len(self.machine.clients), 2)
        self.assertEqual(self.lost, [])

    async def test_retry_connects_by_address_so_bleak_scans_again(self):
        class Device:
            address = "AA:BB"
        self.machine.silent_connections = 1
        device = Device()
        await self.make(device).connect()
        self.assertIs(self.machine.clients[0].target, device)
        self.assertEqual(self.machine.clients[1].target, "AA:BB")

    async def test_always_silent_raises_and_closes(self):
        self.machine.silent_connections = 99
        c = self.make()
        with self.assertRaises(CeraError):
            await c.connect()
        self.assertEqual(len(self.machine.clients), 4)       # the default number of attempts
        self.assertFalse(any(f.is_connected for f in self.machine.clients))
        await c.disconnect()                                 # harmless after a failed connect

    async def test_unparseable_reply_closes_the_connection(self):
        self.machine.settings = SETTINGS[:11]  # another model, say
        c = self.make()
        with self.assertRaises(CeraError):
            await c.connect()
        self.assertEqual(len(self.machine.clients), 1)       # not worth a reconnect
        self.assertFalse(self.machine.clients[0].is_connected)
        self.assertFalse(c.is_connected)

    async def test_cancelled_connect_closes_the_connection(self):
        self.machine.silent_connections = 99
        task = asyncio.ensure_future(self.make().connect())
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(any(f.is_connected for f in self.machine.clients))

    async def test_lost_status_request_is_sent_again(self):
        self.machine.lose_status_requests = 1
        await self.make().connect()
        self.assertEqual(len(self.machine.clients), 1)       # no reconnect needed
        sent = [decode(w).cmd for w in self.machine.clients[0].written]
        self.assertEqual(sent.count(m.CMD_STATUS), 2)

    async def test_reconnect_starts_from_a_clean_state(self):
        c = self.make()
        await c.connect()
        await c.disconnect()
        self.assertEqual(self.lost, [1])                     # reported once
        self.assertFalse(c.is_connected)
        self.machine.silent_connections = 99                 # every further connection is silent
        with self.assertRaises(CeraError):
            await c.connect()                                # must not pass on the old heartbeat
        self.assertIsNone(c.status)
        self.assertIsNone(c.info)

    async def test_unexpected_drop_is_reported_once(self):
        c = self.make()
        await c.connect()
        fake = self.machine.clients[0]
        fake.is_connected = False
        fake._lost(fake)
        self.assertEqual(self.lost, [1])
        await c.disconnect()
        self.assertEqual(self.lost, [1])

    async def test_write_errors_become_cera_errors(self):
        c = self.make()
        await c.connect()
        self.machine.write_error = BleakError("gone")
        with self.assertRaises(CeraError):
            await c.stop()
        self.machine.write_error = OSError("gone")
        with self.assertRaises(CeraError):
            await c.get_settings()

    async def test_same_command_calls_take_turns(self):
        c = self.make()
        await c.connect()
        a, b = await asyncio.gather(c.set_target_temp_c(90), c.set_target_temp_c(95))
        self.assertEqual((a, b), (None, None))               # neither was rejected or timed out
        sent = [decode(w).payload for w in self.machine.clients[0].written if decode(w).cmd == m.CMD_TARGET_TEMP]
        self.assertEqual(sent, [bytes([0xC2]), bytes([0xCB])])

    async def test_heartbeat_between_fragments(self):
        c = self.make()
        await c.connect()
        fake = self.machine.clients[0]
        payload = bytes([1, 0x39]) + bytes(range(40))

        def stats(_p):
            return [reply(m.CMD_STATS, payload[:15], FLAG_FIRST), reply(m.CMD_STATUS, HEARTBEAT_HEATING),
                    reply(m.CMD_STATS, payload[15:30], FLAG_MIDDLE), reply(m.CMD_STATS, payload[30:], FLAG_LAST)]

        self.machine.replies[m.CMD_STATS] = stats
        s = await c.get_stats()
        self.assertEqual(s.daily, tuple(range(40)))
        self.assertEqual(c.status.state, m.State.HEAT)
        self.assertEqual(self.statuses[-1].state, m.State.HEAT)
        fake.push(b"\x7e\x00garbage")                       # corrupt frames are dropped
        self.assertEqual(c.status.state, m.State.HEAT)


if __name__ == "__main__":
    unittest.main()
