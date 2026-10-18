#!/usr/bin/env python3
"""正点原子 HP20 加热台 —— Windows 蓝牙上位机 (bleak + Tkinter)。

    python atk_hp20.py              # 真实 BLE：扫描 → 连接 → 握手 → 轮询
    python atk_hp20.py --sim        # 进程内模拟设备，无硬件跑通全流程
    python atk_hp20.py --selftest   # 非 GUI 端到端自检(模拟设备)
    python atk_hp20.py --smoke 5    # 起窗口跑 5 秒自动关闭并打印结论
    python atk_hp20.py --scan-only  # 只扫描并打印设备表
    python atk_hp20.py --address AA:BB:CC:DD:EE:FF      # 跳过扫描直连
    python atk_hp20.py --name HP20                      # 按名查找

字段语义以真机实测为准（见 hp20_commands.py 顶部）：payload[0:2] 才是当前温度，
payload[3:5] 恒 200 只作诊断；U1 无停止命令，停止按钮一律禁用。

线程模型：bleak 与设备层回调都在 AsyncRunner 的独立 asyncio 线程触发，统一投递到
queue.Queue，由 Tk 的 after() 在主线程消费 —— 界面永不阻塞 BLE IO。
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import os
import queue
import re
import sys
import time
import traceback

import chart
import chart_export
import framediag
import framing
import hp20_commands as hc
from connection import (EVT_STAGE, NOT_BROADCAST_HINT, ConnectEvent,
                        ConnectionManager, pick_unique_candidate)
from hp20_device import DeviceInfo, Hp20Device
from logs import (CsvRecorder, FrameLogger, default_logs_dir, legacy_logs_hint,
                  make_recorder)
from selftest import run_selftest
from sim_device import SimTransport
from transport import AsyncRunner, BleTransport, ScanResult

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    _TK_ERR: str | None = None
except Exception as _exc:  # noqa: BLE001
    tk = None  # type: ignore[assignment]
    filedialog = messagebox = ttk = None  # type: ignore[assignment]
    _TK_ERR = str(_exc)

APP_TITLE = "正点原子 HP20 加热台上位机"
MONO = ("Consolas", 10)
CHART_WINDOW = 400
# U1 已由真机实测解决：没有独立 stop 命令，等价做法是把 T5 目标温度设到低于当前温度。
FOLLOW_THRESHOLD = 0.999      # yview()[1] 到这个值就算"在底部"
TAB_DIAG = 6                  # 连接诊断页在 notebook 里的下标（不要裸写 6）
TAB_DEVICE = 4                # 设备页（诊断快照所在页）
# CSV 下拉项：文案可改，逻辑只认**下标 key**
CSV_KIND_LABELS = ("完整", "简洁")
CSV_KIND_KEYS = ("full", "compact")
TEXT_MAX_LINES = 2000         # 原始帧 / 诊断两个文本框的行数上限（长跑不越长越卡）
DEBUG_MIN_INTERVAL = 0.5      # 诊断面板最多 2 次/秒重写（否则每帧重写会跳滚动位置）


def scroll_at_bottom(yview_end: float, threshold: float = FOLLOW_THRESHOLD) -> bool:
    return float(yview_end) >= threshold


def should_follow(manual_follow: bool, yview_end: float,
                  threshold: float = FOLLOW_THRESHOLD) -> bool:
    """是否跟随最新帧：手动开启 且 视图已在底部（向上滚=看历史=不跟随）。"""
    return bool(manual_follow) and scroll_at_bottom(yview_end, threshold)


POLL_MIN_S, POLL_MAX_S = 0.05, 5.0
GAP_MIN_S, GAP_MAX_S = 0.0, 1.0


def parse_interval(text: str, lo: float, hi: float) -> float | None:
    """把输入框文本转成 [lo, hi] 内的浮点数；非法或越界返回 None。

    用户反馈：手输数值不生效（只有 ▲▼ 生效），所以两条入口（Enter/失焦）都要走这里。
    """
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    if not (lo <= value <= hi):
        return None
    return value


def clamp_or_reject_temp(text) -> tuple[bool, int | None, str]:
    """温度输入校验（纯函数）：返回 (是否合法, 整数值, 中文原因)。

    手册控温范围 40~350℃。负数必须在这一层挡住 —— 以前 -1 会被 pack_i16 打成 0xFFFF，
    设备按无符号显示成 655，用户看到的是「设 -1 变成了 655℃」。
    """
    lo, hi = hc.TEMP_MIN_C, hc.TEMP_MAX_C
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return False, None, f"温度需在 {lo:g}~{hi:g}℃ 之间（请输入数字）"
    if value != value or value in (float("inf"), float("-inf")):
        return False, None, f"温度需在 {lo:g}~{hi:g}℃ 之间（请输入数字）"
    if not (lo <= value <= hi):
        return False, None, f"温度需在 {lo:g}~{hi:g}℃ 之间（当前 {str(text).strip() or '空'}）"
    return True, int(round(value)), ""


def link_button_states(connected: bool, connecting: bool = False) -> tuple[str, str]:
    """返回 (连接按钮, 断开按钮) 的 ttk state。

    断开/失败/扫描完成后「连接」必须可用，否则用户就被锁死了（L 阶段 bug）。
    连接中「连接」按钮**仍然可用**：它的文案变成「取消连接」，点一下就能中断
    （用户反馈：连错设备只能硬等 45s 总预算走完）。
    """
    if connected:
        return "disabled", "normal"
    if connecting:
        return "normal", "disabled"
    return "normal", "disabled"


def connect_button_text(connecting: bool, connected: bool = False) -> str:
    """连接按钮的文案：进行中显示「取消连接」。"""
    return "取消连接" if (connecting and not connected) else "连接"


COLOR_TEMP = "#c0392b"
COLOR_TARGET = "#2c80c0"
COLOR_POWER = "#00BCD4"   # 青色：与温度红线区分（用户反馈橙色太像温度线）


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def should_auto_connect(opts) -> bool:
    """只要给了 --address / --name（或 --sim）就自动连接。

    修 BUG：原先只判 --sim，于是「--address X --smoke 40」与用户带地址启动时
    根本不发起连接（connected=False / samples=0）。
    """
    return bool(getattr(opts, "sim", False) or getattr(opts, "address", None)
                or getattr(opts, "name", None))


def should_smoke_heat(opts) -> bool:
    """模拟设备冒烟时自己开一段加热，输出里就能看到温度/功率变化。"""
    smoke = getattr(opts, "smoke", None)
    return bool(getattr(opts, "sim", False) and smoke and smoke >= 3)


def schedule_startup(opts, schedule, *, auto_connect, smoke_heat, smoke_done) -> None:
    """启动后的排程决策（schedule 可注入替身，便于单测；真机是 root.after）。"""
    if should_auto_connect(opts):
        schedule(150, auto_connect)
    if getattr(opts, "smoke", None):
        if should_smoke_heat(opts):
            schedule(1600, smoke_heat)
        schedule(int(opts.smoke * 1000), smoke_done)


def looks_like_address(text: str) -> bool:
    parts = (text or "").split(":")
    return len(parts) == 6 and all(len(p) == 2 for p in parts)


def make_transport(sim: bool):
    return SimTransport() if sim else BleTransport()


# ====================================================================== 只读探测
async def _wait_first_status(device: Hp20Device, timeout: float = 6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if device.status is not None:
            return device.status
        await asyncio.sleep(0.05)
    return None


async def _probe_async(opts, transport=None, emit=print,
                     status_timeout: float = 6.0) -> int:
    """无 GUI 的真机只读自检：连接 → 识别 → 读四类参数 → 打印一帧状态 → 退出。

    **绝不写**：只会发 0x00(识别) / 0x04 / 0x07 / 0x0C / 0x09 / 0x0A(轮询)。
    退出码：0 = 正常；1 = 有设备但数据/状态异常；2 = 未连上（CI 可当跳过）。
    """
    target = opts.probe
    address = target if looks_like_address(target) else None
    name_hint = None if address else target
    transport = transport if transport is not None else make_transport(opts.sim)
    device = Hp20Device(transport, dev_id=opts.dev_id, family=opts.family,
                        timeout=opts.timeout, tick=hc.TIMEOUT_TICK_S,
                        write_gap=opts.gap, poll_interval=opts.poll, retries=opts.retries)
    rx: list[bytes] = []

    def on_frame(direction: str, data: bytes) -> None:
        if direction == "RX":
            rx.append(bytes(data))

    device.on_frame = on_frame
    manager = ConnectionManager(transport, device, address=address, name_hint=name_hint,
                                max_retries=opts.connect_retries)
    started = time.monotonic()
    emit(f"=== HP20 只读探测：{target} ===")
    try:
        if not await manager.connect():
            # 退出码语义不变：连上了但识别失败/型号不对 = 1（有设备但异常），
            # 链路根本没连上 = 2（CI 可当跳过）。
            if device.open_failure == "not_hp20" and device.info is not None:
                emit(f"识别：{device.info.device_type}  "
                     f"dev_header=0x{device.info.dev_header:04X}")
                emit(f"设备不是 HP20（{device.info.device_type}），停止探测")
                return 1
            if device.open_failure == "no_reply":
                emit("识别：无应答")
                return 1
            emit(f"连接失败：{manager.last_error or '未知原因'}")
            emit(NOT_BROADCAST_HINT)
            emit("退出码 2 = 未连上设备（CI 可当作跳过）")
            return 2
        info = device.info
        if info is None:
            emit("识别：无应答")
            return 1
        emit(f"设备地址：{manager.last_address or target}   "
             f"连接耗时：{time.monotonic() - started:.1f}s")
        emit(f"识别：{info.device_type}  dev_header=0x{info.dev_header:04X}"
             f"  polling={device.polling}")
        if not info.is_hp20:
            emit(f"设备不是 HP20（{info.device_type}），停止探测")
            return 1
        ok = True
        presets = await device.read_presets()
        emit(f"预设 T1..T4：{presets}")
        ok &= presets is not None
        reflow = await device.read_reflow()
        emit(f"回流曲线：{reflow}")
        ok &= reflow is not None
        repair = await device.read_repair()
        emit(f"维修参数：{repair}")
        ok &= repair is not None
        dev_name = await device.read_name()
        emit(f"设备名：{dev_name!r}")
        ok &= dev_name is not None
        status = await _wait_first_status(device, status_timeout)
        if status is None:
            emit(f"状态帧：{status_timeout:g}s 内没收到（轮询未生效？）")
            return 1
        raw = rx[-1] if rx else b""
        emit(f"状态帧：RX {framing.to_hex(raw)} => {status.summary()}")
        emit("（本命令只读：只发 0x00/0x04/0x07/0x0C/0x09/0x0A，不写任何参数）")
        emit(f"总耗时：{time.monotonic() - started:.1f}s")
        return 0 if ok else 1
    finally:
        try:
            await manager.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _probe(opts) -> int:
    return asyncio.run(_probe_async(opts))


# ====================================================================== GUI
class Hp20App:
    def __init__(self, root, opts) -> None:
        self.root = root
        self.opts = opts
        self.events: queue.Queue = queue.Queue()
        self.runner = AsyncRunner()
        self.runner.start()
        self.logger = FrameLogger(opts.log)
        # 写盘失败只提示一次，且走队列投递 —— 绝不能顺着日志调用栈把事件泵带走
        self.logger.on_error = lambda msg: self.events.put(("sys", msg))
        self.transport = make_transport(opts.sim)
        self.device = Hp20Device(self.transport, dev_id=opts.dev_id, family=opts.family,
                                 timeout=opts.timeout, write_gap=opts.gap,
                                 poll_interval=opts.poll, retries=opts.retries)
        self.conn = ConnectionManager(self.transport, self.device,
                                      address=opts.address, name_hint=opts.name,
                                      max_retries=opts.connect_retries)
        self.scan_results: list[ScanResult] = []
        self.samples: collections.deque = collections.deque(maxlen=6000)
        self.csv: CsvRecorder | None = None
        self.connected = False
        self._connecting = False
        self._ticker_job: str | None = None
        self._pump_job: str | None = None
        self.raw_follow = True
        self.identified_ok: bool | None = None
        self.sample_count = 0
        self._power_pct = 0.0
        self._last_status = None
        self.tx_count = 0
        self.rx_count = 0
        self._chart_dirty = False
        # 用户从「原始帧控制台」钉进来的那一帧（方向, 原始帧, 时间）；只由用户操作改变
        self._pinned_frame: tuple[str, bytes, str] | None = None
        self._last_tx: tuple[bytes, str] | None = None    # (原始帧, 时间) —— 发送侧自己的最近一帧
        self._last_rx: tuple[bytes, str] | None = None    # 接收侧同上；两段互不覆盖（否则 TX/RX 抢位置来回闪）
        self._debug_dirty = True
        self._debug_cache = ""
        self._debug_rendered_at = 0.0
        self._debug_timer = None
        self._reconnect_tries = 0
        self._reconnects_ok = 0
        self._cursor_x: float | None = None
        self._hover_job = None
        self._closing = False
        self.preset_vars: dict[int, tk.StringVar] = {}
        self.reflow_vars: list[tk.StringVar] = []
        self.repair_vars: list[tk.StringVar] = []
        self._wire_callbacks()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_job = self.root.after(80, self._pump)
        self._log_sys(f"模式: {self.transport.describe()}；目标设备头 0x"
                      f"{opts.dev_id | (opts.family << 8):04X}")
        logs_dir = default_logs_dir()
        self._log_sys(f"日志目录: {logs_dir}")
        legacy = legacy_logs_hint(logs_dir)
        if legacy:
            self._log_sys(legacy)
        schedule_startup(opts, self.root.after,
                         auto_connect=self._auto_connect,
                         smoke_heat=self._smoke_heat,
                         smoke_done=self._smoke_done)

    # ------------------------------------------------------------ 回调接线
    def _wire_callbacks(self) -> None:
        self.device.on_status = lambda s: self.events.put(("status", s))
        self.device.on_frame = lambda d, b: self.events.put(("frame", (d, b)))
        self.device.on_system = lambda s: self.events.put(("sys", s))
        self.device.on_params = lambda k, v, silent=False: self.events.put(
            ("params", (k, v, silent)))
        self.device.on_identified = lambda i: self.events.put(("identified", i))
        self.conn.on_event = lambda e: self.events.put(("conn_event", e))
        self.transport.on_system = lambda s: self.events.put(("sys", s))

    # ------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        self.root.title(f"{APP_TITLE}  [{self.transport.describe()}]")
        self.root.geometry("1180x880")
        self.root.minsize(1000, 700)      # P 阶段：保证 1000x700 下设备页等信息不截断

        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text="设备:").pack(side="left")
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(top, textvariable=self.device_var, width=44,
                                       state="readonly")
        self.device_box.pack(side="left", padx=(4, 6))
        self.scan_btn = ttk.Button(top, text="扫描", width=7, command=self._on_scan)
        self.scan_btn.pack(side="left")
        self.connect_btn = ttk.Button(top, text="连接", width=7, command=self._on_connect)
        self.connect_btn.pack(side="left", padx=4)
        self.disconnect_btn = ttk.Button(top, text="断开", width=7, state="disabled",
                                        command=self._on_disconnect)
        self.disconnect_btn.pack(side="left")
        self.reconnect_btn = ttk.Button(top, text="重新扫描并连接", width=16,
                                        command=self._on_rescan_connect)
        self.reconnect_btn.pack(side="left", padx=4)
        self.link_var = tk.StringVar(value="未连接")
        ttk.Label(top, textvariable=self.link_var).pack(side="left", padx=10)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self._build_monitor_tab()
        self._build_heat_tab()
        self._build_reflow_tab()
        self._build_repair_tab()
        self._build_device_tab()
        self._build_raw_tab()
        self._build_diag_tab()
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._on_tab_changed())

        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(fill="x")
        self.stats_var = tk.StringVar(value="TX 0 / RX 0")
        ttk.Label(bar, textvariable=self.stats_var).pack(side="left")
        self.rate_var = tk.StringVar(value=f"轮询 {self.opts.poll:.3f}s · 写间隔 {self.opts.gap:.3f}s")
        ttk.Label(bar, textvariable=self.rate_var).pack(side="left", padx=12)
        self.csv_var = tk.StringVar(value="CSV: 未记录")
        ttk.Label(bar, textvariable=self.csv_var).pack(side="left", padx=12)
        self.tip_var = tk.StringVar(value="就绪")
        self._warn_tip = ""
        self.tip_label = ttk.Label(bar, textvariable=self.tip_var, foreground="#555")
        self.tip_label.pack(side="right")
        # 任何非警告文案写进来时自动把颜色恢复成常规灰
        self.tip_var.trace_add("write", lambda *_a: self._on_tip_changed())

    @staticmethod
    def _wrap_label(widget, width: int, pad: int = 24) -> None:
        """按容器宽度设置 wraplength，避免长文本把窗口撑爆或被截断。"""
        try:
            widget.configure(wraplength=max(200, int(width) - pad))
        except Exception:  # noqa: BLE001
            pass

    def _build_monitor_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text=" 实时监控 ")
        self.monitor_tab = tab

        big = ttk.Frame(tab)
        big.pack(fill="x", pady=(0, 4))
        self.cur_var = tk.StringVar(value="--.-")
        self.tgt_var = tk.StringVar(value="--.-")
        self.mode_var = tk.StringVar(value="未连接")
        for col, (title, var, color) in enumerate((
                ("当前温度", self.cur_var, COLOR_TEMP),
                ("目标温度", self.tgt_var, "#2c3e50"),
                ("模式", self.mode_var, "#1f6f3f"))):
            box = ttk.LabelFrame(big, text=title, padding=6)
            box.grid(row=0, column=col, sticky="nsew", padx=4)
            big.columnconfigure(col, weight=1)
            ttk.Label(box, textvariable=var, font=("Consolas", 24, "bold"),
                      foreground=color).pack(anchor="center")

        # 输出功率占比进度条（= 输出功率 / 供电功率，与 power_pct 同一算法）
        bar_row = ttk.Frame(tab)
        bar_row.pack(fill="x", pady=(2, 2))
        self.power_pct_var = tk.StringVar(value="输出功率占比 --%")
        ttk.Label(bar_row, textvariable=self.power_pct_var,
                  foreground="#333", width=22).pack(side="left")
        self.power_bar = tk.Canvas(bar_row, height=14, background="#eeeeee",
                                   highlightthickness=1, highlightbackground="#cccccc")
        self.power_bar.pack(side="left", fill="x", expand=True, padx=(2, 6))
        self.power_bar.bind("<Configure>", lambda _e: self._draw_power_bar())

        self.diag_var = tk.StringVar(
            value="供电 --W   输入 --.-V   当前功率 --W（--）   输出状态 --   加热时间 --")
        ttk.Label(tab, textvariable=self.diag_var, foreground="#555").pack(anchor="w")

        info = ttk.Frame(tab)
        info.pack(fill="x", pady=(2, 0))
        self.chart_info_var = tk.StringVar(value="曲线: 等待数据")
        ttk.Label(info, textvariable=self.chart_info_var, foreground="#555").pack(side="left")
        ttk.Button(info, text="清空曲线", command=self._clear_chart).pack(side="right")
        ttk.Button(info, text="保存图表", width=10,
                   command=self._on_save_chart).pack(side="right", padx=6)
        ttk.Label(info, text="->写间隔(s)").pack(side="right", padx=(10, 2))
        self.gap_var = tk.StringVar(value=f"{self.opts.gap:.3f}")
        gap = ttk.Spinbox(info, from_=GAP_MIN_S, to=GAP_MAX_S, increment=0.005, width=6,
                          textvariable=self.gap_var, command=self._apply_gap)
        gap.pack(side="right")
        gap.bind("<Return>", lambda _e: self._apply_gap())
        gap.bind("<FocusOut>", lambda _e: self._apply_gap())
        ttk.Label(info, text="->轮询间隔(s)").pack(side="right", padx=(10, 2))
        self.poll_var = tk.StringVar(value=f"{self.opts.poll:.3f}")
        poll = ttk.Spinbox(info, from_=POLL_MIN_S, to=POLL_MAX_S, increment=0.025, width=6,
                           textvariable=self.poll_var, command=self._apply_poll)
        poll.pack(side="right")
        # 用户反馈：手输数值不生效（只有 ▲▼ 生效）-> Enter 与失焦都提交
        poll.bind("<Return>", lambda _e: self._apply_poll())
        poll.bind("<FocusOut>", lambda _e: self._apply_poll())
        self.poll_spin, self.gap_spin = poll, gap

        # CSV 记录控件（从原始帧页挪到监控页，靠近图表）
        csv_row = ttk.Frame(tab)
        csv_row.pack(fill="x", pady=(0, 2))
        ttk.Label(csv_row, text="CSV:").pack(side="left")
        self.csv_kind_var = tk.StringVar(value=CSV_KIND_LABELS[0])
        self.csv_kind_box = ttk.Combobox(csv_row, textvariable=self.csv_kind_var, width=6,
                                         state="readonly", values=CSV_KIND_LABELS)
        self.csv_kind_box.current(0)
        self.csv_kind_box.pack(side="left", padx=(2, 6))
        self.csv_btn = ttk.Button(csv_row, text="开始记录", width=10,
                                  command=self._toggle_csv)
        self.csv_btn.pack(side="left")
        ttk.Label(csv_row, text="（完整=全部字段；简洁=对齐手机 APP 的三列）",
                  foreground="#888").pack(side="left", padx=8)

        self.chart = tk.Canvas(tab, background="#fdfdfd", height=360,
                               highlightthickness=1, highlightbackground="#cccccc",
                               cursor="crosshair")
        self.chart.pack(fill="both", expand=True, pady=6)
        self.chart.bind("<Configure>", lambda _e: self._redraw_chart())
        self.chart.bind("<Motion>", self._on_chart_motion)
        self.chart.bind("<Leave>", self._on_chart_leave)

    def _build_heat_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text=" 加热 ")

        t5 = ttk.LabelFrame(tab, text=f"临时温度 T5", padding=8)
        t5.pack(fill="x", pady=(0, 8))
        self.target_var = tk.StringVar(value="60")
        self._temp_last = {"target": "60"}       # 输入非法时回退到上一有效值
        ttk.Entry(t5, textvariable=self.target_var, width=8,
                  font=("Consolas", 16)).pack(side="left")
        ttk.Label(t5, text="℃", font=("Segoe UI", 12)).pack(side="left", padx=(2, 10))
        self.t5_btn = ttk.Button(t5, text="设置目标温度", width=13,
                                command=self._on_set_target)
        self.t5_btn.pack(side="left")

        pre = ttk.LabelFrame(tab, text="预设温度", padding=8)
        pre.pack(fill="x", pady=(0, 8))
        for i, name in enumerate(hc.PRESET_NAMES):
            is_stop_channel = (i == hc.STOP_CHANNEL)
            label = f"{name}（停止加热）:" if is_stop_channel else f"{name}:"
            ttk.Label(pre, text=label).grid(row=0, column=i * 3, sticky="e", padx=(8, 2))
            var = tk.StringVar(value="0" if is_stop_channel else "--")
            self.preset_vars[i] = var
            self._temp_last[f"preset{i}"] = var.get()
            entry = ttk.Entry(pre, textvariable=var, width=7, font=("Consolas", 12))
            entry.grid(row=0, column=i * 3 + 1, padx=2)
            # T4 预设固定 0℃，不参与加热，也不允许改写
            ttk.Button(pre, text="写入", width=6, state="disabled" if is_stop_channel
                       else "normal",
                       command=lambda idx=i: self._on_write_preset(idx)).grid(
                row=0, column=i * 3 + 2, padx=(2, 6))
        ttk.Button(pre, text="读取全部预设", command=self._on_read_presets).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(pre, foreground="#a05a00",
                  text="T4（停止加热）：面板会显示 T3。"
                  ).grid(row=2, column=0, columnspan=13, sticky="w", pady=(4, 0))

        run = ttk.LabelFrame(tab, text="加热挡位切换", padding=8)
        run.pack(fill="x", pady=(0, 8))
        ttk.Label(run, text="挡位:").pack(side="left")
        self.channel_var = tk.IntVar(value=0)
        for ch in range(3):          # 官方 APP 只有 T1/T2/T3；T4 是停止通道
            ttk.Radiobutton(run, text=f"T{ch + 1}", value=ch,
                            variable=self.channel_var,
                            command=self._update_channel_hint).pack(side="left", padx=2)
        self.custom_target_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(run, text="自定义目标", variable=self.custom_target_var,
                        command=self._update_channel_hint).pack(side="left", padx=(10, 2))
        self.channel_hint_var = tk.StringVar(value="T1 预设 --℃")
        ttk.Label(run, textvariable=self.channel_hint_var,
                  foreground="#1f6f3f").pack(side="left", padx=(4, 10))
        self.start_btn = ttk.Button(run, text="开始加热", width=12,
                                    command=self._on_start_heating)
        self.start_btn.pack(side="left", padx=(14, 4))
        self.stop_btn = ttk.Button(run, text="停止加热", width=20,
                                   command=self._on_stop_heating)
        self.stop_btn.pack(side="left", padx=4)

        self.heat_var = tk.StringVar(value="当前: 未连接")
        ttk.Label(tab, textvariable=self.heat_var, font=("Segoe UI", 11)).pack(anchor="w")

    def _build_reflow_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text=" 回流焊 ")

        box = ttk.LabelFrame(tab, text="回流曲线", padding=8)
        box.pack(fill="x")
        stages = ("预热 PH", "恒温 SK", "回流 RF", "冷却 CL")
        for i, stage in enumerate(stages):
            ttk.Label(box, text=f"{stage}：").grid(row=i, column=0, sticky="e", padx=(4, 2))
            # 每个输入框旁直接写出手册范围，用户不用记
            ttk.Label(box, text=hc.field_range_text(hc.REFLOW_FIELDS[i * 2])).grid(
                row=i, column=1, sticky="e")
            temp_var = tk.StringVar(value="--")
            self.reflow_vars.append(temp_var)
            ttk.Entry(box, textvariable=temp_var, width=8, font=MONO).grid(
                row=i, column=2, padx=(2, 12))
            ttk.Label(box, text=hc.field_range_text(hc.REFLOW_FIELDS[i * 2 + 1])).grid(
                row=i, column=3, sticky="e")
            time_var = tk.StringVar(value="--")
            self.reflow_vars.append(time_var)
            ttk.Entry(box, textvariable=time_var, width=8, font=MONO).grid(
                row=i, column=4, padx=(2, 12))

        act = ttk.Frame(tab)
        act.pack(fill="x", pady=8)
        ttk.Button(act, text="写曲线", width=12, command=self._on_write_reflow).pack(side="left")
        ttk.Button(act, text="重置默认参数", width=14,
                   command=self._on_reset_reflow).pack(side="left", padx=6)
        ttk.Button(act, text="读曲线", width=12, command=self._on_read_reflow).pack(
            side="left", padx=6)
        ttk.Button(act, text="停止加热", width=20,
                   command=self._on_stop_heating).pack(side="left", padx=6)
        self.reflow_var = tk.StringVar(value="当前 -- / W2=----")
        ttk.Label(act, textvariable=self.reflow_var,
                  font=("Segoe UI", 11, "bold")).pack(side="left", padx=10)

    def _build_repair_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text=" 维修 ")

        box = ttk.LabelFrame(tab, text="维修参数", padding=8)
        box.pack(fill="x")
        for i, (name, unit) in enumerate(hc.REPAIR_LABELS):
            col = 0 if i % 2 == 0 else 2
            lo, hi, _u = hc.FIELD_LIMITS[hc.REPAIR_FIELDS[i]]
            ttk.Label(box, text=f"{name} {lo:g}–{hi:g}{unit}：").grid(
                row=i // 2, column=col, sticky="e", padx=(4, 2))
            var = tk.StringVar(value="--")
            self.repair_vars.append(var)
            ttk.Entry(box, textvariable=var, width=8, font=MONO).grid(
                row=i // 2, column=col + 1, padx=(2, 12))

        act = ttk.Frame(tab)
        act.pack(fill="x", pady=8)
        ttk.Button(act, text="写参数", width=12, command=self._on_write_repair).pack(side="left")
        ttk.Button(act, text="重置默认参数", width=14,
                   command=self._on_reset_repair).pack(side="left", padx=6)
        ttk.Button(act, text="读参数", width=12, command=self._on_read_repair).pack(
            side="left", padx=6)
        ttk.Button(act, text="停止加热", width=20,
                   command=self._on_stop_heating).pack(side="left", padx=6)
        self.repair_state_var = tk.StringVar(value="当前 --")
        ttk.Label(act, textvariable=self.repair_state_var,
                  font=("Segoe UI", 11, "bold")).pack(side="left", padx=10)

    def _build_device_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text=" 设备 ")
        self.device_tab = tab

        info = ttk.LabelFrame(tab, text="识别结果", padding=8)
        info.pack(fill="x")
        # grid + weight：窄窗口时不会把内容挤出可视区（P 阶段 bug：设备头行右侧被截断）
        info.columnconfigure(0, weight=1)
        self.ident_var = tk.StringVar(value="未识别")
        ttk.Label(info, textvariable=self.ident_var, font=("Segoe UI", 12, "bold"),
                  justify="left").grid(row=0, column=0, sticky="ew")
        self.ident_detail_var = tk.StringVar(value="设备头 --；GATT 特征 --")
        self.ident_detail_label = ttk.Label(info, textvariable=self.ident_detail_var,
                                            foreground="#555", justify="left")
        self.ident_detail_label.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        ttk.Button(info, text="重新识别", command=self._on_identify).grid(
            row=2, column=0, sticky="w", pady=(6, 0))
        info.bind("<Configure>",
                  lambda e: self._wrap_label(self.ident_detail_label, e.width))

        name = ttk.LabelFrame(tab, text="蓝牙名（固定 20 字节 ASCII）",padding=8)
        name.pack(fill="x", pady=8)
        self.name_var = tk.StringVar(value="")
        ttk.Entry(name, textvariable=self.name_var, width=26, font=MONO).pack(side="left")
        ttk.Button(name, text="读取", width=8, command=self._on_read_name).pack(side="left", padx=6)
        ttk.Button(name, text="写入", width=8, command=self._on_write_name).pack(side="left")

        detail = ttk.LabelFrame(tab, text="诊断详情", padding=8)
        detail.pack(fill="both", expand=True)
        detail.rowconfigure(1, weight=1)
        detail.columnconfigure(0, weight=1)
        dbar = ttk.Frame(detail)
        dbar.grid(row=0, column=0, sticky="ew")
        ttk.Button(dbar, text="复制全部", width=10,
                   command=self._copy_debug).pack(side="left")
        ttk.Button(dbar, text="刷新一次", width=10,
                   command=lambda: self._refresh_debug_panel(force=True)).pack(
            side="left", padx=6)
        self._debug_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(dbar, text="自动刷新", variable=self._debug_auto,
                        command=self._on_debug_auto_toggle).pack(side="left", padx=(2, 6))
        ttk.Button(dbar, text="清空", width=8,
                   command=self._clear_debug).pack(side="left")
        self.clear_pin_btn = ttk.Button(dbar, text="清除选择", width=10, state="disabled",
                                       command=self._on_clear_pin)
        self.clear_pin_btn.pack(side="left", padx=6)
        ttk.Label(dbar, foreground="#888",
                  text="只读快照：最近发送/接收两帧 + 计数；另可在「原始帧控制台」"
                       "选中一条 →「解析选中帧」钉成第三段"
                  ).pack(side="left", padx=6)

        dbox = ttk.Frame(detail)
        dbox.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        dbox.rowconfigure(0, weight=1)
        dbox.columnconfigure(0, weight=1)
        self.debug_text = tk.Text(dbox, font=MONO, wrap="none", height=12,
                                  background="#fbfbfb", state="disabled")
        self.debug_text.grid(row=0, column=0, sticky="nsew")
        dscroll = ttk.Scrollbar(dbox, orient="vertical", command=self.debug_text.yview)
        dscroll.grid(row=0, column=1, sticky="ns")
        self.debug_text.configure(yscrollcommand=dscroll.set)

    def _build_raw_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text=" 原始帧控制台 ")
        self.raw_tab = tab

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, text="HEX:").pack(side="left")
        self.hex_var = tk.StringVar(value=framing.to_hex(hc.frame_realtime()))
        entry = ttk.Entry(row, textvariable=self.hex_var, font=MONO)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self._on_send_hex())
        ttk.Button(row, text="发送", width=8, command=self._on_send_hex).pack(side="left")
        ttk.Button(row, text="组帧发送", width=10,
                   command=self._on_build_send).pack(side="left", padx=4)

        actions = ttk.Frame(tab)
        actions.pack(fill="x", pady=4)
        ttk.Button(actions, text="清空日志", command=self._clear_raw).pack(side="left")
        ttk.Button(actions, text="保存日志", command=self._save_log).pack(side="left", padx=4)
        self.follow_btn = ttk.Button(actions, text="暂停自动滚动", width=14,
                                     command=self._toggle_follow)
        self.follow_btn.pack(side="left", padx=(12, 4))
        self.parse_sel_btn = ttk.Button(actions, text="解析选中帧", width=12,
                                        state="disabled", command=self._on_parse_selected_raw)
        self.parse_sel_btn.pack(side="left", padx=(12, 4))
        self.follow_hint_var = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.follow_hint_var,
                  foreground="#a05a00").pack(side="left")

        box = ttk.Frame(tab)
        box.pack(fill="both", expand=True, pady=(6, 0))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.raw_text = tk.Text(box, font=MONO, wrap="none", height=18,
                                background="#101418", foreground="#d6e2e8")
        self.raw_text.grid(row=0, column=0, sticky="nsew")
        vbar = ttk.Scrollbar(box, orient="vertical", command=self.raw_text.yview)
        vbar.grid(row=0, column=1, sticky="ns")
        hbar = ttk.Scrollbar(box, orient="horizontal", command=self.raw_text.xview)
        hbar.grid(row=1, column=0, sticky="ew")
        self.raw_text.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        # 用户向上滚（看历史）时自动暂停跟随
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Prior>", "<Next>"):
            self.raw_text.bind(seq, lambda _e: self.root.after(
                12, self._sync_follow_from_view), add="+")
        # 单击=选中整行（好挑一条完整帧），双击=直接解析；选中变化时刷新按钮可用性
        self.raw_text.bind("<ButtonRelease-1>", self._on_raw_click)
        self.raw_text.bind("<Double-Button-1>", self._on_raw_double_click)
        self.raw_text.bind("<KeyRelease>", lambda _e: self._update_parse_button_state())

    def _build_diag_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text=" 连接诊断 ")

        head = ttk.Frame(tab)
        head.pack(fill="x")
        self.conn_state_var = tk.StringVar(value="未连接")
        ttk.Label(head, textvariable=self.conn_state_var,
                  font=("Segoe UI", 12, "bold")).pack(side="left")
        ttk.Button(head, text="重新扫描并连接", width=16,
                   command=self._on_rescan_connect).pack(side="right")
        ttk.Button(head, text="取消重连", width=10,
                   command=self._on_cancel_reconnect).pack(side="right", padx=6)

        self.hint_var = tk.StringVar(
            value="连接不上时看这里：设备未广播最常见原因是正被手机 APP 占用，或已休眠/断电。")
        ttk.Label(tab, textvariable=self.hint_var, foreground="#a05a00",
                  wraplength=980, justify="left").pack(anchor="w", pady=(6, 6))

        box = ttk.Frame(tab)
        box.pack(fill="both", expand=True)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.diag_text = tk.Text(box, font=MONO, wrap="none", height=16,
                                 background="#fbfbfb")
        self.diag_text.grid(row=0, column=0, sticky="nsew")
        vbar = ttk.Scrollbar(box, orient="vertical", command=self.diag_text.yview)
        vbar.grid(row=0, column=1, sticky="ns")
        self.diag_text.configure(yscrollcommand=vbar.set)

    # ------------------------------------------------------------ 事件泵
    def _pump(self) -> None:
        self._pump_job = None
        if self._closing:
            return
        # 先续命再干活：Tk 的 after 回调抛异常不会中断 mainloop，重排一旦放在循环后面，
        # 任一处理器抛异常就等于事件泵永久停摆 —— 界面还在、按钮还亮、数据再不动、也没报错。
        self._pump_job = self.root.after(80, self._pump)
        for _ in range(400):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_event(kind, payload)
            except Exception:  # noqa: BLE001  单个事件失败不连坐：打印后继续泵后面的
                traceback.print_exc()
        if self._chart_dirty:
            self._chart_dirty = False
            self._redraw_chart()
        if self._debug_dirty:
            self._refresh_debug_panel()

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "status":
            self._on_status(payload)
            self._debug_dirty = True
        elif kind == "frame":
            direction, data = payload
            self._append_raw(direction, data)
            self._note_debug_frame(direction, data)
            if direction == "TX":
                self.tx_count += 1
            else:
                self.rx_count += 1
            self.stats_var.set(f"TX {self.tx_count} / RX {self.rx_count}")
        elif kind == "params":
            self._apply_params(*payload)
        elif kind == "identified":
            self._apply_identified(payload)
        elif kind == "conn_event":
            self._on_conn_event(payload)
        elif kind == "sys":
            self._log_sys(payload)
        elif kind == "error":
            self.tip_var.set(f"错误: {payload}")
            self._log_sys(f"错误: {payload}")
        elif kind == "scan_done":
            self._apply_scan(payload)
        elif kind == "connect_done":
            self._connecting = False
            self._set_link(bool(payload), self.conn.state_text())
        elif kind == "disconnected":
            self._connecting = False
            self._set_link(False, self.conn.state_text())

    def _on_status(self, s: hc.RealtimeStatus) -> None:
        self.sample_count += 1
        self.cur_var.set(f"{s.current_temp} {s.unit}")
        self.tgt_var.set(f"{s.target_temp} {s.unit}")
        self.mode_var.set(f"{s.mode_name} · {s.slot_text}")
        self.diag_var.set(
            f"供电 {s.supply_text}   输入 {s.voltage_text}   "
            f"当前功率 {s.power_text}   输出状态 {s.output_state_text}   "
            f"{s.timer_label} {s.timer_text}")
        self.heat_var.set(f"当前 {s.current_temp}{s.unit} → 目标 {s.target_temp}{s.unit}，"
                          f"{s.mode_name}·{s.slot_text}，功率 {s.power_text}，"
                          f"供电 {s.supply_text} / 输入 {s.voltage_text}")
        self.reflow_var.set(f"当前 {s.mode_name}·{s.slot_text} / "
                            f"{s.timer_label} {s.timer_text}")
        self.repair_state_var.set(f"当前 {s.mode_name}·{s.slot_text} / "
                                  f"{s.timer_label} {s.timer_text}")
        self._last_status = s
        pct = hc.bar_power_percent(s)      # 每帧重算；待机帧按 0%（不保留上一帧的值）
        self._power_pct = pct
        self.power_pct_var.set(f"输出功率占比 {pct:.0f}%")
        self._draw_power_bar()
        self.samples.append(chart.Sample(ts=time.time(), temp=s.current_temp,
                                        target=s.target_temp,
                                        power=s.effective_power_w,      # 不加热记 0
                                        w2=s.w2, mode=s.effective_mode,
                                        timer=s.timer_text,
                                        mode_text=s.mode_name,
                                        timer_label=s.timer_label,
                                        heating=s.is_heating))
        self._chart_dirty = True
        if self.csv is not None:
            self.csv.record(s)
            self.csv_var.set(f"CSV: {os.path.basename(self.csv.path)} ({self.csv.rows} 行)")

    def _apply_params(self, kind: str, values, silent: bool = False) -> None:
        # silent=True：内部补读，只更新控件，不写用户可见提示（否则每秒弹一次提示会把界面拖卡）
        if kind == "presets":
            for i, v in enumerate(values[:4]):
                self.preset_vars[i].set(str(v))
                self._temp_last[f"preset{i}"] = str(v)
            self._update_channel_hint()
            if not silent:
                self.tip_var.set(f"预设温度: {values}")
        elif kind == "reflow":
            for i, v in enumerate(values[:8]):
                self.reflow_vars[i].set(str(v))
            if not silent:
                self.tip_var.set("回流曲线已读回")
        elif kind == "repair":
            for i, v in enumerate(values[:8]):
                self.repair_vars[i].set(str(v))
            if not silent:
                self.tip_var.set("维修参数已读回")
        elif kind == "name":
            self.name_var.set(str(values))

    def _apply_identified(self, info: DeviceInfo) -> None:
        self.identified_ok = info.is_hp20
        self.ident_var.set(f"{info.device_type}  (0x{info.dev_header:04X})"
                           + ("" if info.is_hp20 else "  —— 非 HP20！"))
        chars = "；".join(self.transport.char_summary[:3]) or "--"
        self.ident_detail_var.set(f"设备头 0x{info.dev_header:04X}；{chars}")
        if not info.is_hp20:
            self.tip_var.set(f"设备 {info.device_type} 不是 HP20，已停止发送命令")
            self._log_sys(f"非 HP20 设备（0x{info.dev_header:04X}），不再自动发命令")
        else:
            self.tip_var.set(f"识别成功: {info.device_type}")
            # 预设要用于「挡位回显」与「自定义目标是否等于预设」，连上就主动读一次
            self._submit(self.device.read_presets, label="读预设温度")

    def _on_conn_event(self, event: ConnectEvent) -> None:
        self.conn_state_var.set(self.conn.state_text())
        self._append_diag(event.format())
        if event.kind == EVT_STAGE:
            # 三段式切换立刻反映到状态栏，避免用户以为卡死
            self._connecting = True
            self._set_link(False, f"连接中 · {event.detail}")
        if event.detail:
            self.tip_var.set(f"{event.kind}: {event.detail}")
        if event.kind in ("not_found", "give_up"):
            self._show_hint(NOT_BROADCAST_HINT if event.kind == "not_found" else
                            (self.conn.last_error or NOT_BROADCAST_HINT))
        if event.kind == "reconnect_start":
            self._connecting = True
            self._set_link(False, self.conn.state_text())
        if event.kind in ("connect_ok", "reconnect_ok"):
            self._set_link(True, self.conn.state_text())
        if event.kind == "reconnect_start":
            self._reconnect_tries += 1
        if event.kind == "reconnect_ok":
            self._reconnects_ok += 1
        if event.kind in ("disconnected", "give_up", "connect_ok", "reconnect_ok", "cancelled"):
            self._stop_conn_ticker()
        if event.kind in ("disconnected", "give_up", "cancelled"):
            # 断开/放弃/取消后必须把「连接」恢复可用（L 阶段 bug：断开后按钮一直灰着）
            self._connecting = False
            self._set_link(False, self.conn.state_text())
        if event.kind == "cancelled":
            self.tip_var.set(event.detail)      # 「已取消连接（耗时 Xs）」
            self._debug_dirty = True
    def _append_diag(self, line: str) -> None:
        self.diag_text.insert("end", line + "\n")
        self._trim_text(self.diag_text)
        self.diag_text.see("end")

    @staticmethod
    def _trim_text(widget) -> None:
        """超过 TEXT_MAX_LINES 就砍掉最旧的 500 行（长跑几小时后界面会越来越卡）。"""
        lines = int(widget.index("end-1c").split(".")[0])
        if lines > TEXT_MAX_LINES:
            widget.delete("1.0", "500.0")

    def _set_tip(self, text: str, warn: bool = False) -> None:
        """写状态栏；warn=True 用橙色，提醒「命令发了但不一定生效」这类情况。"""
        self._warn_tip = text if warn else ""
        if warn:
            try:
                self.tip_label.configure(foreground="#a05a00")
            except Exception:  # noqa: BLE001
                pass
        self.tip_var.set(text)

    def _on_tip_changed(self) -> None:
        if self.tip_var.get() != self._warn_tip:
            try:
                self.tip_label.configure(foreground="#555")
            except Exception:  # noqa: BLE001
                pass

    def _show_hint(self, text: str) -> None:
        self.hint_var.set(text)
        self.tip_var.set(text)
        self.notebook.select(TAB_DIAG)

    def _set_link(self, up: bool, text: str) -> None:
        self.connected = up
        if up:
            self._connecting = False
        else:
            self._reset_power_display()     # 没有数据就不该留着上一次的占比
        self.link_var.set(text)
        self._refresh_buttons()

    def _reset_power_display(self) -> None:
        """进度条归零（无数据/断开时用），避免把上一帧的占比留在界面上。"""
        self._power_pct = 0.0
        self.power_pct_var.set("输出功率占比 0%")
        self._draw_power_bar()

    def _start_conn_ticker(self) -> None:
        """连接/重连等待期间每秒刷新一次「②直接连接 … 12s」，让用户看到在动。"""
        if self._ticker_job is not None:
            return

        def tick() -> None:
            self._ticker_job = None
            if not self._connecting:
                return
            self.link_var.set(self.conn.progress_text())
            self._ticker_job = self.root.after(1000, tick)

        self._ticker_job = self.root.after(1000, tick)

    def _stop_conn_ticker(self) -> None:
        job, self._ticker_job = self._ticker_job, None
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass

    def _refresh_buttons(self) -> None:
        connecting = getattr(self, "_connecting", False)
        connect_state, disconnect_state = link_button_states(self.connected, connecting)
        self.connect_btn.configure(
            state=connect_state,
            text=connect_button_text(connecting, self.connected))
        self.disconnect_btn.configure(state=disconnect_state)
        if self._connecting and not self.connected:
            self._start_conn_ticker()
        else:
            self._stop_conn_ticker()

    def _log_sys(self, text: str) -> None:
        rec = self.logger.log("SYS", text)
        self._append_text(f"{rec.ts} SYS {text}")
        self.tip_var.set(text)

    def _append_raw(self, direction: str, data: bytes) -> None:
        rec = self.logger.log(direction, framing.to_hex(data))
        self._append_text(f"{rec.ts} {direction} {framing.to_hex(data)}")

    # ------------------------------------------------------------ 诊断快照（只读）
    def _note_debug_frame(self, direction: str, data: bytes) -> None:
        """按方向各记一帧（互不覆盖），等节流后再重写面板。

        轮询是 TX/RX 成对出现：只留「最近一帧」会让 TX 与 RX 交替占位、面板来回闪。
        """
        if direction == "TX":
            self._last_tx = (bytes(data), _ts())
        else:
            self._last_rx = (bytes(data), _ts())
        self._debug_dirty = True

    def _counts_text(self) -> str:
        st = self.device.stats
        return (f"TX {st.tx_frames} / RX {st.rx_frames} / 超时 {st.timeouts} / "
                f"重试 {st.retries} / 忙拒 {st.busy_rejects} / 空应答 {st.empty_acks} / "
                f"丢帧 {self.device.dropped_frames} / "
                f"重连 {self._reconnects_ok}/{self._reconnect_tries}（成功/尝试）")

    @staticmethod
    def _debug_block(title: str, where: str, item, placeholder: str) -> list[str]:
        """一段诊断（发送或接收）：标题带方向与时刻；没有数据时用占位文案，不留空白。"""
        if item is None:
            return [f"{title} ({where}  {placeholder})"]
        raw, stamp = item
        return [f"{title} ({where}  {stamp})"] + framediag.render(raw)

    def _pinned_lines(self) -> list[str]:
        """第三段：用户钉住的那一帧（只由用户操作改变，轮询/清空都不影响）。"""
        item = self._pinned_frame
        if item is None:
            return []
        direction, raw, stamp = item
        where = "TX→设备" if direction == "TX" else "RX←设备"
        title = f"选中的帧（来自原始帧控制台  {where}  {stamp}）"
        if len(raw) < 6:
            return [title, "  该记录不是完整帧"]
        return [title] + framediag.render(raw)

    def _debug_snapshot(self) -> str:
        """诊断快照全文：选中的帧（若有）+ 最近发送 + 最近接收 + 计数。"""
        lines: list[str] = []
        pinned = self._pinned_lines()
        if pinned:
            lines += pinned + [""]
        lines += self._debug_block("最近发送", "TX→设备", self._last_tx, "还没有发送过帧")
        lines.append("")
        lines += self._debug_block("最近接收", "RX←设备", self._last_rx, "还没有收到过帧")
        lines += ["", "计数", f"  {self._counts_text()}"]
        return "\n".join(lines)

    def _debug_visible(self) -> bool:
        """面板所在页是不是当前页（不可见时不重写，切回来再刷）。"""
        tab = getattr(self, "device_tab", None)
        if tab is None:
            return False
        try:
            return self.notebook.select() == str(tab)
        except Exception:  # noqa: BLE001
            return False

    def _refresh_debug_panel(self, force: bool = False) -> None:
        """重写面板：2 次/秒节流 + 只读 + 保滚动位置 + 不可见不刷。"""
        widget = getattr(self, "debug_text", None)
        if widget is None:
            return
        if not force and not self._debug_auto.get():
            return                       # 自动刷新关掉：等用户点「刷新一次」
        if not force and not self._debug_visible():
            return                       # 不在这一页：保持 dirty，切回来再刷
        if not force:
            wait = DEBUG_MIN_INTERVAL - (time.monotonic() - self._debug_rendered_at)
            if wait > 0:
                # 节流窗口内先不动，安排一次补刷，保证最后一帧最终一定显示出来
                if self._debug_timer is None:
                    self._debug_timer = self.root.after(
                        int(wait * 1000) + 5, self._on_debug_timer)
                return
        self._debug_dirty = False
        self._debug_rendered_at = time.monotonic()
        text = self._debug_snapshot()
        if text == self._debug_cache and not force:
            return
        self._debug_cache = text
        try:
            yview = widget.yview()
        except Exception:  # noqa: BLE001
            yview = None
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")
        if yview is not None:
            try:
                widget.yview_moveto(yview[0])     # 用户正在看历史时不要跳
            except Exception:  # noqa: BLE001
                pass

    def _on_tab_changed(self) -> None:
        """切回设备页时把积压的那一帧补刷出来（不可见期间是不重写的）。"""
        if getattr(self, "_debug_dirty", False) and self._debug_visible():
            self._refresh_debug_panel(force=True)

    def _on_debug_timer(self) -> None:
        self._debug_timer = None
        if self._debug_dirty:
            self._refresh_debug_panel()

    def _on_debug_auto_toggle(self) -> None:
        if self._debug_auto.get():
            self._refresh_debug_panel(force=True)

    def _copy_debug(self) -> None:
        text = self._debug_snapshot()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception as exc:  # noqa: BLE001
            self.tip_var.set(f"复制失败：{exc}")
            return
        self.tip_var.set(f"已复制诊断快照（{len(text)} 字符）")

    def _clear_debug(self) -> None:
        self._last_tx = None
        self._last_rx = None
        self._debug_cache = ""
        self._debug_dirty = True
        self._refresh_debug_panel(force=True)

    # ------------------------------------------------------------ 自动滚动
    def _raw_yview_end(self) -> float:
        try:
            return float(self.raw_text.yview()[1])
        except Exception:  # noqa: BLE001
            return 1.0

    def _update_follow_ui(self) -> None:
        follow = self.raw_follow
        self.follow_btn.configure(text="暂停自动滚动" if follow else "恢复自动滚动")
        self.follow_hint_var.set("" if follow else "已暂停")

    def _toggle_follow(self) -> None:
        self.raw_follow = not self.raw_follow
        if self.raw_follow:
            self.raw_text.see("end")
        self._update_follow_ui()

    def _sync_follow_from_view(self) -> None:
        """滚动后回调：一旦离开底部就自动暂停跟随。"""
        if should_follow(self.raw_follow, self._raw_yview_end()):
            return
        if self.raw_follow:
            self.raw_follow = False
            self._update_follow_ui()

    def _append_text(self, line: str) -> None:
        self.raw_text.insert("end", line + "\n")
        self._trim_text(self.raw_text)
        # 跟随状态下无条件滚到底；"向上滚 -> 暂停"由用户滚动绑定触发，
        # 不能拿 yview 当条件（控件还没显示时 yview 会停在顶部，导致看似跟随其实不动）。
        if self.raw_follow:
            self.raw_text.see("end")

    # ------------------------------------------------------------ 曲线
    def _draw_power_bar(self) -> None:
        canvas = getattr(self, "power_bar", None)
        if canvas is None:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 40)
        height = max(canvas.winfo_height(), 10)
        frac, color = chart.power_bar(getattr(self, "_power_pct", 0.0))
        canvas.create_rectangle(0, 0, width * frac, height, fill=color, outline="")
        canvas.create_text(width - 6, height / 2, anchor="e",
                           text=f"{getattr(self, '_power_pct', 0.0):.0f}%",
                           fill="#263238", font=("Segoe UI", 8, "bold"))

    def _clear_chart(self) -> None:
        self.samples.clear()
        self._redraw_chart()

    def _on_chart_motion(self, event) -> None:
        self._cursor_x = float(event.x)
        if self._hover_job is None:
            self._hover_job = self.root.after(20, self._hover_redraw)

    def _hover_redraw(self) -> None:
        self._hover_job = None
        self._chart_dirty = False
        self._redraw_chart()

    def _on_chart_leave(self, _event) -> None:
        self._cursor_x = None
        self._redraw_chart()

    @staticmethod
    def _polyline(canvas, xs, ys, **kw) -> None:
        points: list[float] = []
        for x, y in zip(xs, ys):
            points.extend((x, y))
        if len(points) >= 4:
            canvas.create_line(*points, **kw)

    def _redraw_chart(self) -> None:
        canvas = getattr(self, "chart", None)
        if canvas is None:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 200)
        height = max(canvas.winfo_height(), 160)
        lay = chart.layout(width, height)
        samples = chart.viewport(list(self.samples), CHART_WINDOW)
        if len(samples) < 2:
            canvas.create_text(width / 2, height / 2, text="等待温度数据…",
                               fill="#888888", font=("Segoe UI", 12))
            return
        # 单图双轴：左轴温度（红）、右轴功率（青），共用 X 轴与同一套网格
        axes = chart.dual_axis(samples)
        lo, hi, step, pmax = axes.temp_lo, axes.temp_hi, axes.temp_step, axes.power_max
        last = samples[-1]
        st = self._last_status
        supply = f"{st.supply_power_w}W" if st is not None else "--"
        voltage = st.voltage_text if st is not None else "--"
        self.chart_info_var.set(
            f"样本 {len(samples)} · 供电 {supply} · 输入 {voltage} · 输出 {last.power}W")

        # 抓取进度条
        canvas.create_rectangle(lay.left, lay.top, lay.right, lay.bottom,
                                outline="#dddddd")
        value = lo
        while value <= hi + 1e-9:
            y = chart.y_temp(value, lay, lo, hi)
            canvas.create_line(lay.left, y, lay.right, y, fill="#eeeeee")
            canvas.create_text(lay.left - 6, y, text=f"{value:g}", anchor="e",
                               fill="#c0392b", font=("Segoe UI", 8))
            value += step
        for frac in (0.0, 0.5, 1.0):
            val = pmax * frac
            y = chart.y_power(val, lay, pmax)
            canvas.create_text(lay.right + 6, y, text=f"{val:g}", anchor="w",
                               fill=COLOR_POWER, font=("Segoe UI", 8))
        canvas.create_text(lay.left + 2, lay.top - 8, anchor="w",
                           text="温度 ℃", fill=COLOR_TEMP, font=("Segoe UI", 8))
        canvas.create_text(lay.right - 2, lay.top - 8, anchor="e",
                           text="功率 W", fill=COLOR_POWER, font=("Segoe UI", 8))
        canvas.create_text(lay.left + 2, lay.top + 2, anchor="nw",
                           text="— （红色）温度   -- 目标温度   — （蓝色）功率", fill="#888888",
                           font=("Segoe UI", 8))

        # X 轴是**真实时间**（不是样本序号）：轮询间隔变了也不会把距离画歪
        t0 = min(s.ts for s in samples)
        t1 = max(s.ts for s in samples)
        xs = [chart.x_time(s.ts, t0, t1, lay) for s in samples]

        # 目标温度曲线只画加热段（不加热时设备报的是残留目标值）
        seg_x: list[float] = []
        seg_y: list[float] = []
        for s, x in zip(samples, xs):
            if s.heating:
                seg_x.append(x)
                seg_y.append(chart.y_temp(s.target, lay, lo, hi))
            else:
                self._polyline(canvas, seg_x, seg_y, fill=COLOR_TARGET, width=1,
                               dash=(4, 3))
                seg_x, seg_y = [], []
        self._polyline(canvas, seg_x, seg_y, fill=COLOR_TARGET, width=1, dash=(4, 3))

        self._polyline(canvas, xs, [chart.y_temp(s.temp, lay, lo, hi) for s in samples],
                       fill=COLOR_TEMP, width=2)
        self._polyline(canvas, xs, [chart.y_power(s.power, lay, pmax) for s in samples],
                       fill=COLOR_POWER, width=2)

        # X 轴时间刻度（4~6 个；样本不足或同一时刻 -> --:--）
        ticks = chart.time_ticks(samples, 5)
        if ticks:
            for frac, label in ticks:
                x = lay.left + frac * lay.plot_w
                canvas.create_line(x, lay.bottom, x, lay.bottom + 4, fill="#bbbbbb")
                canvas.create_text(x, lay.bottom + 7, text=label, fill="#666666",
                                   anchor="n", font=("Segoe UI", 8))
        else:
            canvas.create_text((lay.left + lay.right) / 2, lay.bottom + 7,
                               text="--:--", fill="#999999", anchor="n",
                               font=("Segoe UI", 8))

        cursor_x = self._cursor_x
        if cursor_x is not None:
            sample = chart.hit_time(cursor_x, lay, samples)
            if sample is not None:
                x = chart.x_time(sample.ts, t0, t1, lay)
                canvas.create_line(x, lay.top, x, lay.bottom,
                                   fill="#999999", dash=(2, 2))
                canvas.create_oval(x - 3, chart.y_temp(sample.temp, lay, lo, hi) - 3,
                                   x + 3, chart.y_temp(sample.temp, lay, lo, hi) + 3,
                                   fill=COLOR_TEMP, outline="")
                py = chart.y_power(sample.power, lay, pmax)
                canvas.create_oval(x - 3, py - 3, x + 3, py + 3,
                                   fill=COLOR_POWER, outline="")
                self._draw_tooltip(canvas, sample, cursor_x, width, lay)

    def _draw_tooltip(self, canvas, sample: chart.Sample, cursor_x: float,
                      width: float, lay: chart.Layout) -> None:
        rows = chart.readout_rows(sample)
        box_w, line_h = 156, 16
        box_h = 10 + line_h * len(rows)
        x0 = min(max(cursor_x + 14, lay.left + 4), width - box_w - 6)
        y0 = lay.top + 6
        canvas.create_rectangle(x0, y0, x0 + box_w, y0 + box_h,
                                fill="#ffffff", outline="#888888")
        for i, (key, val) in enumerate(rows):
            y = y0 + 6 + i * line_h
            canvas.create_text(x0 + 8, y, text=key, anchor="nw", fill="#666666",
                               font=("Segoe UI", 8))
            canvas.create_text(x0 + box_w - 8, y, text=val, anchor="ne", fill="#222222",
                               font=("Segoe UI", 8, "bold"))

    # ------------------------------------------------------------ 扫描/连接
    def _on_scan(self) -> None:
        self.tip_var.set("扫描中…")
        self.scan_btn.configure(state="disabled")
        self._submit(self.transport.scan, 6.0, label="扫描", event="scan_done",
                     on_error=lambda e: self.scan_btn.configure(state="normal"))

    def _apply_scan(self, results: list[ScanResult]) -> None:
        self.scan_btn.configure(state="normal")
        self._connecting = False
        self._refresh_buttons()
        self.scan_results = list(results)
        labels = [r.label() for r in self.scan_results]
        self.device_box.configure(values=labels)
        if labels:
            self.device_box.current(0)
            self.tip_var.set(f"扫描完成，共 {len(labels)} 个设备（未按名字过滤）")
        else:
            self._show_hint(NOT_BROADCAST_HINT)

    def _selected(self) -> ScanResult | None:
        idx = self.device_box.current()
        if 0 <= idx < len(self.scan_results):
            return self.scan_results[idx]
        if self.opts.address:
            return ScanResult(self.opts.address, "(手动地址)", None, "ble")
        return None

    def _on_connect(self) -> None:
        if self._connecting and not self.connected:
            self._on_cancel_connect()      # 按钮此刻就是「取消连接」
            return
        target = self._selected()
        if target is None and not (self.opts.address or self.opts.name):
            self._show_hint("请先扫描并选择设备（或使用 --address / --name）")
            return
        self.conn.address = self.opts.address or (target.address if target else None)
        self.conn.name_hint = self.opts.name
        self._connecting = True
        self._refresh_buttons()
        self._submit(self.conn.connect, target, label="连接", event="connect_done")

    def _on_rescan_connect(self) -> None:
        self._submit(self._rescan_connect_job, label="重新扫描并连接",
                     event="connect_done")

    async def _rescan_connect_job(self) -> bool:
        found = await self.transport.scan(6.0)
        self.events.put(("scan_done", found))
        target = None
        if self.conn.address:
            target = next((r for r in found
                           if r.address.lower() == self.conn.address.lower()), None)
        else:
            target = pick_unique_candidate(found)   # 不盲连 RSSI 最强的任意设备
        if target is None:
            self.events.put(("sys", "未自动选择设备：请在下拉框里选中后点「连接」"
                                    "（或用 --address / --name 启动）"))
            return False
        self.conn.address = self.conn.address or target.address
        return await self.conn.connect(target)

    def _on_disconnect(self) -> None:
        self.disconnect_btn.configure(state="disabled")
        self._submit(self.conn.disconnect, label="断开", event="disconnected")

    def _on_cancel_reconnect(self) -> None:
        self._on_cancel_connect()

    def _on_cancel_connect(self) -> None:
        """取消正在进行的连接：中断当前那一段 + 释放 BLE 客户端 + 状态回未连接。

        cancel() 必须在 IO 线程里跑（task.cancel() 不是线程安全的），所以走 call_soon。
        """
        self.tip_var.set("正在取消连接…")
        self._stop_conn_ticker()
        self.runner.call_soon(self.conn.cancel)

    def _on_identify(self) -> None:
        self._submit(self.device.identify, label="重新识别")

    def _auto_connect(self) -> None:
        self._submit(self._auto_connect_job, label="自动连接", event="connect_done")

    async def _auto_connect_job(self) -> bool:
        if self.opts.address:
            target = ScanResult(self.opts.address, "(命令行地址)", None, "ble")
        elif self.opts.name:
            found = await self.transport.scan(4.0)
            self.events.put(("scan_done", found))
            target = next((r for r in found if self.opts.name.lower() in (r.name or "").lower()),
                          None)
        else:
            found = await self.transport.scan(3.0)
            self.events.put(("scan_done", found))
            target = pick_unique_candidate(found)
            if target is None:
                self.events.put(("sys", "扫描到{0}个设备：请在下拉框里选中后点「连接」"
                                        "（或用 --address / --name 启动）".format(len(found))))
                return False
        return await self.conn.connect(target)

    # ------------------------------------------------------------ 命令按钮
    def _int_or_none(self, var: tk.StringVar, what: str) -> int | None:
        try:
            return int(float(var.get()))
        except (TypeError, ValueError):
            self.tip_var.set(f"{what} 必须是数字")
            return None

    def _collect(self, variables: list[tk.StringVar], what: str) -> list[int] | None:
        out: list[int] = []
        for i, var in enumerate(variables):
            try:
                out.append(int(float(var.get())))
            except (TypeError, ValueError):
                self.tip_var.set(f"{what} 第 {i + 1} 个值不是数字")
                return None
        return out

    def _temp_value(self, var, key: str, what: str) -> int | None:
        """读温度输入框：合法则返回；非法则回退上一有效值 + 提示，且**不发帧**。"""
        ok, value, msg = clamp_or_reject_temp(var.get())
        if ok:
            self._temp_last[key] = str(var.get()).strip()
            return value
        var.set(self._temp_last.get(key, ""))
        self.tip_var.set(f"{what}：{msg}")
        return None

    def _on_set_target(self) -> None:
        value = self._temp_value(self.target_var, "target", "目标温度")
        if value is not None:
            self._submit(self.device.set_thermostat, hc.TARGET_INDEX, value,
                         label=f"设目标温度 {value}℃")

    def _on_write_preset(self, index: int) -> None:
        value = self._temp_value(self.preset_vars[index], f"preset{index}", f"T{index + 1}")
        if value is not None:
            self._submit(self.device.set_thermostat, index, value,
                         label=f"写 T{index + 1} = {value}℃")

    def _on_read_presets(self) -> None:
        self._submit(self.device.read_presets, label="读预设温度")

    def _update_channel_hint(self) -> None:
        """回显「当前挡位预设」；勾了自定义目标就提示会用输入框的值。"""
        channel = int(self.channel_var.get())
        preset = self.device._preset_of(channel)
        if self.custom_target_var.get():
            self.channel_hint_var.set(f"T{channel + 1} 预设 {preset}℃ → 将覆盖为自定义目标")
        else:
            self.channel_hint_var.set(f"T{channel + 1} 预设 "
                                      f"{'--' if preset is None else f'{preset}℃'}")

    def _on_start_heating(self) -> None:
        channel = int(self.channel_var.get())
        target = None
        if self.custom_target_var.get():
            target = self._int_or_none(self.target_var, "自定义目标温度")
            if target is None:
                return
        label = (f"开始加热 T{channel + 1} @ {target}℃（自定义）" if target is not None
                 else f"开始加热 T{channel + 1}（挡位预设）")
        self._submit(self.device.start_heating, channel, target, label=label,
                     on_done=self._on_heating_sent)

    def _on_heating_sent(self, ok) -> None:
        """开始加热下发之后的提示：命令成功 ≠ 设备真的开始加热。

        设备停在待机/设置界面时，目标温度会被写进设备，但不一定会开始加热 ——
        这时不能报「成功」，否则用户以为在加热而实际没动。
        """
        if ok is False:
            self._set_tip("开始加热: 失败/无应答")
            return
        status = self.device.status
        if not hc.is_heating(status):
            where = f"（当前：{status.mode_name}）" if status is not None else ""
            self._set_tip("已下发到设备（注意：设备当前在待机/设置界面，可能不会开始加热；"
                          f"请先切到加热界面）{where}", warn=True)
        else:
            self._set_tip("开始加热: 成功")

    def _on_stop_heating(self) -> None:
        self._submit(self.device.stop_heating, label="停止加热")

    def _on_write_reflow(self) -> None:
        values = self._collect(self.reflow_vars, "回流曲线")
        if values is None:
            return
        ok, msg = hc.validate_fields(values, hc.REFLOW_FIELDS)
        if not ok:
            self.tip_var.set(msg)          # 8 个值必须全合法，缺一个就整条不发
            return
        self._submit(self.device.write_reflow, values, label="写回流曲线")

    # ------------------------------------------------------------ 重置默认参数
    def _on_reset_reflow(self) -> None:
        if not messagebox.askyesno(
                "重置默认参数",
                "把回流曲线写成手册出厂值？\n\n"
                "预热 150℃/120s，恒温 180℃/90s，回流 260℃/60s，冷却 80℃/300s",
                parent=self.root):
            return
        self._submit(self._reset_reflow_job, label="重置回流参数")

    def _on_reset_repair(self) -> None:
        if not messagebox.askyesno(
                "重置默认参数",
                "把维修参数写成手册出厂值？\n\n"
                "除胶 100℃/120s，分层 80℃/120s，贴合 80℃/120s，植球 260℃/120s",
                parent=self.root):
            return
        self._submit(self._reset_repair_job, label="重置维修参数")

    async def _reset_reflow_job(self) -> str:
        return await self._reset_and_verify(hc.REFLOW_DEFAULTS, hc.REFLOW_FIELDS,
                                            self.device.write_reflow,
                                            self.device.read_reflow, "回流参数")

    async def _reset_repair_job(self) -> str:
        return await self._reset_and_verify(hc.REPAIR_DEFAULTS, hc.REPAIR_FIELDS,
                                            self.device.write_repair,
                                            self.device.read_repair, "维修参数")

    async def _reset_and_verify(self, values, fields, write, read, what: str) -> str:
        """写手册出厂值 -> 读回比对 -> 返回一句给人看的结论（单一行，直接进状态栏）。"""
        values = [int(v) for v in values]
        ok, msg = hc.validate_fields(values, fields)
        if not ok:
            return f"重置{what}失败：{msg}"
        if not await write(values):
            return f"重置{what}失败：设备无应答"
        got = await read()
        if got is None:
            return f"重置{what}：已下发，但读回失败，请检查"
        got = [int(v) for v in got]
        if got != values:
            return f"重置{what}：读回不一致，请检查（写入 {values}，读回 {got}）"
        return f"重置{what}：已重置并读回确认"

    # ------------------------------------------------------------ 导出折线图
    def _on_save_chart(self) -> None:
        samples = chart.viewport(list(self.samples), CHART_WINDOW)
        if len(samples) < 2:
            self._set_tip("没有数据可保存：先让设备上报几帧再试", warn=True)
            return
        path = filedialog.asksaveasfilename(
            title="保存折线图", defaultextension=".png", initialdir=default_logs_dir(),
            initialfile=time.strftime("hp20_chart_%Y%m%d_%H%M%S.png"),
            filetypes=[("PNG 图片", "*.png"), ("SVG 矢量图", "*.svg")])
        if not path:
            return
        device = self.device.device_name or self.opts.name or "HP20"
        ok, msg = chart_export.save_chart(path, samples, device_name=device)
        self._set_tip(msg, warn=not ok)

    def _on_read_reflow(self) -> None:
        self._submit(self.device.read_reflow, label="读回流曲线")

    def _on_write_repair(self) -> None:
        values = self._collect(self.repair_vars, "维修参数")
        if values is None:
            return
        ok, msg = hc.validate_fields(values, hc.REPAIR_FIELDS)
        if not ok:
            self.tip_var.set(msg)
            return
        self._submit(self.device.write_repair, values, label="写维修参数")

    def _on_read_repair(self) -> None:
        self._submit(self.device.read_repair, label="读维修参数")

    def _on_read_name(self) -> None:
        self._submit(self.device.read_name, label="读蓝牙名")

    def _on_write_name(self) -> None:
        name = (self.name_var.get() or "").strip()
        if not name:
            self.tip_var.set("蓝牙名不能为空")
            return
        self._submit(self.device.write_name, name, label=f"写蓝牙名 {name}")

    # ------------------------------------------------------------ 原始帧
    def _parse_hex(self, text: str) -> bytes | None:
        clean = text.replace(" ", "").replace(",", "").replace("0x", "").replace("0X", "")
        if not clean or len(clean) % 2:
            self.tip_var.set("HEX 长度必须是偶数")
            return None
        try:
            return bytes.fromhex(clean)
        except ValueError:
            self.tip_var.set("HEX 解析失败")
            return None

    def _on_send_hex(self) -> None:
        data = self._parse_hex(self.hex_var.get())
        if data is not None:
            self._submit(self.device.send_raw, data, label="原始发送")

    def _on_build_send(self) -> None:
        data = self._parse_hex(self.hex_var.get())
        if data is None:
            return
        if len(data) >= 6 and data[0] in (framing.REQ_HEADER, framing.RESP_HEADER):
            self._submit(self.device.send_raw, data, label="原始发送")
            return
        frame = framing.build_frame(data[0], data[1:],
                                    dev_id=self.opts.dev_id, family=self.opts.family)
        self.hex_var.set(framing.to_hex(frame))
        self._append_text(f"{_ts()} SYS 组帧: {framing.to_hex(frame)}")
        self._submit(self.device.send_raw, frame, label="组帧发送")

    # ------------------------------------------------ 控制台 -> 诊断面板（钉住一条）
    @staticmethod
    def _parse_console_line(line: str):
        """把控制台一行 `hh:mm:ss TX AF 09 …` 还原成 (方向, 原始字节, 时间)。

        不是「时间 + 方向 + 完整十六进制」的行（SYS 注释行、被折开的续行、空行）返回 None。
        """
        # 控制台帧行 = 日志时间戳 + 方向 + hex；时间戳可能是 `2026-10-18 15:52:31.123`
        # （FrameLogger 的 ISO 形式）也可能只是 `15:52:31`，两种都要认
        match = re.match(r"^\s*(?:\d{4}-\d{2}-\d{2}\s+)?(\d{2}:\d{2}:\d{2})(?:\.\d+)?"
                         r"\s+(TX|RX)\s+((?:[0-9A-Fa-f]{2}\s*)+)$", line or "")
        if not match:
            return None
        try:
            raw = bytes.fromhex(match.group(3).replace(" ", ""))
        except ValueError:
            return None
        if len(raw) < 6 or raw[0] not in (framing.REQ_HEADER, framing.RESP_HEADER):
            return None                 # 结构上就不是一帧
        return match.group(2), raw, match.group(1)

    def _raw_selection_text(self) -> str:
        try:
            return self.raw_text.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def _selected_raw_frame(self):
        return self._parse_console_line(self._raw_selection_text())

    def _update_parse_button_state(self) -> None:
        """没有选中或选中的不是完整帧 -> 「解析选中帧」置灰（选错时给一句提示）。"""
        ok = self._selected_raw_frame() is not None
        btn = getattr(self, "parse_sel_btn", None)
        if btn is not None:
            btn.configure(state="normal" if ok else "disabled")
        if not ok and self._raw_selection_text().strip():
            self.tip_var.set("请选择一条完整帧")

    def _on_raw_click(self, event) -> None:
        """单击一行 = 选中整行；已在同一行内拖选则保留用户的选区。"""
        try:
            first = self.raw_text.index("sel.first")
            last = self.raw_text.index("sel.last")
            keep = first.split(".")[0] == last.split(".")[0] and first != last
        except tk.TclError:
            keep = False
        if not keep:
            index = self.raw_text.index(f"@{event.x},{event.y}")
            self.raw_text.tag_remove("sel", "1.0", "end")
            self.raw_text.tag_add("sel", f"{index} linestart", f"{index} lineend")
        self._update_parse_button_state()

    def _on_raw_double_click(self, event) -> None:
        """双击某条帧 = 点「解析选中帧」。"""
        index = self.raw_text.index(f"@{event.x},{event.y}")
        record = self._parse_console_line(
            self.raw_text.get(f"{index} linestart", f"{index} lineend"))
        if record is None:
            self.tip_var.set("请选择一条完整帧")
            return
        self._pin_raw_frame(*record)

    def _on_parse_selected_raw(self) -> None:
        record = self._selected_raw_frame()
        if record is None:
            self.tip_var.set("请选择一条完整帧")
            self._update_parse_button_state()
            return
        self._pin_raw_frame(*record)

    def _pin_raw_frame(self, direction: str, raw: bytes, stamp: str) -> None:
        """把一条控制台帧钉进诊断面板第三段（不会被轮询刷新覆盖）。"""
        self._pinned_frame = (direction, bytes(raw), stamp)
        btn = getattr(self, "clear_pin_btn", None)
        if btn is not None:
            btn.configure(state="normal")
        self.notebook.select(self.device_tab)
        self._refresh_debug_panel(force=True)
        self.tip_var.set(f"已解析选中的 {direction} 帧（{stamp}）")

    def _on_clear_pin(self) -> None:
        self._pinned_frame = None
        btn = getattr(self, "clear_pin_btn", None)
        if btn is not None:
            btn.configure(state="disabled")
        self._refresh_debug_panel(force=True)

    def _clear_raw(self) -> None:
        self.raw_text.delete("1.0", "end")
        self.raw_follow = True          # 清空后回到跟随最新
        self._update_follow_ui()
        self._update_parse_button_state()      # 选区没了 -> 按钮回置灰

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存收发日志", defaultextension=".log",
            initialdir=default_logs_dir(), initialfile="hp20_frames.log")
        if not path:
            return
        rows = self.logger.save(path)
        self.tip_var.set(f"已保存 {rows} 行到 {path}")

    def _toggle_csv(self) -> None:
        if self.csv is not None:
            rows = self.csv.close()
            self.tip_var.set(f"CSV 已保存 {self.csv.path} ({rows} 行)")
            self.csv = None
            self.csv_btn.configure(text="开始记录")
            self.csv_var.set("CSV: 未记录")
            return
        kind = self._csv_kind_key()     # 只认下标 key，不拿中文文案驱动业务分支
        path = self.opts.csv
        if not path:
            os.makedirs(default_logs_dir(), exist_ok=True)
            stamp = time.strftime("hp20_%Y%m%d_%H%M%S.csv")
            path = os.path.join(default_logs_dir(),
                                f"hp20_curve_{stamp}" if kind == "full"
                                else f"hp20_compact_{stamp}")
        self.csv = make_recorder(kind, path)
        self.csv.on_error = lambda msg: self.events.put(("sys", msg))
        self.csv_btn.configure(text="停止记录")
        self.csv_var.set(f"CSV: 记录中 → {os.path.basename(path)}")

    def _csv_kind_key(self) -> str:
        """下拉项的**内部 key**（按序号）：文案改了逻辑也不会跟着错。"""
        index = self.csv_kind_box.current()
        if 0 <= index < len(CSV_KIND_KEYS):
            return CSV_KIND_KEYS[index]
        return CSV_KIND_KEYS[0]

    def _apply_poll(self) -> None:
        value = parse_interval(self.poll_var.get(), POLL_MIN_S, POLL_MAX_S)
        if value is None:
            self.poll_var.set(f"{self.device.poll_interval:.3f}")     # 回退到当前生效值
            self.tip_var.set(f"轮询间隔需在 {POLL_MIN_S:g}~{POLL_MAX_S:g} s 之间，"
                             f"已回退为 {self.device.poll_interval:.3f}s")
            return
        self.device.poll_interval = value
        self.runner.call_soon(self.device.rearm_poll)
        self.poll_var.set(f"{value:.3f}")
        self._update_rate_text()
        self.tip_var.set(f"轮询间隔 {value:.3f}s 已生效")

    def _apply_gap(self) -> None:
        value = parse_interval(self.gap_var.get(), GAP_MIN_S, GAP_MAX_S)
        if value is None:
            self.gap_var.set(f"{self.device.write_gap:.3f}")
            self.tip_var.set(f"写间隔需在 {GAP_MIN_S:g}~{GAP_MAX_S:g} s 之间，"
                             f"已回退为 {self.device.write_gap:.3f}s")
            return
        self.device.write_gap = value
        self.gap_var.set(f"{value:.3f}")
        self._update_rate_text()
        self.tip_var.set(f"写间隔 {value:.3f}s 已生效")

    def _update_rate_text(self) -> None:
        self.rate_var.set(f"轮询 {self.device.poll_interval:.3f}s · "
                          f"写间隔 {self.device.write_gap:.3f}s")

    # ------------------------------------------------------------ 提交与等待
    def _submit(self, fn, *args, label: str = "", event: str | None = None,
                on_error=None, on_done=None) -> None:
        try:
            fut = self.runner.submit(fn, *args)
        except Exception as exc:  # noqa: BLE001
            self._handle_event("error", f"{label} 无法提交: {exc}")
            if on_error:
                on_error(exc)
            return
        self._watch(fut, label, event, on_error, on_done)

    def _watch(self, fut, label: str, event: str | None, on_error, on_done=None) -> None:
        def check() -> None:
            if self._closing:
                return
            if not fut.done():
                self.root.after(80, check)
                return
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                self._handle_event("error", f"{label or '操作'} 失败: {exc}")
                if on_error:
                    on_error(exc)
                return
            if event:
                self._handle_event(event, result)
            elif on_done is not None:
                on_done(result)          # 自己负责提示（比如要按当前状态换语气）
            elif label:
                if isinstance(result, str):
                    self._set_tip(result)          # 任务自带结论文案
                else:
                    ok = "成功" if result is not False else "失败/无应答"
                    self._set_tip(f"{label}: {ok}")

        self.root.after(80, check)

    # ------------------------------------------------------------ 收尾
    def _smoke_heat(self) -> None:
        """冒烟时自己开一段加热（走自定义目标路径，验证 0x05 后再 0x03）。"""
        self.target_var.set("120")
        self.channel_var.set(0)
        self.custom_target_var.set(True)
        self._on_start_heating()

    def _smoke_done(self) -> None:
        print(f"SMOKE samples={self.sample_count} tx={self.tx_count} rx={self.rx_count} "
              f"connected={self.connected} identified={self.identified_ok} "
              f"state={self.conn.state_text()} stats={self.device.stats}")
        recent = self.conn.recent(1)
        if recent:
            print(f"SMOKE last_conn_event: {recent[0].format()}")
        last = self.samples[-1] if self.samples else None
        if last is not None:
            print(f"SMOKE last_sample temp={last.temp} target={last.target} "
                  f"power={last.power} w2=0x{last.w2:04X}")
        ok = self.sample_count > 0 and self.rx_count > 0 and self.identified_ok is True
        print(f"SMOKE {'OK' if ok else 'FAIL'} — 窗口已创建并运行 {self.opts.smoke:g}s")
        self._on_close(exit_code=0 if ok else 1)

    def _on_close(self, exit_code: int | None = None) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_conn_ticker()
        for job in (getattr(self, "_pump_job", None),):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:  # noqa: BLE001
                    pass
        try:
            fut = self.runner.submit(self.conn.disconnect)
            fut.result(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        self.runner.stop()
        if self.csv is not None:
            self.csv.close()
            self.csv = None
        self.logger.close()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        if exit_code is not None:
            self.exit_code = exit_code


def run_gui(opts) -> int:
    if tk is None:
        print(f"未能加载 tkinter: {_TK_ERR}", file=sys.stderr)
        return 2
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        print(f"无法创建窗口(无图形环境?): {exc}", file=sys.stderr)
        return 2
    app = Hp20App(root, opts)
    app.exit_code = 0
    root.mainloop()
    return app.exit_code


# ====================================================================== CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="正点原子 HP20 加热台上位机")
    p.add_argument("--sim", action="store_true", help="使用进程内模拟设备(无需蓝牙)")
    p.add_argument("--selftest", action="store_true", help="非 GUI 端到端自检, 退出码 0/1")
    p.add_argument("--smoke", type=float, nargs="?", const=5.0, default=None, metavar="秒",
                   help="起窗口运行 N 秒后自动关闭并打印结论")
    p.add_argument("--scan-only", action="store_true", help="只扫描 BLE 设备并打印")
    p.add_argument("--probe", metavar="地址或名称",
                   help="无 GUI 的真机只读自检（只读、绝不写；退出码 0/1/2）")
    p.add_argument("--address", help="直接连接指定 BLE 地址(跳过按名扫描)")
    p.add_argument("--name", help="按广播名查找设备(子串匹配)")
    p.add_argument("--csv", help="CSV 记录路径(默认: logs/hp20_curve_*.csv)")
    p.add_argument("--log", help="收发日志文件(默认仅内存, 可界面另存)")
    p.add_argument("--gap", type=float, default=hc.WRITE_GAP_S, help="写队列最小间隔秒, 默认 0.015")
    p.add_argument("--timeout", type=float, default=hc.RESPONSE_TIMEOUT_S,
                   help="命令应答超时秒, 默认 2.0")
    p.add_argument("--retries", type=int, default=0, help="命令超时重试次数, 默认 0")
    p.add_argument("--connect-retries", type=int, default=hc.RECONNECT_MAX_ATTEMPTS,
                   help="连接/重连退避重试次数, 默认 4 (间隔 1/2/4/8s)")
    p.add_argument("--poll", type=float, default=hc.POLL_INTERVAL_S,
                   help="轮询间隔秒, 默认 0.125")
    p.add_argument("--dev-id", type=lambda s: int(s, 0), default=hc.DEV_ID,
                   help="帧内设备 ID, HP20=0x09")
    p.add_argument("--family", type=lambda s: int(s, 0), default=hc.FAMILY,
                   help="帧内 family, HP20=0x02")
    return p


def _scan_only(opts) -> int:
    async def run() -> int:
        transport = make_transport(opts.sim)
        try:
            found = await transport.scan(6.0)
        except Exception as exc:  # noqa: BLE001
            print(f"扫描失败: {exc}", file=sys.stderr)
            return 2
        print(f"发现 {len(found)} 个设备 ({transport.describe()}):")
        print(f"  {'地址':<20} {'名称':<26} {'RSSI':>8}  广播服务")
        for r in found:
            print(f"  {r.address:<20} {r.name:<26} {str(r.rssi) + ' dBm':>8}  {r.detail}")
        print("\n提示: 型号靠 cmd 0x00 握手应答判定（HP20 = payload 09 02），不靠蓝牙名；")
        print("     直连: python atk_hp20.py --address <地址>   按名: --name HP20")
        if not found:
            print("\n" + NOT_BROADCAST_HINT)
        return 0

    return asyncio.run(run())


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    opts = build_parser().parse_args(argv)
    if opts.probe:
        return _probe(opts)
    if opts.selftest:
        return asyncio.run(run_selftest(opts, link_button_states))
    if opts.scan_only:
        return _scan_only(opts)
    return run_gui(opts)


if __name__ == "__main__":
    sys.exit(main())
