"""界面配色与文案的纯常量断言（G 阶段：功率线换青色，避免与温度红线混淆）。"""
import unittest

import atk_hp20 as app_mod
import chart


class TestChartColors(unittest.TestCase):
    def test_power_line_differs_from_temperature(self):
        self.assertNotEqual(app_mod.COLOR_POWER, app_mod.COLOR_TEMP)

    def test_power_line_is_cyan(self):
        self.assertEqual(app_mod.COLOR_POWER.lower(), "#00bcd4")

    def test_temperature_line_stays_red(self):
        self.assertEqual(app_mod.COLOR_TEMP.lower(), "#c0392b")

    def test_legend_texts_differ(self):
        labels = {key for key, _ in
                  chart.readout_rows(chart.Sample(0, 40, 250, 5, 0x29BF, 2))}
        self.assertIn("当前温度", labels)
        self.assertIn("当前功率", labels)


class TestLinkButtons(unittest.TestCase):
    """L 阶段 bug：断开后「连接」按钮必须恢复可用，否则用户被锁死。"""

    def test_after_disconnect_connect_is_enabled(self):
        connect, disconnect = app_mod.link_button_states(connected=False)
        self.assertEqual(connect, "normal")
        self.assertEqual(disconnect, "disabled")

    def test_after_scan_done_connect_is_enabled(self):
        connect, _ = app_mod.link_button_states(connected=False, connecting=False)
        self.assertEqual(connect, "normal")

    def test_while_connecting_connect_stays_clickable(self):
        """连错设备时不能再被锁死（用户反馈：只能硬等 45s）—— 按钮变成「取消连接」。"""
        connect, disconnect = app_mod.link_button_states(connected=False,
                                                         connecting=True)
        self.assertEqual(connect, "normal")
        self.assertEqual(disconnect, "disabled")
        self.assertEqual(app_mod.connect_button_text(True), "取消连接")
        self.assertEqual(app_mod.connect_button_text(False), "连接")
        # 已连接时不该再显示「取消连接」
        self.assertEqual(app_mod.connect_button_text(True, connected=True), "连接")

    def test_when_connected_disconnect_is_enabled(self):
        connect, disconnect = app_mod.link_button_states(connected=True)
        self.assertEqual(connect, "disabled")
        self.assertEqual(disconnect, "normal")

    def test_connected_beats_connecting(self):
        """已连接时即使还残留 connecting 标志，也不该把断开禁用掉。"""
        connect, disconnect = app_mod.link_button_states(connected=True,
                                                         connecting=True)
        self.assertEqual(connect, "disabled")
        self.assertEqual(disconnect, "normal")


class TestModeWording(unittest.TestCase):
    def test_power_row_shows_watts(self):
        rows = dict(chart.readout_rows(chart.Sample(0, 40, 250, 11, 0x29BF, 2)))
        self.assertEqual(rows["当前功率"], "11 W")
        self.assertNotIn("单位待定", rows["当前功率"])


if __name__ == "__main__":
    unittest.main()
