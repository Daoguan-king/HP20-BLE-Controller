"""T 阶段：从「原始帧控制台」挑一条历史帧 → 钉进诊断面板第三段（不被轮询刷新覆盖）。"""
import time
import unittest

import framediag
import framing
import hp20_commands as hc

STATUS_PAYLOAD = dict(current_temp=40, supply_power_w=140, output_power_w=6,
                      target_temp=40, channel_or_stage=0, mode=2, w2=0x29BF,
                      input_voltage_raw=279, output_state_raw=0x02)


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


class ConsoleCase(unittest.TestCase):
    def setUp(self):
        self.root, self.app = _app_or_skip(self)
        self.app_mod = __import__("atk_hp20")

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app._on_close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------- 控制台辅助
    def log_frame(self, direction: str, raw: bytes) -> None:
        self.app._append_raw(direction, raw)
        self.root.update()

    def last_content_line(self) -> int:
        """最后一条非空行的行号（用文本内容算，别拿 end-1c 去猜行号）。"""
        lines = self.app.raw_text.get("1.0", "end-1c").split("\n")
        for i in range(len(lines), 0, -1):
            if lines[i - 1].strip():
                return i
        return 1

    def select_line(self, number: int) -> None:
        self.app.raw_text.tag_remove("sel", "1.0", "end")
        self.app.raw_text.tag_add("sel", f"{number}.0", f"{number}.0 lineend")
        self.app._update_parse_button_state()

    def select_last_line(self) -> int:
        number = self.last_content_line()
        self.select_line(number)
        return number

    def status_frame(self) -> bytes:
        return hc.response_frame(hc.CMD_REALTIME, hc.build_realtime_payload(**STATUS_PAYLOAD))


class TestConsoleLineParser(unittest.TestCase):
    """控制台行 -> (方向, 原始字节, 时间) 的纯解析。"""

    def parse(self, line):
        import atk_hp20 as app_mod
        return app_mod.Hp20App._parse_console_line(line)

    def test_frame_line(self):
        record = self.parse("15:52:31 TX AF 09 02 03 03 04 3C 00 00")
        self.assertIsNotNone(record)
        direction, raw, stamp = record
        self.assertEqual(direction, "TX")
        self.assertEqual(stamp, "15:52:31")
        self.assertEqual(raw, bytes.fromhex("AF09020303043C0000"))

    def test_lowercase_hex_accepted(self):
        self.assertIsNotNone(self.parse("15:52:31 RX df 09 02 0a 00 fd"))

    def test_sys_and_blank_lines_rejected(self):
        self.assertIsNone(self.parse("15:52:31 SYS 组帧: AF 09 02 0A 00 3C"))
        self.assertIsNone(self.parse(""))
        self.assertIsNone(self.parse("   续行 AF 09"))

    def test_too_short_or_not_a_frame_rejected(self):
        self.assertIsNone(self.parse("15:52:31 TX AF 09 02 03"))          # 不足 6 字节
        self.assertIsNone(self.parse("15:52:31 TX 11 22 33 44 55 66"))    # 头不对

    def test_odd_hex_rejected(self):
        self.assertIsNone(self.parse("15:52:31 TX AF 09 02 0A 0"))


class TestPinFromConsole(ConsoleCase):
    def test_selected_tx_frame_fills_third_block(self):
        tx = hc.frame_set_thermostat(hc.TARGET_INDEX, 60)
        self.log_frame("TX", tx)
        self.log_frame("RX", self.status_frame())
        self.select_line(self.last_content_line() - 1)      # 选中 TX 那一行
        self.assertEqual(str(self.app.parse_sel_btn.cget("state")), "normal")
        self.app._on_parse_selected_raw()

        text = self.app._debug_snapshot()
        self.assertIn("选中的帧（来自原始帧控制台", text)
        for line in framediag.byte_band(tx):                # 字节带与该帧一致
            self.assertIn(line, text)
        for word in ("字段解析", "index", "温度 temp", "60℃", "目标温度 T5"):
            self.assertIn(word, text)
        self.assertEqual(self.app.notebook.index("current"), self.app_mod.TAB_DEVICE)
        self.assertEqual(str(self.app.clear_pin_btn.cget("state")), "normal")

    def test_pinned_block_survives_new_frames(self):
        pinned = hc.frame_set_thermostat(1, 45)
        self.log_frame("TX", pinned)
        self.select_last_line()
        self.app._on_parse_selected_raw()
        stamp_before = self.app._pinned_frame[2]
        last_rx_before = self.app._last_rx

        for i in range(5):                                  # 再投 10 帧并刷新
            self.app._handle_event("frame", ("TX", hc.frame_realtime()))
            self.app._handle_event("frame", ("RX", self.status_frame()))
            self.app._refresh_debug_panel(force=True)
        text = self.app._debug_snapshot()
        self.assertIn(f"选中的帧（来自原始帧控制台  TX→设备  {stamp_before}）", text)
        for line in framediag.byte_band(pinned):
            self.assertIn(line, text, "第三段被新帧覆盖了")
        self.assertNotEqual(self.app._last_rx, last_rx_before)
        self.assertEqual(self.app._pinned_frame[1], pinned)

    def test_clear_pin_removes_block(self):
        self.log_frame("TX", hc.frame_set_thermostat(0, 100))
        self.select_last_line()
        self.app._on_parse_selected_raw()
        self.assertIn("选中的帧", self.app._debug_snapshot())
        self.app._on_clear_pin()
        text = self.app._debug_snapshot()
        self.assertNotIn("选中的帧", text)
        self.assertNotIn("来自原始帧控制台", text)
        self.assertEqual(str(self.app.clear_pin_btn.cget("state")), "disabled")

    def test_copy_all_includes_pinned_only_when_present(self):
        clipboard = []
        self.app.root.clipboard_clear = lambda: clipboard.clear()
        self.app.root.clipboard_append = lambda text: clipboard.append(text)
        self.app._copy_debug()
        self.assertNotIn("选中的帧", clipboard[0])

        self.log_frame("RX", self.status_frame())
        self.select_last_line()
        self.app._on_parse_selected_raw()
        self.app._copy_debug()
        self.assertIn("选中的帧（来自原始帧控制台", clipboard[-1])
        self.assertIn("当前温度", clipboard[-1])

    def test_latest_clear_does_not_touch_pinned(self):
        pinned = hc.frame_set_thermostat(2, 55)
        self.log_frame("TX", pinned)
        self.select_last_line()
        self.app._on_parse_selected_raw()
        self.app._handle_event("frame", ("RX", self.status_frame()))
        self.app._clear_debug()                     # 只清最近收发
        text = self.app._debug_snapshot()
        self.assertIn("选中的帧", text)
        for line in framediag.byte_band(pinned):
            self.assertIn(line, text)
        self.assertIn("还没有收到过帧", text)       # 最近收发被清了
        self.assertIsNone(self.app._last_rx)

    def test_broken_pinned_record_is_reported(self):
        self.app._pinned_frame = ("TX", b"\xAF\x09", "12:00:00")
        text = self.app._debug_snapshot()
        self.assertIn("该记录不是完整帧", text)
        self.assertIn("选中的帧（来自原始帧控制台  TX→设备  12:00:00）", text)

    def test_double_click_pins_the_clicked_line(self):
        self.app.notebook.select(self.app.raw_tab)
        self.root.update()
        tx = hc.frame_set_thermostat(3, 70)
        self.log_frame("TX", tx)
        number = self.last_content_line()
        bbox = self.app.raw_text.bbox(f"{number}.0")
        self.assertIsNotNone(bbox, "原始帧控制台不可见，取不到行坐标")
        event = type("Event", (), {"x": bbox[0] + 3, "y": bbox[1] + 3, "widget": self.app.raw_text})
        self.app._on_raw_double_click(event)
        self.assertIsNotNone(self.app._pinned_frame)
        self.assertEqual(self.app._pinned_frame[1], tx)
        for line in framediag.byte_band(tx):
            self.assertIn(line, self.app._debug_snapshot())


class TestParseButtonState(ConsoleCase):
    def test_disabled_without_selection(self):
        self.assertEqual(str(self.app.parse_sel_btn.cget("state")), "disabled")
        self.app._update_parse_button_state()
        self.assertEqual(str(self.app.parse_sel_btn.cget("state")), "disabled")

    def test_disabled_for_non_frame_line(self):
        self.app._append_text(f"{time.strftime('%H:%M:%S')} SYS 这是一条说明行")
        self.root.update()
        self.select_last_line()
        self.assertEqual(str(self.app.parse_sel_btn.cget("state")), "disabled")
        self.assertEqual(self.app.tip_var.get(), "请选择一条完整帧")
        self.app._on_parse_selected_raw()            # 点了也不该钉住
        self.assertIsNone(self.app._pinned_frame)

    def test_enabled_after_selecting_frame_line(self):
        self.log_frame("RX", self.status_frame())
        self.select_last_line()
        self.assertEqual(str(self.app.parse_sel_btn.cget("state")), "normal")

    def test_clear_log_disables_button(self):
        self.log_frame("RX", self.status_frame())
        self.select_last_line()
        self.assertEqual(str(self.app.parse_sel_btn.cget("state")), "normal")
        self.app._clear_raw()
        self.assertEqual(str(self.app.parse_sel_btn.cget("state")), "disabled")
        self.assertEqual(self.app._raw_selection_text(), "")


if __name__ == "__main__":
    unittest.main()
