"""S2 请求-应答时序：迟到应答绝不能顶替当前请求。

协议帧没有序号字段，配对键只有 cmd 一个字节。修复前 `_drop_inbox(cmd)` 清在 write
**之前**，而 `_await_response` 末尾还有一句无条件 `return self._take(cmd)`：上一次请求
超时后迟到的应答会在本次写完之后落进 inbox，被当成「本次应答」返回 —— 0.125s 轮询遇上
真机 0.7~1.5s 的延迟应答，界面显示的永远是上一次的温度（数据系统性差一拍）。
"""
import time
import unittest

import framing
import hp20_commands as hc
from hp20_device import Hp20Device
from transport import Transport


class ScriptedTransport(Transport):
    """按 write 的调用序号投递脚本化应答，精确复现「迟到应答」时序。"""

    def __init__(self) -> None:
        super().__init__()
        self.script: list[list[bytes]] = []    # 第 N 次 write 之后投递的帧
        self.writes: list[bytes] = []
        self._connected = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        for frame in (self.script.pop(0) if self.script else []):
            cb = self.on_data
            if cb is not None:
                cb(frame)

    async def scan(self, timeout: float = 1.0):
        return []

    async def connect(self, target=None, timeout: float = 5.0) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False


def presets_frame(values) -> bytes:
    return hc.response_frame(hc.CMD_GET_THERMOSTAT, hc.pack_u16(list(values)))


class PairingCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = ScriptedTransport()
        self.device = Hp20Device(self.transport, timeout=0.12, tick=0.005,
                                 write_gap=0.0, poll_interval=30.0)
        self.device.transport.on_data = self.device._on_data     # 跳过 open()，只测事务
        self.logs: list[str] = []
        self.device.on_system = self.logs.append

    async def asyncTearDown(self):
        await self.device.close()


class TestLateReplyIsDiscarded(PairingCase):
    async def test_late_reply_does_not_stand_in_for_current_request(self):
        """上一次超时的应答在本次写完之后才到：必须丢弃，返回本次的真应答。"""
        self.transport.script = [
            [],                                     # ① 请求 A：故意不回 -> 超时
            [presets_frame([111, 222, 333, 444]),   # ② 请求 B：A 的迟到应答先落地
             presets_frame([10, 20, 30, 40])],      #    B 自己的应答随后落地
        ]
        self.assertIsNone(await self.device.read_presets(), "第一次应当超时")
        self.assertEqual(self.device.stats.timeouts, 1)
        second = await self.device.read_presets()
        self.assertEqual(second, [10, 20, 30, 40],
                         "迟到应答被当成本次结果，数据会永远差一拍")
        self.assertEqual(self.device.stats.stale_replies, 1)
        self.assertTrue(any("迟到应答" in line for line in self.logs))

    async def test_credit_is_one_shot_and_self_heals(self):
        """欠账只认一帧：丢掉迟到应答之后，下一次请求立刻恢复正常。"""
        self.transport.script = [
            [],
            [presets_frame([111, 222, 333, 444]), presets_frame([10, 20, 30, 40])],
            [presets_frame([70, 80, 90, 100])],
        ]
        self.assertIsNone(await self.device.read_presets())
        self.assertEqual(await self.device.read_presets(), [10, 20, 30, 40])
        self.assertEqual(await self.device.read_presets(), [70, 80, 90, 100])
        self.assertEqual(self.device.stats.stale_replies, 1)

    async def test_expired_credit_does_not_eat_genuine_reply(self):
        """欠账过期（那条应答大概真的丢了）时不能继续吃本次的真应答。"""
        self.device._stale[hc.CMD_GET_THERMOSTAT] = time.monotonic() - 1.0
        self.transport.script = [[presets_frame([10, 20, 30, 40])]]
        self.assertEqual(await self.device.read_presets(), [10, 20, 30, 40])
        self.assertEqual(self.device.stats.stale_replies, 0)
        self.assertNotIn(hc.CMD_GET_THERMOSTAT, self.device._stale)


class TestNormalPairingUnchanged(PairingCase):
    async def test_reply_delivered_during_write_is_accepted(self):
        """模拟设备会在 write() 里就回帧，这种正当应答必须收下。"""
        self.transport.script = [[presets_frame([200, 250, 300, 350])]]
        self.assertEqual(await self.device.read_presets(), [200, 250, 300, 350])
        self.assertEqual(self.device.stats.stale_replies, 0)

    async def test_stale_leftover_before_write_is_cleaned(self):
        """写之前就堆在 inbox 里的陈旧同 cmd 帧照旧要被清掉。"""
        self.device._on_data(presets_frame([9, 9, 9, 9]))
        self.transport.script = [[presets_frame([10, 20, 30, 40])]]
        self.assertEqual(await self.device.read_presets(), [10, 20, 30, 40])

    async def test_pairing_by_cmd_survives_foreign_header(self):
        """真机应答设备头是 FF FF，配对只比 cmd（原有行为不能回退）。"""
        self.transport.script = [[hc.response_frame(hc.CMD_GET_THERMOSTAT,
                                                    hc.pack_u16([1, 2, 3, 4]),
                                                    dev_id=0xFF, family=0xFF)]]
        self.assertEqual(await self.device.read_presets(), [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
