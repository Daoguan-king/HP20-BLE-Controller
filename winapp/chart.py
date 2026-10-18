"""曲线几何、取点与双轴数据准备 —— 不依赖 tkinter，可单独单测。

单图双 Y 轴：左轴温度（℃）、右轴功率（W），共用 X 轴与同一套网格。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

PAD_L = 58.0     # 左侧温度刻度
PAD_R = 58.0     # 右侧功率刻度
PAD_T = 18.0
PAD_B = 24.0


@dataclass(frozen=True)
class Sample:
    """一帧实时状态的采样点。温度取 payload[0:2]，功率取 payload[6]。"""
    ts: float
    temp: int
    target: int
    power: int
    w2: int
    mode: int
    timer: str = ""       # 计时文案（MM:SS / OFF）由上层填，chart 保持设备无关
    mode_text: str = ""   # 模式文案同样由上层填
    timer_label: str = "" # 「加热时间」/「休眠倒计时」也由上层按模式填
    heating: bool = True  # 由上层用 hp20_commands.is_heating 判定；不加热不画目标曲线


@dataclass(frozen=True)
class Layout:
    width: float
    height: float
    left: float      # 绘图区左边界
    right: float     # 绘图区右边界
    top: float
    bottom: float
    plot_w: float
    plot_h: float


@dataclass(frozen=True)
class DualAxis:
    """单图双轴所需的数据与两套范围。"""
    temps: list[float] = field(default_factory=list)
    powers: list[float] = field(default_factory=list)
    temp_lo: float = 0.0
    temp_hi: float = 0.0
    temp_step: float = 10.0
    power_lo: float = 0.0        # 功率轴固定从 0 起（与温度轴一致）
    power_max: int = 10
    targets: list[float | None] = field(default_factory=list)   # 目标曲线（不加热 -> None）


# 导出图（离屏重绘）专用的四边内边距，单位是 **1x 基准**；实际按 scale 乘。
# 屏幕上的 PAD_* 是给 14px 字体留的，导出图字体更大、还要放标题与图例，所以单独一套，
# 但**仍然共用同一个 Layout 结构体与 layout() 函数**，避免出现「两套布局各自漂移」。
PAD_L_EXPORT = 35.0
PAD_R_EXPORT = 35.0
PAD_T_EXPORT = 30.0
PAD_B_EXPORT = 35.0


def layout(width: float, height: float, pad_l: float = PAD_L, pad_r: float = PAD_R,
           pad_t: float = PAD_T, pad_b: float = PAD_B) -> Layout:
    width = max(float(width), 140.0)
    height = max(float(height), 100.0)
    left, right = pad_l, width - pad_r
    top, bottom = pad_t, height - pad_b
    return Layout(width=width, height=height, left=left, right=right,
                  top=top, bottom=bottom,
                  plot_w=max(right - left, 10.0), plot_h=max(bottom - top, 10.0))


def nice_bounds(lo: float, hi: float, target_ticks: int = 5) -> tuple[float, float, float]:
    """把区间扩到整刻度，返回 (lo, hi, step)。"""
    span = max(float(hi) - float(lo), 1e-6)
    for cand in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if span / cand <= target_ticks:
            step = float(cand)
            break
    else:
        step = span / target_ticks
    lo2 = (int(lo // step)) * step if lo >= 0 else -((-int(lo // step)) * step)
    hi2 = int(-(-hi // step)) * step
    if hi2 <= lo2:
        hi2 = lo2 + step
    return lo2, hi2, step


def power_bound(samples: Sequence[Sample], floor: int = 10) -> int:
    peak = max((s.power for s in samples), default=0)
    for cand in (10, 20, 50, 65, 100, 140, 150, 200, 300, 500, 1000):
        if cand >= peak:
            return max(cand, floor)
    return max(peak, floor)


AXIS_MIN = 0.0        # 两个坐标轴的下界都固定为 0（用户要求）


def dual_axis(samples: Sequence[Sample], temp_margin: float = 5.0) -> DualAxis:
    """把采样点拆成（温度序列, 功率序列, 左轴范围, 右轴范围）。

    下界固定 0：温度/功率都从 0 起，上限仍按数据自适应（带一点余量）。
    以前温度下界跟着数据走，停热与加热两段之间会得到 44~56 与 54~66 两种局部区间，
    同一张图的斜率含义随数据跳动，用户看不出「升了多少度」。
    """
    samples = list(samples)
    if not samples:
        return DualAxis()
    temps = [float(s.temp) for s in samples]
    powers = [float(s.power) for s in samples]
    # 不加热时目标温度是设备残留值：既不画曲线，也不许它把量程拉大
    heating_targets = [float(s.target) for s in samples if s.heating]
    targets: list[float | None] = [float(s.target) if s.heating else None for s in samples]
    top = max(temps + heating_targets) if (temps or heating_targets) else AXIS_MIN
    lo, hi, step = nice_bounds(AXIS_MIN, top + temp_margin)
    return DualAxis(temps=temps, powers=powers, temp_lo=lo, temp_hi=hi,
                    temp_step=step, power_lo=AXIS_MIN,
                    power_max=power_bound(samples), targets=targets)


def viewport(samples: Sequence[Sample], limit: int) -> list[Sample]:
    if limit > 0 and len(samples) > limit:
        return list(samples)[-limit:]
    return list(samples)


def x_of(index: int, count: int, lay: Layout) -> float:
    if count <= 1:
        return lay.left
    return lay.left + index / (count - 1) * lay.plot_w


def x_time(ts: float, t0: float, t1: float, lay: Layout) -> float:
    """真实时间戳 -> 画布 x（X 轴是时间，不是样本序号）。"""
    span = t1 - t0
    if span <= 0:
        return lay.left
    frac = min(max((float(ts) - t0) / span, 0.0), 1.0)
    return lay.left + frac * lay.plot_w


# 时间刻度的粒度阈值（秒）
TICK_SUB_SECOND_SPAN = 10.0      # <= 10 s：3 个刻度 + 0.1 秒精度
TICK_MINUTE_SPAN = 60.0          # <= 60 s：4 个刻度 + MM:SS；更大：5 个 + HH:MM:SS


def _time_label(stamp: float, span: float) -> str:
    """时间标签：跨度 <=10s 用 MM:SS.d（0.1 秒），<=60s 用 MM:SS，否则 HH:MM:SS。

    秒级精度在几秒的窗口里必然撞车（28:46 会连着出现两次），所以短跨度给到 0.1 秒。
    """
    if span <= TICK_SUB_SECOND_SPAN:
        tenth = round(float(stamp) * 10) / 10
        whole = int(tenth)
        frac = int(round((tenth - whole) * 10))
        return f"{time.strftime('%M:%S', time.localtime(whole))}.{frac}"
    if span <= TICK_MINUTE_SPAN:
        return time.strftime("%M:%S", time.localtime(stamp))
    return time.strftime("%H:%M:%S", time.localtime(stamp))


def _tick_count(span: float) -> int:
    """刻度数量按跨度自适应：越短越少（少而准，胜过挤满重复标签）。"""
    if span <= TICK_SUB_SECOND_SPAN:
        return 3
    if span <= TICK_MINUTE_SPAN:
        return 4
    return 5


def time_ticks(samples: Sequence[Sample], n: int = 5) -> list[tuple[float, str]]:
    """X 轴时间刻度：返回 [(x_ratio 0..1, 标签)]，按跨度自适应 3~5 个且标签**严格互不相同**。

    - 用样本自带的真实 ts（不看轮询间隔，也不拿序号当时间）；
    - 数量：<=10s 三个、<=60s 四个、更大五个；`n` 只为兼容旧调用保留，不参与决定数量；
    - 生成后按标签去重（保第一个），保证严格递增；若去重后只剩一个，退回首尾两点；
    - 样本 < 2 个、或所有时间戳相同（无跨度）时返回 []，由界面显示 "--:--"。
    """
    samples = list(samples)
    if len(samples) < 2:
        return []
    ts = [float(s.ts) for s in samples]
    t0, t1 = min(ts), max(ts)
    span = t1 - t0
    if span <= 0:
        return []
    count = _tick_count(span)
    ticks: list[tuple[float, str]] = []
    seen: set[str] = set()
    for i in range(count):
        frac = i / (count - 1)
        label = _time_label(t0 + span * frac, span)
        if label in seen:
            continue                      # 秒级精度撞车：丢掉重复的那个
        seen.add(label)
        ticks.append((frac, label))
    if len(ticks) <= 1:
        return [(0.0, _time_label(t0, span)), (1.0, _time_label(t1, span))]
    return ticks


def hit_time(x: float, lay: Layout, samples: Sequence[Sample]) -> Sample | None:
    """画布 x -> 时间最近的那个样本（十字准线与浮框按时间对齐）。"""
    samples = list(samples)
    if not samples:
        return None
    frac = (float(x) - lay.left) / lay.plot_w
    if frac < -0.02 or frac > 1.02:
        return None
    frac = min(max(frac, 0.0), 1.0)
    ts = [float(s.ts) for s in samples]
    target = min(ts) + (max(ts) - min(ts)) * frac
    return min(samples, key=lambda s: abs(float(s.ts) - target))


def y_temp(value: float, lay: Layout, lo: float, hi: float) -> float:
    span = max(hi - lo, 1e-6)
    frac = (value - lo) / span
    frac = min(max(frac, 0.0), 1.0)
    return lay.bottom - frac * lay.plot_h


def y_power(value: float, lay: Layout, pmax: float) -> float:
    """右轴功率用同一块绘图区，只是量程不同。"""
    span = max(pmax, 1e-6)
    frac = min(max(value / span, 0.0), 1.0)
    return lay.bottom - frac * lay.plot_h


def hit_index(x: float, lay: Layout, count: int) -> int | None:
    """画布 x -> 数据点下标。缩放/量程变化只影响 layout，不改变这个换算。"""
    if count <= 0:
        return None
    if count == 1:
        return 0
    frac = (float(x) - lay.left) / lay.plot_w
    if frac < -0.02 or frac > 1.02:
        return None
    frac = min(max(frac, 0.0), 1.0)
    return int(round(frac * (count - 1)))


def hit_sample(x: float, samples: Sequence[Sample], lay: Layout) -> Sample | None:
    idx = hit_index(x, lay, len(samples))
    if idx is None:
        return None
    return samples[idx]


# ---------------------------------------------------------------- 功率进度条
POWER_BAR_COLORS = ((50.0, "#2e7d32"), (80.0, "#f57c00"), (100.01, "#c62828"))


def power_bar(percent: float) -> tuple[float, str]:
    """输出功率占比 -> (填充比例 0..1, 颜色)。阈值：<50% 绿 / <80% 橙 / 其余红。"""
    pct = min(max(float(percent), 0.0), 100.0)
    color = POWER_BAR_COLORS[-1][1]
    for limit, candidate in POWER_BAR_COLORS:
        if pct < limit:
            color = candidate
            break
    return pct / 100.0, color


def readout_rows(sample: Sample, unit: str = "℃") -> list[tuple[str, str]]:
    """悬浮框内容（键, 值）—— 界面只负责排版。温度与功率一起给。"""
    return [
        ("时间", _clock(sample.ts)),
        ("当前温度", f"{sample.temp} {unit}"),
        ("目标温度", f"{sample.target} {unit}"),
        ("当前功率", f"{sample.power} W"),
        (sample.timer_label or "计时", sample.timer or "--"),
        ("模式", sample.mode_text or f"{sample.mode}"),
    ]


def _clock(ts: float) -> str:
    import time as _t
    return _t.strftime("%H:%M:%S", _t.localtime(ts))
