"""帧诊断：把一帧原始字节拆成「字节带 + 逐字段解析表」（纯文本，无 Tk 依赖）。

诊断面板用它展示**最近一帧**（发送/接收各一块）：先给出原始字节带，
再按命令给出每段的偏移/长度/字段/类型/原始字节/解码值。

设计取舍：Tk 的 Text 不支持合并单元格，所以分成两块 ——
  ① 字节带：索引行 + 字节行，等宽列对齐，每行 32 字节（HP20 实际帧最长 26 字节，一律单行）；
  ② 字段表：一个字段一行，天然支持跨字节（温度 2 字节、名字 20 字节都不挤）。
未知命令只给字节带 + 一行说明，**绝不硬解 payload**。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import framing
import hp20_commands as hc

BYTES_PER_LINE = 32          # 固定 32，不按窗口宽度动态算（复制的文本要可逐字比对）
RAW_DISPLAY_MAX = 6          # 字段表里「原始字节」最多显示几个（再长就看字节带）

_CMD_NAMES = hc.COMMAND_NAMES
# 写命令的应答只是 1 字节 ack，不走「写参数」的字段布局
_WRITE_CMDS = {hc.CMD_SET_THERMOSTAT, hc.CMD_SET_PREHEAT, hc.CMD_SET_REFLOW,
               hc.CMD_SET_NAME, hc.CMD_SET_REPAIR}


@dataclass(frozen=True)
class Field:
    """一个字段：原始字节 + 解码文本。"""
    offset: int
    length: int
    name: str
    type_text: str
    raw: bytes
    decoded: str


# ------------------------------------------------------------------ 小工具
def _disp_width(text: str) -> int:
    """显示宽度（中文占 2 格），用于在等宽字体里对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, align: str = "l") -> str:
    gap = max(0, width - _disp_width(text))
    return (" " * gap + text) if align == "r" else (text + " " * gap)


def _u16(raw: bytes, offset: int) -> int:
    return int.from_bytes(raw[offset:offset + 2], "little", signed=False)


def _i16(raw: bytes, offset: int) -> int:
    return int.from_bytes(raw[offset:offset + 2], "little", signed=True)


def _hex(raw: bytes) -> str:
    return framing.to_hex(raw)


def _mode_text(mode: int) -> str:
    return f"{hc.MODE_NAMES.get(mode, '未知')}({mode})"


def _slot_text(mode: int, slot: int) -> str:
    names = hc.MODE_SLOT_NAMES.get(mode)
    if names and 0 <= slot < len(names):
        return names[slot]
    return f"字段值 {slot}"


# ------------------------------------------------------------------ 帧头 / LRC
def _header_fields(raw: bytes) -> list[Field]:
    sof = raw[0]
    dev = raw[1] | (raw[2] << 8)
    cmd = raw[3]
    length = raw[4]
    actual = max(0, len(raw) - 6)
    len_note = f"{length} 字节"
    if actual != length:
        len_note += f"（实际 {actual}，长度不符）"
    return [
        Field(0, 1, "SOF 帧头", "u8", raw[0:1],
              {framing.REQ_HEADER: "请求 (TX→设备)",
               framing.RESP_HEADER: "应答/通知 (RX←设备)"}.get(sof, f"未知 0x{sof:02X}")),
        Field(1, 2, "设备头 dev_id|family<<8", "u16le", raw[1:3],
              f"{hc.DEV_TYPES.get(dev, '未知机型')} (0x{dev:04X})"),
        Field(3, 1, "命令 cmd", "u8", raw[3:4],
              f"0x{cmd:02X} {_CMD_NAMES.get(cmd, '未知命令')}"),
        Field(4, 1, "payload 长度", "u8", raw[4:5], len_note),
    ]


def _lrc_field(raw: bytes) -> Field:
    want = framing.compute_lrc(raw[:-1])
    got = raw[-1]
    return Field(len(raw) - 1, 1, "LRC 校验", "u8", raw[-1:],
                 "ok" if got == want else f"错（应为 0x{want:02X}）")


def _short(cmd: int, payload: bytes, expect: int) -> tuple[list[Field], str]:
    field = Field(5, len(payload), "payload（不完整）", f"期望 {expect} 字节",
                  bytes(payload), f"收到 {len(payload)} 字节")
    return [field], f"0x{cmd:02X} 的 payload 不足 {expect} 字节，未按字段解析"


# ------------------------------------------------------------------ payload 布局
def _field_8x16(fields_order, payload: bytes, base: int) -> list[Field]:
    out: list[Field] = []
    for i, name in enumerate(fields_order):
        offset = base + i * 2
        _lo, _hi, unit = hc.FIELD_LIMITS[name]
        out.append(Field(offset, 2, f"{name}({unit})", "u16le", payload[i * 2:i * 2 + 2],
                         f"{_u16(payload, i * 2)}{unit}"))
    return out


def _payload_fields(raw: bytes, cmd: int) -> tuple[list[Field], str]:
    """返回 (payload 字段, 备注)。空 payload / 未知命令 / 长度不符都只给说明。"""
    payload = raw[5:-1]
    is_tx = raw[0] == framing.REQ_HEADER
    if not payload:
        return [], ""
    if not is_tx and cmd in _WRITE_CMDS:
        return [Field(5, len(payload), "应答 ack", "u8", bytes(payload),
                      "成功" if payload[0] == 0 else f"0x{payload[0]:02X}")], ""

    if cmd == hc.CMD_IDENTIFY:
        dev = payload[0] | (payload[1] << 8) if len(payload) >= 2 else None
        if dev is None:
            return _short(cmd, payload, 2)
        is_hp20 = len(payload) >= 2 and payload[:2] == hc.IDENT_PAYLOAD
        return [Field(5, 2, "型号 payload", "dev_id+family", payload[0:2],
                      f"HP20 (0x{dev:04X})" if is_hp20 else f"非 HP20 (0x{dev:04X})")], ""

    if cmd == hc.CMD_SET_THERMOSTAT and is_tx:
        if len(payload) < 3:
            return _short(cmd, payload, 3)
        index = payload[0]
        index_text = ("目标温度 T5" if index == hc.TARGET_INDEX
                      else f"T{index + 1}" if 0 <= index < hc.TARGET_INDEX
                      else f"非法 index {index}")
        return [Field(5, 1, "index", "u8", payload[0:1], index_text),
                Field(6, 2, "温度 temp", "i16le", payload[1:3],
                      f"{_i16(payload, 1)}℃")], ""

    if cmd == hc.CMD_SET_PREHEAT and is_tx:
        channel = payload[0]
        text = ("T4（停止通道，固定 0℃）" if channel == hc.STOP_CHANNEL
                else f"T{channel + 1}" if 0 <= channel <= 3 else f"非法 channel {channel}")
        return [Field(5, 1, "channel", "u8", payload[0:1], text)], ""

    if cmd in (hc.CMD_GET_THERMOSTAT, hc.CMD_GET_REFLOW, hc.CMD_GET_REPAIR) and not is_tx:
        expect = 8 if cmd == hc.CMD_GET_THERMOSTAT else 16
        if len(payload) < expect:
            return _short(cmd, payload, expect)
        out: list[Field] = []
        if cmd == hc.CMD_GET_THERMOSTAT:
            for i in range(4):
                name = hc.PRESET_NAMES[i] + ("（停止通道，固定 0℃）"
                                             if i == hc.STOP_CHANNEL else "")
                out.append(Field(5 + i * 2, 2, f"预设 T{i + 1}", "i16le",
                                 payload[i * 2:i * 2 + 2], f"{_i16(payload, i * 2)}℃ [{name}]"))
            return out, ""
        order = hc.REFLOW_FIELDS if cmd == hc.CMD_GET_REFLOW else hc.REPAIR_FIELDS
        return _field_8x16(order, payload, 5), ""

    if cmd == hc.CMD_SET_REFLOW and is_tx:
        if len(payload) < 16:
            return _short(cmd, payload, 16)
        return _field_8x16(hc.REFLOW_FIELDS, payload, 5), ""

    if cmd == hc.CMD_SET_REPAIR and is_tx:
        if len(payload) < 16:
            return _short(cmd, payload, 16)
        return _field_8x16(hc.REPAIR_FIELDS, payload, 5), ""

    if cmd == hc.CMD_SET_NAME and is_tx:
        text = hc.decode_name(payload)
        return [Field(5, len(payload), "蓝牙名", f"ASCII×{len(payload)}", bytes(payload),
                      repr(text))], ""

    if cmd == hc.CMD_GET_NAME and not is_tx:
        return [Field(5, len(payload), "蓝牙名", f"ASCII×{len(payload)}", bytes(payload),
                      repr(hc.decode_name(payload)))], ""

    if cmd == hc.CMD_REALTIME and not is_tx:
        if len(payload) < 11:
            return _short(cmd, payload, 11)
        w1 = _u16(payload, 7)
        w2 = _u16(payload, 9)
        mode = (w1 >> 12) & 7
        return [
            Field(5, 2, "当前温度", "i16le", payload[0:2], f"{_i16(payload, 0)}℃"),
            Field(7, 1, "供电功率", "u8", payload[2:3], f"{payload[2]}W"),
            Field(8, 2, "输入电压", "i16le", payload[3:5],
                  f"{_i16(payload, 3) / 10:.1f}V"),
            Field(10, 1, "输出状态", "u8", payload[5:6],
                  {0x00: "待机", 0x02: "加热模式运行中"}.get(payload[5], f"未知 0x{payload[5]:02X}")),
            Field(11, 1, "当前功率", "u8", payload[6:7], f"{payload[6]}W"),
            Field(12, 2, "W1 目标/挡位/模式", "i16le", payload[7:9],
                  f"目标 {w1 & 0x3FF}℃ / 挡位 {_slot_text(mode, (w1 >> 10) & 3)} / "
                  f"模式 {_mode_text(mode)}"),
            Field(14, 2, "W2 计时（64 进制 MM:SS）", "i16le", payload[9:11],
                  f"{hc.format_timer(w2)}（0x{w2:04X}={w2}）"),
        ], ""

    return [], f"未知命令 0x{cmd:02X}：payload 未解析（共 {len(payload)} 字节）"


# ------------------------------------------------------------------ 对外
def describe(raw: bytes) -> tuple[list[Field], str]:
    """拆一帧：返回 (字段列表, 备注)。"""
    raw = bytes(raw)
    if len(raw) < 6:
        return [], f"帧太短（{len(raw)} 字节），只显示字节带"
    fields = _header_fields(raw)
    payload_fields, note = _payload_fields(raw, raw[3])
    fields += payload_fields
    fields.append(_lrc_field(raw))
    return fields, note


def byte_band(raw: bytes) -> list[str]:
    """字节带：索引行 + 字节行，每行 32 字节。"""
    raw = bytes(raw)
    lines = ["字节带"]
    for start in range(0, len(raw), BYTES_PER_LINE):
        chunk = raw[start:start + BYTES_PER_LINE]
        index_row = "".join(_pad(str(start + i), 4, "r") for i in range(len(chunk)))
        hex_row = "".join(_pad(f"{b:02X}", 4, "r") for b in chunk)
        lines.append("  偏移" + index_row)
        lines.append("  字节" + hex_row)
    return lines


def field_table(fields: list[Field], note: str = "") -> list[str]:
    """字段表：一个字段一行。"""
    widths = (4, 4, 26, 10, 14)
    head = (_pad("偏移", widths[0], "r") + "  " + _pad("长度", widths[1], "r") + "  "
            + _pad("字段", widths[2]) + "  " + _pad("类型", widths[3]) + "  "
            + _pad("原始字节", widths[4]) + "  " + "解码")
    lines = ["字段解析", "  " + head, "  " + "-" * _disp_width(head)]
    for f in fields:
        shown = f.raw[:RAW_DISPLAY_MAX]
        raw_text = _hex(shown) + (" …" if len(f.raw) > RAW_DISPLAY_MAX else "")
        lines.append("  " + _pad(str(f.offset), widths[0], "r") + "  "
                     + _pad(str(f.length), widths[1], "r") + "  "
                     + _pad(f.name, widths[2]) + "  " + _pad(f.type_text, widths[3]) + "  "
                     + _pad(raw_text, widths[4]) + "  " + f.decoded)
    if note:
        lines.append(f"  注：{note}")
    return lines


def render(raw: bytes) -> list[str]:
    """字节带 + 字段表（诊断面板与「复制全部」共用同一份文本）。"""
    fields, note = describe(raw)
    return byte_band(raw) + [""] + field_table(fields, note)
