# 台指期監控分鐘時鐘隔離測試

本目錄是獨立實驗，不屬於既有 Telegram 機器人或正式 Codex heartbeat。

## 不變更範圍

- 不修改 `main.py`、`monitor_service.py`、`config.json`。
- 不修改正式 `trade_monitor/bridge.py` 與 `trade_monitor/resume_guard.py`。
- 不修改 `C:\Users\紀成達\.codex\automations\1-k\automation.toml`。
- 不匯入 Telegram 模組、不讀取 Token 或 Chat ID、不發送 TG。
- 所有實驗輸出只會寫入本目錄的 `.runtime`，且已由 `.gitignore` 排除。

## 1. 建立唯讀提示詞快照與保護雜湊

```powershell
python -m trade_monitor.experiments.clock_probe.clock_probe snapshot
```

這會把目前 automation 的提示詞逐字複製至 `.runtime/prompt_snapshot.txt`，並記錄受保護檔案 SHA-256。它不會回寫來源檔。

重新驗證來源未變：

```powershell
python -m trade_monitor.experiments.clock_probe.clock_probe verify
```

## 2. 測試本機分鐘時鐘

每分鐘第 5 秒觸發，共觀察 10 次：

```powershell
python -m trade_monitor.experiments.clock_probe.clock_probe observe --cycles 10 --align-second 5
```

結果寫入 `.runtime/clock_events.jsonl` 與 `.runtime/clock_summary.json`。若 Chrome 控制端另外將唯讀截圖放入指定目錄，可加上 `--capture-dir <path>`，本工具只會記錄最新 PNG 的路徑與時間，不會操作瀏覽器。

## 3. 預覽 Codex dry-run

未加 `--execute` 時只建立命令預覽，不會呼叫 Codex：

```powershell
python -m trade_monitor.experiments.clock_probe.clock_probe codex-dry-run --image <chart.png>
```

正式隔離測試需明確加上 `--execute`。它使用 `codex exec --ephemeral --sandbox read-only --ignore-user-config --ignore-rules`，只分析附圖，輸出留在 `.runtime`，不允許工具、瀏覽器、網路、Telegram、bridge、resume guard 或檔案修改。原始提示詞以逐字不變的區塊附加；外層只增加實驗安全限制。

```powershell
python -m trade_monitor.experiments.clock_probe.clock_probe codex-dry-run --image <chart.png> --execute
```

此測試只比較啟動與分析耗時，不會取代正式監控，也不會自動切換 TG 訊息來源。

## 4. 時間錨定與固定格式的結構化 dry-run

`structured-dry-run` 會在 Codex 分析前由本機程式計算：

- 擷取當下的台灣時間。
- 當前未收盤 1 分 K。
- 最新已收盤 1 分 K（當前分鐘減一分鐘）。
- 圖片是否與上一輪完全相同，以及連續未變時間。
- 是否發生相同 K 棒重複處理或時間倒退。

Codex 只依 `analysis_schema.json` 回傳 JSON；`message_renderer.py` 再以固定的九段順序產生 Markdown。原提示詞快照不會被修改。

```powershell
python -m trade_monitor.experiments.clock_probe.clock_probe structured-dry-run `
  --image <chart.png> `
  --captured-at 2026-09-01T23:11:05+08:00 `
  --execute
```

隔離輸出包括：

- `capture_context.json`：時間錨定與圖片新鮮度。
- `analysis.json`：符合 JSON Schema 的 Codex 原始分析。
- `analysis_message.md`：固定九段 Markdown。
- `structured_dry_run_result.json`：耗時、結構驗證及事件編號。
- `monitor_state.json`：上一個已完成事件、圖片指紋及停滯時間。

`FRESH` 才會呼叫 Codex。`SAME_BAR`、`REGRESSION` 或 `STALE` 會跳過分析並產生固定格式的禁止交易訊息。本實驗仍不會發送 Telegram。
