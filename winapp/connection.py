"""连接/重连状态机：扫描 → 连接 → 握手，失败退避重试；断线后台自动重连。

真机实测（2026-10-17）：设备会被手机 APP 占用/休眠，连不上或中途 Not connected，
且一段时间完全扫不到。因此：
  - 每次尝试都**重新扫描**拿新的 BLEDevice（不重用旧对象）；
  - 退避 1/2/4/8 s，最多重试 RECONNECT_MAX_ATTEMPTS 次；
  - 扫不到时给出可操作文案，而不是一行日志；
  - 断线后自动重连，成功后重新握手识别并恢复轮询。
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

import hp20_commands as hc
from transport import ScanResult

# 事件种类
EVT_STAGE = "stage"
EVT_SCAN = "scan"
EVT_CONNECT_OK = "connect_ok"
EVT_CONNECT_FAIL = "connect_fail"
EVT_DISCONNECTED = "disconnected"
EVT_RECONNECT_START = "reconnect_start"
EVT_RECONNECT_OK = "reconnect_ok"
EVT_NOT_FOUND = "not_found"
EVT_GIVE_UP = "give_up"
EVT_CANCELLED = "cancelled"

NOT_BROADCAST_HINT = ("设备未广播：可能正被手机 APP 占用，或已进入休眠/断开电源。"
                      "请先关闭手机 APP / 唤醒设备，再点「重新扫描并连接」。")

# 总连接预算：三段式 + 退避全跑满能到 90s+（真机实测一次 94.5s），用户只能干等。
# 超预算就停手并给可操作提示，不闷头重试。
DEFAULT_TOTAL_BUDGET_S = 45.0
BUDGET_HINT = ("设备未响应：可能正被手机 APP 占用，请关闭手机蓝牙后重试。"
               "（本次已用 {elapsed:.0f}s，超过 {budget:g}s 预算，已停止重试）")

# 无 --address / --name 时的自动选择：只有**唯一**候选才算无歧义。
# 以前是无条件 found[0]（RSSI 最强的任意 BLE 设备）—— 连错设备也报绿灯（P0）。
MAX_AUTO_CANDIDATES = 1


def pick_unique_candidate(found) -> ScanResult | None:
    """扫描结果里只有唯一候选时返回它，否则 None（把候选交给用户自己选）。"""
    results = list(found or [])
    if len(results) == MAX_AUTO_CANDIDATES:
        return results[0]
    return None


STATE_IDLE = "idle"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"
STATE_FAILED = "failed"


@dataclass(frozen=True)
class ConnectEvent:
    ts: float
    kind: str
    detail: str
    attempt: int = 0

    def stamp(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.ts))

    def format(self) -> str:
        attempt = f"#{self.attempt}" if self.attempt else ""
        return f"{self.stamp()}  {self.kind}{attempt}  {self.detail}"


class ConnectionManager:
    """把「扫描-连接-握手」与「断线重连」收在一处，只管连不连得上。

    真正的数据收发仍在 Hp20Device；本类只调用 transport.connect / device.open。
    """

    def __init__(self, transport, device, *, address: str | None = None,
                 name_hint: str | None = None, scan_timeout: float = 6.0,
                 find_timeout: float = 6.0, connect_timeout: float = 15.0,
                 max_retries: int = hc.RECONNECT_MAX_ATTEMPTS,
                 backoff: tuple[float, ...] = hc.RECONNECT_BACKOFF_S,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 auto_reconnect: bool = True, keep_events: int = 50,
                 total_budget: float = DEFAULT_TOTAL_BUDGET_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.transport = transport
        self.device = device
        self.address = address
        self.name_hint = name_hint
        self.scan_timeout = scan_timeout
        self.find_timeout = find_timeout
        self.connect_timeout = connect_timeout
        self.last_address: str | None = address
        self.stage_log: list[tuple[str, bool]] = []     # 便于测试断言三段式顺序
        self.max_retries = max(1, int(max_retries))
        self.backoff = tuple(backoff) or (1.0,)
        self._sleep = sleep or asyncio.sleep
        self.auto_reconnect = auto_reconnect
        self.events: deque[ConnectEvent] = deque(maxlen=keep_events)
        self.on_event: Callable[[ConnectEvent], None] | None = None
        self.state = STATE_IDLE
        self.attempt = 0
        self.total_attempts = 0
        self.last_error = ""
        self.delays: list[float] = []      # 便于测试断言退避序列
        self.total_budget = max(0.05, float(total_budget))
        self._clock = clock
        self.started_at: float | None = None
        self.stage_text = ""               # 当前段，界面显示「②直接连接 … 12s」
        self.budget_exhausted = False
        self._preferred: ScanResult | None = None
        self._task: asyncio.Task | None = None
        self._closing = False
        self._cancel_requested = False
        self.device.on_disconnect = self._on_down

    # ---------------------------------------------------------------- 状态
    @property
    def connected(self) -> bool:
        return self.state == STATE_CONNECTED and self.transport.is_connected

    def state_text(self) -> str:
        if self.state == STATE_CONNECTED:
            return "已连接"
        if self.state == STATE_RECONNECTING:
            return f"重连中(第 {self.attempt}/{self.max_retries} 次)"
        if self.state == STATE_CONNECTING:
            return f"连接中(第 {self.attempt}/{self.max_retries} 次)"
        if self.state == STATE_FAILED:
            return "连接失败"
        return "未连接"

    def elapsed(self) -> float:
        """本次连接已用秒数（界面每秒刷新用）。"""
        if self.started_at is None:
            return 0.0
        return max(0.0, self._clock() - self.started_at)

    def remaining(self) -> float:
        if self.started_at is None:
            return self.total_budget
        return max(0.0, self.total_budget - self.elapsed())

    def progress_text(self) -> str:
        """给界面看的进度，例如「②直接连接 … 12s」。"""
        if not self.stage_text:
            return self.state_text()
        return f"{self.stage_text} … {self.elapsed():.0f}s"

    def _budget_hint(self) -> str:
        return BUDGET_HINT.format(elapsed=self.elapsed(), budget=self.total_budget)

    def recent(self, limit: int = 20) -> list[ConnectEvent]:
        return list(self.events)[-limit:]

    # ---------------------------------------------------------------- 对外
    async def connect(self, preferred: ScanResult | None = None) -> bool:
        """首次连接，含退避重试。用户取消时返回 False（不抛异常，界面照常收尾）。"""
        self._closing = False
        self._cancel_requested = False
        self._preferred = preferred
        self.state = STATE_CONNECTING
        self.started_at = self._clock()
        self.budget_exhausted = False
        self.stage_text = ""
        task = asyncio.current_task()
        self._task = task          # cancel() 靠它中断「正在等待的那一段」
        ok = False
        try:
            # 硬超时：任何一段卡住都不会拖过总预算
            ok = await asyncio.wait_for(self._attempt_loop(is_reconnect=False),
                                       timeout=self.total_budget)
        except asyncio.TimeoutError:
            ok = await self._stop_on_budget()
        except asyncio.CancelledError:
            ok = False             # 用户点了「取消连接」：下面统一收尾
        finally:
            if self._task is task:
                self._task = None
        if self._cancel_requested:
            await self._abort("连接")
            return False
        return ok

    async def disconnect(self) -> None:
        """用户主动断开：关闭自动重连并清理两边。"""
        self._closing = True
        self.cancel()
        await self.device.close()
        try:
            await self.transport.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.state = STATE_IDLE
        self.attempt = 0
        self._emit(EVT_DISCONNECTED, "用户主动断开")

    def cancel(self) -> None:
        """取消当前连接/重连尝试。

        置 _closing：取消之后**绝不能再自动重连**（那是断线路径的行为）。
        真正的中断由 task.cancel() 完成，能打断正在等待的某一段（①查找 / ②直连 / ③扫描）。
        """
        self._cancel_requested = True
        self._closing = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    async def abort(self) -> None:
        """取消并收尾（界面线程的「取消连接」走这里：取消 + 释放链路 + 状态回未连接）。"""
        self.cancel()
        await self._abort("连接")

    async def _abort(self, label: str) -> None:
        """释放链路、状态收回「未连接」，并给出一句带耗时的提示。"""
        self._closing = True
        self.stage_text = ""
        try:
            await self._safe_close()      # device.close() + transport.disconnect() -> _cleanup()
        except Exception:  # noqa: BLE001
            pass
        self.state = STATE_IDLE
        self.attempt = 0
        self._emit(EVT_CANCELLED, f"已取消{label}（耗时 {self.elapsed():.1f}s）")

    # ---------------------------------------------------------------- 重连
    def _on_down(self, reason: str) -> None:
        if self._closing or not self.auto_reconnect:
            return
        self._emit(EVT_DISCONNECTED, reason or "链路断开")
        if self._task is not None and not self._task.done():
            return
        self.state = STATE_RECONNECTING
        self.started_at = self._clock()
        self.budget_exhausted = False
        self.stage_text = ""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._reconnect(), name="hp20-reconnect")

    async def _reconnect(self) -> None:
        try:
            await self.device.close()          # 停轮询、清在途
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.transport.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            ok = await asyncio.wait_for(self._attempt_loop(is_reconnect=True),
                                        timeout=self.total_budget)
        except asyncio.TimeoutError:
            ok = await self._stop_on_budget()
        except asyncio.CancelledError:
            ok = False
        if self._cancel_requested:
            await self._abort("重连")
            return
        if not ok:
            self.state = STATE_FAILED

    # ---------------------------------------------------------------- 主循环
    async def _attempt_loop(self, is_reconnect: bool) -> bool:
        tries = self.max_retries + 1        # 首次 + max_retries 次重试
        for attempt in range(1, tries + 1):
            self.attempt = attempt
            if self._closing:
                return False
            if self.remaining() <= 0:
                return await self._stop_on_budget()
            ok, detail = await self._try_stages()
            if ok:
                self.state = STATE_CONNECTED
                self.last_error = ""
                self._emit(EVT_RECONNECT_OK if is_reconnect else EVT_CONNECT_OK,
                           detail, attempt)
                return True
            self.last_error = detail
            self._emit(EVT_CONNECT_FAIL, detail or NOT_BROADCAST_HINT, attempt)
            if attempt <= self.max_retries:
                delay = self.backoff[min(attempt - 1, len(self.backoff) - 1)]
                if delay >= self.remaining():
                    # 退避都比剩下的预算长：没必要再等，直接收手
                    return await self._stop_on_budget()
                self.delays.append(float(delay))
                self._emit(EVT_RECONNECT_START,
                           f"{delay:g}s 后重试（第 {attempt + 1}/{tries} 次）", attempt + 1)
                await self._sleep(delay)
        if self._closing:
            return False            # 用户取消：不打「放弃重试 / 未找到」的噪
        self.state = STATE_FAILED if not self.connected else self.state
        if not self.connected:
            self._emit(EVT_NOT_FOUND, NOT_BROADCAST_HINT, tries)
        self._emit(EVT_GIVE_UP, self.last_error or NOT_BROADCAST_HINT, tries)
        return False

    async def _stop_on_budget(self) -> bool:
        """总预算用完：停下并给可操作提示（不再闷头重试）。"""
        self.budget_exhausted = True
        self.state = STATE_FAILED
        self.stage_text = ""
        hint = self._budget_hint()
        self.last_error = hint
        try:
            await self._safe_close()
        except Exception:  # noqa: BLE001
            pass
        self._emit(EVT_NOT_FOUND, hint, self.attempt)
        self._emit(EVT_GIVE_UP, hint, self.attempt)
        return False

    async def _try_stages(self) -> tuple[bool, str]:
        """三段式连接（真机实测：扫描扫不到 ≠ 连不上）。

        ① find_device_by_address（系统缓存，6s）→ 连接
        ② 直接用地址建 BleakClient（15s）→ 连接
        ③ 重新扫描（6s，按名/地址匹配）→ 连接
        每段独立超时；每段开始时发 stage 事件让界面能显示进度；三段都失败才交给外层退避。
        真机实测：①常常空等 10s 仍失败，②约 18s 成功 —— 因此默认值收成 6/15/6，
        避免用户长时间看不到任何反馈。重连从上次地址开始。
        """
        address = self.last_address or self.address
        if not address and self._preferred is not None:
            address = self._preferred.address
        errors: list[str] = []

        def _timeout(default: float) -> float:
            """每段超时都不能超过总预算剩余，保证总耗时可控。"""
            return max(0.05, min(float(default), self.remaining()))

        if address:
            self.stage_text = f"①按地址查找 {address}"
            self._emit(EVT_STAGE, self.stage_text, self.attempt)
            result = None
            try:
                result = await self.transport.find_by_address(address, _timeout(self.find_timeout))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"①查找异常 {exc}")
            if result is not None:
                ok, err = await self._connect_and_open(result, "①")
                if ok:
                    return True, f"①按地址查找并连接成功 {result.name} ({result.address})"
                errors.append(err)
            elif not errors:
                errors.append("①系统缓存里没有该地址")

        if address and self.remaining() > 0:
            self.stage_text = f"②直接连接 {address}"
            self._emit(EVT_STAGE, self.stage_text, self.attempt)
            connected = False
            try:
                await self.transport.connect_direct(address, _timeout(self.connect_timeout))
                connected = True
            except Exception as exc:  # noqa: BLE001
                errors.append(f"②直连失败 {exc}")
                await self._safe_close()
            if connected:
                ok, err = await self._open_after_connect("②")
                if ok:
                    self.last_address = address
                    return True, f"②直接连接成功 {address}"
                errors.append(err)

        if self.remaining() > 0:
            self.stage_text = "③重新扫描"
            self._emit(EVT_STAGE, self.stage_text, self.attempt)
        result = await self._resolve_by_scan(_timeout(self.scan_timeout))
        if result is None:
            errors.append("③扫描未发现匹配设备")
        else:
            ok, err = await self._connect_and_open(result, "③")
            if ok:
                return True, f"③扫描后连接成功 {result.name} ({result.address})"
            errors.append(err)
        return False, " | ".join(errors)

    async def _connect_and_open(self, result: ScanResult, tag: str) -> tuple[bool, str]:
        try:
            await self.transport.connect(result, self.connect_timeout)
        except Exception as exc:  # noqa: BLE001
            await self._safe_close()
            return False, f"{tag}连接失败 {exc}"
        ok, err = await self._open_after_connect(tag)
        if not ok:
            return False, err
        self.last_address = result.address
        return True, ""

    async def _open_after_connect(self, tag: str) -> tuple[bool, str]:
        """链路连上之后必须**握手识别成功**才算连上。

        P0：以前 device.open() 在「识别失败 / 识别成非 HP20」时照常返回，上层于是判
        CONNECTED 并发绿灯。链路通 ≠ 设备对，识别结论必须参与判定。
        """
        try:
            opened = await self.device.open()
        except Exception as exc:  # noqa: BLE001
            await self._safe_close()
            return False, f"{tag}握手失败 {exc}"
        if opened:
            return True, ""
        info = getattr(self.device, "info", None)
        what = (f"（{info.device_type} 0x{info.dev_header:04X}）" if info is not None
                else "（识别命令无应答）")
        await self._safe_close()
        return False, f"{tag}已连上但设备未识别为 HP20{what}"

    async def _safe_close(self) -> None:
        try:
            await self.device.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.transport.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def _resolve_by_scan(self, timeout: float | None = None) -> ScanResult | None:
        """第 3 段：重新扫描后按地址/名字匹配。"""
        timeout = self.scan_timeout if timeout is None else float(timeout)
        self._emit(EVT_SCAN, f"扫描 {timeout:g}s", self.attempt)
        try:
            found = await self.transport.scan(timeout)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"扫描失败: {exc}"
            return None
        wanted = (self.address or self.last_address or
                  (self._preferred.address if self._preferred else None))
        if wanted:
            for r in found:
                if r.address.lower() == wanted.lower():
                    return r
        if self.name_hint:
            hint = self.name_hint.lower()
            for r in found:
                if hint in (r.name or "").lower():
                    return r
        if wanted is None:
            # 不盲连：多个候选时交给用户在下拉框里选（以前是 RSSI 最强的任意设备）
            return pick_unique_candidate(found)
        return None

    # ---------------------------------------------------------------- 事件
    def _emit(self, kind: str, detail: str, attempt: int = 0) -> None:
        event = ConnectEvent(time.time(), kind, detail, attempt)
        self.events.append(event)
        cb = self.on_event
        if cb is not None:
            cb(event)
