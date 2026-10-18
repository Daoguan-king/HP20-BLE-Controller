"""折线图导出：Pillow 离屏重绘 PNG（2 倍尺寸、约 200 DPI），无 Pillow 时降级 SVG。

为什么不截屏：截屏只能拿到屏幕像素（还要看窗口有没有被挡），复制到报告里也会糊。
这里用同一套 chart 几何离屏重画一遍：曲线 + 坐标轴 + 图例 + 标题（设备名/时间范围）。

**四边留白**（V3 修正）：屏幕上 `chart.PAD_*` 是给 14px 字体留的，导出图字体更大、
还要放标题与图例，直接沿用会把 X 轴刻度裁在画布边缘。所以导出用
`chart.PAD_*_EXPORT`（1x 基准，实际乘 scale）——仍然是**同一个 `chart.Layout` 与
`chart.layout()`**，只是换了一套内边距，避免出现两套各自漂移的布局。
所有文字位置都用 `textbbox` 量出真实尺寸再摆，不写死像素偏移。

本模块不 import tkinter，可在无窗口环境单测。
"""
from __future__ import annotations

import os
import time
from typing import Sequence

import chart

SCALE = 2                    # 2 倍尺寸
BASE_W, BASE_H = 900.0, 300.0
DPI = 200

COLOR_TEMP = "#c0392b"
COLOR_TARGET = "#2c80c0"
COLOR_POWER = "#00BCD4"
COLOR_GRID = "#eeeeee"
COLOR_TEXT = "#444444"
COLOR_MUTED = "#666666"
BACKGROUND = (255, 255, 255)

try:                          # Pillow 可选：没有就只出 SVG
    from PIL import Image, ImageDraw, ImageFont
    PILLOW_ERR: str | None = None
except Exception as _exc:     # noqa: BLE001
    Image = ImageDraw = ImageFont = None      # type: ignore[assignment]
    PILLOW_ERR = str(_exc)

_FONT_CANDIDATES = ("msyh.ttc", "msyhl.ttc", "simhei.ttf", "segoeui.ttf")


def _font(size: int):
    """尽量拿一个带中文的 TrueType；拿不到就退回 Pillow 内置字体。"""
    size = max(8, int(size))
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except Exception:     # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size)
    except Exception:         # noqa: BLE001
        return ImageFont.load_default()


def export_layout(width: int, height: int, scale: float = SCALE) -> chart.Layout:
    """导出图布局：与屏幕共用 `chart.layout()`，只把四边内边距按 scale 放大。

    2x 画布下即 左/右 70 px、上 60 px、下 70 px —— 够放刻度文字、轴名、标题与图例。
    """
    return chart.layout(width, height,
                        pad_l=chart.PAD_L_EXPORT * scale,
                        pad_r=chart.PAD_R_EXPORT * scale,
                        pad_t=chart.PAD_T_EXPORT * scale,
                        pad_b=chart.PAD_B_EXPORT * scale)


def _title(samples: Sequence[chart.Sample], device_name: str) -> str:
    t0, t1 = min(s.ts for s in samples), max(s.ts for s in samples)
    span = t1 - t0
    fmt = "%H:%M:%S" if span >= 60 else "%M:%S"
    lo = time.strftime(fmt, time.localtime(t0))
    hi = time.strftime(fmt, time.localtime(t1))
    return (f"HP20 加热曲线 · 设备 {device_name} · {lo} ~ {hi} · "
            f"{len(samples)} 样本")


def _measure(draw, text: str, font) -> tuple[float, float]:
    """量出文字真实宽高（不写死偏移，字体/语言换了也不会裁）。"""
    box = draw.textbbox((0, 0), text, font=font)
    return float(box[2] - box[0]), float(box[3] - box[1])


def _draw_text(draw, xy, text: str, font, fill: str) -> tuple[float, float]:
    """左上角定位（先量后画，返回实际宽高）。"""
    draw.text(xy, text, fill=fill, font=font)
    return _measure(draw, text, font)


def _series(samples, axes, lay):
    """三条曲线（温度/功率/目标，目标是分段列表）的坐标序列。"""
    t0, t1 = min(s.ts for s in samples), max(s.ts for s in samples)
    xs = [chart.x_time(s.ts, t0, t1, lay) for s in samples]
    temps = [chart.y_temp(s.temp, lay, axes.temp_lo, axes.temp_hi) for s in samples]
    powers = [chart.y_power(s.power, lay, axes.power_max) for s in samples]
    target_segments: list[tuple[list[float], list[float]]] = []
    seg_x: list[float] = []
    seg_y: list[float] = []
    for s, x in zip(samples, xs):
        if s.heating:
            seg_x.append(x)
            seg_y.append(chart.y_temp(s.target, lay, axes.temp_lo, axes.temp_hi))
        elif seg_x:
            target_segments.append((seg_x, seg_y))
            seg_x, seg_y = [], []
    if seg_x:
        target_segments.append((seg_x, seg_y))
    return xs, temps, powers, target_segments


def _draw_header(draw, lay, width, scale, title: str, font_title, font_small):
    """标题（左上）+ 图例（右上）；放不下就先缩小图例字号，再换行，绝不压到绘图区。"""
    title_w, title_h = _draw_text(draw, (lay.left, 6 * scale / 2), title,
                                  font_title, "#111111")
    items = ((COLOR_TEMP, "实测温度"), (COLOR_TARGET, "目标温度"), (COLOR_POWER, "输出功率"))
    swatch = 18 * scale / 2
    gap = 8 * scale / 2
    font_legend = font_small
    for _attempt in range(2):
        widths = [_measure(draw, text, font_legend)[0] for _c, text in items]
        total = sum(swatch + gap + w for w in widths) + gap * (len(items) - 1)
        x = width - 8 * scale / 2 - total
        y = 6 * scale / 2 + title_h + 4 * scale / 2
        if x >= lay.left + title_w + 10 * scale / 2:
            break
        font_legend = _font(int(8 * scale))      # 挤了就缩小图例一号
    for (color, text), tw in zip(items, widths):
        mid = y + _measure(draw, text, font_legend)[1] / 2
        draw.line([(x, mid), (x + swatch, mid)], fill=color, width=max(1, int(2 * scale / 2)))
        draw.text((x + swatch + gap, y), text, fill="#555555", font=font_legend)
        x += swatch + gap + tw + gap


def _draw_grid(draw, lay, axes, scale, font_small, width):
    """网格 + 左右刻度（先画网格，曲线才能盖在上面；否则正好压在某条网格上的
    目标温度虚线会被网格吃掉）。所有文字都量过尺寸，保证完整落在画布内。"""
    draw.rectangle([lay.left, lay.top, lay.right, lay.bottom], outline="#dddddd")

    # 左轴：温度（最上面一个带轴名）
    value = axes.temp_lo
    while value <= axes.temp_hi + 1e-9:
        y = chart.y_temp(value, lay, axes.temp_lo, axes.temp_hi)
        draw.line([(lay.left, y), (lay.right, y)], fill=COLOR_GRID, width=1)
        is_top = value + axes.temp_step > axes.temp_hi
        text = f"{value:g}" + (" ℃" if is_top else "")
        tw, th = _measure(draw, text, font_small)
        draw.text((max(2.0, lay.left - 4 * scale - tw), y - th / 2),
                  text, fill=COLOR_TEMP, font=font_small)
        value += axes.temp_step

    # 右轴：功率（最上面一个带轴名）
    for frac in (0.0, 0.5, 1.0):
        val = axes.power_max * frac
        y = chart.y_power(val, lay, axes.power_max)
        text = f"{val:g}" + (" W" if frac == 1.0 else "")
        tw, th = _measure(draw, text, font_small)
        x = min(width - tw - 2, lay.right + 4 * scale)
        draw.text((x, y - th / 2), text, fill=COLOR_POWER, font=font_small)

def _draw_time_axis(draw, lay, samples, scale, font_small):
    """X 轴时间刻度（不足 2 个样本时给 --:--）。"""
    ticks = chart.time_ticks(samples, 5)
    baseline = lay.bottom + 4 * scale
    if not ticks:
        text = "--:--"
        tw, _th = _measure(draw, text, font_small)
        draw.text(((lay.left + lay.right - tw) / 2, baseline), text,
                  fill="#999999", font=font_small)
        return
    for frac, label in ticks:
        x = lay.left + frac * lay.plot_w
        tw, _th = _measure(draw, label, font_small)
        left = min(max(x - tw / 2, 2.0), lay.width - tw - 2.0)
        draw.line([(x, lay.bottom), (x, lay.bottom + 3 * scale)], fill="#bbbbbb")
        draw.text((left, baseline), label, fill=COLOR_MUTED, font=font_small)


def render_png(path: str, samples: Sequence[chart.Sample], device_name: str = "HP20",
               scale: int = SCALE) -> tuple[int, int]:
    """离屏渲染 PNG，返回 (宽, 高) 像素。Pillow 不可用时抛 RuntimeError。"""
    if Image is None:
        raise RuntimeError(f"Pillow 不可用: {PILLOW_ERR}")
    samples = list(samples)
    width = int(BASE_W * scale)
    height = int(BASE_H * scale)
    img = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(img)
    lay = export_layout(width, height, scale)
    axes = chart.dual_axis(samples)
    font_title = _font(int(11 * scale))
    font_small = _font(int(8 * scale))

    _draw_header(draw, lay, width, scale, _title(samples, device_name),
                 font_title, font_small)
    _draw_grid(draw, lay, axes, scale, font_small, width)

    xs, temps, powers, target_segments = _series(samples, axes, lay)
    for seg_x, seg_y in target_segments:
        if len(seg_x) >= 2:
            draw.line(list(zip(seg_x, seg_y)), fill=COLOR_TARGET, width=max(1, scale // 2))
    if len(xs) >= 2:
        draw.line(list(zip(xs, temps)), fill=COLOR_TEMP, width=scale)
        draw.line(list(zip(xs, powers)), fill=COLOR_POWER, width=scale)

    _draw_time_axis(draw, lay, samples, scale, font_small)

    img.save(path, dpi=(DPI, DPI))
    return width, height


def render_svg(path: str, samples: Sequence[chart.Sample],
               device_name: str = "HP20") -> tuple[int, int]:
    """降级方案：手拼 SVG（无损、可缩放）。返回 (宽, 高)。"""
    samples = list(samples)
    width = int(BASE_W * SCALE)
    height = int(BASE_H * SCALE)
    lay = export_layout(width, height, SCALE)
    axes = chart.dual_axis(samples)
    xs, temps, powers, target_segments = _series(samples, axes, lay)
    base = 8 * SCALE

    def _pts(px, py):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(px, py))

    def _text(x, y, body, size, fill, anchor="start"):
        return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
                f'text-anchor="{anchor}">{_escape(body)}</text>')

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        f'<rect width="100%" height="100%" fill="#ffffff"/>',
        _text(lay.left, 6 * SCALE + base, _title(samples, device_name), 11 * SCALE,
              "#111111"),
        f'<rect x="{lay.left}" y="{lay.top}" width="{lay.plot_w}" '
        f'height="{lay.plot_h}" fill="none" stroke="#dddddd"/>',
    ]
    # 左轴刻度：最上面一个带轴名 ℃（文字靠右对齐，天然不会越界）
    value = axes.temp_lo
    while value <= axes.temp_hi + 1e-9:
        y = chart.y_temp(value, lay, axes.temp_lo, axes.temp_hi)
        is_top = value + axes.temp_step > axes.temp_hi
        parts.append(f'<line x1="{lay.left}" y1="{y:.1f}" x2="{lay.right}" '
                     f'y2="{y:.1f}" stroke="#eeeeee"/>')
        parts.append(_text(lay.left - 4 * SCALE, y + base / 2,
                           f"{value:g}" + (" ℃" if is_top else ""), base,
                           COLOR_TEMP, anchor="end"))
        value += axes.temp_step
    # 右轴刻度
    for frac in (0.0, 0.5, 1.0):
        val = axes.power_max * frac
        y = chart.y_power(val, lay, axes.power_max)
        parts.append(_text(lay.right + 4 * SCALE, y + base / 2,
                           f"{val:g}" + (" W" if frac == 1.0 else ""), base,
                           COLOR_POWER))
    # X 轴时间刻度（底边距 70px @2x，放得下）
    ticks = chart.time_ticks(samples, 5)
    if ticks:
        for frac, label in ticks:
            x = lay.left + frac * lay.plot_w
            parts.append(_text(x, lay.bottom + 4 * SCALE + base, label, base,
                               COLOR_MUTED, anchor="middle"))
    else:
        parts.append(_text((lay.left + lay.right) / 2, lay.bottom + 4 * SCALE + base,
                           "--:--", base, "#999999", anchor="middle"))
    for seg_x, seg_y in target_segments:
        if len(seg_x) >= 2:
            parts.append(f'<polyline points="{_pts(seg_x, seg_y)}" fill="none" '
                         f'stroke="{COLOR_TARGET}" stroke-width="1" '
                         f'stroke-dasharray="4 3"/>')
    if len(xs) >= 2:
        parts.append(f'<polyline points="{_pts(xs, temps)}" fill="none" '
                     f'stroke="{COLOR_TEMP}" stroke-width="{SCALE}"/>')
        parts.append(f'<polyline points="{_pts(xs, powers)}" fill="none" '
                     f'stroke="{COLOR_POWER}" stroke-width="{SCALE}"/>')
    parts.append("</svg>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    return width, height


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def save_chart(path: str, samples: Sequence[chart.Sample], device_name: str = "HP20"
               ) -> tuple[bool, str]:
    """保存图表：返回 (成功?, 给状态栏的一句话)。

    - 数据不足 -> 不写空图，直接给提示；
    - .svg -> SVG；.png 且 Pillow 可用 -> PNG；.png 但 Pillow 缺失 -> 自动降级写 .svg 并提示；
    - 任何异常都不静默：返回失败文案。
    """
    samples = list(samples)
    if len(samples) < 2:
        return False, "没有数据可保存：先让设备上报几帧再试"
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".svg":
            width, height = render_svg(path, samples, device_name=device_name)
            return True, f"已保存 SVG 图表 {path}（{width}×{height}）"
        if Image is None:
            svg_path = os.path.splitext(path)[0] + ".svg"
            width, height = render_svg(svg_path, samples, device_name=device_name)
            return True, (f"Pillow 不可用（{PILLOW_ERR}），已改存 SVG：{svg_path}"
                          f"（{width}×{height}）")
        width, height = render_png(path, samples, device_name=device_name)
        return True, f"已保存 PNG 图表 {path}（{width}×{height}, {DPI} DPI）"
    except Exception as exc:  # noqa: BLE001
        return False, f"保存图表失败：{exc}"
