"""HP20 协议的**唯一常量真源**：帧头 / 机型身份 / 时序 / 哨兵。

以前这些事实散在 framing.py（帧头）、hp20_commands.py（机型身份 + 超时）、
sim_device.py（0x29BF 哨兵又写一份）里，改一处不改另一处就会「发对帧但解析错」，
或者更糟：模拟器照旧通过测试，真机才暴露。现在全部收到这里，其余模块只做派生，
不再出现任何字面量。
"""
from __future__ import annotations


class Proto:
    """协议常量。纯数据，不 import 本项目任何模块（它是最底层，避免循环导入）。"""

    # ------------------------------------------------------------- 帧层
    REQ_HEADER = 0xAF          # 请求帧头
    RESP_HEADER = 0xDF         # 应答/通知帧头
    MIN_FRAME_LEN = 6          # header + dev_id + family + cmd + length + LRC
    MAX_PAYLOAD = 0xFF

    # ------------------------------------------------------------- 机型身份
    DEV_ID = 0x09              # HP20 帧内 dev_id
    FAMILY = 0x02              # HP20 帧内 family
    DEV_HEADER = DEV_ID | (FAMILY << 8)          # 0x0209，派生而非再写一遍
    IDENT_PAYLOAD = bytes((DEV_ID, FAMILY))      # 识别应答 payload 前两字节 == 09 02
    ACK_MIN_PAYLOAD = 1        # 实测写命令应答带 1 字节 payload

    # ------------------------------------------------------------- 时序（ms->s）
    # 反汇编里的文档值 0x3E8 = 1000 ms；真机部分应答要 0.7~1.5 s，1000 ms 会误判，
    # 因此运行时默认用 RESPONSE_TIMEOUT_S。两个值都留在这里，避免「改一个不生效」。
    PROTOCOL_TIMEOUT_S = 1.0
    RESPONSE_TIMEOUT_S = 2.0
    POLL_INTERVAL_S = 0.125    # 125 ms 轮询
    TIMEOUT_TICK_S = 0.010     # 等应答的粒度 10 ms
    WRITE_GAP_S = 0.015        # 写节流 15 ms
    POST_CONNECT_DELAY_S = 1.0 # connect -> 1s -> 订阅 -> 1s -> 握手
    RECONNECT_BACKOFF_S = (1.0, 2.0, 4.0, 8.0)
    RECONNECT_MAX_ATTEMPTS = 4

    # ------------------------------------------------------------- 设备侧固定值
    NAME_LEN = 20              # 蓝牙名 20 字节 ASCII 补 0
    TIMER_OFF_SENTINEL = 0x29BF   # W2「计时未启用」哨兵：SS=(>>0 &0x3F)=63 >= 60 -> OFF
