@echo off
rem 双击运行：真实 BLE
setlocal
set PY=C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe
if not exist "%PY%" set PY=python
"%PY%" "%~dp0atk_hp20.py" %*
if errorlevel 1 pause
