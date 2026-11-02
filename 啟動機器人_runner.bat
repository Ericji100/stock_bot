@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title 股票機器人監控中...
echo 正在啟動股票機器人，請勿關閉此視窗...
cd /d "D:\code\stock_ai_bot"
set "STOCK_AI_BOT_WORKDATA_ROOT=%CD%\.workdata"
set "STOCK_AI_BOT_WORKDATA_PROFILE=prod"
set "UV_CACHE_DIR=%CD%\.runtime\uv_cache"
set "UV_TOOL_DIR=%CD%\.runtime\uv_tools"

REM AI analysis and search use the official DeepSeek API configured in config/secrets.json.

".venv\Scripts\python.exe" main.py
pause
