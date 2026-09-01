@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title 停止股票機器人
cd /d "D:\code\stock_ai_bot"

if not exist ".venv\Scripts\python.exe" (
    echo 找不到 .venv\Scripts\python.exe，無法執行停止程序。
    pause
    exit /b 1
)

echo 正在停止股票機器人與背景 Watchdog...
".venv\Scripts\python.exe" tools\bot_watchdog.py --stop
if errorlevel 1 (
    echo 停止程序執行失敗，請查看 logs\watchdog\watchdog.log。
    pause
    exit /b 1
)

echo 股票機器人停止要求已完成。
exit /b 0
