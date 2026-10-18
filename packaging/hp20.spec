# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：同一套 winapp 代码出两个 target。

  dist/HP20上位机.exe   windowed（无控制台）—— 双击即用
  dist/hp20-cli.exe     console  —— --probe / --selftest / --sim / --scan-only 能看到输出

构建（在项目根执行）：
  python -m PyInstaller --noconfirm --clean packaging/hp20.spec

说明：
- 代码唯一来源是 winapp/，本文件所在目录只放入口垫片；
- test_*.py 从不被 import，因此不会进包（构建后可查 build/*/Analysis-00.toc 核对）；
- bleak / winrt 全部 collect_all：Windows BLE 的 winrt 投影是延迟加载的 .pyd，
  静态分析收不全，必须显式收集，否则 exe 能启动但扫不到设备。
"""
import os

from PyInstaller.utils.hooks import collect_all

_WINAPP = os.path.abspath(os.path.join(SPECPATH, "..", "winapp"))
_ENTRY_GUI = os.path.join(SPECPATH, "entry_gui.py")
_ENTRY_CLI = os.path.join(SPECPATH, "entry_cli.py")

datas, binaries, hiddenimports = [], [], []
for _pkg in ("bleak", "winrt"):
    _d, _b, _h = collect_all(_pkg)          # 数据 + 二进制(.pyd) + 子模块
    datas += _d
    binaries += _b
    hiddenimports += _h

hiddenimports = list(dict.fromkeys(hiddenimports + [
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.bluetooth.advertisement",
    "winrt.windows.devices.bluetooth.genericattributeprofile",
    "winrt.windows.devices.enumeration",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.storage.streams",
    "winrt.system",
]))

# 测试类与打包工具本身不进包
EXCLUDES = ["test", "unittest", "pytest", "nose2", "pip", "setuptools", "PyInstaller",
            "tkinter.test", "sqlite3"]


def _analysis(entry):
    return Analysis(
        [entry],
        pathex=[_WINAPP],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=EXCLUDES,
        noarchive=False,
        optimize=0,
    )


# ------------------------------------------------------------------ GUI（onefile, windowed）
gui = _analysis(_ENTRY_GUI)
gui_pyz = PYZ(gui.pure, gui.zipped_data, cipher=None)
gui_exe = EXE(
    gui_pyz,
    gui.scripts,
    gui.binaries,
    gui.datas,
    [],
    name="HP20上位机",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                      # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# ------------------------------------------------------------------ CLI（onefile, console）
cli = _analysis(_ENTRY_CLI)
cli_pyz = PYZ(cli.pure, cli.zipped_data, cipher=None)
cli_exe = EXE(
    cli_pyz,
    cli.scripts,
    cli.binaries,
    cli.datas,
    [],
    name="hp20-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
