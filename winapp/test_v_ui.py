"""V 阶段：用户 8 条 UI 问题/改进（不加热归一化、目标曲线、时间轴、重置默认值、导出图表）。"""
import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

import chart
import chart_export
import hp20_commands as hc


def status(mode=2, state=0x02, supply=140, output=45, target=200, temp=25, w2=1280):
    return hc.parse_realtime(hc.build_realtime_payload(
        current_temp=temp, supply_power_w=supply, output_power_w=output,
        target_temp=target, channel_or_stage=0, mode=mode, w2=w2,
        input_voltage_raw=278, output_state_raw=state))


def sample(ts=0.0, temp=25, target=200, power=0, mode=2, heating=True):
    return chart.Sample(ts=ts, temp=temp, target=target, power=power, w2=1280,
                        mode=mode, heating=heating)


# ------------------------------------------------------------------ 1 不加热判据
class TestIsHeating(unittest.TestCase):
    def test_idle_setting_main_are_not_heating(self):
        for mode in (hc.MODE_IDLE, hc.MODE_SETTING, hc.MODE_MAIN):
            st = status(mode=mode, state=0x02)          # 设备在待机界面也报 0x02
            self.assertFalse(hc.is_heating(st), mode)
            self.assertEqual(st.output_state_text, "待机", mode)

    def test_heating_modes(self):
        for mode in (hc.MODE_REFLOW, hc.MODE_HEATING, hc.MODE_REPAIR):
            self.assertTrue(hc.is_heating(status(mode=mode, state=0x02)), mode)

    def test_state_byte_zero_means_not_heating(self):
        st = status(mode=hc.MODE_HEATING, state=0x00)
        self.assertFalse(hc.is_heating(st))
        self.assertEqual(st.output_state_text, "待机")

    def test_none_is_not_heating(self):
        self.assertFalse(hc.is_heating(None))
        self.assertEqual(hc.effective_power(None), 0)

    def test_state_text_per_mode(self):
        self.assertEqual(status(mode=hc.MODE_HEATING).output_state_text, "加热模式运行中")
        self.assertEqual(status(mode=hc.MODE_REFLOW).output_state_text, "回流焊运行中")
        self.assertEqual(status(mode=hc.MODE_REPAIR).output_state_text, "维修模式运行中")

    def test_raw_byte_kept_for_diagnostics(self):
        st = status(mode=hc.MODE_IDLE, state=0x02)
        self.assertEqual(st.output_state_raw, 0x02)          # 诊断页仍看原值
        self.assertEqual(st.output_state_text, "待机")        # 主界面归一化


# ------------------------------------------------------------------ 2 不加热功率为 0
class TestEffectivePower(unittest.TestCase):
    def test_standby_residual_power_becomes_zero(self):
        st = status(mode=hc.MODE_IDLE, supply=140, output=45)
        self.assertEqual(st.output_power_w, 45)              # 原始值不动
        self.assertEqual(st.effective_power_w, 0)
        self.assertEqual(st.power_percent, 0.0)
        self.assertEqual(st.power_text, "0W（0.0%）")
        self.assertEqual(hc.bar_power_percent(st), 0.0)

    def test_heating_keeps_raw_power(self):
        st = status(mode=hc.MODE_HEATING, supply=140, output=45)
        self.assertEqual(st.effective_power_w, 45)
        self.assertAlmostEqual(st.power_percent, 45 * 100 / 140, places=6)

    def test_csv_writes_zero_when_not_heating(self):
        import csv as _csv
        import logs
        path = os.path.join(tempfile.mkdtemp(), "v.csv")
        rec = logs.CsvRecorder(path)
        rec.record(status(mode=hc.MODE_IDLE, output=45))
        rec.close()
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(_csv.reader(fh))
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual(row["输出功率(W)"], "0")
        self.assertEqual(row["功率占比(%)"], "0.0")
        self.assertEqual(row["输出状态"], "待机")

    def test_target_curve_and_tooltip(self):
        st = status(mode=hc.MODE_IDLE, output=45)
        smp = chart.Sample(ts=0.0, temp=st.current_temp, target=st.target_temp,
                           power=st.effective_power_w, w2=st.w2,
                           mode=st.effective_mode, heating=st.is_heating)
        self.assertEqual(dict(chart.readout_rows(smp))["当前功率"], "0 W")


# ------------------------------------------------------------------ 4 目标曲线
class TestTargetCurveHidden(unittest.TestCase):
    def test_not_heating_excludes_target_from_range(self):
        samples = [sample(temp=25, target=300, heating=False),
                   sample(ts=5, temp=26, target=300, heating=False)]
        axes = chart.dual_axis(samples)
        self.assertEqual(axes.targets, [None, None])
        self.assertLessEqual(axes.temp_hi, 40, "待机时目标 300℃ 不该把量程拉大")

    def test_heating_includes_target(self):
        samples = [sample(temp=25, target=300, heating=True),
                   sample(ts=5, temp=26, target=300, heating=True)]
        axes = chart.dual_axis(samples)
        self.assertEqual(axes.targets, [300.0, 300.0])
        self.assertGreaterEqual(axes.temp_hi, 300.0)

    def test_mixed_series_marks_only_heating(self):
        axes = chart.dual_axis([sample(heating=False), sample(ts=5, heating=True)])
        self.assertIsNone(axes.targets[0])
        self.assertEqual(axes.targets[1], 200.0)


# ------------------------------------------------------------------ 7 时间轴
class TestTimeAxis(unittest.TestCase):
    def lay(self):
        return chart.layout(600, 300)

    def test_ticks_use_mmss_under_a_minute(self):
        samples = [sample(ts=0.0), sample(ts=30.0)]
        ticks = chart.time_ticks(samples, 5)
        self.assertTrue(4 <= len(ticks) <= 6)
        self.assertEqual(ticks[0][0], 0.0)
        self.assertEqual(ticks[-1][0], 1.0)
        self.assertTrue(all(len(label) == 5 for _f, label in ticks), ticks)   # MM:SS

    def test_ticks_use_hhmmss_over_a_minute(self):
        ticks = chart.time_ticks([sample(ts=0.0), sample(ts=120.0)], 5)
        self.assertTrue(all(len(label) == 8 for _f, label in ticks), ticks)   # HH:MM:SS

    def test_tick_count_is_4_to_6(self):
        pair = [sample(ts=0.0), sample(ts=600.0)]
        for n in (1, 3, 5, 9):
            self.assertTrue(4 <= len(chart.time_ticks(pair, n)) <= 6, n)

    def test_too_few_or_same_time(self):
        self.assertEqual(chart.time_ticks([], 5), [])
        self.assertEqual(chart.time_ticks([sample(ts=10.0)], 5), [])
        self.assertEqual(chart.time_ticks([sample(ts=10.0), sample(ts=10.0)], 5), [])

    def test_unsorted_and_duplicate_timestamps(self):
        samples = [sample(ts=30.0), sample(ts=0.0), sample(ts=30.0), sample(ts=15.0)]
        ticks = chart.time_ticks(samples, 4)
        self.assertEqual(ticks[0][0], 0.0)
        self.assertEqual(ticks[-1][0], 1.0)

    def test_x_time_maps_by_timestamp_not_index(self):
        lay = self.lay()
        self.assertAlmostEqual(chart.x_time(100.0, 100.0, 200.0, lay), lay.left)
        self.assertAlmostEqual(chart.x_time(200.0, 100.0, 200.0, lay), lay.right)
        self.assertAlmostEqual(chart.x_time(150.0, 100.0, 200.0, lay),
                               lay.left + lay.plot_w / 2)
        self.assertAlmostEqual(chart.x_time(50.0, 100.0, 200.0, lay), lay.left)   # 越界夹紧

    # ---- V2 补丁：刻度数量自适应 + 标签严格递增不重复
    @staticmethod
    def _assert_strictly_increasing(test, ticks):
        labels = [label for _frac, label in ticks]
        test.assertEqual(len(labels), len(set(labels)), f"刻度标签重复: {labels}")
        for a, b in zip(ticks, ticks[1:]):
            test.assertLess(a[0], b[0], f"刻度位置没递增: {ticks}")

    def test_three_second_span_has_no_duplicate_labels(self):
        """3 秒跨度、20 个样本：以前固定 5 个刻度会撞出两个 28:46。"""
        base = 1_700_000_000.0
        samples = [sample(ts=base + i * 3.0 / 19) for i in range(20)]
        ticks = chart.time_ticks(samples)
        self.assertTrue(2 <= len(ticks) <= 3, ticks)
        self._assert_strictly_increasing(self, ticks)
        self.assertTrue(all("." in label for _f, label in ticks), ticks)   # MM:SS.d

    def test_95_second_span_has_4_to_6_unique_labels(self):
        base = 1_700_000_000.0
        samples = [sample(ts=base + i * 95.0 / 9) for i in range(10)]
        ticks = chart.time_ticks(samples)
        self.assertTrue(4 <= len(ticks) <= 6, ticks)
        self._assert_strictly_increasing(self, ticks)
        self.assertTrue(all(len(label) == 8 for _f, label in ticks), ticks)  # HH:MM:SS

    def test_two_hour_span_uses_hhmmss_and_no_duplicates(self):
        base = 1_700_000_000.0
        samples = [sample(ts=base + i * 7200.0 / 9) for i in range(10)]
        ticks = chart.time_ticks(samples)
        self.assertTrue(4 <= len(ticks) <= 6, ticks)
        self._assert_strictly_increasing(self, ticks)
        self.assertTrue(all(len(label) == 8 for _f, label in ticks), ticks)

    def test_identical_timestamps_degenerate_without_crash(self):
        ticks = chart.time_ticks([sample(ts=5.0), sample(ts=5.0)])
        self.assertLessEqual(len(ticks), 2, ticks)      # 保持 --:-- 行为（返回空）
        self.assertEqual(ticks, [])

    def test_hit_time_picks_nearest_sample(self):
        lay = self.lay()
        samples = [sample(ts=0.0), sample(ts=100.0), sample(ts=200.0)]
        self.assertIs(chart.hit_time(lay.left, lay, samples), samples[0])
        self.assertIs(chart.hit_time(lay.right, lay, samples), samples[2])
        self.assertIs(chart.hit_time(lay.left + lay.plot_w * 0.52, lay, samples), samples[1])
        self.assertIsNone(chart.hit_time(lay.left - 50, lay, samples))


# ------------------------------------------------------------------ 6 重置默认参数
class TestDefaultsAndReset(unittest.TestCase):
    MANUAL_REFLOW = (150, 120, 180, 90, 260, 60, 80, 300)
    MANUAL_REPAIR = (100, 120, 80, 120, 80, 120, 260, 120)

    def test_constants_match_manual(self):
        self.assertEqual(hc.REFLOW_DEFAULTS, self.MANUAL_REFLOW)
        self.assertEqual(hc.REPAIR_DEFAULTS, self.MANUAL_REPAIR)

    def test_defaults_are_within_field_limits(self):
        self.assertTrue(hc.validate_fields(list(hc.REFLOW_DEFAULTS), hc.REFLOW_FIELDS)[0])
        self.assertTrue(hc.validate_fields(list(hc.REPAIR_DEFAULTS), hc.REPAIR_FIELDS)[0])

    def _job(self, written, readback):
        import atk_hp20 as app_mod

        async def write(values):
            written.append(list(values))
            return True

        async def read():
            return readback

        app = app_mod.Hp20App.__new__(app_mod.Hp20App)     # 只借方法，不建窗口
        return app._reset_and_verify(hc.REFLOW_DEFAULTS, hc.REFLOW_FIELDS, write, read, "回流参数")

    def test_reset_writes_and_confirms(self):
        written = []
        import asyncio
        msg = asyncio.run(self._job(written, list(hc.REFLOW_DEFAULTS)))
        self.assertEqual(written, [list(self.MANUAL_REFLOW)], "发出的 8 个值必须是手册出厂值")
        self.assertIn("已重置并读回确认", msg)

    def test_reset_reports_mismatch(self):
        import asyncio
        bad = list(hc.REFLOW_DEFAULTS)
        bad[0] = 999
        msg = asyncio.run(self._job([], bad))
        self.assertIn("读回不一致", msg)
        self.assertIn("999", msg)

    def test_reset_reports_readback_failure(self):
        import asyncio
        msg = asyncio.run(self._job([], None))
        self.assertIn("读回失败", msg)


# ------------------------------------------------------------------ 8 导出图表
class TestChartExport(unittest.TestCase):
    def samples(self):
        return [sample(ts=1000 + i * 5, temp=25 + i, target=60, power=(i * 3 if i > 2 else 0),
                       mode=(2 if i > 2 else 0), heating=(i > 2)) for i in range(10)]

    def test_png_is_at_least_double_size(self):
        path = os.path.join(tempfile.mkdtemp(), "chart.png")
        ok, msg = chart_export.save_chart(path, self.samples(), device_name="HP20-T")
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.getsize(path) > 1000)
        img = self._open(path)
        self.assertEqual(img.size, (1800, 600))
        dpi = img.info.get("dpi") or (0, 0)
        self.assertAlmostEqual(dpi[0], chart_export.DPI, delta=1)
        w, h = chart_export.render_png(
            os.path.join(tempfile.mkdtemp(), "x.png"), self.samples(), "T")
        self.assertGreaterEqual(w, int(chart_export.BASE_W * 2))
        self.assertGreaterEqual(h, int(chart_export.BASE_H * 2))

    # ---- V3 补丁：导出图四边不能被裁
    @staticmethod
    def _open(path):
        from PIL import Image
        return Image.open(path).convert("RGB")

    def _assert_clean_edges(self, path):
        """最外 8 行/列必须全是背景色 —— 有非白像素就说明文字被画布边缘切了。"""
        img = self._open(path)
        w, h = img.size
        for name, box in (("bottom", (0, h - 8, w, h)), ("top", (0, 0, w, 8)),
                          ("right", (w - 8, 0, w, h)), ("left", (0, 0, 8, h))):
            colors = img.crop(box).getcolors(1 << 24) or []
            self.assertEqual(len(colors), 1, f"{name} 边缘被裁: {colors[:5]}")
            self.assertEqual(colors[0][1], (255, 255, 255), f"{name} 边缘不是白色")

    def _samples_span(self, span, n=9):
        base = 1_700_000_000.0
        return [sample(ts=base + i * span / (n - 1), temp=40 + i, target=60, power=45)
                for i in range(n)]

    def test_png_edges_clean_for_short_span(self):
        path = os.path.join(tempfile.mkdtemp(), "short.png")
        ok, msg = chart_export.save_chart(path, self._samples_span(5.0))
        self.assertTrue(ok, msg)
        self._assert_clean_edges(path)

    def test_png_edges_clean_for_two_hour_span(self):
        path = os.path.join(tempfile.mkdtemp(), "long.png")
        ok, msg = chart_export.save_chart(path, self._samples_span(2 * 3600))
        self.assertTrue(ok, msg)
        self._assert_clean_edges(path)

    def test_plot_rect_keeps_room_for_labels(self):
        """绘图区四边都要留出放文字的空间（2x 画布：左/右/下 >=70、上 >=60）。"""
        lay = chart_export.export_layout(1800, 600)
        self.assertGreaterEqual(lay.left, 70)
        self.assertGreaterEqual(1800 - lay.right, 70)
        self.assertGreaterEqual(lay.top, 60)
        self.assertGreaterEqual(600 - lay.bottom, 70)

    def test_svg_is_valid_xml(self):
        path = os.path.join(tempfile.mkdtemp(), "chart.svg")
        ok, msg = chart_export.save_chart(path, self.samples())
        self.assertTrue(ok, msg)
        root = ET.parse(path).getroot()
        self.assertTrue(root.tag.endswith("svg"))
        self.assertGreater(len(list(root)), 3)

    def test_no_data_does_not_write_empty_image(self):
        path = os.path.join(tempfile.mkdtemp(), "empty.png")
        ok, msg = chart_export.save_chart(path, [])
        self.assertFalse(ok)
        self.assertIn("没有数据", msg)
        self.assertFalse(os.path.exists(path))

    def test_bad_path_reports_failure(self):
        ok, msg = chart_export.save_chart(
            os.path.join(tempfile.mkdtemp(), "no_such_dir", "x.png"), self.samples())
        self.assertFalse(ok)
        self.assertIn("保存图表失败", msg)

    def test_pillow_missing_falls_back_to_svg(self):
        saved = chart_export.Image
        chart_export.Image = None
        try:
            path = os.path.join(tempfile.mkdtemp(), "degraded.png")
            ok, msg = chart_export.save_chart(path, self.samples())
            self.assertTrue(ok, msg)
            self.assertIn("SVG", msg)
            self.assertTrue(os.path.exists(os.path.splitext(path)[0] + ".svg"))
        finally:
            chart_export.Image = saved


# ------------------------------------------------------------------ GUI 级（1/2/3/5/8）
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


class GuiCase(unittest.TestCase):
    def setUp(self):
        self.root, self.app = _app_or_skip(self)
        self.app_mod = __import__("atk_hp20")

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app._on_close()
            except Exception:  # noqa: BLE001
                pass


class TestGuiNormalization(GuiCase):
    def test_standby_hides_power_and_target_curve(self):
        standby = status(mode=hc.MODE_IDLE, output=45, target=300)
        for _ in range(4):
            self.app._on_status(standby)
        self.root.update()
        self.app._redraw_chart()
        self.root.update()
        self.assertIn("当前功率 0W", self.app.diag_var.get())
        self.assertEqual(self.app._power_pct, 0.0)
        fills = [self.app.chart.itemcget(i, "fill") for i in self.app.chart.find_all()
                 if self.app.chart.type(i) == "line"]
        self.assertEqual(fills.count(self.app_mod.COLOR_TARGET), 0,
                         "待机时不该画目标温度曲线")

    def test_heating_draws_target_curve(self):
        heating = status(mode=hc.MODE_HEATING, output=45, target=60)
        for _ in range(4):
            self.app._on_status(heating)
        self.root.update()
        self.app._redraw_chart()
        self.root.update()
        fills = [self.app.chart.itemcget(i, "fill") for i in self.app.chart.find_all()
                 if self.app.chart.type(i) == "line"]
        self.assertGreater(fills.count(self.app_mod.COLOR_TARGET), 0,
                           "加热时应画出目标温度曲线")

    def test_timer_label_follows_mode_on_three_pages(self):
        self.app._on_status(status(mode=hc.MODE_IDLE, w2=1280))
        self.assertIn("休眠倒计时", self.app.reflow_var.get())
        self.assertIn("休眠倒计时", self.app.repair_state_var.get())
        self.app._on_status(status(mode=hc.MODE_HEATING, w2=1280))
        self.assertIn("加热时间", self.app.reflow_var.get())
        self.assertIn("加热时间", self.app.repair_state_var.get())


class TestStartHeatingWarning(GuiCase):
    def test_standby_gives_orange_warning(self):
        self.app.device.status = status(mode=hc.MODE_IDLE)
        self.app._on_heating_sent(True)
        tip = self.app.tip_var.get()
        self.assertIn("已下发到设备", tip)
        self.assertIn("可能不会开始加热", tip)
        self.assertEqual(str(self.app.tip_label.cget("foreground")), "#a05a00")

    def test_heating_reports_success_and_resets_colour(self):
        self.app.device.status = status(mode=hc.MODE_HEATING)
        self.app._on_heating_sent(True)
        self.assertEqual(self.app.tip_var.get(), "开始加热: 成功")
        self.assertEqual(str(self.app.tip_label.cget("foreground")), "#555")

    def test_failure_is_reported(self):
        self.app._on_heating_sent(False)
        self.assertIn("失败", self.app.tip_var.get())

    def test_normal_tip_clears_warning_colour(self):
        self.app.device.status = status(mode=hc.MODE_IDLE)
        self.app._on_heating_sent(True)
        self.app.tip_var.set("普通提示")
        self.assertEqual(str(self.app.tip_label.cget("foreground")), "#555")


class TestSaveChartButton(GuiCase):
    def test_no_data_warns_without_dialog(self):
        self.app.samples.clear()
        self.app._on_save_chart()          # 没有数据时不该弹对话框
        self.assertIn("没有数据可保存", self.app.tip_var.get())

    def test_saves_when_data_present(self):
        for _ in range(4):
            self.app._on_status(status(mode=hc.MODE_HEATING))
        path = os.path.join(tempfile.mkdtemp(), "gui.png")
        original = self.app_mod.filedialog.asksaveasfilename
        self.app_mod.filedialog.asksaveasfilename = lambda **_kw: path
        try:
            self.app._on_save_chart()
        finally:
            self.app_mod.filedialog.asksaveasfilename = original     # 恢复原函数，别污染别的用例
        self.assertTrue(os.path.exists(path))
        self.assertIn("已保存 PNG", self.app.tip_var.get())


if __name__ == "__main__":
    unittest.main()
