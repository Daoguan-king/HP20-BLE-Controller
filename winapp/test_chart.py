"""图表几何 / 双轴数据准备 / 百分条 / 取点 —— 纯函数单测（不需要 tkinter）。"""
import time
import unittest

import chart


class TestLayout(unittest.TestCase):
    def test_single_plot_area(self):
        """L 阶段改成单图双轴：只有一块绘图区，不再上下分栏。"""
        lay = chart.layout(900, 400)
        self.assertEqual(lay.left, chart.PAD_L)
        self.assertEqual(lay.right, 900 - chart.PAD_R)
        self.assertEqual(lay.top, chart.PAD_T)
        self.assertEqual(lay.bottom, 400 - chart.PAD_B)
        self.assertEqual(lay.plot_w, lay.right - lay.left)
        self.assertEqual(lay.plot_h, lay.bottom - lay.top)
        self.assertFalse(hasattr(lay, "temp_top"), "不应再有上下两栏")

    def test_tiny_canvas_is_clamped(self):
        lay = chart.layout(10, 10)
        self.assertGreaterEqual(lay.plot_w, 10)
        self.assertGreaterEqual(lay.plot_h, 10)


class TestDualAxis(unittest.TestCase):
    """验收项：数据准备函数返回 (温度序列, 功率序列, 双轴范围)。"""

    def samples(self):
        return [chart.Sample(ts=float(i), temp=30 + i, target=120, power=i * 5,
                             w2=1229, mode=2) for i in range(5)]

    def test_returns_both_series_and_both_ranges(self):
        axis = chart.dual_axis(self.samples())
        self.assertEqual(axis.temps, [30.0, 31.0, 32.0, 33.0, 34.0])
        self.assertEqual(axis.powers, [0.0, 5.0, 10.0, 15.0, 20.0])
        self.assertLessEqual(axis.temp_lo, 30.0)
        self.assertGreaterEqual(axis.temp_hi, 120.0)      # 目标温度也要进范围
        self.assertGreater(axis.temp_step, 0)
        self.assertGreaterEqual(axis.power_max, 20)

    def test_empty_series(self):
        axis = chart.dual_axis([])
        self.assertEqual((axis.temps, axis.powers), ([], []))
        self.assertEqual(axis.power_max, 10)

    def test_power_range_covers_peak(self):
        self.assertGreaterEqual(chart.dual_axis(self.samples()).power_max, 20)
        big = [chart.Sample(0, 30, 30, 135, 0, 2)]
        self.assertGreaterEqual(chart.dual_axis(big).power_max, 135)


class TestAxes(unittest.TestCase):
    def setUp(self):
        self.lay = chart.layout(900, 400)

    def test_both_series_share_one_plot_area(self):
        """双轴共用同一块绘图区：y_temp 与 y_power 的端点必须对齐。"""
        self.assertAlmostEqual(
            chart.y_temp(0, self.lay, 0, 100),
            chart.y_power(0, self.lay, 100))
        self.assertAlmostEqual(
            chart.y_temp(100, self.lay, 0, 100),
            chart.y_power(100, self.lay, 100))

    def test_y_temp_endpoints(self):
        self.assertAlmostEqual(chart.y_temp(0, self.lay, 0, 100), self.lay.bottom)
        self.assertAlmostEqual(chart.y_temp(100, self.lay, 0, 100), self.lay.top)
        self.assertAlmostEqual(chart.y_temp(50, self.lay, 0, 100),
                               (self.lay.top + self.lay.bottom) / 2)

    def test_y_power_endpoints(self):
        self.assertAlmostEqual(chart.y_power(0, self.lay, 100), self.lay.bottom)
        self.assertAlmostEqual(chart.y_power(100, self.lay, 100), self.lay.top)

    def test_out_of_range_is_clamped(self):
        self.assertGreaterEqual(chart.y_temp(-50, self.lay, 0, 100), self.lay.top)
        self.assertLessEqual(chart.y_temp(500, self.lay, 0, 100), self.lay.bottom)


class TestNiceBounds(unittest.TestCase):
    def test_round_steps(self):
        lo, hi, step = chart.nice_bounds(37, 212)
        self.assertLessEqual(lo, 37)
        self.assertGreaterEqual(hi, 212)
        self.assertGreater(step, 0)

    def test_step_not_absurd(self):
        _, _, step = chart.nice_bounds(0, 1000, target_ticks=5)
        self.assertGreaterEqual(step, 100)

    def test_zero_span_gets_a_step(self):
        lo, hi, step = chart.nice_bounds(50, 50)
        self.assertGreater(hi, lo)
        self.assertGreater(step, 0)


class TestHitTest(unittest.TestCase):
    def setUp(self):
        self.lay = chart.layout(900, 400)
        self.samples = [chart.Sample(float(i), 30, 40, i, 0, 2) for i in range(5)]

    def test_left_right_edges(self):
        self.assertEqual(chart.hit_index(self.lay.left, self.lay, 5), 0)
        self.assertEqual(chart.hit_index(self.lay.right, self.lay, 5), 4)

    def test_middle(self):
        mid = self.lay.left + self.lay.plot_w / 2
        self.assertEqual(chart.hit_index(mid, self.lay, 5), 2)

    def test_outside_returns_none(self):
        self.assertIsNone(chart.hit_index(self.lay.left - 50, self.lay, 5))
        self.assertIsNone(chart.hit_index(self.lay.right + 50, self.lay, 5))

    def test_hit_sample(self):
        gap = self.lay.plot_w / 4
        self.assertEqual(chart.hit_sample(self.lay.left + gap * 3, self.samples,
                                         self.lay).temp, 30)
        self.assertEqual(chart.hit_sample(self.lay.left + gap * 3, self.samples,
                                         self.lay).power, 3)


class TestPowerBar(unittest.TestCase):
    def test_percent_from_watts(self):
        """验收项：44W / 45W ≈ 97%。"""
        frac, _ = chart.power_bar(44 / 45 * 100)
        self.assertAlmostEqual(frac, 0.9777, places=3)

    def test_colors_change_with_power(self):
        low = chart.power_bar(10)[1]
        mid = chart.power_bar(60)[1]
        high = chart.power_bar(95)[1]
        self.assertNotEqual(low, mid)
        self.assertNotEqual(mid, high)

    def test_clamped(self):
        self.assertEqual(chart.power_bar(-5)[0], 0.0)
        self.assertEqual(chart.power_bar(150)[0], 1.0)


class TestReadout(unittest.TestCase):
    def test_rows_contain_every_measured_field(self):
        sample = chart.Sample(time.mktime((2024, 5, 1, 12, 30, 45, 1, 1, -1)),
                              42, 250, 4, 0x03E6, 2, "20:29", "加热", "加热时间")
        rows = dict(chart.readout_rows(sample))
        self.assertEqual(rows["时间"], "12:30:45")
        self.assertEqual(rows["当前温度"], "42 ℃")       # 温度取 payload[0:2]
        self.assertEqual(rows["目标温度"], "250 ℃")
        self.assertEqual(rows["当前功率"], "4 W")         # 功率取 payload[6]
        self.assertEqual(rows["模式"], "加热")
        self.assertNotIn("W2 原始", rows, "原值不进悬浮框")
        self.assertIn("加热时间", rows)

    def test_timer_label_follows_mode(self):
        sleep = chart.Sample(0, 40, 250, 5, 1229, 0, "19:13", "待机", "休眠倒计时")
        self.assertEqual(dict(chart.readout_rows(sleep))["休眠倒计时"], "19:13")

    def test_timer_placeholder_when_missing(self):
        rows = dict(chart.readout_rows(chart.Sample(0, 40, 250, 5, 0x29BF, 2)))
        self.assertEqual(rows["计时"], "--")


class TestViewport(unittest.TestCase):
    def test_keeps_tail(self):
        samples = [chart.Sample(float(i), 30, 40, i, 0, 2) for i in range(10)]
        self.assertEqual(chart.viewport(samples, 3)[-1].power, 9)
        self.assertEqual(len(chart.viewport(samples, 3)), 3)

    def test_zero_means_all(self):
        samples = [chart.Sample(float(i), 30, 40, i, 0, 2) for i in range(4)]
        self.assertEqual(len(chart.viewport(samples, 0)), 4)


if __name__ == "__main__":
    unittest.main()
