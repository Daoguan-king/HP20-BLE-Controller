"""第 3 阶段验收：协议布局的「编码」与「解码」必须共用同一份实现（往返测试）。

以前 0x03 的 [index]+i16le 布局有 3 份实现（hp20_commands 里没人调用的 frame_*、
hp20_device 手拼、sim_device 手解 struct），改一处不改另一处就会出现「发对帧但解析错」，
而且模拟器照旧骗过测试。现在编码只走 hc.frame_*、解码只走 hc.parse_set_thermostat /
hc.unpack_* / hc.parse_realtime，下面用往返测试把它们钉在一起。
"""
import unittest

import framing
import hp20_commands as hc
from sim_device import Hp20SimCore


class TestThermostatLayoutRoundTrip(unittest.TestCase):
    """0x03 = [index] + i16le：frame_set_thermostat 编出来的必须能被 parse 解回。"""

    def test_round_trip_all_indices_and_temps(self):
        for index in range(5):                      # 0..3 = T1..T4，4 = T5 目标
            for temp in (-20, -1, 0, 60, 180, 250, 300):
                payload = framing.parse_frame(hc.frame_set_thermostat(index, temp)).payload
                self.assertEqual(hc.parse_set_thermostat(payload), (index, temp),
                                 f"index={index} temp={temp}")

    def test_short_payload_returns_none(self):
        self.assertIsNone(hc.parse_set_thermostat(b""))
        self.assertIsNone(hc.parse_set_thermostat(b"\x04"))
        self.assertIsNone(hc.parse_set_thermostat(b"\x04\x00"))

    def test_sim_decodes_thermostat_with_same_layout(self):
        """模拟设备必须用同一份解码：改成 0x03 index=4 后它的 t5 要跟着变。"""
        core = Hp20SimCore()
        core.handle(framing.parse_frame(hc.frame_set_thermostat(hc.TARGET_INDEX, 120)))
        self.assertEqual(core.t5, 120)
        core.handle(framing.parse_frame(hc.frame_set_thermostat(1, 200)))
        self.assertEqual(core.presets[1], 200)


class TestSimStatusPayloadRoundTrip(unittest.TestCase):
    """sim 用 hc.build_realtime_payload 造的帧，必须能被 hc.parse_realtime 原样解回。"""

    def test_status_payload_parses_back(self):
        core = Hp20SimCore()
        core.temp = 61.4
        core.heating = True
        core.goal = 60.0
        core.channel = 1
        core.mode = hc.MODE_HEATING
        core.output_power_w = 45
        core.timer_left = 1229.0
        payload = core.status_payload()
        self.assertEqual(len(payload), 11, "状态帧 payload 必须 11 字节")
        st = hc.parse_realtime(payload)
        self.assertIsNotNone(st)
        self.assertEqual(st.current_temp, 61)
        self.assertEqual(st.target_temp, 60)
        self.assertEqual(st.channel_or_stage, 1)
        self.assertEqual(st.mode, hc.MODE_HEATING)
        self.assertEqual(st.mode_name, "加热")
        self.assertEqual(st.output_power_w, 45)
        self.assertEqual(st.supply_power_w, core.SUPPLY_POWER_W)
        self.assertEqual(st.input_voltage_raw, core.INPUT_VOLTAGE_RAW)
        self.assertEqual(st.w2, 1229)
        self.assertEqual(st.timer_text, "19:13")

    def test_idle_status_uses_timer_sentinel(self):
        """计时未启用时用协议哨兵（唯一真源 Proto.TIMER_OFF_SENTINEL）。"""
        core = Hp20SimCore()
        core.heating = False
        core.timer_left = None
        st = hc.parse_realtime(core.status_payload())
        self.assertEqual(st.w2, hc.TIMER_OFF_SENTINEL)
        self.assertEqual(st.timer_text, "OFF")
        self.assertEqual(st.mode, hc.MODE_IDLE)

    def test_sim_8x_u16_payloads_use_protocol_packers(self):
        """0x06/0x0B 的 8×u16 复用 hc.pack_u16/unpack_u16：写进去什么要读得回什么。"""
        core = Hp20SimCore()
        curve = [150, 120, 180, 90, 260, 60, 80, 300]
        core.handle(framing.parse_frame(hc.frame_set_reflow(curve)))
        self.assertEqual(core.reflow, curve)
        self.assertEqual(hc.unpack_u16(hc.pack_u16(curve), 8), curve)


class TestProtoIsSingleSource(unittest.TestCase):
    """常量只留一处：其它模块的取值必须全部等于 Proto 里的定义。"""

    def test_framing_derives_from_proto(self):
        import framing
        from proto_const import Proto
        self.assertEqual(framing.REQ_HEADER, Proto.REQ_HEADER)
        self.assertEqual(framing.RESP_HEADER, Proto.RESP_HEADER)
        self.assertEqual(framing.MIN_FRAME_LEN, Proto.MIN_FRAME_LEN)
        self.assertEqual(framing.MAX_PAYLOAD, Proto.MAX_PAYLOAD)

    def test_hp20_commands_derives_from_proto(self):
        from proto_const import Proto
        self.assertEqual(hc.DEV_ID, Proto.DEV_ID)
        self.assertEqual(hc.FAMILY, Proto.FAMILY)
        self.assertEqual(hc.DEV_HEADER, Proto.DEV_HEADER)
        self.assertEqual(hc.IDENT_PAYLOAD, Proto.IDENT_PAYLOAD)
        self.assertEqual(hc.ACK_MIN_PAYLOAD, Proto.ACK_MIN_PAYLOAD)
        self.assertEqual(hc.PROTOCOL_TIMEOUT_S, Proto.PROTOCOL_TIMEOUT_S)
        self.assertEqual(hc.RESPONSE_TIMEOUT_S, Proto.RESPONSE_TIMEOUT_S)
        self.assertEqual(hc.POLL_INTERVAL_S, Proto.POLL_INTERVAL_S)
        self.assertEqual(hc.TIMEOUT_TICK_S, Proto.TIMEOUT_TICK_S)
        self.assertEqual(hc.WRITE_GAP_S, Proto.WRITE_GAP_S)
        self.assertEqual(hc.POST_CONNECT_DELAY_S, Proto.POST_CONNECT_DELAY_S)
        self.assertEqual(hc.RECONNECT_BACKOFF_S, Proto.RECONNECT_BACKOFF_S)
        self.assertEqual(hc.RECONNECT_MAX_ATTEMPTS, Proto.RECONNECT_MAX_ATTEMPTS)
        self.assertEqual(hc.NAME_LEN, Proto.NAME_LEN)
        self.assertEqual(hc.TIMER_OFF_SENTINEL, Proto.TIMER_OFF_SENTINEL)

    def test_sim_sentinel_is_not_a_second_copy(self):
        from sim_device import Hp20SimCore
        self.assertEqual(Hp20SimCore.W2_RAW, hc.TIMER_OFF_SENTINEL)


if __name__ == "__main__":
    unittest.main()
