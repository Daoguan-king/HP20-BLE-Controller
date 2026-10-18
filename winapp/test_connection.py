"""连接/重连状态机：退避序列、扫不到提示、断线自动重连、取消、总预算。"""
import asyncio
import time
import unittest

import connection as conn_mod
import hp20_commands as hc
from proto_const import Proto
from connection import (EVT_CANCELLED, EVT_CONNECT_FAIL, EVT_CONNECT_OK, EVT_DISCONNECTED,
                        EVT_GIVE_UP,
                        EVT_NOT_FOUND, EVT_RECONNECT_OK, EVT_SCAN, EVT_STAGE,
                        NOT_BROADCAST_HINT, STATE_CONNECTED, STATE_FAILED, ConnectionManager)
from hp20_device import Hp20Device
from test_device import FakeTransport
from transport import ScanResult


class NeverTransport:
    """三段全部失败、每步都花一点时间的假 transport（用来测总预算）。"""

    is_connected = False
    char_summary: list[str] = []

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.timeouts: list[tuple[str, float]] = []

    async def find_by_address(self, address, timeout):
        self.timeouts.append(("find", timeout))
        await asyncio.sleep(min(timeout, self.delay))
        return None

    async def connect_direct(self, address, timeout):
        self.timeouts.append(("direct", timeout))
        await asyncio.sleep(min(timeout, self.delay))
        raise RuntimeError("never connects")

    async def connect(self, result, timeout):
        self.timeouts.append(("connect", timeout))
        raise RuntimeError("no device")

    async def scan(self, timeout):
        self.timeouts.append(("scan", timeout))
        await asyncio.sleep(min(timeout, self.delay))
        return []

    async def disconnect(self):
        pass


class NeverDevice:
    on_disconnect = None

    async def open(self):
        raise RuntimeError("open failed")

    async def close(self):
        pass


class ManagerCase(unittest.IsolatedAsyncioTestCase):
    scan_results_override = None

    async def asyncSetUp(self):
        self.transport = FakeTransport()
        if self.scan_results_override is not None:
            self.transport.scan_results_override = self.scan_results_override
        self.device = Hp20Device(self.transport, timeout=0.3, tick=0.005, write_gap=0.0,
                                 poll_interval=30.0, handshake_delay=0.0)
        self.sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            self.sleeps.append(delay)

        self.manager = ConnectionManager(self.transport, self.device, scan_timeout=0.01,
                                         max_retries=4, backoff=(1.0, 2.0, 4.0, 8.0),
                                         sleep=fake_sleep)
        self.events = []
        self.manager.on_event = self.events.append

    async def asyncTearDown(self):
        self.manager.cancel()
        await self.device.close()

    def kinds(self):
        return [e.kind for e in self.events]


class TestConnectFlow(ManagerCase):
    async def test_connect_first_try(self):
        self.assertTrue(await self.manager.connect())
        self.assertEqual(self.manager.state, STATE_CONNECTED)
        self.assertEqual(self.manager.state_text(), "已连接")
        self.assertEqual(self.sleeps, [])
        # 没给地址时直接走第 ③ 段：先发 stage 事件，再扫描，再连接成功
        self.assertEqual(self.kinds()[:3], [EVT_STAGE, EVT_SCAN, EVT_CONNECT_OK])
        self.assertTrue(self.device.identified)

    async def test_backoff_sequence_on_total_failure(self):
        self.transport.fail_connects = 99
        self.assertFalse(await self.manager.connect())
        # max_retries=4 -> 首次 + 4 次重试；退避 1/2/4/8
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0, 8.0])
        self.assertEqual(self.manager.delays, [1.0, 2.0, 4.0, 8.0])
        self.assertEqual(self.transport.connect_calls, 5)
        self.assertEqual(self.manager.state, STATE_FAILED)
        self.assertEqual(self.kinds()[-1], EVT_GIVE_UP)
        self.assertTrue(any("Not connected" in e.detail for e in self.events))

    async def test_recovers_after_two_failures(self):
        self.transport.fail_connects = 2
        self.assertTrue(await self.manager.connect())
        self.assertEqual(self.sleeps, [1.0, 2.0])
        self.assertEqual(self.transport.connect_calls, 3)
        self.assertEqual(self.manager.state, STATE_CONNECTED)

    async def test_resent_scan_every_attempt(self):
        """每次重试都要重新扫描（不重用旧的 BLEDevice）。"""
        self.transport.fail_connects = 2
        await self.manager.connect()
        self.assertEqual(self.kinds().count(EVT_SCAN), 3)

    async def test_fresh_device_object_per_attempt(self):
        seen = []
        original = self.transport.connect

        async def spy(target=None, timeout=5.0):
            seen.append(id(target))
            return await original(target, timeout)

        self.transport.connect = spy
        self.transport.fail_connects = 1
        await self.manager.connect()
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1], "重试不应复用同一个 ScanResult/BLEDevice")


class TestNotBroadcasting(ManagerCase):
    scan_results_override: list = []

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager.name_hint = "HP20"

    async def test_actionable_hint_instead_of_silent_failure(self):
        self.assertFalse(await self.manager.connect())
        not_found = [e for e in self.events if e.kind == EVT_NOT_FOUND]
        self.assertTrue(not_found)
        self.assertEqual(not_found[0].detail, NOT_BROADCAST_HINT)
        self.assertIn("手机 APP", NOT_BROADCAST_HINT)
        self.assertEqual(self.kinds()[-1], EVT_GIVE_UP)
        self.assertEqual(self.manager.recent(20)[-1].kind, EVT_GIVE_UP)


class TestAutoReconnect(ManagerCase):
    async def test_reconnect_after_drop(self):
        self.assertTrue(await self.manager.connect())
        self.assertEqual(self.transport.connect_calls, 1)
        before = len(self.events)
        self.transport.simulate_disconnect("模拟掉线")
        self.assertEqual(self.events[before].kind, EVT_DISCONNECTED)

        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            if (self.manager.state == STATE_CONNECTED
                    and self.transport.connect_calls >= 2 and self.device.status is not None):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.manager.state, STATE_CONNECTED)
        self.assertEqual(self.transport.connect_calls, 2, "自动重连应重新 connect")
        self.assertIn(EVT_RECONNECT_OK, self.kinds())
        self.assertTrue(self.device.identified, "重连后要重新握手识别")
        # 轮询是单命令在途：若后台轮询恰好占用事务槽就跳过，这里重试几次
        polled = False
        for _ in range(30):
            polled = await self.device.poll_once()
            if polled:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(polled, "重连后轮询要恢复")

    async def test_no_reconnect_when_disabled(self):
        self.manager.auto_reconnect = False
        self.assertTrue(await self.manager.connect())
        self.transport.simulate_disconnect("模拟掉线")
        await asyncio.sleep(0.1)
        self.assertEqual(self.transport.connect_calls, 1, "关掉自动重连就不该再连")
        self.assertNotIn(EVT_RECONNECT_OK, self.kinds())

    async def test_user_disconnect_suppresses_reconnect(self):
        self.assertTrue(await self.manager.connect())
        await self.manager.disconnect()
        await asyncio.sleep(0.15)
        self.assertEqual(self.transport.connect_calls, 1)
        self.assertEqual(self.manager.state, "idle")


class TestCancelReconnect(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_stops_retrying(self):
        transport = FakeTransport()
        device = Hp20Device(transport, timeout=0.3, tick=0.005, write_gap=0.0,
                            poll_interval=30.0, handshake_delay=0.0)
        manager = ConnectionManager(transport, device, scan_timeout=0.01,
                                    max_retries=3, backoff=(0.2, 0.2, 0.2))
        events = []
        manager.on_event = events.append
        try:
            self.assertTrue(await manager.connect())
            self.assertEqual(manager.state_text(), "已连接")
            transport.fail_connects = 99
            transport.simulate_disconnect("模拟掉线")
            await asyncio.sleep(0.05)
            self.assertIn("重连中", manager.state_text())
            manager.cancel()
            await asyncio.sleep(0.05)
            self.assertIn(EVT_CANCELLED, [e.kind for e in events])
            self.assertNotEqual(manager.state, STATE_CONNECTED)
        finally:
            manager.cancel()
            await device.close()


class TestThreeStageOrder(unittest.IsolatedAsyncioTestCase):
    """真机实测：扫描扫不到 ≠ 连不上。连接必须依次尝试 ①缓存地址 ②直连 ③重新扫描。"""

    ADDR = "AA:BB:CC:DD:EE:FF"

    async def asyncSetUp(self):
        self.transport = FakeTransport()
        self.device = Hp20Device(self.transport, timeout=0.3, tick=0.005, write_gap=0.0,
                                 poll_interval=30.0, handshake_delay=0.0)
        self.sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            self.sleeps.append(delay)

        self.manager = ConnectionManager(self.transport, self.device,
                                         scan_timeout=0.01, max_retries=4,
                                         backoff=(1.0, 2.0, 4.0, 8.0),
                                         sleep=fake_sleep)
        self.events = []
        self.manager.on_event = self.events.append
        self.preferred = ScanResult(self.ADDR, "HP20", -50, "ble")
        # 扫描里要有这台设备，第 ③ 段才可能成功
        self.transport.scan_results_override = [self.preferred]

    async def asyncTearDown(self):
        self.manager.cancel()
        await self.device.close()

    def kinds(self):
        return [e.kind for e in self.events]

    async def test_stage1_cache_hit_only(self):
        self.assertTrue(await self.manager.connect(self.preferred))
        self.assertEqual(self.transport.stage_order, ["find"])
        self.assertEqual(self.manager.state, STATE_CONNECTED)

    async def test_stage2_after_stage1_miss(self):
        self.transport.fail_find = 1
        self.assertTrue(await self.manager.connect(self.preferred))
        self.assertEqual(self.transport.stage_order, ["find", "direct"])
        self.assertIn(EVT_CONNECT_OK, self.kinds())

    async def test_stage3_after_stage1_and_2_miss(self):
        self.transport.fail_find = 1
        self.transport.fail_direct = 1
        self.assertTrue(await self.manager.connect(self.preferred))
        self.assertEqual(self.transport.stage_order, ["find", "direct", "scan"])
        self.assertEqual(self.manager.state, STATE_CONNECTED)

    async def test_all_three_fail_then_backoff(self):
        self.transport.fail_find = 99
        self.transport.fail_direct = 99
        self.transport.scan_results_override = []      # 扫描也扫不到
        self.assertFalse(await self.manager.connect(self.preferred))
        # 每次尝试都走完三段；退避 1/2/4/8
        self.assertEqual(self.transport.stage_order,
                         ["find", "direct", "scan"] * 5)
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0, 8.0])
        self.assertEqual(self.kinds()[-1], EVT_GIVE_UP)
        self.assertIn(EVT_NOT_FOUND, self.kinds())
        details = [e.detail for e in self.events if e.kind == EVT_CONNECT_FAIL]
        self.assertTrue(details and "①" in details[0] and "③" in details[0])

    async def test_reconnect_starts_from_last_address(self):
        """第 ① 段成功后记住地址，断线重连从该地址开始（不再从扫描开始）。"""
        self.transport.fail_find = 0
        self.assertTrue(await self.manager.connect(self.preferred))
        self.assertEqual(self.manager.last_address, self.ADDR)
        self.transport.stage_order.clear()
        self.transport.simulate_disconnect("模拟掉线")
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            if self.manager.state == STATE_CONNECTED and self.transport.stage_order:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.transport.stage_order[:1], ["find"])
        self.assertEqual(self.manager.state, STATE_CONNECTED)


class TestTimeoutConstant(unittest.TestCase):
    def test_command_timeout_is_two_seconds(self):
        self.assertEqual(hc.RESPONSE_TIMEOUT_S, 2.0)
        # 该常量已并入 proto_const.Proto，文档值仅作参考（反汇编 0x3E8 = 1000 ms）
        self.assertEqual(Proto.PROTOCOL_TIMEOUT_S, 1.0)
        self.assertEqual(hc.PROTOCOL_TIMEOUT_S, Proto.PROTOCOL_TIMEOUT_S)   # hc 只是派生

    def test_three_stage_timeouts(self):
        """真机实测 ①空等 10s、②约 18s 成功 -> 收成 6/15/6，避免用户以为卡死。"""
        class StubDevice:
            on_disconnect = None

        manager = ConnectionManager(transport=object(), device=StubDevice())
        self.assertEqual(manager.find_timeout, 6.0)
        self.assertEqual(manager.connect_timeout, 15.0)
        self.assertEqual(manager.scan_timeout, 6.0)


class TestTotalBudget(unittest.IsolatedAsyncioTestCase):
    """M 阶段：三段式 + 退避全跑满会到 90s+（真机实测 94.5s），必须有总预算。"""

    async def test_all_stages_failing_stops_within_budget(self):
        transport = NeverTransport(delay=0.05)
        device = NeverDevice()
        manager = ConnectionManager(transport, device, address="AA:BB:CC:DD:EE:FF",
                                    scan_timeout=1.0, find_timeout=1.0,
                                    connect_timeout=2.0, max_retries=4,
                                    backoff=(0.2, 0.4, 0.8, 1.6), total_budget=1.0)
        events = []
        manager.on_event = events.append
        started = time.monotonic()
        ok = await manager.connect()
        spent = time.monotonic() - started
        self.assertFalse(ok)
        self.assertLessEqual(spent, 1.25, f"超出预算太多: {spent:.2f}s")
        self.assertTrue(manager.budget_exhausted)
        kinds = [e.kind for e in events]
        self.assertIn("give_up", kinds)
        self.assertIn("not_found", kinds)
        give_up = [e for e in events if e.kind == "give_up"][-1]
        self.assertIn("设备未响应", give_up.detail)
        self.assertIn("关闭手机蓝牙后重试", give_up.detail)

    async def test_stage_timeouts_are_clamped_to_remaining(self):
        transport = NeverTransport(delay=0.4)
        manager = ConnectionManager(transport, NeverDevice(),
                                    address="AA:BB:CC:DD:EE:FF",
                                    scan_timeout=30.0, find_timeout=30.0,
                                    connect_timeout=30.0, max_retries=0,
                                    total_budget=0.6)
        await manager.connect()
        used = [t for _, t in transport.timeouts]
        self.assertTrue(used)
        self.assertTrue(all(t <= 0.65 for t in used), used)

    async def test_progress_text_shows_stage_and_elapsed(self):
        manager = ConnectionManager(NeverTransport(), NeverDevice(),
                                    total_budget=45.0)
        manager.started_at = manager._clock() - 12.0
        manager.stage_text = "②直接连接 AA:BB"
        self.assertIn("②直接连接", manager.progress_text())
        self.assertIn("12s", manager.progress_text())
        self.assertAlmostEqual(manager.elapsed(), 12.0, delta=0.5)
        self.assertGreater(manager.remaining(), 30.0)

    async def test_default_budget_is_45s(self):
        manager = ConnectionManager(NeverTransport(), NeverDevice())
        self.assertEqual(manager.total_budget, 45.0)
        self.assertEqual(conn_mod.DEFAULT_TOTAL_BUDGET_S, 45.0)


if __name__ == "__main__":
    unittest.main()
