"""Q 阶段：用户 6 条打磨需求（坐标轴从 0 / 输入校验 / 诊断面板 / 可取消连接 / 中文表头）。"""
import asyncio
import time
import unittest

import chart
import hp20_commands as hc
import logs
from connection import (EVT_CANCELLED, ConnectEvent, ConnectionManager,
                        STATE_CONNECTED, STATE_IDLE)
from hp20_device import Hp20Device
from test_device import FakeTransport


def sample(temp=50, target=60, power=8):
    return chart.Sample(ts=0.0, temp=temp, target=target, power=power,
                        w2=1280, mode=2, timer="20:00", mode_text="加热",
                        timer_label="加热时间")


# ----------------------------------------------------------------- 1 坐标轴从 0
class TestAxisStartsAtZero(unittest.TestCase):
    def test_local_range_still_starts_at_zero(self):
        """用户报的现象：数据只在 44~56 之间时，轴变成 44~56 这种局部区间。"""
        axis = chart.dual_axis([sample(44), sample(50), sample(56, target=56)])
        self.assertEqual(axis.temp_lo, 0.0)
        self.assertEqual(axis.power_lo, 0.0)
        self.assertGreaterEqual(axis.temp_hi, 56.0)

    def test_upper_bound_still_adaptive(self):
        small = chart.dual_axis([sample(60, target=60)])
        big = chart.dual_axis([sample(260, target=300)])
        self.assertEqual(small.temp_lo, 0.0)
        self.assertEqual(big.temp_lo, 0.0)
        self.assertLess(small.temp_hi, big.temp_hi)
        self.assertGreaterEqual(big.temp_hi, 300.0)

    def test_empty_samples(self):
        axis = chart.dual_axis([])
        self.assertEqual((axis.temp_lo, axis.power_lo), (0.0, 0.0))

    def test_power_axis_from_zero(self):
        axis = chart.dual_axis([sample(power=45)])
        self.assertEqual(axis.power_lo, 0.0)
        self.assertGreaterEqual(axis.power_max, 45)


# ----------------------------------------------------------------- 2 温度校验
class TestClampOrRejectTemp(unittest.TestCase):
    def test_valid_range(self):
        import atk_hp20 as app_mod
        for text, value in (("40", 40), ("350", 350), ("60", 60), (" 60 ", 60),
                            ("40.4", 40), ("250.0", 250)):
            ok, got, msg = app_mod.clamp_or_reject_temp(text)
            self.assertTrue(ok, text)
            self.assertEqual(got, value, text)
            self.assertEqual(msg, "")

    def test_out_of_range_and_garbage(self):
        import atk_hp20 as app_mod
        for text in ("39", "351", "-1", "0", "abc", "", "  ", "nan", "inf"):
            ok, got, msg = app_mod.clamp_or_reject_temp(text)
            self.assertFalse(ok, text)
            self.assertIsNone(got, text)
            self.assertIn("40~350", msg, text)


# ----------------------------------------------------------------- 3 字段校验
class TestFieldLimits(unittest.TestCase):
    SPECS = ((hc.REFLOW_FIELDS, [150, 120, 180, 90, 260, 60, 80, 300]),
             (hc.REPAIR_FIELDS, [100, 120, 80, 120, 80, 120, 260, 120]))

    def test_every_field_boundaries(self):
        """每个字段：lo-1 拒 / lo 收 / hi 收 / hi+1 拒（其它字段取合法值）。"""
        for fields, valid in self.SPECS:
            for i, name in enumerate(fields):
                lo, hi, unit = hc.FIELD_LIMITS[name]
                for value, want in ((lo - 1, False), (lo, True),
                                    (hi, True), (hi + 1, False)):
                    values = list(valid)
                    values[i] = value
                    ok, msg = hc.validate_fields(values, fields)
                    self.assertEqual(ok, want, f"{name}={value} {msg}")
                    if not want:
                        self.assertIn(name, msg)
                        self.assertIn(f"{lo:g}~{hi:g}{unit}", msg)

    def test_wrong_length_rejected(self):
        self.assertFalse(hc.validate_fields([1, 2, 3], hc.REFLOW_FIELDS)[0])

    def test_range_text_for_labels(self):
        self.assertEqual(hc.field_range_text("预热温度"), "预热温度 120–180℃")
        self.assertEqual(hc.field_range_text("冷却时间"), "冷却时间 300–480s")


# ----------------------------------------------------------------- 6 CSV 表头
class TestChineseCsvHeader(unittest.TestCase):
    EXPECTED = ("时间", "ISO时间", "当前温度(℃)", "目标温度(℃)", "输出功率(W)", "供电功率(W)",
                "功率占比(%)", "输入电压(0.1V)", "模式", "模式名", "挡位", "输出状态",
                "W2原始", "计时")

    def test_header_is_chinese_and_ordered(self):
        self.assertEqual(logs.CSV_HEADER, self.EXPECTED)
        self.assertEqual(logs.CompactCsvRecorder.header, ("时间", "V(℃)", "A(W)"))
        self.assertEqual(len(logs.CSV_HEADER), 14)

    def test_column_order_unchanged(self):
        """表头只换文字：同一帧写出来的数值顺序与旧表头一一对应。"""
        import csv as _csv
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "full.csv")
        rec = logs.CsvRecorder(path)
        st = hc.parse_realtime(hc.build_realtime_payload(
            current_temp=50, supply_power_w=45, output_power_w=44, target_temp=60,
            channel_or_stage=0, mode=2, w2=1229, input_voltage_raw=271,
            output_state_raw=0x02))
        rec.record(st)
        rec.close()
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(_csv.reader(fh))
        self.assertEqual(tuple(rows[0]), self.EXPECTED)
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual(row["当前温度(℃)"], "50")
        self.assertEqual(row["目标温度(℃)"], "60")
        self.assertEqual(row["输出功率(W)"], "44")
        self.assertEqual(row["供电功率(W)"], "45")
        self.assertEqual(row["功率占比(%)"], "97.8")
        self.assertEqual(row["输入电压(0.1V)"], "271")
        self.assertEqual(row["模式"], "2")
        self.assertEqual(row["模式名"], "加热")
        self.assertEqual(row["挡位"], "0")
        self.assertEqual(row["输出状态"], "加热模式运行中")
        self.assertEqual(row["W2原始"], "1229")
        self.assertEqual(row["计时"], "19:13")


# ----------------------------------------------------------------- 5 取消连接
class HangingTransport(FakeTransport):
    """②直连段永久挂起：模拟「连错设备只能硬等 45s」。"""

    def __init__(self) -> None:
        super().__init__()
        self.hang = True
        self.disconnect_calls = 0
        self.closed = 0

    async def find_by_address(self, address, timeout=10.0):
        return None                    # ① 缓存未命中 -> 进 ②

    async def connect_direct(self, address, timeout=20.0):
        if self.hang:
            await asyncio.Event().wait()      # 永不返回
        await self.connect()

    async def disconnect(self):
        self.disconnect_calls += 1
        await super().disconnect()


class TestCancelConnect(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = HangingTransport()
        self.device = Hp20Device(self.transport, timeout=0.2, tick=0.005,
                                 write_gap=0.0, poll_interval=30.0, handshake_delay=0.0)
        self.logs: list[str] = []
        self.device.on_system = self.logs.append
        self.manager = ConnectionManager(self.transport, self.device,
                                        address="AA:BB:CC:DD:EE:FF",
                                        scan_timeout=0.01, max_retries=1,
                                        total_budget=45.0)
        self.events = []
        self.manager.on_event = self.events.append

    async def asyncTearDown(self):
        self.manager.cancel()
        await self.device.close()

    async def test_cancel_interrupts_hanging_stage(self):
        task = asyncio.ensure_future(self.manager.connect())
        await asyncio.sleep(0.08)                 # 让它进入 ② 并挂住
        self.assertEqual(self.manager.state, "connecting")
        self.manager.cancel()
        ok = await asyncio.wait_for(task, timeout=1.0)     # 1s 内必须结束
        self.assertFalse(ok)
        self.assertEqual(self.manager.state, STATE_IDLE)
        self.assertGreaterEqual(self.transport.disconnect_calls, 1, "取消必须释放 BLE 客户端")
        self.assertTrue(any(e.kind == EVT_CANCELLED for e in self.events))
        cancel_msg = [e for e in self.events if e.kind == EVT_CANCELLED][-1].detail
        self.assertIn("已取消连接", cancel_msg)
        self.assertIn("耗时", cancel_msg)

    async def test_cancel_does_not_trigger_auto_reconnect(self):
        task = asyncio.ensure_future(self.manager.connect())
        await asyncio.sleep(0.08)
        self.manager.cancel()
        await asyncio.wait_for(task, timeout=1.0)
        self.assertTrue(self.manager._closing, "取消后必须抑制自动重连")
        names = [t.get_name() for t in asyncio.all_tasks()]
        self.assertFalse([n for n in names if "reconnect" in n], names)
        self.assertFalse([e for e in self.events if e.kind.startswith("reconnect")],
                         f"取消不该产生重连事件: {[e.kind for e in self.events]}")

    async def test_can_reconnect_immediately_after_cancel(self):
        task = asyncio.ensure_future(self.manager.connect())
        await asyncio.sleep(0.08)
        self.manager.cancel()
        await asyncio.wait_for(task, timeout=1.0)
        self.transport.hang = False
        started = time.monotonic()
        self.assertTrue(await asyncio.wait_for(self.manager.connect(), timeout=2.0),
                        "取消后应立即可以重新连接（不必等 45s 预算）")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(self.manager.state, STATE_CONNECTED)


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

    def capture_submit(self):
        calls = []
        self.app._submit = lambda fn, *a, **k: calls.append((getattr(fn, "__name__", fn), a))
        return calls


class TestTemperatureInputInGui(GuiCase):
    def test_invalid_reverts_and_does_not_send(self):
        calls = self.capture_submit()
        self.app.target_var.set("-1")            # 用户实测：-1 曾被当成 655 发出去
        self.app._on_set_target()
        self.assertEqual(calls, [], "非法温度不能发帧")
        self.assertEqual(self.app.target_var.get(), "60", "要回退到上一有效值")
        self.assertIn("40~350", self.app.tip_var.get())

    def test_valid_remembers_and_sends(self):
        calls = self.capture_submit()
        self.app.target_var.set("250")
        self.app._on_set_target()
        self.assertEqual(calls[0][1][1], 250)
        self.app.target_var.set("abc")           # 回退到刚才那个有效值
        self.app._on_set_target()
        self.assertEqual(self.app.target_var.get(), "250")
        self.assertEqual(len(calls), 1)

    def test_preset_range_enforced(self):
        calls = self.capture_submit()
        self.app.preset_vars[0].set("500")
        self.app._on_write_preset(0)
        self.assertEqual(calls, [])
        self.assertEqual(self.app.preset_vars[0].get(), "--")
        self.assertIn("40~350", self.app.tip_var.get())

    def test_preset_out_of_low_bound(self):
        calls = self.capture_submit()
        self.app.preset_vars[1].set("39")
        self.app._on_write_preset(1)
        self.assertEqual(calls, [])


class TestFieldValidationInGui(GuiCase):
    def test_reflow_whole_command_blocked(self):
        calls = self.capture_submit()
        valid = [150, 120, 180, 90, 260, 60, 80, 300]
        for var, value in zip(self.app.reflow_vars, valid):
            var.set(str(value))
        self.app._on_write_reflow()
        self.assertEqual(len(calls), 1, "全合法要发")
        calls.clear()
        self.app.reflow_vars[6].set("40")        # 冷却温度 50~80℃
        self.app._on_write_reflow()
        self.assertEqual(calls, [], "一个字段越界就整条不发")
        self.assertIn("冷却温度", self.app.tip_var.get())
        self.assertIn("50~80℃", self.app.tip_var.get())

    def test_repair_whole_command_blocked(self):
        calls = self.capture_submit()
        valid = [100, 120, 80, 120, 80, 120, 260, 120]
        for var, value in zip(self.app.repair_vars, valid):
            var.set(str(value))
        self.app._on_write_repair()
        self.assertEqual(len(calls), 1)
        calls.clear()
        self.app.repair_vars[7].set("301")       # 植球时间 10~300s
        self.app._on_write_repair()
        self.assertEqual(calls, [])
        self.assertIn("植球时间", self.app.tip_var.get())

    def test_labels_show_ranges(self):
        texts = []

        def walk(widget):
            try:
                t = widget.cget("text")
                if t:
                    texts.append(str(t))
            except Exception:  # noqa: BLE001
                pass
            for child in widget.winfo_children():
                walk(child)

        walk(self.app.notebook)
        joined = " ".join(texts)
        for expected in ("预热温度 120–180℃", "冷却时间 300–480s",
                         "除胶(Debond)温度 60–150℃", "植球(Reballing)时间 10–300s"):
            self.assertIn(expected, joined)


class TestConnectButtonCancel(GuiCase):
    def test_connecting_shows_cancel_and_is_clickable(self):
        self.app._connecting = True
        self.app._refresh_buttons()
        self.assertEqual(self.app.connect_btn.cget("text"), "取消连接")
        self.assertEqual(str(self.app.connect_btn.cget("state")), "normal")
        self.app._connecting = False
        self.app._refresh_buttons()
        self.assertEqual(self.app.connect_btn.cget("text"), "连接")

    def test_click_while_connecting_requests_cancel_on_io_thread(self):
        queued = []
        self.app.runner.call_soon = lambda fn, *a: queued.append((fn, a))
        self.app._connecting = True
        self.app._on_connect()
        self.assertEqual(len(queued), 1, "连接中点击 = 取消，不能再发起一次连接")
        self.assertEqual(queued[0][0], self.app.conn.cancel)

    def test_cancelled_event_restores_ui(self):
        self.app._connecting = True
        self.app._refresh_buttons()
        self.app._on_conn_event(ConnectEvent(time.time(), "cancelled",
                                            "已取消连接（耗时 0.4s）"))
        self.assertFalse(self.app._connecting)
        self.assertEqual(self.app.connect_btn.cget("text"), "连接")
        self.assertEqual(str(self.app.connect_btn.cget("state")), "normal")
        self.assertIn("已取消连接", self.app.tip_var.get())


if __name__ == "__main__":
    unittest.main()
