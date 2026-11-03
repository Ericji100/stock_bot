# 維運手冊

本文件整理啟動、設定、排程、報告位置與常見維護動作。

## 啟動

```bash
python main.py
```

Windows 可執行：

```text
啟動機器人.bat
```

日常只使用 `啟動機器人.bat`，沿用單一 watchdog 與 runner，不從開發 worktree 啟動第二個 Bot。DeepSeek-only 模式的分析與搜尋使用官方 DeepSeek；不再啟動舊 MiniMax MCP 或使用舊 Gemini／Tavily 金鑰。公開行情與非 AI 選股照原流程。

## 設定檔

| 檔案 | 用途 |
|---|---|
| `config.json` | Telegram token、chat id、Fugle key、掃描設定、監控清單 |
| `portfolio.json` | 個人持股 |
| `config/research_center.json` | AI 投研公開設定、報告位置、搜尋開關 |
| `config/secrets.json` | AI、搜尋與 API 金鑰 |
| `stock_list.json` | 股票代號與名稱 |

`config/secrets.json` 不應提交到 Git。

## 排程

| 時間 | 任務 |
|---|---|
| 啟動後 | 視條件補發晨報 |
| 12:30 | 監控掃描 |
| 13:50 | 台股午報 |
| 17:45 | 持股籌碼推播 |
| 18:00 | 每日新聞背景整理；正常新聞不發 Telegram，異常通知另行去重 |
| 20:30 | 交易日全部選股 |
| 21:30 | 交易日 Radar 推播，15 檔 DeepSeek Flash 短評 |
| 週二、五 22:30 | AI 題材庫維護，產生待審核更新包，不自動套用 |
| 16:30、18:30、21:00 | 籌碼快取回補 |
| 每 2 小時 | 完整資料回補健康檢查 |

## 報告位置

| 位置 | 內容 |
|---|---|
| `reports/` | Markdown、HTML、JSON 報告 |
| `database/stock_research.db` | 報告 metadata、新聞、事件、來源快照 |
| `.cache/research_structured/` | AI 投研結構化快取 |

報告查詢：

```text
/report
/report latest
/report 2330 latest
/report theme AI伺服器 latest
```

## 日常檢查

AI 月總額 US$10：背景 7、手動／測試 2、緩衝 1。背景約 US$6.63 是目前樣本估算，不是固定帳單。額度不足或未知費用仍占額度時，停止新付費請求、保存成果並發去重異常通知；行情與本地選股不因此停用。完整驗證與已知限制見 [DeepSeek 遷移驗證](deepseek-unification-verification.md)。

```text
/backfill_status
/data_status 2330
/news_status 2330
/radar_more
/report latest
```

## 常見維護動作

| 情境 | 動作 |
|---|---|
| 掃描資料疑似過期 | `/backfill` |
| 指定日期資料缺漏 | `/backfill 2026-05-22 force` |
| AI 報告來源不足 | 檢查 `config/secrets.json` 與 `config/research_center.json` 搜尋開關 |
| 題材代表股誤判 | 建立或審核 change pack，使用 `/topic_review`、`/topic_confirm` |
| 新聞庫沒有新資料 | `/news refresh --model deepseek` |
| 長任務卡住 | `/stop` |

## 編碼

專案以 UTF-8 儲存中文文件與設定。Windows 終端若出現亂碼，先確認：

```text
chcp 65001
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
```

`啟動機器人.bat` 已包含這些設定。

檢查檔案內容是否混入亂碼 marker：

```text
python -B tools/encoding_health_check.py
```

這個工具只做健檢，不會批次修檔。若終端顯示中文亂碼，先確認上述環境設定；若健檢工具回報非 allowlist 檔案有 UTF-8 decode error 或 mojibake marker，再針對該檔案查來源與入口清理邏輯，不要直接做全專案轉碼或批次移除 BOM。
