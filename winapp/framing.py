"""HP20 帧层：组帧、LRC、长度驱动的流式解析（设备无关）。

帧结构（docs/hp20_protocol.md §4）：
    <header> <dev_id> <family> <cmd> <length> <payload[length]> <LRC>
    header = 0xAF(请求) / 0xDF(应答·通知)，总长 = 6 + length
    LRC    = (256 - (sum(所有 LRC 之前的字节) & 0xFF)) & 0xFF   ← 含 header
    dev_id | family<<8 即 16 位型号头（HP20 = 0x0209）

本模块不含任何机型常量：dev_id/family 由调用方给出；帧头等字面量统一派生自
proto_const.Proto（唯一真源），这里只做流式解析。
"""
from __future__ import annotations

from dataclasses import dataclass

from proto_const import Proto      # 帧头/长度上限的唯一真源


REQ_HEADER = Proto.REQ_HEADER
RESP_HEADER = Proto.RESP_HEADER
MIN_FRAME_LEN = Proto.MIN_FRAME_LEN
MAX_PAYLOAD = Proto.MAX_PAYLOAD


def compute_lrc(data: bytes) -> int:
    return (256 - (sum(data) & 0xFF)) & 0xFF


def to_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in bytes(data))


def build_frame(cmd: int, payload: bytes = b"", *, dev_id: int, family: int,
                header: int = REQ_HEADER) -> bytes:
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload 超过 {MAX_PAYLOAD} 字节: {len(payload)}")
    if not (0 <= dev_id <= 0xFF and 0 <= family <= 0xFF and 0 <= cmd <= 0xFF):
        raise ValueError("dev_id/family/cmd 必须是单字节")
    head = bytes((header, dev_id, family, cmd, len(payload))) + payload
    return head + bytes((compute_lrc(head),))


@dataclass(frozen=True)
class Frame:
    header: int
    dev_id: int
    family: int
    cmd: int
    payload: bytes
    raw: bytes

    @property
    def dev_header(self) -> int:
        return self.dev_id | (self.family << 8)

    @property
    def is_response(self) -> bool:
        return self.header == RESP_HEADER

    def hex(self) -> str:
        return to_hex(self.raw)

    def __str__(self) -> str:
        return (f"Frame({'RSP' if self.is_response else 'REQ'} "
                f"dev=0x{self.dev_header:04X} cmd=0x{self.cmd:02X} "
                f"payload={to_hex(self.payload)})")


def parse_frame(raw: bytes) -> Frame:
    """严格解析单帧；长度字段或 LRC 不符时抛 ValueError。"""
    raw = bytes(raw)
    if len(raw) < MIN_FRAME_LEN:
        raise ValueError(f"帧过短({len(raw)}): {to_hex(raw)}")
    if raw[0] not in (REQ_HEADER, RESP_HEADER):
        raise ValueError(f"未知 header {raw[0]:02X}: {to_hex(raw)}")
    length = raw[4]
    if len(raw) != MIN_FRAME_LEN + length:
        raise ValueError(f"长度字段={length} 但帧长={len(raw)}: {to_hex(raw)}")
    if raw[-1] != compute_lrc(raw[:-1]):
        raise ValueError(f"LRC 不符(应为 {compute_lrc(raw[:-1]):02X}): {to_hex(raw)}")
    return Frame(raw[0], raw[1], raw[2], raw[3], raw[5:5 + length], raw)


class FrameParser:
    """长度驱动的应答流解析器（FrameParser::feed @0x43565c 语义）。

    - 跨 notify 分包累积：不足一帧就等下一包
    - 单包多帧：循环产出
    - 帧前垃圾字节丢弃；LRC 错帧静默丢弃（不产出、不报错）
    - LRC 错时逐字节重同步：App 是整帧丢弃，但长度字段被写坏时那样会把后面的
      好帧一起吃掉，这里取更稳的重同步，对外行为同样只是"坏帧不产出"
    """

    def __init__(self, header: int = RESP_HEADER, max_payload: int = MAX_PAYLOAD) -> None:
        self._buf = bytearray()
        self._header = bytes((header,))
        self._max_payload = max_payload
        self.dropped = 0     # 被丢弃的垃圾字节
        self.bad_lrc = 0     # LRC/长度字段非法次数
        self.frames = 0      # 成功产出的帧数

    @property
    def pending(self) -> int:
        return len(self._buf)

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buf.extend(chunk)
        out: list[Frame] = []
        while True:
            idx = self._buf.find(self._header)
            if idx < 0:
                self.dropped += len(self._buf)
                self._buf.clear()
                return out
            if idx:
                self.dropped += idx
                del self._buf[:idx]
            if len(self._buf) < 5:
                return out
            length = self._buf[4]
            if length > self._max_payload:
                self.bad_lrc += 1
                self.dropped += 1
                del self._buf[:1]
                continue
            total = MIN_FRAME_LEN + length
            if len(self._buf) < total:
                return out
            cand = bytes(self._buf[:total])
            if cand[-1] == compute_lrc(cand[:-1]):
                out.append(Frame(cand[0], cand[1], cand[2], cand[3],
                                 cand[5:5 + length], cand))
                self.frames += 1
                del self._buf[:total]
                continue
            self.bad_lrc += 1
            self.dropped += 1
            del self._buf[:1]
