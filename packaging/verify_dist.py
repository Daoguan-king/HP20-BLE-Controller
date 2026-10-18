"""验证 dist/ 产物（打包后用，也可单独跑）。

检查项：
  1. 两个 exe 存在并打印体积；
  2. 从成品 exe 内省 PYZ：应用模块齐全、**没有任何 test_* 模块**、不含 unittest；
  3. dist/hp20-cli.exe --selftest 退出码 0 且输出 PASS (44/44)。

退出码 0 = 全部通过；非 0 = 有检查失败（输出里以 [FAIL] 标出）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / 'dist'
GUI_EXE = DIST / 'HP20上位机.exe'
CLI_EXE = DIST / 'hp20-cli.exe'
APP_MODULES = {
    'atk_hp20', 'chart', 'connection', 'framediag', 'framing', 'hp20_commands',
    'hp20_device', 'logs', 'proto_const', 'selftest', 'sim_device', 'transport',
}
fails: list[str] = []


def ok(msg: str) -> None:
    print(f'  [OK]   {msg}')


def bad(msg: str) -> None:
    print(f'  [FAIL] {msg}')
    fails.append(msg)


def check_files() -> None:
    for exe in (GUI_EXE, CLI_EXE):
        if not exe.is_file():
            bad(f'缺少产物 {exe.name}')
        else:
            ok(f'{exe.name}  {exe.stat().st_size / 1048576:.2f} MB')


def check_readme() -> None:
    readme = DIST / '使用说明.txt'
    if readme.is_file():
        ok(f'{readme.name}  {readme.stat().st_size / 1024:.1f} KB')
    else:
        bad('dist 里缺少 使用说明.txt（build.ps1 会从 packaging/ 拷入）')


def check_archive(exe: Path) -> None:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception as exc:  # noqa: BLE001
        bad(f'无法导入 PyInstaller 读取器（{exc}）：跳过 {exe.name} 的内省')
        return
    try:
        arc = CArchiveReader(str(exe))
        pyz = arc.open_embedded_archive('PYZ.pyz')
        modules = set(pyz.toc.keys())
    except Exception as exc:  # noqa: BLE001
        bad(f'{exe.name} 的 PYZ 读取失败（{exc}）')
        return
    tests = sorted(m for m in modules if m.split('.')[0].startswith('test'))
    missing = sorted(APP_MODULES - modules)
    if tests:
        bad(f'{exe.name} 里混进了测试模块: {tests[:5]}')
    else:
        ok(f'{exe.name}: {len(modules)} 个模块，测试模块 0 个')
    if 'unittest' in modules:
        bad(f'{exe.name} 里打进了 unittest（体积与启动都吃亏）')
    if missing:
        bad(f'{exe.name} 缺少应用模块: {missing}')
    else:
        ok(f'{exe.name}: 12 个应用模块齐全')


def check_cli_selftest() -> None:
    if not CLI_EXE.is_file():
        return
    try:
        proc = subprocess.run(
            [str(CLI_EXE), '--selftest'], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=180,
        )
    except Exception as exc:  # noqa: BLE001
        bad(f'hp20-cli.exe --selftest 无法执行（{exc}）')
        return
    tail = (proc.stdout or '').strip().splitlines()[-1:] or ['']
    if proc.returncode == 0 and 'PASS (44/44)' in (proc.stdout or ''):
        ok(f'hp20-cli.exe --selftest: {tail[0]}')
    else:
        bad(f'hp20-cli.exe --selftest 失败（exit={proc.returncode}）: {tail[0]}')


def main() -> int:
    print('验证 dist/ 产物：')
    check_files()
    check_readme()
    for exe in (GUI_EXE, CLI_EXE):
        if exe.is_file():
            check_archive(exe)
    check_cli_selftest()
    print()
    if fails:
        print(f'结果: FAIL（{len(fails)} 项）')
        return 1
    print('结果: PASS（产物齐全、无测试模块、CLI 自检 44/44）')
    return 0


if __name__ == '__main__':
    sys.exit(main())