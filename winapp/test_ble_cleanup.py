"""P0-B BLE 客户端泄漏：失败必须回滚，收尾必须统一。

修复前：`_open_client` 里 connect/挑特征/start_notify 任一步抛异常都不回滚；
`disconnect()` 只把 _client 置 None（不 stop_notify、不清 notify/write 特征）；
`_on_disconnected()` 什么都不清。真机表现：RECONNECT_MAX_ATTEMPTS=4 的退避重连每失败
一次就新建 BleakClient 覆盖旧的，GATT 句柄留在 Windows BLE 栈里 ——
「第一次能连，之后越连越连不上，重启软件才好」。
"""
import unittest
from unittest import mock

import transport as transport_mod
from transport import BleTransport, TransportError
from test_transport import REAL_HP20, FakeClient as _CharOnlyClient, FakeServices


class FakeBleakClient:
    """可编排的 BleakClient 替身：记录 connect/stop_notify/disconnect 的先后顺序。"""

    instances: list["FakeBleakClient"] = []
    fail_connect = False
    fail_notify = False
    chars = REAL_HP20

    def __init__(self, target, disconnected_callback=None, timeout=None):
        self.target = target
        self.disconnected_callback = disconnected_callback
        self.timeout = timeout
        self.services = FakeServices(list(self.chars))
        self.connected = False
        self.ever_connected = False
        self.disconnect_calls = 0
        self.calls: list[str] = []
        FakeBleakClient.instances.append(self)

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.calls.append("connect")
        if FakeBleakClient.fail_connect:
            raise RuntimeError("connect 失败(模拟)")
        self.connected = True
        self.ever_connected = True

    async def start_notify(self, char, handler) -> None:
        self.calls.append("start_notify")
        if FakeBleakClient.fail_notify:
            raise RuntimeError("start_notify 失败(模拟)")

    async def stop_notify(self, char) -> None:
        self.calls.append("stop_notify")

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.disconnect_calls += 1
        self.connected = False


class BleCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeBleakClient.instances = []
        FakeBleakClient.fail_connect = False
        FakeBleakClient.fail_notify = False
        self.transport = BleTransport()
        self.logs: list[str] = []
        self.transport.on_system = self.logs.append
        self._patch = mock.patch.object(transport_mod, "BleakClient", FakeBleakClient)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def assertRefsCleared(self):
        self.assertIsNone(self.transport._client, "_client 未清空")
        self.assertIsNone(self.transport._notify_char, "_notify_char 未清空")
        self.assertIsNone(self.transport._write_char, "_write_char 未清空")
        self.assertEqual(self.transport.char_summary, [], "特征表未清空")

    def leaked(self) -> list[FakeBleakClient]:
        """真正泄漏的客户端：连上过却始终没被断开。"""
        return [c for c in FakeBleakClient.instances
                if c.ever_connected and c.disconnect_calls == 0]


class TestCleanup(BleCase):
    async def test_cleanup_stops_notify_then_disconnects(self):
        await self.transport.connect("AA:BB:CC:DD:EE:FF", timeout=1.0)
        client = FakeBleakClient.instances[-1]
        self.assertTrue(client.connected)
        await self.transport.disconnect()
        self.assertEqual([c for c in client.calls if c in ("stop_notify", "disconnect")],
                         ["stop_notify", "disconnect"], "必须先停通知再断链")
        self.assertRefsCleared()
        self.assertEqual(self.leaked(), [])

    async def test_cleanup_is_idempotent(self):
        await self.transport.disconnect()          # 没有客户端时什么都不做
        self.assertRefsCleared()
        await self.transport.connect("AA:BB:CC:DD:EE:FF", timeout=1.0)
        await self.transport.disconnect()
        await self.transport.disconnect()          # 第二次不该抛
        self.assertRefsCleared()

    async def test_on_disconnected_clears_refs(self):
        await self.transport.connect("AA:BB:CC:DD:EE:FF", timeout=1.0)
        self.assertIsNotNone(self.transport._notify_char)
        reasons: list[str] = []
        self.transport.on_disconnect = reasons.append
        FakeBleakClient.instances[-1].disconnected_callback(FakeBleakClient.instances[-1])
        self.assertRefsCleared()
        self.assertEqual(reasons, ["链路已断开"])
        self.assertFalse(self.transport.is_connected)


class TestOpenClientRollback(BleCase):
    async def test_start_notify_failure_rolls_back(self):
        FakeBleakClient.fail_notify = True
        with self.assertRaises(RuntimeError):
            await self.transport.connect("AA:BB:CC:DD:EE:FF", timeout=1.0)
        client = FakeBleakClient.instances[-1]
        self.assertIn("stop_notify", client.calls, "已订阅的通知必须先停掉")
        self.assertEqual(client.disconnect_calls, 1, "连上的链路必须断开")
        self.assertRefsCleared()
        self.assertEqual(self.leaked(), [])

    async def test_resolve_chars_failure_rolls_back(self):
        FakeBleakClient.chars = [REAL_HP20[2]]      # 只有写特征，没有 notify
        try:
            with self.assertRaises(TransportError):
                await self.transport.connect("AA:BB:CC:DD:EE:FF", timeout=1.0)
        finally:
            FakeBleakClient.chars = REAL_HP20
        client = FakeBleakClient.instances[-1]
        self.assertEqual(client.disconnect_calls, 1)
        self.assertRefsCleared()
        self.assertEqual(self.leaked(), [])

    async def test_connect_failure_leaves_refs_empty(self):
        FakeBleakClient.fail_connect = True
        with self.assertRaises(RuntimeError):
            await self.transport.connect("AA:BB:CC:DD:EE:FF", timeout=1.0)
        self.assertRefsCleared()
        self.assertEqual(self.leaked(), [])
        self.assertFalse(self.transport.is_connected)

    async def test_four_failures_do_not_accumulate_clients(self):
        """连续 4 次失败（真机退避重连次数）不能留下任何活着的客户端。"""
        FakeBleakClient.fail_notify = True
        for _ in range(4):
            with self.assertRaises(RuntimeError):
                await self.transport.connect("AA:BB:CC:DD:EE:FF", timeout=1.0)
            self.assertRefsCleared()
        self.assertEqual(len(FakeBleakClient.instances), 4)
        self.assertEqual(self.leaked(), [], "失败后仍有客户端没被断开 = 泄漏")
        self.assertTrue(all(c.disconnect_calls == 1 for c in FakeBleakClient.instances))

    async def test_reopen_after_failure_succeeds(self):
        FakeBleakClient.fail_notify = True
        with self.assertRaises(RuntimeError):
            await self.transport.connect("AA:BB:CC:DD:EE:FF", timeout=1.0)
        FakeBleakClient.fail_notify = False
        await self.transport.connect("AA:BB:CC:DD:EE:FF", timeout=1.0)
        self.assertTrue(self.transport.is_connected)
        failed = FakeBleakClient.instances[0]
        self.assertEqual(failed.disconnect_calls, 1, "失败的那一次必须回滚")
        await self.transport.disconnect()
        self.assertEqual(self.leaked(), [], "断开后不得留下任何客户端")


if __name__ == "__main__":
    unittest.main()
