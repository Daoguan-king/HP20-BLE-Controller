"""最后一批可靠性/长跑小修（lead 批准清单 #4 #6 #7 #8 #9 #10 + S3 最小版）。

逐条对应：
  #4  `_append_diag` 无上限增长 -> 2000 行裁剪
  #6  TX 计数移到 write() 成功之后（写失败不再记一帧）
  #7  `AsyncRunner.stop()` 取消未完成 task + is_alive 检查 + 重复调用安全
  #8  `CsvRecorder.record` 写盘异常就地吞掉、只提示一次
  #9  CSV 下拉改用内部 key，不再拿中文文案 `startswith("简洁")` 驱动业务分支
  #10 `notebook.select(6)` -> 具名常量 TAB_DIAG
  S3  `FrameLogger.log` 写盘失败不传染事件泵
"""
import asyncio
import os
import tempfile
import unittest

import hp20_commands as hc
import logs
import transport as transport_mod
from hp20_device import Hp20Device
from test_device import FakeTransport


def _status():
    return hc.parse_realtime(hc.build_realtime_payload(
        current_temp=50, supply_power_w=45, output_power_w=44, target_temp=60,
        channel_or_stage=0, mode=2, w2=1280, input_voltage_raw=271, output_state_raw=0x02))


class BoomWriter:
    """每次 writerow 都炸的替身（模拟磁盘满 / 句柄失效）。"""

    def __init__(self) -> None:
        self.calls = 0

    def writerow(self, _row) -> None:
        self.calls += 1
        raise OSError("磁盘满(模拟)")


class TestCsvRecordSwallowsDiskError(unittest.TestCase):
    """#8：CSV 是旁路记录，写盘失败不能打断采集，也不能顺着事件泵把界面拖死。"""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="hp20_csv_"), "x.csv")
        self.rec = logs.CsvRecorder(self.path)
        self.msgs: list[str] = []
        self.rec.on_error = self.msgs.append

    def tearDown(self):
        try:
            self.rec.close()
        except Exception:  # noqa: BLE001
            pass

    def test_boom_writer_does_not_raise_and_reports_once(self):
        self.rec._writer = BoomWriter()
        for _ in range(3):
            self.rec.record(_status())            # 不许抛
        self.assertEqual(self.rec.write_errors, 3)
        self.assertEqual(len(self.msgs), 1, "只提示一次，别每帧刷屏")
        self.assertIn("CSV 写盘失败", self.msgs[0])
        self.assertEqual(self.rec.rows, 0, "写失败不能记行")

    def test_closed_file_does_not_raise(self):
        self.rec._fh.close()
        self.rec.record(_status())
        self.assertEqual(self.rec.write_errors, 1)
        self.assertEqual(len(self.msgs), 1)

    def test_compact_recorder_too(self):
        rec = logs.CompactCsvRecorder(self.path + ".compact.csv")
        msgs: list[str] = []
        rec.on_error = msgs.append
        rec._writer = BoomWriter()
        try:
            rec.record(_status())
            rec.record(_status())
        finally:
            try:
                rec.close()
            except Exception:  # noqa: BLE001
                pass
        self.assertEqual((rec.write_errors, len(msgs)), (2, 1))


class TestFrameLoggerSwallowsDiskError(unittest.TestCase):
    """S3：`_log_sys`/`_append_raw` 在主线程调 logger.log，写失败不能抛回事件泵。"""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="hp20_log_"), "f.log")
        self.lg = logs.FrameLogger(self.path)
        self.msgs: list[str] = []
        self.lg.on_error = self.msgs.append

    def tearDown(self):
        self.lg.close()

    def test_closed_file_keeps_logging_in_memory(self):
        self.lg._fh.close()
        for i in range(3):
            rec = self.lg.log("TX", f"帧{i}")     # 不许抛
            self.assertEqual(rec.direction, "TX")
        self.assertEqual(len(self.lg.records), 3, "内存环形缓冲照旧可用")
        self.assertEqual(self.lg.write_errors, 3)
        self.assertEqual(len(self.msgs), 1)
        self.assertIn("收发日志写盘失败", self.msgs[0])

    def test_save_still_works_after_failure(self):
        self.lg._fh.close()
        self.lg.log("SYS", "写盘失败之后仍然能另存")
        out = os.path.join(os.path.dirname(self.path), "saved.log")
        self.assertEqual(self.lg.save(out), 1)


class TestTxCounterAfterSuccessfulWrite(unittest.TestCase):
    """#6：写失败不能记一帧 TX（否则收发统计这唯一物证会撒谎）。"""

    class FailingWriteTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.fail_write = 0

        async def write(self, data: bytes) -> None:
            if self.fail_write > 0:
                self.fail_write -= 1
                raise transport_mod.TransportError("写失败(模拟)")
            await super().write(data)

    async def _make(self):
        tr = self.FailingWriteTransport()
        dev = Hp20Device(tr, timeout=0.2, tick=0.005, write_gap=0.0, poll_interval=30.0)
        await tr.connect()
        tr.on_data = dev._on_data      # 不 open()（不想起轮询），但要能收到应答
        return tr, dev

    def test_write_failure_does_not_count_tx(self):
        async def run():
            tr, dev = await self._make()
            try:
                before = dev.stats.tx_frames
                tr.fail_write = 1
                with self.assertRaises(transport_mod.TransportError):
                    await dev.set_preheat(0)
                self.assertEqual(dev.stats.tx_frames, before, "写失败仍然记了一帧 TX")
                self.assertTrue(await dev.set_preheat(0))
                self.assertEqual(dev.stats.tx_frames, before + 1, "写成功才记 TX")
            finally:
                await dev.close()

        asyncio.run(run())


class TestAsyncRunnerStop(unittest.TestCase):
    """#7：关窗后 IO 线程不能悬挂；未完成 task 要被取消；重复 stop 安全。"""

    async def _forever(self) -> None:
        await asyncio.sleep(3600)

    def test_stop_ends_thread_and_is_idempotent(self):
        runner = transport_mod.AsyncRunner()
        runner.start()
        thread = runner._thread
        self.assertIsNotNone(thread)
        self.assertTrue(thread.is_alive())

        fut = runner.submit(self._forever)
        runner.stop(timeout=5.0)

        self.assertFalse(thread.is_alive(), "stop() 之后 IO 线程不能还活着")
        self.assertIsNone(runner._thread)
        self.assertTrue(fut.cancelled() or fut.done(), "未完成的 task 应被取消/结束")

        runner.stop()               # 重复调用安全
        runner.stop()
        with self.assertRaises(RuntimeError):
            runner.submit(self._forever)     # 停掉之后不能再提交

    def test_stop_without_start_is_safe(self):
        transport_mod.AsyncRunner().stop()


class TestAppendDiagTrimmed(unittest.TestCase):
    """#4：诊断详情框长跑不能无限长（原来只有 _append_text 裁剪，它没有）。"""

    def setUp(self):
        import tkinter as tk
        import atk_hp20 as app_mod
        try:
            root = tk.Tk()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"没有可用显示: {exc}")
        opts = app_mod.build_parser().parse_args(["--sim"])
        original = app_mod.should_auto_connect
        app_mod.should_auto_connect = lambda _o: False
        try:
            self.app = app_mod.Hp20App(root, opts)
        finally:
            app_mod.should_auto_connect = original
        root.update()
        self.app_mod = app_mod

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app._on_close()
            except Exception:  # noqa: BLE001
                pass

    def _lines(self, widget) -> int:
        return int(widget.index("end-1c").split(".")[0])

    def test_3000_lines_trimmed_to_limit(self):
        for i in range(3000):
            self.app._append_diag(f"事件 {i}")
        self.assertLessEqual(self._lines(self.app.diag_text), self.app_mod.TEXT_MAX_LINES,
                             "诊断详情框超过上限没有裁剪")
        self.assertIn("事件 2999", self.app.diag_text.get("1.0", "end"), "最新一行必须还在")

    def test_raw_text_uses_same_limit(self):
        for i in range(2500):
            self.app._append_text(f"帧 {i}")
        self.assertLessEqual(self._lines(self.app.raw_text), self.app_mod.TEXT_MAX_LINES)
        self.assertIn("帧 2499", self.app.raw_text.get("1.0", "end"))


class TestCsvKindUsesInternalKey(unittest.TestCase):
    """#9：把下拉文案改掉，业务分支必须仍按内部 key 走（不再 startswith("简洁")）。"""

    def setUp(self):
        import tkinter as tk
        import atk_hp20 as app_mod
        try:
            root = tk.Tk()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"没有可用显示: {exc}")
        opts = app_mod.build_parser().parse_args(["--sim"])
        original = app_mod.should_auto_connect
        app_mod.should_auto_connect = lambda _o: False
        try:
            self.app = app_mod.Hp20App(root, opts)
        finally:
            app_mod.should_auto_connect = original
        root.update()
        self.app_mod = app_mod
        self.tmp = tempfile.mkdtemp(prefix="hp20_kind_")

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app._on_close()
            except Exception:  # noqa: BLE001
                pass

    def test_default_labels_unchanged(self):
        self.assertEqual(tuple(self.app.csv_kind_box.cget("values")),
                         self.app_mod.CSV_KIND_LABELS)
        self.assertEqual(self.app.csv_kind_box.current(), 0)
        self.assertEqual(self.app._csv_kind_key(), "full")

    def test_text_change_does_not_change_logic(self):
        box = self.app.csv_kind_box
        box.configure(values=("A 完全不一样", "B 也别提简洁"))
        box.current(1)                    # 文案已换成 "B 也别提简洁"，不再含「简洁」
        self.assertEqual(self.app.csv_kind_box.get(), "B 也别提简洁")
        self.assertEqual(self.app._csv_kind_key(), "compact")

    def test_toggle_creates_compact_recorder_from_index(self):
        box = self.app.csv_kind_box
        box.configure(values=("x", "y"))
        box.current(1)                              # 下标 1 = compact
        self.app.opts.csv = os.path.join(self.tmp, "kind.csv")
        self.app._toggle_csv()
        try:
            self.assertEqual(type(self.app.csv).__name__, "CompactCsvRecorder")
        finally:
            self.app._toggle_csv()


class TestDiagTabConstant(unittest.TestCase):
    """#10：`notebook.select(6)` 换成具名常量。"""

    def test_constant_and_selection(self):
        import tkinter as tk
        import atk_hp20 as app_mod
        self.assertEqual(app_mod.TAB_DIAG, 6)
        try:
            root = tk.Tk()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"没有可用显示: {exc}")
        opts = app_mod.build_parser().parse_args(["--sim"])
        original = app_mod.should_auto_connect
        app_mod.should_auto_connect = lambda _o: False
        try:
            app = app_mod.Hp20App(root, opts)
        finally:
            app_mod.should_auto_connect = original
        try:
            root.update()
            app._show_hint("连接不上时看这里")
            self.assertEqual(app.notebook.index("current"), app_mod.TAB_DIAG)
        finally:
            try:
                app._on_close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    unittest.main()
