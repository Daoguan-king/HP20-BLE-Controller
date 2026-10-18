"""R 阶段：诊断面板（字节带 + 逐字段解析表）与帧解析 schema。

覆盖：各命令的字段布局、未知命令不硬解、LRC 错误标注、
以及面板的节流（≤2 次/秒）、滚动保持、不可见不刷、自动刷新开关、
以及「原始收发 / W2 = 0x」两个已删块的反向断言。
"""
import time
import unittest

import framediag
import hp20_commands as hc


def text_of(raw: bytes) -> str:
    return "\n".join(framediag.render(raw))


def names(raw: bytes) -> list[str]:
    fields, _note = framediag.describe(raw)
    return [f.name for f in fields]


class TestSchemaIdentify(unittest.TestCase):
    def test_hp20_payload(self):
        raw = hc.response_frame(hc.CMD_IDENTIFY, hc.IDENT_PAYLOAD)
        t = text_of(raw)
        self.assertIn("型号 payload", t)
        self.assertIn("HP20 (0x0209)", t)
        self.assertIn("RX←设备", t)

    def test_other_device_is_labelled(self):
        raw = hc.response_frame(hc.CMD_IDENTIFY, bytes((0x07, 0x03)))
        self.assertIn("非 HP20 (0x0307)", text_of(raw))

    def test_identify_request_has_no_payload_fields(self):
        fields, note = framediag.describe(hc.frame_identify())
        self.assertEqual(note, "")
        self.assertEqual([f.name for f in fields],
                         ["SOF 帧头", "设备头 dev_id|family<<8", "命令 cmd",
                          "payload 长度", "LRC 校验"])


class TestSchemaWriteCommands(unittest.TestCase):
    def test_set_thermostat_tx(self):
        t = text_of(hc.frame_set_thermostat(1, 45))
        self.assertIn("index", t)
        self.assertIn("T2", t)
        self.assertIn("温度 temp", t)
        self.assertIn("45℃", t)
        self.assertIn("0x03 设置预设温度", t)

    def test_set_thermostat_target_index(self):
        t = text_of(hc.frame_set_thermostat(hc.TARGET_INDEX, 250))
        self.assertIn("目标温度 T5", t)
        self.assertIn("250℃", t)

    def test_set_preheat_channel(self):
        self.assertIn("T2", text_of(hc.frame_set_preheat(1)))
        self.assertIn("T4（停止通道，固定 0℃）", text_of(hc.frame_set_preheat(3)))

    def test_set_reflow_eight_fields(self):
        values = [150, 120, 180, 90, 260, 60, 80, 300]
        t = text_of(hc.frame_set_reflow(values))
        for name, unit, value in zip(hc.REFLOW_FIELDS, ["℃", "s"] * 4, values):
            self.assertIn(f"{name}({unit})", t)
            self.assertIn(f"{value}{unit}", t)
        self.assertIn("u16le", t)

    def test_set_repair_eight_fields(self):
        values = [100, 120, 80, 120, 80, 120, 260, 120]
        t = text_of(hc.frame_set_repair(values))
        for name in hc.REPAIR_FIELDS:
            self.assertIn(name, t)

    def test_set_name_ascii(self):
        t = text_of(hc.frame_set_name("HP20-A"))
        self.assertIn("蓝牙名", t)
        self.assertIn("'HP20-A'", t)

    def test_write_ack_is_not_parsed_as_payload(self):
        ack = hc.response_frame(hc.CMD_SET_PREHEAT, b"\x00")
        fields, note = framediag.describe(ack)
        self.assertEqual(note, "")
        self.assertEqual([f.name for f in fields][-2:], ["应答 ack", "LRC 校验"])
        self.assertIn("成功", text_of(ack))


class TestSchemaReadCommands(unittest.TestCase):
    def test_presets_four_values(self):
        raw = hc.response_frame(hc.CMD_GET_THERMOSTAT, hc.pack_u16([66, 200, 300, 0]))
        t = text_of(raw)
        for i, value in enumerate([66, 200, 300, 0]):
            self.assertIn(f"预设 T{i + 1}", t)
            self.assertIn(f"{value}℃", t)
        self.assertIn("停止通道", t)

    def test_read_reflow(self):
        raw = hc.response_frame(hc.CMD_GET_REFLOW, hc.pack_u16([150, 120, 180, 90,
                                                              260, 60, 80, 300]))
        t = text_of(raw)
        self.assertIn("预热温度(℃)", t)
        self.assertIn("冷却时间(s)", t)
        self.assertIn("300s", t)

    def test_read_repair(self):
        raw = hc.response_frame(hc.CMD_GET_REPAIR, hc.pack_u16([100, 120, 80, 120,
                                                              80, 120, 260, 120]))
        self.assertIn("除胶温度(℃)", text_of(raw))

    def test_read_name(self):
        raw = hc.response_frame(hc.CMD_GET_NAME, b"HP20\x00")
        t = text_of(raw)
        self.assertIn("蓝牙名", t)
        self.assertIn("'HP20'", t)

    def test_realtime_status_all_fields(self):
        payload = hc.build_realtime_payload(
            current_temp=40, supply_power_w=140, output_power_w=6, target_temp=40,
            channel_or_stage=0, mode=2, w2=0x29BF, input_voltage_raw=279,
            output_state_raw=0x02)
        t = text_of(hc.response_frame(hc.CMD_REALTIME, payload))
        for word in ("当前温度", "40℃", "供电功率", "140W", "输入电压", "27.9V",
                     "输出状态", "加热模式运行中", "当前功率", "6W",
                     "W1 目标/挡位/模式", "目标 40℃", "挡位 T1", "模式 加热(2)",
                     "W2 计时（64 进制 MM:SS）", "OFF"):
            self.assertIn(word, t, word)

    def test_w2_decoded_as_mmss(self):
        payload = hc.build_realtime_payload(
            current_temp=25, supply_power_w=45, output_power_w=0, target_temp=0,
            channel_or_stage=3, mode=0, w2=1280, input_voltage_raw=200,
            output_state_raw=0x00)
        self.assertIn("20:00", text_of(hc.response_frame(hc.CMD_REALTIME, payload)))


class TestUnknownAndBrokenFrames(unittest.TestCase):
    def test_unknown_command_is_not_parsed(self):
        raw = hc.response_frame(0x0D, b"\x01\x02\x03")
        fields, note = framediag.describe(raw)
        self.assertIn("未知命令 0x0D", note)
        self.assertNotIn("payload（不完整）", [f.name for f in fields])
        self.assertNotIn("当前温度", text_of(raw))
        self.assertIn("字节带", text_of(raw))

    def test_lrc_error_is_flagged(self):
        raw = bytearray(hc.frame_realtime())
        raw[-1] ^= 0xFF
        t = text_of(bytes(raw))
        self.assertIn("LRC 校验", t)
        self.assertIn("错（应为 0x", t)

    def test_lrc_ok(self):
        self.assertIn("ok", text_of(hc.frame_realtime()))

    def test_short_payload_is_reported(self):
        raw = hc.response_frame(hc.CMD_REALTIME, b"\x28\x00\x8C")
        t = text_of(raw)
        self.assertIn("payload（不完整）", t)
        self.assertIn("期望 11 字节", t)

    def test_length_field_mismatch_is_reported(self):
        raw = bytearray(hc.frame_realtime())
        raw[4] = 5                      # 长度字段写坏
        t = text_of(bytes(raw))
        self.assertIn("长度不符", t)

    def test_byte_band_is_one_line_up_to_32_bytes(self):
        """17 字节的 RX 帧必须单行：以前 16 字节/行会留一个「偏移 16 / 字节 8B」的半行。"""
        payload = hc.build_realtime_payload(
            current_temp=40, supply_power_w=140, output_power_w=6, target_temp=40,
            channel_or_stage=0, mode=2, w2=0x29BF, input_voltage_raw=279,
            output_state_raw=0x02)
        raw = hc.response_frame(hc.CMD_REALTIME, payload)
        self.assertEqual(len(raw), 17)
        lines = framediag.byte_band(raw)
        self.assertEqual(sum(1 for line in lines if "偏移" in line), 1, lines)
        self.assertEqual(sum(1 for line in lines if line.startswith("  字节")), 1, lines)
        body = [line for line in lines if line.startswith("  字节")][0]
        self.assertEqual(len(body.replace("  字节", "").split()), 17)

    def test_byte_band_wraps_only_over_32_bytes(self):
        raw = hc.response_frame(0x0D, bytes(range(28)))          # 5+28+1 = 34 字节
        self.assertEqual(len(raw), 34)
        lines = framediag.byte_band(raw)
        self.assertEqual(sum(1 for line in lines if "偏移" in line), 2, lines)
        rows = [line for line in lines if line.startswith("  字节")]
        self.assertEqual(len(rows[0].replace("  字节", "").split()), 32)
        self.assertEqual(len(rows[1].replace("  字节", "").split()), 2)

    def test_bytes_per_line_constant_is_32(self):
        self.assertEqual(framediag.BYTES_PER_LINE, 32)


def _app_or_skip(test):
    import tkinter as tk
    import atk_hp20 as app_mod
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        test.skipTest(f"没有可用显示: {exc}")
        return None, None
    opts = app_mod.build_parser().parse_args(["--sim"])
    original = app_mod.should_auto_connect
    app_mod.should_auto_connect = lambda _o: False
    try:
        app = app_mod.Hp20App(root, opts)
    finally:
        app_mod.should_auto_connect = original
    root.update()
    return root, app


STATUS_PAYLOAD = dict(current_temp=40, supply_power_w=140, output_power_w=6,
                      target_temp=40, channel_or_stage=0, mode=2, w2=0x29BF,
                      input_voltage_raw=279, output_state_raw=0x02)


class PanelCase(unittest.TestCase):
    def setUp(self):
        self.root, self.app = _app_or_skip(self)
        self.renders = []
        original = self.app.debug_text.insert

        def counting_insert(*args, **kwargs):
            self.renders.append(time.monotonic())
            return original(*args, **kwargs)

        self.app.debug_text.insert = counting_insert
        self.show_panel()

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app._on_close()
            except Exception:  # noqa: BLE001
                pass

    def show_panel(self):
        """切到设备页（面板可见）。"""
        self.app.notebook.select(self.app.device_tab)
        self.root.update()

    def hide_panel(self):
        self.app.notebook.select(0)
        self.root.update()

    def feed_status(self, direction="RX"):
        raw = hc.response_frame(hc.CMD_REALTIME, hc.build_realtime_payload(**STATUS_PAYLOAD))
        self.app._handle_event("frame", (direction, raw))
        return raw


class TestPanelContent(PanelCase):
    def test_snapshot_has_band_table_and_counts(self):
        raw = self.feed_status()
        self.app._refresh_debug_panel(force=True)
        text = self.app._debug_snapshot()
        self.assertIn("最近发送", text)
        self.assertIn("最近接收", text)
        self.assertIn("RX←设备", text)
        self.assertIn("字节带", text)
        self.assertIn("字段解析", text)
        self.assertIn("计数", text)
        self.assertIn("丢帧", text)
        self.assertIn("重连", text)
        self.assertIn("DF  09  02  0A", text)     # 状态帧原始字节在字节带里
        self.assertIn("当前温度", text)
        self.assertIn("0A 读实时状态", text)

    def test_removed_blocks_are_gone(self):
        """反向断言：用户要求删掉的两块不能再出现。"""
        self.feed_status()
        self.feed_status("TX")
        text = self.app._debug_snapshot()
        self.assertNotIn("原始收发", text)
        self.assertNotIn("W2 = 0x", text)
        self.assertNotIn("低6位", text)
        self.assertNotIn("次7位", text)

    def test_tx_frame_is_shown_with_direction(self):
        tx = hc.frame_set_thermostat(hc.TARGET_INDEX, 60)
        self.app._handle_event("frame", ("TX", tx))
        text = self.app._debug_snapshot()
        self.assertIn("最近发送 (TX→设备", text)
        self.assertIn("温度 temp", text)
        self.assertIn("60℃", text)

    def test_text_widget_is_read_only(self):
        self.feed_status()
        self.app._refresh_debug_panel(force=True)
        self.assertEqual(str(self.app.debug_text.cget("state")), "disabled")

    def test_copy_all_matches_panel(self):
        self.feed_status()
        self.app._refresh_debug_panel(force=True)
        clipboard = []
        self.app.root.clipboard_clear = lambda: clipboard.clear()
        self.app.root.clipboard_append = lambda text: clipboard.append(text)
        self.app._copy_debug()
        self.assertEqual(len(clipboard), 1)
        self.assertTrue(clipboard[0].strip())
        for word in ("字节带", "字段解析", "计数"):
            self.assertIn(word, clipboard[0])
        self.assertNotIn("原始收发", clipboard[0])

    def test_tx_and_rx_blocks_coexist(self):
        """回归防线：TX 与 RX 各自保留最后一帧，不再互相覆盖（面板不再来回闪）。"""
        tx = hc.frame_set_thermostat(hc.TARGET_INDEX, 60)
        rx = hc.response_frame(hc.CMD_REALTIME, hc.build_realtime_payload(**STATUS_PAYLOAD))
        self.app._handle_event("frame", ("TX", tx))
        self.app._handle_event("frame", ("RX", rx))
        text = self.app._debug_snapshot()
        self.assertIn("最近发送 (TX→设备", text)
        self.assertIn("最近接收 (RX←设备", text)
        # 字节带是按列对齐的（字节间两个空格），所以逐行比对字节带里的真实文本
        for line in framediag.byte_band(tx):
            self.assertIn(line, text, "TX 的原始字节被 RX 覆盖了")
        for line in framediag.byte_band(rx):
            self.assertIn(line, text, "RX 的原始字节被 TX 覆盖了")
        # 两段各自的时间戳都要在
        self.assertIn(self.app._last_tx[1], text)
        self.assertIn(self.app._last_rx[1], text)

    def test_placeholder_when_one_direction_missing(self):
        tx = hc.frame_set_thermostat(hc.TARGET_INDEX, 60)
        self.app._handle_event("frame", ("TX", tx))
        text = self.app._debug_snapshot()
        self.assertIn("最近发送 (TX→设备", text)
        self.assertIn("还没有收到过帧", text)
        self.assertNotIn("还没有发送过帧", text)
        self.app._clear_debug()
        self.app._handle_event("frame", ("RX", hc.response_frame(
            hc.CMD_REALTIME, hc.build_realtime_payload(**STATUS_PAYLOAD))))
        text = self.app._debug_snapshot()
        self.assertIn("还没有发送过帧", text)
        self.assertIn("最近接收 (RX←设备", text)

    def test_copy_all_contains_both_blocks(self):
        tx = hc.frame_set_thermostat(hc.TARGET_INDEX, 60)
        rx = hc.response_frame(hc.CMD_REALTIME, hc.build_realtime_payload(**STATUS_PAYLOAD))
        self.app._handle_event("frame", ("TX", tx))
        self.app._handle_event("frame", ("RX", rx))
        self.app._refresh_debug_panel(force=True)
        clipboard = []
        self.app.root.clipboard_clear = lambda: clipboard.clear()
        self.app.root.clipboard_append = lambda text: clipboard.append(text)
        self.app._copy_debug()
        copied = clipboard[0]
        self.assertIn("最近发送", copied)
        self.assertIn("最近接收", copied)
        self.assertIn("计数", copied)

    def test_clear_shows_empty_state(self):
        self.feed_status()
        self.app._refresh_debug_panel(force=True)
        self.app._clear_debug()
        text = self.app.debug_text.get("1.0", "end")
        self.assertIn("还没有发送过帧", text)
        self.assertIn("还没有收到过帧", text)
        self.assertIsNone(self.app._last_tx)
        self.assertIsNone(self.app._last_rx)


class TestPanelRefreshPolicy(PanelCase):
    def test_ten_frames_in_one_second_render_at_most_three_times(self):
        started = time.monotonic()
        for _ in range(10):
            self.feed_status()
            self.app._refresh_debug_panel()
            self.root.update()
            time.sleep(0.05)                 # ~0.5s 内 10 帧
        while time.monotonic() - started < 1.0:
            self.root.update()
            time.sleep(0.02)
        self.root.update()
        self.assertLessEqual(len(self.renders), 3,
                             f"1 秒内重写了 {len(self.renders)} 次（应 ≤2 次/秒）")

    def test_scroll_position_is_preserved(self):
        for _ in range(6):                   # 塞长内容，制造可滚动
            self.feed_status()
            self.app._refresh_debug_panel(force=True)
        self.root.update()
        self.app.debug_text.yview_moveto(0.35)
        self.root.update()
        before = self.app.debug_text.yview()
        self.app._refresh_debug_panel(force=True)
        self.root.update()
        after = self.app.debug_text.yview()
        self.assertAlmostEqual(before[0], after[0], places=2,
                               msg=f"重写后滚动位置跳了: {before} -> {after}")

    def test_not_visible_no_rewrite_then_refresh_when_back(self):
        self.hide_panel()
        before = len(self.renders)
        for _ in range(5):
            self.feed_status()
            self.app._refresh_debug_panel()
            self.root.update()
        self.assertEqual(len(self.renders), before, "面板不可见时不该重写")
        self.assertTrue(self.app._debug_dirty, "不可见期间要留着待刷标记")
        self.show_panel()                    # 切回来 -> 补刷
        self.root.update()
        self.assertGreater(len(self.renders), before)

    def test_auto_refresh_off_waits_for_manual_refresh(self):
        self.app._debug_auto.set(False)
        self.app._refresh_debug_panel(force=True)      # 先让缓存与内容同步
        before = len(self.renders)
        self.feed_status()
        self.app._refresh_debug_panel()
        self.root.update()
        self.assertEqual(len(self.renders), before, "关掉自动刷新后不该自己刷")
        self.app._refresh_debug_panel(force=True)      # 「刷新一次」
        self.root.update()
        self.assertGreater(len(self.renders), before)
        self.assertIn("最近接收", self.app.debug_text.get("1.0", "end"))


if __name__ == "__main__":
    unittest.main()
