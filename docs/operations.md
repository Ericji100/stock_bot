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

批次檔會設定 UTF-8、MiniMax MCP 所需的 `UV_CACHE_DIR` / `UV_TOOL_DIR`，並嘗試檢查 MiniMax MCP 是否可用；若不可用，系統會繼續以 Tavily/Gemini fallback 執行。

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
| 08:45、18:00 | 新聞整理與推播 |
| 20:30 | Radar 推播 |
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
