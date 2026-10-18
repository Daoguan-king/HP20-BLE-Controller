"""真机实测帧回归夹具。

数据来源：2026-10-17 在真机 HP20（AA:BB:CC:DD:EE:FF，广播名 HP20）上与 A1 协议交叉验证的抓包。
整帧三条是实测原样；预设/回流/维修只有 payload 字节，帧由本实现编码器补 LRC 生成，
因此这里断言的是「payload 解码结果」，LRC 正确性由另外三条实测整帧覆盖。
"""
import asyncio
import re
import unittest

import framing
import hp20_commands as hc
from hp20_device import Hp20Device
from test_device import FakeTransport

REAL_IDENTIFY_RX = "DF FF FF 00 02 09 02 16"
REAL_WRITE_ACK_RX = "DF 09 02 03 01 00 12"
REAL_STATUS_RX = "DF 09 02 0A 0B 2C 00 41 C8 00 00 02 FA 0C A2 03 1F"
# F 阶段真机帧（lead 在 60℃/目标 60℃/功率 6 时抓的）：W2=0x29BF，按 MM:SS 会算出 38:63（非法）
REAL_STATUS_RX_W2_29BF = "DF 09 02 0A 0B 3C 00 41 C8 00 02 06 3C 20 BF 29 70"
# G 阶段真机帧（换 140W/27.8V 电源后）：66℃ / 供电140 / 输入27.8V / 输出11W / 目标66 / T1 / mode2
REAL_STATUS_RX_G = "DF 09 02 0A 0B 42 00 8C 16 01 02 0B 42 20 BF 29 C5"
REAL_PRESETS_PAYLOAD = "64 00 C8 00 2C 01 00 00"                                  # 100/200/300/0
REAL_REFLOW_PAYLOAD = "96 00 78 00 B4 00 5A 00 04 01 3C 00 50 00 2C 01"           # 150/120 180/90 260/60 80/300
REAL_REPAIR_PAYLOAD = "64 00 78 00 50 00 78 00 50 00 78 00 04 01 78 00"           # 100/120 80/120 80/120 260/120
REAL_NAME = "HP20"


def hx(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


class TestRealIdentifyFrame(unittest.TestCase):
    def test_frame_parses_with_ff_header(self):
        frame = framing.parse_frame(hx(REAL_IDENTIFY_RX))
        self.assertEqual(frame.header, framing.RESP_HEADER)
        self.assertEqual(frame.dev_header, 0xFFFF)      # 真机识别应答设备头是 FF FF
        self.assertEqual(frame.cmd, hc.CMD_IDENTIFY)
        self.assertEqual(framing.to_hex(frame.payload), "09 02")

    def test_payload_identifies_hp20(self):
        frame = framing.parse_frame(hx(REAL_IDENTIFY_RX))
        self.assertTrue(hc.identify_matches(frame.payload))

    def test_identify_matches_rejects_other_types(self):
        self.assertFalse(hc.identify_matches(b"\x07\x03"))    # EL15
        self.assertFalse(hc.identify_matches(b"\x09"))         # 太短
        self.assertFalse(hc.identify_matches(b""))


class TestRealWriteAck(unittest.TestCase):
    def test_ack_has_one_byte_payload(self):
        frame = framing.parse_frame(hx(REAL_WRITE_ACK_RX))
        self.assertEqual(frame.cmd, hc.CMD_SET_THERMOSTAT)
        self.assertEqual(len(frame.payload), 1)
        self.assertGreaterEqual(len(frame.payload), hc.ACK_MIN_PAYLOAD)
        self.assertEqual(frame.payload, b"\x00")

    def test_ack_frame_length_matches_length_field(self):
        raw = hx(REAL_WRITE_ACK_RX)
        self.assertEqual(len(raw), 6 + raw[4])


class TestRealStatusFrame(unittest.TestCase):
    def setUp(self):
        self.frame = framing.parse_frame(hx(REAL_STATUS_RX))
        self.status = hc.parse_realtime(self.frame.payload)

    def test_frame_shape(self):
        self.assertEqual(self.frame.cmd, hc.CMD_REALTIME)
        self.assertEqual(len(self.frame.payload), 11)
        self.assertEqual(len(self.frame.raw), 17)

    def test_field_decode(self):
        """实测标定：payload[0:2] 才是当前温度，[3:5] 恒 200 只是诊断值。"""
        s = self.status
        self.assertEqual(s.current_temp, 0x002C)           # 44 = 真实温度
        self.assertEqual(s.input_voltage_raw, 0x00C8)      # 200 -> 20.0V 输入电压
        self.assertEqual(s.voltage_text, "20.0V")
        self.assertEqual(s.supply_power_w, 0x41)           # 65 = 供电功率 W
        self.assertEqual(s.output_state_raw, 0)     # 该帧 [5]=0x00 -> 待机
        self.assertEqual(s.output_power_w, 2)              # [6] 原始值 2 W（设备残留）
        # V 阶段：待机帧的功率一律归一化为 0（占比同理），原始值仍在 output_power_w
        self.assertEqual(s.effective_power_w, 0)
        self.assertAlmostEqual(s.power_percent, 0.0, places=2)
        self.assertEqual(s.output_state_text, "待机")
        self.assertEqual(s.target_temp, 250)               # W1 & 0x3FF
        self.assertEqual(s.channel_or_stage, 3)            # 挡位 T4
        self.assertEqual(s.mode, 0)                        # 实测 0=待机
        self.assertEqual(s.mode_name, "待机")
        self.assertEqual(s.slot_name, "T4")
        self.assertEqual(s.w2, 0x03A2)
        self.assertEqual(s.w2_text, "W2=0x03A2")
        self.assertEqual(s.timer_text, "14:34")     # 0x03A2: MM=(>>6)&0x7F=14, SS=&0x3F=34
        self.assertEqual(s.unit, "℃")

    def test_voltage_never_used_as_temperature(self):
        """[3:5] 是输入电压（×0.1V）；当温度用就会显示 200° —— 这正是早期用户报的 bug。"""
        s = self.status
        self.assertNotEqual(s.current_temp, s.input_voltage_raw)
        self.assertEqual(s.input_voltage_raw, 200)
        self.assertEqual(s.voltage_text, "20.0V")
        self.assertLess(s.current_temp, 100)

    def test_write_read_closed_loop_for_target(self):
        """把「250 -> 30 -> 250」三步实验里的 W1 值都过一遍解码。"""
        def payload_with_w1(w1: int) -> bytes:
            raw = bytearray(hx(REAL_STATUS_RX))
            raw[12] = w1 & 0xFF           # payload[7]
            raw[13] = (w1 >> 8) & 0xFF    # payload[8]
            body = bytes(raw[:-1])
            return body + bytes((framing.compute_lrc(body),))
        for w1, expect in ((0x0CFA, 250), (0x0C1E, 30)):
            status = hc.parse_realtime(framing.parse_frame(
                payload_with_w1(w1)).payload)
            self.assertEqual(status.target_temp, expect)


class TestRealW2Regression(unittest.TestCase):
    """F 阶段真机 bug：W2=0x29BF 被显示成「计时 38:63」。"""

    def setUp(self):
        self.frame = framing.parse_frame(hx(REAL_STATUS_RX_W2_29BF))
        self.status = hc.parse_realtime(self.frame.payload)

    def test_frame_is_valid(self):
        self.assertEqual(self.frame.cmd, hc.CMD_REALTIME)
        self.assertEqual(len(self.frame.payload), 11)

    def test_fields_match_lead_observation(self):
        s = self.status
        self.assertEqual(s.current_temp, 60)
        self.assertEqual(s.input_voltage_raw, 200)
        self.assertEqual(s.voltage_text, "20.0V")
        self.assertEqual(s.supply_power_w, 0x41)
        self.assertEqual(s.output_power_w, 6)
        self.assertEqual(s.target_temp, 60)
        self.assertEqual(s.mode, 2)
        self.assertEqual(s.mode_name, "加热")
        self.assertEqual(s.slot_name, "T1")
        self.assertEqual(s.w2, 0x29BF)

    def test_29bf_is_the_timer_off_sentinel(self):
        """W2=0x29BF(10687) 是"计时未启用"哨兵：低位 SS=(0x29BF&0x3F)=63 >= 60 -> OFF。"""
        s = self.status
        self.assertEqual(s.w2, 0x29BF)
        self.assertEqual(s.timer_packed, 0x29BF)
        self.assertEqual(s.w2_low6, 0x3F)          # 63 -> 触发 OFF 判定
        self.assertEqual(s.timer_text, "OFF")
        self.assertNotIn("38:63", s.summary())
        self.assertIn("加热时间 OFF", s.summary())
        self.assertEqual(s.w2_text, "W2=0x29BF")


class TestRealGFrame(unittest.TestCase):
    """G 阶段真机帧：换 140W/27.8V 电源后三个字段一次性确证。"""

    def setUp(self):
        self.frame = framing.parse_frame(hx(REAL_STATUS_RX_G))
        self.status = hc.parse_realtime(self.frame.payload)

    def test_frame_valid(self):
        self.assertEqual(len(self.frame.raw), 17)
        self.assertEqual(len(self.frame.payload), 11)

    def test_all_fields(self):
        s = self.status
        self.assertEqual(s.current_temp, 66)
        self.assertEqual(s.supply_power_w, 140)
        self.assertEqual(s.input_voltage_raw, 278)
        self.assertEqual(s.voltage_text, "27.8V")
        self.assertEqual(s.output_power_w, 11)
        self.assertEqual(s.power_percent_text, "7.9%")
        self.assertEqual(s.target_temp, 66)
        self.assertEqual(s.slot_name, "T1")
        self.assertEqual(s.mode, 2)
        self.assertEqual(s.mode_name, "加热")
        self.assertEqual(s.w2, 0x29BF)
        self.assertEqual(s.output_state_raw, 0x02)      # 加热模式运行中（保温时也是 0x02）
        self.assertEqual(s.output_state_text, "加热模式运行中")

    def test_summary_carries_supply_and_voltage(self):
        text = self.status.summary()
        self.assertIn("供电 140W", text)
        self.assertIn("输入 27.8V", text)
        self.assertIn("11W（7.9%）", text)


class TestSupplyPowerIndependence(unittest.TestCase):
    """供电功率/输入电压变化不影响其它字段（旧电源 65W/20.0V 版本）。"""

    def test_old_psu_variant(self):
        payload = hc.build_realtime_payload(
            current_temp=66, supply_power_w=65, output_power_w=6, target_temp=66,
            channel_or_stage=0, mode=2, w2=0x29BF, input_voltage_raw=200)
        s = hc.parse_realtime(payload)
        self.assertEqual(s.current_temp, 66)
        self.assertEqual(s.supply_power_w, 65)
        self.assertEqual(s.voltage_text, "20.0V")
        self.assertEqual(s.output_power_w, 6)
        self.assertEqual(s.power_percent_text, "9.2%")
        self.assertEqual(s.target_temp, 66)
        self.assertEqual(s.mode, 2)
        self.assertEqual(s.w2, 0x29BF)

    def test_changing_supply_does_not_touch_other_fields(self):
        old = hc.parse_realtime(hc.build_realtime_payload(
            current_temp=66, supply_power_w=65, output_power_w=6, target_temp=66,
            channel_or_stage=0, mode=2, w2=0x29BF, input_voltage_raw=200))
        new = hc.parse_realtime(hc.build_realtime_payload(
            current_temp=66, supply_power_w=140, output_power_w=11, target_temp=66,
            channel_or_stage=0, mode=2, w2=0x29BF, input_voltage_raw=278))
        self.assertEqual((old.current_temp, old.target_temp, old.mode, old.w2),
                         (new.current_temp, new.target_temp, new.mode, new.w2))


class TestRealParamPayloads(unittest.TestCase):
    def test_presets(self):
        frame = framing.parse_frame(hc.response_frame(hc.CMD_GET_THERMOSTAT,
                                                      hx(REAL_PRESETS_PAYLOAD)))
        self.assertEqual(hc.unpack_i16(frame.payload, 4), [100, 200, 300, 0])

    def test_reflow(self):
        frame = framing.parse_frame(hc.response_frame(hc.CMD_GET_REFLOW,
                                                      hx(REAL_REFLOW_PAYLOAD)))
        self.assertEqual(hc.unpack_u16(frame.payload, 8),
                         [150, 120, 180, 90, 260, 60, 80, 300])

    def test_repair(self):
        frame = framing.parse_frame(hc.response_frame(hc.CMD_GET_REPAIR,
                                                      hx(REAL_REPAIR_PAYLOAD)))
        self.assertEqual(hc.unpack_u16(frame.payload, 8),
                         [100, 120, 80, 120, 80, 120, 260, 120])

    def test_name(self):
        payload = REAL_NAME.encode("ascii") + b"\x00" * (hc.NAME_LEN - len(REAL_NAME))
        self.assertEqual(hc.decode_name(payload), "HP20")

    def test_sim_defaults_match_real_device(self):
        from sim_device import Hp20SimCore
        core = Hp20SimCore()
        self.assertEqual(core.presets, [66, 200, 300, 0])
        self.assertEqual(core.SUPPLY_POWER_W, 140)
        self.assertEqual(core.INPUT_VOLTAGE_RAW, 278)
        self.assertEqual(core.W2_RAW, 0x29BF)
        self.assertEqual(core.reflow, [150, 120, 180, 90, 260, 60, 80, 300])
        self.assertEqual(core.repair, [100, 120, 80, 120, 80, 120, 260, 120])
        self.assertEqual(core.name, "HP20")


class TestIdentificationFlowWithRealFrame(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = FakeTransport()
        # 用真机原始帧应答识别请求（设备头 FF FF）
        self.transport.reply_queue[hc.CMD_IDENTIFY] = [hx(REAL_IDENTIFY_RX)]
        self.device = Hp20Device(self.transport, timeout=0.3, tick=0.005,
                                 write_gap=0.0, poll_interval=30.0)
        self.logs: list[str] = []
        self.device.on_system = self.logs.append
        await self.transport.connect()

    async def asyncTearDown(self):
        await self.device.close()

    async def test_identify_accepts_ff_ff_header(self):
        await self.device.open(handshake_delay=0)
        info = self.device.info
        self.assertIsNotNone(info)
        self.assertTrue(info.is_hp20)
        self.assertEqual(info.device_type, "HP20")
        self.assertEqual(info.dev_header, hc.DEV_HEADER)
        self.assertTrue(any("设备头 0xFFFF" in line for line in self.logs))
        self.assertTrue(self.device.identified)

    async def test_poll_not_blocked_by_ff_header(self):
        """配对只比 cmd：应答设备头与请求不同也必须能配上。"""
        await self.device.open(handshake_delay=0)
        await asyncio.sleep(0.1)
        self.assertTrue(await self.device.poll_once())
        self.assertIsNotNone(self.device.status)
        self.assertTrue(self.device.hp20)


if __name__ == "__main__":
    unittest.main()