"""传输层: 抽象接口 + 真实 BLE(bleak) + 独立 asyncio 事件循环桥。

bleak 全部运行在 AsyncRunner 的独立线程里, Tk 主线程只做 submit/轮询 future,
因此 BLE IO 永远不会阻塞界面。
"""
from __future__ import annotations

import abc
import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

try:  # bleak 只服务真实 BLE; 缺失时 --sim 仍应可用
    from bleak import BleakClient, BleakScanner
    from bleak.backends.device import BLEDevice
    BLEAK_ERR: str | None = None
except Exception as _exc:  # noqa: BLE001
    BleakClient = BleakScanner = BLEDevice = None  # type: ignore[assignment]
    BLEAK_ERR = str(_exc)


# 真机实测（2026 真机 AA:BB:CC:DD:EE:FF）：service fff0，char fff1[notify,write-without-response]
# 同时收发；fff2[notify,read]，fff3[write-without-response]。APK 字符串里的 ff01/ff02/ffff
# 在真机上并不存在，所以特征一律"发现后按属性挑"，不按 UUID 写死
# （UUID 常量本身已无消费者，2026-10 删除，避免被当成"可用 API"再引回来）。
class TransportError(RuntimeError):
    pass


@dataclass
class ScanResult:
    address: str
    name: str
    rssi: int | None = None
    source: str = "ble"
    detail: str = ""
    raw: Any = field(default=None, repr=False)

    def label(self) -> str:
        rssi = f"{self.rssi} dBm" if self.rssi is not None else "RSSI ?"
        return f"{self.name}  [{rssi}]  {self.address}"


class Transport(abc.ABC):
    """上层只依赖这 6 个成员。"""

    def __init__(self) -> None:
        self.on_data: Callable[[bytes], None] | None = None
        self.on_disconnect: Callable[[str], None] | None = None
        self.on_system: Callable[[str], None] | None = None

    def describe(self) -> str:
        return self.__class__.__name__

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        ...

    @property
    def char_summary(self) -> list[str]:
        return []

    @abc.abstractmethod
    async def scan(self, timeout: float = 6.0) -> list[ScanResult]:
        ...

    @abc.abstractmethod
    async def connect(self, target: ScanResult | str | None = None,
                      timeout: float = 20.0) -> None:
        ...

    async def find_by_address(self, address: str, timeout: float = 10.0) -> "ScanResult | None":
        """第 1 段：按已知地址查（实机走 Windows 缓存，不需要广播）。默认实现是扫描匹配。"""
        for result in await self.scan(timeout):
            if result.address.lower() == address.lower():
                return result
        return None

    async def connect_direct(self, address: str, timeout: float = 20.0) -> None:
        """第 2 段：直接用地址建连接（不依赖缓存/广播）。默认实现复用 connect。"""
        await self.connect(address, timeout)

    @abc.abstractmethod
    async def disconnect(self) -> None:
        ...

    @abc.abstractmethod
    async def write(self, data: bytes) -> None:
        ...


class BleTransport(Transport):
    """真实 BLE GATT 传输。

    特征解析顺序: 已知 UUID -> 按属性(notify/indicate、write)扫描全部特征。
    两者都失败时抛出带完整特征表的 TransportError, 便于对着真机改 UUID。
    """

    def __init__(self, write_with_response: bool = False,
                 notify_uuid: str | None = None,
                 write_uuid: str | None = None) -> None:
        super().__init__()
        self.write_with_response = write_with_response
        # 留出显式覆盖口（排查其它固件时用）；默认 None = 自动发现
        self.notify_uuid = notify_uuid
        self.write_uuid = write_uuid
        self._client: Any = None
        self._notify_char: Any = None
        self._write_char: Any = None
        self._known: dict[str, Any] = {}
        self._chars: list[str] = []

    def describe(self) -> str:
        return "蓝牙模式"

    @property
    def is_connected(self) -> bool:
        return bool(self._client is not None and self._client.is_connected)

    @property
    def char_summary(self) -> list[str]:
        return list(self._chars)

    # ------------------------------------------------------------------ 扫描
    async def scan(self, timeout: float = 6.0) -> list[ScanResult]:
        if BleakScanner is None:
            raise TransportError(f"bleak 不可用: {BLEAK_ERR}")
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        results: list[ScanResult] = []
        items = found.items() if isinstance(found, dict) else [(d.address, (d, None)) for d in found]
        for address, pair in items:
            device, adv = pair if isinstance(pair, tuple) else (pair, None)
            name = getattr(device, "name", None)
            if not name and adv is not None:
                name = getattr(adv, "local_name", None)
            rssi = getattr(adv, "rssi", None) if adv is not None else None
            uuids = list(getattr(adv, "service_uuids", []) or []) if adv is not None else []
            detail = ", ".join(uuids[:4])
            results.append(ScanResult(address, name or "(未命名)", rssi, "ble", detail, device))
            self._known[address] = device
        results.sort(key=lambda r: (r.rssi if r.rssi is not None else -999), reverse=True)
        return results

    # ------------------------------------------------------------------ 连接
    async def connect(self, target: ScanResult | str | None = None,
                      timeout: float = 20.0) -> None:
        if BleakClient is None:
            raise TransportError(f"bleak 不可用: {BLEAK_ERR}")
        address = target.address if isinstance(target, ScanResult) else (target or "")
        if not address:
            raise TransportError("未指定设备地址")
        await self._open_client(self._known.get(address, address), address, timeout)

    async def connect_direct(self, address: str, timeout: float = 20.0) -> None:
        """第 2 段：直接用地址字符串建 BleakClient（真机实测：扫描扫不到也能连上）。"""
        if not address:
            raise TransportError("未指定设备地址")
        await self._open_client(address, address, timeout)

    async def find_by_address(self, address: str, timeout: float = 10.0) -> ScanResult | None:
        """第 1 段：BleakScanner.find_device_by_address（走系统缓存，不需要设备在广播）。"""
        if BleakScanner is None:
            raise TransportError(f"bleak 不可用: {BLEAK_ERR}")
        try:
            device = await BleakScanner.find_device_by_address(address, timeout=timeout)
        except Exception as exc:  # noqa: BLE001  找不到时 bleak 会抛 DeviceNotFound
            if "not found" in str(exc).lower() or "NotFound" in type(exc).__name__:
                return None
            raise TransportError(f"按地址查找失败: {exc}") from exc
        if device is None:
            return None
        self._known[address] = device
        return ScanResult(device.address, getattr(device, "name", None) or "(未命名)",
                          None, "ble", "系统缓存地址", device)

    async def _open_client(self, target: Any, address: str, timeout: float) -> None:
        # 兜底：上一次的残留（哪怕是半死客户端）必须先清掉，否则每失败一次就在
        # Windows BLE 栈里多留一个僵尸连接，表现为「第一次能连，之后越连越连不上」。
        await self._cleanup()
        client = BleakClient(target, disconnected_callback=self._on_disconnected,
                             timeout=timeout)
        self._client = client
        try:
            await client.connect()
            self._resolve_chars()
            await client.start_notify(self._notify_char, self._handle_notify)
        except BaseException:      # noqa: BLE001  含 CancelledError：失败/取消都必须回滚
            await self._cleanup()
            raise
        self._system(f"已连接 {address}, 通知特征 {self._notify_char.uuid}, "
                     f"写特征 {self._write_char.uuid}")

    def _resolve_chars(self) -> None:
        """按属性挑特征：RX = 第一个带 notify/indicate，TX = 第一个带写。

        真机上 fff1 同时具备 notify 与 write-without-response，因此两者会选到同一个特征。
        完全不依赖 UUID，也不因为 UUID 与 APK 字符串不一致而判失败。
        """
        collection = self._client.services
        chars = list(collection.characteristics.values())
        self._chars = [f"{c.uuid}  handle={getattr(c, 'handle', '?')}  "
                       f"props={','.join(getattr(c, 'properties', []) or [])}"
                       for c in chars]
        notify = self._pick_uuid(chars, self.notify_uuid) or self._pick_props(
            chars, ("notify", "indicate"))
        write = self._pick_uuid(chars, self.write_uuid) or self._pick_props(
            chars, ("write-without-response",)) or self._pick_props(chars, ("write",))
        table = "\n".join(self._chars) or "(未发现特征)"
        if notify is None:
            raise TransportError(f"未发现任何 notify/indicate 特征。设备特征表:\n{table}")
        if write is None:
            raise TransportError(f"未发现任何 write 特征。设备特征表:\n{table}")
        if len(notify.properties or []) and "write-without-response" in (
                getattr(notify, "properties", []) or []):
            self.write_with_response = False
        self._notify_char, self._write_char = notify, write
        self._system(f"特征选择: RX={notify.uuid} TX={write.uuid} "
                     f"(write_with_response={self.write_with_response})")

    @staticmethod
    def _pick_uuid(chars, uuid: str | None):
        if not uuid:
            return None
        target = str(uuid).lower()
        for c in chars:
            if str(c.uuid).lower() == target:
                return c
        return None

    @staticmethod
    def _pick_props(chars, props: tuple[str, ...]):
        want = set(props)
        for c in chars:
            have = {p.lower() for p in (getattr(c, "properties", []) or [])}
            if have & want:
                return c
        return None

    async def disconnect(self) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        """统一收尾：stop_notify → disconnect → 三处引用置 None。

        引用先置 None 再 await：await 期间用户再点「连接」也不会复用到正在拆的旧对象。
        每一段的失败都不阻断后面 —— 收尾本身失败不能变成新的泄漏。
        """
        client, notify = self._client, self._notify_char
        self._clear_refs()
        if client is None:
            return
        try:
            if notify is not None and client.is_connected:
                await client.stop_notify(notify)
        except Exception as exc:  # noqa: BLE001
            self._system(f"停止通知时异常(已忽略): {exc}")
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception as exc:  # noqa: BLE001
            self._system(f"断开时异常(已忽略): {exc}")

    def _clear_refs(self) -> None:
        """只清引用（同步版本）：bleak 的断链回调是同步的，那里不能 await。"""
        self._client = None
        self._notify_char = None
        self._write_char = None
        self._chars = []

    async def write(self, data: bytes) -> None:
        if not self.is_connected:
            raise TransportError("未连接")
        await self._client.write_gatt_char(self._write_char, bytes(data),
                                          response=self.write_with_response)

    # ------------------------------------------------------------------ 回调
    def _handle_notify(self, _char: Any, payload: bytearray) -> None:
        cb = self.on_data
        if cb is not None:
            cb(bytes(payload))

    def _on_disconnected(self, _client: Any) -> None:
        # 走同一套清理（同步版本）：旧客户端已经掉了，引用绝不能留着
        self._clear_refs()
        self._system("链路已断开")
        cb = self.on_disconnect
        if cb is not None:
            cb("链路已断开")

    def _system(self, text: str) -> None:
        cb = self.on_system
        if cb is not None:
            cb(text)


class AsyncRunner:
    """把 asyncio 事件循环放到独立守护线程, 供 Tk 主线程提交协程并轮询结果。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread: threading.Thread | None = None
        self._started = False
        self._futures: set[concurrent.futures.Future] = set()   # 在途提交，stop 时统一结算

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, name="ble-io", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, fn: Callable[..., Any], *args: Any) -> concurrent.futures.Future:
        """fn 是 async 可调用对象; 协程在 IO 线程内创建, 避免跨线程持有协程。"""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        if not self._started:
            raise RuntimeError("AsyncRunner 未启动")

        def _create() -> None:
            if fut.cancelled():
                return
            try:
                coro = fn(*args)
            except Exception as exc:  # noqa: BLE001
                fut.set_exception(exc)
                return
            task = self.loop.create_task(coro)

            def _done(t: asyncio.Task) -> None:
                self._futures.discard(fut)
                if fut.cancelled():
                    return
                if t.cancelled():
                    fut.cancel()
                elif t.exception() is not None:
                    fut.set_exception(t.exception())
                else:
                    fut.set_result(t.result())

            task.add_done_callback(_done)

        self._futures.add(fut)
        self.loop.call_soon_threadsafe(_create)
        return fut

    def call_soon(self, fn: Callable[..., Any], *args: Any) -> None:
        """在 IO 线程里安排一次同步调用（UI 线程用，比如唤醒轮询）。"""
        if self._started:
            self.loop.call_soon_threadsafe(fn, *args)

    def stop(self, timeout: float = 3.0) -> None:
        """停 IO 线程：取消未完成 task -> 停 loop -> join（带 is_alive 检查）。

        关窗后线程绝不能悬挂；重复调用安全。join 超时的线程会留回 self._thread，
        允许下一次 stop 重试（而不是丢掉引用让它变成看不见的僵尸线程）。
        """
        was_started, self._started = self._started, False
        thread, self._thread = self._thread, None
        if not was_started and thread is None:
            return
        if self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(self._cancel_pending)
                self.loop.call_soon_threadsafe(self.loop.stop)
            except RuntimeError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                self._thread = thread
        # 结算还在途的 future：界面侧 _watch 靠 fut.done() 收尾，留一个永远不结算的
        # future 等于留一个关窗也醒不来的等待。
        for fut in list(self._futures):
            if not fut.done():
                fut.cancel()
        self._futures.clear()

    def _cancel_pending(self) -> None:
        """（在 IO 线程里跑）取消所有未完成 task，别把悬挂协程留给进程退出。"""
        for task in asyncio.all_tasks(self.loop):
            task.cancel()
