@echo off
rem 一键构建（双击即可）：转发到 build.ps1，参数原样传递
setlocal
set SCRIPT=%~dp0build.ps1
where pwsh >nul 2>nul
if %errorlevel%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
)
set RC=%errorlevel%
echo.
if not %RC%==0 echo 构建失败（退出码 %RC%），请查看上面的 [FAIL] 行。
if %RC%==0 echo 构建成功。
pause
exit /b %RC%