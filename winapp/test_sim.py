"""模拟设备：实测命令集、热学行为、阶段推进、状态解码、断链模拟、端到端。"""
import asyncio
import os
import tempfile
import unittest

import framing
import hp20_commands as hc
from hp20_device import Hp20Device
from logs import CsvRecorder
from sim_device import Hp20SimCore, SimTransport


def req(cmd, payload=b""):
    return framing.parse_frame(framing.build_frame(cmd, payload, dev_id=hc.DEV_ID,
                                                   family=hc.FAMILY))


class TestSimCommands(unittest.TestCase):
    def setUp(self):
        self.core = Hp20SimCore()

    def test_identify_reports_ff_header_and_09_02_payload(self):
        reply = self.core.handle(req(hc.CMD_IDENTIFY))
        frame = framing.parse_frame(reply[0])
        self.assertEqual(frame.dev_header, 0xFFFF)
        self.assertEqual(frame.cmd, hc.CMD_IDENTIFY)
        self.assertTrue(hc.identify_matches(frame.payload))

    def test_real_defaults(self):
        frame = framing.parse_frame(self.core.handle(req(hc.CMD_GET_THERMOSTAT))[0])
        self.assertEqual(hc.unpack_i16(frame.payload, 4), [66, 200, 300, 0])
        self.core.handle(req(hc.CMD_SET_THERMOSTAT, bytes((0,)) + hc.pack_i16(123)))
        self.assertEqual(self.core.presets[0], 123)

    def test_set_t5(self):
        self.core.handle(req(hc.CMD_SET_THERMOSTAT, bytes((hc.TARGET_INDEX,))
                             + hc.pack_i16(60)))
        self.assertEqual(self.core.t5, 60)

    def test_stop_by_setting_target_below_current(self):
        """真机实测：把 T5 设到低于当前温度就等于停止加热（功率归零、温度回落）。"""
        core = Hp20SimCore()
        core.handle(req(hc.CMD_SET_PREHEAT, bytes((0,))))
        core.handle(req(hc.CMD_SET_THERMOSTAT, bytes((hc.TARGET_INDEX,)) + hc.pack_i16(60)))
        for _ in range(60):
            core.tick(0.05)
        self.assertGreater(core.output_power_w, 0)
        temp_before = core.temp

        core.handle(req(hc.CMD_SET_THERMOSTAT, bytes((hc.TARGET_INDEX,)) + hc.pack_i16(0)))
        core.tick(0.05)
        self.assertEqual(core.output_power_w, 0, "目标置 0 后功率应立刻归零")
        for _ in range(40):
            core.tick(0.05)
        self.assertLess(core.temp, temp_before, "温度应开始回落")
        status = hc.parse_realtime(core.status_payload())
        self.assertEqual(status.target_temp, 0)
        self.assertEqual(status.mode_name, "加热")   # 模式不因停热而改变

    def test_reflow_and_repair_roundtrip(self):
        curve = [100, 10, 120, 20, 140, 30, 60, 40]
        self.core.handle(req(hc.CMD_SET_REFLOW, hc.pack_u16(curve)))
        frame = framing.parse_frame(self.core.handle(req(hc.CMD_GET_REFLOW))[0])
        self.assertEqual(hc.unpack_u16(frame.payload, 8), curve)
        params = [80, 5, 90, 6, 100, 7, 110, 8]
        self.core.handle(req(hc.CMD_SET_REPAIR, hc.pack_u16(params)))
        frame = framing.parse_frame(self.core.handle(req(hc.CMD_GET_REPAIR))[0])
        self.assertEqual(hc.unpack_u16(frame.payload, 8), params)

    def test_ble_name_roundtrip(self):
        self.core.handle(req(hc.CMD_SET_NAME, hc.build_name_payload("MY-HP20")))
        frame = framing.parse_frame(self.core.handle(req(hc.CMD_GET_NAME))[0])
        self.assertEqual(hc.decode_name(frame.payload), "MY-HP20")

    def test_preheat_sets_target_to_that_channel_preset(self):
        """实测：0x05 [ch] 会把目标温度改写成该挡位预设（自定义目标必须在它之后发）。"""
        self.core.handle(req(hc.CMD_SET_THERMOSTAT, bytes((hc.TARGET_INDEX,))
                             + hc.pack_i16(80)))
        self.core.handle(req(hc.CMD_SET_PREHEAT, bytes((2,))))
        self.assertEqual((self.core.mode, self.core.channel, self.core.heating),
                         (hc.MODE_HEATING, 2, True))
        self.assertEqual(self.core.t5, self.core.presets[2])   # 300，而不是 80
        self.assertEqual(self.core.goal, float(self.core.presets[2]))

    def test_custom_target_after_channel_wins(self):
        """先选挡位、再下发自定义目标 -> 目标以自定义为准（H 阶段修复的正是这个顺序）。"""
        self.core.handle(req(hc.CMD_SET_PREHEAT, bytes((0,))))
        self.assertEqual(self.core.t5, self.core.presets[0])
        self.core.handle(req(hc.CMD_SET_THERMOSTAT, bytes((hc.TARGET_INDEX,))
                             + hc.pack_i16(120)))
        self.assertEqual(self.core.t5, 120)
        self.assertEqual(self.core.goal, 120.0)

    def test_timer_counts_down_in_64_base_units(self):
        """模拟设备：20 min 超时 = 1280（64 进制），每真实秒 -1；停热后回到哨兵 OFF。"""
        core = Hp20SimCore()
        core.handle(req(hc.CMD_SET_PREHEAT, bytes((0,))))
        self.assertEqual(Hp20SimCore.TIMER_START_PACKED, 20 * 64)
        self.assertEqual(hc.parse_realtime(core.status_payload()).timer_text, "20:00")
        seen = []
        for _ in range(70):
            core.tick(1.0)
            text = hc.parse_realtime(core.status_payload()).timer_text
            seen.append(text)
            self.assertNotEqual(text, "OFF", "模拟设备不该产出非法秒")     # SS=60..63 被跳过
            self.assertRegex(text, r"^\d{2}:[0-5]\d$")
        self.assertLess(seen[-1], seen[0], "计时必须单调递减")
        self.assertEqual(seen[0], "19:59")
        core.handle(req(hc.CMD_SET_PREHEAT, bytes((hc.STOP_CHANNEL,))))
        self.assertEqual(hc.parse_realtime(core.status_payload()).timer_text, "OFF")

    def test_stop_channel_reports_t4_field(self):
        """停止后状态里的挡位字段 = 3（T4），设备物理面板显示 T3。"""
        self.core.handle(req(hc.CMD_SET_PREHEAT, bytes((hc.STOP_CHANNEL,))))
        status = hc.parse_realtime(self.core.status_payload())
        self.assertEqual(status.channel_or_stage, hc.STOP_CHANNEL)
        self.assertEqual(status.slot_name, "T4")
        self.assertEqual(status.slot_text, "T4（停止通道）")
        self.assertEqual(status.target_temp, 0)
        self.assertFalse(self.core.heating)

    def test_unknown_command_is_silent(self):
        self.assertEqual(self.core.handle(req(0x7E)), [])

    def test_ack_payload_modes(self):
        frame = req(hc.CMD_SET_PREHEAT, bytes((0,)))
        one = framing.parse_frame(self.core.handle(frame, "one")[0])
        empty = framing.parse_frame(self.core.handle(frame, "empty")[0])
        echo = framing.parse_frame(self.core.handle(frame, "echo")[0])
        self.assertEqual(one.payload, b"\x00")     # 真机实测 1 字节
        self.assertEqual(empty.payload, b"")
        self.assertEqual(echo.payload, bytes((0,)))
        self.assertEqual(SimTransport().ack_payload, "one")


class TestSimPhysics(unittest.TestCase):
    def test_heating_approaches_goal(self):
        core = Hp20SimCore()
        core.handle(req(hc.CMD_SET_THERMOSTAT, bytes((hc.TARGET_INDEX,))
                           + hc.pack_i16(200)))
        core.handle(req(hc.CMD_SET_PREHEAT, bytes((0,))))
        core.handle(req(hc.CMD_SET_THERMOSTAT, bytes((hc.TARGET_INDEX,))
                           + hc.pack_i16(200)))     # 自定义目标在选挡位之后
        for _ in range(400):
            core.tick(0.02)
        self.assertGreater(core.temp, 60.0)
        self.assertLess(core.temp, 200.0)
        self.assertGreater(core.output_power_w, 0)
        self.assertLessEqual(core.output_power_w, core.SUPPLY_POWER_W)
        self.assertGreater(core.timer, 7.0)

    def test_cooling_returns_to_ambient(self):
        core = Hp20SimCore()
        core.temp = 120.0
        for _ in range(6000):
            core.tick(0.05)
        self.assertAlmostEqual(core.temp, core.AMBIENT, delta=1.5)
        self.assertEqual(core.output_power_w, 0)

    def test_reflow_advances_through_stages_then_idle(self):
        core = Hp20SimCore()
        core.enter_reflow()
        self.assertEqual((core.mode, core.stage), (hc.MODE_REFLOW, 0))
        self.assertEqual(core.goal, float(core.reflow[0]))
        seen = []
        for _ in range(700):        # 真机默认 120+90+60+300 = 570 s
            core.tick(1.0)
            seen.append(core.stage)
        self.assertEqual(core.mode, hc.MODE_IDLE)
        self.assertFalse(core.heating)
        self.assertEqual(sorted(set(seen)), [0, 1, 2, 3])

    def test_repair_program(self):
        core = Hp20SimCore()
        core.enter_repair(1)
        self.assertEqual((core.mode, core.channel), (hc.MODE_REPAIR, 1))
        self.assertEqual(core.goal, float(core.repair[2]))
        for _ in range(450):
            core.tick(1.0)
        self.assertEqual(core.mode, hc.MODE_IDLE)


class TestSimStatusPayload(unittest.TestCase):
    def test_payload_shape_and_fields(self):
        core = Hp20SimCore()
        core.handle(req(hc.CMD_SET_THERMOSTAT, bytes((hc.TARGET_INDEX,))
                           + hc.pack_i16(120)))
        core.handle(req(hc.CMD_SET_PREHEAT, bytes((1,))))
        core.handle(req(hc.CMD_SET_THERMOSTAT, bytes((hc.TARGET_INDEX,))
                           + hc.pack_i16(120)))     # 覆盖挡位预设 = 200
        for _ in range(100):
            core.tick(0.02)
        payload = core.status_payload()
        self.assertEqual(len(payload), 11)
        s = hc.parse_realtime(payload)
        self.assertIsNotNone(s)
        self.assertEqual(s.current_temp, int(round(core.temp)))   # [0:2] 真实温度
        self.assertEqual(s.input_voltage_raw, Hp20SimCore.INPUT_VOLTAGE_RAW)
        self.assertEqual(s.supply_power_w, 140)
        self.assertEqual(s.output_power_w, core.output_power_w)
        self.assertEqual(s.mode, hc.MODE_HEATING)
        self.assertEqual(s.mode_name, "加热")
        self.assertEqual(s.slot_name, "T2")
        self.assertEqual(s.target_temp, 120)
        self.assertFalse(s.celsius_bit)          # 真机 bit14 从不置位
        self.assertEqual(s.unit, "℃")

    def test_idle_payload(self):
        s = hc.parse_realtime(Hp20SimCore().status_payload())
        self.assertEqual(s.mode, hc.MODE_IDLE)
        self.assertEqual(s.mode_name, "待机")
        self.assertEqual(s.output_power_w, 0)
        self.assertEqual(s.w2, Hp20SimCore.W2_RAW)
        self.assertEqual(s.w2_text, "W2=0x29BF")
        self.assertEqual(s.timer_text, "OFF")     # SS=(0x29BF&0x3F)=63 >= 60 -> OFF

    def test_reflow_reports_stage_names(self):
        core = Hp20SimCore()
        core.enter_reflow()
        s = hc.parse_realtime(core.status_payload())
        self.assertEqual(s.mode_name, "回流焊")
        self.assertEqual(s.slot_text, "PH 预热")


class TestSimDisconnect(unittest.IsolatedAsyncioTestCase):
    async def test_simulate_disconnect_notifies_upper_layer(self):
        transport = SimTransport()
        seen = []
        transport.on_disconnect = seen.append
        await transport.connect()
        self.assertTrue(transport.is_connected)
        transport.simulate_disconnect("测试掉线")
        self.assertFalse(transport.is_connected)
        self.assertEqual(seen, ["测试掉线"])

    async def test_write_after_disconnect_raises(self):
        transport = SimTransport()
        await transport.connect()
        transport.simulate_disconnect()
        with self.assertRaises(RuntimeError):
            await transport.write(b"\xaf")


class TestSimEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_full_flow(self):
        transport = SimTransport(latency=0.002, tick_dt=0.02)
        device = Hp20Device(transport, timeout=hc.RESPONSE_TIMEOUT_S, tick=0.005,
                            write_gap=0.005, poll_interval=0.05, retries=0,
                            handshake_delay=0.0)
        statuses, frames = [], []
        device.on_status = statuses.append
        device.on_frame = lambda d, b: frames.append((d, b))
        csv_path = os.path.join(tempfile.mkdtemp(), "curve.csv")
        recorder = CsvRecorder(csv_path)     # 提前创建：finally 里一定能 close
        await transport.connect()
        try:
            await device.open()
            self.assertTrue(device.identified)
            self.assertEqual(device.info.device_type, "HP20")
            self.assertEqual(await device.read_presets(), [66, 200, 300, 0])
            # 走真实顺序：先选挡位、再覆盖自定义目标（H 阶段修复）
            self.assertTrue(await device.start_heating(0, 60))

            t0 = asyncio.get_running_loop().time()
            last = None
            while asyncio.get_running_loop().time() - t0 < 1.2:
                await asyncio.sleep(0.03)
                cur = device.status
                if cur is not None and cur is not last:
                    last = cur
                    recorder.record(cur)
            self.assertGreater(len(statuses), 3)
            self.assertGreater(device.status.current_temp, Hp20SimCore.AMBIENT)
            self.assertLessEqual(device.status.current_temp, 60)
            self.assertEqual(device.status.mode_name, "加热")
            self.assertEqual(device.status.target_temp, 60)
            self.assertEqual(device.status.input_voltage_raw, 278)
            self.assertEqual(device.status.voltage_text, "27.8V")
            self.assertEqual(device.status.supply_power_w, 140)

            curve = [100, 10, 120, 20, 140, 30, 60, 40]
            self.assertTrue(await device.write_reflow(curve))
            self.assertEqual(await device.read_reflow(), curve)
            params = [80, 5, 90, 6, 100, 7, 110, 8]
            self.assertTrue(await device.write_repair(params))
            self.assertEqual(await device.read_repair(), params)
            self.assertEqual(await device.read_name(), "HP20")
        finally:
            rows = recorder.close()
            await device.close()
            await transport.disconnect()
        self.assertGreater(rows, 3)
        self.assertIn("功率占比(%)", recorder.header)     # 表头已中文化
        tx = sum(1 for d, _ in frames if d == "TX")
        rx = sum(1 for d, _ in frames if d == "RX")
        self.assertGreater(tx, 8)
        self.assertEqual(rx, tx, "模拟设备对每个请求恰好回一帧")
        self.assertEqual(device.stats.timeouts, 0)
        self.assertEqual(device.stats.empty_acks, 0)

    async def test_no_stop_command_in_protocol(self):
        """U1：协议没有停止命令，设备层也不提供伪造实现。"""
        device = Hp20Device(SimTransport(), poll_interval=30.0)
        self.assertFalse(hasattr(device, "stop"))
        self.assertFalse(hasattr(device, "emergency_stop"))


if __name__ == "__main__":
    unittest.main()