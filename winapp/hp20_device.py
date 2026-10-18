"""HP20 设备层：识别、单命令在途的事务、节流写、暂停式轮询、超时。

时序常量全部来自 docs/hp20_protocol.md §8 的反汇编证据：
  轮询 125 ms / 应答超时 1000 ms / 等待粒度 10 ms / 写间隔 15 ms /
  单命令在途（忙则拒绝）/ 写命令先暂停轮询 / 连接后延时 1 s 再握手。

所有协程跑在 transport.AsyncRunner 的 IO 线程；回调也在该线程触发，UI 需自行投递。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

import framing
import hp20_commands as hc
from transport import Transport

_BUSY_TEXT = "指令任务繁忙，拒绝重复调用"


@dataclass
class DeviceInfo:
    dev_header: int
    device_type: str
    is_hp20: bool


@dataclass
class DeviceStats:
    tx_frames: int = 0
    rx_frames: int = 0
    timeouts: int = 0
    retries: int = 0
    busy_rejects: int = 0
    bad_status: int = 0
    empty_acks: int = 0
    stale_replies: int = 0      # S2：被丢弃的迟到应答数（不进 __str__，避免改日志格式）

    def __str__(self) -> str:
        return (f"TX {self.tx_frames} / RX {self.rx_frames} / 超时 {self.timeouts} / "
                f"重试 {self.retries} / 忙拒 {self.busy_rejects} / 空应答 {self.empty_acks}")


class Hp20Device:
    def __init__(self, transport: Transport, dev_id: int = hc.DEV_ID,
                 family: int = hc.FAMILY, timeout: float = hc.RESPONSE_TIMEOUT_S,
                 tick: float = hc.TIMEOUT_TICK_S, write_gap: float = hc.WRITE_GAP_S,
                 poll_interval: float = hc.POLL_INTERVAL_S, retries: int = 0,
                 auto_refresh_params: bool = False,
                 require_nonempty_ack: bool = True,
                 handshake_delay: float | None = None) -> None:
        self.transport = transport
        self.dev_id = dev_id
        self.family = family
        self.timeout = timeout
        self.tick = tick
        self.write_gap = write_gap
        self.poll_interval = poll_interval
        self.retries = retries
        # 默认关闭：W2 在计时状态下每秒都变，旧实现"W2 变化就补读 0x04+0x0C"会每秒发两条
        # 参数读命令并弹一次提示，直接把界面拖卡。需要的话只按「mode 变化」触发，且静默。
        # 默认关闭：W2 在计时状态下每秒都变，"W2 变化就补读 0x04+0x0C"会每秒发两条参数
        # 读命令并弹一次提示，直接把界面拖卡（L 阶段用户反馈的 bug）。
        # 需要时只按「mode 变化」触发，且内部读取静默、不进界面提示区。
        self.auto_refresh_params = auto_refresh_params
        # 真机写命令的应答带 1 字节 payload，空 payload 视为异常应答
        self.require_nonempty_ack = require_nonempty_ack
        # None = 用协议常量；测试/重连可注入更短的值
        self.handshake_delay = handshake_delay

        # 回调（都在 IO 线程触发）
        self.on_frame: Callable[[str, bytes], None] | None = None
        self.on_status: Callable[[hc.RealtimeStatus], None] | None = None
        self.on_params: Callable[[str, object, bool], None] | None = None
        self.on_system: Callable[[str], None] | None = None
        self.on_disconnect: Callable[[str], None] | None = None
        self.on_identified: Callable[[DeviceInfo], None] | None = None

        self.stats = DeviceStats()
        self.info: DeviceInfo | None = None
        self.identified = False
        self.hp20: bool | None = None
        self.status: hc.RealtimeStatus | None = None
        self.presets: list[int] | None = None
        self.reflow: list[int] | None = None
        self.repair: list[int] | None = None
        self.device_name: str | None = None

        self._parser = framing.FrameParser(header=framing.RESP_HEADER)
        self._inbox: list[framing.Frame] = []
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._lock = asyncio.Lock()        # 事务槽：所有收发串行
        self._cmd_inflight = False         # 有用户命令在途（App 的 field_4b 忙标志）
        self._poll_paused = False
        self._blocked = False
        self._refresh_pending = False
        self._refresh_attempts = 0
        # open() 的失败原因（"" / "no_reply" / "not_hp20"）；--probe 靠它区分退出码 1/2
        self.open_failure = ""
        # S2 欠账表：cmd -> 欠账过期时刻。协议帧没有序号，只能靠「哪条请求超时过」来
        # 识别下一次同 cmd 的帧是不是它的迟到应答。每 cmd 至多欠一条，避免欠账叠加。
        self._stale: dict[int, float] = {}
        self._last_mode: int | None = None
        # 轮询按绝对时刻调度（对齐 App 的 Timer.periodic(125ms)）；改间隔时用它立刻醒来
        self._interval_event = asyncio.Event()
        self._last_write = 0.0

    # ---------------------------------------------------------------- 生命周期
    async def open(self, handshake_delay: float | None = None) -> bool:
        """握手识别并启动轮询；返回**是否真的识别成功**。

        返回 False 时上层绝不能再判 CONNECTED（P0「假连接成功」）。失败原因留在
        self.open_failure 里（"no_reply" / "not_hp20"），供 --probe 区分退出码 1/2。
        """
        if self._running:
            return self.identified and bool(self.hp20)
        # 上一次连错设备会置 _blocked；不复位的话换回真机也「显示识别成功、命令全被静默丢弃」
        self._blocked = False
        self.identified = False
        self.info = None
        self.hp20 = None
        self.open_failure = ""
        self._parser.reset()
        self._inbox.clear()
        self._stale.clear()
        self.transport.on_data = self._on_data
        self.transport.on_disconnect = self._on_transport_down
        self.transport.on_system = self._system
        self._running = True
        self._last_write = 0.0
        delay = (self.handshake_delay if handshake_delay is None else handshake_delay)
        delay = hc.POST_CONNECT_DELAY_S if delay is None else delay
        if delay > 0:
            await asyncio.sleep(delay)      # App: connect → 1s → discover/subscribe → 1s → 握手
        info = await self.identify()
        if info is None or not info.is_hp20:
            self.open_failure = "no_reply" if info is None else "not_hp20"
            self._system("未启动轮询：" + ("识别命令 0x00 无应答" if info is None else
                                          f"{info.device_type} 不是 HP20"))
            return False
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._poller(), name="hp20-poller")]
        self._system(f"轮询已启动（{self.poll_interval * 1000:.0f} ms）")
        return True

    async def close(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []
        self._fail_pending("已关闭", cancel=True)
        self.transport.on_data = None
        self.transport.on_disconnect = None

    @property
    def polling(self) -> bool:
        """轮询任务是否在跑（connect 成功后应为 True）。"""
        return any(not t.done() for t in self._tasks)

    @property
    def dropped_frames(self) -> int:
        """解析器丢掉的字节/坏帧数（诊断面板的「丢帧」）。"""
        return self._parser.dropped + self._parser.bad_lrc

    @property
    def last_status(self) -> hc.RealtimeStatus | None:
        """最近一帧实时状态（别名，方便外部探针取用）。"""
        return self.status

    # ---------------------------------------------------------------- 识别
    async def identify(self) -> DeviceInfo | None:
        frame = await self.request_frame(
            hc.frame_identify(dev_id=self.dev_id, family=self.family),
            hc.CMD_IDENTIFY, "识别/读型号", pause_poll=False)
        if frame is None:
            self._system("识别失败：命令 0x00 无应答")
            self.identified = False
            return None
        if len(frame.payload) < 2:
            self._system(f"识别失败：应答 payload 不足 2 字节（{frame.hex()}）")
            self.identified = False
            return None
        if frame.dev_header != hc.DEV_HEADER:
            # 真机识别应答的设备头是 FF FF，型号只看 payload 前两字节
            self._system(f"握手应答设备头 0x{frame.dev_header:04X}，"
                         f"型号按 payload {framing.to_hex(frame.payload[:2])} 判定")
        header = frame.payload[0] | (frame.payload[1] << 8)
        is_hp20 = header == hc.DEV_HEADER
        info = DeviceInfo(header, hc.DEV_TYPES.get(header, f"未知(0x{header:04X})"), is_hp20)
        self.info, self.identified, self.hp20 = info, True, is_hp20
        if is_hp20:
            self._system(f"设备识别成功：{info.device_type} (0x{header:04X})")
        else:
            self._blocked = True
            self._system(f"设备识别：{info.device_type} (0x{header:04X}) —— 非 HP20，"
                         f"已停止发送任何命令")
        cb = self.on_identified
        if cb is not None:
            cb(info)
        return info

    # ---------------------------------------------------------------- 事务原语
    async def request(self, cmd: int, payload: bytes = b"", description: str = "", *,
                      pause_poll: bool = True, internal: bool = False,
                      raw: bytes | None = None) -> framing.Frame | None:
        """一次完整事务：串行 + 节流 + 等应答。返回应答帧或 None。

        优先级分层（与 App 的 runWithRealDataPaused 一致）：
        - 用户命令：先暂停轮询、再占用事务槽；只有「已有别的用户命令在途」才忙拒
          （App 的 "指令任务繁忙，拒绝重复调用"），不会因为轮询恰好在跑而失败；
        - 轮询(internal)：有任何在途事务/暂停请求就静默跳过这一次。
        """
        if self._blocked:
            if cmd != hc.CMD_IDENTIFY:
                self._system(f"非 HP20 设备，忽略命令 0x{cmd:02X}")
            return None
        if internal:
            if self._cmd_inflight or self._poll_paused or self._lock.locked():
                return None
        elif self._cmd_inflight:
            self.stats.busy_rejects += 1
            self._system(f"{_BUSY_TEXT}: {description or f'0x{cmd:02X}'}")
            return None
        else:
            self._cmd_inflight = True
        paused_here = False
        if pause_poll and not self._poll_paused:
            self._poll_paused = True
            paused_here = True
        try:
            async with self._lock:
                return await self._transact(cmd, payload, description, raw)
        finally:
            if paused_here:
                self._poll_paused = False
            if not internal:
                self._cmd_inflight = False

    async def request_frame(self, frame: bytes, cmd: int, description: str, *,
                            pause_poll: bool = True,
                            internal: bool = False) -> framing.Frame | None:
        """用 hp20_commands.frame_* 组好的帧发一次事务（组帧时已做入参校验）。

        设备层不再手拼 payload：同一份协议布局只保留 hc.frame_* 一处真源，
        入参校验（index ≤ 4、channel ≤ 3、8 个 u16、名字 20 字节）随之复活，
        非法入参在**发帧之前**就抛 ValueError，而不是悄悄发出怪帧。
        """
        return await self.request(cmd, b"", description, pause_poll=pause_poll,
                                  internal=internal, raw=bytes(frame))

    async def _transact(self, cmd: int, payload: bytes, description: str,
                        raw: bytes | None) -> framing.Frame | None:
        attempts = self.retries + 1
        for attempt in range(attempts):
            gap = self.write_gap - (time.monotonic() - self._last_write)
            if gap > 0:
                await asyncio.sleep(gap)
            data = raw if raw is not None else framing.build_frame(
                cmd, payload, dev_id=self.dev_id, family=self.family)
            self._drop_inbox(cmd)      # 清掉同 cmd 的陈旧应答，保证配对不串号
            # TX 日志事件仍在写之前发：模拟设备会在 write() 内就回帧，否则日志会 RX 在前。
            # 但**计数**必须等写成功 —— 写失败还记一帧，收发统计就成了唯一物证的谎。
            self._emit_frame("TX", data)
            await self.transport.write(data)
            self.stats.tx_frames += 1
            self._last_write = time.monotonic()
            reply = await self._await_response(cmd)
            if reply is not None:
                if (self.require_nonempty_ack and cmd != hc.CMD_IDENTIFY
                        and len(reply.payload) < hc.ACK_MIN_PAYLOAD):
                    self.stats.empty_acks += 1
                    self._system(f"应答 payload 为空（cmd 0x{cmd:02X}）：{description} 按失败处理")
                    if attempt + 1 >= attempts:
                        return None
                    continue
                return reply
            self.stats.timeouts += 1
            if attempt + 1 < attempts:
                self.stats.retries += 1
                self._system(f"应答超时，重试 {attempt + 1}/{attempts - 1}：{description}")
            else:
                # 彻底放弃这条请求：它的应答迟早还会落进 inbox，记欠账让下一次丢弃它。
                # 重试期间不记 —— 那时同 cmd 的下一帧更可能是重试自己换来的应答。
                self._arm_stale(cmd)
                self._system(f"应答超时 {self.timeout * 1000:.0f} ms：{description}")
        return None

    async def _await_response(self, cmd: int) -> framing.Frame | None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self.tick)        # 10 ms 等待粒度（App 用 Timer.periodic）
            frame = self._take_current(cmd)
            if frame is not None:
                return frame
        return self._take_current(cmd)

    def _take_current(self, cmd: int) -> framing.Frame | None:
        """取**本次请求**的应答；先丢掉迟到应答再继续找（S2）。

        协议帧没有序号字段，配对键只有 cmd 一个字节。上一次请求超时后我们放弃了它的
        应答，那条应答迟早还会落进 inbox，却带着上一轮的数据 —— 它绝不能顶替本次请求，
        否则界面永远显示上一次的温度（数据系统性差一拍）。
        """
        while True:
            frame = self._take(cmd)
            if frame is None:
                return None
            if not self._consume_stale(cmd):
                return frame

    def _arm_stale(self, cmd: int) -> None:
        """记下「这条请求的应答还欠着」：同 cmd 的下一帧先当迟到应答丢掉。

        每 cmd 至多欠一条（不叠加），且只在一个应答窗口内有效 —— 再晚的帧与本次请求
        在协议上已经无法区分了。代价是：若那条应答真的丢了，本次请求会白等一帧；
        这比「静默返回上一轮的数据」便宜得多。
        """
        if cmd not in self._stale:
            self._stale[cmd] = time.monotonic() + self.timeout

    def _consume_stale(self, cmd: int) -> bool:
        """这一帧是上次超时请求的迟到应答吗？是则消费欠账并返回 True。"""
        expiry = self._stale.pop(cmd, None)
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            return False                     # 欠账过期：那条应答大概永远不来了
        self.stats.stale_replies += 1
        self._system(f"丢弃 cmd 0x{cmd:02X} 的迟到应答（上一条请求已超时，不能顶替本次）")
        return True

    # ---------------------------------------------------------------- 命令
    async def poll_once(self) -> bool:
        return await self.request_frame(
            hc.frame_realtime(dev_id=self.dev_id, family=self.family),
            hc.CMD_REALTIME, "读实时状态", pause_poll=False, internal=True) is not None

    async def set_thermostat(self, index: int, temp_c: int) -> bool:
        what = "T5 目标温度" if index == hc.TARGET_INDEX else f"T{index + 1}"
        frame = hc.frame_set_thermostat(index, temp_c, dev_id=self.dev_id,
                                        family=self.family)
        return await self.request_frame(frame, hc.CMD_SET_THERMOSTAT,
                                        f"设置{what} {temp_c}℃") is not None

    async def set_preheat(self, channel: int) -> bool:
        return await self.request_frame(
            hc.frame_set_preheat(channel, dev_id=self.dev_id, family=self.family),
            hc.CMD_SET_PREHEAT, f"启动预热通道 T{channel + 1}") is not None

    async def start_heating(self, channel: int, target_c: int | None = None) -> bool:
        """开始加热：**先** 0x05 [挡位]，需要自定义目标时**再**补 0x03 index=4。

        真机实测（H 阶段）：0x05 [ch] 会把目标温度改写成该挡位的预设值，
        所以自定义目标必须在选挡位**之后**下发，否则会被预设覆盖掉（用户输入被吞）。
        target_c=None 表示直接用挡位预设，不再发第二条帧。
        两次事务各自遵守 15 ms 写间隔 + 单命令在途。
        """
        if not await self.set_preheat(channel):
            return False
        if target_c is None:
            return True
        preset = self._preset_of(channel)
        if preset is not None and int(target_c) == int(preset):
            self._system(f"目标 {target_c}℃ 与 T{channel + 1} 预设相同，无需覆盖")
            return True
        self._system(f"下发自定义目标 {target_c}℃（0x05 已把目标设为 T{channel + 1} "
                     f"预设 {preset}℃）" if preset is not None else
                     f"下发自定义目标 {target_c}℃（0x05 已选挡位 T{channel + 1}）")
        return await self.set_thermostat(hc.TARGET_INDEX, target_c)

    def _preset_of(self, channel: int) -> int | None:
        if not self.presets or not 0 <= channel < len(self.presets):
            return None
        return int(self.presets[channel])

    async def stop_heating(self) -> bool:
        """停止加热（默认路径）：发 0x05 [3] 选 T4 停止通道。

        官方 APP 只有 T1/T2/T3 三个挡位；T4 是隐藏的第 4 挡、预设固定 0℃。
        真机实测：选 T4 -> 目标温度变 0℃ -> 停止加热，设备面板显示 T3。
        帧字节 AF 09 02 05 01 03 3D（与 A1 JSON 的「启动 T4」示例一致）。
        物理面板按钮仍是最终保障。
        """
        self._system("停止加热：选 T4 停止通道（0x05 [3]，等价官方 APP 的停止，面板会显示 T3）")
        return await self.set_preheat(hc.STOP_CHANNEL)

    async def stop_heating_by_target(self) -> bool:
        """停止加热（备用路径）：把 T5 目标温度设成 0℃（0x03 index=4）。"""
        self._system("停止加热（备用）：目标温度置 0（0x03 index=4）")
        return await self.set_thermostat(hc.TARGET_INDEX, hc.STOP_TARGET_TEMP)

    async def read_presets(self, internal: bool = False) -> list[int] | None:
        frame = await self.request_frame(
            hc.frame_get_thermostat(dev_id=self.dev_id, family=self.family),
            hc.CMD_GET_THERMOSTAT, "读 T1..T4 预设", internal=internal)
        values = hc.unpack_i16(frame.payload, 4) if frame is not None else None
        if values:
            self.presets = values
            self._emit_params("presets", values, silent=internal)
        return values

    async def read_reflow(self, internal: bool = False) -> list[int] | None:
        frame = await self.request_frame(
            hc.frame_get_reflow(dev_id=self.dev_id, family=self.family),
            hc.CMD_GET_REFLOW, "读回流曲线", internal=internal)
        values = hc.unpack_u16(frame.payload, 8) if frame is not None else None
        if values:
            self.reflow = values
            self._emit_params("reflow", values, silent=internal)
        return values

    async def write_reflow(self, values) -> bool:
        return await self.request_frame(
            hc.frame_set_reflow(values, dev_id=self.dev_id, family=self.family),
            hc.CMD_SET_REFLOW, "写回流曲线") is not None

    async def read_repair(self, internal: bool = False) -> list[int] | None:
        frame = await self.request_frame(
            hc.frame_get_repair(dev_id=self.dev_id, family=self.family),
            hc.CMD_GET_REPAIR, "读维修参数", internal=internal)
        values = hc.unpack_u16(frame.payload, 8) if frame is not None else None
        if values:
            self.repair = values
            self._emit_params("repair", values, silent=internal)
        return values

    async def write_repair(self, values) -> bool:
        return await self.request_frame(
            hc.frame_set_repair(values, dev_id=self.dev_id, family=self.family),
            hc.CMD_SET_REPAIR, "写维修参数") is not None

    async def read_name(self, internal: bool = False) -> str | None:
        frame = await self.request_frame(
            hc.frame_get_name(dev_id=self.dev_id, family=self.family),
            hc.CMD_GET_NAME, "读蓝牙名", internal=internal)
        if frame is None:
            return None
        name = hc.decode_name(frame.payload)
        self.device_name = name
        self._emit_params("name", name, silent=internal)
        return name

    async def write_name(self, name: str) -> bool:
        return await self.request_frame(
            hc.frame_set_name(name, dev_id=self.dev_id, family=self.family),
            hc.CMD_SET_NAME, f"写蓝牙名 {name}") is not None

    async def send_raw(self, data: bytes) -> bool:
        """原始帧发送：能识别出 cmd 就按事务等应答，否则只写不等。"""
        data = bytes(data)
        if len(data) >= 6 and data[0] in (framing.REQ_HEADER, framing.RESP_HEADER):
            cmd = data[3]
            return await self.request(cmd, data[5:-1], "原始帧发送", raw=data) is not None
        if self._cmd_inflight:
            self.stats.busy_rejects += 1
            self._system(f"{_BUSY_TEXT}: 原始字节")
            return False
        self._cmd_inflight = True
        self._poll_paused = True
        try:
            async with self._lock:
                await self.transport.write(data)
                self.stats.tx_frames += 1
                self._emit_frame("TX", data)
            return True
        finally:
            self._poll_paused = False
            self._cmd_inflight = False

    # ---------------------------------------------------------------- 轮询
    async def _poller(self) -> None:
        loop = asyncio.get_running_loop()
        next_due = loop.time() + self.poll_interval
        while self._running:
            try:
                if not self._poll_paused and not self._cmd_inflight and not self._blocked:
                    if self._refresh_pending:
                        await self._refresh_params()
                    await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._system(f"轮询异常：{exc}")
            delay = next_due - loop.time()
            next_due += self.poll_interval
            if delay > 0:
                await self._wait_interval(delay)
            else:
                # 事务本身超出一个周期：不追帧，重新对齐
                next_due = loop.time() + self.poll_interval

    async def _refresh_params(self) -> None:
        """w2 变化后的参数补读（App 行为）。内部优先级：忙就下个周期再来，最多 3 次。"""
        values = await self.read_presets(internal=True)
        if values is None:
            self._refresh_attempts += 1
            if self._refresh_attempts >= 3:
                self._refresh_pending = False
                self._system("预设/维修参数补读连续失败，先放弃")
            return
        await self.read_repair(internal=True)
        self._refresh_pending = False
        self._refresh_attempts = 0

    async def _wait_interval(self, delay: float) -> None:
        """睡到下一个周期。分段睡眠会被 Windows 15.6ms 定时器粒度拖成 ~180ms，
        所以这里一次睡到底；界面改间隔时 rearm_poll() 会立刻叫醒重排。"""
        self._interval_event.clear()
        try:
            await asyncio.wait_for(self._interval_event.wait(), delay)
        except asyncio.TimeoutError:
            pass

    def rearm_poll(self) -> None:
        """轮询间隔被改后调用（在 IO 线程里）：立刻重排下一次轮询。"""
        self._interval_event.set()

    # ---------------------------------------------------------------- 接收
    def _on_data(self, data: bytes) -> None:
        for frame in self._parser.feed(data):
            self.stats.rx_frames += 1
            self._emit_frame("RX", frame.raw)
            self._inbox.append(frame)
            if len(self._inbox) > 64:
                del self._inbox[:-64]
            if frame.cmd == hc.CMD_REALTIME:
                self._handle_status(frame)

    def _handle_status(self, frame: framing.Frame) -> None:
        status = hc.parse_realtime(frame.payload)
        if status is None:
            self.stats.bad_status += 1
            self._system(f"状态帧 payload 不足 11 字节({len(frame.payload)})")
            return
        mode_changed = (self._last_mode is not None
                        and status.effective_mode != self._last_mode)
        self._last_mode = status.effective_mode
        self.status = status
        # 只在「mode 变化」时补读参数，且必须由 auto_refresh_params 显式开启。
        # W2 每秒都在变，绝不能拿它当触发条件（L 阶段 bug）。
        if mode_changed and self.auto_refresh_params:
            self._refresh_pending = True
        cb = self.on_status
        if cb is not None:
            cb(status)

    def _take(self, cmd: int) -> framing.Frame | None:
        for i, frame in enumerate(self._inbox):
            if frame.cmd == cmd:
                del self._inbox[i]
                return frame
        return None

    def _drop_inbox(self, cmd: int) -> None:
        self._inbox = [f for f in self._inbox if f.cmd != cmd]

    # ---------------------------------------------------------------- 回调/工具
    def _on_transport_down(self, reason: str) -> None:
        self._fail_pending(reason)
        cb = self.on_disconnect
        if cb is not None:
            cb(reason)

    def _fail_pending(self, reason: str, cancel: bool = False) -> None:
        self._inbox.clear()
        self._stale.clear()
        if not cancel:
            self._system(f"链路中断：{reason}")

    def _emit_frame(self, direction: str, data: bytes) -> None:
        cb = self.on_frame
        if cb is not None:
            cb(direction, bytes(data))

    def _emit_params(self, kind: str, values, silent: bool = False) -> None:
        cb = self.on_params
        if cb is not None:
            cb(kind, values, silent)

    def _system(self, text: str) -> None:
        cb = self.on_system
        if cb is not None:
            cb(text)
