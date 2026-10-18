"""收发日志、连接事件日志与 CSV（完整 / 简洁两种格式）。"""
from __future__ import annotations

import csv
from typing import Callable
import datetime as _dt
import os
import sys
import time
from collections import deque
from dataclasses import dataclass

ENCODING = "utf-8-sig"      # 带 BOM，Excel 才不会把中文按 GBK 解成乱码


def default_logs_dir() -> str:
    """默认日志目录。

    - 源码运行：`<winapp 上级>/logs`（程序目录的上级）；
    - 打包成 exe（`sys.frozen`）后 winapp 目录不存在，改用 **exe 同级的 logs/**：
      双击、快捷方式、从任意目录启动都写同一个可写位置。
    """
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "logs")
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "logs")


def legacy_logs_hint(new_dir: str, cwd: str | None = None) -> str | None:
    """旧版本把日志写到 <当前工作目录>/logs；那里还有 csv 就给一句迁移提示。

    默认目录统一到 default_logs_dir（程序目录的上级）之后，从别的 cwd 启动会找不到
    以前的日志，所以启动时提示一次「旧位置 -> 新位置」。
    """
    old = os.path.join(cwd or os.getcwd(), "logs")
    if os.path.abspath(old) == os.path.abspath(new_dir):
        return None
    try:
        names = os.listdir(old)
    except OSError:
        return None
    if not any(name.lower().endswith(".csv") for name in names):
        return None
    return f"发现旧日志目录 {old}，新日志写入 {new_dir}"


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


@dataclass(frozen=True)
class LogRecord:
    ts: str
    direction: str      # "TX" | "RX" | "EXC" | "SYS"
    text: str

    def format(self) -> str:
        return f"{self.ts} {self.direction:<3} {self.text}"


class FrameLogger:
    """内存环形缓冲 + 可选追加写文件。"""

    def __init__(self, path: str | None = None, keep: int = 4000) -> None:
        self.records: deque[LogRecord] = deque(maxlen=keep)
        self.path = path
        self._fh = None
        self.on_error: Callable[[str], None] | None = None
        self.write_errors = 0
        self.last_error = ""
        self._reported = False
        if path:
            self._open()

    def _open(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._fh = open(self.path, "a", encoding=ENCODING)

    def log(self, direction: str, text: str) -> LogRecord:
        rec = LogRecord(_now_iso(), direction, text)
        self.records.append(rec)     # 内存环形缓冲永远成功，界面照旧能看到
        if self._fh is not None:
            try:
                self._fh.write(rec.format() + "\n")
                self._fh.flush()
            except Exception as exc:  # noqa: BLE001
                # 写盘失败绝不能顺着调用栈传回事件泵（磁盘满 = 界面假死的老坑）
                self._report_error(exc)
        return rec

    def _report_error(self, exc: Exception) -> None:
        self.write_errors += 1
        if self._reported:
            return                    # 只提示一次，避免每帧刷屏
        self._reported = True
        self.last_error = f"收发日志写盘失败（采集继续，后续行继续尝试）：{exc}"
        cb = self.on_error
        if cb is not None:
            cb(self.last_error)

    def save(self, path: str) -> int:
        with open(path, "w", encoding=ENCODING) as fh:
            for rec in self.records:
                fh.write(rec.format() + "\n")
        return len(self.records)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# 完整 CSV：字段全，便于事后分析
# timer_text = 加热时间/休眠倒计时（设备剩余秒数 MM:SS / OFF）；w2_raw 保原值
# 帧里没有风扇信息（W2 bit13 已证伪），所以没有 fan 列
# 表头中文化（仿手机 APP 的叫法）；**列序与数值完全不变**，只改文字
CSV_HEADER = ("时间", "ISO时间", "当前温度(℃)", "目标温度(℃)", "输出功率(W)", "供电功率(W)",
              "功率占比(%)", "输入电压(0.1V)", "模式", "模式名", "挡位", "输出状态",
              "W2原始", "计时")

# 简洁 CSV：对齐手机 APP 的三列，方便直接对照
COMPACT_HEADER = ("时间", "V(℃)", "A(W)")


def _elapsed_text(seconds: float) -> str:
    """会话相对时间 -> HH:MM:SS:mmm（毫秒用冒号分隔，与手机 APP 一致）。"""
    seconds = max(0.0, seconds)
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    millis = int(round((seconds - int(seconds)) * 1000)) % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{millis:03d}"


class CsvRecorder:
    """完整 CSV。每 flush_every 行落一次盘（Windows 逐行 flush 有写盘抖动）。"""

    header = CSV_HEADER
    kind = "full"

    def __init__(self, path: str, flush_every: int = 10, encoding: str = ENCODING) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.rows = 0
        self.encoding = encoding
        self._flush_every = max(1, int(flush_every))
        self.on_error: Callable[[str], None] | None = None
        self.write_errors = 0
        self.last_error = ""
        self._reported = False
        self._fh = open(path, "w", encoding=encoding, newline="")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(self.header)
        self._fh.flush()

    def record(self, status) -> None:
        """status 为 hp20_commands.RealtimeStatus；temp_c 取 payload[0:2]。

        写盘异常在这里就地吞掉（只记一次提示）：CSV 是旁路记录，磁盘错误不该打断采集，
        更不该顺事件泵把界面拖死。
        """
        try:
            self._writer.writerow((
                f"{time.time():.3f}", _now_iso(), status.current_temp, status.target_temp,
                status.effective_power_w, status.supply_power_w,   # 不加热写 0（列名不变）
                f"{status.power_percent:.1f}", status.input_voltage_raw,
                status.mode, status.mode_name, status.channel_or_stage,
                status.output_state_text, status.w2, status.timer_text,
            ))
            self._rows_written()
        except Exception as exc:  # noqa: BLE001
            self._report_error(exc)

    def _report_error(self, exc: Exception) -> None:
        self.write_errors += 1
        if self._reported:
            return
        self._reported = True
        self.last_error = f"CSV 写盘失败（采集继续，后续行继续尝试）：{exc}"
        cb = self.on_error
        if cb is not None:
            cb(self.last_error)

    def _rows_written(self) -> None:
        self.rows += 1
        if self.rows % self._flush_every == 0:
            self._fh.flush()

    def close(self) -> int:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None
        return self.rows


class CompactCsvRecorder(CsvRecorder):
    """简洁 CSV：时间（会话相对）/ 温度（1 位小数）/ 功率（[6] 原值）。

    列头对齐手机 APP：时间,V(℃),A(W)
    """

    header = COMPACT_HEADER
    kind = "compact"

    def __init__(self, path: str, flush_every: int = 10, encoding: str = ENCODING) -> None:
        super().__init__(path, flush_every=flush_every, encoding=encoding)
        self._t0 = time.monotonic()

    def record(self, status) -> None:
        try:
            self._writer.writerow((
                _elapsed_text(time.monotonic() - self._t0),
                f"{status.current_temp:.1f}",
                status.output_power_w,
            ))
            self._rows_written()
        except Exception as exc:  # noqa: BLE001
            self._report_error(exc)


def make_recorder(kind: str, path: str):
    return CompactCsvRecorder(path) if kind == "compact" else CsvRecorder(path)
