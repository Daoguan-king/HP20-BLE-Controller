"""O 阶段：轮询/写间隔输入框（手输不生效的 BUG）+ 主界面文案精简的反向断言。"""
import unittest

import atk_hp20 as app_mod


class TestParseInterval(unittest.TestCase):
    """验收五例 + 边界。"""

    def test_plain_integer(self):
        self.assertEqual(app_mod.parse_interval("1", 0.05, 5.0), 1.0)

    def test_three_decimals(self):
        self.assertEqual(app_mod.parse_interval("0.125", 0.05, 5.0), 0.125)

    def test_non_numeric(self):
        self.assertIsNone(app_mod.parse_interval("abc", 0.05, 5.0))
        self.assertIsNone(app_mod.parse_interval("", 0.05, 5.0))
        self.assertIsNone(app_mod.parse_interval("1.2.3", 0.05, 5.0))

    def test_below_range(self):
        self.assertIsNone(app_mod.parse_interval("0", 0.05, 5.0))
        self.assertIsNone(app_mod.parse_interval("0.01", 0.05, 5.0))

    def test_above_range(self):
        self.assertIsNone(app_mod.parse_interval("9", 0.05, 5.0))

    def test_bounds_are_inclusive(self):
        self.assertEqual(app_mod.parse_interval("0.05", 0.05, 5.0), 0.05)
        self.assertEqual(app_mod.parse_interval("5.0", 0.05, 5.0), 5.0)

    def test_strips_spaces(self):
        self.assertEqual(app_mod.parse_interval("  0.2  ", 0.05, 5.0), 0.2)

    def test_nan_and_inf_rejected(self):
        self.assertIsNone(app_mod.parse_interval("nan", 0.05, 5.0))
        self.assertIsNone(app_mod.parse_interval("inf", 0.05, 5.0))

    def test_gap_range_allows_zero(self):
        self.assertEqual(app_mod.parse_interval("0", 0.0, 1.0), 0.0)


def _app_or_skip(test):
    """需要能创建 Tk 窗口；无显示环境就跳过。"""
    import tkinter as tk
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        test.skipTest(f"没有可用显示: {exc}")
        return None, None
    opts = app_mod.build_parser().parse_args(["--sim"])
    # 测试里不要自动连接（否则退出时会有在途协程告警）
    original = app_mod.should_auto_connect
    app_mod.should_auto_connect = lambda _opts: False
    try:
        app = app_mod.Hp20App(root, opts)
    finally:
        app_mod.should_auto_connect = original
    root.update()
    return root, app


class TestIntervalEntryCommits(unittest.TestCase):
    """验收：模拟 <Return> 后设备层 poll_interval 真的变成输入值。"""

    def setUp(self):
        self.root, self.app = _app_or_skip(self)

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app._on_close()
            except Exception:  # noqa: BLE001
                pass

    def _press_return(self, widget) -> None:
        """Tk 的按键事件需要焦点，否则 event_generate 会被丢弃。"""
        widget.focus_force()
        self.root.update()
        widget.event_generate("<Return>", when="now")
        self.root.update()

    def test_return_commits_poll_interval(self):
        self.app.poll_var.set("1")
        self._press_return(self.app.poll_spin)
        self.assertEqual(self.app.device.poll_interval, 1.0)
        self.assertEqual(self.app.poll_var.get(), "1.000")
        self.assertIn("1.000s", self.app.rate_var.get())

    def test_focus_out_commits_poll_interval(self):
        self.app.poll_var.set("0.5")
        self.app.poll_spin.event_generate("<FocusOut>", when="now")
        self.root.update()
        self.assertEqual(self.app.device.poll_interval, 0.5)

    def test_invalid_value_reverts_and_warns(self):
        before = self.app.device.poll_interval
        self.app.poll_var.set("9")
        self._press_return(self.app.poll_spin)
        self.assertEqual(self.app.device.poll_interval, before, "越界值不能生效")
        self.assertEqual(self.app.poll_var.get(), f"{before:.3f}", "显示要回退成生效值")
        self.assertIn("回退", self.app.tip_var.get())

    def test_garbage_reverts(self):
        before = self.app.device.poll_interval
        self.app.poll_var.set("abc")
        self._press_return(self.app.poll_spin)
        self.assertEqual(self.app.device.poll_interval, before)

    def test_gap_enter_commits(self):
        self.app.gap_var.set("0.02")
        self._press_return(self.app.gap_spin)
        self.assertEqual(self.app.device.write_gap, 0.02)
        self.assertIn("0.020s", self.app.rate_var.get())


class TestMonitorLayout(unittest.TestCase):
    """验收：CSV 控件在监控页；主界面文案不含技术细节。"""

    def setUp(self):
        self.root, self.app = _app_or_skip(self)

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app._on_close()
            except Exception:  # noqa: BLE001
                pass

    def _ancestors(self, widget):
        chain = []
        while widget is not None:
            chain.append(widget)
            widget = widget.master
        return chain

    def _descendant_texts(self, widget):
        texts = []
        try:
            text = widget.cget("text")
        except Exception:  # noqa: BLE001
            text = ""
        if text:
            texts.append(str(text))
        try:
            var = widget.cget("textvariable")
            if var:
                texts.append(self.app.getvar(var))
        except Exception:  # noqa: BLE001
            pass
        for child in widget.winfo_children():
            texts.extend(self._descendant_texts(child))
        return texts

    def test_csv_controls_live_in_monitor_tab(self):
        chain = self._ancestors(self.app.csv_btn)
        self.assertIn(self.app.monitor_tab, chain, "开始/停止记录应在实时监控页")
        self.assertIn(self.app.monitor_tab, self._ancestors(self.app.poll_spin))

    def test_csv_format_choices_are_short(self):
        box_values = None
        for child in self.app.monitor_tab.winfo_children():
            for sub in child.winfo_children():
                try:
                    if sub.winfo_class() == "TCombobox":
                        box_values = tuple(sub.cget("values"))
                except Exception:  # noqa: BLE001
                    pass
        self.assertIsNotNone(box_values)
        self.assertEqual(box_values, ("完整", "简洁"))

    def test_main_ui_has_no_protocol_details(self):
        """反向断言：主界面不允许出现字段偏移/位运算/内部原值。"""
        s = self.app.device.status
        self.app._on_status(self._sample_status())
        texts = " ".join(self._descendant_texts(self.app.monitor_tab))
        for banned in ("payload[", "W1&", "0x7F", "风扇 不可读", "W2=", "bit13"):
            self.assertNotIn(banned, texts, f"主界面不应出现 {banned}")
        self.assertIn("当前温度", texts)
        self.assertIn("目标温度", texts)
        self.assertIn("模式", texts)

    def test_diagnostic_panel_keeps_raw_values(self):
        """被移出主界面的原值要能在设备页「诊断详情」的字段解析表里看到。

        （R 阶段：用户要求删掉开头那行「W2 = 0x… 低6位…」，原值改由字段表承载。）
        """
        import hp20_commands as hc
        payload = hc.build_realtime_payload(
            current_temp=50, supply_power_w=45, output_power_w=44, target_temp=60,
            channel_or_stage=0, mode=2, w2=1280, input_voltage_raw=271,
            output_state_raw=0x02)
        self.app._handle_event("frame", ("RX", hc.response_frame(hc.CMD_REALTIME, payload)))
        text = self.app._debug_snapshot()
        self.assertIn("W2 计时", text)
        self.assertIn("i16le", text)
        self.assertIn("0X0500=1280", text.upper())   # W2=1280 的原始值
        self.assertIn("20:00", text)               # 64 进制解码

    @staticmethod
    def _sample_status():
        import hp20_commands as hc
        return hc.parse_realtime(hc.build_realtime_payload(
            current_temp=50, supply_power_w=45, output_power_w=44, target_temp=60,
            channel_or_stage=0, mode=2, w2=1280, input_voltage_raw=271,
            output_state_raw=0x02))


if __name__ == "__main__":
    unittest.main()
