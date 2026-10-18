"""第 3 阶段验收：命令路径改走 hp20_commands.frame_* 之后，入参校验必须真的活着。

修复前 hp20_device 自己 `framing.build_frame(cmd, payload, ...)` 手拼 payload，
而唯一带校验的 `frame_set_thermostat` / `frame_set_preheat` / `_check8` 全项目 0 调用 ——
校验只活在没人走的代码里。现在设备层只用 frame_*，非法入参必须**在发帧之前**抛异常，
且控制器上不能出现任何帧。
"""
import unittest

import framing
import hp20_commands as hc
from hp20_device import Hp20Device
from test_device import FakeTransport


class ValidationCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = FakeTransport()
        self.device = Hp20Device(self.transport, timeout=0.3, tick=0.005, write_gap=0.0,
                                 poll_interval=30.0, handshake_delay=0.0)
        self.logs: list[str] = []
        self.device.on_system = self.logs.append
        await self.transport.connect()
        await self.device.open()
        await asyncio_sleep(0.05)
        self.transport.writes.clear()

    async def asyncTearDown(self):
        await self.device.close()

    def writes(self) -> list[bytes]:
        return [data for _ts, data in self.transport.writes]


async def asyncio_sleep(delay: float) -> None:
    import asyncio
    await asyncio.sleep(delay)


class TestFrameValidationIsAlive(ValidationCase):
    async def test_thermostat_index_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            await self.device.set_thermostat(9, 100)
        self.assertEqual(self.writes(), [], "非法 index 绝不能真的发出帧")

    async def test_thermostat_index_negative_rejected(self):
        with self.assertRaises(ValueError):
            await self.device.set_thermostat(-1, 100)
        self.assertEqual(self.writes(), [])

    async def test_preheat_channel_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            await self.device.set_preheat(4)
        self.assertEqual(self.writes(), [])

    async def test_reflow_needs_eight_values(self):
        with self.assertRaises(ValueError):
            await self.device.write_reflow([1, 2, 3])
        self.assertEqual(self.writes(), [])

    async def test_repair_needs_eight_values(self):
        with self.assertRaises(ValueError):
            await self.device.write_repair([1] * 7)
        self.assertEqual(self.writes(), [])

    async def test_bad_dev_id_rejected_before_write(self):
        """cmd 层次之外：dev_id 越界同样必须在发帧前抛（build_frame 校验复活）。"""
        bad = Hp20Device(self.transport, dev_id=0x1FF, family=hc.FAMILY, timeout=0.3,
                         tick=0.005, write_gap=0.0, poll_interval=30.0)
        try:
            with self.assertRaises(ValueError):
                await bad.poll_once()
        finally:
            await bad.close()
        self.assertEqual(self.writes(), [])


class TestDeviceLayerUsesFrameBuilders(ValidationCase):
    """设备层发出的字节必须与 hc.frame_* 完全一致（否则「真源」只是名义上的）。"""

    async def test_set_preheat_bytes(self):
        self.assertTrue(await self.device.set_preheat(2))
        self.assertEqual(self.writes()[0], hc.frame_set_preheat(2))

    async def test_set_thermostat_bytes(self):
        self.assertTrue(await self.device.set_thermostat(hc.TARGET_INDEX, 150))
        self.assertEqual(self.writes()[0], hc.frame_set_thermostat(hc.TARGET_INDEX, 150))

    async def test_read_frames_bytes(self):
        await self.device.read_presets()
        await self.device.read_reflow()
        await self.device.read_repair()
        await self.device.read_name()
        self.assertEqual(self.writes(), [hc.frame_get_thermostat(), hc.frame_get_reflow(),
                                         hc.frame_get_repair(), hc.frame_get_name()])

    async def test_custom_dev_id_and_family_used(self):
        dev = Hp20Device(self.transport, dev_id=0x09, family=0x02, timeout=0.3,
                         tick=0.005, write_gap=0.0, poll_interval=30.0)
        try:
            self.transport.writes.clear()
            await dev.set_preheat(0)
            data = self.transport.writes[0][1]
            self.assertEqual(data, hc.frame_set_preheat(0, dev_id=0x09, family=0x02))
            self.assertEqual(framing.parse_frame(data).cmd, hc.CMD_SET_PREHEAT)
        finally:
            await dev.close()

    async def test_long_name_truncated_to_20_bytes(self):
        """名字超长不是错误：按协议截到 20 字节（行为与 frame_set_name 一致）。"""
        self.assertTrue(await self.device.write_name("A" * 30))
        payload = framing.parse_frame(self.writes()[0]).payload
        self.assertEqual(len(payload), hc.NAME_LEN)
        self.assertEqual(self.writes()[0], hc.frame_set_name("A" * 30))


if __name__ == "__main__":
    unittest.main()
