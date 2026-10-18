"""用户报的界面 BUG：待机时输出功率进度条卡死不动，应该显示 0%。

根因：进度条的值直接取 `[6]/[2]`（output_power_w / supply_power_w）。真机待机帧仍报
1~2 W 的静态读数（docs/hp20_extra_findings.md 待机帧 A：输出 2 W / 供电 65 W），
于是待机时进度条稳定停在 1~3% 一动不动；而且它只在收到状态帧时更新，断开后还留着上一帧。
"""
import unittest

import hp20_commands as hc


def status(supply=140, output=11, mode=2, state=0x02, temp=35, target=250):
    return hc.parse_realtime(hc.build_realtime_payload(
        current_temp=temp, supply_power_w=supply, output_power_w=output,
        target_temp=target, channel_or_stage=3, mode=mode, w2=0x04B2,
        input_voltage_raw=200, output_state_raw=state))


class TestBarPowerPercent(unittest.TestCase):
    """纯函数：进度条的占比口径。"""

    def test_no_status_is_zero(self):
        self.assertEqual(hc.bar_power_percent(None), 0.0)

    def test_standby_real_frame_is_zero(self):
        """真机待机帧：输出 2 W / 供电 65 W —— 设备残留值，界面与 CSV 都要按 0 处理。"""
        standby = status(supply=65, output=2, mode=hc.MODE_IDLE, state=0x00)
        self.assertEqual(standby.effective_power_w, 0)     # V 阶段统一口径
        self.assertEqual(standby.power_percent, 0.0)       # CSV power_pct 同样写 0
        self.assertEqual(hc.bar_power_percent(standby), 0.0)

    def test_standby_with_big_supply_also_zero(self):
        self.assertEqual(hc.bar_power_percent(
            status(supply=140, output=2, mode=hc.MODE_IDLE, state=0x00)), 0.0)

    def test_heating_uses_output_over_supply(self):
        self.assertAlmostEqual(hc.bar_power_percent(status(supply=140, output=11)),
                               11 * 100 / 140, places=6)

    def test_holding_with_zero_output_is_zero(self):
        self.assertEqual(hc.bar_power_percent(status(supply=140, output=0)), 0.0)

    def test_zero_supply_is_zero(self):
        self.assertEqual(hc.bar_power_percent(status(supply=0, output=0)), 0.0)


def _app_or_skip(test):
    import tkinter as tk
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        test.skipTest(f"没有可用显示: {exc}")
        return None, None
    import atk_hp20 as app_mod
    opts = app_mod.build_parser().parse_args(["--sim"])
    original = app_mod.should_auto_connect
    app_mod.should_auto_connect = lambda _o: False
    try:
        app = app_mod.Hp20App(root, opts)
    finally:
        app_mod.should_auto_connect = original
    root.update()
    return root, app


class TestPowerBarWidget(unittest.TestCase):
    """界面级：每次状态帧都重画进度条，待机归零、且不保留上一帧。"""

    def setUp(self):
        self.root, self.app = _app_or_skip(self)

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app._on_close()
            except Exception:  # noqa: BLE001
                pass

    def bar_text(self) -> str:
        canvas = self.app.power_bar
        texts = [canvas.itemcget(item, "text") for item in canvas.find_all()
                 if canvas.type(item) == "text"]
        return texts[0] if texts else ""

    def bar_width(self) -> float:
        canvas = self.app.power_bar
        rects = [canvas.coords(item) for item in canvas.find_all()
                 if canvas.type(item) == "rectangle"]
        return rects[0][2] if rects else 0.0

    def test_standby_shows_zero_percent(self):
        self.app._on_status(status(supply=65, output=2, mode=hc.MODE_IDLE, state=0x00))
        self.root.update()
        self.assertEqual(self.bar_text(), "0%")
        self.assertEqual(self.app.power_pct_var.get(), "输出功率占比 0%")
        self.assertEqual(self.bar_width(), 0.0, "待机时进度条不该有任何填充")

    def test_every_frame_recomputed_not_carried_over(self):
        self.app._on_status(status(supply=140, output=11))          # 7.9%
        self.root.update()
        self.assertEqual(self.bar_text(), "8%")
        self.assertGreater(self.bar_width(), 0.0)
        self.app._on_status(status(supply=65, output=2, mode=hc.MODE_IDLE, state=0x00))
        self.root.update()
        self.assertEqual(self.bar_text(), "0%", "上一帧的占比不能留着")
        self.assertEqual(self.bar_width(), 0.0)
        self.app._on_status(status(supply=140, output=44, mode=2))   # 31.4%
        self.root.update()
        self.assertEqual(self.bar_text(), "31%", "回到加热帧要重新算出来")

    def test_link_down_resets_bar(self):
        self.app._on_status(status(supply=140, output=11))
        self.root.update()
        self.assertNotEqual(self.bar_text(), "0%")
        self.app._set_link(False, "未连接")
        self.root.update()
        self.assertEqual(self.bar_text(), "0%")
        self.assertEqual(self.app.power_pct_var.get(), "输出功率占比 0%")


if __name__ == "__main__":
    unittest.main()
