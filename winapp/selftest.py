"""非 GUI 端到端自检（--selftest）：在进程内模拟设备上跑通协议 / 时序 / CSV / 重连。

原来整段住在 atk_hp20.py 里（_selftest_async，252 行，全项目最长函数，也是圈复杂度 80
的主要来源）。搬出后行为、输出格式、44 项检查内容与退出码全部保持不变；
唯一的耦合 link_button_states 由调用方注入，避免 selftest <-> atk_hp20 循环导入。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import framing
import hp20_commands as hc
from connection import STATE_CONNECTED, ConnectionManager
from hp20_device import Hp20Device
from logs import make_recorder
from sim_device import SimTransport


async def run_selftest(opts, link_button_states) -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, info: str = "") -> bool:
        checks.append((label, bool(ok), info))
        print(f"  [{len(checks)}] {label:<24} {'PASS' if ok else 'FAIL'}  {info}")
        return ok

    frames: list[tuple[str, bytes]] = []
    statuses: list[hc.RealtimeStatus] = []
    logs: list[str] = []
    conn_events: list[ConnectEvent] = []
    transport = SimTransport(latency=0.01, tick_dt=0.02)
    device = Hp20Device(transport, dev_id=opts.dev_id, family=opts.family,
                        timeout=opts.timeout, tick=0.005, write_gap=opts.gap,
                        poll_interval=0.05, retries=opts.retries)
    device.on_frame = lambda d, b: frames.append((d, b))
    device.on_status = statuses.append
    device.on_system = logs.append
    manager = ConnectionManager(transport, device, scan_timeout=1.0,
                                max_retries=2, backoff=(0.05, 0.1))
    manager.on_event = conn_events.append
    csv_path = os.path.join(tempfile.mkdtemp(prefix="hp20_selftest_"), "curve.csv")
    recorder = make_recorder("full", csv_path)
    compact_path = os.path.join(os.path.dirname(csv_path), "compact.csv")
    compact = make_recorder("compact", compact_path)
    ok_all = True

    print("=== HP20 上位机自检 (模拟设备, 实测协议) ===")
    try:
        ok_all &= check("扫描并连接", await manager.connect(), manager.state_text())
        info = device.info
        ok_all &= check("握手识别 (cmd 0x00)", bool(info and info.is_hp20),
                        f"{info.device_type} 0x{info.dev_header:04X}" if info else "无")
        ident_tx = next((raw for d, raw in frames
                         if d == "TX" and framing.parse_frame(raw).cmd == hc.CMD_IDENTIFY), b"")
        ok_all &= check("握手帧字节", framing.to_hex(ident_tx) == "AF 09 02 00 00 46",
                        framing.to_hex(ident_tx) or "无")
        ident_rx = next((raw for d, raw in frames
                         if d == "RX" and framing.parse_frame(raw).cmd == hc.CMD_IDENTIFY), b"")
        ok_all &= check("识别应答头容错", bool(ident_rx) and
                        framing.parse_frame(ident_rx).dev_header == 0xFFFF,
                        framing.to_hex(ident_rx) or "无")

        presets = await device.read_presets()
        ok_all &= check("读预设 (cmd 0x04)", presets is not None, str(presets))
        # ---- 开始加热：先 0x05 选挡位（设备会把目标设成挡位预设），自定义目标在其后覆盖
        mark = len(frames)
        ok_all &= check("开始加热（只用挡位预设）", await device.start_heating(0),
                        "T1 预设 66℃")
        preset_tx = [framing.to_hex(raw) for d, raw in frames[mark:]
                     if d == "TX" and framing.parse_frame(raw).cmd in (hc.CMD_SET_THERMOSTAT,
                                                                      hc.CMD_SET_PREHEAT)]
        ok_all &= check("只用预设时只发 0x05", preset_tx == ["AF 09 02 05 01 00 40"],
                        " → ".join(preset_tx) or "无")
        mark = len(frames)
        ok_all &= check("开始加热（自定义 60℃）", await device.start_heating(0, 60),
                        "T1 + 覆盖目标")
        combo = [framing.to_hex(raw) for d, raw in frames[mark:]
                 if d == "TX" and framing.parse_frame(raw).cmd in (hc.CMD_SET_THERMOSTAT,
                                                                  hc.CMD_SET_PREHEAT)]
        ok_all &= check("自定义目标帧在选挡位之后",
                        combo == ["AF 09 02 05 01 00 40", "AF 09 02 03 03 04 3C 00 00"],
                        " → ".join(combo) or "无")
        ack = next((raw for d, raw in frames
                    if d == "RX" and framing.parse_frame(raw).cmd == hc.CMD_SET_THERMOSTAT), b"")
        ok_all &= check("写应答含 payload", bool(ack) and len(
            framing.parse_frame(ack).payload) >= hc.ACK_MIN_PAYLOAD,
                        framing.to_hex(ack) or "无")

        t0 = time.monotonic()
        last = None
        while time.monotonic() - t0 < 2.0:
            await asyncio.sleep(0.03)
            cur = device.status
            if cur is not None and cur is not last:
                last = cur
                recorder.record(cur)
                compact.record(cur)
        st = device.status
        ok_all &= check("读实时状态 (cmd 0x0A)", st is not None and len(statuses) > 3,
                        f"{len(statuses)} 帧")
        first_temp = statuses[0].current_temp if statuses else 0
        ok_all &= check("温度取 payload[0:2]", bool(st and st.current_temp > first_temp),
                        f"{first_temp}℃ → {st.current_temp}℃" if st else "无")
        ok_all &= check("[3:5] = 输入电压 ×0.1V",
                        bool(st and st.voltage_text == "27.8V"),
                        f"raw={st.input_voltage_raw} -> {st.voltage_text}" if st else "无")
        ok_all &= check("目标温度 = W1&0x3FF", bool(st and st.target_temp == 60),
                        f"target={st.target_temp}" if st else "无")
        ok_all &= check("模式按实测映射", bool(st and st.mode in (0, 1, 2, 3, 4, 8)),
                        f"mode={st.mode} {st.mode_name}·{st.slot_text}" if st else "无")
        ok_all &= check("输出状态字节 [5]",
                        bool(st and st.output_state_text in ("待机", "加热模式运行中")),
                        st.output_state_text if st else "无")
        ok_all &= check("加热计时（64 进制打包）",
                        bool(st and (st.timer_text == "OFF" or ":" in st.timer_text)),
                        f"{st.timer_text}  W2=0x{st.w2:04X}={st.w2}s" if st else "无")
        ok_all &= check("计时标签随模式",
                        bool(st and st.timer_label in ("加热时间", "休眠倒计时")),
                        f"{st.timer_label} {st.timer_text}" if st else "无")
        ok_all &= check("功率占比 0~100",
                        bool(st and 0 <= st.power_percent <= 100),
                        st.power_percent_text if st else "无")
        ok_all &= check("帧内无风扇信息",
                        bool(st and not hasattr(st, "fan_on") and not hasattr(st, "fan_text")),
                        "状态帧不含风扇字段（W2 bit13 已证伪）")
        ok_all &= check("供电/输出功率（W）",
                        bool(st and st.supply_power_w == 140
                             and st.output_power_w >= 0),
                        f"输出 {st.power_text} / 供电 {st.supply_text}" if st else "无")

        # ---- L 阶段 bug：W2 每秒在变，轮询绝不能补读 0x04/0x0C
        mark_poll = len(frames)
        await asyncio.sleep(1.6)
        poll_cmds = [framing.parse_frame(raw).cmd for d, raw in frames[mark_poll:]
                     if d == "TX"]
        hit = [c for c in poll_cmds if c in (hc.CMD_GET_THERMOSTAT, hc.CMD_GET_REPAIR)]
        ok_all &= check("轮询期间不补读参数", not hit,
                        f"{len(poll_cmds)} 帧 TX，无 0x04/0x0C" if not hit
                        else f"出现补读命令 {hit}")

        # ---- 停止加热（默认路径）：0x05 [3] 选 T4 停止通道
        mark = len(frames)
        ok_all &= check("停止加热", await device.stop_heating(), "0x05 [3]")
        stop_tx = [framing.to_hex(raw) for d, raw in frames[mark:]
                   if d == "TX" and framing.parse_frame(raw).cmd == hc.CMD_SET_PREHEAT]
        ok_all &= check("停止帧字节", stop_tx[:1] == ["AF 09 02 05 01 03 3D"],
                        stop_tx[0] if stop_tx else "无")
        await asyncio.sleep(0.6)
        stopped = device.status
        ok_all &= check("停止后功率归零/目标 0",
                        bool(stopped and stopped.output_power_w == 0
                             and stopped.target_temp == 0),
                        stopped.summary() if stopped else "无")
        ok_all &= check("备用停止路径仍在",
                        framing.to_hex(hc.frame_stop_by_target())
                        == "AF 09 02 03 03 04 00 00 3C",
                        "0x03 index=4 temp=0")

        curve = [120, 20, 150, 30, 200, 15, 80, 40]
        ok_all &= check("写回流曲线 (cmd 0x06)", await device.write_reflow(curve))
        ok_all &= check("读回流曲线 (cmd 0x07)", await device.read_reflow() == curve, str(curve))
        params = [100, 10, 130, 20, 160, 30, 190, 40]
        ok_all &= check("写维修参数 (cmd 0x0B)", await device.write_repair(params))
        ok_all &= check("读维修参数 (cmd 0x0C)", await device.read_repair() == params)
        ok_all &= check("读设备名 (cmd 0x09)", await device.read_name() == "HP20",
                        str(device.device_name))

        # ---- 断线自动重连（用户实测「经常断开」的核心用例）
        before = len(statuses)
        transport.simulate_disconnect("自检：模拟设备主动断开")
        await asyncio.sleep(0.05)
        saw_reconnect = False
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if manager.state == STATE_CONNECTED and len(statuses) > before + 2:
                saw_reconnect = True
                break
            await asyncio.sleep(0.05)
        ok_all &= check("断线自动重连", saw_reconnect,
                        f"状态 {manager.state_text()}，重连后新增 {len(statuses) - before} 帧")
        ok_all &= check("重连后重新握手", sum(
            1 for e in conn_events if e.kind.endswith("connect_ok") or e.kind == "connect_ok") >= 2,
                        f"{[e.kind for e in conn_events][-6:]}")
        ok_all &= check("重连后有事件记录", len(manager.recent(20)) >= 3,
                        f"{len(manager.recent(20))} 条")
    finally:
        rows = recorder.close()
        try:
            await manager.disconnect()
        except Exception:  # noqa: BLE001
            pass

    ok_all &= check("断开连接", not transport.is_connected)
    compact_rows = compact.close()
    ok_all &= check("CSV 含功率列", "功率占比(%)" in recorder.header and rows > 0,
                    f"表头含 功率占比(%)，{rows} 行")
    if rows > 0:
        with open(csv_path, encoding="utf-8-sig") as fh:
            head = fh.readline().strip()
            body = fh.readline().strip()
        ok_all &= check("CSV 数据行可解析", "," in body and body.split(",")[2] != "",
                        body[:60] + "…")
        ok_all &= check("CSV 表头字段", "功率占比(%)" in head and "当前温度(℃)" in head,
                        head[:60] + "…")
        with open(csv_path, "rb") as fh:
            bom = fh.read(3)
        ok_all &= check("CSV 带 UTF-8 BOM（Excel 不乱码）", bom == b"\xef\xbb\xbf",
                        "EF BB BF")
        with open(compact_path, encoding="utf-8-sig") as fh:
            chead = fh.readline().strip()
            cbody = fh.readline().strip()
        ok_all &= check("简洁 CSV 列头对齐手机 APP", chead == "时间,V(℃),A(W)",
                        f"{chead}，{compact_rows} 行")
        ok_all &= check("简洁 CSV 行格式", cbody.count(",") == 2 and ":" in cbody,
                        cbody or "无")

    print("\n关键帧:")
    wanted = {hc.CMD_IDENTIFY, hc.CMD_GET_THERMOSTAT, hc.CMD_SET_THERMOSTAT,
              hc.CMD_SET_PREHEAT, hc.CMD_REALTIME}
    seen: set[tuple[str, int]] = set()
    shown = 0
    for direction, raw in frames:
        try:
            cmd = framing.parse_frame(raw).cmd
        except ValueError:
            continue
        if cmd in wanted and (direction, cmd) not in seen:
            seen.add((direction, cmd))
            print(f"  {direction} {framing.to_hex(raw)}")
            shown += 1
        if shown >= 8:
            break
    print("\n连接事件:")
    for event in manager.recent(20):
        print("  " + event.format())

    tx = sum(1 for d, _ in frames if d == "TX")
    rx = sum(1 for d, _ in frames if d == "RX")
    print(f"\n收发统计: TX {tx} / RX {rx} 帧；设备统计 {device.stats}")
    if logs:
        print(f"最后一条设备消息: {logs[-1]}")
    # ---- M 阶段：计时判据放宽（设备上报值可能大于面板设置值）
    ok_all &= check("计时 = 64 进制打包",
                    hc.format_timer(1600) == "25:00"
                    and hc.format_timer(1280) == "20:00"
                    and hc.format_timer(1229) == "19:13"
                    and hc.format_timer(0x29BF) == "OFF",
                    f"1600->{hc.format_timer(1600)} 1280->{hc.format_timer(1280)} "
                    f"1229->{hc.format_timer(1229)} 0x29BF->{hc.format_timer(0x29BF)}")

    # ---- M 阶段：连接总预算（三段全失败也不许闷头重试）
    budget_mgr = ConnectionManager(transport, device, address="AA:BB:CC:DD:EE:FF",
                                   scan_timeout=0.4, find_timeout=0.4,
                                   connect_timeout=0.4, max_retries=3,
                                   backoff=(0.1, 0.2, 0.4), total_budget=0.9)
    budget_events: list = []
    budget_mgr.on_event = budget_events.append
    b_start = time.monotonic()
    budget_ok = await budget_mgr.connect()
    b_spent = time.monotonic() - b_start
    hint_ok = any(e.kind == "give_up" and "设备未响应" in e.detail for e in budget_events)
    ok_all &= check("连接总预算生效",
                    (not budget_ok) and b_spent <= 1.3 and hint_ok,
                    f"预算 0.9s，实际 {b_spent:.2f}s，give_up 文案{'有' if hint_ok else '缺'}")

    ok_all &= check("断开后连接按钮可用",
                    link_button_states(False) == ("normal", "disabled")
                    and link_button_states(False, True) == ("normal", "disabled")
                    and link_button_states(True) == ("disabled", "normal"),
                    "断开/扫描完成 enabled，连接中显示「取消连接」")
    passed = sum(1 for _, ok, _ in checks if ok)

    print(f"\n结果: {'PASS' if ok_all else 'FAIL'} ({passed}/{len(checks)})")
    return 0 if ok_all else 1
