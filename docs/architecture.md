# 架構總覽

本專案由 Telegram Bot、量化掃描、資料回補、AI 投研中心、題材與新聞系統組成。`main.py` 是執行入口；`research_center/` 是 AI 與報告核心；`config/`、`.cache/`、`database/`、`reports/` 分別保存設定、快取、資料庫與報告。

## 主要資料流

```text
公開資料 / 外部搜尋 / 本地設定
→ 回補與快取
→ 量化掃描與候選池
→ Radar / 題材雷達 / AI 投研
→ Markdown、HTML、JSON 報告
→ SQLite metadata、事件與來源快照
```

## 核心模組

| 模組 | 職責 |
|---|---|
| `main.py` | Bot 啟動、指令註冊、排程、任務停止與訊息傳送 |
| `data_fetcher.py` | 股價與公開資料抓取的基礎層 |
| `stock_scanner.py` | 財報、營收與基本面候選股掃描 |
| `technical_scanner.py` | 技術面策略與全市場掃描 |
| `technical_strategy_engine.py` | 技術策略引擎 |
| `chip_strategies.py` | 法人、投信、大戶、TDCC 策略 |
| `radar_service.py` | Radar 候選池、評分、摘要與結果保存 |
| `backfill_service.py` | 本地資料回補與快取暖機 |
| `backfill_gap_service.py` | 缺口檢查 |
| `export_service.py` | 個股資料 Excel 匯出 |
| `portfolio_manager.py` | 個人持股管理 |
| `market_summary.py` | 晨報、午報與市場摘要 |
| `research_center/orchestrator.py` | AI 投研流程編排、模型選擇、報告輸出 |
| `research_center/data_services.py` | AI 所需結構化資料組裝 |
| `research_center/report_builder.py` | Markdown、HTML、JSON 報告產生 |
| `research_center/database.py` | SQLite metadata、事件、來源快照 |
| `research_center/theme_radar_service.py` | 題材雷達與強弱統計 |
| `research_center/topic_maintain_service.py` | 題材庫變更包產生 |
| `research_center/news_service.py` | 新聞更新、保存與摘要 |

## 重要目錄

| 目錄 | 內容 |
|---|---|
| `config/` | 題材庫、公司知識庫、供應鏈節點、AI 公開設定 |
| `prompt/` | 報告模板、新聞模板、題材模板、評分規則 |
| `.cache/` | 選股快取、回補 marker、籌碼快取、研究結構化快取 |
| `database/` | `stock_research.db`，保存報告與新聞 metadata |
| `reports/` | AI 投研報告輸出 |
| `tests/` | pytest 測試 |

## 設計約束

- 本地量化底稿與 AI 最終投研評分要分離。
- AI 報告需呈現來源、反證、資料缺口與判斷理由。
- 歷史日期模式不得偷看未來，只能使用當日以前資料或已保存快照。
- 題材代表股、推論型代表股、待驗證候選股與疑似蹭題材要分開顯示。
- 題材庫正式寫入需走 review/confirm 流程，不讓 AI 直接覆寫正式庫。
- 大量掃描優先讀本地快取，避免反覆打外部 API。

## 排程

`main.py` 啟動後會建立以下常態排程：

| 時間 | 任務 |
|---|---|
| 12:30 | 監控掃描 |
| 13:50 | 台股午報 |
| 17:45 | 持股籌碼推播 |
| 08:45、18:00 | 新聞整理與推播 |
| 20:30 | Radar 推播 |
| 16:30、18:30、21:00 | 籌碼快取回補 |
| 每 2 小時 | 完整資料回補健康檢查 |

## 本機 API

API 入口在 `research_center/api_app.py`，設定位於 `config/research_center.json`：

```json
{
  "api_host": "127.0.0.1",
  "api_port": 8000,
  "database_path": "database/stock_research.db",
  "report_root": "reports"
}
```

若啟用 API，請在 `config/secrets.json` 設定 `research_api_token`。
