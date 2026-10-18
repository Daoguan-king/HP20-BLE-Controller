<#
  一键构建：跑单测 -> 跑自检 -> 打包 -> 验证产物

  用法（在项目根目录执行）：
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipTests
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipTests -SkipSelftest   # 只打包
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -VerifyOnly                # 只验证 dist/

  退出码：0 成功；非 0 表示某一步失败（失败行会标红）。
#>
[CmdletBinding()]
param(
  [string]$Python = "",   # 指定解释器；留空则优先用带 bleak 的 DSH 捆绑 Python，其次 PATH 里的 python
  [switch]$SkipTests,
  [switch]$SkipSelftest,
  [switch]$NoClean,
  [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"

# 统一 UTF-8：本脚本、子进程 python、重定向输出保持同一编码（否则 GBK/UTF-8 混排会乱码）
try {
  $null = & chcp 65001
  [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
  $OutputEncoding = [System.Text.UTF8Encoding]::new()
} catch { }
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$ProgressPreference = "SilentlyContinue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
Set-Location $Root

function Say([string]$msg) { Write-Host $msg -ForegroundColor Cyan }
function Ok([string]$msg)  { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Die([string]$msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; exit 1 }

$Bundled = "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

# ---------- 0. 选解释器（优先带 bleak 的那个，否则打出来的 exe 扫不到设备） ----------
if (-not $Python) {
  if (Test-Path $Bundled) { $Python = $Bundled }
  else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source } else { Die "找不到 Python：请用 -Python <路径> 指定" }
  }
}
if (-not (Test-Path $Python)) {
  $cmd = Get-Command $Python -ErrorAction SilentlyContinue
  if (-not $cmd) { Die "解释器不存在: $Python" }
  $Python = $cmd.Source
}
Say "解释器: $Python"
$ver = & $Python -c "import sys; print(sys.version.split()[0])"
Ok "Python $ver"

# ---------- 1. 依赖检查 ----------
if (-not $VerifyOnly) {
  & $Python -c "import bleak" 2>$null
  if ($LASTEXITCODE -ne 0) {
    Die "该解释器没有 bleak，打出来的 exe 会扫不到 BLE 设备。请用 -Python 指定带 bleak 的解释器（例如 $Bundled）"
  }
  $bleakVer = & $Python -c "import importlib.metadata as m; print(m.version('bleak'))"
  Ok "bleak $bleakVer"

  & $Python -c "import PyInstaller" 2>$null
  if ($LASTEXITCODE -ne 0) {
    Say "安装 PyInstaller ..."
    & $Python -m pip install --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) {
      Say "重试一次（网络偶发 SSL 失败）..."
      & $Python -m pip install --quiet pyinstaller
    }
    & $Python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) { Die "PyInstaller 安装失败" }
  }
  $pi = & $Python -c "import PyInstaller; print(PyInstaller.__version__)"
  Ok "PyInstaller $pi"
}

# ---------- 2. 单测 ----------
if (-not $VerifyOnly -and -not $SkipTests) {
  Say "跑单元测试 ..."
  & $Python -m unittest discover -s winapp -p "test_*.py"
  if ($LASTEXITCODE -ne 0) { Die "单元测试未通过，已中止打包" }
  Ok "单元测试通过"
}

# ---------- 3. 自检 ----------
if (-not $VerifyOnly -and -not $SkipSelftest) {
  Say "跑 --selftest（模拟设备端到端）..."
  & $Python winapp\atk_hp20.py --selftest | Select-Object -Last 2
  if ($LASTEXITCODE -ne 0) { Die "--selftest 未通过，已中止打包" }
  Ok "自检通过"
}

# ---------- 4. 打包 ----------
if (-not $VerifyOnly) {
  if (-not $NoClean) {
    Say "清理 build/ 与 dist/ ..."
    if (Test-Path build) { Remove-Item build -Recurse -Force }
    if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }
    Ok "已清理"
  }
  Say "PyInstaller 打包（spec: packaging\hp20.spec）..."
  $sw = [Diagnostics.Stopwatch]::StartNew()
  & $Python -m PyInstaller --noconfirm packaging\hp20.spec
  $sw.Stop()
  if ($LASTEXITCODE -ne 0) { Die "PyInstaller 打包失败" }
  Ok ("打包完成，用时 {0:N1}s" -f $sw.Elapsed.TotalSeconds)
}

# 附带使用说明（构建会清空 dist，这里每次重新拷进去）
$readme = Join-Path $ScriptDir "使用说明.txt"
if (Test-Path $readme) {
  Copy-Item $readme (Join-Path $Root "dist") -Force
  Ok "已放入 dist\使用说明.txt"
}

# ---------- 5. 验证产物 ----------
Say "验证产物 ..."
& $Python packaging\verify_dist.py
if ($LASTEXITCODE -ne 0) { Die "产物验证失败（见上面输出）" }

Say ""
Say "全部完成。产物在 dist\："
Get-ChildItem dist -File | ForEach-Object {  if ($_.Length -ge 1MB) { Ok ("{0}  {1:N2} MB" -f $_.Name, ($_.Length / 1MB)) }  else { Ok ("{0}  {1:N1} KB" -f $_.Name, ($_.Length / 1KB)) }}
Say ""
Say "日常用法: 双击 dist\HP20上位机.exe；排障: dist\hp20-cli.exe --probe <地址>"
exit 0