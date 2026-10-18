"""P0-A 事件泵：任一处理器抛异常都不能让界面永久假死（静默失效）。

修复前：`_handle_event` 不在 try 内，且「重排自己」写在循环最后一行 —— 任一处理器
抛异常（future 已取消、控件已销毁、logger 写盘失败…）就会让 `_pump_job` 永远为 None，
Tk 又不报错，于是界面还在、按钮还亮、数据再也不动。
"""
import contextlib
import io
import unittest

import atk_hp20 as app_mod


def _app_or_skip(test):
    """需要能创建 Tk 窗口；无显示环境就跳过。"""
    import tkinter as tk
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        test.skipTest(f"没有可用显示: {exc}")
        return None, None
    opts = app_mod.build_parser().parse_args(["--sim"])
    original = app_mod.should_auto_connect
    app_mod.should_auto_connect = lambda _opts: False     # 测试里不要自动连接
    try:
        app = app_mod.Hp20App(root, opts)
    finally:
        app_mod.should_auto_connect = original
    root.update()
    return root, app


class TestPumpSurvivesHandlerError(unittest.TestCase):
    def setUp(self):
        self.root, self.app = _app_or_skip(self)

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app._on_close()
            except Exception:  # noqa: BLE001
                pass

    def _cancel_pending(self) -> None:
        """先撤掉已排的那一拍，保证一轮 _pump 之后只剩一个新 job（否则销毁后会刷 Tk 报错）。"""
        job, self.app._pump_job = self.app._pump_job, None
        if job is not None:
            try:
                self.app.root.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass

    def _pump_capturing_stderr(self) -> str:
        self._cancel_pending()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.app._pump()
        return err.getvalue()

    def test_pump_reschedules_before_handling(self):
        """核心断言：处理器抛异常后 `_pump_job` 必须已被重新排上。"""
        self.app.events.put(("status", None))       # -> _on_status(None) 必抛 AttributeError
        stderr = self._pump_capturing_stderr()
        self.assertIsNotNone(self.app._pump_job, "事件泵没有续命，界面会永久假死")
        self.assertIn("AttributeError", stderr, "异常必须打印出来，不能静默吞掉")

    def test_later_events_still_processed(self):
        """单个事件失败不连坐：同一轮里后面的事件仍然要被处理。"""
        self.app.events.put(("status", None))
        self.app.events.put(("sys", "标记-1"))
        self.app.events.put(("status", None))
        self.app.events.put(("sys", "标记-2"))
        self._pump_capturing_stderr()
        self.assertEqual(self.app.tip_var.get(), "标记-2")
        self.assertEqual(self.app.events.qsize(), 0, "队列应被泵空")

    def test_next_pass_still_runs(self):
        """下一拍照常消费新事件（模拟 after 到期后的再次进入）。"""
        self.app.events.put(("status", None))
        self._pump_capturing_stderr()
        self.app.events.put(("sys", "第二拍"))
        self._pump_capturing_stderr()
        self.assertEqual(self.app.tip_var.get(), "第二拍")
        self.assertIsNotNone(self.app._pump_job)

    def test_chart_redraw_error_keeps_pump_alive(self):
        """图表重绘抛异常同样不能停摆（重排已在它之前完成）。"""
        original = self.app._redraw_chart

        def boom() -> None:
            raise RuntimeError("模拟重绘失败")

        self._cancel_pending()
        self.app._redraw_chart = boom
        self.app._chart_dirty = True
        try:
            with self.assertRaises(RuntimeError):
                self.app._pump()
        finally:
            self.app._redraw_chart = original
        self.assertIsNotNone(self.app._pump_job, "重绘异常后事件泵仍须续命")
        self.assertFalse(self.app._chart_dirty)


if __name__ == "__main__":
    unittest.main()
