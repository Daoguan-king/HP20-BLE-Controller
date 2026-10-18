"""日志目录统一：`atk_hp20` 的遮蔽副本已删，落盘位置与 cwd 无关，旧目录有提示。

修复前：`atk_hp20.py` 在 148 行重新定义 `default_logs_dir`（= `os.getcwd()/logs`），
遮蔽了 `logs.py` 的版本（= 程序目录的上级/logs）——改名启动方式就换一个日志目录。
"""
import os
import tempfile
import unittest

import atk_hp20 as app_mod
import logs


class TestSingleImplementation(unittest.TestCase):
    def test_atk_hp20_no_longer_shadows_logs(self):
        self.assertIs(app_mod.default_logs_dir, logs.default_logs_dir,
                      "atk_hp20 里不能再有第二份 default_logs_dir")

    def test_dir_is_program_dir_not_cwd(self):
        """从任意 cwd 启动都落在同一处（双击 / 快捷方式 / 命令行一致）。"""
        expected = logs.default_logs_dir()
        old_cwd = os.getcwd()
        tmp = tempfile.mkdtemp(prefix="hp20_cwd_")
        try:
            os.chdir(tmp)
            self.assertEqual(logs.default_logs_dir(), expected)
        finally:
            os.chdir(old_cwd)
        self.assertTrue(expected.endswith("logs"))
        # 结构断言：logs 与 winapp 包同级的兄弟目录（不写死仓库目录名）
        winapp_dir = os.path.dirname(os.path.abspath(logs.__file__))
        self.assertEqual(os.path.dirname(expected), os.path.dirname(winapp_dir))


class TestFrozenLogsDir(unittest.TestCase):
    """打包成 exe 后（sys.frozen）日志要落在 exe 同级的 logs/，而不是去猜 winapp 在哪。"""

    def test_frozen_uses_exe_directory(self):
        import sys as _sys
        had = hasattr(_sys, "frozen")
        old_frozen = getattr(_sys, "frozen", None)
        old_exe = _sys.executable
        try:
            _sys.frozen = True
            _sys.executable = r"C:\apps\HP20\HP20上位机.exe"
            self.assertEqual(logs.default_logs_dir(), r"C:\apps\HP20\logs")
        finally:
            if had:
                _sys.frozen = old_frozen
            else:
                del _sys.frozen
            _sys.executable = old_exe

    def test_source_mode_unchanged(self):
        self.assertFalse(getattr(__import__("sys"), "frozen", False))
        self.assertTrue(logs.default_logs_dir().endswith("logs"))


class TestLegacyHint(unittest.TestCase):
    def setUp(self):
        self.cwd = tempfile.mkdtemp(prefix="hp20_legacy_")
        self.new_dir = os.path.join(tempfile.mkdtemp(prefix="hp20_new_"), "logs")

    def test_no_old_dir_no_hint(self):
        self.assertIsNone(logs.legacy_logs_hint(self.new_dir, cwd=self.cwd))

    def test_old_dir_without_csv_no_hint(self):
        old = os.path.join(self.cwd, "logs")
        os.makedirs(old)
        open(os.path.join(old, "readme.txt"), "w").close()
        self.assertIsNone(logs.legacy_logs_hint(self.new_dir, cwd=self.cwd))

    def test_old_dir_with_csv_gives_hint(self):
        old = os.path.join(self.cwd, "logs")
        os.makedirs(old)
        open(os.path.join(old, "hp20_curve_20260101_101010.csv"), "w").close()
        hint = logs.legacy_logs_hint(self.new_dir, cwd=self.cwd)
        self.assertIsNotNone(hint)
        self.assertIn(old, hint)
        self.assertIn(self.new_dir, hint)
        self.assertTrue(hint.startswith("发现旧日志目录"))

    def test_same_dir_no_hint(self):
        old = os.path.join(self.cwd, "logs")
        os.makedirs(old)
        open(os.path.join(old, "a.csv"), "w").close()
        self.assertIsNone(logs.legacy_logs_hint(old, cwd=self.cwd))


def _app_or_skip(test, patch_hint=None):
    import tkinter as tk
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        test.skipTest(f"没有可用显示: {exc}")
        return None, None
    opts = app_mod.build_parser().parse_args(["--sim"])
    original_auto = app_mod.should_auto_connect
    original_hint = app_mod.legacy_logs_hint
    app_mod.should_auto_connect = lambda _o: False
    if patch_hint is not None:
        app_mod.legacy_logs_hint = patch_hint
    try:
        app = app_mod.Hp20App(root, opts)
    finally:
        app_mod.should_auto_connect = original_auto
        app_mod.legacy_logs_hint = original_hint
    root.update()
    return root, app


class TestStartupReportsLogDir(unittest.TestCase):
    """验收要求：启动时把「实际使用的日志目录」打进日志/状态栏。"""

    def setUp(self):
        self.root, self.app = _app_or_skip(self)
        self.text = self.app.raw_text.get("1.0", "end") if self.app else ""

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app._on_close()
            except Exception:  # noqa: BLE001
                pass

    def test_startup_logs_real_directory(self):
        self.assertIn("日志目录:", self.text)
        self.assertIn(logs.default_logs_dir(), self.text)


class TestStartupLegacyHint(unittest.TestCase):
    def test_hint_is_logged_when_present(self):
        root, app = _app_or_skip(self, patch_hint=lambda _d: "发现旧日志目录 OLD，新日志写入 NEW")
        try:
            self.assertIsNotNone(app)
            text = app.raw_text.get("1.0", "end")
            self.assertIn("发现旧日志目录 OLD，新日志写入 NEW", text)
        finally:
            if app is not None:
                try:
                    app._on_close()
                except Exception:  # noqa: BLE001
                    pass


if __name__ == "__main__":
    unittest.main()
