@echo off
chcp 65001
title 股票機器人監控中...
echo 正在啟動股票機器人，請勿關閉此視窗...
cd /d "C:\code\stock_tg_bot"
".venv\Scripts\python.exe" main.py
pause
