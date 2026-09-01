@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title 股票機器人監控中...
echo 正在啟動股票機器人，請勿關閉此視窗...
cd /d "D:\code\stock_ai_bot"
set "UV_CACHE_DIR=%CD%\.runtime\uv_cache"
set "UV_TOOL_DIR=%CD%\.runtime\uv_tools"

REM === MiniMax MCP 自動檢查/安裝 ===
REM 執行 MiniMax MCP 檢查腳本，解析 key=value 輸出設定環境變數
set "MINIMAX_MCP_READY=0"
set "MINIMAX_MCP_COMMAND="
set "MINIMAX_MCP_ERROR="
for /f "usebackq tokens=1,2 delims==" %%A in (`".venv\Scripts\python.exe" tools\ensure_minimax_mcp.py`) do (
    if "%%A"=="MINIMAX_MCP_READY" set "MINIMAX_MCP_READY=%%B"
    if "%%A"=="MINIMAX_MCP_COMMAND" set "MINIMAX_MCP_COMMAND=%%B"
    if "%%A"=="MINIMAX_MCP_ERROR" set "MINIMAX_MCP_ERROR=%%B"
)
if "%MINIMAX_MCP_READY%"=="1" (
    echo MiniMax MCP ready: %MINIMAX_MCP_COMMAND%
) else (
    echo MiniMax MCP unavailable: %MINIMAX_MCP_ERROR%
    echo Continuing without MiniMax MCP - Tavily/Gemini will be used as fallback.
)
set "MINIMAX_MCP_ARGS="

".venv\Scripts\python.exe" main.py
pause
