"""HP20 命令表与状态解码 —— 全应用唯一的协议常量来源。

依据：protocol/hp20_commands.json + docs/hp20_protocol.md（A1 逆向）
     + 真机实测标定（2026-10-17，HP20 AA:BB:CC:DD:EE:FF）
冲突时**以实测为准**，文档值保留在常量/注释里以便回溯。

实测要点（与文档不同处）：
  - 帧头 <header> <dev_id> <family> <cmd> <length> <payload> <LRC>，请求侧 09 02；
    识别应答帧头是 FF FF（DF FF FF 00 02 09 02 16），型号只看 payload 前两字节。
  - 写命令应答带 1 字节 payload。
  - 实时状态 payload[0:2] 才是**真实当前温度**；[3:5] 是**输入电压 ×0.1V**（200=20.0V、278=27.8V）；
    [2] 是**供电功率 W**（65→140 随换电源变化），[6] 是**当前输出功率 W**。
  - mode：(W1>>12)&7 实测 0=待机、2=加热模式（功率可为 0）；文档「0/1=加热、2=回流」不成立。
  - 官方 APP 只有 T1/T2/T3 三个挡位；**T4 是隐藏的第 4 挡、预设固定 0℃，选它等于停止加热**。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable, Sequence

from framing import RESP_HEADER, build_frame
from proto_const import Proto

# ------------------------------------------------------------------ 设备头
# 全部派生自 proto_const.Proto：这里不再写任何字面量（改一处即全局生效）
DEV_ID = Proto.DEV_ID
FAMILY = Proto.FAMILY
DEV_HEADER = Proto.DEV_HEADER

DEV_TYPES: dict[int, str] = {
    0x0207: "HP15",
    DEV_HEADER: "HP20",          # 用派生常量做键，型号头不再出现第二处字面量
    0x0305: "DM40",
    0x0306: "ND1",
    0x0307: "EL15",
}

# ------------------------------------------------------------------ 命令字
CMD_IDENTIFY = 0x00        # 握手/读型号     -> payload[0:2] = 09 02
CMD_SET_THERMOSTAT = 0x03  # [index, i16le]  index 0..3=T1..T4, 4=T5(目标温度)
CMD_GET_THERMOSTAT = 0x04  # -> 4 x i16le
CMD_SET_PREHEAT = 0x05     # [channel 0..3]  启动/选预热通道
CMD_SET_REFLOW = 0x06      # 8 x u16le
CMD_GET_REFLOW = 0x07      # -> 8 x u16le
CMD_SET_NAME = 0x08        # 20 字节 ASCII 补 0
CMD_GET_NAME = 0x09        # -> ASCII 去 0x00
CMD_REALTIME = 0x0A        # -> 实时状态，payload >= 11 字节
CMD_SET_REPAIR = 0x0B      # 8 x u16le
CMD_GET_REPAIR = 0x0C      # -> 8 x u16le

COMMAND_NAMES: dict[int, str] = {
    CMD_IDENTIFY: "识别/读型号",
    CMD_SET_THERMOSTAT: "设置预设温度",
    CMD_GET_THERMOSTAT: "读取预设温度",
    CMD_SET_PREHEAT: "启动预热通道",
    CMD_SET_REFLOW: "写回流曲线",
    CMD_GET_REFLOW: "读回流曲线",
    CMD_SET_NAME: "写蓝牙名",
    CMD_GET_NAME: "读蓝牙名",
    CMD_REALTIME: "读实时状态",
    CMD_SET_REPAIR: "写维修参数",
    CMD_GET_REPAIR: "读维修参数",
}

# ---------------------------------------------------------------- 加热计时（W2）
# 真机测速：W2 序列 1268 -> 1229 共 40 个变化点 / 39.23 s，即每真实秒 -1 个**单位**。
# 这个单位**不是秒**：W2 是 64 进制打包的 MM:SS（每分钟 64 个单位），与官方 APP heartTimeStr 一致。
#   设置 25 min -> W2=1600 = 25 × 64 -> 25:00
#   设置 20 min -> W2=1280 = 20 × 64 -> 20:00
# ⚠ 曾经按「1 单位 = 1 秒」当总秒数换算（1229 -> 20:29）是**错的**：1229 实际是 19:13。
# 同一个 W2 字段，标签随模式变化（用户对照官方 APP 观察到）：
#   1 回流焊 / 2 加热 / 4 维修 -> 「加热时间」
#   0 待机 / 3 设置 / 8 主页   -> 「休眠倒计时」
TIMER_LABEL_HEAT = "加热时间"
TIMER_LABEL_SLEEP = "休眠倒计时"

# W2 是 **64 进制打包的 MM:SS**（每分钟 64 个单位，每真实秒 -1），
# 不是 60 进制、也不是总秒数 —— 这是官方 APP heartTimeStr 的做法，也是实测结论。
# 决定性锚点（用户口述设置值 vs 我们读到的原始 W2）：
#   设置 25 min -> W2=1600 = 25 × 64 -> MM=1600>>6=25, SS=1600&0x3F=0 -> "25:00"
#   设置 20 min -> W2=1280 = 20 × 64 -> MM=20,      SS=0          -> "20:00"
# 两个设置值精确等于「分钟 × 64」，不可能是巧合。
TIMER_OFF_SENTINEL = Proto.TIMER_OFF_SENTINEL   # 10687：低位 SS=63 >= 60，即 OFF 判定


def format_timer(w2: int) -> str:
    """W2 -> 'MM:SS'（64 进制打包）；SS >= 60 表示计时未开启 -> 'OFF'。"""
    mm = (w2 >> 6) & 0x7F
    ss = w2 & 0x3F
    if ss >= 60:
        return "OFF"
    return f"{mm:02d}:{ss:02d}"


# 识别判据：payload 前两字节 == 09 02（帧头不可信，实测 FF FF）
IDENT_PAYLOAD = Proto.IDENT_PAYLOAD
ACK_MIN_PAYLOAD = Proto.ACK_MIN_PAYLOAD

# ------------------------------------------------------------------ 时序
PROTOCOL_TIMEOUT_S = Proto.PROTOCOL_TIMEOUT_S   # 文档值（反汇编 0x3E8），仅作对照
RESPONSE_TIMEOUT_S = Proto.RESPONSE_TIMEOUT_S   # 运行时默认：真机会延迟 0.7~1.5s
POLL_INTERVAL_S = Proto.POLL_INTERVAL_S
TIMEOUT_TICK_S = Proto.TIMEOUT_TICK_S
WRITE_GAP_S = Proto.WRITE_GAP_S
POST_CONNECT_DELAY_S = Proto.POST_CONNECT_DELAY_S
RECONNECT_BACKOFF_S = Proto.RECONNECT_BACKOFF_S
RECONNECT_MAX_ATTEMPTS = Proto.RECONNECT_MAX_ATTEMPTS
NAME_LEN = Proto.NAME_LEN

# ------------------------------------------------------------------ 字段标签（UI）
REFLOW_LABELS: tuple[tuple[str, str], ...] = (
    ("预热温度", "℃"), ("预热时间", "s"),
    ("恒温温度", "℃"), ("恒温时间", "s"),
    ("回流温度", "℃"), ("回流时间", "s"),
    ("冷却温度", "℃"), ("冷却时间", "s"),
)
# U4 已结案：用户在设备面板翻到 4 组名称 —— 除胶 / 分层 / 贴合 / 植球
REPAIR_GROUPS = ("除胶(Debond)", "分层(Delam)", "贴合(LAM)", "植球(Reballing)")
REPAIR_LABELS: tuple[tuple[str, str], ...] = (
    (f"{REPAIR_GROUPS[0]}温度", "℃"), (f"{REPAIR_GROUPS[0]}时间", "s"),
    (f"{REPAIR_GROUPS[1]}温度", "℃"), (f"{REPAIR_GROUPS[1]}时间", "s"),
    (f"{REPAIR_GROUPS[2]}温度", "℃"), (f"{REPAIR_GROUPS[2]}时间", "s"),
    (f"{REPAIR_GROUPS[3]}温度", "℃"), (f"{REPAIR_GROUPS[3]}时间", "s"),
)
PRESET_NAMES = ("T1", "T2", "T3", "T4")
TARGET_INDEX = 4           # T5 = 主界面"目标温度"

# ------------------------------------------------------------------ 输入范围（用户手册）
# 控温 40~350℃；回流/维修各字段严格按手册范围 —— 只在这张表里定义，界面标签与
# 写命令校验都从这里派生，避免「提示写 120~180、代码却按 0~250 放行」。
TEMP_MIN_C = 40
TEMP_MAX_C = 350

FIELD_LIMITS: dict[str, tuple[float, float, str]] = {
    # 回流焊
    "预热温度": (120, 180, "℃"),
    "恒温温度": (150, 220, "℃"),
    "回流温度": (230, 300, "℃"),
    "冷却温度": (50, 80, "℃"),
    "预热时间": (60, 300, "s"),
    "恒温时间": (30, 300, "s"),
    "回流时间": (30, 300, "s"),
    "冷却时间": (300, 480, "s"),
    # 维修
    "除胶温度": (60, 150, "℃"),
    "分层温度": (60, 260, "℃"),
    "贴合温度": (60, 260, "℃"),
    "植球温度": (40, 300, "℃"),
    "除胶时间": (10, 300, "s"),
    "分层时间": (10, 300, "s"),
    "贴合时间": (10, 300, "s"),
    "植球时间": (10, 300, "s"),
}

# 与 0x06 / 0x0B payload 的 8 个字段一一对应（每阶段：温度在前、时间在后）
REFLOW_FIELDS = ("预热温度", "预热时间", "恒温温度", "恒温时间",
                 "回流温度", "回流时间", "冷却温度", "冷却时间")
REPAIR_FIELDS = ("除胶温度", "除胶时间", "分层温度", "分层时间",
                 "贴合温度", "贴合时间", "植球温度", "植球时间")


# 手册出厂默认值（「重置默认参数」用，单一来源）
REFLOW_DEFAULTS = (150, 120, 180, 90, 260, 60, 80, 300)
REPAIR_DEFAULTS = (100, 120, 80, 120, 80, 120, 260, 120)


def field_range_text(name: str) -> str:
    """界面标签用：'预热温度 120–180℃'。"""
    lo, hi, unit = FIELD_LIMITS[name]
    return f"{name} {lo:g}–{hi:g}{unit}"


def validate_fields(values: Sequence[int], fields: Sequence[str]) -> tuple[bool, str]:
    """按 FIELD_LIMITS 校验一组字段值；返回 (是否合法, 中文原因)。

    整条命令的字段必须**全部**合法才允许下发（回流/维修各 8 个值）。
    """
    if len(values) != len(fields):
        return False, f"字段数量不对：需要 {len(fields)} 个，收到 {len(values)}"
    for value, name in zip(values, fields):
        lo, hi, unit = FIELD_LIMITS[name]
        if not (lo <= value <= hi):
            return False, f"{name}需在 {lo:g}~{hi:g}{unit} 之间（当前 {value}）"
    return True, ""

# ------------------------------------------------------------------ 编码 helper
def pack_u16(values: Iterable[int]) -> bytes:
    out = bytearray()
    for v in values:
        v = int(v)
        if not -0x8000 <= v <= 0xFFFF:
            raise ValueError(f"数值超出 16 位范围: {v}")
        out += (v & 0xFFFF).to_bytes(2, "little")
    return bytes(out)


def pack_i16(value: int) -> bytes:
    return struct.pack("<h", int(value))


def unpack_u16(payload: bytes, count: int) -> list[int] | None:
    if len(payload) < count * 2:
        return None
    return list(struct.unpack_from(f"<{count}H", payload, 0))


def unpack_i16(payload: bytes, count: int) -> list[int] | None:
    if len(payload) < count * 2:
        return None
    return list(struct.unpack_from(f"<{count}h", payload, 0))


def _i16(payload: bytes, offset: int) -> int:
    value = payload[offset] | (payload[offset + 1] << 8)
    return value - 0x10000 if value & 0x8000 else value


# ------------------------------------------------------------------ 请求帧
def frame_identify(dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    return build_frame(CMD_IDENTIFY, b"", dev_id=dev_id, family=family)


def frame_set_thermostat(index: int, temp_c: int,
                         dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    if not 0 <= int(index) <= TARGET_INDEX:
        raise ValueError(f"index 必须在 0..{TARGET_INDEX}: {index}")
    return build_frame(CMD_SET_THERMOSTAT, bytes((int(index),)) + pack_i16(temp_c),
                       dev_id=dev_id, family=family)


def parse_set_thermostat(payload: bytes) -> tuple[int, int] | None:
    """解析 0x03 写命令 payload = [index] + i16le（与 frame_set_thermostat 同一份布局）。

    曾经这条布局有 3 份实现（hp20_commands 死代码 / hp20_device 手拼 / sim_device 手解）。
    现在编码只用 frame_set_thermostat、解码只用这里，模拟设备也走它。
    """
    if len(payload) < 3:
        return None
    value = unpack_i16(bytes(payload[1:3]), 1)
    if not value:
        return None
    return int(payload[0]), int(value[0])


def frame_get_thermostat(dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    return build_frame(CMD_GET_THERMOSTAT, b"", dev_id=dev_id, family=family)


def frame_set_preheat(channel: int, dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    if not 0 <= int(channel) <= 3:
        raise ValueError(f"channel 必须在 0..3: {channel}")
    return build_frame(CMD_SET_PREHEAT, bytes((int(channel),)),
                       dev_id=dev_id, family=family)


def frame_set_reflow(values: Sequence[int],
                     dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    return build_frame(CMD_SET_REFLOW, _check8(values, "回流曲线"),
                       dev_id=dev_id, family=family)


def frame_get_reflow(dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    return build_frame(CMD_GET_REFLOW, b"", dev_id=dev_id, family=family)


# 停止加热的两条实测路径：
#   默认：0x05 [3] 选 T4 —— T4 是官方 APP 隐藏的第 4 挡、预设固定 0℃，选它 -> 目标变 0℃ -> 停热，
#         设备面板会显示 T3。帧字节 AF 09 02 05 01 03 3D（与 A1 JSON 的「启动 T4」示例一致）。
#   备用：0x03 index=4 把 T5 目标温度设成 0℃ -> 功率立刻归零、温度回落（AF 09 02 03 03 04 00 00 3C）。
STOP_CHANNEL = 3
STOP_TARGET_TEMP = 0


def frame_stop_by_channel(dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    """停止加热（默认路径）= 发 0x05 [3] 选 T4 停止通道。"""
    return frame_set_preheat(STOP_CHANNEL, dev_id=dev_id, family=family)


def frame_stop_by_target(dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    """停止加热（备用路径）= 把 T5 目标温度设成 0℃。"""
    return frame_set_thermostat(TARGET_INDEX, STOP_TARGET_TEMP,
                               dev_id=dev_id, family=family)


def build_name_payload(name: str) -> bytes:
    raw = name.encode("ascii", "replace")[:NAME_LEN]
    return raw + b"\x00" * (NAME_LEN - len(raw))


def frame_set_name(name: str, dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    return build_frame(CMD_SET_NAME, build_name_payload(name),
                       dev_id=dev_id, family=family)


def frame_get_name(dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    return build_frame(CMD_GET_NAME, b"", dev_id=dev_id, family=family)


def frame_realtime(dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    return build_frame(CMD_REALTIME, b"", dev_id=dev_id, family=family)


def frame_set_repair(values: Sequence[int],
                     dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    return build_frame(CMD_SET_REPAIR, _check8(values, "维修参数"),
                       dev_id=dev_id, family=family)


def frame_get_repair(dev_id: int = DEV_ID, family: int = FAMILY) -> bytes:
    return build_frame(CMD_GET_REPAIR, b"", dev_id=dev_id, family=family)


def _check8(values: Sequence[int], what: str) -> bytes:
    if len(values) != 8:
        raise ValueError(f"{what}必须是 8 个 u16，收到 {len(values)}")
    return pack_u16(values)


def decode_name(payload: bytes) -> str:
    return bytes(b for b in payload if b != 0x00).decode("ascii", "replace").strip()


def identify_matches(payload: bytes) -> bool:
    return len(payload) >= 2 and payload[:2] == IDENT_PAYLOAD


# ------------------------------------------------------------------ 实时状态
MODE_IDLE = 0
MODE_REFLOW = 1
MODE_HEATING = 2
MODE_SETTING = 3
MODE_REPAIR = 4
MODE_MAIN = 8

# 以用户手册为准：1=回流焊、2=加热。
# ⚠ 官方 APP 的 stateStr 把 1/2 的字符串表写反了（cf==1 返回 T1..T4、cf==2 返回 PH/SK/RF/CL），
#   这里按手册来，不要照抄 APP。
MODE_NAMES = {
    MODE_IDLE: "待机",
    MODE_REFLOW: "回流焊",
    MODE_HEATING: "加热",
    MODE_SETTING: "设置",
    MODE_REPAIR: "维修",
    MODE_MAIN: "主页",
}

# 挡位/阶段字段 (W1>>10)&3 的解释随模式变化
MODE_SLOT_NAMES = {
    MODE_REFLOW: ("PH 预热", "SK 恒温", "RF 回流", "CL 冷却"),
    MODE_HEATING: ("T1", "T2", "T3", "T4"),
    MODE_REPAIR: REPAIR_GROUPS,
}

# ------------------------------------------------------------------ 不加热判据
# 实测：设备在待机/设置界面仍会把 payload[5] 报成 0x02，功率字段也可能是残留值，
# 所以**不能只看 [5]**；mode 才是「当前界面/工作模式」的真值。[5]==0x00 也一律算不加热。
NON_HEATING_MODES = (MODE_IDLE, MODE_SETTING, MODE_MAIN)


def is_heating(status: "RealtimeStatus | None") -> bool:
    """设备当前是否在加热（主界面 / CSV / 图表统一的唯一判据）。"""
    if status is None:
        return False
    return (status.effective_mode not in NON_HEATING_MODES
            and status.output_state_raw != 0x00)


def effective_power(status: "RealtimeStatus | None") -> int:
    """展示/记录用的输出功率：不加热一律 0（设备侧的残留值不予采信）。"""
    if not is_heating(status):
        return 0
    return int(status.output_power_w)


@dataclass(frozen=True)
class RealtimeStatus:
    current_temp: int        # payload[0:2] i16 —— 实测真实温度 ℃
    supply_power_w: int      # payload[2] —— **供电功率 W**（实测 65 / 140，随电源变化）
    input_voltage_raw: int   # payload[3:5] i16 —— **输入电压 ×0.1V**（200=20.0V / 278=27.8V）
    output_state_raw: int    # payload[5] —— **输出状态**：0x00 待机 / 0x02 加热模式运行中
                             # （保温、功率 0W 时实测也是 0x02，所以它表示"模式在跑"而非"正在出功率"）
    output_power_w: int      # payload[6] —— **当前输出功率 W**
    target_temp: int         # W1 & 0x3FF
    channel_or_stage: int    # (W1>>10) & 3 —— 挡位 T1..T4
    mode: int                # (W1>>12) & 7 —— 实测 0=待机 / 2=加热模式
    w2_low6: int             # W2 低 6 位：官方 APP 的拆分，**已实测证伪**，仅留作对照/诊断
    w2_next7: int            # W2 次 7 位：同上
    bit12: int
    # 注意：状态帧里**没有任何风扇信息**。W2 bit13 曾被当成「风扇是否运转」，已证伪 ——
    # 唯一 bit13=1 的样本全是 W2=0x29BF（加热超时未启用的哨兵常量）本身第 13 位为 1。
    celsius_bit: bool
    w1: int
    w2: int
    mode4: int = 0           # 4 位诊断（文档 &7 装不下 8）

    @property
    def effective_mode(self) -> int:
        """mode 取 (W1>>12)&7，装不下 8；若 4 位视图给出 8（主页）则用它。"""
        if self.mode4 != self.mode and self.mode4 in MODE_NAMES:
            return self.mode4
        return self.mode

    @property
    def mode_name(self) -> str:
        return MODE_NAMES.get(self.effective_mode, f"未知({self.effective_mode})")

    @property
    def slot_name(self) -> str:
        return f"T{self.channel_or_stage + 1}"

    @property
    def slot_text(self) -> str:
        """挡位/阶段字段的文案，按当前模式解释。

        mode=2 加热 -> T1..T4（T4 是隐藏的停止通道）；mode=1 回流焊 -> PH/SK/RF/CL；
        mode=4 维修 -> 除胶(Debond)/分层(Delam)/贴合(LAM)/植球(Reballing)（用户真机确认）；
        mode=0/3/8 及其他 -> 直接显示字段值。
        """
        mode = self.effective_mode
        names = MODE_SLOT_NAMES.get(mode)
        if not names:
            return f"字段值 {self.channel_or_stage}"
        if mode == MODE_HEATING and self.channel_or_stage == STOP_CHANNEL:
            return "T4（停止通道）"
        if self.channel_or_stage >= len(names):
            return f"字段值 {self.channel_or_stage}"
        return names[self.channel_or_stage]

    @property
    def unit(self) -> str:
        # 真机 w2 的 bit14 从不置位，按文档「1=℃」会永远显示 ℉ -> 固定 ℃，bit14 仅诊断
        return "℃"

    @property
    def w2_text(self) -> str:
        """W2 原值（诊断用）；人类可读的计时看 timer_text。"""
        return f"W2=0x{self.w2:04X}"

    @property
    def timer_packed(self) -> int:
        """W2 原值 —— **64 进制打包**的 MM:SS，不是秒数。"""
        return self.w2

    @property
    def timer_label(self) -> str:
        """W2 的标签跟着模式走（换算公式不变）。"""
        mode = self.effective_mode
        if mode in (MODE_REFLOW, MODE_HEATING, MODE_REPAIR):
            return TIMER_LABEL_HEAT
        if mode in (MODE_IDLE, MODE_SETTING, MODE_MAIN):
            return TIMER_LABEL_SLEEP
        return "计时"

    @property
    def timer_text(self) -> str:
        """加热计时（设备剩余秒数）-> MM:SS / OFF。

        **64 进制打包**：MM=(W2>>6)&0x7F、SS=W2&0x3F，SS>=60 -> OFF。
        设置 25 min 时 W2=1600（=25×64）-> "25:00"；20 min 时 W2=1280 -> "20:00"。
        该计时是设备本地「加热超时/休眠」设置，协议没有读写命令，因此不保证与面板完全同步。
        """
        return format_timer(self.w2)

    @property
    def is_heating(self) -> bool:
        """是否在加热（见模块级 is_heating 的判据）。"""
        return is_heating(self)

    @property
    def effective_power_w(self) -> int:
        """展示/记录用的输出功率（不加热 -> 0）。"""
        return effective_power(self)

    @property
    def output_state_text(self) -> str:
        """输出状态文案：按「不加热」判据归一化。

        payload[5] 的原始含义不变（诊断页仍显示 0x02 原值）；主界面在待机/设置/主页
        一律显示「待机」，避免用户看到「加热模式运行中」却根本没在加热。
        """
        if not self.is_heating:
            return "待机"
        mode = self.effective_mode
        if mode == MODE_REFLOW:
            return "回流焊运行中"
        if mode == MODE_REPAIR:
            return "维修模式运行中"
        return "加热模式运行中"

    @property
    def input_voltage_v(self) -> float:
        return self.input_voltage_raw / 10.0

    @property
    def voltage_text(self) -> str:
        return f"{self.input_voltage_v:.1f}V"

    @property
    def supply_text(self) -> str:
        return f"{self.supply_power_w}W"

    @property
    def power_percent(self) -> float:
        """输出功率占供电功率的百分比（不加热 -> 0；与 effective_power 同一口径）。"""
        if self.supply_power_w <= 0:
            return 0.0
        return self.effective_power_w * 100 / self.supply_power_w

    @property
    def power_percent_text(self) -> str:
        return f"{self.power_percent:.1f}%"

    @property
    def power_text(self) -> str:
        return f"{self.effective_power_w}W（{self.power_percent_text}）"

    def summary(self) -> str:
        return (f"{self.current_temp}{self.unit} / 目标 {self.target_temp}{self.unit} | "
                f"{self.mode_name}·{self.slot_text} | 功率 {self.power_text} | "
                f"供电 {self.supply_text} | 输入 {self.voltage_text} | "
                f"{self.timer_label} {self.timer_text} | {self.w2_text}")


def parse_realtime(payload: bytes) -> RealtimeStatus | None:
    """解码 cmd 0x0A 应答；payload 少于 11 字节返回 None。"""
    if len(payload) < 11:
        return None
    w1 = payload[7] | (payload[8] << 8)
    w2 = payload[9] | (payload[10] << 8)
    return RealtimeStatus(
        current_temp=_i16(payload, 0),
        supply_power_w=payload[2],
        input_voltage_raw=_i16(payload, 3),
        output_state_raw=payload[5],
        output_power_w=payload[6],
        target_temp=w1 & 0x3FF,
        channel_or_stage=(w1 >> 10) & 0x03,
        mode=(w1 >> 12) & 0x07,
        mode4=(w1 >> 12) & 0x0F,
        w2_low6=w2 & 0x3F,
        w2_next7=(w2 >> 6) & 0x7F,
        bit12=1 if w2 & 0x1000 else 0,
        celsius_bit=bool(w2 & 0x4000),
        w1=w1,
        w2=w2,
    )


def bar_power_percent(status: "RealtimeStatus | None") -> float:
    """进度条要显示的占比：**每帧重算** [6]/[2]，待机/无数据一律 0%。

    真机待机帧仍报 1~2 W 的静态读数（见 docs/hp20_extra_findings.md 待机帧 A），
    直接按比例算会让进度条卡在 1~3% 一动不动 —— 用户反馈「处于待机时输出功率进度条
    卡死不动，应该显示 0%」。CSV 的 power_pct 列仍用 power_percent，语义不动。
    """
    if status is None or status.mode == MODE_IDLE:
        return 0.0
    return status.power_percent


def build_realtime_payload(current_temp: int, supply_power_w: int, output_power_w: int,
                           target_temp: int, channel_or_stage: int, mode: int,
                           w2_low6: int = 0, w2_next7: int = 0,
                           celsius: bool = False,
                           input_voltage_raw: int = 200, output_state_raw: int = 0x02,
                           w2: int | None = None) -> bytes:
    """模拟设备/测试用：按实测位域拼出 11 字节状态 payload。

    w2 语义未定，因此这里既能显式给整字（w2=...），也能给两个原始位段拼一个。
    """
    w1 = (target_temp & 0x3FF) | ((channel_or_stage & 3) << 10) | ((mode & 0xF) << 12)
    if w2 is None:
        w2 = ((w2_low6 & 0x3F) | ((w2_next7 & 0x7F) << 6)
              | (0x4000 if celsius else 0))
    payload = bytearray()
    payload += (int(current_temp) & 0xFFFF).to_bytes(2, "little")
    payload.append(supply_power_w & 0xFF)
    payload += (int(input_voltage_raw) & 0xFFFF).to_bytes(2, "little")
    payload.append(output_state_raw & 0xFF)
    payload.append(output_power_w & 0xFF)
    payload += w1.to_bytes(2, "little")
    payload += w2.to_bytes(2, "little")
    return bytes(payload)


def response_frame(cmd: int, payload: bytes = b"", dev_id: int = DEV_ID,
                   family: int = FAMILY) -> bytes:
    return build_frame(cmd, payload, dev_id=dev_id, family=family, header=RESP_HEADER)