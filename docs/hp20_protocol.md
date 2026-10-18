# HP20 加热台 BLE 协议规格（逆向自正点原子 atk_xtool V2.0.0）

> **发布说明**：本文档只描述协议字节布局、字段含义与时序等事实性信息；逆向过程中的反汇编证据、二进制与厂商资料不随本仓库分发。本项目与广州市星翼电子科技有限公司（正点原子）无关联。

> 目标设备：ALIENTEK 正点原子 **HP20 加热台**（回流焊 / 预热 / 维修三用）
> 唯一依据：APK `atk_xtool_V2.0.0.apk` 内的 Dart AOT 快照（Dart 3.10.3, product, compressed-pointers）。
> 字符串/ELF 结构用 `lib/x86_64/libapp.so`；**所有反汇编证据来自 `lib/arm64-v8a/libapp.so`**（blutter 仅支持 arm64，两份快照是同一份 Dart 代码）。
> 全部结论均带证据与置信度；未证实的内容集中在 **第 10 章 未确认清单**，不得当作规格实现。

---

## 1. 速览（TL;DR，照这个就能通起来）

| 项 | 值 |
|---|---|
| Service UUID | `0000ff01-0000-1000-8000-00805f9b34fb` |
| Notify（设备→手机） | `0000ff02-0000-1000-8000-00805f9b34fb`，需写 CCCD `00002902-...` = `01 00` |
| Write（手机→设备） | `0000ffff-0000-1000-8000-00805f9b34fb` |
| 设备头（HP20） | `dev_id = 0x09`，`family = 0x02` ⇒ 请求帧固定以 **`AF 09 02`** 开头，应答/通知固定以 **`DF 09 02`** 开头 |
| 帧结构 | `header, dev_id, family, cmd, length, payload[length], LRC`（7 段），总长 = 6 + length |
| LRC | `LRC = (256 - (sum(前面所有字节) & 0xFF)) & 0xFF` |
| 轮询周期 | **125 ms**（cmd `0x0A` 实时帧） |
| 应答超时 | **1000 ms** |
| 写间隔 | **15 ms**（内部写队列节流） |
| 并发 | **单命令在途**：上一条未完成时新命令被拒绝（App 打日志“指令任务繁忙，拒绝重复调用”） |
| 连接后 | `connect → 等 1 s → 发现服务 → 订阅通知 → 等 1 s → 发 cmd 0x00 握手` |

最短可用 hello（读一次实时数据）：

```
TX  AF 09 02 0A 00 3C
RX  DF 09 02 0A 0B <11 字节 payload> <LRC>
```

---

## 2. 证据与可复现性

| 证据类型 | 位置 |
|---|---|
| 反汇编（含 Dart IL 注释、常量池解析、函数名） | `out/blutter/asm/atk_xtool/**`（blutter 生成，可重复生成） |
| 对象池/对象转储 | `out/blutter/objs.txt`、`out/blutter/pp.txt` |
| 生成方式 | `tools/re/run_blutter2.bat`（arm64 libapp.so + libflutter.so，Dart 3.10.3 现场编译 dartvm） |
| 关键函数切片 | `out/evidence_hp20/*.txt`（`tools/re/dump_evidence.py` 生成） |
| 帧自检 | `tools/re/verify_frames.py`（独立重算 LRC，输出见第 11 章） |
| 参考实现 | `tools/re/hp20_proto.py`（编解码 + 实时帧解析）+ `tools/re/gen_hp20_commands.py` |

Dart 层源码结构（来自快照内字符串与 blutter 的库路径）：

- `core/protocol/frame_builder.dart` → `FrameBuilder::buildRaw（0x3d5ae8）` 组帧
- `core/protocol/checksum.dart` → `Checksum::calculationCheck（0x3d5ce0）` LRC
- `core/protocol/frame_parser.dart` → `FrameParser::feed（0x43565c）` 收帧/拆包
- `core/ble/device_session.dart` → 连接、订阅、命令收发与超时、`enum DevType`
- `core/ble/write_queue.dart` → 写串行化（15 ms）
- `core/protocol/managecmd/managecmd_hp20.dart` → `ManagecmdHp20` 各命令
- `data/viewmodel/hp20viewmodel.dart` → 字段语义（单位/功率/时间/模式字符串）
- `presentation/pages/hp20page/*.dart` → 各参数页的 UI 标签（确定参数含义）

---

## 3. 连接、发现与设备识别

### 3.1 识别方式：**不是靠蓝牙名，而是靠握手应答**

App 的连接流程（`DeviceSession::start @0x434bf0`）：

```
connect()                        // ble_client.connect
Future.delayed(1s)               // Duration@6c2d21 = 0xF4240 µs
_discover()                      // 发现 GATT 服务/特征
_subscribe()                     // 打开 notify(+CCCD)
Future.delayed(1s)
RegisterDev(): sendCMDWaitResult(cmd = 0x00)
```

`RegisterDev（@0x431c74）` 用 **应答 payload 的前两个字节** 拼 16 位头：

```asm
; payload[0] → x1 , payload[1] → x0
```

然后 `CreatManageCMD（@0x431d54）` 在 7 个 `DevType` 枚举里按 `field_14 == header` 选出型号
（`firstWhere` 的判定闭包 `@0x434a30`：`t.field_14 == this.field_2b`），再据此实例化 `ManagecmdHp20` 等实现。

**DevType 枚举（`objs.txt` 原文，高置信）**

| 枚举值 | 名称 | `field_14` | 帧内字节 |
|---|---|---|---|
| 0 | BLE03 | 0x0000 | — |
| 1 | HP15 | 0x0207 | `07 02` |
| **2** | **HP20** | **0x0209** | **`09 02`** |
| 3 | DM40 | 0x0305 | `05 03` |
| 4 | ND1 | 0x0306 | `06 03` |
| 5 | EL15 | 0x0307 | `07 03` |
| 6 | BLE | 0xFFFF | — |

> 结论（问题 1）：HP20 帧第 2 字节 = **0x09**，第 3 字节 = **0x02**。
> EL15 = `07 / 03`、DM40 = `05 / 03`、ND1 = `06 / 03`、HP15 = `07 / 02`。
> 因此“第 2 字节是设备 ID”成立，但 **第 3 字节不是恒定的 0x03**，它和 dev_id 一起构成型号头；HP20 属于 `0x02` 家族。
> 该 16 位头同时也是设备在 `DevType` 表里的身份键（`0x0209`）。

### 3.2 扫描期要不要按名字过滤？

- 代码里**没有**基于 `name.contains("HP20")` 之类的过滤逻辑：唯一的 "HP20" 字面量出现在
  `atk_xtoolmainpage.dart @0x430434`，是按 **Managecmd 运行时类型** 决定标题文案（`cmp x1, #0xb6d → "HP20"`），
  与扫描筛选无关（高置信）。
- App 侧 `BleClient::startScan（@0x3c0c08）` 只是转调 FlutterBluePlus 的 `startScan`，无服务/名字参数（高置信）。
- 设备名后缀 `_BLE` / `_SPP`：**本次快照里没有找到任何 `_BLE`/`_SPP` 字面量**（中置信：无法证伪，但静态未见）。
  设备名由 cmd 0x09 读回（见 5.8/5.9），App 侧不做名字规则判断。
- 实践建议：**连接后先用 cmd `0x00` 握手，判 payload[0..1] 是否 `09 02`** 来确认是 HP20；
  这比名字更可靠，也正是官方 App 的做法。

---

## 4. 帧格式

### 4.1 结构与组帧实现

`FrameBuilder::buildRaw（@0x3d5ae8）` 的反汇编即组帧规格：

```asm
```

因此请求帧布局：

| 偏移 | 长度 | 含义 |
|---|---|---|
| 0 | 1 | 帧头 `0xAF`（响应/通知为 `0xDF`） |
| 1 | 1 | `dev_id`（HP20 = 0x09） |
| 2 | 1 | `family`（HP20 = 0x02） |
| 3 | 1 | `cmd` 命令码 |
| 4 | 1 | `length` = payload 字节数（0..255） |
| 5 | length | payload |
| 5+length | 1 | LRC |

总长 = 6 + length。

### 4.2 LRC 算法（`Checksum::calculationCheck @0x3d5ce0`）

```asm
loop:  load frame[i] ; sum += frame[i]
```

```
LRC = (256 - (sum(bytes[0 .. n-2]) mod 256)) mod 256
```

校验范围 **包含帧头到 payload 末字节**（不含 LRC 本身），请求与应答同一算法。

### 4.3 实例（`tools/re/verify_frames.py` 已逐条复算通过）

| 场景 | 帧 |
|---|---|
| 握手 / 识别 | `AF 09 02 00 00 46` |
| 读实时数据 | `AF 09 02 0A 00 3C` |
| 读 T1..T4 预设 | `AF 09 02 04 00 42` |
| 设 T1 = 200 ℃ | `AF 09 02 03 03 00 C8 00 78` |
| 启动 T1 预热 | `AF 09 02 05 01 00 40` |
| 读回流曲线 | `AF 09 02 07 00 3F` |
| 读维修参数 | `AF 09 02 0C 00 3A` |
| 设蓝牙名 "HP20" | `AF 09 02 08 14 48 50 32 30 00×16 30` |

---

## 5. 命令表（HP20 全量：11 条；机器可读版本见 `protocol/hp20_commands.json`）

> 所有命令都从 `DeviceSession::sendCMDWaitResult`（无 payload）/`sendCMDWaitResultWithData`（带 payload）发出，
> 命令码以 **立即数** 形式出现在调用点，下表每个 cmd 都标了对应反汇编地址。
> 每个写命令都先包装在 `runWithRealDataPaused` 中（暂停 125 ms 轮询 → 发送 → 恢复），高置信。

| # | cmd | 名称 | 方向 | payload | 应答 payload | 置信 | 证据地址 |
|---|---|---|---|---|---|---|---|
| 0 | `0x00` | identify / 握手 | W | 空 | ≥2 字节：`dev_id, family` | 高 | `RegisterDev @0x431c74`（`r2 = 0`） |
| 1 | `0x03` | set_thermostat 设预设温度 | W | `index:u8, temp:i16le` | 空 | 高 | closure `@0x45d1b0`（`r2 = 3`） |
| 2 | `0x04` | get_thermostat 读预设温度 | R | 空 | 4 × `i16le` = T1..T4 | 高 | `getThermostat@0x45d2a0` / closure `@0x45d320`（`r2 = 4`） |
| 3 | `0x05` | set_preheat 选择/启动预热通道 | W | `channel:u8` (0..3 = T1..T4) | 空 | 高 | closure `@0x45aa44`（`r2 = 5`） |
| 4 | `0x06` | set_reflow 写回流曲线 | W | 8 × `u16le`（16 字节） | 空 | 高 | closure `@0x45d5c4`（`r2 = 6`） |
| 5 | `0x07` | get_reflow 读回流曲线 | R | 空 | 8 × `u16le` | 高 | `getReflow@0x45e78c` / closure `@0x45e80c`（`r2 = 7`） |
| 6 | `0x08` | set_ble_name 写蓝牙名 | W | 20 × ASCII（补 0x00） | 空 | 高 | closure `@0x461898`（`r2 = 8`） |
| 7 | `0x09` | get_ble_name 读蓝牙名 | R | 空 | ASCII 字节（去掉 0x00） | 高 | closure `@0x461a3c`、`getBleNameDirect@0x43484c`（`r2 = 9`） |
| 8 | `0x0A` | get_realtime 读实时状态 | R | 空 | ≥11 字节状态帧（第 7 章） | 高 | `getRealTimeData@0x433df0`（`r2 = 10`） |
| 9 | `0x0B` | set_repair 写维修参数 | W | 8 × `u16le`（16 字节） | 空 | 高 | closure `@0x45c924`（`r2 = 11`） |
| 10 | `0x0C` | get_repair 读维修参数 | R | 空 | 8 × `u16le` | 高 | `getRepair2@0x4341e4` / closure `@0x45cc20`（`r2 = 12`） |

> App 命令集就这 11 条（全工程 `sendCMDWaitResult*` 调用点枚举：HP20 共 13 处，覆盖上表，无遗漏）。
> **没有**独立的“总开关/急停”命令 —— 见第 10 章未确认项 U3。

### 5.1 `0x03` set_thermostat（设温度）

```
payload = [ index, value_lo, value_hi ]      ; value = u16 小端
index: 0=T1, 1=T2, 2=T3, 3=T4, 4=T5
```

证据：closure `@0x45d1b0` 分配 `Uint8Array(Smi 3)`，写入 `[0]=index`、`[1]=value&0xFF`、`[2]=(value>>8)&0xFF`，随后 `r2 = 3` 调 `sendCMDWaitResultWithData`。

各便捷封装（`@0x46088c/0x4604e4/0x46013c/0x45d0b4`）读取的参数对象字段：

| 方法 | index | 取值来源 |
|---|---|---|
| `setThermostatT1` | 0 | `model + 0xf` （u64 低 16 位） |
| `setThermostatT2` | 1 | `model + 0x17` |
| `setThermostatT3` | 2 | `model + 0x1f` |
| （T4） | 3 | `model + 0x27`（无独立封装，由通用 setThermostat 使用） |
| `setThermostatT5` | 4 | `model + 0x2f` |

语义（高置信）：
- T1..T4 = 4 个**预设温度点**（主界面顶部 T1–T4 按钮 / 加热设置页）。
- T5 = 主界面“**目标温度**”，`SetTarTem` 同时调用 `setThermostatT5` 与 `setReflow`（`hp20mainpage.dart`）。
- 温度数值 **不带缩放**（解析路径无除法/浮点），即 ℃ 或 ℉ 的整数值，由实时帧的单位位指示（第 7 章）。

### 5.2 `0x04` get_thermostat（读预设）

应答 payload = 4 × `i16le`，依次 T1..T4（`@0x45d320` 逐组 `orr (b1<<8)|b0` 写入 4 个字段）。

> 注意：**T5 不在 0x04 的应答里**；T5 当前值由实时帧（`0x0A`）的 `target_temp` 字段反映（见 7 章）。

### 5.3 `0x05` set_preheat（启动加热 / 选通道）

```
payload = [ channel ]   ; 0=T1, 1=T2, 2=T3, 3=T4
```

证据链：
1. `hp20mainpage.dart _OnBtnTopT @0x45a4f8`：按按钮标签 `"T1"/"T2"/"T3"/"T4"` 把 `parameters.field_37` 置 0/1/2/3；
2. 随后调用 `ManagecmdHp20::setPreHeat`；
3. `setPreHeat @0x45a820 → runWithRealDataPaused → closure @0x45aa44`：`Uint8Array(Smi 1)`，`[0] = model.field_37`，`r2 = 5`。

失败时 App 打印 **“设置预设温度失败”**（`@0x45a7a4`），可用来判定应答是否有效（应答非 null 即成功）。

### 5.4 `0x06` / `0x07` 回流焊曲线（4 段，8 × u16）

顺序（写、读一致）：

| # | 字段 | UI 标签（`hp20setreflowpage.dart`） | 单位 |
|---|---|---|---|
| 0 | `preheat_temp` | 预热温度 | ℃/℉ |
| 1 | `preheat_time` | 预热时间 | **s** |
| 2 | `soak_temp` | 恒温温度 | ℃/℉ |
| 3 | `soak_time` | 恒温时间 | **s** |
| 4 | `reflow_temp` | 回流温度 | ℃/℉ |
| 5 | `reflow_time` | 回流时间 | **s** |
| 6 | `cool_temp` | 冷却温度 | ℃/℉ |
| 7 | `cool_time` | 冷却时间 | **s** |

- 证据（写）：`@0x45d5c4` 分配 `Uint8Array(16)`，依次从 `field_3f/47/4f/57/5f/67/6f/77` 取 u16 小端写入 `payload[0..15]`，`r2 = 6`。
- 证据（读）：`@0x45e80c` 按同样顺序把 `payload[0..15]` 还原成 8 个 u16。
- 单位证据：页面输入框后缀字面量 `"s"`（`@0x461f24` 等），温度后缀 `"℃"/"℉"`。
- 阶段名证据：`hp20viewmodel.dart reflowSolderingInterfaceStrs @0x45bac0 = ["PH","SK","RF","CL"]`
  （PreHeat / Soak / ReFlow / CooLing），由实时帧 `channel_or_stage` 位域选择。

示例（150℃/60s、180℃/90s、235℃/45s、100℃/30s）：

```
AF 09 02 06 10 96 00 3C 00 B4 00 5A 00 EB 00 2D 00 64 00 1E 00 B6
```

### 5.5 `0x0B` / `0x0C` 维修模式参数（4 组，8 × u16）

| # | 字段 | UI 标签 | 单位 |
|---|---|---|---|
| 0 | `debond_temp` | 除胶温度（Degumming Temperature） | ℃/℉ |
| 1 | `debond_time` | 除胶时间（Degumming Time） | s |
| 2 | `delam_temp` | 分层温度（Delamination Temperature） | ℃/℉ |
| 3 | `delam_time` | 分层时间 | s |
| 4 | `group3_temp` | 第 3 组温度（标签未取全，见 U4） | ℃/℉ |
| 5 | `group3_time` | 第 3 组时间 | s |
| 6 | `group4_temp` | 第 4 组温度 | ℃/℉ |
| 7 | `group4_time` | 第 4 组时间 | s |

- 写：`@0x45c924`（`r2 = 11`），字段 `field_e7/ef/f7/ff/+0x103/+0x107/+0x10b/+0x10f`。
- 读：`@0x45cc20`（`r2 = 12`），同样的 8 个字段。
- 维修模式的 **子功能选择** 来自实时帧的 `channel_or_stage`：
  `hp20viewmodel.dart GetFunBtnStr @0x45a298` 在 `mode == 4` 时把 T1/T2/T3/T4 映射成
  `"Debond" / "Delam" / "LAM" / "Reballing"`（除胶 / 分层 / LAM / 植球），高置信。

### 5.6 `0x08` / `0x09` 蓝牙名

- 写：payload **固定 20 字节**，ASCII（>127 的字符按低字节截断），不足补 `0x00`，超过 20 字符截断。
  证据 `@0x461898`：`Uint8Array(Smi 20)` + 逐字符写入 + `cmp x4, #0x14`（20）上限，`r2 = 8`。
  因此实现端**应忽略 0x00 填充**。
- 页面标签：`hp20setpage.dart` “蓝牙名称”。

### 5.7 通用设置：有哪些、没有哪些（问题 6）

| 功能 | 是否有专用命令 | 说明 |
|---|---|---|
| 单位 ℃/℉ | ❌ 无独立命令 | 单位由**设备**决定并在实时帧 `w2 bit14` 上报；App 只做显示 |
| 蜂鸣 / 亮度 / 休眠 | ❌ 未发现 | 快照里 HP20 命令集仅上表 11 条；未见相关字符串或命令 |
| 版本读取 | ❌ 未发现 | 无 version 命令；App 不显示固件版本 |
| 设备名读/写 | ✅ `0x09` / `0x08` | 见 5.6 |
| 波形显示选择（温度/功率/温度+功率） | ⛔ 纯 UI | `hp20setpage.dart` 的“波形选择”，不发命令 |
| 加热 / 回流焊 / 维修 模式选择 | ⛔ 无命令 | 模式由**设备当前状态**上报（实时帧 `mode`）；App 只在 UI 上切换页面并下发对应参数 |

---

## 6. 通知帧 / 应答帧（问题 7）

### 6.1 解析实现（`FrameParser::feed @0x43565c`）

```text
1. buffer += 本次 notify 的字节            （跨包累积，天然支持分包）
   若首字节不是 0xDF → 丢弃其之前的垃圾字节
3. buffer.length < 5 → return（连长度字段都还没到）
4. length = buffer[4]；total = length + 6
   buffer.length < total → return（等下一包）
5. frame = buffer[0 : total]
6. LRC 校验：sum(frame[0 : total-1]) 与 frame[total-1] 比较，不等 → 丢弃该帧并继续
7. 组 BleFrame{ dt = (buffer[2]<<8)|buffer[1], func = buffer[3], bytes = payload }
8. yield 该帧，buffer 左移 total，回到第 2 步（一次 notify 可含多帧）
```

要点（高置信）：
- **定长头就是 `0xDF`**，与请求的 `0xAF` 对应；没有其它帧同步字。
- 帧长由 **length 字段** 决定（不是固定 28 字节）。EL15 的 28 字节只是它自己的 payload 长度。
- 支持**跨 notify 分包**与**单包多帧**。
- LRC 错误帧被**静默丢弃**（不会回错误码）。
- 没有转义/字节填充机制。

### 6.2 请求-应答配对

`DeviceSession::sendCMDWaitResult*` 用一个 `Timer.periodic(10 ms)` 轮询等待，判定条件是
**应答帧的 `func`（即 cmd 字节）等于本次请求的 cmd**（`@0x3d5d70`）：

```asm
frames[0].field_f  ==  context.field_13 (请求 cmd)   ; 相等才 complete
否则继续等到超时（1000 ms）
```

所以：
- 设备必须**回显同一个 cmd 字节**；
- 超时未配对 → completer 以 `null` 完成 → App 视为失败（setPreHeat 等打印失败日志）；
- 实时帧（`0x0A`）也是走同一通道，App 靠 cmd 配对区分。

---

## 7. 实时状态帧（cmd `0x0A` 应答 payload，≥11 字节）

解析实现：`ManagecmdHp20::getRealTimeData @0x433df0`（高置信；字节偏移与位域均来自其反汇编）。

| payload 偏移 | 类型 | 含义 | 语义证据 |
|---|---|---|---|
| 0..1 | u16le | **平台温度** `platform_temp` | `platformTemperatureStr @0x45b898` 直接显示该字段 |
| 2 | u8 | **额定/最大功率** `rated_power` | `powerRatio @0x45b7cc` 作分母 |
| 5 | u8 | 未使用（App 读取后丢弃） | `@0x433ff4` 长度校验但无 store |
| 6 | u8 | **当前功率** `current_power` | `powerRatio` 作分子 |
| 7..8 | u16le | 打包字 `w1` | 见下 |
| 9..10 | u16le | 打包字 `w2` | 见下 |

`w1` 位域：

| 位 | 掩码 | 字段 | 含义 | 证据 |
|---|---|---|---|---|
| 9..0 | `w1 & 0x3FF` | `target_temp` | **目标温度**（整数值） | `targetTempStr @0x45b690` 显示 `field_df` |
| 11..10 | `(w1>>10) & 3` | `channel_or_stage` | 加热模式 = 通道 T1..T4；回流模式 = 阶段 PH/SK/RF/CL；维修模式 = 子功能 | `heatingModeStrs @0x45ba48 = [T1,T2,T3,T4]`、`reflowSolderingInterfaceStrs @0x45bac0 = [PH,SK,RF,CL]` |
| 14..12 | `(w1>>12) & 7` | `mode` | 运行模式：`0/1` = 加热，`2` = 回流焊，`4` = 维修，`8` = 主页/待机 | `stateStr @0x45b8ec`（`cf==2` → 回流阶段串）、`GetFunBtnStr @0x45a298`（`cf==4` → 维修子功能；`cf==2` → 回流）、`cf==8 → "Main"` |

> ⚠ `mode` 的 3 位掩码与 `w2` 的单位位在数值上有交叠读法（见 U5），实现时按 `(w1>>12)&7` 取即可，实测可校正。

`w2` 位域：

| 位 | 掩码 | 字段 | 含义 | 证据 |
|---|---|---|---|---|
| 5..0 | `w2 & 0x3F` | `timer_seconds` | 计时器秒位（0..59） | `heartTimeStr @0x45b4f4` 以 `MM:SS` 显示；`>60` 显示 "OFF" |
| 12..6 | `(w2>>6) & 0x7F` | `timer_minutes` | 计时器分位 | 同上 |
| 12 | `w2 & 0x1000` | `bit12` | 未知标志（与分位交叠） | `field_bb`，语义未确认（U5） |
| 13 | `(w2>>13) & 1` | `fan_level` | **风扇档位**（0/1 在资源里出现） | `imgFan @0x45bb6c` → `assets/images/el15/el15wind0{field_b3}.png`（资源 el15wind00..05，故档位可能 0..5） |
| 14 | `w2 & 0x4000` | `celsius` | **单位**：1 = ℃，0 = ℉ | `targetTempStr`：`field_af ? "℃" : "℉"` |

补充行为（高置信）：`getRealTimeData` 会把 `w2` 与上一帧比较，
**一旦 `w2` 变化就自动补发 `cmd 0x04`（读预设）与 `cmd 0x0C`（读维修参数）**——
说明 `w2` 里还含有“参数版本/模式切换”类信息；实现端建议同样在 `w2` 变化时重读参数。

---

## 8. 时序与并发（问题 8，均出自反汇编）

| 参数 | 值 | 证据 |
|---|---|---|
| 实时轮询周期 | **125 ms** | `managecmd_hp20.dart startRealDataLoop` → `Timer.periodic(Duration@6c2da1 = 0x1E848 µs)`（`@0x433bfc`） |
| 等待轮询粒度 | **10 ms** | 同一个 `Timer.periodic(Duration@6c2e61 = 0x2710 µs)` |
| 写间隔（节流） | **15 ms** | `write_queue.dart WriteQueue::enqueue` → `Future.delayed(Duration@6c2e51 = 0x3A98 µs)`（`@0x3d5a88`） |
| 单命令在途 | 是（硬限制） | `sendCMDWaitResultWithData @0x3d76c4`：`field_4b` 忙 → 打印“指令任务繁忙，拒绝重复调用”并直接返回 null |
| 连接后延时 | 2 × 1000 ms | `start()`：connect → 1 s → 发现/订阅 → 1 s → 握手 |
| 写之前暂停轮询 | 是 | 每个写命令都包在 `runWithRealDataPaused`（暂停 125 ms 轮询 → 写 → `resumeRealDataLoop`） |

实现建议（与 App 一致的最小实现）：

```text
1. 串行化所有事务（一把锁），一次只发一帧；
2. 发帧后最多等 1000 ms 收同 cmd 的应答帧；超时即失败（App 不重发，本项目建议最多重试 1 次或直接报错）；
3. 两帧写之间至少间隔 15 ms；
4. 轮询线程 125 ms 一次 cmd 0x0A；任何写操作前先暂停轮询，写完再恢复；
5. 通知回调里做流式拆包（0xDF + length + LRC），一包多帧要循环处理。
```

---

## 9. 典型会话

```text
[发现] 扫描 → 连接 BLE（service ff01）
[等 1 s] 发现服务/特征 → 打开 ff02 通知（CCCD 0100）
[C-0x00] TX AF 09 02 00 00 46        RX DF 09 02 00 02 09 02 <LRC>   ⇒ 确认 HP20
[C-0x04] TX AF 09 02 04 00 42        RX DF 09 02 04 08 <T1..T4 i16le> <LRC>
[C-0x07] TX AF 09 02 07 00 3F        RX DF 09 02 07 10 <8×u16> <LRC>
[C-0x0C] TX AF 09 02 0C 00 3A        RX DF 09 02 0C 10 <8×u16> <LRC>
[C-0x09] TX AF 09 02 09 00 3D        RX DF 09 02 09 05 48 50 32 30 00 <LRC>
[循环]  TX AF 09 02 0A 00 3C        RX DF 09 02 0A 0B <状态帧> <LRC>   每 125 ms
[设定]  TX AF 09 02 03 03 00 C8 00 78            (T1=200℃)
[启动]  TX AF 09 02 05 01 00 40                  (启动 T1 预热)
[曲线]  TX AF 09 02 06 10 <16 字节>             (写回流曲线)
```

---

## 10. 未确认 / 低置信清单（需真机探测）

| 编号 | 项目 | 现状 | 建议探测 |
|---|---|---|---|
| U1 | **停止/急停命令** | App 命令集只有 0x00/03/04/05/06/07/08/09/0A/0B/0C，**没有**独立 stop；UI 有 btnstop 资源但看不到对应命令 | 真机：加热中让 App 点“停止”，抓 BLE 写特征值（预期是 `0x05` 或 `0x03` 的特殊取值，也可能是设备本地操作） |
| U2 | cmd `0x03` / `0x05` / `0x06` / `0x0B` 的**应答 payload 内容** | App 只用“非 null 即成功”，不解析字段 | 真机抓应答 16 进制，确认是 `DF 09 02 <cmd> 00 LRC` 还是带数据 |
| U3 | 温度**数值范围/单位切换** | 解析路径无缩放，认定整数值（℃或℉）；但设备端如何切换到 ℉、以及 ℉ 下是否仍为整数，未知 | 真机切单位后观察 `target_temp`/`current_temp` 变化 |
| U4 | 维修模式第 3/4 组参数的 UI 名称 | 只取到“除胶温度/除胶时间/分层温度/分层时间”与子功能名 Debond/Delam/LAM/Reballing | 打开 App 维修设置页第 3/4 行截图核对 |
| U5 | `w2` 的 bit12（`field_bb`）语义 | 与“分钟位”有交叠读法，无法从静态证据唯一确定 | 真机长时间运行，记录 w2 全位变化 |
| U6 | `fan_level` 位宽（1 位 vs 3 位） | 解析代码是 `(w2>>13)&1`，但图标资源有 el15wind00..05（0..5 档） | 真机调风扇档位观察 |
| U7 | `mode` 取值全集 | 已确认 0/1=加热、2=回流、4=维修、8=主页；其余值未见 | 真机遍历工作状态 |
| U8 | 蓝牙名后缀规律（`_BLE`/`_SPP`） | 快照内无该字面量；识别走 cmd 0x00 握手 | 手机扫描列表看 HP20 实际广播名 |
| U9 | 是否存在 >255 字节的分包发送 | `buildRaw` 禁止 payload >255，且 HP20 最大 payload 为 20 字节，实际不会触发 | — |
| U10 | 应答帧是否需要逐帧 ACK / 设备是否主动推送 | 静态只看到“应答 + 轮询”，无主动上报路径 | 真机静置 10 s，看是否有非应答帧 |

---

## 11. 自检输出（`tools/re/verify_frames.py`）

该脚本**独立重算** LRC（不 import 生成器）并校验帧头/设备头/长度字段，同时用第三方 EL15 抓包交叉验证算法：

```text
HP20 frame self-check  (protocol\hp20_commands.json)
dev header = 0x0209 (bytes 09 02) | 16 frames from 11 commands

#  command            cmd  example                            frame_hex                                      LRC check
1  identify           0x00 identify (＝App 的 RegisterDev)      AF 09 02 00 00 46                              OK
2  get_thermostat     0x04 read presets                       AF 09 02 04 00 42                              OK
3  set_thermostat     0x03 T1 = 200 ℃                         AF 09 02 03 03 00 C8 00 78                     OK
4  set_thermostat     0x03 T2 = 250 ℃                         AF 09 02 03 03 01 FA 00 45                     OK
5  set_thermostat     0x03 T5 (目标温度) = 180 ℃                  AF 09 02 03 03 04 B4 00 88                     OK
6  set_preheat        0x05 启动 T1                              AF 09 02 05 01 00 40                           OK
7  set_preheat        0x05 启动 T2                              AF 09 02 05 01 01 3F                           OK
8  set_preheat        0x05 启动 T3                              AF 09 02 05 01 02 3E                           OK
9  set_preheat        0x05 启动 T4                              AF 09 02 05 01 03 3D                           OK
10 get_reflow         0x07 read reflow profile                AF 09 02 07 00 3F                              OK
11 set_reflow         0x06 150℃/60s, 180℃/90s, 235℃/45s, 100℃ AF 09 02 06 10 96 00 3C 00 B4 00 5A 00 EB 00 2D 00 64 00 1E 00 B6 OK
12 get_repair         0x0C read repair params                 AF 09 02 0C 00 3A                              OK
13 set_repair         0x0B 120℃/60s, 150℃/90s, 180℃/60s, 200℃ AF 09 02 0B 10 78 00 3C 00 96 00 5A 00 B4 00 3C 00 C8 00 2D 00 A2 OK
14 set_ble_name       0x08 name = HP20                        AF 09 02 08 14 48 50 32 30 00 … 00 30          OK
15 get_ble_name       0x09 read name                          AF 09 02 09 00 3D                              OK
16 get_realtime       0x0A poll realtime                      AF 09 02 0A 00 3C                              OK

EL15 reference frames (third-party captures) re-checked with the same LRC rule:
  poll         AF 07 03 08 00 3F      computed LRC=3F frame LRC=3F  OK
  mode CC      AF 07 03 03 01 01 42   computed LRC=42 frame LRC=42  OK
  device name  AF 07 03 07 00 40      computed LRC=40 frame LRC=40  OK

RESULT: ALL CHECKS PASSED
```

> 说明：首次运行曾报 `device name … FAIL`（把参考帧 LRC 手写成 0x4B），脚本正确判定 `256-(0xAF+0x07+0x03+0x07)=0x40`；
> 修正参考值为 `AF 07 03 07 00 40` 后全绿 —— 证明该自检真的在做独立校验。

---

## 12. 证据索引（关键地址 → 结论）

| 地址 | 函数 | 支撑结论 |
|---|---|---|
| `0x3d5ae8` | `FrameBuilder::buildRaw` | 帧头 0xAF、dev_id/family 两字节、cmd、length、payload、LRC；payload ≤255 |
| `0x3d5ce0` | `Checksum::calculationCheck` | LRC 公式 |
| `0x43565c` | `FrameParser::feed` | 0xDF 帧头、按 length 切帧、跨包累积、LRC 校验与丢弃 |
| `0x3d54f8` / `0x3d76c4` | `DeviceSession::sendCMDWaitResult(WithData)` | 命令通道、忙拒绝、Completer、超时定时器 |
| `0x3d5d70` | 超时闭包 | 1000 ms 超时 + 按 cmd 配对 + 10 ms 粒度 |
| `0x431c74` | `RegisterDev` | 由应答 `payload[0..1]` 拼 16 位设备头 |
| `0x431d54` / `0x434a30` | `CreatManageCMD` / 匹配闭包 | 由设备头 0x0209 选出 HP20 实现 |
| `0x433df0` | `ManagecmdHp20::getRealTimeData` | 实时帧全部字节/位域映射，及 w2 变化触发补读 |
| `0x4341e4`/`0x45cc20` | `getRepair2`/`getRepair` | 维修参数 8×u16 |
| `0x434650`/`0x45d320` | `getThermostat2`/`getThermostat` | 预设温度 4×u16 |
| `0x45a820`/`0x45aa44` | `setPreHeat` | cmd 5 / 1 字节通道 |
| `0x45d100`/`0x45d1b0` | `setThermostat` | cmd 3 / 3 字节 |
| `0x45d544`/`0x45d5c4` | `setReflow` | cmd 6 / 16 字节 |
| `0x45c8a4`/`0x45c924` | `setRepair` | cmd 11 / 16 字节 |
| `0x461818`/`0x461898` | `setBleName` | cmd 8 / 20 字节 |
| `0x4619bc`/`0x461a3c` | `getBleName` | cmd 9 / ASCII 去零 |
| `0x45a4f8` | `_HP20MainPageState::_OnBtnTopT` | T1..T4 按钮 → field_37 = 0..3 → setPreHeat（cmd 5 的语义） |
| `0x45b690`/`0x45b898`/`0x45b7cc`/`0x45b4f4`/`0x45b8ec`/`0x45a298`/`0x45ba48`/`0x45bac0`/`0x45bb6c` | `HP20viewmodel` getters | 实时帧各字段的语义（温度/功率/计时/模式/阶段/风扇/单位） |
| `objs.txt Obj!DevType*` | 枚举实例 | DevType.field_14 型号头表（HP20=0x0209） |
| `objs.txt Obj!Duration*` | 常量 | 125 ms / 1000 ms / 10 ms / 15 ms |

---

## 13. 文件清单（本次产出）

| 文件 | 说明 |
|---|---|
| `protocol/hp20_commands.json` | 机器可读命令表（含 frame_hex、payload/应答格式、位域、时序、置信度、证据） |
| `docs/hp20_protocol.md` | 本文档 |
| `tools/re/hp20_proto.py` | 参考编解码（build_frame / parse_frame / parse_realtime） |
| `tools/re/gen_hp20_commands.py` | 从代码生成 hp20_commands.json（保证 LRC 正确） |
| `tools/re/verify_frames.py` | 独立自检（重算 LRC / 结构校验 / EL15 交叉验证） |
| `tools/re/dump_evidence.py` | 抽取关键函数反汇编到 `out/evidence_hp20/` |
| `tools/re/asm_get.py` / `asm_il.py` | 从 blutter 输出里按地址切函数（全量/纯 IL） |
| `tools/re/run_blutter2.bat` | 复现 blutter 分析（含 Dart 3.10.3 现场编译） |

---

# 14. 第二轮修正（re-hp20b，rehber 反汇编 + 真机帧双向对齐）

> 本章**覆盖**第 7 章中与本轮冲突的表述。第 5 章命令表（11 条）不变。

## 14.1 状态帧最终布局（payload 11 字节）
| 偏移 | 含义 | 证据 |
|---|---|---|
| `[0:2]` i16 | **当前温度 ℃** | 真机 + 面板一致 |
| `[2]` u8 | **供电/额定功率 W**（`powerRatio` 的分母） | 换电源 65→140 |
| `[3:5]` u16 | **输入电压 ×0.1V** | 200→20.0V、278→27.8V，与面板/官方 APP 顶栏一致 |
| `[5]` u8 | **输出/加热状态字节**：待机 `0x00`、加热运行 `0x02`（**不是恒 0x02**） | 真机待机帧 vs 加热帧 |
| `[6]` u8 | **当前输出功率 W** | 加热 57→125W、停热 0W |
| `W1=[7:9]` | `W1&0x3FF` 目标温度；`(W1>>10)&3` 挡位/阶段；`(W1>>12)&7` 模式 | 写读闭环 |
| `W2=[9:11]` | **加热计时 MM:SS**（见 14.2） | `heartTimeStr @0x45b4f4` |

## 14.2 `W2` = 计时，位域是 MM:SS（**不是总秒数**）
```
MM = (W2 >> 6) & 0x7F
SS =  W2       & 0x3F
若 SS >= 60  -> App 显示 "OFF"（设备用 SS=0x3F 作哨兵）
否则显示 pad2(MM) + ":" + pad2(SS)
```
样例：`0x04B2` → 18:50；`0x03C4` → 15:04（实测每秒 -1）；`0x29BF` → SS=63 → **OFF**（与官方 APP 的 ⏱OFF 一致）。

## 14.3 模式（`(W1>>12)&7`）取值全集
| 值 | 含义 | 挡位字段的含义 |
|---|---|---|
| 0 | 待机 | 上次挡位 |
| 1 | **回流焊** | 阶段：预热 PH / 恒温 SK / 回流 RF / 冷却 CL |
| 2 | **加热** | 通道 T1..T4 |
| 3 | **设置** | 未观测（是否变参数回显未知） |
| 4 | **维修** | 子功能：除胶 Debond / 分层 Delam / 贴合 LAM / 植球 Reballing |
| 8 | 主页 | — |
⚠️ 官方 APP 的 `stateStr` 把 **1 与 2 的字符串表写反了**（cf==1 返 T1..T4、cf==2 返 PH/SK/RF/CL），照抄会导致加热/回流阶段文字互换。以手册为准：1=回流焊、2=加热。

## 14.4 风扇
状态帧里**只有 1 位**与风扇相关：`W2 bit13`（= `payload[10] bit5`），0/1 布尔（风扇是否运转），**不是 1–5 档**。
面板上的 1~5 叶风速是**固件本地量，不进帧**（官方 APP 也只按 0/1 选 `el15wind0.png` / `el15wind1.png` 两张图）。

## 14.5 加热超时
**没有任何命令读写它**（ManagecmdHp20 全部命令常量仅 0x00/03/04/05/06/07/08/09/0A/0B/0C，`0x03` 的 index 只到 T5）。它是设备本地菜单项（手册：出厂 20 Min，可调 1–30 Min 或关闭）。
`W2` 的计时很可能是该超时的剩余时间（实测从 20:02 起逐秒递减），但「超时值本身」不会出现在帧里。

## 14.6 对第 7 章的两处更正
1. ~~`[3:5]` 当前温度~~ → `[3:5]` 是**输入电压 ×0.1V**；当前温度是 `[0:2]`。
2. ~~mode 0/1=加热、2=回流~~ → **1=回流焊、2=加热**（见 14.3）。

## W2 最终结论（真机测速，推翻第二轮位域结论）
- 实测递减速率 **0.9942 单位/秒 → 1 单位 = 1 秒**；
- `W2=1229` 时用户面板显示「约 20 分钟」，`1229 s = 20:29` **吻合**；
- 故 **`W2` = 加热超时剩余秒数**，显示为 `W2//60:W2%60`；
- 官方 APP 的 `MM=(W2>>6)&0x7F / SS=W2&0x3F` 是**错的**（64 进制当 60 用）：`1229` 会被它显示成 `19:13`；
- `0x29BF`(10687) 是「计时未启用」观测哨兵；超时上限 30 min，故 `W2>1800` 一律显示 OFF。

## 风扇
- 帧里似乎只有 `W2 bit13` 一个布尔位（证据：风扇不转时 bit13=0、66℃ 加热风扇转时 bit13=1）；
- 但 `0x29BF` 同时是计时哨兵，故该位在哨兵状态不可信，**待再抓两组稳定帧定性**；
- 面板 1~5 档风速确定不在帧内（官方 APP 也只有两张图）。

## mode=3（设置）实测帧
`2B 00 8C 17 01 02 0D 32 30 C2 03`：温度/供电/电压/功率/计时全部照常实时，`slot=0`，**无参数回显** → 设置项不经蓝牙。

## 风扇位定性（决定性实验，已证伪）
| 状态 | 帧表现 |
|---|---|
| 保温（功率 0W）、超时 20min 计时 20:21 | `W2=0x04C5`，bit12/13/14/15 全 0 |
| 加热中（**135W**，52→54℃，风扇转 1 档） | `W2=0x04C3`，bit12/13/14/15 全 0（8 帧一致） |
| 加热超时=OFF | `W2=0x29BF`（哨兵常量），bit13=1 ← 曾被误当风扇位 |
**结论：状态帧不含任何风扇信息；`bit13` 只是哨兵常量 `0x29BF` 的一位。** 面板 1–5 档风速纯本地，协议不可读。

## 补充实测
- 加热时输出功率达 **135W**（供电 140W），功率单位 W 再获印证；
- `[5]=0x02` 在「保温（0W）」与「加热（135W）」两种状态都成立 → 它表示「加热模式运行中」，不是「正在出功率」。

## 计时偏移的新证据（待对标）
- 用户设「加热超时 = 20 min」，同时刻读到 `W2=1268` → `21:08`；`1268 − 1200 = **68 秒**`，与用户反馈的「多 1 分 8 秒」完全吻合；
- 另抓到 `W2=1832`（30:32）> 1800（超时上限 30 min），说明**设备上报值可以超过设置值**；
- 推断：设备的计时起点比面板设置值早约 68 秒（可能是加热使能/进入界面即开始计时）；
- 处置：界面只显示 `W2` 原值 + MM:SS，并注明「设备计时（可能比面板多约 1 分钟）」，OFF 判据放宽为 `==0x29BF` 或 `>7200`；等同时刻对标再决定偏移修正。

# 15. 计时编码定论（64 进制，推翻第 14.2 节）
## 15.1 结论
```
MM = (W2 >> 6) & 0x7F
SS =  W2       & 0x3F
SS >= 60  ->  OFF        # 0x29BF 的 SS=63 即哨兵
```
**每分钟 = 64 个单位（不是 60），每真实秒 -1。** 这不是「总秒数」——第 14.2 节的秒制结论作废。

## 15.2 决定性证据（用户真机设置值）
| 面板设置 | 秒制解码（错） | 64 进制解码（对） |
|---|---|---|
| 加热超时 25 min | 1600 s → 26:40 | **25 × 64 = 1600 → 25:00** |
| 休眠时间 20 min | 1280 s → 21:20 | **20 × 64 = 1280 → 20:00** |
交叉验证：`1229 → 19:13`（20 min 倒计时 47 s 后）、`1832 → 28:40`（30 min 倒计时 88 s 后）、`0x29BF → SS=63 → OFF`。

## 15.3 官方 APP 是对的
`ParametersHP20::heartTimeStr`（`hp20model.dart`，字符串 `get:heartTimeStr` @0x58D50）里 `(w2>>6)&0x7F` + `w2&0x3F` + `SS>60 ⇒ "OFF"` 的实现**完全正确**；第一轮文档「0x3C 是 Smi classId 而非除数」的观察也对——因为这里根本不需要除法（是 64 进制打包）。

## U4 结案（用户真机确认）
维修 4 组顺序：**除胶(Debond) / 分层(Delam) / 贴合(LAM) / 植球(Reballing)** —— 用户在设备面板「维修设置」里看到第 3 组为「贴合温度」、第 4 组为「植球温度」，与手册一致。
对应 `0x0B/0x0C` 的 8×u16：`[除胶温度, 除胶时间, 分层温度, 分层时间, 贴合温度, 贴合时间, 植球温度, 植球时间]`。

## 用户界面反馈（P 阶段）
- 设备页「设备头」显示超出窗口被截断；
- 原始帧控制台需要「暂停/恢复自动滚动」；
- 用户手工调整过部分界面文字（后续改动需保留）。
