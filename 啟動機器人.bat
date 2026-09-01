@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "D:\code\stock_ai_bot"

if not exist ".venv\Scripts\pythonw.exe" (
    echo 找不到 .venv\Scripts\pythonw.exe，無法啟動股票機器人。
    pause
    exit /b 1
)

echo 正在背景啟動股票機器人 Watchdog...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%CD%\.venv\Scripts\pythonw.exe' -ArgumentList 'tools\bot_watchdog.py --launch 啟動機器人_runner.bat' -WorkingDirectory '%CD%' -WindowStyle Hidden"
if errorlevel 1 (
    echo Watchdog 啟動失敗，請查看 PowerShell 錯誤訊息。
    pause
    exit /b 1
)

echo 啟動要求已送出，Watchdog 將在背景執行，Bot runner 視窗會另行開啟。
exit /b 0
