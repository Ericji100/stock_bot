# Telegram 台股策略機器人

這是一套以 Python + Telegram Bot 建立的台股量化選股與 AI 投研系統，支援全市場技術、財報、籌碼掃描、每日 Radar 推播、個股 AI 研究、價值重估分析、題材庫維護、新聞趨勢統計、資料回補與報告輸出。

README 只保留專案入口、啟動方式、常用指令與維護索引；完整歷史內容已保留在 [docs/legacy-readme-full.md](docs/legacy-readme-full.md)，後續細節文件集中在 [docs/](docs/)。

## 核心工作流

```text
資料回補 /backfill
→ 全市場選股 /scan
→ 每日雷達 /radar
→ 題材雷達 /theme_radar
→ 單股研究 /research
→ 價值重估 /value_scan
→ 報告查詢 /report
```

日常使用可以從 `/start` 或 `/help` 進入互動選單；工程維護則先看本 README，再依需求進入 `docs/`。

## 目前狀態

| 模組 | 狀態 | 備註 |
|---|---|---|
| Telegram Bot 指令入口 | 穩定可用 | `/start`、`/help`、互動選單與任務停止機制已接入 |
| `/scan` 全市場選股 | 穩定可用 | 技術、財報、籌碼策略與指定日期掃描 |
| `/radar` 每日雷達 | 穩定可用 | 可選 AI 短評；`/radar_more` 查看完整名單 |
| 個股資料匯出 | 穩定可用 | `/export`、`/stock_chart`、`/tmf_chart` |
| 持股與監控 | 穩定可用 | `portfolio.json` 與 `config.json` 分別管理持股、監控清單 |
| AI 投研資料中心 | 可用，持續調整 | Gemini、DeepSeek、MiniMax；輸出 Markdown、HTML、JSON |
| `/value_scan` 價值重估 | 可用，持續調整 | 使用本地重估底稿與外部證據，避免只因熱門題材給高分 |
| 題材雷達與題材庫 | 可用，持續調整 | 代表股、候選股、疑似蹭題材需分開標示 |
| 新聞系統 | 可用，持續調整 | 本地新聞庫、外部搜尋與每日排程整理 |
| 本機 Research API | 實驗中 | FastAPI，預設 `127.0.0.1:8000` |

## 快速啟動

### 1. 安裝依賴

建議使用專案內的 `.venv` 或自己建立虛擬環境：

```bash
pip install -r requirements.txt
```

主要依賴包含 `python-telegram-bot`、`pandas`、`yfinance`、`httpx`、`openpyxl`、`fastapi`、`uvicorn`、`beautifulsoup4` 與 `mcp`。

### 2. 設定 Telegram Bot

建立或修改 `config.json`：

```json
{
  "api_token": "YOUR_TELEGRAM_BOT_TOKEN",
  "chat_id": "YOUR_TELEGRAM_CHAT_ID",
  "fugle_api_key": "YOUR_FUGLE_API_KEY",
  "scan_settings": {
    "min_price": 10,
    "max_price": 80,
    "min_avg_volume_20d": 500,
    "min_monthly_revenue": 50000000
  },
  "monitor_stocks": [
    {"symbol": "2330.TW", "name": "台積電"}
  ]
}
```

`api_token` 與 `chat_id` 是 Bot 基本啟動必填；`fugle_api_key` 可作為資料備援。

### 3. 設定 AI 與搜尋金鑰

AI 投研中心讀取 `config/research_center.json` 與 `config/secrets.json`。`config/research_center.json` 放公開設定，`config/secrets.json` 放金鑰，不要提交到 Git。

常用 secret key：

| Key | 用途 | 必要性 |
|---|---|---|
| `gemini_api_key` 或 `google_api_key` | Gemini 投研與 grounding | AI 功能建議設定 |
| `opencode_api_key` | DeepSeek V4 Pro / OpenCode Go | 使用 `--model deepseek` 時需要 |
| `minimax_api_key` | MiniMax M2.7 報告與搜尋 | 使用 `--model minimax` 或 MiniMax 搜尋時需要 |
| `tavily_api_key` | Tavily Search / Extract | 外部搜尋與新聞補強建議設定 |
| `research_api_token` | 本機 Research API 驗證 | 使用 API 時需要 |

### 4. 啟動

```bash
python main.py
```

Windows 可直接執行：

```text
啟動機器人.bat
```

啟動後會註冊 Telegram slash 指令，並建立每日排程：12:30 監控掃描、13:50 午報、17:45 持股籌碼推播、08:45/18:00 新聞整理、20:30 Radar 推播，以及籌碼與完整資料回補任務。

## 最常用指令

| 需求 | 指令 | 說明 |
|---|---|---|
| 開始使用 | `/start` | 顯示常用入口 |
| 完整說明 | `/help` | 指令總覽與參數範例 |
| 停止任務 | `/stop` | 停止目前執行中的長任務 |
| 選股掃描 | `/scan` | 開啟技術、財報、籌碼掃描選單 |
| 指定日期掃描 | `/scan 2026-05-22` | 以指定日期產生選股結果 |
| 每日雷達 | `/radar` | 今日選股雷達與 AI 短評 |
| 雷達完整名單 | `/radar_more` | 查看最近一次 Radar 完整名單 |
| 單股研究 | `/research 2330` | 個股資料彙整與 AI 研究 |
| 深度研究 | `/research 2330 --deep` | 較完整的外部證據與投研報告 |
| 個股評分 | `/research 2330 --score` | 個股評分研究 |
| 價值重估 | `/value_scan 2330` | 單股價值重估 |
| 候選池重估 | `/value_scan 精選選股 --top 30` | 掃描候選池並排序 |
| 新聞 | `/news latest` | 查看本地新聞庫最新新聞 |
| 更新新聞 | `/news refresh --model deepseek` | 外部搜尋並整理新聞 |
| 題材研究 | `/theme AI伺服器` | 題材研究 |
| 題材雷達 | `/theme_radar` | 市場題材強弱與族群輪動 |
| 題材擴散 | `/theme_flow AI伺服器` | 題材上下游與擴散路徑 |
| 類股強弱 | `/sector_strength` | 族群強弱排行 |
| 報告查詢 | `/report latest` | 查看最近一份報告 |
| 資料回補 | `/backfill` | 回補本地資料與快取 |
| 匯出資料 | `/export 2330` | 匯出價量、法人、融資融券、營收、財報 |

完整命令請看 [docs/commands.md](docs/commands.md)。

## 使用情境

| 你想做的事 | 建議流程 |
|---|---|
| 每日快速選股 | `/backfill` → `/scan` → `/radar` |
| 找近期資金題材 | `/news refresh` → `/theme_radar` → `/theme_flow 題材` |
| 深入研究單一股票 | `/research 股票代號 --deep` → `/report 股票代號 latest` |
| 驗證舊標籤轉新標籤 | `/value_scan 股票代號 --deep` |
| 只看本地已產生報告 | `/report latest` 或 `/report 2330 latest` |
| 維護題材庫 | `/topic_maintain` → `/topic_review` → `/topic_confirm` |
| 檢查資料健康度 | `/data_status 2330`、`/backfill_status`、`/news_status 2330` |

## 功能模組

| 檔案或目錄 | 職責 |
|---|---|
| `main.py` | Telegram Bot 入口、指令註冊、排程與任務控制 |
| `technical_scanner.py` | 技術面選股與多策略掃描 |
| `technical_strategy_engine.py` | 技術策略引擎 |
| `stock_scanner.py` | 財報、營收與全市場候選股掃描 |
| `chip_strategies.py` | 法人、投信、大戶與 TDCC 籌碼策略 |
| `radar_service.py` | 每日 Radar 候選池、評分與推播 |
| `backfill_service.py` | 全市場資料回補與快取暖機 |
| `export_service.py` | 個股資料 Excel 匯出 |
| `portfolio_manager.py` | 個人持股管理 |
| `market_summary.py` | 晨報、午報與市場摘要 |
| `research_center/` | AI 投研、新聞、題材、報告、資料服務與 API |
| `prompt/` | 報告、題材、新聞、評分與規則 Prompt |
| `config/` | 題材庫、公司知識庫、資料來源與 AI 公開設定 |
| `reports/` | Markdown、HTML、JSON 投研報告輸出 |
| `database/` | SQLite 報告 metadata、新聞與事件資料 |
| `.cache/` | 選股、回補、籌碼、研究結構化快取 |
| `tests/` | pytest 測試 |

更完整架構請看 [docs/architecture.md](docs/architecture.md)。

## 資料來源與快取

| 資料 | 主要來源 | 本地位置 | 備註 |
|---|---|---|---|
| 股價與技術指標 | TWSE、TPEx、Yahoo、Fugle 備援 | `.cache/` | 大量掃描優先使用快取 |
| 月營收與財報 | MOPS、公開資料、本地快取 | `.cache/` | 回補後供 `/scan` 與 AI 投研使用 |
| 法人籌碼 | TWSE、TPEx、FinMind、Fugle | `.cache/chip_daily` | 缺口時可嘗試 FinMind 即時備援 |
| 大戶分布 | TDCC | `.cache/tdcc` | 價值重估與籌碼分析使用 |
| 新聞 | MiniMax、Tavily、Gemini、新聞 URL 匯入 | `database/stock_research.db` | `/news latest` 讀本地庫；`/news refresh` 會搜尋 |
| 題材庫 | `config/theme_profiles.json` 等 | `config/`、`data/theme/` | 變更包需 review/confirm |
| AI 報告 | Gemini、DeepSeek、MiniMax | `reports/`、`database/` | 同時保存來源與 metadata |

詳細資料策略請看 [docs/data-sources.md](docs/data-sources.md)。

## AI、搜尋與成本

| 指令 | AI | 外部搜尋 | 說明 |
|---|---|---|---|
| `/scan` | 否 | 否 | 本地量化掃描 |
| `/radar` | 可選 | 通常否 | 預設可產生 AI 短評；`--no-ai-comment` 可關閉 |
| `/radar_more` | 否 | 否 | 讀最近 Radar 結果 |
| `/news latest` | 否 | 否 | 讀本地新聞庫 |
| `/news refresh` | 是 | 是 | 會使用模型與搜尋服務更新新聞 |
| `/theme_radar` | 是 | 視情況 | 主要依本地新聞、掃描與題材資料，可選模型摘要 |
| `/research` | 是 | 通常是 | 個股投研，會按資料覆蓋度補外部證據 |
| `/value_scan` | 是 | 通常是 | 重估分析，需要來源與反證 |
| `/topic_import` | 否 | 否 | 匯入外部 AI JSON，本地轉變更包 |
| `/topic_source_sync` | 否 | 是 | 同步 TPEx/UDN 外部題材來源 |
| `/report latest` | 否 | 否 | 讀本地報告 |

核心規則：

- 本地量化底稿與 AI 最終投研評分必須分離。
- AI 不得直接照抄本地分數，需說明證據、反證與資料缺口。
- 歷史日期模式不得偷看未來；只能使用當時或已保存的快照。
- 題材代表股、推論型代表股、待驗證候選股與疑似蹭題材必須分開標示。
- `/value_scan` 不得只因熱門題材給高分，必須結合營收、財報、估值、籌碼與可驗證催化。

更多細節請看 [docs/ai-research.md](docs/ai-research.md)。

## 題材庫與新聞系統

題材庫維護採「AI 草稿 + 人工確認」流程。AI 可以提出變更包，但正式庫更新必須經過 `/topic_review` 與 `/topic_confirm`。

常用指令：

```text
/topic_maintain
/topic_maintain --bootstrap --model minimax
/topic_review
/topic_review change_xxx
/topic_confirm change_xxx
/topic_reject change_xxx
/topic_profiles
/topic_source_sync --tpex
/topic_source_sync --udn
```

題材分類規則、代表股命名規則與新聞庫流程請看 [docs/topic-system.md](docs/topic-system.md)。

## 報告與 API

AI 投研報告輸出在 `reports/`，metadata、新聞、事件與來源快照保存在 `database/stock_research.db`。

本機 API 設定在 `config/research_center.json`：

```json
{
  "api_host": "127.0.0.1",
  "api_port": 8000,
  "database_path": "database/stock_research.db",
  "report_root": "reports"
}
```

若使用 API，請在 `config/secrets.json` 設定 `research_api_token`。

## 測試

```bash
pytest
```

常見 focused tests：

```bash
pytest tests/test_radar_service.py
pytest tests/test_research_center.py
pytest tests/test_theme_radar_feature.py
pytest tests/test_topic_maintain_service.py
pytest tests/test_backfill_service.py
```

測試策略與 smoke test 請看 [docs/testing.md](docs/testing.md)。

## 文件索引

| 文件 | 內容 |
|---|---|
| [docs/commands.md](docs/commands.md) | Telegram 指令完整速查 |
| [docs/architecture.md](docs/architecture.md) | 模組架構、資料流與重要規則 |
| [docs/ai-research.md](docs/ai-research.md) | AI 投研、模型、搜尋、Prompt 與成本 |
| [docs/data-sources.md](docs/data-sources.md) | 資料來源、快取、回補與新鮮度 |
| [docs/topic-system.md](docs/topic-system.md) | 題材庫、新聞、代表股分類與維護流程 |
| [docs/operations.md](docs/operations.md) | 啟動、排程、設定、報告與日常維運 |
| [docs/testing.md](docs/testing.md) | 測試與驗收指令 |
| [CHANGELOG.md](CHANGELOG.md) | 重要變更摘要 |
| [docs/legacy-readme-full.md](docs/legacy-readme-full.md) | 原始 README 全文備份 |

## 維護原則

- 任何功能、指令、資料流程、AI 行為、設定、排程或重要修正的改動，都必須同步檢查並補充 `README.md` 與 `CHANGELOG.md`。
- README 只寫入口、現況與索引；細節放 `docs/`。
- 日期型更新、修正紀錄與歷史脈絡放 `CHANGELOG.md` 或 legacy 文件。
- Prompt、題材規則、搜尋成本、資料來源規則要放在可維護的獨立文件。
- 新增指令時，同步更新 `research_center/telegram_handlers.py` 的 help 文字與 [docs/commands.md](docs/commands.md)。
- 新增 AI 或外部搜尋行為時，同步更新本 README 的成本表與 [docs/ai-research.md](docs/ai-research.md)。
