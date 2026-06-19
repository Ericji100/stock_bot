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

啟動後會註冊 Telegram slash 指令，並建立每日排程：10:00 AI 題材庫維護（MiniMax M3）、12:30 監控掃描、13:50 午報、17:45 持股籌碼推播、08:45/18:00 新聞整理（預設使用 MiniMax M3 分類）、20:30 交易日全部選股、21:30 Radar 推播（預設使用 MiniMax M3 短評），以及籌碼與完整資料回補任務。

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
| 晨間市場速報 | Yahoo Finance、Yahoo 期貨頁首、TAIFEX 備援 | 即時查詢 | `/morning` 台指期夜盤優先抓最新期貨頁首報價；TAIFEX 日檔只作歷史備援，避免清晨日檔未更新時輸出舊夜盤 |
| 題材庫 | `config/theme_profiles.json` 等 | `config/`、`data/theme/` | 變更包需 review/confirm |
| AI 報告 | Gemini、DeepSeek、MiniMax | `reports/`、`database/` | 同時保存來源與 metadata |

詳細資料策略請看 [docs/data-sources.md](docs/data-sources.md)。

## AI、搜尋與成本

### Tavily 多 Key 輪替

Tavily 可設定多組 Key 共用。建議在 `config/secrets.json` 使用：

```json
{
  "tavily_api_key": "主要 Tavily Key",
  "tavily_api_keys": [
    "備用 Tavily Key 1",
    "備用 Tavily Key 2"
  ]
}
```

執行搜尋時會先用 `tavily_api_key`，若該 Key 被 Tavily 回覆 quota / credit / limit 類錯誤，會自動切到下一組 `tavily_api_keys`。報告 JSON metadata 會記錄 `key_fingerprints_used`、`quota_exhausted_key_fingerprints`、`query_count_by_key_fingerprint`，只保存 Key 指紋，不保存完整 API Key。

### 共用外部搜尋 query 預算

所有會啟動外部搜尋的 AI 指令會先保留搜尋任務分類，再依指令套用每個任務的 query 上限。這是控制 MiniMax Search / Tavily / Gemini Search 的請求數，不是刪除已取得資料；已取得的來源、正文、結構化資料與完整附錄仍照既有入模規則保存。

### 2026-06-18 搜尋品質 Gate 與 Gemini fallback

外部搜尋共用流程適用 `/research`、`/macro`、`/theme`、`/value_scan`、`/theme_radar`、`/theme_flow`、`/sector_strength`、`/news refresh`、`/topic_maintain` 等走共用 discovery flow 的 AI 投研指令。

搜尋順序：

1. MiniMax MCP Search 先搜尋。
2. Tavily Search 在仍有官方剩餘額度時補強。
3. 來源品質不足時才啟用 Gemini Search fallback。
4. 合併、去重、來源分級後，再由 WebFetch 讀取部分來源正文。

Gemini Search 不再只看來源總數是否足夠，而是會檢查來源品質。以下情況會觸發 Gemini fallback：

- 官方來源不足。
- 主流財經或產業媒體不足。
- 社群、論壇、YouTube、短影音或討論區來源比例過高。
- MiniMax MCP Search 錯誤率過高。
- MiniMax 回傳 `minimax_sensitive_query_blocked`。
- 某些搜尋任務完全沒有可用來源。
- 日期可驗證來源比例過低。
- Tavily 額度不可用，且高品質來源數不足。

Gemini discovery 搜尋模型與正式分析模型分開設定。正式分析仍使用 `model`，例如 `gemini-3.1-pro-preview`；搜尋 fallback 使用 `gemini_discovery_model`，預設 `gemini-3.5-flash`，避免搜尋階段先卡在 Pro 模型。若 Gemini Search 逾時，系統會保留 MiniMax / Tavily / 本地來源繼續產出，不會中斷報告。

Live 驗證：

- `2026-06-18` 已用 live audit 跑過主要投研指令批次，包含 `/research`、`/value_scan`、`/macro`、`/theme`、`/theme_flow`、`/theme_radar`、`/sector_strength`、`/radar`、`/news refresh`、`/topic_maintain`，全部成功產出。
- MiniMax MCP Search 在多數指令可取得足量來源；若偵測到 `minimax_sensitive_query_blocked` 或錯誤率過高，系統會自動啟用 Gemini Search fallback。
- Gemini fallback 已使用 `gemini-3.5-flash`，每段 45 秒逾時後安全退出；若 Gemini 未回傳可解析 citations，報告仍使用 MiniMax / WebFetch / 本地來源產出。
- WebFetch 會記錄 `success_ratio`、`quality_status`、`selected_urls`、`web_fetched_sources`。最新 `/news refresh` smoke test 顯示 `success_ratio=0.7`、`quality_status=ok`。
- 完整批次驗證輸出可查 `logs/ai_command_audit/20260618_231058_32728/summary.md`；新聞 WebFetch smoke test 可查 `logs/ai_command_audit/20260618_233907_5228/summary.md`。

預設上限會依指令調整，例如 `/news refresh` 每個新聞分類最多 6 條 query。日期擴充與 site: 查詢仍會保留，但超過上限時只取前幾條代表 query，避免單次新聞更新把 9 個分類展開成上百次搜尋。搜尋 query log 會記錄 `query_budget`，包含原始 query 數、實際送出的 query 數與策略名稱。

可用環境變數微調：

- `AI_DISCOVERY_MAX_QUERIES_NEWS=8`：只調整新聞搜尋。
- `AI_DISCOVERY_MAX_QUERIES_THEME_RADAR=12`：只調整題材雷達搜尋。
- `AI_DISCOVERY_MAX_QUERIES_PER_TASK=8`：作為所有指令的通用預設值。

### 2026-06-19 逐指令搜尋意圖校準

搜尋任務已從泛用新聞搜尋調整為「依指令目的搜尋證據組合」。這次重點是搜尋關鍵字與 discovery rule，不是先改報告格式；報告 prompt 只在資料能被搜尋到後才視需要補章節。

- `/research`：搜尋 MOPS、月營收、財報、年報、法說會、公司 IR、產品客戶、供應鏈、法人關注、毛利率下滑、庫存、需求轉弱與反證。
- `/macro`：搜尋 VIX、美債殖利率、美元指數、油價、黃金、Fed、通膨、台指期、台指選擇權、Put/Call、未平倉、外資期貨、三大法人、融資融券、恐慌 / 貪婪 proxy、關稅、地緣政治、中國、歐洲、日本與壓力測試。若沒有正式台股恐慌 / 貪婪指標，必須標示為 proxy，不得假裝是正式指標。
- `/theme`：搜尋題材定義、產業趨勢、供應鏈位置、產品客戶、訂單、營收驗證、催化事件與題材退燒反證。
- `/theme_flow`：搜尋題材擴散路徑、上中下游傳導、次族群、補漲族群、資金輪動、退潮與輪動失敗。
- `/theme_radar`：搜尋近期爆量題材、主流財經與產業媒體、新聞催化、早期想像空間、過熱與退燒反證；YouTube、Facebook、Threads、論壇會被列入排除或情緒參考。
- `/sector_strength`：搜尋類股資金流、法人買賣、族群強弱、產業催化、短線過熱、法人轉賣與輪動失敗。
- `/value_scan`：搜尋舊標籤 / 新標籤、新產品、新客戶、轉型效益、產品客戶、營收占比、官方公告、月營收、財報、法說會、法人重貼標籤與重估失敗反證。推論型加分必須標示待驗證資料，不能補高財務硬指標。
- `/news refresh`：搜尋台股大盤、題材輪動、AI / 半導體、供應鏈、個股利多利空、政策匯率總經、VIX、台指期、台指選擇權、Put/Call、盤前 / 夜盤突發風險，並持續過濾報價頁、查詢頁、首頁、排行頁與論壇轉貼。
- `/topic_maintain`：搜尋新題材、舊題材退燒、供應鏈新增節點、產品 / 公司 / 客戶 / 營收驗證、反證與資料不足；不得只因社群熱度新增正式題材。

新增測試 `tests/test_search_query_intent.py` 會檢查上述指令是否產出符合目的的 query，並防止搜尋任務檔再次出現 mojibake 亂碼標記。

Live 驗證：

- `2026-06-19` 已用 live audit 跑過 `/research 2330 --deep`、`/macro 台股`、`/theme AI電源`、`/value_scan 精選選股 --deep --top 10`、`/theme_radar`、`/theme_flow AI電源`、`/sector_strength`、`/news refresh`、`/topic_maintain`，全部成功產出。
- `/macro` 實際搜尋任務已包含「國際風險溫度」、「台指期與選擇權」、「台股資金流與籌碼」、「恐慌貪婪與情緒 proxy」、「地緣政治與區域市場」、「反證與壓力測試」。
- `/value_scan` 實際搜尋任務已包含「官方公告與月營收」、「產品客戶與供應鏈驗證」、「舊標籤與新標籤重估」、「法人籌碼與資金確認」、「反證與重估失敗風險」。
- `/theme_radar` 實際搜尋任務已包含「熱門題材與資金輪動」、「題材催化與新聞爆量」、「退燒題材與反證」。
- 本次 Tavily 官方 usage 仍顯示剩餘額度不足；Gemini Search fallback 在需要時會啟動，但 Gemini API 回覆 429 時會安全失敗並沿用 MiniMax / WebFetch / 本地來源，不會中斷報告。
- 完整驗證輸出可查 `logs/ai_command_audit/20260619_001059_17324/summary.md`。

### 2026-06-19 必備資料缺口補搜與正文品質

外部搜尋現在採「原搜尋保留、缺口再補」策略。原本每個指令的搜尋任務仍會先完整執行；MiniMax / Tavily 合併來源後，系統會再檢查該指令的必備資料是否被命中。若缺資料，才建立 `required_gap:*` 補搜任務，並把補搜來源標記為 `required_data_gap_fill` 與「必備資料缺口補搜」。

流程：

1. 執行原本 discovery tasks。
2. 合併 MiniMax MCP Search 與 Tavily Search 來源。
3. 建立 `required_data_gap_summary`。
4. 若缺硬性或軟性必備資料，建立 `required_data_gap_backfill_tasks`。
5. 優先用 MiniMax / Tavily 針對缺口補搜。
6. 仍有缺口或品質不足時，Gemini fallback 只針對剩餘缺口或品質理由補搜。
7. 補不到的項目保留在 `required_data_gap_summary.missing`，報告需標示資料不足。

`/macro` 必備資料分三層：

- 硬性必備：VIX 或全球風險指標、美債殖利率 / Fed / 美元、上市加權指數與成交量及漲跌家數、上櫃櫃買指數與成交量及漲跌家數、上市 / 上櫃三大法人或外資資金流、台指期或台指選擇權、原油 / 黃金 / 關稅 / 戰爭 / 地緣政治至少一項。
- 軟性必備：Put/Call Ratio、未平倉、外資期貨淨部位、恐慌 / 貪婪 proxy、類股輪動、中國 / 歐洲 / 日本區域風險。
- 加分資料：正式 IV、期貨大額交易人、逐產業法人資金流、全球 ETF / 基金流、信用利差或金融壓力指數。

其他指令也有必備資料檢查：

- `/research`：官方公告 / MOPS、月營收、財報、法說會 / IR、產品客戶、法人籌碼、反證風險。
- `/theme`、`/theme_flow`：題材定義、產業趨勢、供應鏈公司、產品客戶驗證、催化劑、退燒或反證。
- `/value_scan`：舊標籤 / 新標籤、月營收與財報、產品客戶變化、法人籌碼、重估失敗風險。
- `/theme_radar`：熱門題材、資金輪動、催化新聞、題材退燒；社群熱度只作輔助，不作核心證據。
- `/news refresh`：台股盤勢、總經與國際風險、題材輪動、個股公告、資金籌碼、反證新聞。

WebFetch 正文抓取新增品質欄位：

- `fetch_status`：`success`、`partial`、`failed`。
- `fetch_quality`：`high`、`medium`、`low`。
- `failure_reason`：可能包含 `too_short`、`no_keyword_match`、`non_article_page`、`missing_published_date`、`not_article_like`、`blocked` 或 `timeout` 等原因。

WebFetch 會檢查正文長度、是否命中標的或指令關鍵字、是否像文章頁、是否為首頁 / 搜尋頁 / tag 頁 / YouTube 或社群頁，以及是否能取得發布日期。歷史日期模式下，沒有日期的來源不得當核心證據。

`/radar` 的選股本體仍以本地資料、技術面、營收、籌碼與 Radar 分數為主；若啟用 AI 短評，外部補搜已拆成五類：催化劑、營收與基本面、籌碼資金、反證退燒、題材想像空間。題材想像空間必須標示為推論型資料，不得當成已驗證事實。

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

### AI 資料中心、入模審計與可信度

投研報告型指令現在會先經過共用 AI 資料中心，再建立送給 AI 的 prompt。適用範圍包含 `/research`、`/value_scan`、`/macro`、`/theme`、`/theme_flow`、`/theme_radar`、`/sector_strength` 等由 research center orchestrator 產出報告的指令。

共用流程如下：

1. 收集結構化資料、新聞、官方來源、論壇來源、搜尋來源與既有快取。
2. 建立共用證據包與資料缺口摘要。
3. 依來源可信度、日期、官方來源、風險反證與題材關聯，挑選本次 AI 實際入模資料。
4. 建立 `ai_prompt_context`，作為 AI 實際收到的規則化資料。
5. 建立 `ai_input_audit`，記錄哪些資料有入模、哪些資料未直接入模、原因是什麼。
6. 建立 `report_confidence`，用官方來源數、媒體來源數、風險反證、日期完整度與結構化資料完整度，產出報告可信度底稿。

注意事項：

- 本地量化底稿、價值重估底稿、資料完整度與可信度，只是 AI 判斷的底稿，不是最終投研結論。
- AI 必須依全部入模資料、來源可信度、反證與資料缺口重新判斷。
- 未直接入模的資料不會被刪除，仍保存在報告 JSON、來源 JSON 或本地快取。
- HTML 報告預設顯示主報告；「入模審計」、「資料品質」、「完整來源」、「本地底稿」、「技術附錄」、「QA」會放在其他分頁。
- 報告正文應使用自然繁體中文；內部英文欄位與 raw key 應放在附錄或審計分頁，並透過 `config/report_display_terms.json` 轉成人可讀名稱。

特殊流程說明：

- `/radar` 已有獨立 Radar Evidence Pack、AI Compact Pack 與三層證據包，邏輯與 AI 資料中心相近，目前維持既有雷達專用流程。
- `/news refresh` 與 `/topic_maintain` 屬於資料維護或分類型 AI 流程，不一定產出投研 HTML 報告；它們仍保留各自的 prompt 與審核流程。

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

Live audit 後的執行保護：

- 若 MiniMax MCP Search 已取得足量 Level 2/高品質來源，`/topic_maintain` 不會只因部分查詢被 `minimax_sensitive_query_blocked` 或少數 discovery task 較弱就再啟動 Gemini Search fallback，避免搜尋階段不必要地多等數分鐘。
- 題材細節補齊階段採小批次處理，並壓縮每批入模資料，只保留候選題材、重點 evidence、來源摘要、題材庫摘要與 company knowledge 摘要；完整資料仍保存在本地 structured data、prompt log 與 change pack 來源中，不會因壓縮而刪除正式資料。
- MiniMax 題材維護每個 AI stage 有時間上限；單一 stage 逾時會記錄 warning 或 fallback，不會讓整個 Telegram 任務長時間看起來卡住。
- 若某一批細節補齊逾時或失敗，系統會把該批候選題材轉成本地骨架 action，並由品質正規化補齊待補欄位，避免整批候選題材消失。

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
- `/news refresh` 使用分類導向搜尋 query，會分別補強台股與大盤、題材與族群輪動、AI / 半導體、個股利多利空、供應鏈與產業、政策 / 匯率 / 總經、台指期與盤前風險等方向，避免全部搜尋結果都集中在大盤新聞，並避免漏掉台指期夜盤、期貨跌停、盤前急跌等突發風險事件。
- `/news refresh` 完成時會輸出分類統計與持股新聞候選數，方便判斷是搜尋不足、過濾過多、分類集中，或只是新增內容重複。
- 新聞入庫與顯示前會排除報價頁、個股行情頁、股票明細頁、查詢頁、清單頁、首頁、行情排行頁、論壇頁、資料百科頁、匯率查詢頁、API/list 頁與泛國際新聞。例如 Yahoo 股價頁、Yahoo 漲幅排行、Yahoo 台股盤勢、Goodinfo 個股頁、HiStock 個股頁、nStock 個股頁、鉅亨個股基本面頁、StatementDog 財報頁與概念股頁、WantGoo 行事曆與殖利率排行、Fugle AI 個股頁、PChome 個股資料、Money-Link 個股新聞列表、鉅亨指數資金流向、Yahoo 法人進出頁、台銀匯率查詢頁、元大熱門新聞 API 頁、CMoney 論壇頁與工商時報行情頁不會當成新聞文章。
- `/news latest` 優先顯示最近 24 小時合格新聞；若新聞量不足，可補最近 48 小時內的合格新聞。系統不會用超過 48 小時、非文章頁或非台股財經新聞硬補；`2 days ago` 這類邊界文字會視為不符合 latest 顯示條件。
- `/news latest` 與 `/news 7d` 顯示前會再做一次分類正規化。報價頁、清單頁、影片頁、活動頁、社群轉貼頁、標題只有網域名稱的 grounding redirect、無明確台股財經關聯的英文泛新聞會被排除，不會因為曾經入庫就直接顯示。DIGITIMES、TWSE/TPEX 英文頁等英文台灣財經資料可保存在新聞庫供調研使用，但 Telegram 新聞推送預設不顯示。
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

- 10:00：每日自動執行 `/topic_maintain --model minimax`，使用 MiniMax M3 產生題材庫變更包；不會自動套用，仍需用 `/topic_review` 檢視後再 `/topic_confirm` 或 `/topic_reject`。
- 08:45、18:00：定時新聞整理與推播，預設使用 MiniMax M3 做新聞分類與整理。
- 20:30：交易日執行全部選股，等同 `/scan` 選項 7，完成後發送 Telegram 訊息。
- 21:30：交易日執行 Radar 推播，原 20:30 Radar 已移到 21:30；定時 Radar 預設啟用 MiniMax M3 短評。
- 報告與推播類定時任務使用序列佇列；若前一個定時任務尚未完成，下一個任務會排隊，等前一個完成後接續執行。
- 回補任務維持背景執行，不進入報告推播佇列，避免回補卡住選股、Radar、新聞、午報或持股推播。

## 資料回補健康度與快取狀態

- `/backfill` 完成後會寫入 `.cache/backfill/YYYY-MM-DD/complete.json` 與 `gaps.json`。
- `curated_scan_cache_ready` 只代表精選選股快取已建立，可供 `/value_scan 精選選股` 讀取。
- `scan_data_ready` 代表技術、月營收、財報/毛利率、法人籌碼等選股依賴資料達到門檻。
- `backfill_ready_for_scan` 只有在 `curated_scan_cache_ready` 與 `scan_data_ready` 都成立時才會是 `true`。
- 快取健康摘要會列出本次補齊數量、各類資料覆蓋率、缺資料前 20 檔、缺口報告路徑與資料來源額度。
- 回補會額外建立 priority pool 統計，包含持股、監控清單、近期掃描、近期投研與精選選股命中的股票。摘要會顯示優先池法人籌碼與 TDCC 覆蓋率，避免只看全候選池平均覆蓋率而不知道真正會用的股票是否補齊。
- 法人籌碼的全候選池覆蓋率可能受長尾股票與資料源限制偏低；`scan_data_ready` 會優先確認 priority pool 的法人籌碼覆蓋是否達標，避免不重要的缺口讓同一天反覆回補。
- 籌碼回補會聚焦 priority pool，不再每次對全候選池逐檔補抓；`gaps.json` 仍會列出全候選池缺口，方便追蹤但不阻塞選股快取可用性。
- 技術日線回補會先檢查本地快取是否已覆蓋目標日期，已足夠時跳過資料源抓取，避免每天重抓全市場日線。
- 今日資料可用性檢查若價量資料缺少明確日期，會改用少量日線樣本確認目標日期，避免誤判 `today_data_date_unconfirmed`。
- 投研結構化底稿有單檔逾時與總時間預算；未完成的核心股會留待下次回補或實際 `/research` 指令補齊，不會阻塞選股快取 marker。
- `backfill_core_research_limit` 會限制研究底稿 core pool 的最大檔數；即使持股、監控與近期命中很多，也不會突破設定上限。
- `gaps.json` 的 `health` 是全候選池健康度，`priority_health` 是 priority pool 健康度，可用來追蹤回補到底補到哪些真正重要的股票。

## News Display Rules

- `/news latest` selects explicit 24-hour news first. It only uses 48-hour fallback when the explicit 24-hour pool is too small, and blank publish-time items are demoted.
- Scheduled news pushes use the same ranking rules, but now allow a limited `created_at` fallback only for `news_origin=refresh` items from trusted L1/L2 preferred sources that pass non-article filtering. Explicit `published_at`, `article:published_time`, `datePublished`, and `<time datetime>` dates still rank ahead of fallback dates.
- Scheduled news refresh runs one optional lightweight refill before sending Telegram if the 24-hour qualified pool has fewer than 8 items or core categories are empty. The refill only searches missing/weak categories, caps sources/WebFetch/AI classification, runs at most once, and does not affect manual `/news latest`, research commands, topic maintenance, Feature Pack routing, or the normal full `/news refresh` path.
- WebFetch extracts publish dates from HTML metadata (`article:published_time`, `datePublished`, `dateModified`, `pubdate`, and `<time>` tags) and writes the normalized date back to the news item when available.
- If generic metadata and JSON-LD do not expose a publish date, WebFetch applies site-specific date parsers for common Taiwan finance sources (`money.udn.com`, `news.cnyes.com`, `m.cnyes.com`, `tw.stock.yahoo.com`, `ctee.com.tw`, `technews.tw`, `moneydj.com`, and `moneyweekly.com.tw`). These parsers only improve `published_at`; they do not change news classification, ranking, preferences, Feature Pack routing, or Telegram formatting.
- Search result dates are normalized before WebFetch. MiniMax/Tavily/Gemini result fields such as `date`, `published_date`, `time`, `datePublished`, plus snippets containing `7 hours ago`, `1 day ago`, `2026/06/15`, or `2026年6月15日`, are converted to `YYYY-MM-DD` when possible.
- `/news refresh` performs one local date backfill before saving refresh-origin items. It re-checks title, summary, full text, URL, and then the refresh `created_at`; this does not apply to manual pasted news or research-origin rows.
- News WebFetch prioritizes article-like URLs from mainstream finance sources such as `money.udn.com/money/story/`, `news.cnyes.com/news/id/`, `ctee.com.tw/news/`, `tw.stock.yahoo.com/news/`, `technews.tw`, `MoneyDJ`, `理財週刊`, and `CNA`; exchange query pages, English list pages, ETF/statistics pages, and announcement lists are skipped or demoted so fetch slots are not wasted.
- Scheduled news logs include diagnostics: raw 48-hour rows, pruned rows, explicit-date count, missing-date count, 24-hour candidates, 48-hour fallback candidates, final display total, and category distribution.
- Portfolio news is strict: a broad market or sector article is not moved into「庫存持股新聞」only because metadata contains a held symbol. It must mention the holding in the title, or be classified as clear company news.
- CMoney/Readmo-style lightweight investment-blog sources are kept in the news database as backup material, but Telegram display applies a stronger ranking penalty. Mainstream finance-media articles appear first, and CMoney/Readmo items are used mainly as category fillers when higher-quality sources are thin.
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

## AI 雙模型資料整理流程

投研報告型指令會先經過共用 AI 資料中心，再執行雙模型流程：

1. 低階資料整理模型固定使用 MiniMax M3。
2. MiniMax M3 只負責整理事實、事件、風險、反證、來源對照與資料缺口，不負責最終投資判斷。
3. 高階分析模型仍依 Telegram 選單或指令參數決定，例如 Gemini、DeepSeek 或 MiniMax。
4. 高階模型會收到原本的入模資料、入模審計、可信度底稿，以及 MiniMax M3 的資料整理底稿。
5. 高階模型必須重新判斷最終結論、評分、排序與風險，不得直接照抄低階模型底稿。
6. 若 MiniMax M3 額度不足、逾時或失敗，報告不會中斷，系統會保留失敗診斷並改用原本資料中心內容繼續產出。
7. HTML 報告會新增「資料整理底稿」分頁；主內容仍只放最終投研報告，技術資料與底稿放在分頁中。

低階整理現在有共用安全閘門，但目的不是節省 MiniMax M3 額度，而是避免 context 爆掉、逾時或格式失敗。低階模型會盡量完整整理資料，真正控制 token 的重點放在高階模型入模。

- MiniMax M3 prompt 會先估算字數與 token，CMD 會顯示 `prompt=... chars`、`est_tokens=...`、來源數與分段數。
- 單次低階 prompt 超過安全上限時，會優先依資料群組完整分段整理，再由本地合併 facts、events、risk_evidence、counter_evidence、missing_data 與 source_map。
- 低階分段會盡量保留每段完整資料；只有單段仍超過硬限制，或段數超過安全上限時，才做最低限度壓縮或截斷，且完整原始資料仍保留在本地 JSON。
- 任一分段失敗時，會先用精簡重試 prompt 重試一次；若仍失敗，系統會把該段記入 `failed_segment_index`，報告會保留診斷並由高階模型搭配本地資料中心判斷，不會靜默忽略。
- 低階快取 fingerprint 會排除 `generated_at`、`created_at`、`updated_at`、prompt path、diagnostics 等易變欄位，避免同一天重跑因時間戳不同而無法命中快取。
- 完整原始資料不會因低階分段被刪除；完整資料仍保存在報告 JSON、完整來源 JSON、本地快取與低階整理 artifact。

### `/value_scan` 搜尋與來源用途

`/value_scan` 的搜尋 query 會依候選股批次拆成官方公告與月營收、產品客戶與供應鏈、舊標籤與新標籤重估、法人籌碼與資金、反證與重估失敗風險等任務。搜尋整理 prompt 會要求每個 finding 標示用途：支持重估、支持反證、只作情緒或資料不足。只有新聞熱度、論壇討論、股價波動或 snippet 的資料不得當成強重估證據；缺少官方、財報、月營收、法說會、產品客戶或供應鏈資料時，必須寫入資料缺口。

`/value_scan 精選選股` 的候選池會先找同資料日期的精選選股交叉命中快取；若使用者未指定 `--date`，且今天或目標日沒有可用快取，會改用最近一筆已建立 `curated_scan_ready` 或 `backfill_ready_for_scan` marker 的精選快取，避免週末或當日資料尚未完成時重新觸發昂貴的精選選股。若使用者明確指定 `--date`，則維持嚴格日期語意，不自動改用其他日期。

`/value_scan` 外部搜尋採「重點候選股批次 + 候選池集合查詢」策略。候選股完整資料仍保留在本地結構化資料、入模包、報告 JSON 與 HTML 附錄；外部搜尋不再把每一檔候選股對每一類任務全部逐檔展開，以避免 MiniMax Search / Tavily / Gemini Search 查詢數暴增。深度模式會保留官方公告與月營收、產品客戶與供應鏈、舊標籤與新標籤重估、法人籌碼與資金確認、反證與重估失敗風險五類搜尋任務，但每類只用重點批次與候選池集合查詢補外部證據。

`/value_scan` 驗收可使用三層 smoke：

1. `python scripts/smoke_value_scan_discovery.py`：不連外，只驗證 discovery tasks、query log 與 prompt 欄位。
2. `python scripts/smoke_value_scan_report_local.py`：不連外，用本地 fixture 跑完整報告 artifacts。
3. `python scripts/smoke_value_scan_tavily_live.py`：會消耗少量 Tavily 額度，驗證外部搜尋來源與 provider diagnostics 進入報告 JSON。

### News / Topic / Radar 分流策略

非完整報告型 AI 功能也會盡量沿用同一個「低階整理、高階判讀」原則，但不會無限制增加高階模型呼叫：

| 功能 | MiniMax M3 低階工作 | 高階模型工作 | 成本控制 |
|---|---|---|---|
| `/news refresh` | 一般新聞分類、摘要、初步利多利空與標籤 | 只複核前 N 則重要新聞、重大來源與重大題材 | `NEWS_HIGH_TIER_CLASSIFY_LIMIT`，預設 12 |
| `/topic_maintain` | 整理候選題材、產品、公司、供應鏈證據與缺口 | 產生可審核變更包，重新去重、驗證來源、判斷是否寫入題材庫 | 分段 pipeline，小 JSON 輸出 |
| `/radar` | 對前 N 檔候選股做一次批次資料整理 | 只針對短名單產生短評、風險與觀察重點 | 批次整理，不做每檔雙模型完整報告 |

低階模型輸出只作為資料底稿；所有分類覆核、題材變更包、Radar 短評的最後判斷仍由高階模型或本地規則完成。若 MiniMax M3 無法使用，這些流程會回到既有單模型或本地 fallback，不會中斷任務。

補充成本控制：

- `/theme_radar`、`/theme_flow`、`/sector_strength` 的高階分段分析不再讓每段都攜帶全部來源；每段只帶與該段 payload 相關的來源，找不到對應來源 ID 時只帶少量代表來源。
- `/topic_maintain` 的低階整理不會把完整題材庫、完整公司題材 map 或完整供應鏈節點送給 MiniMax M3；只送本次變動、候選題材、候選公司、重點證據、資料缺口與題材庫摘要/樣本。
- `/news refresh` 的 AI 分類批次會在 CMD 顯示每批 prompt 字數、估算 token 與新聞筆數；高階模型只複核前 N 則重要新聞。

## 多模型分工 AI 工作流

所有投研報告型指令會先經過共用 AI 資料中心，再依資料量決定高階模型入模方式。

流程：

1. 本地資料中心整理完整資料、來源、反證、資料缺口與可信度。
2. MiniMax M3 只做低階資料整理，產出事實、事件、風險、反證、缺口與來源對照。
3. 系統會驗證低階資料包，並保存 JSON / Markdown 到 `logs/ai_low_model/`。
4. 系統先估算原始 prompt 字數。
5. 若資料量未超過門檻，高階模型沿用完整入模模式。
6. 若資料量過大，高階模型改用 `balanced` 或 `compact` 入模資料包。
7. 完整原始資料不會被刪除，仍保存在報告 JSON、完整來源 JSON、低階資料包與本地快取。
8. 高階模型必須重新判斷、重新評分，不得直接照抄 MiniMax M3 或本地量化底稿。

入模模式：

| 模式 | 觸發條件 | 說明 |
|---|---:|---|
| `full` | 原始 prompt 小於 180,000 字 | 高階模型可直接吃完整規則化資料 |
| `balanced` | 原始 prompt 約 180,000 字以上 | 高階模型主要吃證據包、可信度、反證、缺口與必要原文摘錄 |
| `compact` | 原始 prompt 約 320,000 字以上 | 進一步壓縮入模資料，但完整資料仍保留在本地與附錄 |

報告可追溯資訊：

- HTML「入模審計」與「技術附錄」會顯示高階模型入模模式。
- HTML「資料整理底稿」會顯示 MiniMax M3 的低階整理結果。
- HTML「資料整理底稿」會顯示低階模型分段失敗、重試狀態與失敗段紀錄。
- JSON metadata 會保存 `high_model_input_package`、`high_model_input_mode`、`low_model_validation`、`ai_workflow_policy`。
- Prompt log 仍保存在 `logs/ai_prompts/`，低階資料包保存在 `logs/ai_low_model/`。

這套流程的目標不是節省低階模型額度，而是在報告品質優先的前提下，讓低階模型完整整理資料；只有資料量真的過大時，才降低高階模型重複閱讀大量原始資料造成的時間與 token 浪費。
## AI 市場想像力規格

所有會產生 AI 分析的投研指令，包含 `/research`、`/value_scan`、`/macro`、`/theme`、`/theme_radar`、`/theme_flow`、`/sector_strength`、`/radar` AI 短評、`/news refresh` 分類與 `/topic_maintain` 題材維護，都必須在原本事實分析與評分架構中加入「嵌入式市場想像」。

核心要求：

1. 保留原本報告章節、量化底稿與評分架構。
2. 每個重要判斷盡量拆成已驗證事實、市場可能買單故事、爆發條件、待驗證訊號與失敗條件。
3. 從技術面、籌碼面、營收面、新聞面、產業面、趨勢面、題材面找蛛絲馬跡，推演可能的市場脈動與受惠路徑。
4. 想像力只能補強研究優先度、劇本與觀察指標，不得取代財務硬指標、題材軟指標、飆股基因、價值重估或最終買入評分。
5. `--source-only` 與 source-only 類型任務維持純資料整理，不載入市場想像規則。

共用規則位於 `prompt/rules/embedded_market_imagination_rules.md`，由 `research_center/prompt_registry.py` 對報告型 AI 指令注入。獨立 AI prompt 另在 `prompt/radar/radar_ai_comment.md`、`prompt/news/news_summary.md`、`prompt/topic/topic_maintain.md` 保留對應規範。
## AI 指令感知本地壓縮與入模審計

所有會呼叫 AI 的指令都會先經過共用高階模型入模封包。當原始資料過大而進入 `balanced` 或 `compact` 模式時，系統不再只使用機械式截斷，而是使用「指令感知本地壓縮」：

1. 核心資料必須以可分析摘要直接送入高階模型。
2. 非核心資料才使用通用壓縮與附錄保留。
3. 核心欄位不得出現 `<list truncated>` 或 `<dict truncated>`。
4. MiniMax M3 低階模型只負責整理、去重、分類、標記反證與資料缺口，不負責決定核心資料是否刪除。
5. 完整原始資料仍保留在報告 JSON、完整來源檔、HTML 附錄與本地快取。

各 AI 指令的核心資料保護範圍：

| 指令 | 高階模型必收核心資料 |
|---|---|
| `/research` | 股票基本資料、價量技術、法人籌碼、融資融券、營收、財報、本地量化底稿、題材脈絡、公司知識、風險與反證 |
| `/value_scan` | 候選股票、重估分數、重估理由、題材命中、財報與營收轉折、法人籌碼、價格位置、反證與資料缺口 |
| `/macro` | 市場狀態、波動率、產業資金流、恐懼貪婪、國際事件、利率匯率油價、風險與反證 |
| `/theme` | 題材定義、命中公司、供應鏈輪廓、題材脈絡、代表股、候選股、證據與反證 |
| `/theme_radar` | 題材排行、命中公司、題材脈絡、供應鏈輪廓、族群排行、子族群排行、代表股、候選股、新聞題材統計、風險與反證 |
| `/theme_flow` | 題材名稱、供應鏈分層、每層代表股、下一層候選股、市場驗證、新聞證據、資料缺口與反證 |
| `/sector_strength` | 產業排行、子族群排行、強勢樣本股、代表股、候選股、量價狀態、題材命中、風險與反證 |
| `/radar` | 雷達候選股、命中理由、量價訊號、題材訊號、籌碼訊號、新聞訊號、風險與反證 |
| `/news refresh` | 新聞標題、來源、時間、股票與題材命中、事件類型、分類依據、重要性與可能影響 |
| `/topic_maintain` | 題材庫變更包、候選題材、候選公司、供應鏈證據、新聞證據、反證、資料缺口與原題材庫摘要 |

`/radar` 的 AI 短評會額外把本地價量、營收財報與籌碼底稿標示為官方基礎來源：TWSE / TPEx 價量交易資訊、MOPS 營收財報、TWSE / TPEx / TDCC 法人與集保資料。這些來源代表本地快取的官方資料基礎，不等同於外部新聞；若個股缺少可驗證新聞或公司公告，報告仍需在風險與資料缺口中標示。

HTML 的「AI 入模審計」會保留未入模提示，但狀態會拆成四類：

| 狀態 | 說明 |
|---|---|
| 已直接入模 | 高階模型已收到可分析摘要 |
| 僅保留附錄 | 原始資料存在，但本次未直接送入高階模型 |
| 資料源不足 | 系統本來就沒有取得足夠資料 |
| 本指令不需要 | 此資料類型不是該指令的必要資料，例如單一 `/theme` 不需要全市場題材排行與族群排行 |
| 壓縮異常 | 核心資料被截斷標記取代，需要修正壓縮規則 |

來源日期會由共用來源正規化層處理。系統只接受來源欄位、網頁 metadata、標題、摘要或 URL 中可解析出的日期；抓不到日期時仍標示為「日期不可驗證」，不會用報告產生時間硬補。報告品質與入模審計會分別顯示「明確日期來源」、「推測日期來源」與「日期不可驗證」數量。

這套機制的目標是維持報告品質、精準度與可信度，同時避免高階模型反覆閱讀大量重複 raw data，讓 token 消耗、報告速度與穩定性取得較好的平衡。
來源標題與摘要會先經過共用文字清理，修正常見的 UTF-8/Latin1 亂碼，避免報告資料來源出現不可讀標題。來源日期正規化支援西元日期、民國日期、英文月份日期與網頁 metadata，但不會自行編造日期。

報告 metadata 的 `analysis_model` 會以實際完成回應的模型為準；若 Gemini Pro 因額度或錯誤 fallback 到 Flash，或分段分析服務回傳實際模型名稱，報告會記錄實際模型，避免 prompt log 與報告顯示不一致。

`/macro` QA 會把「VIX」、「波動率」、「市場風險」、「風險偏好」等同義章節視為波動風險內容，不會因章節標題沒有直接寫「波動」就誤判缺漏。

MiniMax M3 低階資料整理若遇到 429、quota、usage limit 等額度或速率限制，HTML 的「資料整理底稿」會用中文說明失敗原因，並標示系統已改由本地 AI 資料中心與高階模型繼續產出報告。

## AI 完整分段入模規則（2026-06 更新，以本節為準）

本節適用所有會呼叫 AI 的投研與維護指令：`/research`、`/value_scan`、`/macro`、`/theme`、`/theme_flow`、`/theme_radar`、`/sector_strength`、`/radar`、`/news refresh`、`/topic_maintain`。

核心目標是維持報告品質、精準度與可信度，同時讓 Token 消耗、產出速度與穩定性取得平衡。系統不再用語意壓縮規則判斷哪些核心資料可以刪除；改為「完整分段入模」：資料可以分段、去重、標記來源與整理格式，但不能因本地規則或低階模型判斷「不重要」而刪掉會影響結論的核心資料。

標準流程：

1. 原始資料進入共用資料層。
2. 本地 AI 資料中心只做機械式整理：去重、來源 ID、資料類型分類、分段、入模審計、可信度標記。
3. MiniMax M3 低階模型只做資料整理：事件合併、來源對齊、利多/利空/中性/矛盾/資料不足標記、資料缺口整理。
4. MiniMax M3 不得產出最終投資結論、不得做最終評分、不得給買賣建議、不得刪除核心資料。
5. 高階模型依使用者選單或定時任務預設模型執行最終分析，必須重新判斷資料、反證、可信度、資料缺口與評分。
6. 低階模型整理失敗時會重試；重試仍失敗時，保留失敗段紀錄與原始分段資料，不可靜默丟棄。
7. HTML 報告主分頁只放正式報告；低階模型底稿、完整來源、入模審計、資料可信度、本地量化底稿與技術附錄放在補充分頁。

高階模型會收到的核心資料包括：本地結構化摘要、完整分段資料包、MiniMax M3 整理底稿、重要反證與風險證據、來源索引、必要原文摘錄、本地量化底稿與資料缺口。若某類資料因技術限制未直接入模，HTML 入模審計仍會用中文明確顯示，不會隱藏。

MiniMax M3 低階整理採「選擇性整理」：只整理新聞、搜尋結果、論壇、公告摘要、來源 snippet、風險反證與資料缺口等文字證據；大型結構化表格、全市場排行、族群排行、完整個股清單、本地量化表與完整技術資料不再重複送給低階模型整理。這些被低階略過的結構化資料仍會留在本地完整分段資料包，並送給高階模型做最終分析；低階略過不代表高階沒收到。系統會在 metadata 記錄低階整理的文字證據數與略過的結構化區塊，方便檢查 MiniMax 額度消耗與入模邊界。

`balanced`、`compact` 等舊名稱只代表入模模式的歷史相容參數；目前規則以完整分段入模為準，不代表刪除核心資料或只保留摘要。

### 分段 AI 卡住防護

為避免 `/theme_radar`、`/theme_flow`、`/sector_strength` 等大型報告卡在單次高階模型呼叫，系統會在共用分段服務中執行以下防護：

1. 分段服務會優先使用共用高階入模資料包，拆成「本地核心資料包、證據與低階底稿、來源索引、本地量化與入模審計」等有意義的資料包段。
2. 只有在沒有高階入模資料包時，才退回舊的原始資料分段流程。
3. 單一資料包段 prompt 若超過硬上限，會自動再切成更小的完整分段。
4. 再切分段只改變送出批次，不刪除核心資料、不做語意捨棄。
5. `/theme_radar` 的本地核心資料包會用「股票主檔 + 題材/產業/子族群/強勢股關聯表」表示；同一檔股票只入模一次，其他區塊用代號參照，完整原始資料仍保留在 JSON / HTML 附錄。
6. 分段執行時，前段結果只會以「累積狀態表、處理清單、最近摘要」傳給下一段，不會把前面所有 Markdown 筆記全文反覆送入模型。
7. 每次高階模型呼叫會套用臨時 timeout；超時或失敗時記錄該段失敗原因。
8. 某一段失敗不會讓整份報告卡死；系統會保留該段本地資料與失敗紀錄，繼續處理後續分段。
9. CMD 會顯示分段進度、目前第幾段、prompt chars、估算 tokens、來源數、timeout 秒數與耗時。
10. 報告 metadata 會保留每段 `prompt_chars`、`timeout_seconds`、`elapsed_seconds`、`status` 與 `error`，方便事後檢查。
## 籌碼資料 TPEx OpenAPI 優先來源

- 籌碼日資料共用同一層 `chip_strategies.py` 抓取流程，`/scan`、全部執行、Radar 與 `/backfill` 不各自實作資料來源。
- 上市資料維持既有 TWSE T86 與 MI_QFIIS 流程。
- 上市法人買賣超若只剩少量缺口（預設 3 檔以內），會略過 TWSE T86 批次查詢，直接用 FinMind 單檔補資料。
- 上市外資持股比例若只剩少量缺口（預設 3 檔以內），會略過 MI_QFIIS 全分類掃描，直接用 FinMind 單檔補資料，避免為小缺口等待多個 `selectType` 查詢。
- 籌碼 60 日資料會排除週末與台灣市場休市日；例如勞動節這類平日休市日不會再硬抓 TWSE / TPEx / FinMind。
- 休市日來源使用 TWSE `holidaySchedule`，支援民國日期如 `1150501`，並排除「開始交易日 / 最後交易日」這類非休市項目。
- 上櫃法人買賣超優先嘗試 TPEx OpenAPI `tpex_3insti_daily_trading`。
- 上櫃外資持股比例優先嘗試 TPEx OpenAPI `tpex_3insti_qfii`。
- TPEx OpenAPI 資料列必須能解析出日期，且日期等於目標交易日；若不符合，視為不可用，避免用最新日資料誤補歷史日。
- TPEx OpenAPI 失敗或補不到時，保留舊版 TPEx `qfiiStat` / `sitcStat` fallback。
- FinMind 仍是最後少量缺口備援，不作為主要批次資料來源，避免大量單檔補資料耗盡額度。
- CMD 進度會顯示 TPEx OpenAPI 是否成功、補到幾檔、是否進入舊版 fallback 或保留估算流程。
- 官方來源分工：TWSE 用於上市股票籌碼，TPEx 用於上櫃股票籌碼，TAIFEX 用於期貨、選擇權與大盤衍生性商品資料；股票籌碼選股不把 TAIFEX 混入 TPEx OpenAPI 流程。
- TPEx OpenAPI 實際日期可能為民國無斜線格式，例如 `1150605`，系統會解析為 `2026-06-05`。
- TPEx OpenAPI 法人買賣超會讀取 `SecuritiesCompanyCode`、`ForeignInvestorsInclude MainlandAreaInvestors-Difference`、`Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference`、`SecuritiesInvestmentTrustCompanies-Difference`。
- TPEx OpenAPI 外資持股比例會讀取 `PercentageOfSharesOC/FMIHeld`，並支援 `87.81%` 這類百分比字串。
- TPEx OpenAPI 若確認只提供最新交易日，系統會記住該日期；回補較早歷史日時會直接略過 OpenAPI，避免每天重複打最新日 API。
- 舊版 TPEx fallback 會同時查買超與賣超表，避免只查買超表時漏掉賣超股票。

## 2026-06 AI 入模防護更新

本節適用所有走共用 AI 工作流或分段分析服務的 AI 指令，包含 `/research`、`/value_scan`、`/macro`、`/theme`、`/theme_radar`、`/theme_flow`、`/sector_strength`、`/radar`、`/news refresh`、`/topic_maintain`。

### 目標

1. 報告品質、精準度、可信度與市場想像力優先。
2. 完整原始資料不刪除，仍保存於報告 JSON、完整來源 JSON、HTML 附錄與本地快取。
3. 高階模型不再因大型題材雷達被切成數十或上百段而長時間卡住。
4. MiniMax M3 低階整理若分段過多，會安全略過並保存診斷，不會無限制消耗額度。
5. CMD 會顯示 prompt 字數、估算 token、來源數、分段數與略過原因，方便即時判斷任務是否過重。

### 高階模型分段規則

- 高階模型優先使用共用「保真核心資料包」。
- 保真核心資料包會拆成固定語意段：本地核心資料包、證據與低階底稿、來源索引、本地量化與入模審計。
- 這些保真核心資料包不再被自動切成幾十段小 prompt。
- 若舊式分段預估超過 12 段，系統會改用保真核心包整合模式。
- 這個流程只控制送給高階模型的批次，不刪除原始資料。

### 題材雷達保真去重規則

- `/theme_radar` 的高階入模資料會保留題材排行、族群排行、子族群排行、強勢股、題材命中公司、題材脈絡、供應鏈輪廓、新聞統計、風險與反證。
- 同一檔股票只在 `stock_index` 建立一次股票主檔；題材排行、族群排行、子族群排行與題材擴散只引用股票代號，不再重複塞入整份股票資料。
- 同一批來源只保留輕量來源索引與必要原文摘錄；完整來源、snippet、搜尋診斷與完整原始資料仍保存在報告 JSON、完整來源檔與 HTML 附錄。
- 大型 `ai_data_center`、`ai_prompt_context` 與完整 evidence pack 不再在高階 prompt 內重複出現多份；高階模型會收到摘要、關聯表、反證、缺口與必要原文。
- 這是「去重與引用正規化」，不是壓縮刪除；若某類核心資料真的沒有直接入模，HTML 入模審計仍會顯示。

### MiniMax M3 低階整理規則

- MiniMax M3 只做資料整理，不做最終投資結論、最終評分或買賣建議。
- 單次低階 prompt 過大時會先分段整理。
- 若低階整理分段數超過 48 段，系統會略過低階整理，保存 `skipped_low_model_digest` 診斷與 artifact。
- 低階整理被略過時，高階模型仍會使用本地保真核心資料包、完整來源索引、反證與風險資料進行最終分析。
- 略過低階整理不代表資料被刪除，也不代表高階模型沒有資料可分析。

### 驗證重點

- `logs/ai_prompts/` 可檢查高階模型實際收到的 prompt。
- `logs/ai_low_model/` 可檢查 MiniMax M3 整理底稿、略過原因或失敗分段。
- 報告 JSON / `.sources.json` 仍保留完整原始資料與完整來源。
- HTML 入模審計若顯示某類資料未直接入模，代表該資料未送給高階模型，不會被隱藏。

### 2026-06-05 稽核補強

- `/sector_strength` 也套用和 `/theme_radar` 相同的「股票主檔 + 關聯代號」格式：同一檔股票只在 `stock_index` 出現一次，產業、子族群、強勢股與市場異動只引用股票代號，降低高階 prompt 重複資料。
- `unified_evidence_pack` 在各指令的 command payload 中只放證據摘要、來源 ID、數量與缺口；完整證據仍保留在 report JSON、來源檔與 HTML 附錄，避免同一批證據在高階 prompt 內重複出現多份。
- `tools/ai_command_live_audit.py` 每次執行會建立含 PID 的唯一資料夾，避免多個稽核同秒執行時互相覆蓋。
- live audit 摘要表的「截斷」欄位以高階模型實際 `prompt.md` 為準；中間資料包若有舊相容欄位，不會誤判成高階 prompt 已截斷。

### 2026-06-06 入模品質閘門

- 報告型 AI 指令的 `high_model_input_package` 會附上 `input_quality_gate`，檢查來源數、候選股數、prompt 規模與證據密度。
- 若來源數低於建議門檻，狀態會標為 `warning`；高階模型仍可分析，但必須降低確信度、列出資料缺口，不得用單一低品質來源支撐高分或強結論。
- `/value_scan` 會額外建立 `candidate_source_coverage`，逐檔標示外部搜尋來源數、本地事件數、財務底稿與公司知識是否存在。若某些候選股沒有可對應的外部來源，會提示高階模型降低該檔確信度並列為待補證據。
- `tools/ai_command_live_audit.py` 的 `/radar` 稽核若最近快取為空，會先尋找最近一筆非空 Radar 快取作為正式 AI 短評流程代表；若仍找不到非空快取，才標為 `warning`，避免把候選股 0 的情境誤判成正式 Radar AI 流程成功。
- live audit 對報告型指令會額外輸出 `raw_core_snapshot.json` 與 `raw_vs_high_model_input.json`。前者保存稽核用 raw 核心資料快照，後者對照 raw 核心資料與高階模型入模包，方便確認資料是「去重/索引化」而不是被偷刪。
- `/radar` AI 短評 prompt 不再輸出 `<dict truncated>` / `<list truncated>` 等內部英文標記；深層欄位或長清單會改成中文說明，並註明完整細項仍保留於本地證據包與快取。
- live audit 現在會在每個指令輸出資料夾產生 `codex_high_model_review.md` / `.json`，由本地規則模擬 Codex 代高階模型檢核：資料是否足夠、來源是否可信、核心資料是否入模、是否可產出正式報告、還缺哪些資料。
- live audit 也會在每個指令輸出資料夾產生 `codex_formal_output.md` / `.json`，依同一份完整資料、來源清單與高階入模包，由 Codex 代高階模型產出正式報告或訊息草稿。此檔用來驗證「資料是否足以產出報告」與「正式輸出是否具備核心結論、可信度、現實推演、主要來源與流程改善建議」。
- 每次 live audit 批次也會輸出 `codex_high_model_quality_audit.md`，彙整所有 AI 指令的 Codex 高階模型替代判讀，作為後續補資料與流程調整依據。
- Codex 高階模型替代判讀不只檢查流程是否跑完，也會逐指令檢查資料足夠性、來源可信度、反證完整度、prompt 是否能引導可信推論、是否保留基於現實的想像力。
- `/news refresh` 會額外檢查新聞分類品質、利多/利空/中性證據、行情頁或工具頁是否被誤當新聞、重大來源是否不足。
- `/topic_maintain` 會額外檢查題材證據、公司與題材連結、供應鏈節點缺口、短期新聞是否污染長期題材庫。
- 報告型指令會額外檢查 `raw_core_snapshot.json` 與 `raw_vs_high_model_input.json`，確認完整原始資料是否有對應入模摘要；若核心資料未入模，必須在 review 中列為補強項目。
- `/value_scan` 高階入模改用候選股主檔摘要 + `ai_candidate_evidence_summary`；完整 `ai_candidates` raw、完整 `ai_candidate_evidence_pack` 與 `chip_backup_data` 仍保留在 JSON 附錄，但不再於高階 prompt 逐檔重複展開。
- `/radar` live audit 會把候選股輕量研究包的來源彙整到 `sources.json` 與高階入模 `source_index`；若沒有外部來源，會補一筆 `Radar 本地候選股快取與選股證據包` 作為本地來源，並在 Codex 判讀中標示它不是外部新聞或官方來源。
- live audit 的 `summary.md` 會以 UTF-8 BOM 寫出，降低 Windows 工具讀取中文時出現亂碼的機率。
## 2026-06-05 `/macro` 宏觀硬數據護欄

`/macro` 現在會在送入 AI 前建立 `macro_data_guard`，用來約束宏觀報告中的硬數字引用。

- 本地會先建立可驗證數據底稿，包含台股/櫃買指數、全球主要指數、匯率、利率 proxy、商品 proxy、VIX、三大法人現貨資金流與台指期法人部位。
- 指數底稿會保留最新收盤、單日漲跌點數、單日漲跌幅、五日漲跌點數、五日漲跌幅、二十日漲跌幅與二十日實現波動率。
- 系統會偵測異常數字，例如台股單日漲跌超過 2,000 點、五日漲跌超過 5,000 點、或單日漲跌幅過大。
- 被標記為異常的數字不得直接成為主結論，只能寫入「資料異常」或「待複核」。
- 若 VIX、法人買賣超、期貨口數等硬數據缺漏，AI 不得自行估算，必須標示資料不足。
- CMD 會顯示硬數據護欄完成狀態，例如可驗證數字、異常警示與資料缺口數量。
- 報告 JSON 會保存 `structured_data.macro_data_guard`，方便後續檢查 AI 是否引用了被攔截或不可驗證的數字。

### 2026-06-10 效能與入模品質補強

- `/macro` 類股流動性 proxy 改為同日快取優先。若 `.cache/macro_indicators/industry_flow_YYYY-MM-DD.json` 已存在，會直接讀取，不再重跑全市場價量載入。若載入逾時，會改用最近快取；沒有快取時才降級為產業分布簡化 proxy。CMD 會顯示快取命中、載入檔數、耗時與是否降級。
- `/theme_radar` 與 `/sector_strength` 延續「股票主檔 `stock_index` + 關聯代號」入模格式。題材排行、產業排行、子族群排行與強勢股清單只引用股票代號；完整明細仍保留在 JSON / HTML 附錄。
- `/value_scan` 高階入模會提供 `ai_candidate_evidence_summary`，逐檔列出重估理由、支持證據、反證 / 失敗條件、來源 ID、資料缺口與本地評分摘要。完整 `ai_candidate_evidence_pack` 仍保留在附錄。
- `/news refresh` prompt 會要求每則新聞標示利多 / 利空 / 中性、影響公司、影響產業 / 題材、可信度、反證、資料不足與頁面類型，避免報價頁、排行榜、工具頁或論壇轉貼被當成高權重新聞。
- `/topic_maintain` prompt 會要求供應鏈節點補齊公司角色、上中下游位置、產品、客戶 / 應用、營收曝險證據、來源 ID、反證與資料缺口，並區分長期題材、短期新聞與蹭題材。
- 21:30 Radar 定時推播會先計算 Telegram 文字長度並自動拆分多段。任一分段失敗會向外拋出錯誤，避免 CMD 出現「推播失敗但定時任務完成」的誤報。
## AI 指令最佳化覆蓋度

所有會呼叫 AI 的功能都會以同一套 `ai_workflow_coverage_v1` 檢查最佳化程度。共同檢查項目包含：本地資料整理包、MiniMax M3 低階整理底稿、高階模型入模包、去重或索引化入模、來源索引、入模審計、HTML 分頁與 prompt/token 診斷。

報告型指令（`/research`、`/value_scan`、`/macro`、`/theme`、`/theme_radar`、`/theme_flow`、`/sector_strength`）會把覆蓋度寫入報告 JSON metadata，並在 HTML 的「入模審計」分頁顯示。獨立 AI 流程（`/radar`、`/news refresh`、`/topic_maintain`）也會在各自的執行 metadata 或 change pack extra 中寫入同一 schema。這讓不同資料型態的指令可以用同一把尺檢查：差異只能來自任務性質，不應來自優化程度不足。

覆蓋度狀態判讀：

- `aligned`：該指令已具備所有適用的共用最佳化能力。
- `partial`：仍有適用能力缺漏，需優先補齊。
- `not_applicable`：該能力因指令性質不適用，例如 `/news refresh`、`/topic_maintain` 是資料維護流程，不產出投研 HTML 報告，因此 `html_sections` 會列為不適用，而不是缺漏。

`tools/ai_command_live_audit.py` 會在 `summary.md` 顯示每個 AI 指令的覆蓋度、待補項目、不適用項目與截斷狀態。目標是所有 AI 指令至少達到 `aligned`；若是 `partial`，代表該指令尚未升級到同一套最佳化程度。
## 實際報告覆蓋度檢查

除了離線檢查所有 AI 指令是否接上共用工作流，也可以檢查已產出的報告 JSON 是否真的寫入 `ai_workflow_coverage`。

執行：

```bash
python tools/ai_report_coverage_check.py --limit 50
```

輸出位置：

```text
logs/ai_report_coverage_check/summary.md
```

判讀：

- `aligned`：該報告已寫入完整 AI 工作流覆蓋資訊。
- `partial`：該報告仍缺少部分能力，需查看「缺少能力」欄位。
- `missing`：該報告看起來是正式報告 JSON，但沒有 `ai_workflow_coverage`；通常代表舊報告，或新流程漏寫 metadata。
- `不適用項目`：例如 `news refresh`、`topic_maintain` 不是正式 HTML 報告型流程時，`html_sections` 可標記為不適用。

若要讓檢查在發現 `partial` 時直接失敗：

```bash
python tools/ai_report_coverage_check.py --fail-on-partial
```

若只想列出已經帶有覆蓋度 metadata 的報告：

```bash
python tools/ai_report_coverage_check.py --coverage-only
```

## 2026-06-12 MiniMax M3 真實驗收補強

本次以 `tools/ai_command_real_m3_validation.py` 實際跑 MiniMax M3 驗收，覆蓋 `/research`、`/value_scan`、`/macro`、`/theme`、`/theme_flow`、`/theme_radar`、`/sector_strength`、`/radar`、`/news refresh`、`/topic_maintain`。

- `/theme_radar` 修正分段最終整合：高階最終整合 prompt 改用 bounded summary，避免把大型排行與股票巢狀明細再次展開。實測 final prompt 約 18 萬字元，完整資料仍保留在 JSON / HTML 附錄與來源檔。
- `/sector_strength` 補跑真實 M3 驗收後，MiniMax M3 低階整理成功，高階模型 4 段分析與最終整合成功；最終整合 prompt 約 6 萬字元。
- MiniMax M3 單段低階整理若 JSON 解析失敗，現在會自動使用 `prompt/workflow/low_model_digest_retry.md` 精簡重試一次；重試仍失敗才標記 failed，並由本地資料中心與高階模型繼續。
- `/news refresh` 的 Telegram 摘要會顯示資料狀態、分類分布與前 5 則重點新聞，不再只回覆新增 / 略過數量。
- 報告顯示正規化補強 `strong stocks`、`stock codes`、`local scoring`、`scores`、`buy rating`、`direct` 等內部欄位詞，輸出前會轉成繁體中文可讀文字。
- 驗收器的 `/macro` 主題必要詞改為同義群判斷，避免宏觀報告因沒有出現固定字詞「總經」而被誤判失敗。

## AI 報告推演骨架

主要 AI 報告與 `/value_scan` 本地 fallback 報告會保留固定推演骨架，避免報告只變成公開資訊整理：

1. 市場正在交易什麼故事。
2. 早期蛛絲馬跡。
3. 下一波可能發酵的催化劑。
4. 如果要大漲，還缺什麼訊號。
5. 反向驗證與失敗條件。
6. 想像力結論。

這些段落嵌入原本評分與報告架構，不取代財務硬指標、題材軟指標、飆股基因、價值重估或最終研究優先度。市場故事可以提出假說，但缺少官方、財務、產品、客戶、供應鏈、籌碼或可靠來源時，必須降級為待驗證假說。

驗收可使用：

```bash
python scripts/smoke_value_scan_report_local.py
python tools/ai_report_coverage_check.py --root reports/_smoke_value_scan --limit 5 --coverage-only --out logs/ai_report_coverage_check/smoke_summary.md
```
## 2026-06-19 AI 投研 live 驗收與 HTML 必備資料檢查

- HTML 報告已新增「必備資料檢查」分頁，會顯示必備項目數、已覆蓋數、缺口數、覆蓋率、待補資料、補搜任務與補搜工具診斷。
- 報告 JSON 的 `structured_data.required_data_gap_summary`、`required_data_gap_backfill_tasks`、`required_data_gap_remaining_tasks`、`required_gap_minimax_discovery`、`required_gap_tavily_discovery` 會保留到報告快照，供 HTML 分頁讀取。
- live 驗收可用 `tools/ai_command_live_audit.py` 跑代表性指令，確認 MiniMax MCP Search、Tavily Search、Gemini Search fallback、WebFetch 與 Required data gap check 是否實際執行。
- 若 live 驗收中 `Required data gap check: complete`，代表該指令的必備資料已覆蓋；若仍有缺口，HTML 的「必備資料檢查」分頁會列出待補資料與建議 query。

## 2026-06-19 全 AI 指令 MiniMax M3 真實健檢補強

- 已使用 `tools/ai_command_real_m3_validation.py` 以 MiniMax M3 真實執行 10 個 AI 入口：`/research`、`/value_scan`、`/macro`、`/theme`、`/theme_flow`、`/theme_radar`、`/sector_strength`、`/radar`、`/news refresh`、`/topic_maintain`。
- 健檢工具會保存每個指令的 prompt、來源數、輸出、品質檢查與執行進度；輸出檢查前會套用 `report_display_normalizer`，避免健檢摘要殘留 `source_id`、`unified_evidence_pack` 等內部欄位名。
- `/topic_maintain` 的 AI JSON 解析器已加入局部救援：若 MiniMax M3 回傳的候選題材 JSON 有單筆壞掉，系統會保留已可解析候選題材，避免整批變更包變成 0 筆。
- 實測 `/topic_maintain --model minimax` 已產生候選 22 筆、可審核變更 8 筆；仍需使用 `/topic_review` 檢視，再用 `/topic_confirm` 或 `/topic_reject` 處理。
- `prompt/base/base.md` 已補強主報告可讀性規則：不得原樣輸出 JSON key、snake_case、內部封包名稱、除錯欄位或模型內部參數名；若需要引用來源，使用 `[S001]` 這類來源編號。
