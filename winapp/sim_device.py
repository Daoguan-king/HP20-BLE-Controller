"""进程内模拟 HP20：真实命令集 + 真机实测默认值 + 最小热模型。

- Hp20SimCore 是纯同步状态机（无 asyncio），可单独单测；
- SimTransport 只搬运字节，并施加应答延迟；
- 不主动推送状态帧（真机是纯轮询设备），上层靠 125ms 轮询取数；
- simulate_disconnect() 可主动断链，用于验证上层自动重连。
"""
from __future__ import annotations

import asyncio

import framing
import hp20_commands as hc
from transport import ScanResult, Transport


class Hp20SimCore:
    """一阶惯性热模型。真机热特性未知，这里只求"形状对、可观测"。

    字段语义按实测：mode 只有 0=待机 / 2=加热模式；channel_or_stage 是挡位 T1..T4。
    """

    AMBIENT = 25.0
    HEAT_RATE = 0.05          # 1/s
    COOL_RATE = 0.015
    SUPPLY_POWER_W = 140      # 实测 payload[2] = 供电功率 W（换电源前 65，换后 140）
    INPUT_VOLTAGE_RAW = 278   # 实测 payload[3:5] = 输入电压×0.1V（278 -> 27.8V；旧电源 200 -> 20.0V）
    W2_RAW = hc.TIMER_OFF_SENTINEL   # 「计时未启用」哨兵（与协议常量同源，不另写）
    # 加热超时默认 20 Min：W2 是 64 进制打包（20 × 64 = 1280），每真实秒 -1
    TIMER_START_PACKED = 1280

    def __init__(self, ambient: float = AMBIENT) -> None:
        self.ambient = ambient
        self.temp = ambient
        # 真机实测默认值（2026-10-17）
        self.presets = [66, 200, 300, 0]              # T1..T4（T4 是固定 0℃ 的停止通道）
        self.t5 = 0                                   # 主界面目标温度
        self.reflow = [150, 120, 180, 90, 260, 60, 80, 300]
        self.repair = [100, 120, 80, 120, 80, 120, 260, 120]
        self.name = "HP20"
        self.mode = hc.MODE_IDLE                      # 0 待机 / 2 加热模式
        self.channel = 0                              # 挡位 T1..T4
        self.heating = False
        self.goal = 0.0
        self.output_power_w = 0                       # 输出功率 W
        self.timer = 0.0                              # 已加热时长（本地统计）
        self.timer_left: float | None = None          # 加热超时剩余秒数；None = 未启用（发哨兵）
        self.celsius = False                          # 真机 bit14 从不置位
        self.program: list[int] | None = None         # 回流/维修曲线（模拟设备本地程序）
        self.stage = 0
        self.stage_remaining = 0.0
        self.rx_frames = 0

    # ------------------------------------------------------------- 物理/推进
    def tick(self, dt: float) -> None:
        if dt <= 0:
            return
        if self.heating and self.program is not None:
            self._advance_stage(dt)
        if self.heating and self.timer_left is not None:
            self.timer_left = max(0.0, self.timer_left - dt)   # 每真实秒 -1 个 64 进制单位
            # 64 进制下 SS=60..63 是非法秒（界面会显示 OFF）。真机会不会经过这 4 个值未实测，
            # 这里按"设备不会显示非法秒"处理：跨分钟时直接跳到上一分钟的 59 秒。
            units = int(self.timer_left)
            if units > 0 and (units & 0x3F) >= 60:
                self.timer_left = float((units & ~0x3F) + 59)
        if self.heating and self.goal > self.temp:
            self.temp += (self.goal - self.temp) * self.HEAT_RATE * dt
            self.timer += dt
            self.output_power_w = max(0, min(self.SUPPLY_POWER_W,
                                             round((self.goal - self.temp) * 0.5)))
        else:
            # 目标低于当前温度（含"停止加热=目标置 0"）时设备不出力：
            # 真机实测 power 6→0、温度 60→59→58 自然回落
            floor = max(self.goal, self.ambient) if self.heating else self.ambient
            self.temp += (floor - self.temp) * self.COOL_RATE * dt
            self.output_power_w = 0
            if self.heating:
                self.timer += dt

    def _advance_stage(self, dt: float) -> None:
        table = self.program or []
        self.stage_remaining -= dt
        while self.stage_remaining <= 0 and self.heating:
            self.stage += 1
            if self.stage >= 4 or self.stage * 2 + 1 >= len(table):
                self.mode, self.heating = hc.MODE_IDLE, False
                self.stage, self.stage_remaining, self.goal = 0, 0.0, 0.0
                self.program = None
                self.timer_left = None
                return
            self.stage_remaining += float(table[self.stage * 2 + 1])
            self.goal = float(table[self.stage * 2])

    # ------------------------------------------------------------- 设备本地动作
    def enter_program(self, table: list[int], channel: int = 0,
                      start_stage: int = 0, mode: int = hc.MODE_HEATING) -> None:
        """模拟"设备端启动回流/维修程序"（协议没有启动命令，见 U1）。"""
        self.program = list(table)
        self.channel = channel
        self.mode = mode
        self.stage = start_stage
        self.stage_remaining = float(self.program[start_stage * 2 + 1])
        self.goal = float(self.program[start_stage * 2])
        self.heating, self.timer, self.output_power_w = True, 0.0, 0
        self.timer_left = float(self.TIMER_START_PACKED)

    def enter_reflow(self) -> None:
        # mode=1 = 回流焊（阶段 PH/SK/RF/CL），按用户手册
        self.enter_program(self.reflow, 0, 0, mode=hc.MODE_REFLOW)

    def enter_repair(self, sub: int) -> None:
        sub = max(0, min(3, sub))
        # mode=4 = 维修（子功能 Debond/Delam/LAM/Reballing），从选中那组参数开始
        self.enter_program(self.repair, sub, sub, mode=hc.MODE_REPAIR)

    def simulate_panel_stop(self) -> None:
        """模拟"按设备物理面板停止"（等价于目标置 0，但模式回到待机）。"""
        self.mode, self.heating, self.goal = hc.MODE_IDLE, False, 0.0
        self.program, self.stage, self.stage_remaining = None, 0, 0.0

    # ------------------------------------------------------------- 帧处理
    def handle(self, frame: framing.Frame, ack_payload: str = "one") -> list[bytes]:
        self.rx_frames += 1
        cmd, p = frame.cmd, frame.payload
        if cmd == hc.CMD_IDENTIFY:
            # 真机识别应答的设备头是 FF FF
            return [hc.response_frame(cmd, hc.IDENT_PAYLOAD, dev_id=0xFF, family=0xFF)]
        if cmd == hc.CMD_REALTIME:
            return [hc.response_frame(cmd, self.status_payload())]
        if cmd == hc.CMD_GET_THERMOSTAT:
            return [hc.response_frame(cmd, hc.pack_u16(self.presets))]
        if cmd == hc.CMD_GET_REFLOW:
            return [hc.response_frame(cmd, hc.pack_u16(self.reflow))]
        if cmd == hc.CMD_GET_REPAIR:
            return [hc.response_frame(cmd, hc.pack_u16(self.repair))]
        if cmd == hc.CMD_GET_NAME:
            return [hc.response_frame(cmd, self.name.encode("ascii", "replace") + b"\x00")]
        if cmd == hc.CMD_SET_THERMOSTAT:
            parsed = hc.parse_set_thermostat(p)      # 与 frame_set_thermostat 同一份布局
            if parsed is None:
                return []
            index, value = parsed
            if index < 4:
                self.presets[index] = int(value)
            elif index == hc.TARGET_INDEX:
                self.t5 = int(value)
                if self.heating:
                    # 真机实测：改 T5 立刻改变在用目标（设 0 就等于停热，功率马上归零）
                    self.goal = float(value)
            return [self._ack(frame, ack_payload)]
        if cmd == hc.CMD_SET_PREHEAT and len(p) >= 1:
            channel = min(3, p[0])
            if channel == hc.STOP_CHANNEL:
                # 实测：选 T4（隐藏的第 4 挡、预设固定 0℃）-> 目标变 0 -> 停止加热；
                # 状态里挡位字段 = 3（T4），设备物理面板显示 T3
                self.channel = hc.STOP_CHANNEL
                self.mode = hc.MODE_HEATING     # 实测 mode 全程为 2（停热也不回到 0）
                self.t5 = 0
                self.goal = 0.0
                self.heating = False
                self.timer = 0.0
                self.timer_left = None          # 停热后回到「计时未启用」哨兵
                self.output_power_w = 0
                self.program, self.stage, self.stage_remaining = None, 0, 0.0
                return [self._ack(frame, ack_payload)]
            self.channel = channel
            self.mode = hc.MODE_HEATING
            # 实测：0x05 [ch] 会把目标温度改写成该挡位的预设值（自定义目标要在此之后再发）
            self.t5 = int(self.presets[channel]) if channel < len(self.presets) else 0
            self.goal = float(self.t5)
            self.heating, self.timer, self.output_power_w = True, 0.0, 0
            self.timer_left = float(self.TIMER_START_PACKED)
            self.program, self.stage, self.stage_remaining = None, 0, 0.0
            return [self._ack(frame, ack_payload)]
        if cmd == hc.CMD_SET_REFLOW:
            values = hc.unpack_u16(p, 8)             # 复用协议层的解包，不另写 struct
            if values is None:
                return []
            self.reflow = values
            return [self._ack(frame, ack_payload)]
        if cmd == hc.CMD_SET_REPAIR:
            values = hc.unpack_u16(p, 8)
            if values is None:
                return []
            self.repair = values
            return [self._ack(frame, ack_payload)]
        if cmd == hc.CMD_SET_NAME and len(p) >= 1:
            self.name = hc.decode_name(p)
            return [self._ack(frame, ack_payload)]
        return []       # 未知命令不应答，让上层超时路径真的跑一次

    def _ack(self, frame: framing.Frame, ack_payload: str) -> bytes:
        if ack_payload == "empty":
            payload = b""
        elif ack_payload == "echo":
            payload = frame.payload
        else:                                    # "one"：真机实测就是 1 字节 0x00
            payload = b"\x00"
        return hc.response_frame(frame.cmd, payload)

    def status_payload(self) -> bytes:
        target = int(round(self.goal)) if self.heating else int(self.t5)
        # mode=1/4 时该字段是阶段/子功能，其余是挡位
        slot = self.stage if self.mode in (hc.MODE_REFLOW, hc.MODE_REPAIR) else self.channel
        return hc.build_realtime_payload(
            current_temp=int(round(self.temp)),
            supply_power_w=self.SUPPLY_POWER_W,
            output_power_w=self.output_power_w,
            target_temp=target,
            channel_or_stage=slot,
            mode=self.mode,
            w2=int(self.timer_left) if self.timer_left is not None else self.W2_RAW,
            celsius=self.celsius,
            input_voltage_raw=self.INPUT_VOLTAGE_RAW,
            output_state_raw=0x02 if self.heating else 0x00,
        )


class SimTransport(Transport):
    """模拟传输：按请求回帧，带固定应答延迟；可主动断链以验证重连。"""

    ADDRESS = "SIM-00-00-00-00-00-01"

    def __init__(self, core: Hp20SimCore | None = None, latency: float = 0.01,
                 tick_dt: float = 0.02, ack_payload: str = "one",
                 name: str = "HP20 模拟加热台") -> None:
        super().__init__()
        self.core = core or Hp20SimCore()
        self.latency = latency
        self.tick_dt = tick_dt
        self.ack_payload = ack_payload
        self.name = name
        self.connect_count = 0
        self._connected = False
        self._task: asyncio.Task | None = None
        self._parser = framing.FrameParser(header=framing.REQ_HEADER)
        self.last_tx = b""

    # ---------------------------------------------------------------- 接口
    def describe(self) -> str:
        return "模拟设备(进程内)"

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def char_summary(self) -> list[str]:
        return ["(模拟) 0000fff1 notify+write-without-response / fff2 notify / fff3 write"]

    async def scan(self, timeout: float = 1.0) -> list[ScanResult]:
        await asyncio.sleep(min(timeout, 0.05))
        return [ScanResult(self.ADDRESS, self.name, -40, "sim", "进程内模拟设备，无需蓝牙")]

    async def find_by_address(self, address: str, timeout: float = 10.0):
        await asyncio.sleep(min(timeout, 0.01))
        if (address or "").lower() != self.ADDRESS.lower():
            return None
        return ScanResult(self.ADDRESS, self.name, -40, "sim", "按地址命中(模拟)")

    async def connect_direct(self, address: str, timeout: float = 5.0) -> None:
        # 模拟"这台设备不存在"：直连不存在的地址必须失败，好让上层走完三段式
        if (address or "").lower() != self.ADDRESS.lower():
            raise RuntimeError(f"模拟设备直连失败：无此设备 {address}")
        await self.connect(ScanResult(address, "(模拟直连)", None, "sim"), timeout)

    async def connect(self, target=None, timeout: float = 5.0) -> None:
        await asyncio.sleep(self.latency)
        self.connect_count += 1
        self._connected = True
        self._parser.reset()
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._ticker())
        self._emit_system(f"模拟设备已连接：{self.name}（第 {self.connect_count} 次）")

    async def disconnect(self) -> None:
        self._connected = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def write(self, data: bytes) -> None:
        if not self._connected:
            raise RuntimeError("模拟设备未连接")
        self.last_tx = bytes(data)
        for frame in self._parser.feed(data):
            replies = self.core.handle(frame, self.ack_payload)
            if replies:
                await asyncio.sleep(self.latency)
                for reply in replies:
                    self._deliver(reply)

    # ---------------------------------------------------------------- 断链模拟
    def simulate_disconnect(self, reason: str = "模拟断线(设备侧断开)") -> None:
        """像真机那样突然掉线：停止时钟并把 on_disconnect 抛给上层。"""
        self._connected = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        cb = self.on_disconnect
        if cb is not None:
            cb(reason)

    # ---------------------------------------------------------------- 内部
    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.tick_dt)
            self.core.tick(self.tick_dt)

    def _deliver(self, data: bytes) -> None:
        cb = self.on_data
        if cb is not None:
            cb(bytes(data))

    def _emit_system(self, text: str) -> None:
        cb = getattr(self, "on_system", None)
        if cb is not None:
            cb(text)
