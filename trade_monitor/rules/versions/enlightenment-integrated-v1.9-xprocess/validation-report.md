# v1.9 正式升級前驗證

- 還原點：`pre-xprocess-v1.8-20260902`，與升級前 Automation prompt、scheduler config、schema 及 active-version 雜湊相符。
- 歷史因果回放：20 個日盤、5,991 根日盤 1 分 K；n=2 為 809 legs，n=3 為 664 legs；20 個第一次端點因果樣本。
- 隔離 Shadow：3 輪合法 v7/state v5，分析時間 70.061、71.955、98.270 秒，平均 80.095 秒；Telegram 全程 dry-run。
- 測試：正式切換前完整集合 `197 passed`。
- 固定介面：原四型態、九欄、canonical message、單口管理與雙模式輸出未新增平行答案。
- 前向限制：五個完整交易日 Shadow 尚未完成；使用者明確接受並要求正式升級。

正式部署後仍須反向核對 Automation prompt SHA-256、RRULE、target thread、PAUSED、scheduler config 與 active-version，全部相符才可宣告完成。
