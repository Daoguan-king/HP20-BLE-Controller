"""P0-C 假连接成功：识别失败 / 识别成非 HP20 都不能判 CONNECTED。

修复前三处：`device.open()` 在识别失败时照常返回（上层于是发绿灯）；`_blocked` 置位后
open()/close() 都不复位（先连错一次，换回真机也「显示识别成功、命令全被静默丢弃」）；
无地址无名字时直接 connect(`found[0]`)，即 RSSI 最强的任意 BLE 设备。
"""
import unittest

import hp20_commands as hc
from connection import (STATE_CONNECTED, STATE_FAILED, EVT_CONNECT_OK,
                        ConnectionManager, pick_unique_candidate)
from hp20_device import Hp20Device
from test_device import FakeTransport
from transport import ScanResult

EL15 = bytes((0x07, 0x03))          # 设备头 0x0307 -> 非 HP20
HP20 = bytes((hc.DEV_ID, hc.FAMILY))


class TestPickUniqueCandidate(unittest.TestCase):
    """纯函数：无地址/无名字时只接受「唯一候选」。"""

    def make(self, n):
        return [ScanResult(f"AA:BB:CC:DD:EE:{i:02X}", f"dev{i}", -40 - i, "ble")
                for i in range(n)]

    def test_no_candidate(self):
        self.assertIsNone(pick_unique_candidate([]))
        self.assertIsNone(pick_unique_candidate(None))

    def test_exactly_one_candidate_is_unambiguous(self):
        found = self.make(1)
        self.assertIs(pick_unique_candidate(found), found[0])

    def test_multiple_candidates_are_not_auto_picked(self):
        """以前返回 found[0]（RSSI 最强的任意设备）—— 这就是连错设备的入口。"""
        self.assertIsNone(pick_unique_candidate(self.make(2)))
        self.assertIsNone(pick_unique_candidate(self.make(5)))


class DeviceOpenCase(unittest.IsolatedAsyncioTestCase):
    identify_payload = HP20

    async def asyncSetUp(self):
        self.transport = FakeTransport()
        self.transport.identify_payload = self.identify_payload
        self.device = Hp20Device(self.transport, timeout=0.3, tick=0.005,
                                 write_gap=0.0, poll_interval=30.0, handshake_delay=0.0)
        self.logs: list[str] = []
        self.device.on_system = self.logs.append
        await self.transport.connect()

    async def asyncTearDown(self):
        await self.device.close()


class TestOpenReturnsVerdict(DeviceOpenCase):
    async def test_hp20_opens_true(self):
        self.assertTrue(await self.device.open())
        self.assertTrue(self.device.polling)

    async def test_not_hp20_opens_false(self):
        self.transport.identify_payload = EL15
        self.assertFalse(await self.device.open(), "非 HP20 不能返回成功")
        self.assertEqual(self.device.open_failure, "not_hp20")
        self.assertFalse(self.device.polling, "没识别成功就不该启动轮询")

    async def test_no_reply_opens_false(self):
        self.transport.drop[hc.CMD_IDENTIFY] = 1
        self.assertFalse(await self.device.open(), "识别无应答不能返回成功")
        self.assertEqual(self.device.open_failure, "no_reply")
        self.assertFalse(self.device.identified)


class TestBlockedReset(DeviceOpenCase):
    async def test_wrong_device_then_real_device_recovers(self):
        """先连错（EL15）再连对（HP20）：_blocked 必须复位，命令要能真的发出去。"""
        self.transport.identify_payload = EL15
        self.assertFalse(await self.device.open())
        self.assertTrue(self.device._blocked, "非 HP20 应停发命令")
        await self.device.close()

        self.transport.identify_payload = HP20
        self.assertTrue(await self.device.open(), "换回真机必须能重新识别成功")
        self.assertFalse(self.device._blocked, "_blocked 未复位 -> 命令会被静默丢弃")
        self.assertIsNotNone(await self.device.read_presets(), "复位后命令要能下发")


class ManagerCase(unittest.IsolatedAsyncioTestCase):
    max_retries = 0

    async def asyncSetUp(self):
        self.transport = FakeTransport()
        self.device = Hp20Device(self.transport, timeout=0.3, tick=0.005, write_gap=0.0,
                                 poll_interval=30.0, handshake_delay=0.0)
        self.logs: list[str] = []
        self.device.on_system = self.logs.append
        self.manager = ConnectionManager(self.transport, self.device, scan_timeout=0.01,
                                        max_retries=self.max_retries, backoff=(0.01,))
        self.events = []
        self.manager.on_event = self.events.append

    async def asyncTearDown(self):
        self.manager.cancel()
        await self.device.close()

    def kinds(self):
        return [e.kind for e in self.events]


class TestConnectVerdict(ManagerCase):
    async def test_identify_no_reply_is_not_connected(self):
        # max_retries 下界是 1（首次 + 1 次重试），所以要让每一次识别都无应答
        self.transport.drop[hc.CMD_IDENTIFY] = 9
        self.assertFalse(await self.manager.connect(), "识别无应答必须判连接失败")
        self.assertEqual(self.manager.state, STATE_FAILED)
        self.assertNotIn(EVT_CONNECT_OK, self.kinds(), "不能发绿灯事件")
        self.assertTrue(any("未识别为 HP20" in e.detail for e in self.events))

    async def test_non_hp20_is_not_connected(self):
        self.transport.identify_payload = EL15
        self.assertFalse(await self.manager.connect(), "非 HP20 必须判连接失败")
        self.assertEqual(self.manager.state, STATE_FAILED)
        self.assertNotIn(EVT_CONNECT_OK, self.kinds())
        self.assertTrue(any("未识别为 HP20" in e.detail for e in self.events))

    async def test_recovers_after_wrong_device(self):
        """先连错再连对：第二次必须成功、且命令能下发（_blocked 复位）。"""
        self.transport.identify_payload = EL15
        self.assertFalse(await self.manager.connect())
        self.transport.identify_payload = HP20
        self.assertTrue(await self.manager.connect())
        self.assertEqual(self.manager.state, STATE_CONNECTED)
        self.assertFalse(self.device._blocked)
        self.assertIsNotNone(await self.device.read_presets())
        self.assertTrue(self.device.polling)


class TestNoBlindConnect(ManagerCase):
    async def test_two_candidates_are_not_auto_connected(self):
        """扫到两台设备且没给地址/名字：不连，交给用户选。"""
        self.transport.scan_results_override = [
            ScanResult("AA:BB:CC:DD:EE:01", "HP20-A", -40, "ble"),
            ScanResult("AA:BB:CC:DD:EE:02", "HP20-B", -70, "ble"),
        ]
        self.assertFalse(await self.manager.connect())
        self.assertEqual(self.transport.connect_calls, 0, "不该盲连任何一台")
        self.assertEqual(self.manager.state, STATE_FAILED)

    async def test_single_candidate_still_connects(self):
        """唯一候选（模拟设备/只有一个广播源）仍然自动连 —— --sim --smoke 依赖它。"""
        self.transport.scan_results_override = [
            ScanResult("AA:BB:CC:DD:EE:01", "HP20-A", -40, "ble")]
        self.assertTrue(await self.manager.connect())
        self.assertEqual(self.manager.state, STATE_CONNECTED)


if __name__ == "__main__":
    unittest.main()
