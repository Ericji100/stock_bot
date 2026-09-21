# TMF 啟蒙純 AI 因果回放 v1

## 目的與隔離範圍

本實驗用完整啟蒙課程做方向中立的市場判讀，第一階段只模擬多單。它只讀取歷史 TMF 1 分K，不匯入或啟動正式 `trade_monitor`、Telegram、Bot 或 Automation。

舊 v195～v197 保留為歷史工程實驗；其結果不得與本版績效混合。

## 固定判讀與成交條件

- 課程契約：`trade_monitor_replay/rules/course-pure-ai-v1-long-only/course-contract.md`
- 輸出 schema：`trade_monitor_replay/rules/course-pure-ai-v1-long-only/analysis-schema.json`
- Cohort：`trade_monitor_replay/plans/pure-ai-course-v1-cohort.json`
- 模型：`gpt-5.6-sol`
- 推理強度：`medium`
- 分析頻率：每 2 根已收盤 1 分K
- 執行：AI 在收盤後決定，程式於下一根可交易 1 分K 開盤成交
- 部位：單口 TMF，不加碼、不攤平
- 基準完整進出成本：NT$50
- 滑價敏感度：單邊 0、1、2 點

契約、schema、system prompt、延續提示詞、完整提示詞模板、行情來源、模型、頻率及成本都以 SHA-256 或 manifest 欄位封存。不同識別資料的 run 不得聚合。

## 因果與傳輸

每輪完整 runtime 都先在本機建立，且不包含 `as_of` 之後的當前時段 K。價格稽核只接受輸入中的真實 1 分K時間、角色及價格。

同一交易日時段使用可恢復的 Codex 對話：首輪載入完整課程契約及盤前背景，後續只傳最新因果 K、當前客觀摘要、持倉及成交事件。這只壓縮重複傳輸；定錨、道氏、象限、太極、戰法與買賣決定仍由 AI 負責。每 8 輪重新載入完整契約及因果快照，限制對話上下文成長。

15 分鐘資料只作壓縮概覽。每列另外保存實際 `high_time`、`low_time`、`close_time`，避免把區間高低錯誤綁到 bucket 起始時間。

## 操作

驗證資料、版本及 cohort：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m trade_monitor_replay.pure_ai_runner --config .runtime\trade_monitor_replay\pure-ai-v1-config.json plan
```

小批次啟動：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m trade_monitor_replay.pure_ai_runner --config .runtime\trade_monitor_replay\pure-ai-v1-config.json run --session-key 2026-08-11:DAY --max-ai-calls 5
```

從既有 checkpoint 繼續：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m trade_monitor_replay.pure_ai_runner --config .runtime\trade_monitor_replay\pure-ai-v1-config.json run --session-key 2026-08-11:DAY --resume-run-id <run_id> --max-ai-calls 5
```

`--max-ai-calls` 是安全 checkpoint 上限，不改變分析頻率。每次暫停都保留原始 prompt、AI 原始輸出、驗證後輸出、成交事件、用量、完整行情副本、manifest 及可恢復狀態。

## 目前狀態與限制

工程冒煙測試已證明盤前與日盤延續判讀可通過 schema、時間及價格稽核。這不等於完成多日績效，也不構成正期望或可實戰證據。

目前完整輸出每輪仍約需 2～3 分鐘；持續對話已大幅減少重複 prompt，但完整 JSON 狀態輸出仍是主要耗時。後續若再縮減輸出，只能改傳輸表示或去除重複文字，不得把課程判讀改成交由程式 gate。
