"""设备层：识别、单命令在途、节流、暂停轮询、超时重试、参数自动刷新。"""
import asyncio
import time
import unittest

import framing
import hp20_commands as hc
from hp20_device import Hp20Device
from transport import ScanResult, Transport


class FakeTransport(Transport):
    """可编排应答的传输替身。"""

    def __init__(self):
        super().__init__()
        self.writes: list[tuple[float, bytes]] = []
        self.reply_delay = 0.0
        self.drop: dict[int, int] = {}
        self.reply_queue: dict[int, list[bytes]] = {}
        self.identify_payload = bytes((hc.DEV_ID, hc.FAMILY))
        self.status_payload = hc.build_realtime_payload(
            current_temp=60, supply_power_w=140, output_power_w=6, target_temp=180,
            channel_or_stage=0, mode=2, w2=0x0045)
        self.auto_reply = True
        self.empty_ack_for: set[int] = set()    # 这些 cmd 故意回空 payload
        self.fail_connects = 0                  # >0 时 connect 抛错（模拟设备被占用/未广播）
        self.connect_calls = 0
        self.fail_find = 0                      # >0 时 find_by_address 返回 None（模拟缓存未命中）
        self.fail_direct = 0                    # >0 时 connect_direct 抛错
        self.stage_order: list[str] = []        # 记录三段式实际走了哪几段
        self.scan_results_override: list | None = None
        self._parser = framing.FrameParser(header=framing.REQ_HEADER)
        self._connected = False

    def describe(self):
        return "fake"

    @property
    def is_connected(self):
        return self._connected

    async def scan(self, timeout: float = 1.0):
        self.stage_order.append("scan")
        if self.scan_results_override is not None:
            return list(self.scan_results_override)
        return [ScanResult("FAKE", "fake", -50, "fake")]

    async def find_by_address(self, address: str, timeout: float = 10.0):
        self.stage_order.append("find")
        if self.fail_find > 0:
            self.fail_find -= 1
            return None
        return ScanResult(address, "fake-cached", -50, "fake")

    async def connect_direct(self, address: str, timeout: float = 20.0):
        self.stage_order.append("direct")
        if self.fail_direct > 0:
            self.fail_direct -= 1
            raise RuntimeError("direct connect failed (模拟)")
        await self.connect(ScanResult(address, "(direct)", None, "fake"))

    async def connect(self, target=None, timeout: float = 5.0):
        self.connect_calls += 1
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise RuntimeError("Not connected (模拟：设备被占用/未广播)")
        self._connected = True

    async def disconnect(self):
        self._connected = False

    async def write(self, data: bytes):
        self.writes.append((time.monotonic(), bytes(data)))
        if not self.auto_reply:
            return
        for frame in self._parser.feed(data):
            cmd = frame.cmd
            if self.drop.get(cmd, 0) > 0:
                self.drop[cmd] -= 1
                continue
            queued = self.reply_queue.get(cmd)
            if queued:
                reply = queued.pop(0)
            elif cmd == hc.CMD_IDENTIFY:
                reply = hc.response_frame(cmd, self.identify_payload)
            elif cmd == hc.CMD_REALTIME:
                reply = hc.response_frame(cmd, self.status_payload)
            elif cmd == hc.CMD_GET_THERMOSTAT:
                reply = hc.response_frame(cmd, hc.pack_u16([200, 250, 300, 350]))
            elif cmd == hc.CMD_GET_REPAIR:
                reply = hc.response_frame(cmd, hc.pack_u16([120, 60, 150, 90, 180, 60, 200, 45]))
            elif cmd == hc.CMD_GET_REFLOW:
                reply = hc.response_frame(cmd, hc.pack_u16([150, 60, 180, 90, 235, 45, 100, 30]))
            elif cmd == hc.CMD_GET_NAME:
                reply = hc.response_frame(cmd, b"HP20\x00")
            elif cmd in self.empty_ack_for:
                reply = hc.response_frame(cmd, b"")
            else:
                reply = hc.response_frame(cmd, b"\x00")   # 真机写命令 ACK 带 1 字节
            if self.reply_delay:
                await asyncio.sleep(self.reply_delay)
            if self.on_data is not None:
                self.on_data(reply)

    def simulate_disconnect(self, reason: str = "模拟掉线") -> None:
        self._connected = False
        cb = self.on_disconnect
        if cb is not None:
            cb(reason)

    def cmds(self) -> list[int]:
        out = []
        for _, data in self.writes:
            try:
                out.append(framing.parse_frame(data).cmd)
            except ValueError:
                out.append(-1)
        return out


class DeviceCase(unittest.IsolatedAsyncioTestCase):
    identify_payload = bytes((hc.DEV_ID, hc.FAMILY))

    async def asyncSetUp(self):
        self.transport = FakeTransport()
        self.transport.identify_payload = self.identify_payload
        self.device = Hp20Device(self.transport, timeout=0.3, tick=0.005,
                                 write_gap=0.015, poll_interval=30.0, retries=0)
        self.logs: list[str] = []
        self.device.on_system = self.logs.append
        await self.transport.connect()

    async def asyncTearDown(self):
        await self.device.close()


class TestIdentify(DeviceCase):
    async def test_hp20_identified_and_starts_polling(self):
        await self.device.open(handshake_delay=0)
        self.assertTrue(self.device.identified)
        self.assertTrue(self.device.hp20)
        self.assertEqual(self.device.info.dev_header, 0x0209)
        self.assertEqual(self.device.info.device_type, "HP20")
        self.assertEqual(self.transport.cmds()[0], hc.CMD_IDENTIFY)
        self.assertEqual(self.transport.writes[0][1], bytes.fromhex("AF0902000046"))

    async def test_wrong_payload_length_fails(self):
        self.transport.reply_queue[hc.CMD_IDENTIFY] = [hc.response_frame(0, b"\x09")]
        await self.device.open(handshake_delay=0)
        self.assertFalse(self.device.identified)
        self.assertIsNone(self.device.info)

    async def test_no_reply_fails(self):
        self.transport.drop[hc.CMD_IDENTIFY] = 1
        await self.device.open(handshake_delay=0)
        self.assertFalse(self.device.identified)


class TestNonHp20(DeviceCase):
    identify_payload = bytes((0x07, 0x03))     # EL15

    async def test_blocks_every_further_command(self):
        await self.device.open(handshake_delay=0)
        self.assertTrue(self.device.identified)
        self.assertFalse(self.device.hp20)
        self.assertEqual(self.device.info.device_type, "EL15")
        before = len(self.transport.writes)
        self.assertIsNone(await self.device.read_presets())
        self.assertFalse(await self.device.set_preheat(0))
        self.assertFalse(await self.device.poll_once())
        self.assertEqual(len(self.transport.writes), before, "非 HP20 不应再发任何帧")
        self.assertTrue(any("非 HP20" in line for line in self.logs))


class TestTransactions(DeviceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.device.open(handshake_delay=0)
        await asyncio.sleep(0.1)          # 让轮询的首拍落地，后面的断言只看自己发的帧
        self.transport.writes.clear()

    async def test_timeout_returns_false(self):
        self.transport.drop[hc.CMD_SET_PREHEAT] = 1
        started = time.monotonic()
        self.assertFalse(await self.device.set_preheat(0))
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertEqual(self.device.stats.timeouts, 1)
        self.assertTrue(any("应答超时" in line for line in self.logs))

    async def test_write_gap_respected(self):
        for ch in range(3):
            self.assertTrue(await self.device.set_preheat(ch))
        stamps = [ts for ts, data in self.transport.writes
                  if framing.parse_frame(data).cmd == hc.CMD_SET_PREHEAT]
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(gaps and all(g >= 0.010 for g in gaps), f"写间隔过小: {gaps}")

    async def test_busy_rejects_concurrent_command(self):
        self.transport.reply_delay = 0.12
        first = asyncio.ensure_future(self.device.set_preheat(0))
        await asyncio.sleep(0.02)
        second = await self.device.set_preheat(1)
        await first
        self.assertFalse(second)
        self.assertEqual(self.device.stats.busy_rejects, 1)
        self.assertTrue(any("指令任务繁忙，拒绝重复调用" in line for line in self.logs))

    async def test_poll_is_suppressed_while_write_in_flight(self):
        self.transport.reply_delay = 0.12
        task = asyncio.ensure_future(self.device.set_preheat(0))
        await asyncio.sleep(0.02)
        self.assertFalse(await self.device.poll_once())
        await task
        self.assertNotIn(hc.CMD_REALTIME, [c for c in self.transport.cmds() if c == hc.CMD_REALTIME])

    async def test_empty_ack_counts_as_failure(self):
        self.transport.empty_ack_for.add(hc.CMD_SET_PREHEAT)
        self.assertFalse(await self.device.set_preheat(0))
        self.assertEqual(self.device.stats.empty_acks, 1)
        self.assertTrue(any("应答 payload 为空" in line for line in self.logs))

    async def test_send_raw_full_frame(self):
        raw = bytes.fromhex("AF09020A003C")
        self.assertTrue(await self.device.send_raw(raw))
        self.assertEqual(self.transport.writes[-1][1], raw)

    async def test_send_raw_bytes_without_frame(self):
        self.assertTrue(await self.device.send_raw(b"\x01\x02\x03"))
        self.assertEqual(self.transport.writes[-1][1], b"\x01\x02\x03")


class TestHeatingControl(DeviceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.device.open(handshake_delay=0)
        await asyncio.sleep(0.1)
        self.transport.writes.clear()

    def heating_frames(self):
        return [framing.parse_frame(d) for _, d in self.transport.writes
                if framing.parse_frame(d).cmd in (hc.CMD_SET_THERMOSTAT,
                                                 hc.CMD_SET_PREHEAT)]

    async def test_start_heating_without_target_sends_only_channel(self):
        """target=None：只用挡位预设，只发一帧 0x05（不覆盖目标）。"""
        await self.device.read_presets()          # 让预设已知（[200,250,300,350]）
        self.transport.writes.clear()
        self.assertTrue(await self.device.start_heating(2))
        frames = self.heating_frames()
        self.assertEqual([f.cmd for f in frames], [hc.CMD_SET_PREHEAT])
        self.assertEqual(frames[0].raw, hc.frame_set_preheat(2))

    async def test_start_heating_custom_target_after_channel(self):
        """BUG 修复点：自定义目标必须在 0x05 之后，否则会被挡位预设覆盖。"""
        self.assertTrue(await self.device.start_heating(2, 150))
        frames = self.heating_frames()
        self.assertEqual([f.cmd for f in frames],
                         [hc.CMD_SET_PREHEAT, hc.CMD_SET_THERMOSTAT],
                         "先发挡位(0x05)，再把目标覆盖成自定义值(0x03)")
        self.assertEqual(frames[0].raw, hc.frame_set_preheat(2))
        self.assertEqual(frames[1].raw, hc.frame_set_thermostat(hc.TARGET_INDEX, 150))
        self.assertEqual(frames[1].payload[0], hc.TARGET_INDEX)
        self.assertEqual(int.from_bytes(frames[1].payload[1:3], "little", signed=True), 150)

    async def test_custom_target_equal_to_preset_is_skipped(self):
        """自定义目标与挡位预设相同 -> 不必要地再发 0x03。"""
        await self.device.read_presets()          # FAKE 返回 [200,250,300,350]
        self.assertEqual(self.device._preset_of(1), 250)
        self.transport.writes.clear()
        self.assertTrue(await self.device.start_heating(1, 250))
        frames = self.heating_frames()
        self.assertEqual([f.cmd for f in frames], [hc.CMD_SET_PREHEAT])
        self.assertTrue(any("预设相同" in line for line in self.logs))

    async def test_start_heating_aborts_when_channel_write_fails(self):
        self.transport.drop[hc.CMD_SET_PREHEAT] = 1
        self.assertFalse(await self.device.start_heating(0, 60))
        self.assertNotIn(hc.CMD_SET_THERMOSTAT, self.transport.cmds(),
                         "选挡位失败就不该再发自定义目标")

    async def test_stop_heating_defaults_to_t4_channel(self):
        """默认停止 = 0x05 [3] 选 T4 停止通道（等价官方 APP 的停止）。"""
        self.assertTrue(await self.device.stop_heating())
        tx = [d for _, d in self.transport.writes
              if framing.parse_frame(d).cmd == hc.CMD_SET_PREHEAT]
        self.assertEqual(framing.to_hex(tx[-1]), "AF 09 02 05 01 03 3D")
        self.assertTrue(any("T4" in line for line in self.logs))
        # 默认路径不该再发 0x03 置 0
        cmds = [framing.parse_frame(d).cmd for _, d in self.transport.writes]
        self.assertNotIn(hc.CMD_SET_THERMOSTAT, cmds)

    async def test_stop_heating_by_target_is_available_as_backup(self):
        self.assertTrue(await self.device.stop_heating_by_target())
        tx = [d for _, d in self.transport.writes
              if framing.parse_frame(d).cmd == hc.CMD_SET_THERMOSTAT]
        self.assertEqual(framing.to_hex(tx[-1]), "AF 09 02 03 03 04 00 00 3C")
        self.assertTrue(any("备用" in line for line in self.logs))

    async def test_stop_and_start_are_serialised(self):
        """两次事务都遵守写间隔与单命令在途（不会并发写信）。"""
        self.assertTrue(await self.device.stop_heating())
        self.assertTrue(await self.device.start_heating(1, 80))
        stamps = [ts for ts, d in self.transport.writes
                  if framing.parse_frame(d).cmd in (hc.CMD_SET_THERMOSTAT, hc.CMD_SET_PREHEAT)]
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(g >= 0.010 for g in gaps), f"写间隔过小: {gaps}")


class TestRetry(unittest.IsolatedAsyncioTestCase):
    async def test_retry_succeeds_on_second_attempt(self):
        transport = FakeTransport()
        device = Hp20Device(transport, timeout=0.15, tick=0.005, write_gap=0.0,
                            poll_interval=30.0, retries=1)
        logs: list[str] = []
        device.on_system = logs.append
        await transport.connect()
        await device.open(handshake_delay=0)
        try:
            transport.writes.clear()
            transport.drop[hc.CMD_SET_PREHEAT] = 1
            self.assertTrue(await device.set_preheat(2))
            self.assertEqual(device.stats.retries, 1)
            self.assertTrue(any("重试" in line for line in logs))
        finally:
            await device.close()
            await transport.disconnect()


class TestDataPaths(DeviceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.device.open(handshake_delay=0)
        await asyncio.sleep(0.1)
        self.transport.writes.clear()

    async def test_read_presets_and_params_callback(self):
        got = []
        self.device.on_params = lambda kind, values, silent=False: got.append((kind, values))
        self.assertEqual(await self.device.read_presets(), [200, 250, 300, 350])
        self.assertEqual(got, [("presets", [200, 250, 300, 350])])

    async def test_read_name(self):
        self.assertEqual(await self.device.read_name(), "HP20")

    async def test_status_callback(self):
        seen = []
        self.device.on_status = seen.append
        self.assertTrue(await self.device.poll_once())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].current_temp, 60)
        self.assertEqual(seen[0].input_voltage_raw, 200)   # [3:5] 是输入电压 ×0.1V
        self.assertEqual(seen[0].voltage_text, "20.0V")
        self.assertEqual(seen[0].mode_name, "加热")

    async def test_short_status_payload_counted(self):
        self.transport.status_payload = b"\x01\x02\x03"
        self.assertTrue(await self.device.poll_once())
        self.assertEqual(self.device.stats.bad_status, 1)

    async def _fresh_device(self, auto_refresh: bool = True):
        transport = FakeTransport()
        device = Hp20Device(transport, timeout=0.3, tick=0.005, write_gap=0.0,
                            poll_interval=0.05, retries=0,
                            auto_refresh_params=auto_refresh)
        device.on_system = self.logs.append
        await transport.connect()
        await device.open(handshake_delay=0)
        await asyncio.sleep(0.15)      # 先用原 payload 建立 w2 基线，再看"变化"
        transport.writes.clear()
        return transport, device

    async def test_ticking_w2_never_triggers_param_reads(self):
        """L 阶段 bug：W2 每秒都在变，绝不能用它当补读触发条件。"""
        for auto_refresh in (True, False):
            transport, device = await self._fresh_device(auto_refresh=auto_refresh)
            try:
                for w2 in (0x0087, 0x0086, 0x0085, 0x0084, 0x0083):
                    transport.status_payload = hc.build_realtime_payload(
                        current_temp=60, supply_power_w=140, output_power_w=6,
                        target_temp=180, channel_or_stage=0, mode=2, w2=w2)
                    await asyncio.sleep(0.12)
                cmds = transport.cmds()
                self.assertNotIn(hc.CMD_GET_THERMOSTAT, cmds,
                                 f"auto_refresh={auto_refresh} 时不该补读预设")
                self.assertNotIn(hc.CMD_GET_REPAIR, cmds,
                                 f"auto_refresh={auto_refresh} 时不该补读维修参数")
            finally:
                await device.close()

    async def test_default_is_not_auto_refresh(self):
        """默认关闭：用户没点读取，就不许自动发 0x04/0x0C。"""
        transport = FakeTransport()
        device = Hp20Device(transport, timeout=0.3, tick=0.005, write_gap=0.0,
                            poll_interval=0.05, retries=0)
        self.assertFalse(device.auto_refresh_params)
        await device.close()

    async def test_mode_change_triggers_refresh_only_when_enabled(self):
        """能力保留但收紧为「mode 变化」触发；关闭时连 mode 变化也不读。"""
        transport, device = await self._fresh_device(auto_refresh=True)
        try:
            transport.status_payload = hc.build_realtime_payload(
                current_temp=60, supply_power_w=140, output_power_w=6, target_temp=180,
                channel_or_stage=0, mode=4, w2=0x0087)     # 2 -> 4：mode 变化
            await asyncio.sleep(0.5)
            cmds = transport.cmds()
            self.assertIn(hc.CMD_GET_THERMOSTAT, cmds)
            self.assertIn(hc.CMD_GET_REPAIR, cmds)
        finally:
            await device.close()

    async def test_internal_reads_are_silent(self):
        """内部补读要标记 silent，界面据此不弹提示。"""
        got = []
        self.device.on_params = lambda kind, values, silent=False: got.append(
            (kind, silent))
        await self.device.read_presets(internal=True)
        await self.device.read_presets()
        self.assertEqual(got, [("presets", True), ("presets", False)])

    async def test_rearm_poll_applies_new_interval(self):
        """界面改轮询间隔后立刻重排，不必等完旧的长周期。"""
        transport = FakeTransport()
        device = Hp20Device(transport, timeout=0.3, tick=0.005, write_gap=0.0,
                            poll_interval=30.0, retries=0)
        device.on_system = self.logs.append
        await transport.connect()
        await device.open(handshake_delay=0)
        try:
            transport.writes.clear()
            device.poll_interval = 0.05
            device.rearm_poll()
            await asyncio.sleep(0.35)
            self.assertGreaterEqual(transport.cmds().count(hc.CMD_REALTIME), 3)
        finally:
            await device.close()


if __name__ == "__main__":
    unittest.main()
