# packaging/ —— 打包与自动构建

这个目录只放**打包相关**的东西，业务代码仍在 `winapp/`。

## 日常：一条命令出 exe

    # 在项目根目录执行（会先跑单测与自检，全绿才打包）
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1

或者直接双击 `packaging\build.bat`（等价，末尾带 pause，方便看结果）。

产物：

| 文件 | 类型 | 用途 |
|---|---|---|
| `dist\HP20上位机.exe` | 无控制台 | 双击即用 |
| `dist\hp20-cli.exe` | 带控制台 | `--probe` / `--selftest` / `--sim` / `--scan-only` |

## 常用参数

| 参数 | 作用 |
|---|---|
| `-SkipTests` | 跳过单元测试（只改界面文案、需要快速重打包时用） |
| `-SkipTests -SkipSelftest` | 跳过全部检查，直接打包 |
| `-NoClean` | 不删 `build/` 与 `dist/`（增量更快，但要小心残留旧产物） |
| `-VerifyOnly` | 不构建，只验证 `dist/` 里已有产物 |
| `-Python <路径>` | 指定解释器（默认 PATH 里的 python，找不到则用 DSH 捆绑 Python） |

## 脚本做了什么

1. 选解释器
2. 缺 PyInstaller 就装（失败自动重试一次）
3. `unittest` 全绿才继续
4. `--selftest` 44 项通过才继续
5. 清理 + `PyInstaller packaging/hp20.spec`
6. `packaging/verify_dist.py` 验证产物（体积、PYZ 内省、CLI 自检）
7. 打印产物清单

任何一步失败都会标红并**中止**，不会产出“看起来成功”的 exe。

## 加功能 / 修 bug 的推荐流程

    1. 改 winapp/ 里的代码（新逻辑尽量落纯函数：hp20_commands.py / framediag.py / chart.py 这类无 Tk 依赖的模块）
    2. 在 winapp/test_*.py 里补或改测试        <- 打包脚本先跑测试，红的出不了包
    3. powershell ... -SkipSelftest            <- 快速迭代时先只跑单测
    4. powershell ...                          <- 交付前跑全套
    5. dist\hp20-cli.exe --probe <地址>        <- 真机只读确认

## 为什么是两个 exe

无控制台（windowed）的 exe 里 `print` 没有去处，`--probe` / `--selftest` 会“跑了但看不到任何输出”。
与其在业务代码里塞 `AttachConsole` 之类的魔法，不如同一份代码出两个 target：GUI 给日常，CLI 给排障。

## 打包雷区（历史踩过，改 spec 前先读）

1. **`winrt` 必须 `collect_all`**：它是命名空间包、投影 `.pyd` 延迟加载，静态分析收不全 → 症状是“exe 能启动但扫不到任何 BLE 设备”。验收标准是 `hp20-cli.exe --scan-only` 能列出设备，不是“打包成功”。
2. **构建机缺 `USERPROFILE`/`HOME`** 时 PyInstaller 会直接崩（只影响构建，不影响 exe）。
3. **日志目录**：`logs.default_logs_dir()` 有 `sys.frozen` 分支，打包后落在 **exe 同级 `logs\`**；源码运行行为不变，有两条测试守着。改这块务必手工确认一次日志真的落在 `dist\logs\`。
4. **测试文件不进包**：`test_*.py` 不被 import，正常不会进；`verify_dist.py` 会直接读成品 exe 的 PYZ 断言“0 个 test_* 模块”，不要删它。

## 已知未做

- 图标（spec 未用 `--icon`）；
- 代码签名：首次运行 SmartScreen 会提示“未知发布者”，点“更多信息 → 仍要运行”。