@echo off
chcp 65001 >nul 2>nul
rem Claude-Control 电脑端一键启动（Windows）
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
  echo [setup] 创建虚拟环境 .venv ...
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -m venv .venv
  ) else (
    python -m venv .venv
  )
)

echo [setup] 安装依赖 ...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo [错误] 依赖安装失败，请检查网络后重试。
  pause
  exit /b 1
)

echo [run] 启动 relay_server.py ...
".venv\Scripts\python.exe" relay_server.py %*
if errorlevel 1 (
  echo.
  echo [错误] 中继启动/运行失败，请查看上方日志。
  pause
)
