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
| `minimax_api_key` | MiniMax M3 報告與搜尋 | 使用 `--model minimax` 或 MiniMax 搜尋時需要 |
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

啟動後會註冊 Telegram slash 指令，並建立每日排程：12:30 監控掃描、13:50 午報、17:45 持股籌碼推播、08:45/18:00 新聞整理（預設使用 MiniMax M3 分類）、20:30 交易日全部選股、21:30 Radar 推播（預設使用 MiniMax M3 短評），以及籌碼與完整資料回補任務。

## 最常用指令

| 需求 | 指令 | 說明 |
|---|---|---|
| 開始使用 | `/start` | 顯示常用入口 |
| 完整說明 | `/help` | 指令總覽與參數範例 |
| 停止任務 | `/stop` | 停止目前執行中的長任務 |
| 選股掃描 | `/scan` | 開啟技術、財報、籌碼掃描選單 |
| 指定日期掃描 | `/scan 2026-05-22` | 以指定日期產生選股結果 |
| 每日雷達 | `/radar` | 今日選股雷達；手動執行可選 AI 短評 |
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

### `/scan` 日期選擇

- `/scan` 不帶日期時，先選擇 8 種選股方式，再選擇「最新日期」或「指定日期」。
- 「最新日期」會使用最近可用交易日；若今天是週末或休市日，會自動往前找最近交易日。
- 「指定日期」與 `/scan 2026-05-22` 會照輸入日期執行，不自動改成最新交易日。
- 「全部執行」會包含財報、法人籌碼、技術面與精選選股，並保存合併候選名單供後續流程使用。

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

### AI 報告閱讀顯示層

所有 AI 報告在輸出 Markdown、HTML 與 Telegram 摘要前，會先經過共用顯示正規化器 `research_center/report_display_normalizer.py`。這層只處理「使用者看到的文字」，不改底層 JSON、metadata、structured_data 或 evidence pack。

共用翻譯表位於 `config/report_display_terms.json`。常見內部狀態會轉成中文投研語句，例如 `verified` 會顯示為「已驗證」、`candidate` 會顯示為「候選觀察」、`missing` 會顯示為「資料缺口」、`L2_media` 會顯示為「媒體來源」、`market_validated = false` 會顯示為「盤面尚未明確驗證」、`news_stats.news_count_24h = 0` 會顯示為「近 24 小時新聞熱度不足」。

HTML 報告的主報告、資料品質、完整來源、本地底稿、QA 與技術附錄分頁也會套用同一套顯示名稱。完整來源會把 `L2_media`、`minimax_mcp_search`、`query=...`、`quality=...` 類型的內部欄位改成人可讀的「來源層級、資料工具、搜尋詞、搜尋任務、品質分數」。技術附錄只顯示中文摘要，不直接把 metadata JSON dump 到畫面。

底層 `.json` 與 `.sources.json` 仍保留原始欄位名稱，供系統回查、測試與後續資料共用層調度。若未來報告正文或 HTML 分頁又出現新的 raw key、英文狀態碼或 snake_case 題材 ID，優先補 `config/report_display_terms.json`，讓所有報告共用同一套顯示名稱。

### Radar Evidence Pack 與分段 AI

`/radar` 的 AI 分析名單會建立 `radar_evidence_pack_v1`。資料包包含候選來源、技術訊號、營收歷史、籌碼評級、本地新聞、外部來源、投研結構化資料、feature pack、data coverage 與 unified evidence pack。

Radar 不會讓 AI 新增候選股，也不會讓 AI 改寫本地分數。AI 的工作是根據完整 evidence pack 產生研究優先級、理由、風險、觀察重點與信心度。

AI 短評採分段處理，預設每段 5 檔。資料量過大時會分段送入模型，不會為了塞進單一 prompt 刪除 evidence pack。每段失敗只會標記該段候選股 AI 短評失敗，保留本地 Radar 評分。

Radar 不執行單檔深度分段調研。若整批 AI prompt 過大，會改成單檔批次並提高壓縮等級；仍然只產生快速短評。完整深度分析維持由 `/research` 負責。

Radar 現在採三層資料策略：

- `Full Evidence Pack`：完整資料保存在本地 artifacts/cache，用於回查與後續指令共用。
- `AI Compact Pack`：送進 AI 的壓縮資料包，保留決策必要欄位，限制來源數、清單長度與單欄文字長度，避免 prompt 過大。
- `Telegram Summary`：Telegram 主訊息只顯示短版理由、風險與觀察重點，完整內容仍可從本地報告檔查看。

`AI Compact Pack` 預設限制外部來源最多 10 則、每則摘要約 300 字、營收近 6 個月、財報近 4 季、籌碼與技術訊號以摘要為主。這是壓縮 AI 輸入，不是刪除本地資料。

若單檔 compact prompt 仍過大，Radar 會依序使用 tighter / minimal compact pack，降低來源數、清單長度與文字長度；不再把同一檔股票拆成大量 AI segment。

Radar AI prompt 會要求 reason、risk、watch 使用繁體中文；專有名詞、公司名、產品名可保留英文，但不得輸出整段英文分析。

長任務會輸出 CMD 心跳進度。`/research`、`/macro`、`/theme`、`/theme_radar`、`/theme_flow`、`/sector_strength`、`/value_scan`、`/topic_maintain`、`/radar` 與 `/backfill` 若執行超過一段時間，會定期顯示已耗時、目前階段與最近進度，避免看起來像卡住。若完整資料回補正在執行，長任務會提示並優先使用既有快取與逾時降級。

Radar Evidence Pack 不再於 `/radar` 現場逐檔執行完整 `/research` 資料蒐集。AI 名單會依序使用同日完整 research structured cache、最近 5 個交易日內的完整 research structured cache、Radar 專用輕量 research cache；若仍沒有快取，才立即建立 Radar 輕量 research pack。

Radar 輕量 research pack 只包含本地 Radar 評分、技術訊號、營收摘要、籌碼評級、本地新聞、外部來源摘要與資料限制，不產生完整 research 報告，也不做深度分段分析。完整個股深度研究仍由 `/research` 負責。

若盤中執行時當日法人資料尚未公告，Radar 會以最近可用籌碼交易日作為資料時點限制；AI prompt 要求把這類情況寫成「資料時點限制」，不要寫成公司營運風險。

每次 Radar 會保存完整分析檔到 `reports/radar/YYYY-MM-DD/radar_xxx/`：

- `radar_summary.md`
- `radar_candidates.json`
- `evidence_pack.json`
- `ai_analysis.json`
- `sources.json`

`data_coverage` 會標示技術、營收、籌碼、新聞、外部來源、財報、融資、法人、TDCC、題材背景與 feature pack 是否可用。AI 必須依資料完整度調整信心度。

Radar 同時建立 `three_layer_context_v1`，固定分成三層：

- `raw_sources`：完整原始來源清單，包含搜尋、抓文、provider 與來源層級資訊。
- `evidence_pack`：本地整理後的候選股證據包，包含技術、營收、籌碼、新聞、結構化投研資料與 unified evidence。
- `final_context`：給 AI 判讀用的濃縮脈絡，但不是唯一資料副本。

若 AI 分析候選股的外部來源少於最低門檻，Radar 會自動追加補搜；若來源仍不足，會在 `source_sufficiency` 與 `data_coverage` 標記不足，不會用空資料硬補結論。

`/value_scan 選股雷達` 會沿用 Radar 快取中的分數、策略、data coverage、AI 來源、three-layer context 與 evidence pack，避免雷達階段蒐集到的資料在價值重估時遺失。

### 族群分析分段 AI

`/theme_radar`、`/theme_flow`、`/sector_strength` 會先建立完整 prompt 並檢查長度；只要 prompt 過大，Gemini、DeepSeek、MiniMax 都會採「完整資料本地保留、AI 分段分析」流程。全市場排行、族群排行、子族群排行、題材命中、供應鏈節點與 data quality 仍會完整保留在本地 structured data 與報告 JSON；送給 AI 時則拆成市場強弱、題材證據、擴散推論與最終整合幾個較小 prompt，避免單次 prompt 超過模型 context window。

`/theme_radar` 的分段會再細分市場漲跌與量能排行、全市場產業排行、族群強弱、子族群強弱、題材排行、強勢股題材命中、新聞趨勢與擴散路徑。這是為了避免單一市場強弱段仍過大；不是刪除本地資料，完整資料仍可在 JSON metadata 與 structured data 回查。

若某一段 AI 失敗，系統只會把該段標記為 fallback，並保留本地段落摘要；不會因單段失敗讓整份族群分析報告直接失敗。報告 metadata 會記錄 `segmented_ai_analysis`、各段 prompt 長度、prompt log path、成功/失敗狀態，方便回查。

補充：`/sector_strength` 會沿用市場漲跌量能、全市場產業排行、族群強弱、子族群強弱等分段，再進行族群整合判讀。`/theme_flow` 會依題材概況、相關股票分批、供應鏈層級分批、盤面驗證、下一層候選與新聞趨勢分段。完整資料仍保留在本地 JSON；分段只控制送給 AI 的批次大小，用來兼顧資料量、報告品質與模型穩定性。

### 共用族群語意資料層

族群分析三個指令共用 `config/sector_alias_map.json`、`config/theme_profiles.json`、`config/company_theme_map.json` 與 `config/supply_chain_nodes.json`。`sector_alias_map.json` 負責把市場產業、新聞用語與常見子族群合併，例如電線電纜/電纜線材歸到電器電纜，汽車材料/汽車零組件/車用電子歸到汽車工業，MLCC/晶片電阻/電感歸到電子零組件的被動元件。

這層資料會同時供 `/theme_radar`、`/sector_strength`、`/theme_flow`、`/topic_maintain` 與 `/value_scan` 使用。市場強勢股進來後，系統會先依別名與子族群規則建立「盤面正在強的族群/子族群」，再用題材庫與供應鏈節點補代表股、受惠邏輯與資料缺口。這樣可以降低報告固定偏向 AI/半導體的問題，也讓被動元件、電線電纜、汽車零組件、重電、金融、營建等非 AI 族群有一致的命名與搜尋詞。

目前資料層已加入第一版 seed：被動元件、電線電纜/強韌電網線材、汽車零組件/車用電子、重電/電力設備、金融保險與營建資產等。這些 seed 是可運作的語意骨架，不代表所有台股子產業已 100% 完整；後續仍應透過 `/topic_maintain`、`/topic_import`、`/topic_review`、`/topic_confirm` 持續補產品、客戶、營收占比與公告/法說證據。

族群強弱判斷不只看單日漲跌。`market_movers` 會保留 `change_pct_5d`、`change_pct_10d`、`change_pct_20d`、`near_high_20d`、`days_since_high`、`pullback_from_high_pct`、`above_ma5/10/20`、`trend_score` 與 `trend_state`。若題材或子族群今天小跌但近期趨勢仍強，會標示為 `trend_pullback`，報告應寫成「近期強勢後整理 / 高位震盪」，不可直接判成弱勢。

## 題材庫與新聞系統

題材庫維護採「分階段 AI 草稿 + 本地組包 + 人工確認」流程。正式庫更新必須經過 `/topic_review` 與 `/topic_confirm`。

`/topic_maintain` 不再要求 AI 一次輸出完整 change pack。流程如下：

1. 本地收集既有題材庫、公司對應、供應鏈節點、company knowledge、近期掃描、Discovery、WebFetch 與規則式 evidence candidates。
2. AI 第一階段只產生候選題材清單。
3. AI 第二階段分批補題材細節，每批只處理少量題材。
4. 本地程式負責 schema 正規化、補欄位、去重、判斷 create/update，最後組成 `TopicChangePack`。
5. 單一批次 JSON 壞掉只會記錄 warning，不會讓整個 `/topic_maintain` 因大型 JSON 解析失敗而中斷。

AI 只提供分析建議；`change_id`、`status`、`raw_response_path`、`prompt_log_path`、正式 JSON 結構與寫檔都由本地程式控制。

新聞推送與新聞庫來源規則：

- `news_articles.news_origin` 用來區分新聞來源：`refresh` 代表 `/news refresh` 或排程新聞整理；`manual` 代表使用者貼到 Telegram 或 `/news_save` 的新聞；`research` 代表 `/research`、`/theme`、`/value_scan` 等調研流程保存的搜尋來源。
- `/news latest`、`/news 7d`、08:45 與 18:00 定時推送只顯示 `news_origin=refresh` 的新聞。手動貼上的新聞與調研來源會保存在新聞庫，但不會主動推送。
- 08:45、18:00 定時推送與 `/news latest` 使用同一套顯示篩選、日期排序、分類正規化與持股分流邏輯；排程不另外走一套新聞排序。
- `/news latest` 先顯示明確發布時間在近 24 小時內的新聞；若合格新聞不足，才補近 48 小時內新聞。明確超過 48 小時的舊新聞不應進入最新新聞推送。
- 顯示新聞時，命中目前持股的新聞會集中到最後的「庫存持股新聞」區塊，不會同時留在前方一般分類。
- 新聞顯示日期優先使用來源提供的 `published_at`。只有 `news_origin=refresh` 且 `published_at` 空白時，才允許用 `created_at` 作為輔助日期；`manual`、`research`、`topic`、`unknown` 不會因為今天入庫就被當成今日新聞推送。
- 顯示前會再做一次本地分類正規化：大盤/法人/指數新聞歸「台股與大盤」；明確個股營收、法說、獲利、目標價、漲停等歸「個股利多利空」；族群、概念股、多檔齊漲、受惠股等歸「題材與族群輪動」；PCB、CCL、散熱、電源、DRAM、記憶體、光通訊、伺服器等歸「供應鏈與產業」。
- 使用者偏好只在通過台股財經過濾、非新聞頁過濾、日期過濾與來源過濾後，對合格新聞做小幅排序加權；偏好不會讓非台股新聞、舊新聞、手動貼文或調研資料進入推送。
- AI 新聞分類預設採較小批次與較短 timeout。若模型連續逾時，該輪剩餘新聞會改用本地分類規則，避免卡在同一批次太久。
- Tavily 搜尋前會優先呼叫官方 `GET https://api.tavily.com/usage` 檢查真實額度。若官方 usage 顯示仍有可用額度，系統會清除 `.cache/search_provider_quota.json` 中的舊 exhausted marker，並以官方剩餘額度為準；只有 usage endpoint 無法取得時，才退回本地 marker 與月用量估算。
- Tavily 若被搜尋 API 明確回覆用量耗盡，系統仍會在 `.cache/search_provider_quota.json` 記錄暫停標記。若後台額度已恢復但系統仍顯示 exhausted，請先確認 bot 讀到的是同一支 API key，再檢查官方 usage 與本地 quota marker。
- `/news refresh` 使用分類導向搜尋 query，會分別補強台股與大盤、題材與族群輪動、AI / 半導體、個股利多利空、供應鏈與產業、政策 / 匯率 / 總經等方向，避免全部搜尋結果都集中在大盤新聞。
- `/news refresh` 完成時會輸出分類統計與持股新聞候選數，方便判斷是搜尋不足、過濾過多、分類集中，或只是新增內容重複。
- 新聞入庫與顯示前會排除報價頁、個股行情頁、股票明細頁、查詢頁、清單頁、首頁、行情排行頁與泛國際新聞。例如 Yahoo 股價頁、Yahoo 漲幅排行、Yahoo 台股盤勢、Goodinfo 個股頁、HiStock 個股頁、nStock 個股頁、鉅亨個股基本面頁、StatementDog 財報頁、WantGoo 行事曆、Fugle AI 個股頁、PChome 個股資料、Money-Link 個股新聞列表、鉅亨指數資金流向與工商時報行情頁不會當成新聞文章。
- `/news latest` 優先顯示最近 24 小時合格新聞；若新聞量不足，可補最近 48 小時內的合格新聞。系統不會用超過 48 小時、非文章頁或非台股財經新聞硬補；`2 days ago` 這類邊界文字會視為不符合 latest 顯示條件。
- `/news latest` 與 `/news 7d` 顯示前會再做一次分類正規化。報價頁、清單頁、影片頁、活動頁、無明確台股財經關聯的英文泛新聞會被排除，不會因為曾經入庫就直接顯示。DIGITIMES、TWSE/TPEX 英文頁等英文台灣財經資料可保存在新聞庫供調研使用，但 Telegram 新聞推送預設不顯示。
- 新聞排序會優先使用來源提供的發布時間；有明確發布時間的新聞會排在只有入庫時間的舊資料前面。缺少發布時間的資料會被降權，避免舊頁面看起來像今日新聞。
- 題材與族群輪動、供應鏈與產業、政策 / 匯率 / 總經、個股利多利空等分類會依本地規則修正。明確的 `sector rotation`、概念股、輪動新聞會歸到題材與族群輪動；PCB、散熱、電源、伺服器零組件等會優先歸到供應鏈與產業；大盤、加權指數、三大法人、短線過熱、台股創高等強盤勢訊號會優先歸到台股與大盤。

題材正式庫資料正規化規則：

- `/topic_confirm` 寫入正式題材庫時，會先經過 `research_center/topic_data_normalizer.py` 正規化。
- 常見簡體詞會轉為繁體，並遞迴清理 dict/list 內的文字。
- `product_lines`、`customers`、`keywords` 等清單欄位只保留純字串；AI 產生的 `{value, status, evidence, missing_data}` 包裝欄位會攤平成正式資料可用的字串陣列。

常用指令：

```text
/topic_maintain
/topic_maintain --model minimax

`/topic_maintain` is the single topic-library maintenance entrypoint. It always runs broad full-market maintenance across multiple sectors; users do not need to choose a maintenance mode.

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

## 定時任務序列與排程

- 08:45、18:00：定時新聞整理與推播，預設使用 MiniMax M3 做新聞分類與整理。
- 20:30：交易日執行全部選股，等同 `/scan` 選項 7，完成後發送 Telegram 訊息。
- 21:30：交易日執行 Radar 推播，原 20:30 Radar 已移到 21:30；定時 Radar 預設啟用 MiniMax M3 短評。
- 報告與推播類定時任務使用序列佇列；若前一個定時任務尚未完成，下一個任務會排隊，等前一個完成後接續執行。
- 回補任務維持背景執行，不進入報告推播佇列，避免回補卡住選股、Radar、新聞、午報或持股推播。

## News Display Rules

- `/news latest` selects explicit 24-hour news first. It only uses 48-hour fallback when the explicit 24-hour pool is too small, and blank publish-time items are demoted.
- Portfolio news is strict: a broad market or sector article is not moved into「庫存持股新聞」only because metadata contains a held symbol. It must mention the holding in the title, or be classified as clear company news.
- CMoney/Readmo-style lightweight investment-blog sources are kept as backup items but receive a display ranking penalty, so mainstream finance-media articles appear first.
- English-only pages from DIGITIMES, TrendForce, TWSE English, and TPEx English can remain in the database for research, but Telegram news display hides them by default.

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
