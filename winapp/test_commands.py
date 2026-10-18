"""命令表与 protocol/hp20_commands.json 逐字节一致性（防漂移）+ 实测语义的状态解码。"""
import json
import pathlib
import re
import unittest

import framing
import hp20_commands as hc
from proto_const import Proto

JSON_PATH = pathlib.Path(__file__).resolve().parent.parent / "protocol" / "hp20_commands.json"

BUILDERS = {
    "identify": lambda args: hc.frame_identify(),
    "get_thermostat": lambda args: hc.frame_get_thermostat(),
    "set_thermostat": lambda args: hc.frame_set_thermostat(args["index"], args["temp"]),
    "set_preheat": lambda args: hc.frame_set_preheat(args["channel"]),
    "get_reflow": lambda args: hc.frame_get_reflow(),
    "set_reflow": lambda args: hc.frame_set_reflow(args["curve"]),
    "get_repair": lambda args: hc.frame_get_repair(),
    "set_repair": lambda args: hc.frame_set_repair(args["params"]),
    "set_ble_name": lambda args: hc.frame_set_name(args["name"]),
    "get_ble_name": lambda args: hc.frame_get_name(),
    "get_realtime": lambda args: hc.frame_realtime(),
}


class TestProtocolJsonDrift(unittest.TestCase):
    """代码 vs A1 协议事实的闸门：JSON 一变，这里先红。"""

    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        cls.cmds = {c["name"]: c for c in cls.doc["commands"]}

    def test_json_exists_and_complete(self):
        self.assertEqual(len(self.doc["commands"]), 11)
        self.assertEqual(set(self.cmds), set(BUILDERS))

    def test_device_header(self):
        want = int(self.doc["device_header"]["value"], 16)
        self.assertEqual(want, hc.DEV_HEADER)
        self.assertEqual(self.doc["device_header"]["bytes"].upper(),
                         f"{hc.DEV_ID:02X} {hc.FAMILY:02X}")

    def test_command_bytes(self):
        consts = {
            "identify": hc.CMD_IDENTIFY, "set_thermostat": hc.CMD_SET_THERMOSTAT,
            "get_thermostat": hc.CMD_GET_THERMOSTAT, "set_preheat": hc.CMD_SET_PREHEAT,
            "set_reflow": hc.CMD_SET_REFLOW, "get_reflow": hc.CMD_GET_REFLOW,
            "set_ble_name": hc.CMD_SET_NAME, "get_ble_name": hc.CMD_GET_NAME,
            "get_realtime": hc.CMD_REALTIME, "set_repair": hc.CMD_SET_REPAIR,
            "get_repair": hc.CMD_GET_REPAIR,
        }
        for name, entry in self.cmds.items():
            self.assertEqual(consts[name], int(entry["cmd"], 16), f"{name} cmd 漂移")

    def test_all_example_frames_byte_exact(self):
        checked = 0
        for name, entry in self.cmds.items():
            for ex in entry.get("examples", []):
                want = ex["frame_hex"].replace(" ", "").upper()
                got = framing.to_hex(BUILDERS[name](ex.get("args") or {})).replace(" ", "")
                self.assertEqual(got, want, f"{name} / {ex.get('desc')} 帧不一致")
                checked += 1
        self.assertEqual(checked, 16, "示例帧数量变了，确认是否需要更新断言")

    def test_protocol_timing_constants(self):
        t = self.doc["timing"]
        self.assertAlmostEqual(hc.POLL_INTERVAL_S * 1000, t["poll_interval_ms"])
        # 该常量已并入 proto_const.Proto，文档值仅作参考（运行时默认 2.0 s 见下一处断言）。
        # 这里仍按协议文档 JSON 校验那 1000 ms，并钉住「hc 只是 Proto 的派生」。
        self.assertAlmostEqual(Proto.PROTOCOL_TIMEOUT_S * 1000, t["response_timeout_ms"])
        self.assertEqual(hc.PROTOCOL_TIMEOUT_S, Proto.PROTOCOL_TIMEOUT_S)
        self.assertAlmostEqual(hc.TIMEOUT_TICK_S * 1000, t["timeout_tick_ms"])
        self.assertAlmostEqual(hc.WRITE_GAP_S * 1000, t["write_interval_ms"])
        self.assertAlmostEqual(hc.POST_CONNECT_DELAY_S * 1000, t["post_connect_delay_ms"])
        self.assertTrue(t["single_command_in_flight"])

    def test_runtime_timeout_raised_to_2000ms(self):
        """真机部分应答延迟 0.7~1.5s，1000ms 会误判超时 -> 运行时默认 2000ms。"""
        self.assertEqual(hc.RESPONSE_TIMEOUT_S, 2.0)

    def test_reconnect_backoff_sequence(self):
        self.assertEqual(tuple(hc.RECONNECT_BACKOFF_S), (1.0, 2.0, 4.0, 8.0))
        self.assertEqual(hc.RECONNECT_MAX_ATTEMPTS, 4)

    def test_realtime_length_min(self):
        self.assertEqual(self.cmds["get_realtime"]["response"]["length_min"], 11)

    def test_reflow_and_repair_have_8_fields(self):
        self.assertEqual(len(self.cmds["set_reflow"]["payload_format"]), 8)
        self.assertEqual(len(self.cmds["set_repair"]["payload_format"]), 8)
        self.assertEqual(len(hc.REFLOW_LABELS), 8)
        self.assertEqual(len(hc.REPAIR_LABELS), 8)

    def test_no_stop_command_exists(self):
        """U1: 协议里没有停止/急停命令 —— 别自己发明。"""
        self.assertFalse([n for n in self.cmds if "stop" in n or "abort" in n])


class TestRealtimeDecode(unittest.TestCase):
    def _payload(self, **kw):
        base = dict(current_temp=88, supply_power_w=140, output_power_w=11, target_temp=250,
                    channel_or_stage=0, mode=2, w2=0x04B2,
                    celsius=False, input_voltage_raw=278,
                    output_state_raw=0x02)
        base.update(kw)
        return hc.build_realtime_payload(**base)

    def test_temperature_comes_from_first_two_bytes(self):
        s = hc.parse_realtime(self._payload(current_temp=45))
        self.assertEqual(s.current_temp, 45)
        # payload 布局：[0:2] 温度 [2] 额定 [3:5] 诊断 [5] 未知 [6] 功率 [7:9] W1 [9:11] W2
        self.assertEqual(self._payload(current_temp=45)[:2], (45).to_bytes(2, "little"))

    def test_voltage_field_is_not_temperature(self):
        """[3:5] 是输入电压 ×0.1V，不是温度（G 阶段实测：278 -> 27.8V，旧电源 200 -> 20.0V）。"""
        s = hc.parse_realtime(self._payload(current_temp=66, input_voltage_raw=278))
        self.assertEqual(s.current_temp, 66)
        self.assertEqual(s.input_voltage_raw, 278)
        self.assertAlmostEqual(s.input_voltage_v, 27.8, places=1)
        self.assertEqual(s.voltage_text, "27.8V")
        old = hc.parse_realtime(self._payload(current_temp=66, input_voltage_raw=200))
        self.assertEqual(old.voltage_text, "20.0V")
        self.assertEqual(old.current_temp, 66, "改电压不能让温度变化")

    def test_negative_temperature(self):
        self.assertEqual(hc.parse_realtime(self._payload(current_temp=-20)).current_temp, -20)
        self.assertEqual(hc.parse_realtime(self._payload(current_temp=-1)).current_temp, -1)

    def test_mode_table_matches_manual(self):
        """1=回流焊、2=加热（官方 APP 的 stateStr 把这两条抄反了，这里按手册）。"""
        expected = {0: "待机", 1: "回流焊", 2: "加热", 3: "设置", 4: "维修", 8: "主页"}
        for mode, name in expected.items():
            self.assertEqual(hc.parse_realtime(self._payload(mode=mode)).mode_name, name,
                             f"mode={mode} 名称不符")
        self.assertEqual(hc.parse_realtime(self._payload(mode=5)).mode_name, "未知(5)")

    def test_slot_text_follows_mode(self):
        """slot_text 随模式解释；mode=1 必须显示 PH/SK/RF/CL（防抄 APP 的错表）。"""
        reflow = ("PH 预热", "SK 恒温", "RF 回流", "CL 冷却")
        for stage, name in enumerate(reflow):
            st = hc.parse_realtime(self._payload(mode=1, channel_or_stage=stage))
            self.assertEqual(st.slot_text, name)
            self.assertEqual(st.mode_name, "回流焊")
        # 防回归：mode=1 绝不能显示 T1..T4
        self.assertNotIn("T1", hc.parse_realtime(
            self._payload(mode=1, channel_or_stage=0)).slot_text)
        for ch, name in enumerate(("T1", "T2", "T3")):
            self.assertEqual(hc.parse_realtime(
                self._payload(mode=2, channel_or_stage=ch)).slot_text, name)
        # T4 是停止通道，UI 上要标出来（slot_name 仍是纯 T4）
        t4 = hc.parse_realtime(self._payload(mode=2, channel_or_stage=3))
        self.assertEqual(t4.slot_text, "T4（停止通道）")
        self.assertEqual(t4.slot_name, "T4")
        self.assertEqual(hc.parse_realtime(
            self._payload(mode=4, channel_or_stage=0)).slot_text, "除胶(Debond)")
        self.assertEqual(hc.parse_realtime(
            self._payload(mode=4, channel_or_stage=2)).slot_text, "贴合(LAM)")
        self.assertEqual(hc.parse_realtime(
            self._payload(mode=4, channel_or_stage=3)).slot_text, "植球(Reballing)")

    def test_repair_group_names_confirmed(self):
        """U4 结案：用户在设备面板确认 4 组 = 除胶/分层/贴合/植球。"""
        self.assertEqual(hc.REPAIR_GROUPS,
                         ("除胶(Debond)", "分层(Delam)", "贴合(LAM)", "植球(Reballing)"))
        self.assertEqual(hc.REPAIR_LABELS[4], ("贴合(LAM)温度", "℃"))
        self.assertEqual(hc.REPAIR_LABELS[6], ("植球(Reballing)温度", "℃"))
        self.assertEqual(hc.MODE_SLOT_NAMES[hc.MODE_REPAIR], hc.REPAIR_GROUPS)
        # mode=0/3/8：显示字段原值
        for mode in (0, 3, 8):
            self.assertEqual(hc.parse_realtime(
                self._payload(mode=mode, channel_or_stage=2)).slot_text, "字段值 2")

    def test_channel_is_slot_t1_t4(self):
        for ch, name in enumerate(("T1", "T2", "T3", "T4")):
            self.assertEqual(hc.parse_realtime(
                self._payload(channel_or_stage=ch)).slot_name, name)

    def test_target_temp_masked_to_10_bits(self):
        s = hc.parse_realtime(self._payload(target_temp=1023, channel_or_stage=2, mode=2))
        self.assertEqual((s.target_temp, s.channel_or_stage, s.mode), (1023, 2, 2))

    def test_power_fields_are_watts(self):
        """G 阶段确证：[6] 输出功率 W，[2] 供电功率 W，% = 输出/供电（与手机 9% 一致）。"""
        s = hc.parse_realtime(self._payload(output_power_w=11, supply_power_w=140))
        self.assertEqual((s.output_power_w, s.supply_power_w), (11, 140))
        self.assertAlmostEqual(s.power_percent, 7.857, places=2)
        self.assertEqual(s.power_percent_text, "7.9%")
        self.assertEqual(s.power_text, "11W（7.9%）")
        self.assertEqual(s.supply_text, "140W")
        self.assertNotIn("单位待定", s.power_text)
        # 换电源前的 6/65 = 9%（手机顶栏显示的就是这个）
        old = hc.parse_realtime(self._payload(output_power_w=6, supply_power_w=65))
        self.assertEqual(old.power_percent_text, "9.2%")
        self.assertEqual(hc.parse_realtime(self._payload(supply_power_w=0)).power_percent, 0.0)

    def test_stop_by_channel_frame_bytes(self):
        """默认停止路径 = 0x05 [3] 选 T4（T4 是隐藏的第 4 挡、预设固定 0℃）。

        帧字节 AF 09 02 05 01 03 3D —— 与 A1 JSON 的「启动 T4」示例逐字节一致，
        LRC 手算 = (256 - (0xAF+0x09+0x02+0x05+0x01+0x03 = 195)) & 0xFF = 0x3D。
        """
        self.assertEqual(framing.to_hex(hc.frame_stop_by_channel()),
                         "AF 09 02 05 01 03 3D")
        self.assertEqual(hc.STOP_CHANNEL, 3)
        frame = framing.parse_frame(hc.frame_stop_by_channel())
        self.assertEqual(frame.cmd, hc.CMD_SET_PREHEAT)
        self.assertEqual(frame.payload, bytes((hc.STOP_CHANNEL,)))

    def test_stop_by_channel_matches_json_example(self):
        """与 A1 协议 JSON 的 set_preheat channel=3 示例一致（反证 3F 是笔误）。"""
        self.assertEqual(hc.frame_stop_by_channel(), hc.frame_set_preheat(3))
        self.assertEqual(framing.compute_lrc(bytes.fromhex("AF0902050103")), 0x3D)

    def test_stop_by_target_is_the_backup_path(self):
        self.assertEqual(framing.to_hex(hc.frame_stop_by_target()),
                         "AF 09 02 03 03 04 00 00 3C")
        self.assertEqual(hc.frame_stop_by_target(),
                         hc.frame_set_thermostat(hc.TARGET_INDEX, 0))
        self.assertEqual(hc.STOP_TARGET_TEMP, 0)

    def test_two_stop_paths_are_distinct(self):
        self.assertNotEqual(hc.frame_stop_by_channel(), hc.frame_stop_by_target())

    def test_no_dedicated_stop_command(self):
        """没有独立 stop 命令：停止靠 0x05 [3](T4) 或 0x03 index=4 的取值实现。"""
        self.assertIsNone(hc.COMMAND_NAMES.get(0x0D))
        self.assertNotIn("stop", [n for n in hc.COMMAND_NAMES.values()])

    def test_timer_is_64_base_packed(self):
        """N 阶段定论：W2 是 **64 进制打包**的 MM:SS（每分钟 64 个单位），不是秒数。

        决定性锚点：设置 25 min -> W2=1600=25×64 -> 25:00；20 min -> 1280=20×64 -> 20:00。
        """
        cases = {1600: "25:00", 1536: "24:00", 1280: "20:00", 1229: "19:13",
                 1832: "28:40", 0x29BF: "OFF", 0x03FF: "OFF", 0: "00:00"}
        for word, text in cases.items():
            st = hc.parse_realtime(self._payload(w2=word))
            self.assertEqual(st.timer_text, text, f"W2={word}")

    def test_timer_label_follows_mode(self):
        """同一个 W2：回流焊/加热/维修 -> 加热时间；待机/设置/主页 -> 休眠倒计时。"""
        for mode in (1, 2, 4):
            self.assertEqual(hc.parse_realtime(self._payload(mode=mode)).timer_label,
                             "加热时间", f"mode={mode}")
        for mode in (0, 3, 8):
            self.assertEqual(hc.parse_realtime(self._payload(mode=mode)).timer_label,
                             "休眠倒计时", f"mode={mode}")
        self.assertEqual(hc.parse_realtime(self._payload(mode=5)).timer_label, "计时")

    def test_timer_value_independent_of_label(self):
        """标签变、换算不变（用户反馈 ±1 分多，本轮不动公式）。"""
        for mode in (0, 1, 2, 3, 4, 8):
            self.assertEqual(hc.parse_realtime(
                self._payload(mode=mode, w2=1229)).timer_text, "19:13")

    def test_power_percent_from_supply(self):
        """44/45 ≈ 97%（界面百分条用同一算法）。"""
        st = hc.parse_realtime(self._payload(output_power_w=44, supply_power_w=45))
        self.assertAlmostEqual(st.power_percent, 97.78, places=2)
        self.assertEqual(st.power_percent_text, "97.8%")

    def test_ss_over_60_is_the_off_sentinel(self):
        """SS >= 60 即 OFF —— 0x29BF 的 SS=63 正由此判定（官方 APP 同样做法）。"""
        self.assertEqual(hc.format_timer(0x29BF), "OFF")     # SS=63
        self.assertEqual(hc.format_timer(0x03FF), "OFF")     # MM=15, SS=63
        self.assertEqual(hc.format_timer(0x03FE), "OFF")       # SS=62 也算未开启
        self.assertEqual(hc.format_timer((15 << 6) | 60), "OFF")  # SS=60 边界
        self.assertEqual(hc.format_timer((15 << 6) | 59), "15:59")  # 可显示的最大秒
        # 该常量已并入 proto_const.Proto，文档值仅作参考（0x29BF 来自真机实测）
        self.assertEqual(Proto.TIMER_OFF_SENTINEL, 0x29BF)
        self.assertEqual(hc.TIMER_OFF_SENTINEL, Proto.TIMER_OFF_SENTINEL)
        self.assertFalse(hasattr(hc, "TIMER_MAX_SECONDS"), "不再用 7200 当边界")
        self.assertFalse(hasattr(hc, "TIMER_HINT"), "不再有「多约 1 分钟」提示")

    def test_timer_packed_property_is_raw_w2(self):
        """字段名是 timer_packed：它是打包值，不是秒。"""
        st = hc.parse_realtime(self._payload(w2=1229))
        self.assertEqual(st.timer_packed, 1229)
        self.assertEqual(st.w2, 1229)
        self.assertFalse(hasattr(st, "timer_seconds"), "不应再叫 timer_seconds")
        self.assertEqual(hc.format_timer(1229), "19:13")

    def test_official_app_64_base_split_is_the_correct_one(self):
        """反转（N 阶段）：官方 APP 的 64 进制位域是对的，1229 **必须**显示 19:13。

        曾经的「按 60 进制当秒数」实现会给出 20:29，那是错的（每 64 分钟差 1 分钟）。
        """
        st = hc.parse_realtime(self._payload(w2=1229))
        self.assertEqual(st.timer_text, "19:13")
        self.assertIn("19:13", st.summary())
        self.assertNotIn("20:29", st.summary())
        # 位段字段与显示口径一致
        self.assertEqual((st.w2_low6, st.w2_next7), (1229 & 0x3F, (1229 >> 6) & 0x7F))
        # 两个精确锚点：设置值 = 分钟 × 64
        self.assertEqual(25 * 64, 1600)
        self.assertEqual(20 * 64, 1280)

    def test_w2_raw_kept_for_diagnostics(self):
        st = hc.parse_realtime(self._payload(w2=0x29BF))
        self.assertEqual(st.w2, 0x29BF)
        self.assertEqual(st.w2_text, "W2=0x29BF")
        self.assertEqual(st.w2_low6, 0x3F)          # 63 -> 触发 OFF 哨兵
        self.assertEqual(st.w2_next7, 0x26)         # 38
        self.assertIn("加热时间 OFF", st.summary())     # mode=2 -> 标签是「加热时间」
        self.assertIn("W2=0x29BF", st.summary())


    def test_unit_always_celsius_bit_is_diagnostic(self):
        # w2=None 时按位拼装，才能单独验证 bit14
        s = hc.parse_realtime(self._payload(w2=None, celsius=False))
        self.assertFalse(s.celsius_bit)
        self.assertEqual(s.unit, "℃")
        s2 = hc.parse_realtime(self._payload(w2=None, celsius=True))
        self.assertTrue(s2.celsius_bit)
        self.assertEqual(s2.unit, "℃")

    def test_output_state_kept_for_diagnostics(self):
        self.assertEqual(
            hc.parse_realtime(self._payload(output_state_raw=0x02)).output_state_raw, 0x02)

    def test_short_payload_returns_none(self):
        self.assertIsNone(hc.parse_realtime(b"\x00" * 10))
        self.assertIsNotNone(hc.parse_realtime(b"\x00" * 11))

    def test_summary_has_measured_fields(self):
        text = hc.parse_realtime(self._payload()).summary()
        self.assertIn("88", text)
        self.assertIn("250", text)
        self.assertIn("加热", text)
        self.assertIn("W2=0x", text)

class TestOutputState(unittest.TestCase):
    def payload(self, **kw):
        base = dict(current_temp=25, supply_power_w=140, output_power_w=0, target_temp=0,
                    channel_or_stage=0, mode=0, w2=None,     # None：按位段拼 w2
                    celsius=False, input_voltage_raw=278,
                    output_state_raw=0x00)
        base.update(kw)
        return hc.build_realtime_payload(**base)

    def test_output_state_byte(self):
        """输出状态文案按「不加热」判据归一化（V 阶段）。

        实测设备在待机/设置界面也会把 [5] 报成 0x02，所以主界面文案不能只看 [5]：
        不加热（mode 0/3/8 或 [5]==0x00）一律显示「待机」；加热中按 mode 细分。
        原始 [5] 仍保留在 output_state_raw（诊断页看原值）。
        """
        # mode=0（待机）：即使 [5]=0x02 也显示待机
        self.assertEqual(hc.parse_realtime(self.payload(output_state_raw=0x02)).output_state_text,
                         "待机")
        self.assertEqual(hc.parse_realtime(self.payload(output_state_raw=0x00)).output_state_text,
                         "待机")
        # 加热中（mode=2）才显示加热；回流/维修按 mode 区分
        self.assertEqual(hc.parse_realtime(self.payload(
            mode=hc.MODE_HEATING, output_state_raw=0x02)).output_state_text,
            "加热模式运行中")
        self.assertEqual(hc.parse_realtime(self.payload(
            mode=hc.MODE_REFLOW, output_state_raw=0x02)).output_state_text,
            "回流焊运行中")
        self.assertEqual(hc.parse_realtime(self.payload(
            mode=hc.MODE_REPAIR, output_state_raw=0x02)).output_state_text,
            "维修模式运行中")
        # [5] 原值照旧可查
        self.assertEqual(hc.parse_realtime(self.payload(output_state_raw=0x07)).output_state_raw,
                         0x07)

    def test_output_state_raw_kept(self):
        self.assertEqual(hc.parse_realtime(self.payload(output_state_raw=0x02)).output_state_raw,
                         0x02)

    def test_no_fan_field_is_derived_from_w2(self):
        """K 阶段结论：状态帧里没有风扇信息 —— 不允许再从 W2 派生任何风扇字段。

        证伪过程：唯一 bit13=1 的样本全是 W2=0x29BF（加热超时未启用哨兵）本身第 13 位；
        保温/加热/恢复三态（0x04C5 / 0x04C3 / 0x04C1）的 bit13 全为 0，而风扇确实在转。
        """
        st = hc.parse_realtime(self.payload())
        self.assertFalse(hasattr(st, "fan_on"), "不应再有 fan_on 派生字段")
        self.assertFalse(hasattr(st, "fan_text"), "不应再有 fan_text 派生字段")
        for word in (0x29BF, 0x04C3, 0x04C5, 0x2000):
            parsed = hc.parse_realtime(self.payload(w2=word))
            self.assertFalse(hasattr(parsed, "fan_on"))
            self.assertNotIn("风扇", parsed.summary())
            self.assertNotIn("风扇", repr(parsed))
        # bit13 置位也不产生任何风扇语义
        with_bit13 = hc.parse_realtime(self.payload(w2=0x2000))
        without = hc.parse_realtime(self.payload(w2=0x0000))
        self.assertEqual(set(vars(with_bit13)), set(vars(without)))


class TestNameAndGuards(unittest.TestCase):
    def test_decode_name_strips_nulls(self):
        self.assertEqual(hc.decode_name(b"HP20\x00\x00\x00"), "HP20")
        self.assertEqual(hc.decode_name(b"\x00" * 20), "")

    def test_set_name_pads_to_20(self):
        frame = framing.parse_frame(hc.frame_set_name("HP20"))
        self.assertEqual(frame.payload, b"HP20" + b"\x00" * 16)
        self.assertEqual(frame.raw[4], 20)

    def test_set_name_truncates(self):
        frame = framing.parse_frame(hc.frame_set_name("A" * 30))
        self.assertEqual(len(frame.payload), hc.NAME_LEN)

    def test_set_thermostat_index_bounds(self):
        with self.assertRaises(ValueError):
            hc.frame_set_thermostat(5, 100)
        with self.assertRaises(ValueError):
            hc.frame_set_thermostat(-1, 100)

    def test_set_preheat_channel_bounds(self):
        with self.assertRaises(ValueError):
            hc.frame_set_preheat(4)

    def test_set_reflow_requires_8(self):
        with self.assertRaises(ValueError):
            hc.frame_set_reflow([1, 2, 3])


if __name__ == "__main__":
    unittest.main()