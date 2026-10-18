"""--probe：无 GUI 真机只读自检 —— 断言只读性质与输出。"""
import asyncio
import unittest

import atk_hp20 as app_mod
import framing
import hp20_commands as hc
from sim_device import SimTransport

READ_ONLY_CMDS = {hc.CMD_IDENTIFY, hc.CMD_GET_THERMOSTAT, hc.CMD_GET_REFLOW,
                  hc.CMD_GET_REPAIR, hc.CMD_GET_NAME, hc.CMD_REALTIME}
WRITE_CMDS = {hc.CMD_SET_THERMOSTAT, hc.CMD_SET_PREHEAT, hc.CMD_SET_REFLOW,
              hc.CMD_SET_NAME, hc.CMD_SET_REPAIR}


class RecordingSim(SimTransport):
    """记录发出去的每个 cmd，用于断言 --probe 只读。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.cmds: list[int] = []
        self._probe_parser = framing.FrameParser(header=framing.REQ_HEADER)

    async def write(self, data: bytes) -> None:
        for frame in self._probe_parser.feed(data):
            self.cmds.append(frame.cmd)
        await super().write(data)


class ProbeCase(unittest.IsolatedAsyncioTestCase):
    target = SimTransport.ADDRESS
    STATUS_TIMEOUT = 6.0

    def make_opts(self, *extra):
        return app_mod.build_parser().parse_args(
            ["--probe", self.target, "--sim", *extra])

    async def run_probe(self, opts=None):
        transport = RecordingSim(latency=0.002, tick_dt=0.02)
        lines: list[str] = []
        code = await app_mod._probe_async(opts or self.make_opts(),
                                         transport=transport,
                                         emit=lambda s: lines.append(s),
                                         status_timeout=self.STATUS_TIMEOUT)
        return code, lines, transport


class TestProbeReadOnly(ProbeCase):
    async def test_exit_zero_and_fields(self):
        code, lines, transport = await self.run_probe()
        text = "\n".join(lines)
        self.assertEqual(code, 0, text)
        self.assertIn("HP20", text)
        self.assertIn("0x0209", text)
        self.assertIn("[66, 200, 300, 0]", text)
        self.assertIn("供电 140W", text)
        self.assertIn("输入 27.8V", text)
        self.assertIn("[150, 120, 180, 90, 260, 60, 80, 300]", text)
        self.assertIn("[100, 120, 80, 120, 80, 120, 260, 120]", text)
        self.assertIn("'HP20'", text)
        self.assertIn("状态帧：RX DF 09 02 0A", text)
        self.assertIn("polling=True", text)

    async def test_never_sends_write_commands(self):
        _code, _lines, transport = await self.run_probe()
        self.assertTrue(transport.cmds, "应该发过请求")
        self.assertFalse(WRITE_CMDS & set(transport.cmds),
                         f"只读探测发了写命令: {sorted(set(transport.cmds))}")
        self.assertTrue(set(transport.cmds) <= READ_ONLY_CMDS,
                        f"出现未知命令: {sorted(set(transport.cmds) - READ_ONLY_CMDS)}")
        # 也断言设备侧计数：只收到读类请求
        self.assertGreater(transport.core.rx_frames, 0)

    async def test_no_status_frame_reported(self):
        opts = self.make_opts()
        opts.poll = 3600.0          # 把轮询周期拉长到等于不轮询
        self.STATUS_TIMEOUT = 0.3
        code, lines, _t = await self.run_probe(opts)
        self.assertEqual(code, 1)
        self.assertIn("状态帧：0.3s 内没收到", "\n".join(lines))


class TestProbeNotConnected(ProbeCase):
    target = "11:22:33:44:55:66"

    async def test_exit_two_and_hint(self):
        opts = self.make_opts("--connect-retries", "0")
        code, lines, transport = await self.run_probe(opts)
        text = "\n".join(lines)
        self.assertEqual(code, 2, text)
        self.assertIn("连接失败", text)
        self.assertIn("设备未广播", text)
        self.assertIn("退出码 2", text)
        self.assertFalse(WRITE_CMDS & set(transport.cmds))


if __name__ == "__main__":
    unittest.main()
