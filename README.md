# Telegram 台股策略機器人

## Telegram 指令入口更新

- `/start` 只顯示不帶參數即可執行的指令，並依照 `/help` 相同分類整理，方便快速掃描。
- `/help` 是新的完整指令說明入口；`/ai_help` 保留為相容舊用法的別名。
- `/research` 不帶參數時會改為互動式流程：先提示輸入股票代號或名稱，再選日期、研究模式與模型。
- `/theme` 不帶參數時會提示輸入題材或產業，送出後直接執行一般題材研究。
- `/theme_flow` 不帶參數時會提示輸入題材或產業，送出後直接執行題材擴散路徑。

主要分類包含：選股與雷達、個股與價值分析、市場與新聞、題材與族群、題材庫維護、持股與監控、資料回補與匯出、報告與系統。

## 題材雷達分類修正

- 記憶體題材不再只靠「半導體業」產業別命中，需具備 DRAM、NAND、NOR、HBM、SSD、記憶體模組、記憶體控制晶片或儲存控制晶片等產品/證據。
- `/theme_radar` 的 `representative_stocks` 只放 verified / inferred / direct_map 類型；`candidate_stocks` 僅能視為待驗證候選股。
- `keyword_or_industry` 命中的股票不得在報告中被寫成已驗證代表股。
- 題材擴散 layer 也會分開 `representative_stocks` 與 `candidate_stocks`；若只有 candidate，報告必須寫「待驗證候選股」或「價格強勢候選」，不得寫「candidate 狀態的代表股」。
- 報告 prompt 已加入命名硬規則：已驗證代表股、推論型代表股、待驗證候選股、疑似蹭題材四種分類要分開顯示。
- `/sector_strength` 也套用相同命名規則：`sector_strong_samples` 只代表類股內價格/量能強勢樣本，不得稱為題材代表股；只有 verified / inferred 題材關聯可列入 `representative_stocks`。
- 已建立待審核修正包 `change_20260523_memory_misclassification_fix`，用於修正偉詮電、通嘉、揚智被誤列為記憶體題材代表股的問題；請用 `/topic_review change_20260523_memory_misclassification_fix` 檢查後，再決定是否 `/topic_confirm change_20260523_memory_misclassification_fix`。

這是一個以 Python 開發的台股 Telegram Bot，現有程式包含三條主要功能線：

1. 監控清單技術訊號
   針對 config.json 的 monitor_stocks 執行 21MA 突破與 105MA 突破；MACD 翻紅後回測突破邏輯保留但目前未啟用。
2. 全市場量化選股
  掃描全台上市櫃股票，支援財報營收選股與法人籌碼選股兩大類報告。
3. 個人庫存籌碼管理
  使用 portfolio.json 維護個人持股，並在每日收盤後自動推播三大法人買賣超。
4. 個股資料匯出
   將單一股票的價量、法人、融資融券、月營收、季財報彙整成 Excel。
5. 市場收盤摘要推播
  提供晨間美股與台指期夜盤，以及午間台股現貨與台指期日盤的自動推播與手動查詢。

本專案同時保留多個回測腳本，可單獨在命令列執行，用來驗證不同版本的策略與組合表現。

---

## 功能總覽

- Telegram Bot 指令管理
- monitor_stocks 名單管理
- portfolio.json 個人庫存管理
- 官方上市櫃股名與代碼同步
- 監控清單技術掃描
- 庫存三大法人定時推播
- 晨間美股與夜盤摘要推播
- 午間台股收盤摘要推播
- 全市場選股掃描
- 財報 + 籌碼複合選股選單
- 個股資料匯出 Excel
- AI 投研資料中心：個股研究、宏觀市場、題材研究、價值重估掃描
- Markdown / HTML / JSON 研究報告輸出與 SQLite metadata
- 本機 FastAPI 供龍蝦或其他 Agent 調用
- 多版本回測腳本
- Windows 一鍵啟動腳本
- 本地量化底稿與 AI 最終投研評分分離機制
- 價值重估底稿（local_rerating_snapshot）：本地計算共享於 /research --deep 與 /value_scan
- Prompt 規則模組化：7 支獨立規則檔（量化、價值重估、籌碼、技術、來源可信度、風險反證、歷史日期）

---

## 專案結構

### 核心程式

- main.py
  Telegram Bot 主控中心，負責註冊指令、定時任務、啟動後初始掃描與訊息發送；選股、監控與資料抓取邏輯交由各服務模組處理。
- monitor_service.py
  監控清單服務模組，負責 monitor_stocks 正規化、加入/移除監控、官方股名補值、即時價格/日 K 備援，以及 21MA、MACD、105MA 監控訊號判斷。
- portfolio_manager.py
  個人庫存模組，負責 portfolio.json 讀寫、股票名稱與代號解析、TWSE/TPEx 三大法人資料抓取與訊息排版。
- stock_scanner.py
  全市場選股引擎，負責上市櫃名單同步、月營收抓取、價量快取、毛利率計算與分組分級。
- technical_scanner.py
  全市場技術面選股引擎，負責硬篩、日 K 快取、MA / MACD / KD 指標計算與交叉訊號判斷；`/scan` 技術面選股輸出包含原始技術訊號與四大策略（A 多頭延續回檔突破、B 強勢紅柱回測突破、C 低檔背離反轉突破、D 強勢股急跌收復）。
- chip_strategies.py
  籌碼策略模組，負責四套法人/大戶策略、共同硬篩、法人日序列快取回補、資料來源退避、TDCC 週資料快取與報告格式化。
- data_fetcher.py
  個股資料抓取層，整合 TWSE、TPEx、MOPS 與 Yahoo Finance，供 /export 使用。
- export_service.py
  匯出 Excel 的組裝層，生成 Price_History、Monthly_Revenue、Quarterly_Financials、Strategy_Summary 四個工作表。
- market_summary.py
  市場摘要模組，負責抓取美股四大指數、台股上市櫃指數與台指期近月日夜盤收盤，並格式化為 Telegram 推播文字。
- research_center/
  AI 投研資料中心模組，包含指令解析、結構化資料整合、Gemini Search grounding、來源分級、報告產生、SQLite metadata、Telegram handler 與 FastAPI app。

### 測試與工具腳本

- test.py
  命令列驗證 /export 結果，可預覽各工作表欄位與列數，也可另存 Excel。
- backtest_v1.py
  單一策略回測，支援策略 A 與 B。
- backtest_v2.py
  在 v1 基礎上加入策略 C，含停損後鎖定機制。
- backtest_v3.py
  支援多策略組合回測與股息收益計算。
- backtest_v4.py
  長期版本的組合回測，預設從 2018 年開始。
- multitasking.py
  簡易 thread wrapper，目前只提供最基本的 task decorator。

### 設定與輔助檔

- config.json
  Telegram token、chat_id 與 monitor_stocks 監控清單。
- portfolio.json
  個人庫存清單，資料格式為 Dictionary，Key 為股票代號、Value 為股票名稱。
- requirements.txt
  Python 相依套件。
- config/research_center.json
  AI 投研中心公開設定，例如 Gemini 模型、報告路徑、SQLite 路徑與本機 API port。
- config/secrets.json
  Gemini API Key 等敏感設定；此檔已加入 .gitignore，不應提交。
- archive/legacy/config/theme_supply_chain.json
  舊版題材供應鏈參考檔，已封存到 archive/legacy；正式題材庫主要讀取 theme_profiles.json、company_theme_map.json、supply_chain_nodes.json。
- stock_list.json
  上市櫃股票清單快取，由 stock_scanner.py 自動更新。
- 啟動機器人.bat
  Windows 命令列啟動腳本。
- 隱藏啟動.vbs
  背景啟動批次檔的 VBS 包裝腳本。

### 執行時快取

- .cache/monthly_revenue
  每月營收原始資料快取。
- .cache/price_metrics.json
  全市場價格與 20 日均量快取。
- .cache/gross_margin.json
  季毛利率快取。
- .cache/chip_daily
  每日外資 / 投信買賣超與外資持股比例快取，供策略一到三重複使用；缺資料時才補抓官方或 FinMind。
- .cache/technical_daily
  技術面選股日 K 快取；執行時優先讀本機快取，缺資料才依序改抓 Yahoo Finance 與 Fugle。
- .cache/tdcc
  TDCC 每週股權分散快照快取，供策略四累積最近 8 週資料。
- .cache/chip_strategy_state.json
  舊版籌碼週報發送狀態檔；目前籌碼選股已改為手動 /scan 與背景快取回補，不再用於定時推播。
- reports/
  AI 投研中心輸出的 Markdown、HTML、JSON 與 sources.json 報告檔；預設不提交 Git。
- database/stock_research.db
  AI 投研中心 SQLite metadata，記錄報告路徑、摘要、來源與 fallback 狀態；預設不提交 Git。

---

## Telegram 指令

### 策略監控指令

- /start
  顯示可用指令。
- /list_m
  查看目前 monitor_stocks 清單。
- /add_m 代碼 名稱
  將股票加入 monitor_stocks。若未提供名稱，會自動從官方股名資料補值。
- /del_m 代碼
  從 monitor_stocks 移除股票。
- /check
  對 monitor_stocks 執行技術面掃描。
- /stop
  停止目前聊天室正在執行中的耗時任務，例如 /check、/scan 選股、/export、/stock_chart、/tmf_chart、/morning、/noon、/backfill。若定時回補正在執行，也會一併停止。

### 個人庫存指令

- /in 代碼或名稱
  將股票加入 portfolio.json。可輸入代號或中文名稱，例如 /in 2330、/in 台積電。
- /out 代碼或名稱
  從 portfolio.json 移除股票。可輸入代號或中文名稱，例如 /out 2330、/out 台積電。
- /my
  查看目前個人庫存清單。

### 其他指令

- /scan
  顯示互動式策略選單，可選擇財報營收選股、四套籌碼策略、技術面選股或全部執行。
- /scan 日期
  以指定日期執行選股選單，日期格式支援 `YYYY-MM-DD`、`YYYY/MM/DD`、`YYYYMMDD` 或 `M/D`；未輸入日期時預設使用今天。
- /export 代碼
  匯出單一股票的 Excel 資料包。
- /morning
  查詢晨間市場速報，內容含美股四大指數與台指期近月夜盤收盤。
- /noon
  查詢台股收盤總結，內容含加權指數、櫃買指數與台指期近月日盤收盤。
- /tw_market
  /noon 的同義指令。
- /stock_chart 代碼 開始日期 結束日期 頻率
  匯出單一台股個股的互動式 HTML 技術分析圖表。頻率支援 1d、1m、5m、15m、60m，預設為 1d。
- /tmf_chart 開始日期 結束日期 盤別 頻率
  匯出微台指 TMF 的互動式 HTML 技術分析圖表。盤別支援日盤、夜盤、全日盤，頻率支援 1m、5m、15m、60m，預設為全日盤 1m。

### AI 投研指令

- /ai_help
  顯示 AI 投研中心指令說明。
- /research 代號或名稱
  產出單一個股研究報告。支援 `--source-only`、`--score`、`--deep`、`--date YYYY-MM-DD`、`--model gemini|deepseek|minimax`。
  - 會自動注入 `topic_context`（題材庫背景參考），但 AI 必須根據當次資料重新驗證，不得照抄題材庫結論。
  - `--deep` 與 `--score` 會共用價值重估底稿服務補 `local_rerating_snapshot`；即使使用投研結構化快取，也會嘗試補齊，若底稿服務失敗則寫入資料缺口提醒，不中斷報告。
  - 模型選項：Gemini（預設）、DeepSeek V4 Pro（OpenCode Go）、MiniMax M2.7
- /macro [市場] [主題]
  產出宏觀市場研究報告。支援 `--source-only`、`--brief`、`--deep`、`--date YYYY-MM-DD`、`--model gemini|deepseek|minimax`。
- /theme 題材
  產出題材研究報告。支援 `--source-only`、`--deep`、`--date YYYY-MM-DD`、`--top N`、`--model gemini|deepseek|minimax`。
  - 會自動注入 `topic_context`（相近題材、既有關鍵字、供應鏈、風險與資料缺口），但 AI 必須根據當次搜尋與資料重新驗證，不得直接沿用題材庫結論。
  - Telegram 輸入 `/theme` 會先要求輸入題材或產業，送出後依序選擇分析模式、資料日期與 AI 模型；輸入 `/theme AI電源` 會從分析模式選單開始。若已帶 `--date`、`--model`、`--deep` 等參數，則維持原本邏輯直接執行。
- /value_scan [候選池]
  產出價值重估掃描報告。支援 `--source-only`、`--deep`、`--date YYYY-MM-DD`、`--top N`、`--model gemini|deepseek|minimax`。
  - 支援單股模式：`/value_scan 6217` 直接對單一股票進行價值重估掃描（不進入候選池）。
  - AI 候選股限制：一般模式最多 10 檔，深度模式最多 30 檔，source-only 模式為 0 檔（只做來源彙整）。
  - 內部共享「價值重估底稿」（local_rerating_snapshot），由 `rerating_snapshot_service.py` 提供，同時用於 `/research --deep` 與 `/value_scan`。
  - 多檔模式會注入 `topic_context`（候選股題材背景），AI 不得只因命中熱門題材就給高分，仍需依財報、營收、公告、新聞、籌碼、價量與反證判斷。
  - Prompt 規則模組化：7 支獨立規則檔（量化、價值重估、籌碼、技術、來源可信度、風險反證、歷史日期）。

### 報告來源顯示規則

- Markdown 報告只附上精簡來源表，預設列前 40 筆來源，避免大量搜尋摘要或網頁片段塞進正文尾端。
- 完整來源、長摘要、provider_detail 與抓取狀態仍完整保存在同名 `.sources.json` 與 HTML 報告的「完整資料來源」分頁。
- 報告 metadata 會保留 `source_coverage_summary`、`qa_warnings` 與 rejected source 數量，用於判斷來源品質與資料缺口。
- 報告輸出會自動移除模型寒暄前言（例如「好的，我將根據...」）與包住整份報告的 Markdown code fence，避免正式報告混入聊天語氣。
- 若模型正文完全沒有標示 `[Sxxx]`，系統會補上一段「來源引用補充」，列出主要參考來源；這是可追溯性補強，不代表模型已完成逐段引用。
- 題材/族群報告的共用來源過濾會排除非台股市場活動、A 股主體內容、Threads/YouTube 標題黨與含「必漲」「穩賺」等不合規字眼的低可信來源。

- /theme_radar [--days 7] [--source market|radar|all|recent] [--date YYYY-MM-DD] [--model gemini|deepseek|minimax]
  全市場題材雷達。優先使用共用 `market_movers` 全市場異動資料（漲幅、跌幅、量增、成交值、創高/創低、漲跌停；缺欄位會明確標示），再用 `config/theme_profiles.json`、`config/company_theme_map.json`、`config/supply_chain_nodes.json` 對應題材、族群與供應鏈節點。
  - 在 Telegram 直接輸入 `/theme_radar` 會先顯示資料日期選單（最新日期/指定日期），再顯示模型選單（Gemini、DeepSeek V4 Pro、MiniMax M2.7）；若已帶 `--date` 或 `--model` 等參數，則維持原本邏輯直接執行。
  - 會輸出 `market_movers`、`theme_rankings`、`theme_flow_summaries`、`sector_strength`、`news_theme_stats` 與 `data_quality`。
  - 報告會區分 `report_date` / `report_generated_at` 與 `market_data_date`；若週末或非交易日執行，盤面判斷會標示實際價量資料日，避免把報告產生日誤寫成盤中資料日。
  - `market_movers` 不套用 `/scan` 的股價、均量、營收等硬篩；低價股、小型股與投機股會被保留為市場訊號，但報告需列為風險。
  - `/scan`、`/radar` 與策略候選名單只能作輔助參考，不再代表市場熱點。
  - 外部搜尋與新聞入庫會排除 farmers market、food market、event calendar、holiday calendar、crypto market cap、A 股主體內容、Threads/YouTube 標題黨等非台股或低可信來源；`/theme_radar` 不再用單獨 `market` 當搜尋詞。
  - 題材代表股規則更嚴格：同產業上漲只能代表類股強，不能直接當題材受惠；HBM、AI ASIC、液冷、BBU、矽光子、功率半導體等精準題材需有產品、客戶、營收、公司公告/法說或可信產業來源，才可列為已驗證或推論型代表股。
  - `/theme_flow` 的擴散推論已整合在 `/theme_radar` 前三大題材內；AI 需要標示事實、推論、待驗證項目與反證，不得輸出買進/賣出指令。
  - `--days` 控制本地新聞題材統計視窗，預設 7 天，最大 60 天。
- /theme_flow 題材 [--days 7] [--date YYYY-MM-DD] [--model gemini|deepseek|minimax]
  單一題材擴散路徑。用題材庫與供應鏈節點拆出上游、中游、下游、周邊受惠層，判斷哪些環節已發動、哪些只是延伸候選，適合追蹤 AI 伺服器由晶片、記憶體、儲存擴散到電源、功率半導體這類路徑。
  - Layer 會分開 `已驗證代表股`、`推論型代表股`、`待驗證候選股`；candidate / keyword_or_industry 不得稱為代表股或核心受惠股。
  - 會讀取 `layer_market_validation`，標示每一層是否已有漲幅、量增、成交值、創高或漲停等盤面驗證；若沒有，只能寫「尚未從盤面驗證」。
  - 報告開頭必須區分 `report_generated_at` / `report_date` 與 `market_data_date`；非交易日執行時不得把報告產生日寫成「今日盤面」。
  - 若核心股只出現在外部新聞或法說來源、但不在本次 `market_movers` / 本地股票池中，報告只能寫「新聞/法說支持，但本次盤面未直接驗證」。
  - 題材擴散需分清楚事實、推論與候選：price-only、同產業或關鍵字命中只能放待驗證候選，不得寫成已驗證受惠。
  - 搜尋來源寫入共用新聞庫前會清洗 `related_topics`，避免把整包題材 JSON/dict 字串寫入 news context；只保留短題材名稱或產業詞。
  - 報告 prompt 禁止輸出 `[S?]`，缺來源時只能寫「來源未對應，僅列為待驗證線索」。
  - 若 AI 發現本地題材庫缺少核心股，報告需列「題材庫待補清單」，供後續 `/topic_maintain 題材 --bootstrap` 或 `/topic_import` 補正式題材庫。
  - 報告輸出會自動清除模型包住整份 Markdown 的 ```markdown code fence，避免 Telegram/HTML 顯示異常。
  - Telegram 輸入 `/theme_flow` 會先要求輸入題材或產業，送出後依序選擇資料日期與 AI 模型；輸入 `/theme_flow AI伺服器` 會從日期選單開始。若已帶 `--date`、`--model`、`--days` 等參數，則維持原本邏輯直接執行。
- /sector_strength [--days 7] [--source market|radar|all|recent] [--date YYYY-MM-DD] [--model gemini|deepseek|minimax]
  傳統產業/類股強弱排行。以共用 `market_movers` 全市場異動資料聚合產業分類，分析今日強勢族群、弱勢族群、量增族群、創高/創低族群與題材共振，作為 `/theme_radar` 的族群面確認。
  - 報告 metadata 會保存 `market_data_date`、`report_generated_at`、`sector_strength` 摘要與 `market_movers` 摘要，方便事後確認 AI 使用的是哪一天的全市場排行資料。
  - 報告開頭必須區分 `report_date` 與 `market_data_date`；若非交易日執行，不能把報告產生日寫成「今日盤面」，只能引用最近可用盤面資料日。
  - 報告會分開 `sector_strong_samples`、`representative_stocks`、`candidate_stocks`：前者是類股強勢樣本，不能直接稱為題材代表股；candidate 只能稱為待驗證候選股。
  - 題材關聯會依 verified / inferred / candidate / missing 統計。若 missing 很高，報告必須寫明「價格/量能強，不代表題材已驗證」。
  - 類股強弱只回答哪些產業分類正在轉強；若沒有 verified / inferred 題材證據，不得直接宣稱 AI、HBM、ASIC、液冷、BBU、矽光子等題材受惠。
  - 若價量快取缺少日漲幅、量增、成交值或新高新低欄位，會在 `market_movers.data_quality.missing_fields` 標示，不會假裝已取得完整排行。
  - 外部搜尋會優先使用台股、TWSE、TPEx、月營收、法人資金與上榜類股名稱等關鍵字；共用來源過濾會排除農夫市集、活動市集、加密貨幣 market update、A 股主體內容、Threads/YouTube 標題黨與非台股商品市場影片等不相關來源。
  - Telegram 輸入 `/sector_strength` 會先顯示資料日期選單，再顯示模型選單。
  - 若已帶 `--date`、`--model`、`--days`、`--source` 等參數，則維持原本邏輯直接執行。
- /radar [--source technical] [--date YYYY-MM-DD] [--ai-top 5] [--model gemini|deepseek|minimax] [--no-ai-comment]
  每日選股雷達。預設來源是技術面選股結果；無參數時會先顯示日期選單，再顯示 AI 短評模型選單。
  - 候選來源第一版支援：`technical`、`curated`、`financial`、`chip`、`monitor`、`portfolio`。
  - 若指定來源沒有既有結果，會呼叫既有選股流程產生名單；不修改原本選股邏輯。
  - `--date` 會指定 Radar 分析日期，新聞與網路來源不得晚於該日期。
  - 未指定 `--date` 且今天不是交易日時，系統會提示並改用最新可用交易日，避免假日直接重跑技術掃描。
  - 題材/族群新鮮度先讀本地新聞資料庫；啟用 AI 短評時，外部來源沿用 Research Center 搜尋鏈：MiniMax Search → Tavily Search → Gemini fallback → WebFetch → 日期過濾。
  - `--no-ai-comment` 只跑本地 Radar 與輕量外部來源補強，不呼叫 AI 短評。
  - 每日 20:30 交易日自動推送，等同 `/radar --source technical --ai-top 5 --model deepseek`。
- /radar_more [YYYY-MM-DD]
  查看最近一次或指定日期已儲存的 Radar 完整名單；只讀快取，不重新執行 Radar。
### Radar 補充規則

- `/radar` 預設來源為 `technical`，預設每策略取 `--ai-top 5`。
- 手動 `/radar` 無參數時會先顯示「最新交易日 / 指定日期」選單；選完日期後才顯示 Gemini / DeepSeek / MiniMax / 略過 AI 短評選單。
- `/radar --model deepseek` 會直接執行 DeepSeek AI 短評；`/radar --no-ai-comment` 會略過 AI 短評。
- AI 短評只輸出 AI 優先級、推薦理由、主要風險、觀察重點與信心度；不修改本地 100 分總分，不新增候選股。
- Radar 顯示會將技術策略與籌碼策略轉成繁體中文，不顯示 `B2_short_reclaim_after_break_ma` 或 `chip_1:B` 這類內部代碼。
- 若同日期已有技術面選股快取，Radar 會先讀快取候選名單。
- 若技術面選股快取只有股票代碼，沒有 A/B/C/D 策略訊號，Radar 會重跑既有 `technical_scanner.run_technical_scan()` 補策略訊號；不改技術選股邏輯。
- Radar 候選股建立後，會用既有 `chip_strategies.build_market_context()` 與 `build_chip_grade_maps()` 補 `chip_1` 到 `chip_4` 籌碼評級；不改法人或大戶選股邏輯。
- 技術分來自 A/B/C/D 策略訊號與 `technical_setup_score`，滿分 40。
- 籌碼分來自既有籌碼評級，滿分 15；未命中籌碼策略才會維持 0。
- 總分滿分 100，組成為技術 40、營收 15、籌碼 15、題材 20、族群 10。
- Radar 寫入 `.cache/radar_results.json` 前會先轉成 JSON-safe 型別，避免 pandas/numpy 技術訊號值造成快取儲存失敗。

- /news [latest|7d|refresh] [--model gemini|deepseek|minimax]
  新聞查詢與自動整理。支援 Gemini、DeepSeek V4 Pro（OpenCode Go）、MiniMax M2.7 三種 AI 模型。
  - `latest` / `7d` 讀取新聞庫時會再次套用台股財經新聞過濾，並隱藏 Dictionary、CNN、CBS、AP、YouTube、Google News 等明顯非台股新聞的舊資料。
  - `latest` / `7d` 顯示前會再次過濾並正規化分類；Telegram 只顯示繁體中文分類，不顯示 `sector_strength`、`theme`、`theme_radar` 等內部 raw category。
  - `latest` / `7d` 會固定顯示新聞分類框架；若某分類本期沒有符合新聞，會顯示「本期暫無符合新聞」，避免分類因當批資料不足而消失。
  - 台股盤前、盤中、盤後、加權指數、櫃買、台指期、外資、投信、三大法人、成交量、創高、萬點等盤勢新聞會優先歸到「台股與大盤」；只有主軸是央行、利率、匯率、CPI、GDP、Fed、美債、政策等事件時，才歸到「政策 / 匯率 / 總經」。
  - 「題材與族群輪動」會優先收族群、題材、概念股、供應鏈、資金輪動、多檔齊漲、被動元件、PCB、散熱、重電、機器人、矽光子、CPO、低軌衛星、記憶體、ASIC、AI 伺服器等新聞；「個股利多利空」會優先收法說、財報、營收、EPS、接單、出貨、訂單、目標價、升評/降評、處置股、注意股、漲停/跌停等公司事件。
  - 「庫存持股新聞」必須在標題或摘要明確命中持股代碼或名稱；`related_symbols`、`related_topics`、`最近掃描:*` 這類候選股標籤不會單獨觸發持股新聞，避免非持股標的混入。
  - 舊新聞庫若已有錯誤分類或泛英文新聞，查詢時會隱藏不合格資料，不直接刪除整個新聞庫。
  - `latest`：只顯示可解析發布時間且位於最近 24 小時內的新聞，依分類顯示，最後固定附上「庫存持股新聞」區塊。
  - `7d`：只顯示可解析發布時間且位於最近 7 天內的新聞，依分類顯示，最後固定附上「庫存持股新聞」區塊。
  - 庫存持股新聞不再是獨立指令；若命中持股代碼或名稱，會從一般分類移出，集中顯示在最後的「庫存持股新聞」區塊，沒有資料時顯示無新聞。
  - Telegram 顯示時，新聞標題會直接變成可點擊連結，不額外顯示完整網址。
  - 顯示前會排除 `share.google` 等轉址網址；若無法取得原文網址，該筆不顯示，避免標題連結打開非原文頁。
  - 顯示前會排除 MoneyDJ / 券商站內的列表或內文中繼頁，例如 `djhtm`、`type=list`、`新聞內文-{...}` 這類非正式新聞頁。
  - 純美股、歐股、加密貨幣或泛國際市場新聞，若沒有明確提到台股、台灣公司或台灣產業影響，不會顯示在 `/news latest` 或 `/news 7d`。
  - 同一篇新聞若由不同來源或不同 query URL 重複進入新聞庫，Telegram 顯示前會用標題近似去重，只保留分數較高的一則；原始資料仍保留在新聞庫。
  - `refresh`：手動觸發新聞搜尋 → 持股專用搜尋 → WebFetch → AI 批次摘要與分類 → 寫入新聞庫。
    - 搜尋使用台股、台灣財經、股票、產業新聞專用 query（來自 `build_news_discovery_queries()`），不使用泛用 `latest news` / `breaking news`。`latest` 搜尋會把今天日期（`YYYY-MM-DD` 與 `YYYY/MM/DD`）放進 query，降低舊新聞混入。
    - 會額外讀取庫存持股，針對每檔持股產生股票新聞 query；命中的持股新聞會寫入同一個新聞庫，查詢時集中到最後的「庫存持股新聞」區塊。
    - 流程：台股財經 query + 持股 query → Gemini Discovery + MiniMax Search → 偏好來源加權 → WebFetch 抓正文 → 本地台股財經過濾 → 發布日期過濾（refresh 預設保留 7 天內，查詢 latest/7d 則必須有可解析發布時間）→ 新聞排序與數量限制 → AI 批次分類摘要（Gemini/DeepSeek/minimax）→ 寫入新聞庫。
    - AI 分類前會先依台股相關度、來源品質、持股命中、文章型態排序；預設最多送 50 則給 AI，可用 `NEWS_AI_CLASSIFY_LIMIT` 調整。
    - AI 分類會分批執行，預設每批 5 則，避免 DeepSeek / MiniMax 長時間無回應；可用 `NEWS_AI_CLASSIFY_BATCH_SIZE` 調整。
    - 每則新聞送 AI 分類時會截斷正文，預設最多 800 字；原文全文仍保存於新聞庫，不會刪除。可用 `NEWS_AI_CLASSIFY_TEXT_LIMIT` 調整。
    - 每批 AI 分類會在 CMD 顯示進度，例如 `AI 分類 1/5 開始`、`AI 分類 1/5 完成`。
    - 每批 AI 分類預設 timeout 為 90 秒，可用 `NEWS_AI_CLASSIFY_TIMEOUT_SECONDS` 調整；單批失敗或 timeout 時，會用標題與短 snippet 輕量重試一次，重試仍失敗才改用本地關鍵字分類，其他批次繼續執行。
    - Smoke test 驗收：搜尋來源數 > 0、WebFetch 成功數 > 0、台灣過濾後數量 > 0、saved > 0。
  - `--model` 參數只影響 `refresh` 的 AI 分類模型，例如 `/news refresh --model deepseek`。`latest` / `7d` 只讀新聞庫，不呼叫 AI。
  - 無參數 `/news` 會先顯示新聞動作選單；只有選擇「搜尋並更新新聞」時，才會再顯示模型選單（Gemini / DeepSeek V4 Pro / MiniMax M2.7）。
  - **每日 08:45、18:00 自動推播**：排程新聞固定使用 DeepSeek V4 Pro（OpenCode Go）進行 AI 摘要。
  - **台股限定過濾**：搜尋與分類只收台股、台灣財經、股票、產業新聞；字典頁、泛國際新聞、娛樂、體育、政治新聞會被過濾排除。
  - **非文章頁面排除**：以下頁面類型會被排除，不寫入新聞庫：
    - 首頁、查詢頁、清單頁、搜尋頁
    - PDF 檔案（年報、簡報、手冊、評分指南、產業報告等）
    - 公司頁、商品頁、ETF 清單頁、權證清單頁、交易制度頁
    - 券商買賣證券日报表查詢系統頁
    - 交易所/櫃買中心的產業研究報告頁、產業價值鏈資訊平台頁、公司治理評鑑頁
    - 公開資訊觀測站（MOPS）頁、期貨/選擇權/牛熊證查詢頁
  - **保留的新聞類型**：
    - 單篇新聞文章（財經媒體、產業媒體）
    - 交易所/櫃買中心的新聞稿內容頁
    - 公司重大訊息新聞稿
    - 產業分析文章
  - **smoke test top 10 人工檢查**：smoke test 输出的 top 10 應為新聞文章，若混入金控查詢頁、ETF 清單頁、PDF、或查詢系統頁，代表過濾規則需要補強。
  - Gemini 分類時使用 `enable_grounding=False`（不使用地情增強）；DeepSeek 不接受 `enable_grounding` 參數，直接呼叫 `generate_report()`。
  - MiniMax 使用 `generate_json()` 而非 `generate_report()`，並自帶診斷資訊（`raw_response_samples`，最多 3 筆，每筆含 `status`、`raw_keys`、`item_count`、`preview`）。
    - MiniMax MCP Search 診斷 reason codes：
      - `mcp_parse_error`：回應成功但無法解析為搜尋結果
      - `mcp_empty_results`：查詢成功但回傳 0 筆（可能 API 金鑰無效或配額用盡）
      - `mcp_error_response`：MCP 回傳 error 欄位
      - `mcp_timeout`、`mcp_protocol_error`、`minimax_quota_or_credit_failed`、`mcp_package_not_installed` 等
  - MiniMax MCP Search 支援 `max_queries_per_task` 參數（每 task 最多查詢數），可用 `NEWS_SMOKE_TEST=1` 環境變數將上限設為 2，避免 smoke test 卡住。MiniMax 失敗時自動 fallback 到 Tavily，不影響整體流程。
  - Smoke test 驗收標準：搜尋來源數 > 0、WebFetch 成功數 > 0、台灣過濾後數量 > 0、saved > 0。
    - **重要**：smoke test 完成後需**人工檢查 top 10** 是否為真正的新聞文章。若 top 10 出現查詢頁、PDF、列表頁、公司頁，代表過濾規則有漏網，需補強 `_is_non_article_page()` 的 URL/Title pattern。

**Smoke test 指令：**

```powershell
# 基本 smoke test（跳過 MiniMax，使用 Tavily）
python scripts/smoke_news_refresh.py --model deepseek --skip-minimax-search

# 包含 MiniMax，但限制每 task 1 個 query、5 秒 timeout
python scripts/smoke_news_refresh.py --model minimax --max-minimax-queries-per-task 1 --minimax-timeout 5 --minimax-ai-timeout 45 --max-total-seconds 180

# 完整驗收（含 MiniMax，5 分鐘上限）
python scripts/smoke_news_refresh.py --model deepseek --max-total-seconds 300
```

Smoke test 環境變數：
- `NEWS_SMOKE_TEST=1`：自動將 MiniMax 每 task 查詢上限設為 2
- `NEWS_SKIP_MINIMAX_SEARCH=1`：完全跳過 MiniMax MCP Search（只用 Tavily）
- `NEWS_SMOKE_TASK_LIMIT=2`：限制新聞 discovery task 數量，避免 smoke test 跑完整 10 個 task。
- `NEWS_SMOKE_MAX_SOURCES=5`：限制送入 WebFetch / 分類的來源數量。
- `NEWS_SMOKE_CLASSIFY_LIMIT=5`：限制送入 AI 分類的新聞數量。
- `--minimax-ai-timeout`：限制 MiniMax 分類呼叫 timeout；只影響 smoke test，不改正式 Telegram 流程。

若 MiniMax Search 卡住，先用 `--skip-minimax-search` 確認 Tavily + WebFetch + AI 分類流程正常，再單獨檢查 MiniMax diagnostics。

- /report latest
  查詢最近一次 AI 投研報告；也可用 `/report 2330 latest`、`/report macro latest` 等方式查詢。
- /data_status 2330
  查詢指定個股或指令資料包的 Feature Pack / 資料覆蓋狀態，不呼叫 AI。
- /backfill_status
  查詢最近一次回補 marker、快取健康度與回補可用狀態。
- /news_status 2330
  查詢新聞庫內指定股票或主題的保存狀態，可搭配 `--days 14`。
- /backfill [YYYY-MM-DD] [force]
  完整資料回補：建立選股 + 投研候選池，並預熱所有本地結構化資料快取。不執行 AI 搜尋與模型分析。

AI 投研報告會保存到 `reports/`，並寫入 `database/stock_research.db`。Telegram 預設只回覆摘要並傳送 Markdown / HTML 檔，JSON 留在本地供 API 或後續 Agent 使用。

### AI 投研共享資料層（2026-05-23）

所有主要 AI 投研指令會在送出 prompt 前統一附加：

- `news_context`：從本地新聞庫讀取可用新聞；若執行時有搜尋到新來源，會去重後回存新聞庫。
- `feature_pack`：依指令類型整理個股、候選股、宏觀或題材的核心資料包。
- `data_coverage`：列出本次 prompt 實際可用與缺漏的資料欄位，避免 AI 誤以為資料完整。

適用指令包含 `/research`、`/value_scan`、`/macro`、`/theme`、`/theme_radar`、`/theme_flow`、`/sector_strength`。狀態查詢可用 `/data_status`、`/backfill_status`、`/news_status`，這些查詢不呼叫 AI。

報告輸出也會保留精簡後的共享資料層，方便事後確認 AI prompt 實際可用的共用資料。JSON 報告會在 `metadata.shared_data_layer`、`metadata.news_context`、`metadata.saved_news_context`、`metadata.news_persistence_status`、`metadata.feature_pack`、`metadata.data_coverage` 保留摘要；source-only 或 fallback Markdown 會額外顯示「共享資料層摘要」。

AI 投研指令範例：

```text
/research 2330
/research 台積電 --deep
/research 6217 --source-only --date 2026-01-07
/research 6217 --score
/macro 台股 AI --deep
/theme AI伺服器 --top 20
/value_scan 精選選股 --deep
/news latest
/news refresh
/report latest
```

### AI 題材知識庫維護

AI 題材知識庫維護使用變更包（Change Pack）機制：AI 只產生變更包，不直接覆寫正式題材庫。完整流程：

1. **初始化或更新題材庫**：`/topic_maintain [題材名] [--bootstrap] [--model gemini|deepseek|minimax]`
   - 正式題材庫為空時 → mode=initial，輸出 initial change pack JSON
   - 正式題材庫已有資料 → mode=update，輸出 update change pack JSON
   - Prompt log 與 raw response 自動保存於 `logs/ai_prompts/` 與 `logs/topic_ai_raw/`（開發除錯用，一般使用者不需要查看）
   - `/topic_maintain` 預設完整維護，不需再選一般/深度；`--bootstrap` 用於一次性補足既有題材庫缺欄位
   - `/topic_maintain` 固定使用最新資料，不提供日期選擇
   - 若帶題材名，例如 `/topic_maintain AI電源 --model deepseek`，Discovery 與 prompt 會聚焦該題材、代表股、產品、客戶、營收曝險、供應鏈角色、反證與資料缺口。
   - 模型選項：Gemini（預設）、DeepSeek V4 Pro（OpenCode Go）、MiniMax M2.7
   - 非致命缺欄位（`supply_chain_nodes`、`affected_companies`、`risk_notes`、`missing_data`）會自動補 placeholder，不會標為 failed

2. **查看變更建議**：`/topic_review [change_id]` - 列出所有變更包或顯示詳情

3. **確認套用變更包**：`/topic_confirm <change_id>` - 只接受 pending，套用前自動備份

4. **拒絕變更包**：`/topic_reject <change_id>` - 狀態改為 rejected，不修改正式庫

5. **查看正式題材庫**：`/topic_profiles` - 列出目前正式題材知識庫內容

   - `/topic_review`、`/topic_maintain`、`/topic_import` 產生的變更包訊息會附上 Telegram 按鈕。
   - 手機版可直接點「確認套用」、「拒絕」、「重新查看」，不需要複製 `change_id`。

6. **重置題材庫**：`/topic_reset --confirm` - 備份後清空正式題材庫（須帶 --confirm 否則只顯示說明）

7. **外部高階 AI 初始化**：
   - `/topic_seed_prompt`：以 Telegram TXT 檔傳送完整深度研究提示詞，內容由 `prompt/topic/topic_seed_prompt.md` 維護。
   - TXT 提示詞會要求外部高階 AI 自行擴充題材關鍵字、上網搜尋即時資料，並輸出可匯入的 JSON。
   - 外部 AI 回傳 JSON 需包含 `actions`；若有公司產品、客戶、營收曝險、供應鏈資料，也可包含 `company_knowledge_updates`。
   - `/topic_import`：貼上或上傳外部 AI 回傳的 JSON，系統會先顯示「建立變更包 / 取消」確認按鈕。
   - `/topic_import` 上傳 JSON / TXT 單檔上限為 10MB；超過上限請拆分後分批匯入。
   - `/topic_import` 只做本地 JSON 匯入與欄位正規化，不會再呼叫 AI；建立後的 change pack 預設標記為 `external`。
   - `/topic_import` 會自動判斷匯入內容是 `initial` 還是 `update`：大量 `create_theme` 會視為初始化；`update_theme` 或裸 `supply_chain_nodes` 補強資料會視為更新。
   - 外部 AI 若是補強既有 `company_theme_map`、`supply_chain_nodes`、`company_knowledge`，應輸出 `mode=update` 與 `update_theme`，不要標成 `initial`。
   - `/topic_import --model minimax {JSON}`：仍保留相容舊用法，可直接用指令加模型來源標記匯入，但不會呼叫 MiniMax。
   - 匯入後仍不會直接寫入正式庫，需用 `/topic_review <change_id>` 檢查，再用 `/topic_confirm <change_id>` 套用。

8. **外部產業來源快取同步**：
   - `/topic_source_sync`：同步 TPEx 產業鏈資料與 UDN 產業資料庫索引到本地快取，並直接套用到正式題材庫。
   - `/topic_source_sync --tpex`：只同步 TPEx 產業鏈資料。
   - `/topic_source_sync --udn`：只同步 UDN 產業資料庫索引。
   - 同步結果會寫入 `config/tpex_industry_chain.json` 與 `config/udn_industry_topics.json`。
   - TPEx / UDN 屬於可信外部來源，系統會將可辨識的產業、題材、公司與供應鏈節點同步到 `config/theme_profiles.json`、`config/company_theme_map.json`、`config/supply_chain_nodes.json`。
   - 寫入正式題材庫時會保留來源追蹤欄位，例如 `source_sync`、`source_sync_status=verified`、`source_sync_method=topic_source_sync`、`synced_at`、`last_seen_at`、`source_confidence=verified`。
   - 後續 `/research`、`/theme`、`/value_scan` 等投研指令不需要各自重抓同一批產業資料，會透過既有 `topic_context` / `feature_pack` 共用正式題材庫內容。
   - TPEx 若遇到 Python SSL 憑證相容問題，系統會只針對 `ic.tpex.org.tw` 先正常驗證、失敗後再用受控 fallback 抓取一次，並在快取 metadata 記錄 `ssl_fallback=true`；其他來源仍維持正常 SSL 驗證。

### AI 題材庫維護 prompt 位置

- 主 prompt 目錄：`prompt/topic/`（優先讀取）
- 舊版備援模板已封存：`archive/legacy/config/prompts/`
- 核心 prompt：`topic_maintain.md`（單一提示詞，透過 `mode=initial` / `mode=update` 控制規則）
- 外部初始化 prompt：`topic_seed_prompt.md`（由 `/topic_seed_prompt` 以 TXT 檔輸出，供外部高階 AI 產生可匯入的 JSON）
- 其他：`topic_discovery_search`、`topic_webfetch_extract`、`topic_review_summary`

### AI 題材庫維護資料來源

搜尋仍維持**全網搜尋**，不是白名單限制。系統額外使用「偏好來源清單」`config/preferred_sources.json` 做以下輔助：

- **補充 `site:` query**：`/topic_maintain` 的 Discovery 會在一般 query 之外，額外產生少量 `site:` 補搜 query。每個 discovery task 最多 4 個 `site:` query，全部 task 的總量受控，避免搜尋成本暴增。
- **來源加權排序**：來源分級時參考偏好清單，L1 官方 > L2 財經/產業媒體 > 未知來源 > L3 社群。
- **WebFetch 優先順序**：抓正文前會以 `sort_sources_by_preferred_weight()` 排序，使高權重來源（官方、主流媒體）優先進入 WebFetch 選取清單。社群與非偏好來源仍會被抓取，只是排序較後。
- **AI 信心度參考**：prompt 要求 AI 根據來源等級判斷信心度，`L3_community` 不得單獨支撐 `high` confidence。

搜尋優先順序：MiniMax MCP → Tavily → Gemini Fallback  
WebFetch：requests+BS4 → Tavily Extract（上限：一般12個 URL、深度30個 URL）

#### 共用搜尋任務層（2026-05-24）

#### 共用資料調度層（2026-05-24）

- `feature_pack` 已升級為 `feature_pack_v2`，會記錄 `schema_version`、`generated_at`、指令、模式、目標、新聞事件數與資料缺口摘要。
- `data_gap_summary` 使用 `data_gap_v1`，統一列出各指令缺漏欄位、覆蓋分數、優先缺口與建議回補動作。
- `unified_evidence_pack` 使用 `evidence_pack_v1`，把 Feature Pack、本地底稿、價值重估底稿、新聞事件、籌碼、技術、資料缺口等整理成同一格式，供 prompt、報告 JSON 與 QA 共用。
- `news_events` 使用 `news_event_v1`，由新聞庫 `news_context` 產生輕量事件，並會一併寫入事件庫，讓新聞庫與事件庫可以互相查用。
- `search_query_log` 會標示 `search_tasks_v1`，代表本次搜尋任務模板版本；MiniMax、Tavily、Gemini fallback 都會寫入同一份 provider summary。
- 回補完成標記 `complete.json` 會加入 `backfill_priority_plan`（`backfill_priority_v1`），依健康度缺口產生下一輪優先回補任務建議。
- 報告 JSON 的 `metadata.shared_data_layer` 會保留 `news_context`、`feature_pack`、`data_gap_summary`、`unified_evidence_pack`、`news_events`、`news_event_summary`、`search_query_log` 摘要；`structured_data` 也會保留 QA 可追溯欄位。
- 驗證方式：執行 `/research 2330 --source-only`、`/value_scan 精選選股 --source-only`、`/theme AI伺服器 --source-only` 後，檢查 JSON 報告是否包含上述 schema version。

- 搜尋任務已集中到 `research_center/search_query_service.py`，由同一層產生不同指令的 discovery tasks。
- 已接入指令：`/research`、`/value_scan`、`/theme`、`/macro`、`/theme_radar`、`/theme_flow`、`/sector_strength`、`/news`、`/topic_maintain`。
- 各指令仍保留不同搜尋意圖與關鍵字：個股研究偏官方公告、財報、新聞、籌碼、反證；價值重估偏候選股重估證據與交叉驗證；題材與宏觀指令偏供應鏈、產業、國際局勢與政策。
- 搜尋 provider 流程不變：MiniMax MCP Search → Tavily Search → Gemini fallback。
- 搜尋來源正規化集中到 `research_center/search_source_normalizer.py`，統一補上 `provider`、`found_by`、`used_in_section`，方便後續新聞庫、Feature Pack、source events 與報告來源共用。
- 日期模式仍沿用既有 `--date` 過濾與 historical policy，不改變歷史報告防偷看未來的保守邏輯。

送入 prompt 的本地資料：
- 股票宇宙摘要（總數、產業分布）
- 近 5 次掃描明細（前 30 檔候選股）
- 市場訊號（高成交量、產業分布、掃描產業龍頭、候補快取摘要）
- 既有料、Company-Topic Map、供應鏈節點
- `/topic_source_sync` 建立的外部產業來源快取（TPEx 產業鏈、UDN 產業資料庫索引）
- 近期 `/theme` 題材研究紀錄（題材名、摘要、來源、建議搜尋詞）。這只作為搜尋線索與背景參考，AI 必須用最新 Discovery、WebFetch、官方公告、新聞與本地資料重新驗證。
- Discovery 來源、WebFetch 正文、**規則式 evidence candidates**（非 AI 萃取）

`/theme` 報告完成後，系統會保存精簡研究紀錄到 `.cache/recent_theme_reports.json`。這不是正式題材庫，不會直接寫入 `theme_profiles.json`；後續 `/topic_maintain` 只把它當作補充搜尋方向與參考脈絡。

### 題材庫維護流程說明

| 階段 | 說明 | 是否呼叫 AI |
|------|------|-------------|
| Discovery 搜尋 | Gemini Search 找相關來源 | 是（Gemini Search） |
| WebFetch 抓文 | 抓取 URL 正文 | 否 |
| Evidence Candidates | 規則式整理候選證據（關鍵字、公司名、來源等級） | **否** |
| Change Pack | 使用者選的模型產出正式題材變更 | 是（Gemini/DeepSeek/MiniMax） |

**Evidence Candidates 與 Change Pack 的差異：**
- **Evidence Candidates**：原料，回答「文章裡提到什麼」，由規則式整理，不是最終結論
- **Change Pack**：決策結果，回答「題材庫要怎麼改」，由 AI 模型根據候選證據判斷

**優點：**
- 不因 WebFetch evidence 萃取呼叫額外 AI，降低 429 與 JSON 解析失敗機率
- 選擇模型只影響最後 change pack，不影響 evidence 整理

### 題材知識庫維護指令範例

```text
/topic_maintain --model deepseek
/topic_maintain --model minimax
/topic_maintain --bootstrap --model minimax
/topic_review
/topic_review change_xxx
/topic_confirm change_xxx
/topic_reject change_xxx
/topic_profiles
/topic_reset --confirm
/topic_seed_prompt
/topic_import --model minimax {"summary":"...","actions":[...]}
/topic_source_sync
/topic_source_sync --tpex
/topic_source_sync --udn
```

`/topic_maintain` 預設就是完整維護模式；`--bootstrap` 可做一次性補缺欄位與回填。品質分級規則：`verified` 有 L1 官方證據可套用；`inferred` 有 L1/L2 證據支撐的合理推論可套用但保留推論標記；`candidate` 只保留在變更包不寫入正式題材庫；`missing` 只記錄缺口。`/topic_confirm` 會自動套用 `verified + inferred`，跳過 `candidate`，並記錄 `missing`。

### 安全規則

- 只有 `/topic_confirm` 會寫入正式題材庫
- `/topic_maintain` 不寫入正式題材庫（只產生 change pack）
- `/topic_confirm` 不呼叫 AI（只備份並套用 change pack）
- 套用前一定自動備份三個正式 JSON
- 壞 JSON 時不保存 pending change pack（但 raw response 仍會保存）

### 初始化合格標準

初始化模式（`/topic_maintain` 或空白資料庫）的品質檢查規則：

| 項目 | 標準 |
|------|------|
| 題材數量 | 12～20 個 create_theme actions |
| theme_id | 英文小寫 snake_case，例：`ai_server` |
| theme_name | 繁體中文，例：`AI伺服器` |

**真正會標為 `failed` 的條件（不可確認）：**
- actions 為空（AI 未回傳任何題材）
- create_theme 數量為 0（無法建立任何題材）
- theme_id 缺失（無法識別題材）

**非致命缺欄位會自動補齊，維持 `pending`：**
- `affected_companies` → 補 `[]`
- `risk_notes` → 補 `["待後續維護補強"]`
- `missing_data` → 補 `["待後續維護補強"]`
- `supply_chain_nodes` → 補 placeholder（`role: "待補供應鏈或題材關聯"`）

**warnings 簡化：**
- 不再逐筆列出 `create_theme '<id>': 缺少必要欄位 [...]`
- 改為統一提示：「部分題材資料尚未完整，系統已補入待補欄位，後續維護會持續修正。」

### 狀態說明

| 狀態 | 含義 | 下一步 |
|------|------|--------|
| `pending` | 可確認 | `/topic_confirm` 或 `/topic_reject` |
| `failed` | 真正無法套用（如 actions 空、theme_id 缺失） | `/topic_reject` 後重新 `/topic_maintain` |
| `confirmed` | 已套用 | 無 |
| `rejected` | 已拒絕 | 無 |

### /topic_confirm 寫入哪些檔案

執行 `/topic_confirm <change_id>` 會寫入以下四個正式檔案：

| 檔案 | 寫入內容 |
|------|----------|
| `config/theme_profiles.json` | TopicProfile（含 risk_notes、missing_data） |
| `config/company_theme_map.json` | 公司與題材對應（company_relations 優先，affected_companies fallback） |
| `config/supply_chain_nodes.json` | 供應鏈節點（supply_chain_nodes，含 layer、產品、客戶、營收曝險、證據） |
| `config/company_knowledge.json` | 公司產品、客戶、營收曝險、供應鏈角色與資料缺口（來自 company_knowledge_updates） |

**欄位保留：** `company_relations`、`affected_companies`、`risk_notes`、`missing_data`、`supply_chain_nodes` 會從 change pack action 寫入對應檔案。`company_knowledge_updates` 會寫入 `company_knowledge.json`。`/topic_confirm` 也會把 action 層級的 `evidence` 繼承到 `company_theme_map.json` 與 `supply_chain_nodes.json`，避免正式題材庫只剩題材名稱但缺少來源依據。

**新版建議輸出格式：** 題材維護 prompt 會要求 AI 使用 `company_relations` 作為公司層級主資料，欄位包含 `company_code`、`company_name`、`role`、`relation_strength`、`relation_type`、`products`、`customers`、`revenue_exposure`、`benefit_logic`、`evidence`、`counter_evidence`、`missing_data`。`affected_companies` 仍保留為相容欄位，但也必須是物件陣列。`supply_chain_nodes` 每筆建議包含 `theme_id`、`layer`、`company_code`、`company_name`、`role`、`customers`、`revenue_exposure`、`benefit_logic`、`confidence`、`source_level`、`evidence`、`risk_notes`、`missing_data`、`upstream`、`downstream`、`product_keywords`。若 AI 只在 action 層提供 evidence，系統仍會在 `/topic_confirm` 時繼承補到正式庫。

**可靠性規則：** `/topic_seed_prompt` 與 `/topic_maintain` 會要求 AI 不得捏造營收占比；無法確認時使用 `revenue_exposure.level = "unknown"` 並寫入 `missing_data`。L3 社群只能當 sentiment 或 candidate evidence，不可單獨支撐 high confidence。`/topic_confirm` 寫入供應鏈節點時會依 `node_id` 或 `theme_id + company_code + role` 去重合併，避免同一節點重複累積。

### 重置與重新初始化

```text
/topic_reset --confirm   # 備份後清空正式題材庫，再執行 /topic_maintain 重新初始化
```

### 題材名稱語言規則

- **theme_id**：英文小寫 snake_case，系統內部識別用（如 `ai_server`、`semiconductor_advanced`）
- **theme_name** 與所有顯示文字：繁體中文（如「AI伺服器」、「半導體先進製程」），不得使用未翻譯英文

### MiniMax 題材庫維護說明

**模型差異：**
- Gemini、DeepSeek V4 Pro（OpenCode Go）：使用一般 Markdown 報告流程（`generate_report()`）
- **MiniMax M2.7**：使用 JSON-only 流程（`generate_json()`），避免 Markdown 輸出導致 JSON 解析失敗

**JSON-only 機制：**
- `generate_json()` 使用不同的 system prompt，要求模型只輸出 JSON object，不輸出 Markdown、不使用 code fence、不輸出解釋文字
- 若模型未回傳有效 JSON，系統會將變更包標為 `failed`，並提供簡短錯誤說明。使用者只需拒絕該變更包或重新執行 `/topic_maintain`

**手動 Smoke Test（會消耗 MiniMax 額度）：**
```bash
python scripts/smoke_topic_maintain_minimax.py
```
成功條件：無 429、 無 JSONDecodeError、 change pack status 為 pending 或有明確 warnings。

**開發者除錯（一般使用者不需要）：**
- Prompt log 與 raw response 自動保存於 `logs/ai_prompts/` 與 `logs/topic_ai_raw/`
- 若 JSON 解析失敗，可查看上述目錄確認模型實際輸出
- 若 raw response 為 Markdown：表示模型未遵守 JSON-only prompt，屬於 MiniMax 模型行為異常
- 若錯誤訊息包含 429：等待後重試，或切換 Gemini / DeepSeek 模型
- 若錯誤訊息包含 `MiniMax API request failed; status=400`，代表 MiniMax API 拒收本次請求。系統會在 fallback 原因中保留 `status_code`、`reason_phrase`、`prompt_chars`、`payload_bytes` 與 MiniMax response preview，方便判斷是模型名稱/權限、payload 過大、格式或供應商端限制造成。
- `/theme_radar`、`/sector_strength`、`/theme_flow` 這類長 prompt 報告若選擇 MiniMax，可能因來源與題材底稿較大而觸發 400；遇到時報告會改用本地 fallback，Telegram 會顯示實際失敗模型，不再籠統標示為 Gemini / 搜尋失敗。

**若出現 429：**
- 等待 30 秒後重試
- 短期內多次執行會觸發 rate limit
- 建議改用 Gemini 或 DeepSeek 模型

### HTML K 線圖表功能

- 價格座標預設為自動縮放，圖表右上角可切換為手動模式；手動模式可自行拖曳價格軸調整顯示範圍。
- K 線主圖、量能、KD、MACD 區塊可用分隔線上下拖曳調整高度。
- 下方指標區塊依序為量能、KD、MACD，三個區塊皆可收合或展開，預設為展開。
- KD 指標參數為期間 9、RSVt 權值 9、Ktf 權值 55。
- MACD 參數維持 21 / 55 / 55。

### /stock_chart 指令範例

```text
/stock_chart 2330 2026-01-01 2026-05-01 1d
/stock_chart 0050 2026-04-01 2026-04-25 1m
```

### /tmf_chart 指令範例

```text
/tmf_chart 2026-05-01 2026-05-05 全日盤 1m
/tmf_chart 2026-05-01 2026-05-05 日盤 5m
/tmf_chart 2026-05-01 2026-05-02 夜盤 15m
```

### 市場摘要指令範例

```text
/morning
/noon
/tw_market
```

### /scan 互動流程

- 使用者輸入 `/scan` 後，機器人會先顯示 Inline Keyboard，列出 8 個選股策略。
- 選擇任一策略後，會出現日期選單，再進入該策略的執行。
- 日期選單提供兩個選項：
  - **📅 最新日期**：以今天作為目標資料日期，立即執行該策略。
  - **📝 指定日期**：輸入 `YYYY-MM-DD` 或 `YYYY/MM/DD` 格式的日期後執行。
- 所有 8 個選股策略都支援日期選單（1. 財報營收、2. 60日法人動態、3. 投信認養、4. 法人持股比例增加、5. 每週大戶持股、6. 技術面選股、7. 全部執行、8. 精選選股）。
- `/scan` 可接日期參數，例如 `/scan 2026-05-05`、`/scan 2026/05/05`、`/scan 20260505`、`/scan 5/5`；此用法會顯示 8 個策略選項，點選任一策略後直接使用指定日期執行，不再顯示日期選單。
- 日期參數不可晚於今天。若未輸入日期，系統預設顯示日期選單，由使用者選擇最新日期或指定日期。
- 財報營收選股本身仍以最新可取得月營收、毛利率與價量快取為主；日期參數主要影響法人日資料、技術面日 K 與精選報告的目標日期。

### /in 與 /out 指令範例

```text
/in 2330
/in 台積電
/out 2303
/out 聯電
```

### 庫存防呆與錯誤回覆

- 重複加入庫存時，會回覆「⚠️ 2330 台積電 已在您的庫存清單中。」
- 移除不存在的庫存時，會回覆「⚠️ 庫存內找不到 2330，請確認代號或名稱。」
- 無法解析代號或名稱時，會回覆「❌ 查無此股票，請確認輸入正確的台股代號或名稱。」

---

## 個人庫存籌碼推播

portfolio_manager.py 會獨立管理個人庫存與三大法人推播，不與 config.json 的 monitor_stocks 混用。

### 資料來源

本專案採用分層並行策略，並非所有來源同時全量抓取。來源優先順序：

```
cache → Yahoo / Fugle / FinMind 有限並行 → official 補正 → stale cache
```

#### 分層並行說明

- **cache**：本機快取，命中直接返回，不發網路請求。
- **Yahoo / Fugle / FinMind 有限並行**：快來源，可同時或短間隔啟動；任一成功即可讓流程先繼續，避免單一來源慢卡住流程。
- **official 補正**：官方（TWSE/TPEx/MOPS），僅在快來源有缺口、補正或失敗後使用。
- **stale cache**：最後保底，只在完全無資料時使用。

#### FinMind 限制（免費方案）

- 官方上限：600 次/hour
- 程式安全上限：500 次/hour（避免觸發官方限制）
- `/backfill` 建議上限：300 次/run
- `/scan` 建議上限：80 次/run
- `/research` 建議上限：20 次/run
- 跨小時自動重置

#### Fugle 限制（免費方案）

- historical：60 次/min
- intraday：60 次/min
- websocket connection：1
- websocket subscription：5
- 跨分鐘自動重置 historical / intraday

#### 來源失敗冷卻（SourceHealthManager）

- 第 1 次失敗：記錄，不冷卻
- 第 2 次失敗：冷卻 5 分鐘
- 第 3 次失敗：冷卻 10 分鐘
- 第 4 次以上：冷卻 30 分鐘
- 成功後清除失敗次數與冷卻

#### API Key 存放位置

- FinMind API Key：`config/secrets.json` → `finmind_api_key`（已加入 .gitignore）
- Fugle API Key：`config.json` → `fugle_api_key` 或環境變數 `FUGLE_API_KEY`

### 資料儲存格式

- portfolio.json 使用 Dictionary 結構，例如：

```json
{
  "2330": "台積電",
  "2605": "新興"
}
```

### 股票名稱與代號解析

- /in 與 /out 支援股票代號與中文名稱雙向輸入。
- 解析來源會優先讀取 stock_list.json，並補抓 TWSE / TPEx 官方股名 Open API，避免名稱快取不完整。
- 寫入 portfolio.json 時一律轉成純股票代號作為 Key。

### 三大法人資料來源

- 上市：TWSE /fund/T86
- 上櫃外資：TPEx /www/zh-tw/insti/qfiiStat
- 上櫃投信：TPEx /www/zh-tw/insti/sitcStat
- 上櫃自營商：TPEx /www/zh-tw/insti/dealerStat
- 若庫存內上市股票在 TWSE T86 查無資料或 T86 暫時被擋，會針對缺漏股票改用 FinMind TaiwanStockInstitutionalInvestorsBuySell 單檔補外資、投信與自營商資料。
- 籌碼策略一、二會先讀 .cache/chip_daily，只補缺日期與缺股票；若能抓到 TWSE / TPEx 單日全市場資料，會一次寫回當日快取，之後策略優先讀快取。
- TWSE 被 307 或其他安全頁擋住時，會進入退避冷卻，改用 FinMind TaiwanStockInstitutionalInvestorsBuySell 逐檔補缺口，成功一檔就立即寫回快取。
- 籌碼策略三的外資持股比例優先使用 TWSE MI_QFIIS；失敗時改用 FinMind TaiwanStockShareholding 的 ForeignInvestmentSharesRatio；再失敗時才使用外資買賣超對發行股數累計估算。
- 籌碼策略四維持 TDCC 官方資料與 .cache/tdcc 週快取。
- 法人選股可透過 `/scan 日期` 指定目標資料日期；策略一到三會以該日往前收集近 60 個交易日法人資料，策略四會取該日前可取得的 TDCC 週快照。
- 手動執行法人選股時會依策略需求補資料：策略一、二只補外資 / 投信買賣超；只有策略三或包含策略三的全部執行 / 精選選股，才會補 TWSE MI_QFIIS / FinMind 外資持股比例。
- 官方與 FinMind 請求會節流；官方來源同時間只走單線請求，請求間隔至少約 3 秒，FinMind 約 1.2 秒。
- 若 TWSE T86 或 MI_QFIIS 發生 307、安全頁、逾時等錯誤，會依序退避 1 分鐘、5 分鐘、30 分鐘，冷卻期間不重複撞同一端點。
- FinMind 只作缺漏補資料使用，每個交易日最多補 50 檔，避免免費額度一次被打滿；成功補到的資料會寫回 .cache/chip_daily，後續優先讀本機快取。
- .cache/chip_daily 會記錄資料來源欄位 source，例如 cache、TWSE、TPEX、FinMind、estimated，報告尾端會依實際資料來源顯示。
- 法人日資料回補會在 CMD 顯示來源進度，例如本機快取已補到幾檔、嘗試 TWSE T86 / TPEx 官方、官方補到幾檔，以及剩餘缺口是否改用 FinMind 單檔補資料，方便確認不是一開始就直接打 FinMind。

### 排程與重試機制

- 每日台北時間 17:45 觸發庫存籌碼推播。
- 若 portfolio.json 為空，任務直接中止，不發送訊息。
- 若交易所資料尚未更新，會每 5 分鐘重試 1 次，最多重試 3 次。
- 若重試 3 次後仍無資料，會推播「今日無法人籌碼資料更新 (可能為非交易日或交易所延遲)」。

---

## 籌碼策略模組

chip_strategies.py 依規格書新增 4 套策略，且都會先經過共同硬篩，避免一開始就對全市場做高成本回溯。

### 共同硬篩

- 最新收盤價介於 10 到 80 元
- 過去 20 日平均成交量大於 500 張
- 最新公告單月營收大於 50,000,000 元

### 策略一：60 日法人動態

- 觀察外資與投信合計淨買超天數
- S 級：今日外資或投信買超；近 60 個交易日外資 + 投信合計買超 >= 18 日，最近 10 日買超 >= 5 日，且最大單日賣超 < 買超日平均買超的 120%
- A 級：今日外資或投信買超；近 60 個交易日外資 + 投信合計買超 >= 10 日，最近 10 日買超 >= 4 日，且最大單日賣超 < 買超日平均買超的 180%
- B 級：今日外資或投信買超；近 60 個交易日外資 + 投信合計買超 >= 5 日，且最近 10 日內買超 >= 4 日

### 策略二：投信認養股

- 觀察投信 60 日靜默期與近 10 到 20 日發動期
- 共同條件：今日投信買超，且前 45 日投信估計持股比例平均 < 0.10%
- S 級：前 45 日投信合計買賣超絕對值 < 80 張，最近 15 日投信買超 >= 7 日
- A 級：前 40 日投信合計買賣超絕對值 < 250 張，最近 20 日投信買超 >= 5 日
- B 級：前 50 日投信合計買賣超絕對值 < 100 張，最近 10 日內至少 1 日投信買超

### 策略三：法人持股比例增加

- 觀察外資持股比例與投信持股估計比例的 60 日變化
- 法人持股比例 = 外資持股比例 + 投信估計持股比例
- 共同條件：今日外資或投信買超
- S 級：近 60 日法人持股比例增加 > 2.0 個百分點，最新值距 60 日高點 <= 0.2 個百分點，期間最大回落 <= 0.5 個百分點
- A 級：近 60 日法人持股比例增加 > 1.0 個百分點，最新值距 60 日高點 <= 0.4 個百分點，期間最大回落 <= 0.8 個百分點
- B 級：近 60 日法人持股比例增加 > 0.5 個百分點，且最新值高於最近 20 日平均

### 策略四：每週大戶持股

- 使用 TDCC 集保股權分散表快照
- 目前以 API 可直接聚合的級距，將 400 張以上視為大戶、50 張以下視為散戶
- 透過本地快取累積最近 8 週快照後，依大戶增持與散戶減碼節奏分成 S / A / B 三級

### 📈 技術面選股 (Technical Scanner)

基於客製化指標參數，自動掃描全市場具備轉折與突破訊號之標的。

- **操作方式**：輸入 `/scan` 後，於選單中點擊「技術面選股」。
- **日 K 來源順序**：先讀 `.cache/technical_daily` 本機快取，缺資料才抓 Yahoo Finance；Yahoo 無資料時改用 Fugle，成功後寫回快取。
- **指定日期**：可用 `/scan 2026-05-05` 先指定目標日期，再點擊「技術面選股」；若查詢過去日期，本機快取只要已涵蓋該日就會直接使用，不受 12 小時 TTL 限制。

#### 日 K 快取與資料來源備援

四大技術策略（A/B/C/D）依賴完整日 K（OHLCV）與以下指標：MA5/13/21/60/105/144、MACD(21,55,55)、KD(9,9,55)。資料來源沿用既有機制：

- 優先讀取本機 `.cache/technical_daily` 快取。
- 快取缺漏時依序抓取 Yahoo Finance → Fugle，成功後寫回快取。
- `/backfill` 可預熱快取，但不是即時掃描的必要前置。

#### CMD 進度提示

執行 `/scan` 技術面選股時，CMD 會顯示掃描進度，方便確認程式仍在執行、未卡住：

- 硬篩候選建立進度。
- 逐檔日 K / 指標計算進度。
- 四大策略偵測進度。
- 完成後統計（總掃描檔數、通過硬篩檔數、符合技術選股邏輯檔數）。

#### CMD 進度訊息時間戳（2026-05-21）

2026-05-21 起，所有 CMD 進度訊息統一加蓋時間戳，格式為 `[YYYY-MM-DD HH:MM:SS] [分類] [任務] | 訊息`：

| 分類 | 說明 |
|------|------|
| `選股進度` | 財報營收、籌碼、技術面選股進度 |
| `AI投研` | 研究報告、價值重估、題材研究進度 |
| `回填進度` | /backfill 資料回補進度 |
| `完整回補` | 手動 `/backfill` 進度 |
| `定時回補檢查` | 定時回補排程進度 |
| `監控策略` | 監控策略檢查訊息 |

受益檔案：
- `progress_logger.py`：時間戳公用程式（`now_timestamp()`, `format_progress_message()`, `print_progress()`, `format_duration()`, `format_cmd_message()`, `print_cmd()`）
- `research_center/orchestrator.py`：AI 投研進度訊息
- `backfill_service.py`：回補候選池、投研結構化資料、毛利率快取、精選選股進度
- `stock_scanner.py`：財報營收掃描進度
- `chip_strategies.py`：籌碼資料來源進度
- `technical_scanner.py`：技術面選股進度
- `monitor_service.py`：監控策略檢查訊息
- `main.py`：`/scan` 主流程、排程、新聞/晨報/午報失敗訊息等

時間戳由 `progress_logger.now_timestamp()` 動態產生，不使用靜態時間。

**避免雙時間戳規則：**
- 同一行 CMD 訊息最多只會出現一個 `[YYYY-MM-DD HH:MM:SS]`。
- AI 投研進度由 `telegram_handlers._print_progress()` 統一包裝時間戳與指令名稱。
- 內部服務（如 `orchestrator._emit_progress()`）只回報進度文字，不再重複加時間戳。
- 若訊息已帶時間戳（由內部服務預先加入），外層 handler 會偵測並跳過，避免雙重包裝。

正確格式：
```
[2026-05-21 10:32:18] [AI投研] /research 2330 --deep | 開始收集資料
```

應避免的錯誤格式（已修復）：
```
[2026-05-21 10:32:18] [AI投研] /research 2330 --deep | [2026-05-21 10:32:18] 開始收集資料
```

**回補進度時間戳規則：**
- `/backfill` 與定時回補進度也已納入時間戳規則。
- `backfill_service.py` 只回報純訊息（無時間戳）。
- `main.py` 的手動與定時回補 callback 負責加時間戳與分類前綴。
- 分類前綴：`[完整回補]`（手動 `/backfill`）、`[定時回補檢查]`（定時回補）。
- 同一行最多一個時間戳。

**統一格式規則（2026-05-21 修正）：**
所有 CMD 進度訊息統一為：`[YYYY-MM-DD HH:MM:SS] [分類] 訊息`

若上游訊息已帶時間戳，下游只能重組分類，不可新增第二個 timestamp：
- 已有時間戳 + 無分類：原樣輸出
- 已有時間戳 + 有分類：重組為 `[原時間戳] [分類] 原訊息內容`

**禁止的錯誤格式：**
- `[完整回補] [YYYY-MM-DD HH:MM:SS] 訊息`（分類在時間戳前）
- `[定時回補檢查] [YYYY-MM-DD HH:MM:SS] 訊息`（分類在時間戳前）
- 雙時間戳：`[時間] [分類] [時間] 訊息`

**正確格式：**
- `[YYYY-MM-DD HH:MM:SS] [完整回補] 訊息`
- `[YYYY-MM-DD HH:MM:SS] [定時回補檢查] 訊息`

回補進度範例：
```
[2026-05-21 10:32:18] [完整回補] 毛利率快取進度 1720/1762
[2026-05-21 10:32:25] [完整回補] 毛利率快取完成：0 檔更新
[2026-05-21 12:00:01] [定時回補檢查] 毛利率快取進度 1720/1762
[2026-05-21 12:00:08] [定時回補檢查] 毛利率快取完成：0 檔更新
```

技術面選股進度範例：
```
[2026-05-21 13:10:01] [選股進度][技術面選股] 100.00% 完成，符合技術邏輯 89 檔
[2026-05-21 13:10:05] [選股進度][技術面選股] 98.00% 技術面報告完成，準備傳送 Telegram
```

監控策略訊息範例：
```
[2026-05-21 13:10:12] 🔎 檢查監控策略：21MA 突破 6443.TW (元晶)...
```

`/scan` 主流程進度範例：
```
[2026-05-21 13:05:01] [選股進度][技術面選股] 0.00% 收到 /scan 選股任務，目標日期 2026-05-21
[2026-05-21 13:05:10] [選股進度][技術面選股] 75.00% 開始技術面選股
[2026-05-21 13:05:15] [選股進度][技術面選股] 98.00% 技術面報告完成，準備傳送 Telegram
[2026-05-21 13:05:20] [選股進度][技術面選股] 100.00% 完成
```

#### 技術指標參數

- **MACD**：21, 55, 55（FAST=21, SLOW=55, SIGNAL=55）
- **KD**：RSV=9, K=9, D=55
- **均線**：MA5、MA13、MA21、MA60、MA105、MA144、ATR14、volume_ma20

#### 報告輸出結構

`/scan` 技術面選股報告包含四個區塊：

1. **正面訊號標的**：均線突破 (21MA/105MA)、MACD/KD 黃金交叉
2. **負面訊號標的**：MACD/KD 死亡交叉
3. **四大技術策略**：A/B/C/D 四個策略區塊
4. **掃描統計**：總掃描檔數、通過硬篩檔數、符合技術選股邏輯檔數

> **注意**：原始技術訊號（正面/負面訊號標的）仍保留在報告中，四大策略是新增的分類區塊，**不取代**突破 21MA、突破 105MA、MACD/KD 金叉死叉等原始訊號。

#### 四大技術策略

| 策略 | 名稱 | 說明 |
|------|------|------|
| **A** | 多頭延續回檔突破 | MACD 前波紅柱結束後的回檔止跌；前波漲幅需 >= 5%，回檔比例需合理（0～200%）；訊號日 MACD 只接受仍是綠柱或今日剛翻紅，已紅柱多日者應歸策略 B |
| **B** | 強勢紅柱回測突破 | MACD 紅柱期間回測 MA13/MA21 後轉強；需 MACD 紅柱且今日有明確轉強；B3 不要求今日剛突破 MA21 |
| **C** | 低檔背離反轉突破 | MACD 綠柱低檔背離後突破 MA21；KD 只作輔助驗證 |
| **D** | 強勢股急跌收復 | 長均線背景（MA60/MA105）與 MACD 動能同時成立時，急跌後快速收復短均線；屬**高風險短線策略** |

**策略 A 細節**（含基本品質過濾）：
- 波段定義：green_zone 最低價（wave_low）→ red_zone 最高價（wave_high）；red_zone 必須已完成（後續 MACD_HIST 轉回 <= 0）。波段的漲幅（wave_return_pct）需 >= 5%。
- 回檔比例：retracement_ratio 需在合理範圍（0～200%）；異常回檔比例（< 0 或 > 200%）不輸出策略 A。
- 波段日期欄位：wave_start_date、wave_green_end_date（綠柱結束日）、wave_red_start_date、wave_red_end_date（紅柱結束日）、wave_end_date（等同 wave_red_end_date），皆使用實際交易日期，**不可使用 DataFrame index**。
- **MACD 狀態限制**：策略 A 是前波完成後的回檔止跌。訊號日 MACD 只接受：`latest MACD_HIST <= 0`（仍是綠柱）或 `prev MACD_HIST <= 0 且 latest MACD_HIST > 0`（今日剛翻紅）。若 `latest > 0 AND prev > 0`（已紅柱多日），直接排除，不輸出策略 A。
- A1（直接突破型）：前波完成後，直接收復 MA21，允許回檔破前波低。
- A2（轉折不破低）：回檔區間找到兩個 pivot low，第二個不破第一個，今日突破 MA21。
- A3（長均線同日收復）：回檔時 MA105 或 MA144 跌破，今日同日收復 MA21 與 MA105/MA144。
- A4 已移除，不再作為 `/scan` 獨立子訊號。

**策略 B 細節**：
- B1（當日低點碰觸型）：今日低點碰觸 MA13/MA21，收盤重新站上，MACD 紅柱。不要求昨日已在均線下方。
- B2（近 1-3 日跌破收復型）：1-3 日前跌破 MA13/MA21，今日收復，MACD 紅柱。
- B3（紅柱區間回測突破型）：MACD 紅柱期間（使用每日當下均線判斷）回測 MA13/MA21，**最近回測群後首次收盤突破前高**。紅柱期間碰觸 MA13/MA21 的位置，若相鄰 touch 間隔 ≤3 個交易日，視為同一回測群；取最後一個回測群的第一個 touch，其之前的紅柱區間最高點即為「突破前高」。最近回測群結束後到昨天，若已經有任一日收盤大於突破前高，今天不得再觸發 B3（只用收盤價判斷，不用最高價）。不使用第一次回測前高。支援 MA13 與 MA21，優先標示 MA13。**B3 不要求今日剛突破 MA21**；若昨日已站上 MA21，但今日突破最近回測群前高，仍可觸發 B3。6282 康舒 2025-09-12 可作為 B3 案例參考。5351 鈺創 2025-10-07 不符合 B3，因為尚未突破最近回測群前高 40.2。2221 大甲 2026-05-20 不符合 B3，因為 2026-05-14 已收盤突破過前高 42.75，不是首次突破。5425 台半 2026-05-19 可符合 B3，因為 2026-04-29 起漲當天盤中低點跌破 MA60，但收盤站上 MA60，且起漲後沒有收盤跌破 MA60，2026-05-19 收盤突破最近回測群前高。
- B3 的 MA60 排除條件：從紅柱起漲後一日開始檢查，若任一日收盤價 `close < MA60`，才排除 B3。紅柱起漲當天盤中低點 `low < MA60` 但收盤 `close >= MA60` 不排除 B3。不用最低價判斷，只用收盤價。
- B3 紅柱起點判斷：前一日 MACD_HIST≤0 → 當日 MACD_HIST>0 的轉折點。

**策略 C 細節**：
- C1（MACD 低檔背離突破 MA21）：DIF<0，兩個 MACD 綠柱 Zone，Zone2 低點低於 Zone1，Zone2 histogram 最小值高於 Zone1，今日突破 MA21
- C2（0 軸下紅柱鈍化突破）：DIF<0，MACD 紅柱持續 3 日以上，價格在紅柱期間未明顯上漲，今日紅 K 突破 MA21
- C3（KD 背離輔助）：不作為 `/scan` 獨立訊號，僅作為 C1 輔助驗證

**策略 D 細節**（全部含「高風險短線策略」標註；需同時符合長均線背景與 MACD 動能）：
- 長均線背景：收盤站於 MA60/MA105 之上
- MACD 動能背景：DIF>0 或近 5 日有 MACD 紅柱
- 必須同時符合長均線背景與 MACD 動能背景，才進入策略 D 判斷
- D1（近 1-3 日跌破收復型）：1-3 日前跌破 MA13/MA21，今日收復同一條均線，且需 close > prev_close 與紅 K
- D2（MACD 高檔紅柱翻綠後快速反轉）：前日 MACD_HIST>0，今日 MACD_HIST≤0（紅柱翻綠），DIF 仍在 0 軸上方，今日出現快速轉強（站回 MA5/MA13 或紅 K）
- D3（KD 死叉後快速轉強）：近 1-3 日內先發生 KD 死亡交叉（K≥D → K<D），今日出現快速轉強（KD 黃金交叉、紅 K 或站回 MA5/MA13）
- D4（急跌或長下影後收復）：強勢背景 + 近 1-3 日曾跌破 MA13/MA21 或今日長下影線紅 K + 今日收復 MA13（不只 MA5）且為紅 K

#### 策略子訊號顯示

各策略子訊號以繁體中文顯示，不顯示內部英文欄位名或 True/False；格式為「策略 → 分組 → 產業 → 股票」四層結構：

```text
策略 A：多頭延續回檔突破

A3｜同日收復 21MA 與長均線

【半導體業】 3221 台嘉碩 (45.2)｜收復 MA105、收復 MA144

策略 B：強勢紅柱回測突破

B3｜回測 MA13/MA21 後突破前高

【電子零組件業】 6282 康舒 (50.8)｜回測 MA13 後突破前高
【半導體業】 3372 典範 (19.2) | 2330 台積電 (xxx)

策略 D：強勢股急跌收復

D4｜急跌或長下影後收復 MA5/MA13

【電子零組件業】 5678 範例二 (31.2)｜高風險短線策略
```

> **A4 / C3 不輸出**：A4（舊版突破後回測確認型）與 C3（KD 背離輔助）已不作為 `/scan` 獨立訊號，不會顯示在策略分組或 Telegram 報告中；A4 邏輯已移除，C3 僅保留作為未來 Radar 輔助加分參考。

#### 精選選股

- `/scan` 選單中的「精選選股」會一起執行技術面、營收財報與四個法人大戶策略。
- 可用 `/scan 日期` 指定精選選股的技術面與法人資料日期；未輸入日期則使用今天。
- 報告以技術面正面訊號作為主要分類，例如「突破 21MA」「突破 105MA」「MACD 黃金交叉」「KD 黃金交叉」。
- 每個技術面分類內，再依「命中幾個營收/籌碼策略」分組；技術面分類本身不列入命中數。
- 只列出同時命中營收財報或法人大戶 2 個以上策略的股票，並顯示產業、股價、20 日均量、月營收與完整命中策略。

### 籌碼報告格式

- 每份報告都會顯示：標題、日期、最新交易日或最新集保快照、分類定義、S / A / B 三級名單
- 名單格式統一為：股票代號 股票名稱 (最新收盤價)
- 同級別多檔股票以 | 串接；若無標的則顯示 無符合標的

### 推播格式

推播訊息使用 Telegram Markdown V2，格式如下：

```text
💼 【本日庫存籌碼總結】
📅 日期：2026-05-04

🔸 2330 台積電
   外資：+1,500 張 | 投信：+200 張 | 自營商：-300 張
   👉 法人合計：+1,400 張
```

---

## 技術策略後續建議

### 第一階段：人工驗收與參數微調

- 先用真實 `/scan` 結果對照歷史行情，確認策略 A/B/C/D 候選是否符合預期。
- 目前 `/scan` 不以 `technical_setup_score` 作為硬性過濾；`technical_setup_score` 僅作為訊號欄位與未來 Radar 評分參考。若候選太多，後續可在 Radar 階段加入 Top N 或分數門檻。
- 策略 D 屬高風險短線策略，目前 `/scan` 仍列完整名單；後續 Radar 階段可加入每策略 MaxCandidates 控制。

### 第二階段：Radar 功能（後續階段）

Radar 是技術策略名單的延伸功能，第一版已接入 `/radar` 與 `/radar_more`。目前實作重點：

- **名單來源**：從策略 A/B/C/D 候選中選取。
- **日期選擇**：可指定只看特定日期範圍內的候選。
- **每策略上限**：Top 10 或可設定，實作於 Radar 階段。
- **指令**：`/radar` 顯示各策略 Radar 概覽，`/radar_more` 展開完整名單。

### 優化方向

- **效能**：避免 `detect_signals` 與策略偵測各自呼叫 `apply_indicators` 造成重複計算；可將指標結果快取後共享。
- **版本控制**：將 `technical_strategy_engine.py` 與 `tests/test_technical_strategies.py` 納入 Git 版本控制。
- **Prompt marker**：`test_topic_maintain_service.py::test_formal_prompt_topic_maintain_is_not_marker` 失敗屬既有問題，需另案處理。

---

## AI 投研資料中心

research_center/ 是獨立模組，目標是把既有結構化資料抓取、Gemini Search grounding、來源分級、AI 分析、報告輸出與本機 API 串成可擴充的投研資料中心。

### 模組組成

- command_parser.py
  解析 `/research`、`/macro`、`/theme`、`/value_scan`、`/report` 與共用參數，並處理參數衝突。
- data_services.py
  復用既有 data_fetcher、market_summary、stock_scanner 等資料能力，整理成 AI 報告使用的結構化資料；第二版加入 macro 市場分數、theme 供應鏈 profile、value_scan 分項評分。
- gemini_service.py
  使用 Gemini API 與 Google Search grounding 產出 AI 報告；若 API 或搜尋失敗，會改用本地資料 fallback 報告。
- source_rank.py
  依來源網域分成 Level 1 到 Level 5；並整合 `config/preferred_sources.json` 的偏好來源清單，對 official / media / industry / community 進行加權與排序。社群來源（L3）不得單獨支撐高信心題材。
- forum_service.py
  Best-effort 蒐集 PTT Stock、Dcard 與 Mobile01 公開討論來源；論壇只作市場情緒參考。
- date_guard.py
  在 --date 模式過濾晚於報告日或缺少日期的非官方外部來源，降低 look-ahead bias。
- report_builder.py
  同步輸出 Markdown、HTML、JSON 與 sources.json。
- database.py
  使用 SQLite 儲存報告 metadata、來源與 fallback 狀態。
- telegram_handlers.py
  封裝 Telegram 指令處理器，main.py 只負責註冊。
- api_app.py
  提供本機 FastAPI 給龍蝦或其他 Agent 調用。

### 設定檔

`config/research_center.json` 範例：

```json
{
  "model": "gemini-3-pro-preview",
  "fallback_models": ["gemini-3-flash-preview"],
  "enable_grounding": true,
  "report_root": "reports",
  "database_path": "database/stock_research.db",
  "api_host": "127.0.0.1",
  "api_port": 8000,
  "output_formats": ["md", "html", "json"]
}
```

`config/secrets.json` 放 Gemini API Key，格式如下；此檔已被 `.gitignore` 忽略，不應提交：

```json
{
  "gemini_api_key": "YOUR_GEMINI_API_KEY",
  "gemini_model": "gemini-3-pro-preview",
  "gemini_fallback_models": ["gemini-3-flash-preview"]
}
```

### 報告輸出

每次 AI 投研任務會輸出：

- Markdown 完整報告
- HTML 完整報告
- JSON 結構化報告
- sources.json 來源列表
- SQLite reports / sources metadata

預設路徑依類型分到：

```text
reports/stock/
reports/macro/
reports/theme/
reports/value_scan/
database/stock_research.db
```

### 本機 API

啟動 API：

```powershell
.\.venv\Scripts\uvicorn.exe research_center.api_app:app --host 127.0.0.1 --port 8000
```

端點：

- POST `/research`
- POST `/macro`
- POST `/theme`
- POST `/value_scan`
- GET `/reports/{report_id}`
- GET `/stock/{stock_id}/data`

API 預設綁定 `127.0.0.1`，只供本機使用，不需要分享器 port forwarding。若要開給區網或外網，需另外設定 Windows 防火牆、分享器與身份驗證；目前不建議直接開外網。

### 第二版強化與限制

- Gemini Search grounding 已接入，但實際搜尋品質取決於 Gemini API 與公開網頁可用性。
- `--date` 已加入日期護欄：本地結構化資料會切到報告日以前，外部來源若晚於報告日會排除，缺少日期的非 Level 1 外部來源也會保守排除。這比前一版更安全，但仍不是完整回測級事件資料庫。
- 論壇來源已加入 best-effort 蒐集：PTT Stock、Dcard、Mobile01。論壇資料只標示為 Level 4 市場情緒，不可作為事實基礎；若網站改版、阻擋或連線失敗，系統會保留報告產出並在 notes 標示。
- `/macro` 已加入指數均線、廣度、量能與 Market Score，並依分數給建議持股水位；完整台指選擇權波動率、類股資金流、恐懼貪婪分數仍待補強。
- `/theme` 與題材雷達已改用新版題材庫三檔（`theme_profiles.json`、`company_theme_map.json`、`supply_chain_nodes.json`）建立題材、公司關聯與供應鏈參考；舊版 `theme_supply_chain.json` 已封存到 `archive/legacy/config/`，不再作為主要讀取來源。
- `/value_scan` 已加入舊市場標籤、新市場標籤、分項評分與重估證據；仍需後續補產品公告、客戶結構、法人報告摘要與財報細項交叉驗證。
- FastAPI 目前沒有身份驗證，僅建議本機使用。

---
## /export 匯出內容

- Price_History
  提供最近 6 個月交易日的收盤價、成交量、三大法人與融資融券資料。
- Monthly_Revenue
  提供自 2023 年起的月營收歷史。
- Quarterly_Financials
  提供最近 12 季的營收、毛利、營業利益、稅後淨利與 EPS。
- Strategy_Summary
  彙整最新交易日、最近營收、最近季報與資料來源備註。
- 上市三大法人歷史資料優先使用 TWSE T86；單日查詢失敗或查無該股時，會改用 FinMind TaiwanStockInstitutionalInvestorsBuySell 補該股該日外資、投信與自營商資料。

### /export 指令範例

```text
/export 2330
/export 1785
```

## Prompt 模板維護

投研中心提示詞集中在 `prompt/`：

- `prompt/base/`：共用角色與總規則。
- `prompt/report/`：各指令正式分析模板。
- `prompt/discovery/`：搜尋代理模板。
- `prompt/scoring/`：評分模型與量化底稿規則。
- `prompt/rules/`：歷史日期、來源引用、AI 最終評分等共用規則。
- `prompt/topic/`：題材庫維護、Discovery、WebFetch、審核摘要與 `/topic_seed_prompt` 外部初始化模板。

`logs/ai_prompts/` 保存每次實際送給 AI 的完整 prompt 紀錄，不是模板。

舊版 `config/prompts/` 與 `config/scoring/` 已封存到 `archive/legacy/config/`；現行主要維護位置為 `prompt/`。

Prompt 模板以繁體中文維護；JSON key、enum、指令名稱、placeholder（例如 `{report_date}`、`theme_id`、`verified`）需保留原字串，避免破壞程式解析與測試契約。

---

## 監控清單技術掃描

monitor_service.py 目前內建 21MA 突破、MACD 翻紅後回測突破、105MA 突破三種技術訊號；/check、啟動後初始掃描與每日定時掃描會使用 collect_monitor_signals() 掛入的訊號，main.py 只負責觸發與發送 Telegram 訊息。

目前實際啟用的監控訊號為 21MA 突破與 105MA 突破；MACD 翻紅後回測突破函式保留在 monitor_service.py，但尚未掛入 collect_monitor_signals() 執行流程。

> **注意**：以下「舊版回測策略」代號（A/B/C）僅用於 backtest_v1~v4，**不等同於** `/scan` 四大技術策略 A/B/C/D。詳見「技術面選股」章節。

### 舊版回測策略 A：21MA 突破

- 使用 500 日日線資料
- 計算 21MA
- 條件為昨日收盤價低於 21MA，今日現價高於 21MA
- 訊息中會附上前三日最低價作為停損參考

### 舊版回測策略 B：MACD 翻紅後回測突破

- 使用 21 / 55 / 55 參數計算 DIF、DEA、MACD Histogram
- 只在當前為紅柱時檢查
- 找出當前紅柱區間內最近一次低點 <= MA21 的回測日
- 取該回測日之前的最高價作為突破關卡
- 昨日收盤需 <= 回測前高，今日現價需 > 回測前高，且今日現價 > MA21
- 此邏輯已同步到 backtest_v1.py、backtest_v2.py、backtest_v3.py、backtest_v4.py 的策略 B，但監控端目前尚未啟用此訊號

### 舊版回測策略 C：105MA 突破

- 使用 500 日日線資料
- 計算 105MA
- 條件為昨日收盤價低於 105MA，今日現價高於 105MA

### 監控現價來源邏輯

- 技術指標與均線仍使用 Yahoo Finance 的日線資料計算
- 盤中判斷時，先取 Yahoo 的即時欄位
- 若 Yahoo 回傳的報價日期不是今天，改取證交所即時報價端點
- 官方即時報價優先順序為：成交價、委買賣中間價、單邊最優委買價或委賣價
- 若 Yahoo 與官方即時資料都不是今天，則退回前一個交易日收盤價
- Telegram 訊息中的「現價來源」會標示為 Yahoo 盤中、官方成交價、官方委買賣中間價，或前日收盤 fallback

### 非開盤日行為

- 週末、國定假日或其他休市日，不會強制採用官方即時報價
- 若當天沒有有效的今日報價，監控訊號會以最近一個交易日收盤價做判斷
- 此時訊息中的現價來源會顯示為前日收盤 fallback

### 排程與通知

- 啟動後會立即執行一次初始掃描
- 每日台灣時間 12:30 自動執行定時掃描
- 每日台灣時間 13:50 自動推播台股收盤摘要
- 每日台灣時間 08:45、18:00 自動執行新聞整理與推播（查詢最近 24 小時新聞並發送摘要）
- 每日台灣時間 17:45 自動執行個人庫存法人籌碼推播
- 每日台灣時間 16:30 背景回補今日籌碼快取
- 每日台灣時間 18:30 再次背景回補今日籌碼快取
- 每日台灣時間 21:00 背景完整回補近 60 個交易日籌碼快取
- Telegram 訊息有自動分段機制，避免單則超過平台長度限制

### 籌碼背景回補

- 籌碼選股與大戶持股不再定時主動推播，改由 /scan 手動執行。
- 背景回補只整理 .cache/chip_daily 與 .cache/tdcc，不發送選股報告。
- 16:30 與 18:30 只補當日缺口；21:00 會慢速補齊近 60 個交易日，讓隔天 /scan 優先讀快取。
- 背景回補會以策略一到三的資料需求預先整理：先補法人買賣超，再補策略三需要的外資持股比例，目標是晚上手動執行法人選股時盡量直接讀快取。
- 若官方來源被擋或逾時，該來源會進入退避冷卻，期間改讀快取或以 FinMind 補缺口。

---

## 市場摘要推播

market_summary.py 是獨立模組，main.py 只負責註冊指令與排程，市場資料抓取與文字格式化集中在此模組內。

### 晨報

- 啟動時若台北時間落在 06:00 到 09:00，機器人會主動推播晨報。
- 可用 /morning 手動查詢。
- 內容包含美股四大指數最新收盤，以及台指期近月夜盤最新完成盤別的收盤、漲跌與漲跌幅。

### 午報

- 每日台北時間 13:50 自動推播午報。
- 可用 /noon 或 /tw_market 手動查詢。
- 若當日尚無完整台股現貨收盤資料，排程會自動略過，不會推送舊資料。

### 資料來源

- 美股四大指數：Yahoo Finance。
- 台股加權與櫃買指數：TWSE 官方 MIS 指數端點，分別使用加權指數 channel `tse_t00.tw` 與櫃買指數 channel `otc_o00.tw`。
- 台指期近月日盤與夜盤：期交所逐筆成交日檔，依成交時間切分日盤 08:45 到 13:45 與夜盤 15:00 到次日 05:00，再取近月契約最後一筆成交作為收盤。

### 推播格式

晨報範例：

```text
🌅 【晨間市場速報】
📅 日期：2026-05-05

🇺🇸 美股四大指數：
• 道瓊工業：38,888.88 ( +150.20 | +0.40% )
• 標普 500：5,100.50 ( -10.50 | -0.20% )
• 納斯達克：16,200.00 ( -50.00 | -0.31% )
• 費城半導：4,700.00 ( +20.00 | +0.42% )

🇹🇼 台指期 (夜盤收盤)：
• 台指期近月：20,500 ( +80 | +0.39% )
```

午報範例：

```text
📊 【台股收盤總結】
📅 日期：2026-05-05

🇹🇼 台灣現貨指數：
• 加權指數：20,600.00 ( +120.00 | +0.58% )
• 櫃買指數：250.50 ( +1.20 | +0.48% )

🇹🇼 台指期 (日盤收盤)：
• 台指期近月：20,620 ( +100 | +0.49% )
```

---

## 全市場量化選股

stock_scanner.py 會掃描全市場上市與上櫃股票，流程如下：

1. 載入上市櫃股票清單
2. 抓取最近數月營收資料
3. 批次抓取最新價格與 20 日平均成交量
4. 對通過硬篩的股票抓最近 3 季毛利率
5. 依營收分組與毛利分級輸出報告

### 資料來源

- Yahoo Finance / yfinance
  - 最新價格
  - 20 日平均成交量
  - 季損益表，用於計算毛利率
- TWSE / TPEx 官方資料
  - 上市櫃代碼名單 Open API
  - 月營收歷史頁面

註：最新公開的 Open API 快照不足以直接重建最近 4 個月的月營收歷史，所以目前全市場掃描仍保留官方月報頁抓取邏輯。

### 基礎過濾

三項條件必須同時成立：

- 價格：10 < Close < 100
- 20 日平均成交量：大於 500 張
- 最新月營收：大於 50,000,000

### 營收分組

營收第一組：連 4 月成長

- 最近 4 個月 YoY 全部大於等於 1%

營收第二組：動能轉強

- 最近 4 個月至少 2 個月 YoY 大於等於 1%
- 所有衰退月份都必須大於 -5%

### 毛利分級

A 級

- 最近 3 季毛利率皆為正
- 且最新季 > 前一季 > 前兩季

B 級

- 最近 3 季毛利率皆為正
- 至少有 1 次季增
- 所有衰退幅度都小於 5 個百分點

C 級

- 最近 3 季中出現由負轉正
- 且最新一季仍為正

D 級

- 不符合 A、B、C 的其他情況

### /scan 輸出格式

```text
🔍 今日財報營收選股掃描報告
【營收第一組：連4月成長】
A級 (毛利連增): 3702 大聯大
B級 (毛利穩健): 2347 聯強, 8112 至上
C級 (轉虧為盈): (無)
D級 (其他): (無)

【營收第二組：動能轉強】
A級 (毛利連增): (無)
B級 (毛利穩健): 1216 統一
C級 (轉虧為盈): (無)
D級 (其他): (無)

掃描時間：2026-04-18 10:00
掃描範圍：1962 檔，通過硬篩：4 檔
```

---

## /export 匯出功能

/export 會為指定股票建立 Excel 檔，資料由 data_fetcher.py 與 export_service.py 負責組裝。

### 匯出工作表

- Price_History
  最近約 6 個月歷史價格，並合併法人與融資融券資料。
- Monthly_Revenue
  月營收、MoM%、YoY%。
- Quarterly_Financials
  季營收、毛利、營業利益、淨利、EPS。
- Strategy_Summary
  整理後的摘要與抓取備註。

### 個股資料來源

- 上市歷史價量：TWSE 官方日資料
- 上櫃歷史價量：Yahoo Finance 補齊
- 三大法人：TWSE 與 TPEx 官方端點
- 融資融券：TWSE 與 TPEx 官方端點
- 月營收：官方月營收頁面
- 季財報：MOPS Plus API

### 命令列驗證

```bash
python test.py 2330 --preview-rows 3
```

另存 Excel：

```bash
python test.py 2330 --save 2330_export.xlsx
```

---

## 回測腳本

專案目前有四個回測版本，主要用途是策略實驗與績效檢查，並沒有直接整合到 Telegram 指令中。

> **注意**：backtest_v1~v4 的策略代號（A/B/C）僅用於回測環境，**不等同於** `/scan` 四大技術策略 A/B/C/D。

### backtest_v1.py

- 舊版回測策略 A：MA21 突破
- 舊版回測策略 B：MACD 翻紅回測再突破，已同步為「今日第一次突破回測前高」邏輯
- 固定停損與 5% 目標後跌破 MA21 出場

### backtest_v2.py

- 新增舊版回測策略 C：綠柱中的 MA21 突破
- 舊版回測策略 C 停損後，直到 MACD 再次翻紅前不再進場

### backtest_v3.py

- 支援 A、B、C 組合模式，例如 A+B、B+C
- 支援最大持股數限制
- 納入持有期間股息計算
- 策略 B 與 v1/v2/v4 共用同一套「今日第一次突破回測前高」條件

### backtest_v4.py

- 長期回測版本
- 預設從 2018-01-01 開始
- 同樣支援組合模式、最大部位與股息收益

---

## 技術策略測試

### 技術策略單元測試

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_technical_strategies.py
```

此測試涵蓋四大技術策略（A 多頭延續回檔突破、B 強勢紅柱回測突破、C 低檔背離反轉突破、D 強勢股急跌收復）的偵測邏輯、子訊號格式與報告顯示格式；目前應全部通過。

### 全量測試（不含 backup/）

```powershell
.\.venv\Scripts\python.exe -m pytest tests --ignore=backup
```

若 `test_topic_maintain_service.py::test_formal_prompt_topic_maintain_is_not_marker` 失敗，屬既有 prompt marker 問題，與技術策略無關，勿在本任務處理。

---

## 安裝與啟動

### 1. 安裝依賴

```bash
pip install -r requirements.txt
```

### 2. 建立與編輯設定檔

config.json 需要包含：

- api_token
- chat_id
- fugle_api_key（選填，股價/K線第三備援）
- scan_settings
- monitor_stocks

範例：

```json
{
  "api_token": "YOUR_TELEGRAM_BOT_TOKEN",
  "chat_id": "YOUR_CHAT_ID",
  "fugle_api_key": "YOUR_FUGLE_API_KEY",
  "scan_settings": {
    "min_price": 10,
    "max_price": 100,
    "min_avg_volume_20d": 500,
    "min_monthly_revenue": 50000000
  },
  "monitor_stocks": [
    "2330.TW",
    {"symbol": "2317.TW", "name": "鴻海"},
    {"symbol": "2454.TW", "name": "聯發科"}
  ]
}
```

Fugle 也可改用環境變數 `FUGLE_API_KEY` 設定。它目前只作為股價與 K 線第三備援：

- `/stock_chart`：TWSE/TPEX 或 Yahoo 取不到日 K / 分 K 時，改抓 Fugle historical candles。
- `/export`：股價歷史資料官方或 Yahoo 失敗時，改抓 Fugle historical candles。
- `/scan` 與法人大戶候選池：Yahoo 價格與 20 日均量缺資料時，改用 Fugle 補齊。
- `/check` 監控掃描：Yahoo 日線失敗時，改用 Fugle 日 K 補齊。

Fugle 免費方案不提供法人買賣超資料，因此法人籌碼資料仍維持「本機快取 / TWSE / TPEX / FinMind / 估算」。

scan_settings 用來控制 /scan 的硬篩條件：

- min_price：價格下限，條件為價格必須大於此值
- max_price：價格上限，條件為價格必須小於此值
- min_avg_volume_20d：20 日平均成交量下限，單位為張
- min_monthly_revenue：最新月營收下限，單位為元

monitor_stocks 同時支援：

- 純字串格式
- 物件格式，包含 symbol 與 name

### 3. 啟動 Bot

```bash
python main.py
```

Windows 也可直接使用：

- 啟動機器人.bat
- 隱藏啟動.vbs

### Windows 中文亂碼處理

若 CMD / PowerShell 顯示 AI 投研進度時出現中文亂碼，請確認啟動前已設定 UTF-8：

```bat
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
```

`啟動機器人.bat` 已預設加入上述設定。Python 程式檔案請維持 UTF-8 編碼。

若直接用 PowerShell 手動執行，可先輸入：

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

### 4. 單獨執行全市場掃描

```bash
python stock_scanner.py
```

---

## 快取策略

為避免大量重複抓取，stock_scanner.py 內建多層快取：

- stock_list.json
  上市櫃代碼名單快取，24 小時
- .cache/monthly_revenue
  月營收頁面快取，12 小時
- .cache/price_metrics.json
  全市場價格與均量快取，30 分鐘
- .cache/gross_margin.json
  毛利率快取，12 小時
- .cache/chip_daily
  籌碼策略一到三的每日法人資料快取；每個交易日一個 CSV，包含外資買賣超、投信買賣超、可取得的外資持股比例與 source 來源欄位。讀取時先用本機快取，只針對缺日期、缺股票補抓官方或 FinMind；外資持股比例只在策略三或背景預補需要時補抓。
- .cache/tdcc
  籌碼策略四的 TDCC 週快照快取，最多讀取最近 8 週
- 資料源冷卻
  TWSE T86 或 MI_QFIIS 失敗後會依序退避 1 分鐘、5 分鐘、30 分鐘；FinMind fallback 每個交易日最多補 50 檔，並在錯誤時進入冷卻，降低被限流機率。

目前程式已處理兩個常見快取邊界：

- stock_list.json 為空時不可視為有效快取
- price_metrics.json 若未完整覆蓋本次請求範圍，會自動補抓缺漏而非直接沿用

---

## /backfill 完整資料回補

### 背景回補與不中斷指令（2026-05-22）

`/backfill` 現在會以背景任務執行。Telegram 收到指令後會先回覆「已啟動背景完整資料回補」，後續回補在背景繼續跑，完成、略過、停止或失敗時再補發結果訊息。

- 回補執行中仍可使用 `/scan`、`/research`、`/value_scan`、`/macro` 等其他指令。
- 同一聊天室同時間只允許一個手動 `/backfill` 背景任務，避免重複搶資料來源。
- 定時回補也改為背景任務；若上一個定時回補仍在跑，下一次定時檢查會略過。
- `/stop` 仍可停止正在執行的手動或定時回補；停止後不會寫入 complete marker。
- 回補迴圈加入低優先權節流，可用 `config.json` 的 `backfill_throttle_batch_size` 與 `backfill_throttle_sleep_seconds` 調整。

### 缺口導向回補與健康度（2026-05-22）

`/backfill` 已改為缺口導向健康檢查，不只判斷「是否跑過」，也會檢查候選股在各類快取中的覆蓋狀態。

回補完成後會額外寫入：

- `.cache/backfill/YYYY-MM-DD/complete.json`：回補完成標記與各類資料健康度。
- `.cache/backfill/YYYY-MM-DD/gaps.json`：各類資料缺口明細、缺口股票代號與原因。

健康度目前涵蓋：

- 技術面：日 K 是否存在、資料長度是否足夠、是否有成交量與日期欄位。
- 月營收：是否有最近月營收、YoY、至少 4 個月資料。
- 財報 / 毛利率：是否有毛利率快取與足夠季度資料。
- 籌碼法人：近 60 交易日法人資料、外資買賣超、投信買賣超、外資持股比例。
- TDCC 大戶：是否有最近 TDCC 週資料與足夠週數。
- 投研結構化：核心股是否已有 research structured cache。

注意：

- `籌碼覆蓋 60/60 交易日` 只代表近 60 個交易日都有資料，不等於所有候選股都有資料。
- `候選股籌碼覆蓋率` 代表候選股中有多少股票真的在籌碼資料內可用。
- 若覆蓋率不足，`complete.json` 不會被視為完整可用，下次 `/backfill` 或定時回補會重新嘗試補缺口。
- `gaps.json` 會保留 `still_missing` 與 `reason_by_code`，用來判斷哪些股票缺技術面、營收、財報、籌碼、TDCC 或投研結構化資料。

回補健康度顯示範例：

```text
【快取健康度】
- 選股快取可用：否
- 技術面：92%（可用 320/348，缺 28 檔）
- 月營收：88%（可用 306/348，缺 42 檔）
- 財報/毛利率：61%（可用 212/348，缺 136 檔）
- 籌碼法人：80%（可用 278/348，缺 70 檔）
- TDCC 大戶：73%（可用 254/348，缺 94 檔）
- 投研結構化：96%（可用 77/80，缺 3 檔）
- 缺口明細：.cache/backfill/2026-05-22/gaps.json
```

/backfill 指令可預熱所有本地結構化資料快取，讓後續 /research、/scan、/value_scan 等指令可以直接使用快取資料，不需要即時抓取。

若先執行 /backfill，後續 /scan、精選選股、交叉命中與部分投研指令會優先使用本地快取，可減少等待抓取資料的時間。

### 指令格式

```text
/backfill
/backfill 2026-05-15
/backfill force
/backfill 2026-05-15 force
```

- 未指定日期時，自動判斷目標日期：
  - 15:00 前：前一交易日（週一→上週五，週二～五→前一天，週末→週五）
  - 15:00 後：今天（若資料尚未發布則略過）
  - 歷史日期照指定日期使用
- `force` 或 `強制` 或 `強制刷新`：忽略現有快取，強制重新抓取所有資料（含股票宇宙清單）。

### 手動 /backfill 規則

| 情境 | 目標日期 | 行為 |
|------|----------|------|
| 15:00 前 | 前一交易日 | 正常執行 |
| 15:00 後 | 今天 | 若資料未發布則略過（每 2 小時自動重檢）|
| 指定日期 | 照指定 | 正常執行（歷史日期不受時間限制）|
| 已有完整快取 | — | 略過（除非 force）|

### 定時回補規則

定時回補每 2 小時自動檢查一次，使用相同目標日期判斷邏輯：

- 若已有 `.cache/backfill/YYYY-MM-DD/complete.json` marker，跳過。
- 若 15:00 前或資料尚未發布，跳過。
- 若另有回補執行中，跳過。
- `/stop` 可停止手動 `/backfill` 與正在執行的定時回補，會等目前小階段結束再停止，且不寫 complete marker。

### 三層回補流程

完整資料回補會分三層執行，不再對全市場所有股票執行完整投研結構化回補：

1. **全市場輕量回補（Tier 1）**：
   - 月營收快取
   - 價量指標快取
   - 技術日線快取（全部 ~1700 檔）

2. **候選股中量回補（Tier 2）**：
   - 月營收（候選股）
   - 價量快取（候選股）
   - 技術面日線快取（候選股）
   - 毛利率快取
   - 籌碼快取
   - 精選選股快取

3. **核心股完整投研回補（Tier 3）**：
   - 只針對核心股執行 `collect_research_data()`
   - 核心股上限：80 檔（可由 `config.json` 的 `backfill_core_research_limit` 調整）
   - 核心股來源：個人持股 > 監控清單 > 最近掃描 > 最近報告 > 候選股中來源最多者
   - 若核心池為空，則跳過此層
   - 單檔逾時（預設 30 秒，可由 `config.json` 的 `backfill_structured_timeout_seconds` 調整）會跳過，不中斷整個回補

AI 搜尋與 AI 模型分析不會在 /backfill 預先執行，仍會在 /research、/macro、/theme、/value_scan 執行時即時處理。

`/backfill force` 會強制刷新股票宇宙、價量快取與可支援 force 的回補資料。

### 候選池來源

/backfill 會從多個來源建立候選池，然後對候選池內的股票預熱快取：

1. **營收正面硬篩**：最近月營收 YoY > 0。
2. **營收改善硬篩**：最近月營收 YoY 高於前一期 YoY。
3. **價量合理硬篩**：依 `config.json` 的 `scan_settings` 判斷 `min_price`、`max_price`、`min_avg_volume_20d`。
4. **營收規模硬篩**：依 `config.json` 的 `scan_settings.min_monthly_revenue` 判斷。
5. **個人庫存**：`portfolio.json` 中的持股。
6. **監控清單**：`config.json` 的 `monitor_stocks`。
7. **最近掃描結果**：最近 `/scan` 保存的結果。
8. **最近投研報告**：最近 research / value_scan 報告中出現過的股票。

### 回補項目

對候選池內的股票，/backfill 會依序執行以下預熱：

1. **月營收快取**：批次載入候選股月營收歷史。
2. **價量快取**：更新候選股價格與 20 日均量。
3. **技術面日線快取**：更新候選股技術面日線歷史。
4. **核心股完整投研結構化資料快取**：只對核心股（≤80 檔）執行 `collect_research_data()`，其餘候選股不執行。逾時（預設 30 秒）會跳過，不中斷整個回補。
5. **毛利率快取**：更新候選股毛利率序列。
6. **籌碼資料快取**：回補候選股法人與大戶資料。
7. **精選選股快取**：執行精選選股交叉命中，並將結果存入快取供 /value_scan 使用。

### 結構化資料快取機制

- `/research` 執行時會先檢查 `.cache/research_structured/{日期}/{代號}.json` 是否存在且未超過 24 小時。
- 若快取命中，直接使用快取資料，不需要重新抓取。
- 若快取不存在或已過期，會重新抓取並存入快取。
- `/backfill force` 可強制忽略快取，重新抓取所有資料。

### 投研結構化資料進度顯示

回補投研結構化資料時，CMD 會逐檔顯示開始、快取命中、完成、逾時跳過、失敗與耗時：

```text
[完整回補] 核心股完整投研回補開始：80 檔
投研結構化資料 2/80 開始：5425 台半
投研結構化資料 2/80 逾時跳過：5425 台半，超過 30 秒
投研結構化資料 3/80 開始：2330 台積電
投研結構化資料 3/80 完成：2330 台積電，用時 18.3 秒
投研結構化資料 4/80 快取命中：6282 康弘
```

### 回報格式

```text
✅ 完整資料回補完成。
資料日期：2026-05-15

【全市場輕量回補】
- 股票宇宙：1973 檔
- 月營收涵蓋：1800 檔
- 價量資料涵蓋：1900 檔
- 技術日線快取：1700 檔

【候選股中量回補】
- 候選池：156 檔
- 毛利率快取：135 檔
- 籌碼候選：50 檔
- 精選選股快取：12 檔

【核心股完整投研回補】
- 核心股：80 檔
- 完整投研成功：72 檔
- 快取命中：20 檔
- 逾時跳過：3 檔
```

---

## 依賴套件

requirements.txt 目前包含：

- yfinance
- pandas
- python-telegram-bot[job-queue]
- httpx
- pytz
- openpyxl
- lxml
- fastapi
- uvicorn
---

## 已知限制

- 大量抓取仍依賴 Yahoo Finance，可用性受外部服務穩定度影響
- 上櫃歷史價量在目前實作中仍由 Yahoo Finance 補齊，不是全程官方來源
- 回測腳本屬研究工具，與 Telegram Bot 主流程分離，輸出格式與主系統不同
- multitasking.py 目前只有最小實作，並未實際被主程式使用
- AI 投研中心採保守 fallback 設計；Gemini、論壇或公開搜尋失敗時仍會保存本地資料報告，但分析深度會下降
- AI 投研中心第二版已補論壇 best-effort、日期護欄、macro 市場分數、theme 供應鏈 profile 與 value_scan 分項評分；仍需後續補完整事件資料庫、產品/客戶資料與更嚴格的歷史回測級資料治理

---

## AI 投研 Telegram 選單修復（2026-05-20）

- 修復 `/macro`、`/research`、`/theme`、`/value_scan` 在選擇模式後無反應的問題。
- 原因是日期選單 helper `_date_keyboard()` 與分析模型選單 helper `_analysis_model_keyboard()` 曾在題材庫維護選單調整時被誤刪。
- 現在互動流程為：選指令 → 選模式 → 選資料日期 → 選分析模型 → 執行。
- 已補回 `_date_keyboard()` 與 `_analysis_model_keyboard()`，並新增測試防止再次誤刪。

---

## 驗證方式

語法檢查：

```bash
python -m py_compile main.py stock_scanner.py data_fetcher.py export_service.py
```

匯出功能檢查：

```bash
python test.py 2330 --preview-rows 1
```

AI 投研中心測試：

```bash
python -m unittest tests.test_research_center
```

FastAPI route 檢查：

```bash
python -c "from research_center.api_app import app; print([route.path for route in app.routes])"
```
全市場掃描檢查：

```bash
python stock_scanner.py
```

---

## 維護建議

- 若 /scan 結果異常偏少，先檢查 .cache 與 stock_list.json 是否為最新資料
- 若 /export 某些欄位缺漏，優先確認官方端點是否暫時無資料或該公司尚未公告
- 若 Telegram 無法發送，先確認 config.json 的 chat_id 與 api_token 是否正確

---

## AI 投研資料中心第三版補強（2026-05-07）

本次延續前一版未完成項目，採安全、保守、可回滾的方式擴充在 `research_center/` 內，未重寫原本 Telegram Bot 核心流程。

### 新增與強化內容

- `research_center/event_store.py`
  - 將報告使用到的來源轉成事件資料，寫入 SQLite `events` table。
  - `--date` 模式會搭配事件資料庫與來源發布日期治理，逐步降低 look-ahead bias。

- `research_center/macro_indicators.py`
  - `/macro` 新增 VIX proxy、台指選擇權 IV 接入狀態、類股流動性 proxy。
  - 新增系統版 `fear_greed` 分數，使用指數趨勢、VIX proxy、類股流動性集中度組合。
  - 若外部資料失敗，報告會標示資料不足，不中斷產出。

- `config/company_knowledge.json`
  - 新增第一版公司知識庫，用於產品線、客戶結構、營收暴露、供應鏈角色與證據來源。
  - `/theme` 會輸出公司知識覆蓋率與缺漏狀態。

- `research_center/value_validation.py`
  - `/value_scan` 新增 `verification_score` 與 `cross_validation`。
  - 交叉檢查項目包含公告事件、客戶結構、法人報告摘要、財報細項、產品與供應鏈資料。
  - 缺資料時會保守列為 risk flag，不會因重估分數高就提高結論強度。

- `research_center/database.py`
  - SQLite 新增 `events` table 與查詢方法。
  - 報告 metadata 仍保留在 `reports` / `sources`。

- `research_center/api_app.py`
  - FastAPI 已加入 token 驗證。
  - Token 放在 `config/secrets.json` 的 `research_api_token`，此檔已被 `.gitignore` 忽略。
  - 呼叫 API 時可使用：

```powershell
$headers = @{ "X-Research-Token" = "你的 research_api_token" }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/macro -Headers $headers -ContentType "application/json" -Body '{"market_scope":"台股"}'
```

也可使用 Bearer header：

```text
Authorization: Bearer 你的 research_api_token
```

### 目前資料庫

SQLite 目前包含：

- `reports`：報告 metadata。
- `sources`：每份報告使用的來源。
- `events`：來源事件資料，用於未來歷史日期報告、公告追蹤與交叉驗證。

### 仍需持續補強的限制

- 論壇蒐集仍是 best-effort；PTT、Dcard、Mobile01 改版或阻擋時仍可能失敗，但不會中斷報告。
- `--date` 已加入事件資料庫與來源日期治理，但歷史完整度取決於系統累積過多少事件，仍不是完整機構級事件資料庫。
- `/macro` 已有 VIX proxy、類股流動性 proxy、fear/greed 系統分數；正式台指選擇權 IV、期貨籌碼與法人資金流仍需接官方或付費資料源。
- `/theme` 已有公司知識庫架構，但 `config/company_knowledge.json` 需要持續人工或資料源匯入更新。
- `/value_scan` 已加入交叉驗證框架，但公告、法人報告摘要、財報細項仍需要更多來源匯入後，分數可信度才會更高。
- FastAPI 已有 token 驗證，但目前仍建議只綁定 `127.0.0.1` 本機使用；若要對外服務，還需要 HTTPS、反向代理、rate limit、log audit 與權限分級。

### 本次驗證

```powershell
.\.venv\Scripts\python.exe -m py_compile research_center\config.py research_center\database.py research_center\event_store.py research_center\macro_indicators.py research_center\knowledge_base.py research_center\value_validation.py research_center\data_services.py research_center\api_app.py research_center\orchestrator.py tests\test_research_center.py
.\.venv\Scripts\python.exe -m unittest tests.test_research_center
.\.venv\Scripts\python.exe -c "import main; import research_center.api_app as api; print('imports ok', bool(api.app))"
```

測試結果：`14 tests OK`，主程式與 FastAPI 匯入正常，FastAPI token 驗證已確認未帶 token 回 401、帶 token 可進入路由。

---

## AI 投研資料中心第四版補強（2026-05-07）

本次針對上一輪剩餘的三個重點繼續開發：`/macro` 官方公開資料接入、公司知識庫擴充、`/value_scan` 公告與財報細項交叉驗證。

### `/macro` 官方公開資料 connector

新增 `research_center/official_connectors.py`：

- TAIFEX 臺指選擇權波動率指數公開頁 connector。
  - 可解析時回傳 `official_public`。
  - 若頁面可連但表格在本機環境不可解析，回傳 `official_public_reference`，報告會保守標示，不中斷流程。
  - 仍保留 `paid_feed_ready`，未來可接 TAIFEX 智慧資訊商店或其他付費 IV 歷史資料。
- TAIFEX 期貨三大法人公開下載頁 connector。
  - 會 best-effort 解析三大法人期貨交易與未平倉資料。
  - `/macro` fear/greed 會納入外資台指期未平倉偏多/偏空訊號。
- TWSE 三大法人買賣金額 BFI82U connector。
  - 會讀取公開 JSON，納入現貨法人資金流。
  - 本機遇到憑證鏈問題時，connector 會以公開資料 best-effort 方式處理。

`/macro` 現在會輸出：

- `volatility.taifex_option_iv`
- `official_futures_institutional`
- `official_cash_institutional_flow`
- `industry_flow.official_cash_flow`
- 強化後的 `fear_greed`

### 公司知識庫擴充

更新 `config/company_knowledge.json`：

- 擴充為 `starter_manual_database_expanded`。
- 增加更多 AI 伺服器、半導體、重電、電線電纜相關公司範例。
- 每家公司維護：
  - `product_lines`
  - `customers`
  - `revenue_exposure`
  - `supply_chain_roles`
  - `evidence_sources`
  - `confidence`
  - `updated_at`

使用原則：若公司沒有明確公開客戶，請填「全球品牌客戶」、「雲端服務客戶」、「資料中心供應鏈」這類公開分類，不要填未授權或未驗證的客戶名稱。

### `/value_scan` 公告與財報細項交叉驗證

新增 `research_center/mops_sources.py`，並強化 `research_center/value_validation.py`：

- 每次 `/value_scan` 的前段候選股會建立 MOPS 官方查詢事件：
  - `mops_material_reference`
  - `mops_announcement_reference`
  - `mops_connectivity_check`
- 這些事件會寫入 SQLite `events` table，供後續歷史報告與交叉驗證使用。
- 前段候選股會嘗試抓取季度財報，建立 `financial_detail` snapshot。
- `cross_validation` 現在會區分：
  - `verified`：已有可用明細或足夠結構化資料。
  - `partial`：有官方入口、連線檢查或部分欄位。
  - `missing`：缺少可用證據。

法人報告摘要目前仍保留 `broker_report_reference` 事件型別，但不自動抓取或整理未授權研報。若未來有合法來源、付費 API 或你自己的研報檔案，可以接到同一套事件與驗證框架。

### 本次最小化連線驗證

```text
TAIFEX VIX: official_public_reference
TWSE 三大法人現貨: official_public
TAIFEX 期貨法人: official_public
```

`TAIFEX VIX` 在目前本機環境可連到公開頁，但表格值不可解析，因此保守回傳 reference 狀態。這已比前版更安全：報告知道官方來源存在，但不會把無法解析的值硬當作 IV 數字。

### 本次測試

```powershell
.\.venv\Scripts\python.exe -m py_compile research_center\official_connectors.py research_center\mops_sources.py research_center\macro_indicators.py research_center\value_validation.py research_center\event_store.py research_center\orchestrator.py research_center\data_services.py tests\test_research_center.py
.\.venv\Scripts\python.exe -m unittest tests.test_research_center
.\.venv\Scripts\python.exe -c "import main; import research_center.api_app as api; print('imports ok', bool(api.app))"
```

結果：`17 tests OK`，主程式與 FastAPI 匯入正常。

### 仍需後續補強

- TAIFEX VIX 若要完整歷史 IV、日內 IV 或穩定欄位，建議接 TAIFEX 智慧資訊商店或其他正式付費資料源。
- 期貨法人資料目前是公開頁 best-effort parser，官方頁欄位變動時會降級，不中斷報告。
- 類股「資金流」目前仍不是逐產業法人買賣超，類股層級還是用本地 20 日均量 proxy；若要精準，需要接逐股法人買賣超後再依產業彙總。
- `company_knowledge.json` 已擴充，但仍是人工 starter database，後續要持續補每家公司產品、客戶分類、營收占比與供應鏈關係。
- MOPS 目前建立官方查詢事件與連線檢查，尚未完整解析每一筆公告內文；若官方表單欄位或反自動化機制允許，後續可再做明細 parser。
- 法人報告摘要仍需要合法資料來源或你提供的本地研報檔，系統目前不會自動抓取付費/授權限制內容。

---

## AI 投研進度顯示與模型備註（2026-05-07）

執行 `main.py` 後，當 Telegram 收到 AI 投研指令時，CMD 視窗會顯示目前處理進度。適用指令包含：

- `/research`
- `/macro`
- `/theme`
- `/value_scan`
- `/report`

CMD 會看到類似：

```text
[2026-05-07 14:30:01] [AI投研] /research 2330 --deep | 收到 Telegram AI 投研指令
[2026-05-07 14:30:01] [AI投研] /research 2330 --deep | 解析 AI 投研指令
[2026-05-07 14:30:02] [AI投研] /research 2330 --deep | 開始收集結構化資料與外部來源
[2026-05-07 14:30:20] [AI投研] /research 2330 --deep | 呼叫 AI 模型中：gemini-3-pro-preview
[2026-05-07 14:31:10] [AI投研] /research 2330 --deep | AI 模型回應完成：gemini-3-pro-preview
[2026-05-07 14:31:12] [AI投研] /research 2330 --deep | AI 投研任務完成：research_...
```

Telegram 摘要訊息也會在有調用 AI 模型時附上模型名稱，例如：

```text
AI 模型：gemini-3-pro-preview
```

若使用 `--source-only`，Telegram 會標示未調用 AI；若 AI 呼叫失敗並改用本地 fallback，也會在 Telegram 訊息中標示 fallback 原因。
fallback 訊息會依照實際選擇模型顯示，例如 MiniMax 失敗會顯示「MiniMax 調用失敗」，並附上 HTTP status、prompt 長度或 payload 大小等可用診斷。

本次驗證：

```powershell
.\.venv\Scripts\python.exe -m py_compile research_center\models.py research_center\orchestrator.py research_center\telegram_handlers.py main.py
.\.venv\Scripts\python.exe -m unittest tests.test_research_center
.\.venv\Scripts\python.exe -c "import main; import research_center.api_app as api; print('imports ok', bool(api.app))"
```

結果：`17 tests OK`，主程式與 FastAPI 匯入正常。

---

## AI 投研規格化 Prompt 與評分引擎更新（2026-05-07）

本次已依照最新規格文件與兩份原始評分模型，將 AI 投研指令的 prompt、輸出章節、評分邏輯、資料來源規則與禁止事項拆成可維護的設定檔與程式合約。

> **注意**：此段為 2026-05-07 歷史紀錄；現行主要 prompt 維護位置已改為 `prompt/`，舊版 `config/prompts/` 與 `config/scoring/` 已封存到 `archive/legacy/config/`。

### 新增與調整內容

- `archive/legacy/config/prompts/`
  - `base.md`：所有 AI 投研指令共用規則，包含資料可信度 Level 1～5、不得捏造、資料不足需明示、論壇只能作情緒參考、禁止保證獲利與自動下單等限制。
  - `research_summary.md`、`research_score.md`、`research_deep.md`：對應 `/research` 一般、評分、深度模式。
  - `macro.md`、`macro_deep.md`：對應 `/macro` 一般、brief、deep 模式。
  - `theme.md`、`theme_deep.md`：對應 `/theme` 一般、深度模式。
  - `value_scan.md`、`value_scan_deep.md`：對應 `/value_scan` 一般、深度模式。
  - `source_only_summary.md`、`telegram_summary.md`：保留資料彙整與 Telegram 摘要用途。
- `archive/legacy/config/scoring/`
  - 已放入原始評分模型：`股票標籤重估模型.md`、`股票量化評分標準.md`。
  - `/research --score`、`/research --deep`、`/value_scan` 會把相關評分原稿帶入 AI prompt。
- `research_center/prompt_registry.py`
  - 統一管理「指令 + 模式」對應的 prompt 模板。
  - 自動組合 base rules、指令模板、模式補充、評分原稿、指令 JSON、結構化資料與來源列表。
  - 對外提供 `prompt_metadata()`，讓報告 JSON 可記錄本次使用的 prompt 與評分檔。
- `research_center/gemini_service.py`
  - AI 呼叫前會使用新的 prompt registry 組裝完整 prompt。
  - 保留舊呼叫方式相容性，並補強 `report_date` 字串相容處理。
- `/report`
  - 未帶參數時，預設列出最近報告清單，不呼叫 AI。
  - 支援 `/report 6217 latest`、`/report 6217 2026-05-06`、`/report macro latest`、`/report theme AI伺服器 latest`。
- 指令預設值
  - `/macro` 未指定市場時，預設為「全球」，並以 global region 處理。
  - `/theme` 未指定 `--top` 時預設前 10 名。
  - `/value_scan` 未指定 `--top` 時預設前 10 名。
- Telegram / CMD
  - AI 投研指令執行時，CMD 會顯示處理進度。
  - 若有呼叫 Gemini，Telegram 訊息會標示實際模型，例如 `gemini-3-flash-preview`。
  - `--source-only` 與 `/report` 查詢既有報告不會呼叫 AI。

### AI Prompt 觸發規則

- `/research 股票`
  - 呼叫 AI，使用個股研究摘要 prompt。
- `/research 股票 --score`
  - 呼叫 AI，使用個股評分 prompt，並附上兩份原始評分模型。
- `/research 股票 --deep`
  - 呼叫 AI，使用深度個股 prompt，並附上兩份原始評分模型。
- `/research 股票 --source-only`
  - 不呼叫 AI，只整理本地與公開來源資料。
- `/macro [市場] [主題]`
  - 呼叫 AI，使用總經與市場情緒 prompt。
- `/macro --brief`
  - 呼叫 AI，但 Telegram 摘要更短。
- `/macro --deep`
  - 呼叫 AI，加入深度總經、風險分數與持股水位要求。
- `/theme 題材`
  - 呼叫 AI，使用題材與供應鏈 prompt。
- `/theme 題材 --deep`
  - 呼叫 AI，加入產品、客戶、營收占比、供應鏈角色與反證要求。
- `/value_scan [候選池]`
  - 呼叫 AI，使用價值重估掃描 prompt，並附上標籤重估與量化評分原稿。
- `/value_scan --deep`
  - 呼叫 AI，加入完整交叉驗證、舊標籤/新標籤與反證要求。
- `/report ...`
  - 不呼叫 AI，只查詢 SQLite 既有報告與檔案。

### 測試

本次新增 `tests/test_prompt_contracts.py`，用來固定以下規格合約：

- `/research --score` prompt 必須載入原始評分模型。
- `/value_scan` prompt 必須使用前 10 名預設，並載入重估模型與量化評分原稿。
- `/macro` 與 `/report` 預設行為符合最新規格。
- SQLite 最近報告查詢支援指定日期。

已通過檢查：

```powershell
.\.venv\Scripts\python.exe -m py_compile research_center\prompt_registry.py research_center\gemini_service.py research_center\orchestrator.py research_center\command_parser.py research_center\database.py research_center\telegram_handlers.py research_center\data_services.py research_center\api_app.py tests\test_prompt_contracts.py tests\test_research_center.py main.py
.\.venv\Scripts\python.exe -m unittest tests.test_prompt_contracts tests.test_research_center
.\.venv\Scripts\python.exe -c "import main; import research_center.api_app as api; print('imports ok', bool(api.app))"
```

結果：`21 tests OK`，`imports ok True`。

### 仍需持續補強

- 規格中的嚴格歷史回測仍需要完整事件資料庫、各來源發布時間與防偷看未來機制，目前已做保守日期過濾。
- 論壇資料仍屬 best-effort，PTT / Dcard / Mobile01 若改版或阻擋，報告會降級但不中斷。
- `company_knowledge.json` 仍是 starter knowledge base，需要長期補產品、客戶、營收占比與供應鏈關係。
- `/macro` 的正式台指選擇權 IV、完整期貨籌碼、法人資金流仍需要官方可穩定下載來源或付費資料源。
- `/value_scan` 的公告、法人報告、財報細項交叉驗證框架已存在，但仍需要更多來源匯入與人工校正。

---

## AI 投研規格落差補齊更新（2026-05-07）

本次依照規格文件補齊先前盤點的可開發落差，保留指定行為：`/report` 列最近報告清單，`/report latest` 回傳最新單一報告。

### 已補齊

- `/report` 行為
  - `/report`：列出各類型最近報告清單。
  - `/report latest`：查詢最新單一報告。
  - `/report 6217 latest`、`/report macro latest`、`/report theme AI伺服器 latest`、`/report value_scan latest` 維持可用。
- 輸出格式控制
  - 新增 `--no-html`、`--no-md`、`--no-json`。
  - 不允許三種格式全部關閉。
  - `sources.json` 仍會輸出，保留來源追蹤。
- 本地量化底稿引擎
  - 新增 `research_center/scoring_engine.py`。
  - `/research` 會依本地可量化資料產生營益率、營收成長性、EPS、獲利成長、自由現金流、存貨週轉、法人資金流、谷底翻轉、技術籌碼與綜合量化評分。
  - `/value_scan` 會將重估分與證據覆蓋分依權重合成結構化分數。
  - `/macro` 會輸出市場恐懼貪婪分數。
  - `/theme` 會輸出供應鏈知識庫覆蓋度。
- JSON schema 強化
  - `report_json` 現在會填入 `scores`，不再固定空陣列。
  - `sections` 會解析章節內容，並抓取 `[S001]` 這類來源引用到 `evidence_sources`。
  - `risks`、`positive_factors`、`watch_items` 會從更多章節關鍵字中抽取。
- 報告 QA validator
  - 新增 `research_center/report_validator.py`。
  - 會檢查必要 schema key、章節缺漏、來源章節、來源引用、禁止語句與評分資料。
  - QA 結果會寫入 JSON metadata 的 `qa_validation`。
  - 若 AI 報告缺章節或來源，Markdown 會追加「規格檢查提醒」。
- Telegram 摘要
  - `summarize_for_telegram()` 已改成接近 `telegram_summary.md` 的固定格式：標題、總結、關鍵判斷、完整報告提示。
  - 保持 1200 字上限，不貼完整報告。
- Logging
  - 新增 `research_center/research_logger.py`。
  - AI 投研任務會寫入 `logs/app.log`、`logs/task.log`、`logs/error.log`。
  - Gemini 失敗與任務完成會記錄可追蹤 metadata。
- FastAPI 預設值
  - `/macro` API 預設市場改為「全球」。
  - `/value_scan` API 預設候選池改為「精選選股」。

### 新增測試

- `tests/test_report_schema.py`
  - 驗證本地量化底稿會寫入 JSON `scores`。
  - 驗證 QA validator 能記錄缺少章節與來源引用。
  - 驗證 `--no-html --no-json` 只輸出 Markdown 與 sources。
- `tests/test_prompt_contracts.py`
  - 補 `/report latest` 與輸出格式 flags 測試。

已通過檢查：

```powershell
.\.venv\Scripts\python.exe -m py_compile research_center\command_parser.py research_center\orchestrator.py research_center\report_builder.py research_center\scoring_engine.py research_center\report_validator.py research_center\research_logger.py research_center\api_app.py tests\test_prompt_contracts.py tests\test_report_schema.py
.\.venv\Scripts\python.exe -m unittest tests.test_prompt_contracts tests.test_report_schema tests.test_research_center
.\.venv\Scripts\python.exe -c "import main; import research_center.api_app as api; from research_center.command_parser import parse_command_text; print('imports ok', bool(api.app), parse_command_text('/report latest').target)"
```

結果：`25 tests OK`，`imports ok True latest`。

### 仍需持續補強

- 本地量化底稿引擎已依規格建立，但沒有資料的項目會保守給分；例如 CAGR、護城河、完整產品營收占比仍需要資料庫補齊。
- QA validator 會檢查並提示缺漏，但目前不會自動二次呼叫 Gemini 重寫報告，以避免增加 token 與成本。
- 正式台指選擇權 IV、完整期貨籌碼、完整法人報告內容仍需要穩定官方/付費/授權資料源。

---

## AI 投研免費資料源補強更新（2026-05-07）

本次補上第一批可免費取得或可由本地快取使用的外部資料，並接入 `/research`、`/macro`、`/value_scan` 的結構化資料與評分流程。

新增資料模組：

- `research_center/free_sources.py`：集中管理免費公開資料與本地快取讀取。
- TWSE/TPEx 官方估值資料：嘗試取得本益比、股價淨值比、殖利率。
- TDCC 集保股權分散表快取：讀取 `.cache/tdcc/*.csv`，建立大戶級距、散戶級距與集中度訊號。
- 毛利率本地快取：讀取 `.cache/gross_margin.json`，提供毛利率趨勢驗證。
- MOPS 官方文件入口：年報與法說會查詢連結會放入結構化資料，作為官方來源入口。
- TWSE 類股/大盤公開資料：供 `/macro` 作為類股指數輔助資料。

指令整合：

- `/research`：報告資料會包含 `free_public_sources`、`valuation_data`、`tdcc_data`、`gross_margin_cache`、`mops_documents`。
- `/macro`：新增 `industry_index_data`，補 TWSE 類股指數公開資料狀態。
- `/value_scan`：前段候選股會補估值、TDCC、毛利率快取與 MOPS 官方文件入口，交叉驗證分數會納入這些免費證據。

評分補強：

- 單股研究新增 `TDCC 籌碼集中度`、`估值安全邊際`、`毛利率快取驗證` 三個本地量化底稿項目。
- 價值重估候選股合成分改為：重估分 60%、證據覆蓋分 25%、TDCC 10%、估值 5%。
- `value_validation` 新增 TDCC、官方估值、毛利率快取三項 evidence coverage 檢查。

限制說明：

- 免費來源皆為 best-effort；官方網站改版、阻擋、欄位變動或無資料時，系統會回傳 `unavailable`、`empty` 或 `official_reference`，不會中斷報告產出。
- MOPS 年報與法說會目前先提供官方入口與查詢參數，PDF/HTML 逐公司深度解析仍屬後續加強。
- 正式台指選擇權 IV、完整期貨籌碼、授權法人報告內容仍需要官方授權、付費資料源或使用者自行提供合法資料。

---

## AI 投研歷史快照與中文互動選單更新（2026-05-07）

本次依照最新需求補強 AI 投研中心的歷史日期治理、Gemini 使用規則、報告評分輸出、value_scan 候選名單流程與 Telegram 中文互動選單。

### 歷史日期 `--date` 補強

新增 `source_snapshots` 快照資料表與 `research_center/source_snapshots.py`：

- 每次報告會把來源 URL、標題、來源等級、發布日期、抓取時間、報告日期與來源摘要寫入 snapshot。
- `--date` 模式會查詢該日期以前已保存的 snapshot。
- 歷史日期模式會停用 Gemini Search / grounding，不直接搜尋現在網路。
- Gemini 在歷史模式只能整理本地結構化資料與 historical snapshots；如果快照不足，必須標示資料不足，不得用現在資訊補推。

注意：歷史回測級完整度取決於系統是否長期累積 snapshot。第一次查很久以前的日期時，資料可能偏少。

### Gemini Search 使用規則

非歷史模式下，Gemini prompt 會依指令要求補強搜尋：

- `/research --score`、`/research --deep`：補找 CAGR、護城河、轉型效益、題材熱度、MOPS、年報、法說會與新聞來源。
- `/theme`：補找產品線、客戶分類、供應鏈角色、營收占比與證據來源。
- `/value_scan`：補找公告、法說、年報、產品、客戶、產業題材、新聞與反證。
- `/macro`：補找免費公開宏觀資料與新聞脈絡，但正式 IV / 籌碼數字仍以官方或結構化資料為準。

Gemini grounding citations 會併入 `sources.json`、SQLite sources 與報告來源清單。

### AI Prompt Logging

新增 `research_center/prompt_logging.py`：

- 完整 prompt 會保存到 `logs/ai_prompts/`。
- CMD 會顯示 prompt 檔案路徑、template、模型、prompt 長度、grounding 是否啟用與來源數。
- prompt log 不會放進報告，也不會傳到 Telegram。

### `/research --score` 推薦買入評分

新增本地結構化 `buy_rating`：

```json
{
  "score": 3,
  "max": 5,
  "label": "中性觀察",
  "reason": "本地量化評分平均換算",
  "risk": "低分項與資料不足提醒"
}
```

此分數是研究輔助，不構成投資建議或自動買入訊號。

### Fallback Markdown 完整評分

本地 fallback 報告已不再只顯示前 12 項本地量化底稿項目，會列出完整本地量化底稿。JSON 仍保留完整 `scores` 與 `buy_rating`。

### 論壇來源 CMD 進度

PTT、Dcard、Mobile01 搜尋會在 CMD 顯示：

- 搜尋開始
- 成功來源數
- notes 數量
- individual failure note / blocked / unavailable reason

論壇資料仍是 Level 4，只能作市場情緒參考。

### `/value_scan` 候選名單流程

`/value_scan` 現在將「名單來源」與「分析模式」分離；一般模式最多送 AI 分析前 10 檔，深度模式最多前 30 檔，避免候選池過大造成成本與品質失控。

名單來源：

- 精選選股：優先使用精選選股快取；沒有快取時調用主程式精選選股交叉命中取得候選名單，再做本地重估排序。
- 選股雷達：讀取最近一次或指定日期的 `/radar` 快取候選名單，不重新執行 Radar，適合把近期技術/籌碼/題材發動股再做價值重估。
- 我的持股：讀取 `portfolio.json`。
- 監控清單：讀取 `config.json` 的 `monitor_stocks`。
- 最近掃描結果：列出已保存的最近 `/scan` 結果供選擇；若沒有快取，會提示先執行 `/scan`。
- 自訂股票清單：使用者輸入股票代號清單。
- 單一股票：提示輸入股票代號或名稱，例如 `6282` 或 `康舒`，再套用同一套模式/日期/模型流程。
- 全市場初篩：讀取上市櫃 universe 後做本地重估排序；此來源較重，建議優先使用精選選股、選股雷達、持股或監控清單縮小候選池。

分析模式說明：

- 一般重估：本地排序後，AI 最多分析前 10 檔。
- 深度重估：本地排序後，AI 最多分析前 30 檔。
- source-only：不呼叫 AI，只整理來源與本地底稿。
- `--top N` 是進階參數，只影響本地排序或顯示數量，不代表全部 N 檔都送入 AI。

分析模式：

- 一般重估
- 深度重估
- 只看資料來源

### Telegram 中文互動選單

若輸入完整指令，例如 `/research 2330 --score --date 2026-01-15`，會直接執行。

若輸入簡短指令，會進入互動式中文選單。

`/research 2330`：

1. 選研究模式：一般研究、深度研究、量化評分、只看資料來源。
2. 選資料日期：最新日期、指定日期。
3. 若選指定日期，輸入 `YYYY-MM-DD` 後執行。

`/value_scan`：

1. 選股票名單來源：精選選股名單、全市場初篩、我的持股、自訂股票清單、最近掃描結果。
2. 選分析模式：一般重估（AI 最多分析 10 檔）、深度重估（AI 最多分析 30 檔）、只看資料來源（不呼叫 AI）。
3. 選資料日期：最新日期、指定日期。

### SQLite 復原保護

若 SQLite 初始化遇到 Windows `disk I/O error` 或無法開啟資料庫，程式會嘗試保留舊資料庫並建立可用資料庫；若目前執行環境仍無法寫入 SQLite，會以 in-memory fallback 讓 bot 先啟動。正式執行時建議確認 `database/` 可寫，才能永久保存 reports、sources、events 與 snapshots。

### 本次驗證

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_free_sources tests.test_research_center_new_features
.\.venv\Scripts\python.exe -B -c "import main; import research_center.api_app as api; from research_center.telegram_handlers import AI_CALLBACK_PREFIX; print('imports ok', bool(api.app), AI_CALLBACK_PREFIX)"
```

結果：新增功能測試 `6 tests OK`，主程式與 FastAPI import 正常。

---

## AI 投研未完成項補齊更新（2026-05-07）

本次針對上一輪尚未完整完成的項目繼續補強，範圍限於免費公開資料、現有本地資料與 Gemini Search 可協助整理的內容；正式付費或授權資料源仍保留接口，不自動抓取。

### 最近掃描結果保存與 `/value_scan` 接入

新增 `research_center/recent_scans.py`：

- `/scan` 產生精選選股報告後，會保存最近掃描結果到 `.cache/recent_scan_results.json`。
- 保存內容包含：掃描類型、日期、建立時間、股票代號清單、候選數與摘要。
- `/value_scan` 選擇「最近掃描結果」時，會列出最近保存的掃描結果，例如：

```text
[精選選股 2026-05-07，18 檔]
```

選定後，系統會用該掃描結果的股票名單做本地重估排序，再依以下規則送 AI 分析：一般模式最多 10 檔候選股、深度模式最多 30 檔候選股、source-only 模式不呼叫 AI。`--top N` 只影響本地排序或顯示數量，不代表全部 N 檔都送入 AI。

### `/macro` 與 `/theme` 中文互動選單

除了 `/research` 與 `/value_scan`，本次也補上：

`/macro`：

1. 選市場範圍：全球、台股、美國、中國、歐洲、亞洲、手動輸入市場範圍。手動輸入只作為宏觀市場範圍，不會改成 `/theme` 供應鏈題材報告。
2. 選分析模式：快速總覽、一般宏觀、深度總經、只看資料來源。
3. 選日期：最新日期、指定日期。

`/theme 題材`：

1. 選分析模式：一般題材、深度題材、只看資料來源。
2. 選日期：最新日期、指定日期。

若使用者直接輸入完整指令，例如 `/theme AI伺服器 --deep --top 30`，仍會直接執行，不進選單。

### MOPS 免費 parser 補強

`research_center/mops_sources.py` 已由「官方入口與連線檢查」進一步補強為 best-effort parser：

- 嘗試讀取 MOPS 重大訊息頁。
- 嘗試讀取 MOPS 公告查詢頁。
- 使用 HTML table parser 擷取可用列資料。
- 成功時建立 `mops_material_parsed` 或 `mops_announcement_parsed` 事件。
- 失敗時保留 `parse_status` 事件與錯誤原因，不中斷報告。

限制：MOPS 表單、PDF、驗證與欄位常變動，因此仍屬 best-effort。若要機構級穩定，後續可接官方下載檔、固定公告 API 或人工匯入資料。

### `/macro` 免費公開資料最大化

`research_center/macro_indicators.py` 新增 `global_public_macro`：

- USD/TWD 匯率 proxy
- US10Y proxy
- NASDAQ
- S&P 500
- SOX 半導體指數
- WTI 原油
- Gold 黃金

這些資料使用 Yahoo Finance 公開市場 proxy，搭配既有 TWSE、TAIFEX、Global VIX、台股指數與類股流動性 proxy。正式 IV、日內選擇權、完整籌碼與逐產業法人資金流仍需官方穩定檔或付費資料源。

### Gemini 知識沉澱草稿

新增 `research_center/knowledge_drafts.py`：

- 對 `/research`、`/theme`、`/value_scan` 的 AI 報告抽取產品、客戶、供應鏈、營收占比、CAGR、護城河、轉型、題材、公告、法說與反證等線索。
- 抽取結果保存到 `logs/knowledge_drafts/`。
- 草稿狀態為 `draft_requires_review`，不會自動覆寫 `company_knowledge.json`。

這樣做的原因是：Gemini Search 可以協助找與整理，但正式知識庫仍應經人工或規則審核，避免把未確認資訊長期固化。

### CAGR、護城河、轉型效益、題材熱度

目前已做到：

- Prompt 明確要求 Gemini Search 補找這些資料。
- 若沒有明確來源，不得給高分。
- AI 報告中的相關線索會保存為 knowledge draft。
- 本地量化底稿仍維持保守，不會只因 AI 推測就提高分數。

下一步若要更嚴格，可把 knowledge draft 審核後轉入 SQLite company knowledge table，再讓本地量化底稿引擎直接讀取結構化欄位。

### 本次驗證

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_free_sources tests.test_research_center_new_features
.\.venv\Scripts\python.exe -B -c "import main; import research_center.api_app as api; from research_center.telegram_handlers import AI_CALLBACK_PREFIX; print('imports ok', bool(api.app), AI_CALLBACK_PREFIX)"
```

結果：`8 tests OK`，主程式與 FastAPI import 正常。

### 仍保留的硬限制

- 正式付費/授權法人報告不自動抓取。
- 正式台指選擇權完整 IV、日內資料、波動率曲面仍需 TAIFEX 或授權資料源。
- MOPS PDF/年報逐頁全文解析仍可能受格式、驗證與反自動化影響，目前採 best-effort。
- Gemini Search 產生的知識只進草稿，不自動寫入正式知識庫。

---

## AI 投研規格比對結論（2026-05-07）

本段整理目前程式與最新規格文件的比對結果，區分為「已對應開發」、「仍有程式落差」與「資料源限制」。

### 已對應開發

- 已建立 `research_center/` 模組，包含指令解析、SQLite、報告產生、來源分級、Gemini 服務、FastAPI、Telegram handler 與基本測試。
- 已支援 `/research`、`/macro`、`/theme`、`/value_scan`、`/report`，並加入中文第二層選單流程。
- `/report` 保持目前約定：`/report` 列最近報告清單，`/report latest` 回傳最新單一報告。
- 已依規格拆出 prompt 模板與評分原稿，AI 呼叫會依不同指令、模式、日期與評分需求組合提示詞。
- 已加入 Gemini Search / grounding，非歷史模式會允許 AI 查找公開資料；歷史日期模式會關閉 grounding，避免混入現在才可見的資料。
- 已加入 AI prompt logging，實際送給 AI 的 prompt、模型、是否 grounding、來源數量與 log 檔路徑會輸出到 CMD，不放進報告正文。
- 已加入 CMD 處理進度顯示，論壇來源成功、失敗、被阻擋或無資料時也會顯示狀態，不會讓報告流程靜默卡住。
- 已加入 Telegram AI 訊息的模型備註；若有調用 Gemini，會標註使用模型，若是 fallback 或純資料來源也會標示。
- 已加入 `/research --score` 的本地量化底稿中的機械式買入分數 `buy_rating`，以 1 到 5 分呈現，並在 JSON 與 fallback Markdown 中保留；此分數不是 AI 最終投研評分。
- fallback Markdown 已改成列出完整評分項目，不再只顯示前 12 項；JSON 仍保留完整 `scores`。
- `/value_scan` 已支援精選選股名單、全市場初篩、我的持股、自訂股票清單、最近掃描結果等股票池，並先做本地初篩排序，再依一般模式（最多 10 檔）或深度模式（最多 30 檔）送 AI 分析。
- 已加入最近掃描結果快取，讓 `/value_scan` 可以沿用最近一次精選選股結果。
- 已擴充免費公開資料來源，包括 TWSE/TPEx 估值、TDCC、毛利率快取、MOPS 入口/事件參考、TWSE 產業指數、TAIFEX/TWSE best-effort、Yahoo Finance 公開總經代理資料等。
- 已加入 `source_snapshots` 與歷史 snapshot 雛形，讓 `--date` 可以讀取指定日期以前保存過的來源快照。
- 已加入 Gemini 產出的公司知識草稿 `logs/knowledge_drafts/`，用於累積產品、客戶、供應鏈、營收占比、CAGR、護城河、轉型效益與題材熱度等資料線索。

### 仍有程式落差

以下不是付費資料源限制，而是若要完全貼齊規格文件，後續仍可再補的程式行為：

- 規格提到 `/help`，目前 AI 投研說明主要是 `/ai_help` 與 `/start`，尚未把完整 AI 投研說明掛成正式 `/help` alias。
- AI 投研邏輯雖然已集中在 `research_center/`，但舊有 `main.py` 仍保留大量既有掃描與 Telegram 流程，尚未嚴格整理成「main.py 只做啟動與註冊」。
- `/value_scan` 在某些情境下會採用 fallback 股票池；若要完全照規格，可改成沒有精選選股結果時明確提示先執行掃描，而不是自動放寬到全市場。
- QA validator 目前會檢查章節、來源、禁止事項與格式，但尚未做到「檢查失敗後自動要求 Gemini 重寫直到合格」的完整 retry 流程。
- `--date` 已有快照資料庫與歷史模式限制，但尚未加入每日自動快照排程；沒有長期累積快照前，歷史回測完整度仍受限。
- Gemini 整理出的知識目前先寫入草稿檔，尚未自動審核並併入正式 `company_knowledge.json` 或公司知識表。

### 資料源限制

以下屬於免費公開資料難以完全補齊，或需要長期累積、授權資料、正式 API 才能達到規格中的機構級品質：

- MOPS 目前有官方入口、事件參考與 best-effort HTML table parser，但尚未穩定解析所有公司公告、年報 PDF、法說會 PDF/影音與歷史全文。
- 正式台指選擇權 IV、完整波動率曲面、逐日或盤中選擇權資料，通常需要 TAIFEX 正式資料、資料商或付費資料源。
- 完整期貨籌碼、逐產業法人資金流、長期可回測資金流資料，目前免費來源只能 best-effort 補 proxy，還不到正式量化資料庫等級。
- 授權法人報告、券商研究摘要與內部投研內容不能直接抓取或重製，只能引用公開新聞、法說會、公告與公開資料。
- 公司產品、客戶、供應鏈、營收占比、護城河、轉型效益等資料，可以透過 Gemini Search 輔助整理，但仍需要長期審核後沉澱成知識庫，不能完全依賴單次搜尋。
- 僅限於 `--date` 模式，無法取得完整歷史資料，因為資料源與發布日期的對應關係尚未完全建立。

### 總結

目前主要 AI 投研功能、指令、prompt、評分、報告、進度顯示、Gemini Search、來源記錄與中文互動流程都已對應開發。剩下的差距主要分成兩類：少部分可再補的程式行為，以及免費資料源無法完全達成的機構級資料深度。

---

## AI 投研 CMD 進度修正（2026-05-07）

本次修正 `/value_scan 我的持股 --deep --top 9999` 在 CMD 停留於「開始收集結構化資料與外部來源」過久的問題，並同步補強其他 AI 調研指令的進度顯示。

### 修正內容

- `/value_scan 我的持股` 現在會先套用「我的持股」候選池，再抓營收、價量與官方來源，不會先跑全市場。
- 修正 `/value_scan` 內部 `universe_policy` 未定義的潛在錯誤。
- `/value_scan` 現在會在 CMD 顯示候選池、候選檔數、營收資料、價量資料、本地初評、官方/公開資料蒐證、交叉驗證等進度。
- Yahoo Finance 價量資料失敗時，不會把候選股全部剔除；會保留候選並以「價量資料缺漏，保守處理」寫入證據與評分脈絡。
- 論壇來源搜尋已拆成 PTT Stock、Dcard、Mobile01 個別進度，成功、失敗與新增筆數都會印在 CMD。
- `/research` 已補上個股解析、股價、法人、融資融券、月營收、季度財報、策略摘要、免費公開來源等進度。
- `/macro` 已補上台股摘要、美股/台指期摘要、台股/櫃買指數、VIX/TAIFEX、期貨法人籌碼、TWSE 法人資金流、類股流動性 proxy、全球公開總經 proxy、fear/greed 計算等進度。
- `/theme` 已補上股票宇宙載入、題材知識庫讀取、股票宇宙比對與公司知識庫補強進度。

### 注意事項

- 部分外部來源仍可能因網路、網站阻擋或資料源暫時失效而失敗，但現在會在 CMD 顯示目前正在處理哪一步。
- `--top 9999` 只影響本地排序或顯示數量，不代表全部 9999 檔都送入 AI。實際 AI 候選股仍受一般模式 10 檔、深度模式 30 檔限制。

---

## AI 投研資料來源備援鏈更新（2026-05-07）

本次補強 AI 調研指令在主要資料源失敗時的 fallback 行為，避免單一來源如 Yahoo Finance 批次價量失敗時，報告直接變成空候選或缺少關鍵價量欄位。

### 價量資料 fallback

新增 `research_center/price_fallbacks.py`：

- 第一層：使用 `stock_scanner.load_price_metrics()`，優先讀取既有快取與 Yahoo Finance 批次價量。
- 第二層：若主要來源缺資料，逐檔改走 `StockDataFetcher.fetch_price_history()`。
- `StockDataFetcher` 會依股票類型嘗試既有鏈：TWSE 官方、Fugle、Yahoo Finance；上櫃股則使用 Yahoo / Fugle 備援。
- 大型全市場情境會限制逐檔備援數量，避免 Telegram 指令長時間無回應；持股、自訂清單等小型候選池會盡量逐檔補齊。
- 價量仍取不到時，候選股不會被直接剔除，會保留並標記「價量資料缺漏，保守處理」。

### 指令套用情況

- `/research`：個股價量已走 `StockDataFetcher`，上市股新增 Yahoo 單股備援；上櫃股維持 Yahoo / Fugle 備援。
- `/value_scan`：改用 `load_price_metrics_with_fallback()`，Yahoo 批次失敗時會逐檔嘗試官方/Fugle/Yahoo 備援。
- `/macro`：類股流動性 proxy 改用同一個價量備援 helper；全市場時會限制逐檔備援數量，避免卡住。
- `/theme`：主要依股票宇宙、題材知識庫與 Gemini Search；本身不強依賴價量資料，後續若加入題材量價排序可直接使用同一個 helper。

### 仍需注意

- 備援來源不是保證一定成功；若網路中斷、Fugle API Key 未設定、Yahoo/TWSE/TPEx 都不可用，系統仍會保守標示資料不足。
- 對大型全市場掃描，不會為所有缺資料股票逐檔無限制重抓，避免再次造成長時間等待。
- 報告 JSON 會保留 `price_data_policy`，可檢查主要來源涵蓋數、備援嘗試數、備援補到幾檔與 sample errors。

---

## AI 投研選股資料備援與 Gemini Search 檢查（2026-05-07）

本次針對兩個問題補強：選股程式既有備用資料源接入 AI 調研，以及 `/research` 一般模式沒有 Gemini Search 來源的問題。

### 選股程式備用資料源接入

新增 `research_center/chip_sources.py`，AI 調研現在可讀取選股程式已建立的本機快取：

- `.cache/chip_daily/`：法人日資料快取，包含外資買賣超、投信買賣超、外資持股比例與資料來源標記。
- `.cache/tdcc/`：TDCC 集保週資料快取，整理大戶持股比例與散戶持股比例。
- 資料會進入 `/research` 的 `chip_backup_data`，並寫入 `source_events`，供 SQLite events 與未來歷史模式使用。
- `/value_scan` 前段候選股也會附上 `chip_backup_data`，並把 `chip_daily_cache`、`tdcc_weekly_cache` 事件納入交叉驗證資料。
- 這一層預設先讀快取，不會每次 AI 報告都跑完整籌碼掃描，避免 Telegram 指令等待太久；若快取不存在，會標示 missing。

### Gemini Search 修正

舊版 `/research 5425` 是一般模式，prompt 沒有明確要求 Gemini Search，所以即使 `grounding=True`，Gemini 可能只根據本地結構化資料作答，導致沒有 grounding citations。

本次已修正：

- `/research` 所有非歷史、非 source-only 模式都會加入 Gemini Search 任務。
- `/research --score`、`/research --deep` 仍使用更深的 CAGR、護城河、轉型效益、題材熱度搜尋規則。
- `/macro`、`/theme`、`/value_scan` 原本已經有 Gemini Search 任務，仍維持。
- 報告 JSON metadata 會保留 `gemini_search_diagnostics`，包含 grounding metadata、查詢數、chunks 數、可解析來源數。
- CMD 會顯示 `Gemini Search 診斷：metadata=... queries=... chunks=... sources=...`。

### 實測結果

最小 Gemini grounding 測試：

```text
metadata=True
queries=7
chunks=7
sources=7
```

重新執行 `/research 5425 --no-html` 後：

```text
Gemini Search 診斷：metadata=True，queries=4，chunks=12，sources=12
報告來源總數：15
Gemini grounding citations：12
```

同次 `/research 5425` 也確認讀到選股程式籌碼備援：

```text
chip_backup_data.status = covered
法人日資料：60 筆
TDCC 週資料：1 筆
最近 10 日外資合計：約 +6961 張
最新 TDCC 大戶比例：37.96%
最新 TDCC 散戶比例：49.22%
```

### 驗證

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_research_center_new_features tests.test_free_sources
.\.venv\Scripts\python.exe -B -c "import main; import research_center.api_app as api; from research_center.telegram_handlers import AI_CALLBACK_PREFIX; print('imports ok', bool(api.app), AI_CALLBACK_PREFIX)"
```

結果：`11 tests OK`，主程式、FastAPI 與 Telegram handler 匯入正常。

---

## AI 投研 FinMind 備援與 Gemini Search 全模式驗證（2026-05-07）

本次針對兩個重點再補強並實測：

1. AI 調研不只使用本機快取，也會把 FinMind API 納入法人 / 籌碼備援鏈。
2. 確認所有 AI 調研指令的模式與參數，哪些會調用 Gemini Search，並實際驗證 Gemini grounding 能回傳搜尋來源。

### FinMind 備援

`research_center/chip_sources.py` 現在會依序整合：

- `.cache/chip_daily/`：選股程式已建立的法人日資料快取。
- `.cache/tdcc/`：TDCC 大戶 / 散戶週資料快取。
- FinMind `TaiwanStockInstitutionalInvestorsBuySell`：作為外資、投信、自營商買賣超的即時備援。
- FinMind `TaiwanStockShareholding`：作為外資持股比例的即時備援。

套用範圍：

- `/research`：個股報告會附上 `chip_backup_data`，並把法人 / TDCC / FinMind 事件寫入來源事件。
- `/value_scan`：候選股前段蒐證會附上 `chip_backup_data`，並納入交叉驗證。

實測 `5425`：

```text
status covered
finmind covered 10
daily covered 61
source_types = FinMind, TPEX, cache
latest_daily_date = 2026-05-07
recent_10d_foreign_net_lots = 6702.08
latest_tdcc_date = 2026-04-30
latest_big_holder_pct = 37.96
latest_retail_holder_pct = 49.22
```

### Gemini Search 觸發規則

會調用 Gemini Search 的情況：

- `/research`：一般、`--score`、`--deep`，使用最新日期時會啟用。
- `/macro`：一般、`--brief`、`--deep`，使用最新日期時會啟用。
- `/theme`：一般、`--deep`、`--top N`，使用最新日期時會啟用。
- `/value_scan`：一般、`--deep`、`--top N`，使用最新日期時會啟用。

不會調用 Gemini Search 的情況：

- `--source-only`：只看資料來源，不呼叫 AI。
- `--date YYYY-MM-DD`：歷史模式會關閉 Gemini Search，避免搜尋到指定日期之後才出現的資料。
- `/report`：只讀取既有報告，不呼叫 AI。

輸出格式參數如 `--no-html`、`--no-json` 只影響檔案輸出，不影響 Gemini Search 是否啟用。

### 觸發矩陣驗證

已檢查 `/research`、`/macro`、`/theme`、`/value_scan`、`/report` 的主要模式與參數：

```text
/research latest modes: ai=True grounding=True search_task=True
/research --source-only: ai=False grounding=False
/research --date: ai=True grounding=False
/macro latest modes: ai=True grounding=True search_task=True
/macro --source-only: ai=False grounding=False
/macro --date: ai=True grounding=False
/theme latest modes: ai=True grounding=True search_task=True
/theme --source-only: ai=False grounding=False
/theme --date: ai=True grounding=False
/value_scan latest modes: ai=True grounding=True search_task=True
/value_scan --source-only: ai=False grounding=False
/value_scan --date: ai=True grounding=False
/report latest: ai=False grounding=False
```

### Gemini Search 實際回傳來源驗證

最小化實測四大調研類型，每一類都實際呼叫 Gemini Search 並收到 grounding metadata 與來源：

```text
research sources 6 metadata True queries 4 chunks 6 finish STOP
macro sources 3 metadata True queries 7 chunks 3 finish STOP
theme sources 12 metadata True queries 3 chunks 12 finish STOP
value_scan sources 4 metadata True queries 4 chunks 4 finish STOP
```

這代表目前所有需要 AI 的非歷史、非 source-only 調研模式，程式都會啟用 Gemini Search，且本次端到端驗證可收到搜尋結果。執行正式報告時，CMD 也會顯示 `Gemini Search 診斷`，報告 JSON metadata 會保存 `gemini_search_diagnostics`。

### 本次驗證

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_research_center_new_features tests.test_free_sources
.\.venv\Scripts\python.exe -B -c "import main; import research_center.api_app as api; from research_center.telegram_handlers import AI_CALLBACK_PREFIX; print('imports ok', bool(api.app), AI_CALLBACK_PREFIX)"
```

結果：`11 tests OK`，主程式、FastAPI 與 Telegram handler 匯入正常。

---

## AI 投研 Gemini Search discovery 與本地量化底稿邏輯修正（2026-05-07）

本次修正 `/research 5425` 可能出現 `grounding=True` 但正式報告沒有 Google 搜尋來源的情況。

原因是：程式原本只在正式長 prompt 啟用 Gemini Search。當 prompt 內含大量結構化資料時，Gemini 可能直接產生報告文字，但不一定會實際回傳 grounding metadata，因此報告來源列表會只有本地來源或論壇來源。

修正後流程：

1. 先收集本地結構化資料與論壇 / 官方來源。
2. 若是最新日期、非 source-only 模式，先執行短 prompt 的 `Gemini Search discovery`。
3. discovery 成功取得 Google grounding citations 後，先併入來源列表。
4. 再把本地資料、既有來源與 Google discovery 摘要一起交給正式 Gemini prompt 產出報告。
5. 若正式長 prompt 沒有再回傳 citations，仍保留 discovery 取得的 Google 來源。

套用範圍：`/research`、`/macro`、`/theme`、`/value_scan` 所有最新日期且會調用 AI 的模式。

不套用範圍：

- `--source-only`：不調用 AI。
- `--date YYYY-MM-DD`：歷史模式停用現在網路搜尋，避免偷看未來。
- `/report`：只讀既有報告。

另外同步修正 `/research` 一般模式的本地量化底稿邏輯：

- `/research 代號` 一般模式不再產生完整 17 項本地量化底稿。
- `/research 代號 --score` 與 `/research 代號 --deep` 才會產生 17 項本地量化底稿，並交給 AI 依規格整理、補充來源與保守判讀。
- `/value_scan` 仍保留本地初篩排序，因為它需要先限制候選名單再送 AI，以節省 token。
- `/macro`、`/theme` 仍保留本地輔助指標，例如 fear/greed 或供應鏈覆蓋度，這些不是完整個股買入評分。

實測 `/research 5425 --no-html --no-md`：

```text
Local full scoring skipped for this mode
Gemini Search discovery diagnostics: metadata=True, queries=5, chunks=11, sources=11
Gemini Search discovery got 11 Google sources and merged them into final prompt
Gemini Search diagnostics: metadata=False, queries=0, chunks=0, sources=0
Final report returned no citations; keeping 11 Google sources from Search discovery
Total sources: 14
```

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\orchestrator.py research_center\prompt_registry.py research_center\scoring_engine.py research_center\report_validator.py research_center\report_builder.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_research_center_new_features tests.test_free_sources
```

結果：`11 tests OK`。

---

## AI 投研多段 Gemini Search discovery 更新（2026-05-07）

本次將所有會調用 AI 的調研指令，由單一短 prompt 搜尋，改成多段短 prompt 搜尋，以提升資料來源覆蓋率與搜尋品質。

適用範圍：

- `/research` 最新日期模式。
- `/macro` 最新日期模式。
- `/theme` 最新日期模式。
- `/value_scan` 最新日期模式。

不適用範圍維持不變：

- `--source-only`：不調用 AI。
- `--date YYYY-MM-DD`：歷史模式停用 Gemini Search，避免偷看未來。
- `/report`：只讀取既有報告。

### 多段搜尋任務

`/research` 一般模式會拆成 5 段：

1. `official_filings`：MOPS、TWSE、TPEx、公司官網、法說會、月營收、財報。
2. `recent_news`：近期主流財經新聞、營收、訂單、產品與經營變化。
3. `industry_and_theme`：產業脈絡、產品線、CAGR、護城河、轉型效益、題材熱度。
4. `chips_and_institutions`：法人、籌碼、融資融券、股權分散與市場關注。
5. `risks_and_contradictions`：風險、反證、需求放緩、毛利率壓力、庫存與客戶集中。

`/research --score` 與 `/research --deep` 會再增加：

6. `scoring_evidence`：評分專用證據，包含 CAGR、護城河、重估、轉型與反證。

`/macro` 會拆成 9 段：

1. `official_macro_data`
2. `taiwan_market_news`
3. `global_cross_asset`
4. `futures_options_chips`
5. `macro_risks`

`/theme` 會拆成 5 段：

1. `theme_definition`
2. `supply_chain`
3. `company_evidence`
4. `news_and_catalysts`
5. `risks_and_hype`

`/value_scan` 會拆成 6 段：

1. `candidate_news`
2. `official_announcements`
3. `old_new_label_evidence`
4. `valuation_and_financials`
5. `institutional_and_chips`
6. `rerating_risks`

每一段都會獨立呼叫 Gemini Search grounding，取得 citations 後立即合併進來源列表。若其中一段逾時或失敗，CMD 會顯示該段失敗，其他段仍會繼續執行。

### CMD 進度

正式執行時會看到類似：

```text
Run multi-stage Gemini Search discovery: 5 compact prompts
Gemini Search discovery 1/5 [official_filings] start
Gemini Search discovery 1/5 [official_filings] diagnostics: metadata=True, queries=14, chunks=17, sources=17, added=17
...
Multi-stage Gemini Search discovery completed: N unique Google sources merged into final prompt
```

### timeout 與重試

Gemini API 呼叫已加入基本重試：

- 單次 timeout 預設 90 秒。
- timeout、網路傳輸錯誤、429、5xx 會重試 1 次。
- 多段 discovery 採逐段容錯；單段失敗不會中斷整份報告。

### 代表性端到端驗證

已實測四種調研指令各自第一段 discovery，確認新的多段 prompt 能實際取得 Gemini Search grounding citations：

```text
research official_filings sources 17 metadata True queries 14 chunks 17
macro official_macro_data sources 19 metadata True queries 7 chunks 19
theme theme_definition sources 13 metadata True queries 5 chunks 13
value_scan candidate_news sources 13 metadata True queries 6 chunks 13
```

完整 21 段搜尋會在正式報告執行時逐段進行；若外部 Gemini API 單段回應過慢，該段會記錄失敗並繼續下一段。

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\gemini_service.py research_center\orchestrator.py research_center\prompt_registry.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_research_center_new_features tests.test_free_sources
```

結果：`11 tests OK`。

---

## AI 投研完整來源清單附加修正（2026-05-07）

本次修正 AI 報告正文可能只列出少數引用來源的問題。

原因是：Gemini 正文的「資料來源列表」會依模型判斷只列出有直接引用的來源，可能少於程式實際保存的 `sources.json`。例如 `/research 5425` 最新報告中，`sources.json` 實際保存 51 筆來源，其中 48 筆為 Gemini grounding source，但 Markdown 正文只列出 5 筆。

修正後：

- 報告產生器會在 Markdown / HTML 報告尾端自動附加 `完整資料來源清單`。
- 這份完整清單直接來自程式保存的 `sources.json`，不是 AI 自行挑選。
- AI 正文仍可保留精簡引用清單；尾端完整清單用於稽核與追溯。

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\report_builder.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_research_center_new_features tests.test_free_sources
```

結果：`11 tests OK`。

---

## AI 投研所有報告完整來源清單統一規則（2026-05-07）

本次將完整資料來源清單改為所有 AI 調研報告的共用輸出規則，不限於 `/research`。

適用指令：

- `/research`
- `/macro`
- `/theme`
- `/value_scan`

修正後，凡是透過 `research_center.report_builder.write_report_artifacts()` 輸出的 Markdown / HTML 報告，都會在尾端自動附加：

```text
## 完整資料來源清單
```

這份完整清單直接來自同一份報告的 `sources.json`，包含：

- 本地官方來源，例如 TWSE、TPEx、MOPS、TAIFEX、TDCC。
- Gemini Search grounding citations。
- 論壇或社群來源，但仍依 Level 4 / Level 5 規則只作情緒參考。
- 其他公開新聞、產業資料與輔助來源。

AI 正文中的「資料來源列表」可能仍只列出該段報告直接引用到的部分來源；尾端的「完整資料來源清單」則一定以程式保存的 `sources.json` 為準，用於稽核、追溯與確認 Gemini Search 實際取得多少來源。

注意：`/report` 是查詢歷史報告，不會重寫舊報告內容。這項規則會套用在之後新產出的 AI 調研報告。

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\report_builder.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_research_center_new_features tests.test_free_sources
```

結果：`11 tests OK`。

---

## AI 題材研究選單簡化（2026-05-07）

本次簡化 `/theme` 的 Telegram 中文互動選單。

調整前：

1. 選分析模式。
2. 選輸出數量：前 10 / 前 20 / 前 30。
3. 選日期。

調整後：

1. 選分析模式：一般題材 / 深度題材 / 只看資料來源。
2. 選日期：最新日期 / 指定日期。

原因：`/theme` 的定位是題材研究，不是選股排名；輸出數量選單容易讓流程變複雜，且多數情境不需要使用者手動決定幾檔公司。

保留項目：手動指令仍支援 `--top N`，例如：

```text
/theme AI伺服器 --deep --top 30
```

也就是說，互動選單移除數量選擇，但進階使用者仍可用參數控制候選公司數量。

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\telegram_handlers.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_research_center_new_features tests.test_free_sources
```

結果：`11 tests OK`。

---

## AI 價值重估掃描逐檔分析規則（2026-05-07）

本次修正 `/value_scan` 報告可能只對第一名候選股產出完整分析的問題。

適用指令：

- `/value_scan 我的持股 --top 9999`
- `/value_scan 我的持股 --deep --top 9999`
- `/value_scan 精選選股 --top N`
- `/value_scan 自訂股票清單 --top N`

> **以下為歷史紀錄，描述 2026-05-07 當時的規則，現已進一步調整（見 2026-05-18 記錄）。**
>
> 當時新規則（2026-05-07）：
>
> 1. `/value_scan 股票池 --top N` 代表先從股票池取出最多 N 檔候選股。
> 2. 排名表必須列出這 N 檔候選股。
> 3. `六、個股重估分析` 也必須逐檔分析這 N 檔。
> 4. 不得只分析第一名，不得使用「以某檔為例」或「其餘略」取代逐檔分析。
> 5. 每一檔候選股都需包含：舊市場標籤、新市場標籤、重估證據、營收與財報驗證、法人籌碼與技術確認、是否只是蹭題材、重估分數、未來 1～3 個月觀察重點、風險與反證。
>
> **現已調整（2026-05-18）：**
> - `--top N` 只影響本地候選池排序、取樣或顯示數量，不等於全部 N 檔都送入 AI。
> - AI 實際分析數量由模式決定：一般重估最多 10 檔、深度重估最多 30 檔、source-only 為 0 檔。
> - 排名表顯示本地排序結果；個股重估分析只要求完整分析實際送入 AI 的候選股。
> - 程式保底機制（report_builder 自動附加候選股逐檔分析）仍適用於實際送 AI 的候選股。

**歷程說明：**
- 2026-05-07 規則：AI 必須逐檔分析全部 `--top N` 候選股（當時說明如上）。
- 2026-05-18 調整：`--top N` 只影響本地排序，AI 分析數量回歸模式限制（一般 10 檔、深度 30 檔、source-only 0 檔）。
- 程式保底機制仍適用於實際送入 AI 的候選股，確保報告尾部有完整逐檔分析。

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\report_builder.py research_center\report_validator.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_research_center_new_features tests.test_free_sources tests.test_prompt_contracts tests.test_report_schema
```

結果：`19 tests OK`。

---

## AI 投研 MiniMax Search 與雙模型比較（2026-05-07）

本次新增 MiniMax-M2.7 作為第二套搜尋與比較分析模型。正式分析模型仍維持 Gemini；MiniMax-M2.7 會在開發比較模式下使用同一份資料另產一份比較報告。

### 設定位置

敏感金鑰放在 `config/secrets.json`，此檔已由 `.gitignore` 忽略，不應提交：

```json
{
  "minimax_api_key": "...",
  "minimax_model": "MiniMax-M2.7",
  "serper_api_key": "...",
  "jina_api_key": "..."
}
```

公開開關放在 `config/research_center.json`：

```json
{
  "minimax_model": "MiniMax-M2.7",
  "minimax_base_url": "https://api.minimax.io/v1",
  "enable_minimax_search": true,
  "enable_minimax_comparison": true
}
```

### 搜尋流程（2026-05-16 更新）

**停用：Serper、Jina Reader 已停用。**

非歷史日期、非 source-only 的 AI 投研指令會先建立同一批 discovery tasks，然後執行：

1. **MiniMax Token Plan MCP web_search**（第一優先）：使用 MiniMax MCP subprocess 呼叫 `web_search` tool，無需 Serper/Jina key。
2. **Tavily Search**（第二優先）：若 MiniMax MCP 未啟用或失敗，使用 Tavily Search API。
3. **Gemini Search fallback**：若 MiniMax + Tavily 來源不足，Gemini Search 會作為 fallback。

正文抓取順序：
1. **requests + BeautifulSoup**：嘗試直接讀取 URL 正文，移除 script/style/nav/header/footer/aside。
2. **Tavily Extract fallback**：若 requests 失敗（403/timeout/SSL）或內容過短（中文<300字/英文<800字），使用 Tavily Extract API。
3. 保留搜尋 snippet：若 Tavily Extract 也失敗，保留原始搜尋 snippet。

MiniMax/Tavily 會消耗 API 額度。額度不足不會中斷報告，會繼續嘗試下一個來源。

### 搜尋任務與關鍵字優化（2026-05-22 更新）

本次將所有 AI 投研搜尋任務改為繁體中文主導，並保留必要英文 query 作為國際資料補強。既有搜尋鏈維持不變：MiniMax MCP Search → Tavily Search → Gemini Search fallback → WebFetch。

- `/research`：搜尋任務拆成官方公告與財報、近期新聞與公司事件、產業與題材、籌碼與法人、風險與反證、論壇與社群情緒、評分證據。新增產品/客戶/營收占比、同業競爭、自由現金流、存貨週轉、推論型加分與扣分反證 query。
- `/macro`：補強國際局勢、關稅、出口管制、Fed/ECB/BOJ/PBOC、油價、原物料、美元/台幣、中國/歐洲/美國房地產、信用風險、TAIFEX 期貨與波動率 query。
- `/theme`：補強供應鏈層級、產品角色、客戶分類、營收占比、近期催化、替代技術、競爭者、題材退燒與營收連結不足 query。
- `/value_scan`：多檔候選股改成分批搜尋，每批約 4 檔，避免把過多股票塞進同一條 query 造成搜尋品質下降。搜尋面向包含近期新聞、官方公告、新舊標籤重估、估值財務、法人籌碼與重估失敗風險。
- `/news refresh`：補入今日、盤中、收盤、近 24 小時、本週、法說會、月營收、法人、利多/利空、庫存、客戶、目標價/下修等時間與事件關鍵字。
- `/theme_radar`：新增專屬 discovery 任務，搜尋熱門題材、資金輪動、新聞爆量、政策/訂單/法說催化與題材退燒反證。
- `/theme_flow`：新增題材擴散路徑搜尋，聚焦上游/中游/下游、下一層受惠、替代技術與營收連結反證。
- `/sector_strength`：新增族群強弱搜尋，聚焦類股輪動、法人資金、成交量、強勢族群過熱與基本面風險。
- `/topic_maintain`：本地 search query plan 補強供應鏈層級、催化事件、替代技術、公司官方證據與反證缺口回補。
- 論壇資料：直連 PTT/Dcard/Mobile01/理財寶仍保留；同時 `/research` discovery 會補 `site:` 社群搜尋，但報告規則仍要求論壇只能當市場情緒與待驗證線索，不得單獨支撐高分。
- `search_query_log`：報告 JSON metadata 會保存每個 discovery task 的實際 query、任務數、總 query 數，以及 MiniMax/Tavily/Gemini 各 provider 回傳來源數、query_count、status 與 error_reasons，方便事後稽核「到底搜尋了什麼」。

### MiniMax MCP Search 啟動與自動安裝機制

為了解決跨設備部署、使用者名稱路徑變動或執行檔缺失等問題，本專案提供**自動檢查與安裝機制**。MiniMax MCP 會優先安裝在專案內 `.runtime` 目錄，避免 Windows 使用者名稱含中文時，批次檔傳遞路徑發生亂碼。
#### 1. 運作流程
當你執行 `啟動機器人.bat` 時，系統會自動在啟動前執行檢查腳本 `tools/ensure_minimax_mcp.py`：
- **啟動路徑固定為專案 `.runtime`**：啟動檔只使用 `.runtime\uv_tools\minimax-coding-plan-mcp\Scripts\minimax-coding-plan-mcp.exe`，避免 `%TEMP%` 或使用者目錄含中文時造成 CMD 路徑亂碼。工具函式仍保留 `%TEMP%`、`%APPDATA%\uv\tools`、`%LOCALAPPDATA%\uv\tools` 搜尋能力供診斷與相容用途，但啟動成功輸出以 `.runtime` 為準。
- **自動安裝**：若找不到該執行檔，會利用專案本地虛擬環境中的 `.venv\Scripts\uv.exe`（或 fallback 到系統的 `uv` 命令）自動將 `minimax-coding-plan-mcp` 安裝到專案內 `.runtime\uv_tools`。安裝指令使用 `uv tool install --force minimax-coding-plan-mcp`，避免其他工具目錄已有同名 executable 時安裝中斷。`.runtime` 已加入 `.gitignore`，不會提交到版本庫。
- **環境變數動態綁定**：安裝或偵測成功後，腳本會輸出 `key=value` 格式（`MINIMAX_MCP_READY=1`、`MINIMAX_MCP_COMMAND=<路徑>`），由批次檔以 `for /f` 解析動態設定環境變數。不需再手動填寫寫死的使用者路徑（如 `C:\Users\username\...`）。
- **啟動安全保護**：若 MiniMax MCP 因網路中斷等原因無法自動安裝或 exe 不存在，批次檔會顯示黃色警告提示，倒數 3 秒後自動 fallback 啟動主程式（使用 Tavily/Gemini 搜尋），不會中斷主程式運行。

#### 2. 三層安全防護機制 (避免假裝完成)
為確保 MiniMax 搜尋功能真實有效運作，不因靜默失敗或缺失 exe 而「假裝完成」，本專案實作以下三層保護：
1. **啟動 exe 存在性檢查**：`ensure_minimax_mcp.py` 只檢查 `minimax-coding-plan-mcp.exe` 是否存在（使用 `os.path.exists()`），**不做耗費額度的實際 smoke test**，也不嘗試啟動 subprocess。真正的健康檢查在執行 `/research` 或呼叫 `MiniMaxSearchService.health_check(run_smoke=True)` 時才會真正對 MCP 發送請求（會消耗 MiniMax 搜尋額度）。
2. **CMD 明確顯示結果**：在執行 `/news` 新聞回補或 `/research` 時，命令列 (CMD) 會明確輸出 MiniMax Search 實際的運作狀態。例如：
   - 取得來源：`[選股進度] MiniMax Search | 查詢 "台積電 法說會" 成功 (取得 4 個來源)`
   - 發生錯誤：`[選股進度] MiniMax Search | 查詢 "台積電 法說會" 失敗 (原因: mcp_timeout, 詳情可見 metadata)`
3. **報告 JSON Metadata 完整記錄**：產出的投研報告 JSON 檔之 `metadata` 內，會完整寫入 `minimax_search_discovery` 診斷資訊。包含：
   - `source_count`：實際取得來源數。
   - `error_reasons`：包含 `mcp_timeout`、`mcp_package_not_installed`、`uv_permission_denied` 等詳細錯誤原因，以去重方式記錄。
   - `error_samples`：前 3 筆異常範例，以利快速排除故障。

#### 3. 手動驗證與排障
如果你想手動測試 MiniMax MCP 與 Python 整合是否正常，可在終端機執行：
```powershell
# 執行 MiniMax 獨立健康檢查
.venv\Scripts\python.exe tools\ensure_minimax_mcp.py

# 執行 MiniMax 整合測試
.venv\Scripts\python.exe -m pytest tests/test_minimax_integration.py
```

**驗證搜尋**：
```powershell
@'
from research_center.minimax_search_service import MiniMaxSearchService
from research_center.command_parser import parse_command_text
service = MiniMaxSearchService(timeout_seconds=60, max_results_per_query=5)
request = parse_command_text('/research 2330')
result = service.discover(request, [{'label':'test','queries':['台積電 2026 法說會']}])
print('sources=', len(result.sources))
print('status=', result.diagnostics['runs'][0]['status'])
print('error_count=', result.diagnostics['runs'][0].get('error_count', 0))
print('error_reasons=', result.diagnostics['runs'][0].get('error_reasons'))
print('mcp_command=', result.diagnostics.get('mcp_command'))
'@ | .\.venv\Scripts\python.exe -B -
```
成功時：`sources > 0`，`status=ok`（或 `partial` 若有部分失敗但不影響結果），`provider=minimax_mcp_search`。

**diagnostics 欄位說明**：

| 欄位 | 說明 |
|------|------|
| `source_count` | 總共取得的來源數量，`> 0` 代表搜尋成功 |
| `runs[].status` | `ok`：全部成功；`partial`：部分失敗但有結果；`failed`：完全失敗 |
| `runs[].error_count` | 失敗的查詢數量，0 表示無錯誤 |
| `runs[].error_reasons` | 錯誤分類（如 `mcp_unknown_error`、`uv_permission_denied`），可查下表 |
| `runs[].error_samples` | 最多 3 筆錯誤摘要，格式為 `{"error": "..."}`，用於診斷非致命錯誤 |
| `raw_response_samples` | 前 3 筆成功回應的預覽（限 500 字），不含 API key |
| `mcp_command` | 實際使用的 MCP 啟動指令（固定 exe 路徑或 uvx） |

**error_reasons 代碼表**：

| 代碼 | 意義 | 可能原因 |
|------|------|---------|
| `minimax_api_key_missing` | API Key 未設定 | `config/secrets.json` 未填或環境變數未設 |
| `minimax_api_auth_failed` | API 認證失敗 | API Key 過期或無效 |
| `minimax_quota_or_credit_failed` | API 額度/信用額不足 | MiniMax 帳戶额度耗盡 |
| `mcp_empty_response` | MCP 回應空白 | 伺服器異常或網路瞬斷 |
| `mcp_protocol_error` | 通訊格式錯誤 | MCP 回應解析失敗（TypeError、AttributeError、JSONDecodeError 等） |
| `uv_permission_denied` | UV 目錄寫入被拒 | Windows Controlled Folder Access 或防毒軟體阻擋 |
| `pypi_connection_failed` | PyPI 連線失敗 | 公司網路封鎖 PyPI |
| `mcp_package_not_installed` | 工具未安裝或依賴缺失 | `uv tool install` 未執行、安裝失敗，或 diagnostics 顯示 `No module named 'mcp'` |
| `mcp_timeout` | MCP 啟動或執行逾時 | 網路延遲或工具無回應 |
| `mcp_unknown_error` | 未分類錯誤 | 需查看 `error_samples` 進一步判斷 |

**重要**：`status=partial` 不一定代表失敗。若 `source_count > 0`，代表仍有可用搜尋來源，報告會正常繼續。只有當 `status=failed` 且 `source_count=0` 時才表示 MiniMax 完全未回應（此時已 fallback 到 Tavily/Gemini）。

MiniMax MCP 失敗不會中斷報告，會自動 fallback 到 Tavily → Gemini。

### 雙模型比較報告

主報告仍由 Gemini 產出。若 `enable_minimax_comparison=true` 且 MiniMax API Key 已設定，系統會把同一份正式 prompt 與同一份來源資料再送給 MiniMax-M2.7，另存一份比較報告。

檔名會帶有 `_minimax_`，例如：

```text
reports/stock/5425/research_5425_minimax_YYYYMMDD_HHMMSS_xxxxxxxx.md
```

Telegram 會先傳送 Gemini 主報告；MiniMax-M2.7 比較報告改由背景任務使用同一份完整 prompt 產生。成功後會補傳 Markdown / HTML，失敗也會另發 Telegram 通知。CMD 會顯示 MiniMax Search 進度、MiniMax prompt 路徑、比較報告路徑或失敗原因。

### 備份

本次修改前已先備份既有檔案到：

```text
backup/minimax_integration_20260507_235727
```

若未來需要回到本次整合前的架構，可從此資料夾還原對應檔案。

### 驗證

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\config.py research_center\minimax_service.py research_center\minimax_search_service.py research_center\orchestrator.py research_center\report_builder.py research_center\telegram_handlers.py research_center\prompt_registry.py research_center\source_rank.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_minimax_integration tests.test_research_center_new_features tests.test_free_sources tests.test_prompt_contracts tests.test_report_schema
```

結果：`23 tests OK`。

---

## AI 投研論壇來源修復與理財寶新增（2026-05-08）

本次補強論壇/社群來源收集，目標是降低 PTT、Dcard、Mobile01 被阻擋或改版時造成的資料缺口。

新增與調整：

1. 新增理財寶股市爆料同學會個股討論區來源。
   - 若查詢字串包含股票代號，會優先嘗試 `https://social.cmoney.tw/forum/stock/{股票代號}?tab=discuss`。
   - 來源歸類為 Level 4，僅作市場情緒與討論熱度參考。
2. PTT Stock 改為多關鍵字搜尋。
   - 會依序嘗試完整查詢、股票代號、股票名稱等關鍵字。
   - 保留 `over18=1` cookie 與 browser-like headers。
3. Dcard 增加雙路徑。
   - 先嘗試公開 API。
   - 若無結果，改嘗試公開搜尋頁解析。
4. Mobile01 增加 browser-like headers 與多種連結格式解析。
5. 現行論壇 `site:` 搜尋 fallback。
   - Serper / Jina 已停用，不再呼叫 Serper Google Search。
   - 若 PTT、Dcard、Mobile01、理財寶直接抓取失敗或無結果，`/research` discovery 會補社群 `site:` query，交由 MiniMax MCP Search → Tavily Search → Gemini fallback 搜尋。
   - 例如：`site:ptt.cc/bbs/Stock 5425 台半`、`site:social.cmoney.tw/forum/stock 5425 台半`。
   - 社群來源只作市場情緒與待驗證線索，不得單獨支撐高分。

限制：

- Dcard 與 Mobile01 若回傳 403，直接抓取仍可能失敗；fallback 可補搜尋引擎找到的該站頁面或摘要，但不保證能讀完整內文。
- 論壇來源一律不能當成財報、公告或正式事實來源，只能當成情緒與市場討論參考。
- `--date` 歷史模式仍會保守排除無法確認發布日期的論壇來源。

備份：

```text
backup/forum_sources_20260508_011647
```

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\forum_service.py research_center\source_rank.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_forum_sources tests.test_research_center tests.test_research_center_new_features tests.test_free_sources
```


結果：`31 tests OK`。

---

## MiniMax 比較報告 Telegram 附件修正（2026-05-08）

問題：若 MiniMax-M2.7 比較報告產生失敗，`comparison_reports` 可能只保存 `model/status/error`，沒有 `markdown_path` 或 `html_path`。舊版 Telegram 附件傳送邏輯會把空路徑轉成 `Path("")`，在 Windows 會等同目前資料夾 `.`，因此出現：

```text
Permission denied: '.'
```

修正：

- 比較報告狀態為 `failed` 或含 `error` 時，Telegram 不再嘗試傳附件。
- `markdown_path` / `html_path` 為空時直接跳過。
- 路徑存在但不是檔案，例如資料夾，也會跳過。
- 主報告附件也改用同一個安全檢查 helper。

備份：

```text
backup/telegram_minimax_attachment_20260508_012043
```

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\telegram_handlers.py tests\test_minimax_integration.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_minimax_integration tests.test_research_center_new_features tests.test_free_sources
```

結果：`15 tests OK`。

---

## `/value_scan` 我的持股選單簡化（2026-05-08）

本次簡化 Telegram 中文互動選單中 `/value_scan` 的「我的持股」流程。

> **以下為歷史紀錄，描述當時的調整，現已進一步調整（見 2026-05-18 記錄）。**

當時調整前（2026-05-08 前的流程）：

1. 選股票名單來源：我的持股。
2. 再選要分析幾檔：前 10 / 前 30 / 全部。
3. 再選分析模式與日期。

當時調整後（2026-05-08）：

1. 選股票名單來源：我的持股。
2. 系統直接預設分析全部持股，等同 `--top 9999`。
3. 直接進入分析模式選單。

原因：我的持股通常本來就是小型清單，額外詢問前 10 / 前 30 / 全部實用性低，且容易誤以為只會分析部分持股。

## 舊流程已調整（2026-05-18）

> 以下描述為舊版選單行為，僅保留作歷史紀錄：
> - 「我的持股」過去有「前 10 / 前 30 / 全部」選單，現已移除，改為直接進入分析模式。
> - 「精選選股名單」、「全市場初篩」、「最近掃描結果」過去也有「前 10 / 前 30 / 全部」選單，`_value_top_keyboard` 函式已刪除。
> - AI 候選股限制：一般模式最多 10 檔，深度模式最多 30 檔，source-only 模式為 0 檔（只做來源彙整）。
> - `--top N` 僅影響本地排序或顯示數量，不代表全部 N 檔都送入 AI。

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\telegram_handlers.py tests\test_telegram_menus.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_telegram_menus tests.test_research_center_new_features tests.test_free_sources
```

結果：`13 tests OK`。

---

## `/macro` 手動輸入市場範圍（2026-05-08）

本次調整 Telegram 中文互動選單：

- `/macro` 的市場範圍新增「手動輸入市場範圍」。
- 選擇後可輸入例如「台股電子股」、「美股科技股」、「台幣匯率與電子股」，接著再選宏觀分析模式與日期。
- 手動輸入仍以宏觀市場視角處理，重點是指數、利率、匯率、資金流、風險偏好、法人籌碼、產業輪動與持股水位；不會改寫成 `/theme` 的供應鏈受惠股排行報告。
- `/macro 台股 --deep` 這類完整手打指令仍維持直接執行。

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\telegram_handlers.py tests\test_telegram_menus.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_telegram_menus tests.test_prompt_contracts tests.test_research_center
```

結果：`26 tests OK`。

---

## MiniMax 背景比較報告與長 timeout（2026-05-08）

本次調整 MiniMax-M2.7 比較報告流程：

- MiniMax comparison 不再阻塞 Gemini 主報告。主報告完成後會先傳 Telegram 摘要與 Markdown / HTML。
- MiniMax-M2.7 會在背景使用同一份完整 prompt 與同一份來源資料產生比較報告，不做精簡 prompt，以維持和 Gemini 的公平比較基準。
- MiniMax API timeout 從 120 秒拉長到 1200 秒，降低長 prompt / 長報告讀取逾時機率。
- MiniMax 成功後會補發 Telegram 訊息並傳送比較報告附件。
- MiniMax 失敗後也會補發 Telegram 警告，包含失敗原因與 prompt log 路徑。
- 主報告 JSON metadata 會先記錄 `comparison_reports.status=pending`，背景任務完成後更新為 `success` 或 `failed`。

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\models.py research_center\minimax_service.py research_center\orchestrator.py research_center\telegram_handlers.py tests\test_minimax_integration.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_minimax_integration tests.test_research_center_new_features tests.test_free_sources tests.test_prompt_contracts tests.test_telegram_menus
```

結果：`25 tests OK`。
---

## `/macro` 國際宏觀升級（2026-05-08）

本次擴充 `/macro` 的市場範圍、搜尋任務與報告章節：

- Telegram 市場範圍選單新增「中國」與「歐洲」。
- `/macro` 固定章節從 19 章擴充為 24 章，新增國際局勢、戰爭/制裁、關稅/貿易政策、央行利率、債券、匯率、能源原物料、房地產與信用風險。
- 深度模式會交叉分析 Fed、ECB、BOJ、PBOC、台灣央行、升息降息預期、美債殖利率、美元指數、台幣、人民幣、日圓、歐元、油價、天然氣、銅鋁金鋼鐵、房市與銀行壓力如何影響台股資金與產業輪動。
- Gemini Search / MiniMax Search 的 `/macro` discovery tasks 從 5 段擴充為 9 段，新增：
  - `geopolitics_trade_tariffs`
  - `central_banks_rates_fx`
  - `commodities_energy`
  - `real_estate_credit`
- 若輸入內容看起來像產業題材，`/macro` 仍只做宏觀視角，不會改寫成 `/theme` 供應鏈受惠股排行。

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\telegram_handlers.py research_center\prompt_registry.py tests\test_telegram_menus.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_telegram_menus tests.test_prompt_contracts tests.test_research_center_new_features
```

結果：`17 tests OK`。
---

## AI 投研 HTML 報告頁籤與 RWD（2026-05-08）

本次調整所有 AI 調研指令產出的 HTML 報告顯示方式，適用：

- `/research`
- `/macro`
- `/theme`
- `/value_scan`
- MiniMax-M2.7 比較報告

調整後 HTML 報告預設顯示「主報告」頁籤，讓打開檔案時先看到摘要、主要章節、評分/水位/結論等正文內容。

輔助內容改成同一份 HTML 內的頁籤切換：

- `主報告`：預設開啟。
- `QA 驗證`：規格檢查、缺漏提醒、schema/來源檢查。
- `完整資料來源`：完整 `sources.json` 來源清單，預設不展開。
- `Metadata`：模型、搜尋診斷、比較報告狀態等摘要。
- `候選股逐檔資料`：僅 `/value_scan` 報告在有完整候選股底稿時顯示。

實作原則：

- Markdown、JSON、sources.json 仍完整保留，不改原始資料內容。
- HTML 只改顯示方式，不拆成多個實體檔案，避免 Telegram 附件與歷史報告路徑管理變複雜。
- 手機瀏覽器採 RWD：容器寬度限制在螢幕內，長網址與文字會自動換行，`pre/code` 只在自身區塊內捲動，整個頁面避免橫向卷軸。

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\report_builder.py tests\test_report_schema.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_report_schema tests.test_research_center tests.test_research_center_new_features
```

結果：`28 tests OK`。

---

## AI 投研多模型並行報告（2026-05-08）

本次將 Telegram AI 投研指令的 Gemini / MiniMax 報告產生流程改成多模型並行模式。

適用條件：

- 指令會調用 AI 模型。
- 不是 `--source-only`。
- Gemini API Key 與 MiniMax API Key 都已設定。
- `enable_minimax_comparison=true`。

新流程：

1. 本地資料、官方來源、論壇來源、Gemini Search discovery、MiniMax Search discovery 仍只收集一次。
2. 程式合併所有來源後，建立同一份完整正式 prompt。
3. Gemini 與 MiniMax-M2.7 同時在背景產生報告。
4. 哪個模型先完成，就先傳送該模型的 Telegram 成功訊息與 Markdown / HTML 報告。
5. 另一個模型完成後再補傳。
6. 任一模型失敗，都會各自傳送 Telegram 警告，包含模型名稱、失敗原因與 prompt log 路徑。

模型公平性：

- Gemini 與 MiniMax-M2.7 使用同一份正式 prompt。
- Gemini Search discovery 與 MiniMax Search discovery 取得的資料會先合併後再送給兩個模型。
- Gemini 正式生成時仍可依設定使用 Google Search grounding；MiniMax 正式生成時不再額外搜尋，只分析同一份已合併資料。

回退行為：

- 如果沒有啟用 MiniMax comparison，或 MiniMax/Gemini key 不完整，會回到原本單模型流程。
- `/report` 與 `--source-only` 不進多模型並行。

驗證：

```powershell
.\.venv\Scripts\python.exe -B -m py_compile research_center\orchestrator.py research_center\telegram_handlers.py tests\test_minimax_integration.py
.\.venv\Scripts\python.exe -B -m unittest tests.test_minimax_integration tests.test_report_schema tests.test_research_center_new_features tests.test_prompt_contracts tests.test_telegram_menus
```

結果：`31 tests OK`。

---

## Gemini Pro 優先與 Flash 備援（2026-05-08）

本次將 AI 投研中心的 Gemini 預設模型改為 gemini-3-pro-preview，並加入 gemini-3-flash-preview 作為 fallback。

### 設定方式

config/research_center.json：

    model: gemini-3-pro-preview
    fallback_models: gemini-3-flash-preview

也可以在 config/secrets.json 使用 gemini_model 與 gemini_fallback_models 覆蓋，但不建議把 API Key 或 secrets 提交到 Git。

### Fallback 觸發條件

當主要模型遇到下列狀況時，程式會自動改用 fallback 模型重試：

- 額度不足、rate limit、RESOURCE_EXHAUSTED 類錯誤。
- 429 或 5xx 暫時性錯誤。
- 模型不可用、模型名稱不支援、權限或 preview 可用性相關錯誤。
- 連線逾時或傳輸錯誤。

### 報告與進度顯示

- CMD 會在 fallback 發生時顯示：Gemini fallback used: gemini-3-pro-preview -> gemini-3-flash-preview。
- 報告 metadata 會保留 requested_model、actual_model、fallback_used 與 fallback_attempts。
- Telegram 訊息與報告 metadata 以實際完成的模型為準；如果 fallback 成功，會顯示 Flash。
- 多模型並行模式下，Gemini 這一路也會套用相同 fallback 規則；MiniMax-M2.7 流程不受影響。

### 本次驗證

    .\.venv\Scripts\python.exe -B -m py_compile research_center\config.py research_center\gemini_service.py research_center\orchestrator.py tests\test_minimax_integration.py
    .\.venv\Scripts\python.exe -B -m unittest tests.test_minimax_integration tests.test_report_schema tests.test_research_center_new_features tests.test_prompt_contracts tests.test_telegram_menus
```

結果：33 tests OK。

---

## AI 投研暫停 MiniMax / Serper / Jina（2026-05-14）

本次只調整執行開關，不刪除程式碼，也不刪除 `config/secrets.json` 內既有金鑰。未來若要恢復 MiniMax-M2.7、Serper 或 Jina，只需要把設定開關改回 `true`。

目前狀態：

- 分析模型：只使用 Gemini。
- AI 搜尋來源：只使用 Gemini Google Search / Grounding。
- MiniMax-M2.7 分析比較報告：暫停。
- MiniMax Search：暫停。
- Serper API：暫停，不會呼叫。
- Jina Reader：暫停，不會呼叫。

設定位置：`config/research_center.json`

```json
{
  "enable_minimax_search": false,
  "enable_minimax_comparison": false
}
```

影響：

- Telegram AI 投研指令不再進入 Gemini / MiniMax 多模型並行流程。
- 不會產生 `_minimax_` 比較報告。
- 不會呼叫 Serper Google Search。
- 不會呼叫 Jina Reader 讀取網頁正文。
- Gemini 仍會依非歷史、非 source-only 模式執行 Gemini Search discovery 與正式 grounding。

恢復方式：

```json
{
  "enable_minimax_search": true,
  "enable_minimax_comparison": true
}
```

備份：

```text
backup/disable_minimax_search_20260514
```

---

## AI 投研新增 DeepSeek V4 Pro / OpenCode Go（2026-05-14）

本次新增 OpenCode Go 作為可選分析模型供應商，使用 `DeepSeek V4 Pro` 分析同一份投研資料。MiniMax / Serper / Jina 仍維持暫停，不會執行。

目前模型分工：

- 搜尋資料：Gemini Google Search / Grounding。
- Gemini 分析：`gemini-3-pro-preview`，額度或可用性異常時 fallback 到 `gemini-3-flash-preview`。
- DeepSeek 分析：OpenCode Go `deepseek-v4-pro`，`reasoning_effort=medium`。
- MiniMax-M2.7：仍停用。
- Serper API：仍停用。
- Jina Reader：仍停用。

設定位置：

```json
{
  "opencode_model": "deepseek-v4-pro",
  "opencode_base_url": "https://opencode.ai/zen/go/v1",
  "opencode_reasoning_effort": "medium",
  "enable_opencode_analysis": true
}
```

敏感金鑰放在 `config/secrets.json`：

```json
{
  "opencode_api_key": "YOUR_OPENCODE_GO_API_KEY"
}
```

### 指令參數

所有會產生 AI 分析的投研指令現在支援 `--model`：`gemini`、`deepseek`、`minimax`。

```text
/research 2330 --deep --model gemini
/research 2330 --deep --model deepseek
/research 2330 --deep --model minimax
/macro 全球 --deep --model deepseek
/macro 全球 --deep --model minimax
/theme AI伺服器 --model deepseek
/theme AI伺服器 --model minimax
/value_scan 精選選股 --deep --top 30 --model deepseek
/value_scan 精選選股 --deep --top 30 --model minimax
/topic_maintain --model minimax
```

互動式 Telegram 選單的最後一層會詢問分析模型：

- Gemini
- DeepSeek V4 Pro（OpenCode Go）

### 執行流程

非歷史、非 source-only 模式下，程式仍會先執行 Gemini Search discovery 蒐集 Google 來源。接著：

- 選 Gemini：用 Gemini 產生正式報告，正式生成時仍可 grounding。
- 選 DeepSeek：用同一份 prompt、同一批本地資料與 Gemini Search discovery 來源，送給 OpenCode Go / DeepSeek V4 Pro 分析；DeepSeek 本身不額外搜尋。

CMD 會顯示 prompt 路徑、模型名稱、呼叫進度與完成狀態。Telegram 摘要與報告 metadata 會記錄實際分析模型。

### 驗證

已完成 OpenCode Go 最小 API 測試：

```text
endpoint: https://opencode.ai/zen/go/v1/chat/completions
model: deepseek-v4-pro
reasoning_effort: medium
result: HTTP 200, content 可解析
```

已完成整合測試：

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_telegram_menus tests.test_research_center tests.test_report_schema tests.test_prompt_contracts tests.test_research_center_new_features
```

結果：`48 tests OK`。

備份：

```text
backup/opencode_deepseek_20260514
```

## `/value_scan 精選選股` 候選池修正（2026-05-14）

`/value_scan 精選選股` 已改為使用主程式 `/scan -> 精選選股` 的「精選選股交叉命中」名單，不再只取 `scan_tw_market()` 的財報營收候選池。

目前流程：

- 先找 `.cache/recent_scan_results.json` 中 `scan_type=精選選股` 且 `report_date` 等於本次資料日期的快取。
- 若找到同資料日期快取，直接使用快取中的結構化 `selected_codes` / `codes`，不重新執行選股程式。
- 若找不到快取，才執行精選選股交叉命中邏輯，交叉比對技術面正面訊號、營收財報與法人大戶策略。
- 新產出的精選結果會保存結構化股票代碼，避免從報告文字擷取代碼時誤抓年份。
- 若交叉命中結果為 0 檔或精選流程失敗，不會自動放寬成全市場初篩；報告會明確顯示候選池為空或失敗原因。

`--top` 的作用是先取得真正的精選交叉命中名單後，再取本地重估排序前 N 檔送入 AI：

```text
/value_scan 精選選股 --top 10
/value_scan 精選選股 --top 30
/value_scan 精選選股 --top 9999
```

## AI 投研搜尋 Prompt 中文化（2026-05-14）

Gemini Search discovery 已改為中文搜尋代理 prompt。
每個搜尋任務只負責搜尋、整理、分類與標記來源，不產出最終投資結論、買入評分、目標價或買賣建議。
搜尋輸出要求為 JSON，包含 findings、sources、missing_data、data_completeness。
/research、/macro、/theme、/value_scan 均使用指令專屬搜尋任務。

## AI 投研本地量化底稿與 AI 最終投研評分規則（2026-05-15）

AI 投研中心已將「本地量化底稿」與「AI 最終投研評分」分離。

- 本地量化底稿是 Python 根據結構化資料產生的機械式資料檢查，僅作為 AI 參考。
- 本地量化底稿不是最終投研評分，也不是買賣建議。
- AI 最終投研評分必須由 AI 根據全部資料、搜尋來源、公告、財報、法人籌碼、反證與資料完整性重新評估。
- 若 AI 最終分數高於本地量化底稿，報告必須說明新增證據、來源與差異原因。
- 若資料不足，AI 不得用題材故事、熱門新聞、低股價或模型推測補高分。
- HTML 報告預設顯示主報告；本地量化底稿會放在獨立分頁，不放在主要內容。
- JSON metadata 會保留 `local_scoring` 與 `ai_final_scoring_policy`，方便日後追蹤與除錯。

## AI 投研搜尋來源與 Gemini fallback（2026-05-15）

目前搜尋優先順序：

1. 本地資料、官方資料與既有免費 API。
2. Tavily Search basic。
3. Tavily Extract basic。
4. 自建 HTML fallback。
5. Gemini Search fallback。

Serper、Jina、MiniMax Search 目前預設停用，不會消耗額度。

Gemini Search 已改為 fallback：當 Tavily 與其他來源已達到該模式的來源數量與品質門檻時，會跳過 Gemini Search；若來源不足，才啟用 Gemini Search 補資料。

每個資料來源會標示 provider，例如：

- official_connector
- tavily_search
- tavily_extract
- html_fetch
- gemini_grounding
- forum_direct
- forum_search

## 日期感知資料與新聞庫引用

本段為 AI 投研與題材庫維護的最新資料規則。

- 適用指令：`/research`、`/macro`、`/theme`、`/value_scan`、`/topic_maintain`。
- 若指令有 `--date YYYY-MM-DD`，該日期就是分析日期；系統優先使用該日與該日前的新聞、Web 來源、已保存新聞庫內容。
- 若指令沒有 `--date`，系統以今天作為分析日期，優先使用最新新聞與最新 Web 來源。
- 來源晚於分析日期者會被排除，避免報告偷看未來資料。
- 搜尋 query 不只查單一天，會同時補入精確日期、當月、當季或近期範圍描述，避免只搜到單日資料。
- 已保存新聞庫會依指令類型自動取用：
  - `/macro`：優先 7 天，不足時擴到 14 天、30 天。
  - `/research`：優先 30 天，不足時擴到 90 天、180 天。
  - `/theme`：優先 90 天，不足時擴到 180 天。
  - `/value_scan`：優先 90 天，不足時擴到 180 天。
  - `/topic_maintain` 更新模式：優先 90 天，不足時擴到 180 天。
  - `/topic_maintain` 初始化模式：優先 180 天，不足時擴到 365 天。
- 「不足」指可用且符合日期範圍的新聞數低於該指令的最低需求；系統會自動擴大時間窗，不需要使用者手動指定。
- `/topic_maintain` 不在 Telegram 顯示舊資料標記；日期新舊只用於內部排序、過濾與 prompt 注入。

Tavily 若遇到額度用完，程式會把 Tavily 停用到下個月 1 日，期間自動跳過，避免重複失敗。

若更換 Tavily API Key，系統會比對新舊 key 的 fingerprint。若 fingerprint 不同，會自動清除舊 key 的 quota exhausted 暫停狀態，讓新 key 重新嘗試搜尋。系統只保存短雜湊 fingerprint，不保存完整 API Key。

## Telegram slash 指令選單（2026-05-22）

Bot 啟動後會透過 Telegram `set_my_commands` 註冊常用指令清單。使用者在 Telegram 輸入框輸入 `/` 時，Telegram 會顯示指令選單；繼續輸入文字時，Telegram 會依照指令名稱自動篩選。例如輸入 `/ne` 會篩選出 `/news`。

注意事項：
- Telegram 只會篩選指令名稱，不會篩選中文描述。
- 指令名稱不包含 `/`，但使用者輸入時仍以 `/news` 這種格式執行。
- 指令參數不會出現在 slash menu 裡；例如 `/research 2330 --deep` 仍需手動輸入，或使用既有中文互動選單。
- 若註冊失敗，主程式只會在 CMD 顯示警告，不會阻止 Bot 啟動。
- Slash menu 已補入 `/theme_flow` 與 `/sector_strength`；`/ai_help` 仍保留為 `/help` 的相容別名，但不放入 slash menu，避免重複。
- 一般文字 handler 會先處理 AI 互動選單，再處理 `/scan` 日期輸入；因此 `/research` 要求輸入股票代號或名稱時，輸入 `6282` 或 `康舒` 會進入 AI 投研流程，不會被 `/scan` 日期 handler 吃掉。
### /news 新聞列表、詳情與貼連結保存

- `/news latest`：顯示最近 24 小時新聞，各分類最多顯示 8 則。列表只顯示新聞編號、可點擊標題、來源與日期，不顯示摘要。
- `/news 7d`：顯示最近 7 天新聞，各分類最多顯示 8 則。列表只顯示新聞編號、可點擊標題、來源與日期，不顯示摘要。
- `/news_detail N123`：查看單則新聞摘要、分類、來源、關聯股票與原文連結。
- 直接貼新聞 URL 給 Telegram 機器人：系統會抓取原文、判斷是否為台股/台灣財經/產業新聞、分類後保存到新聞庫。若已存在則提示已保存過。
  - 直接貼新聞 URL 或使用 `/news_save <URL>` 時，系統會額外保存一筆輕量偏好紀錄，包含分類、來源與本地規則判斷的新聞型態，例如「市場炒作型」「供應鏈受惠型」「報價漲價型」「公司催化型」等。這只是偏好線索，不會改寫 Feature Pack 或強制影響所有調研排序。
  - `/news latest`、`/news 7d` 與每日 08:45 推送會讀取這些偏好紀錄，對同類型、同分類或同來源新聞做小幅加權排序；偏好只影響新聞列表排序，不會讓非台股新聞通過過濾，也不會改變調研指令的資料調度。
- 貼新聞 URL 的 Telegram handler 會優先於一般文字輸入 handler 執行，避免網址被其他互動流程吃掉而沒有回應。
- 若在 Telegram 群組中因 bot 隱私模式收不到一般文字，可改用 `/news_save <新聞URL>`，這會走同一套抓取、分類與保存流程。
- 本功能只在新聞資料庫新增輕量 `news_preferences` 表，用於記錄使用者貼新聞的偏好線索；不改 Feature Pack 資料調度中心，`news_articles` 主表、WebFetch、AI 分類與本地過濾邏輯維持原流程。
## /news 顯示前過濾補強（2026-05-24）

- `/news latest` 與 `/news 7d` 會在讀取新聞庫後再次套用顯示前過濾，不會只依賴寫入時的過濾結果。
- 已保存的舊資料若屬於市集活動頁、旅遊活動頁、泛國際新聞、YouTube/Google News 首頁、純加密貨幣新聞、英文 AI/行銷/職缺頁，查詢時會被隱藏，不會直接從資料庫刪除。
- 英文新聞若要顯示，必須明確包含台灣市場、台股、台灣公司、上市櫃或可信台灣財經來源；不能只因為含有 `AI`、`market`、`supply chain` 等泛用字就通過。
- `Readmo.ai`、優分析、理財週刊等台股投資內容會保留，但仍需通過台股財經關聯判斷。
- Telegram 顯示仍維持標題超連結、繁體中文分類，不顯示 raw category。
## /news 標題清洗補強（2026-05-24）

- 若來源標題是 `Readmo.ai - 投資網誌` 這類網站通用標題，系統會優先從正文第一個 Markdown H1 取代顯示標題。
- 此規則會套用在新抓取新聞、手動貼上的新聞連結、以及 `/news latest` / `/news 7d` 查詢既有新聞庫資料時。
- 不需要修改新聞庫 schema，也不需要 migration；舊資料會在顯示前自動清洗標題。
# 早期異動與推論型選股更新（2026-05-24）

本次調整維持系統既有路線：先用技術、量價、籌碼、營收斜率發現早期異動，再由 AI 連結題材、供應鏈、產業趨勢與可能催化，最後用公告、營收、財報、法說、法人籌碼驗證劇本。

- `/scan -> 精選選股` 保留原本交叉確認名單，並新增「早期單點異動觀察（未交叉確認）」區塊；這些股票只是劇本開端線索，不等同精選確認。
- `/value_scan` 若候選池來自「精選選股」或「最近掃描結果」，排序會納入 `early_signal_priority`，避免只用重估分把早期異動候選排掉；全市場、持股、自訂清單仍維持原排序邏輯。
- 新聞資料新增 `tags`、`news_signal_score`、`news_heat_risk_score`：少量高品質新聞視為題材線索，新聞爆量、社群追高、漲停爆量視為過熱/出貨風險。
- `/research` prompt 新增「觀察劇本」：可能劇本、劇本依據、尚未驗證處、成立條件、失效條件，允許推論但必須標示邊界。
- `/theme_radar` prompt 新增未定價節點、下一層擴散、過熱警訊、價格先動但證據弱候選，且 candidate 不得列為正式代表股。
- `/topic_maintain` 與 `/topic_seed_prompt` 的 `candidate` 公司關聯會收斂保留在既有 `company_theme_map` 的 `candidate_themes`，標示 `usage_policy=hypothesis_only`、`not_representative=true`；不新增候選檔案，也不寫入正式 `themes`。
## /theme 題材來源精選與供應鏈覆蓋度補強（2026-05-24）

本次針對 `/theme AI電源` 題材報告品質補強，重點是讓搜尋來源「數量多」不再等於「品質足夠」。

### 來源精選

- `/theme` 仍會保留完整來源清單到 `sources.json` 與報告 metadata。
- AI prompt 會另外使用精選後的 `prompt_sources`，避免 100+ 筆低相關來源稀釋分析品質。
- 精選邏輯會優先保留：
  - Level 1 官方來源。
  - Level 2 主流財經與產業媒體。
  - 有 WebFetch 正文成功的來源。
  - 標題、摘要或網址命中題材、關鍵字、公司代號或公司名稱的來源。
- 社群、YouTube、一般 AI 新聞、宏觀評論、與台股公司無直接關聯的來源會排序較後，只能作為背景脈絡。

### Gemini Search fallback

- `gemini_search_mode=fallback` 不再只看來源總數。
- `/theme` 會額外檢查：
  - 題材相關來源數。
  - 高品質題材相關來源數。
- 若 MiniMax / Tavily 來源很多但與題材關聯不足，仍會啟用 Gemini Search 補找 Google grounding 來源。

### 供應鏈資料覆蓋度

- `/theme` 的本地量化底稿「供應鏈資料覆蓋度」改為優先使用新版題材共用資料層。
- 會整合：
  - `matched_companies` / `matched_universe`
  - `company_knowledge_summary`
  - `topic_context.related_supply_chain_nodes`
  - `theme_quality_context`
- 若題材庫有命中供應鏈節點，不會再因舊版 `company_knowledge_summary` 空白而誤判 0 分。

### Prompt 要求

- `/theme` 與 `/theme --deep` prompt 已補充：
  - 優先引用與本題材、台股公司、產品、客戶、法說會、公告或產業媒體直接相關的來源。
  - 一般 AI 新聞、社群影片、宏觀評論只能作背景，不能支撐核心受惠結論。
  - 每家公司至少標示公司、供應鏈角色、產品或服務、證據來源、證據強度與待驗證缺口。

### 檢查重點

- 報告正文的資料來源數可能少於 `sources.json`，這是正常現象；正文使用精選來源，完整來源仍保留供稽核。
- 若看到 `theme_prompt_source_selection` metadata，代表本次報告已套用 prompt 來源精選。
## 公司知識庫自動補全與 Evidence Pack（2026-05-24）

- `/research`、`/value_scan`、`/theme` 在資料收集完成、新聞庫與 Feature Pack 建立前，會檢查公司知識庫是否缺產品線、供應鏈角色、客戶或營收曝險資料。
- 若缺資料，系統會優先使用已收集到的官方來源、MOPS、交易所、公司 IR、財報、法說會與可信財經媒體，自動補入 `config/company_knowledge.json`。
- PTT、Dcard、Mobile01、社群貼文、無 URL 或來源品質過低的資料，不會寫入正式公司知識庫；只會保留為情緒或輔助參考。
- 已存在的正式公司知識欄位不會被覆蓋，只會補空欄位或追加高品質 evidence source。
- `/value_scan` 報告 JSON 會固定保存 `structured_data.ai_candidate_evidence_pack`，方便追查 AI 實際收到哪些候選股資料。
- `/value_scan` 報告正文會新增「資料完整度矩陣」，逐檔列出財報細項、毛利率、籌碼、估值、TDCC、MOPS、來源事件與公司知識庫是否完整。
- 報告 JSON metadata 會保存來源品質分數摘要，協助檢查低品質來源是否被排除。
## 投研報告品質共用層（2026-05-24）

本次將公司知識庫補全、Evidence Pack、來源品質、資料完整度與 QA 提醒整理成共用的 Report Quality Layer，避免各指令各自輸出不同格式。

- 所有投研報告 JSON 都會在 `metadata.report_quality` 保存統一品質資訊。
- 所有投研報告 JSON 都會標記 `metadata.report_schema_version = report_quality_v1`。
- 所有投研報告 JSON 的 `structured_data` 都會包含共用欄位：`evidence_pack`、`data_completeness_matrix`、`data_coverage_score`、`source_coverage_summary`、`qa_warnings`。
- `/research` 的 evidence pack 會保存個股研究核心資料，例如股價、營收、財報、籌碼摘要、價值重估底稿與公司知識庫。
- `/value_scan` 的 evidence pack 會保存實際送入 AI 的 `ai_candidate_evidence_pack`、候選數量、排序與公司知識庫補全狀態。
- `/theme`、`/theme_radar`、`/theme_flow`、`/sector_strength` 的 evidence pack 會保存題材、候選公司、題材上下文、供應鏈與公司知識庫摘要。
- 資料完整度矩陣會依指令類型採用不同必要欄位：`/theme_radar` 以 `market_movers`、`theme_rankings`、`sector_strength`、新聞與 feature pack 為主；`/sector_strength` 以 `market_movers`、`sector_rankings`、新聞與 feature pack 為主；`/theme_flow` 保持較嚴格，會檢查題材、layer、盤面驗證、相關股票、供應鏈與公司知識庫。
- `/macro` 的 evidence pack 會保存總經、市場分數、波動、產業資金流、恐懼貪婪與公開總經資料。
- Markdown 報告會自動附加「報告資料完整度與來源品質」區塊，方便直接檢查資料覆蓋、來源數量與 missing data policy。
- 來源品質統一由來源等級、URL、官方/媒體/社群屬性、可用性與低可信來源規則評估；低品質來源不會作為公司知識庫正式寫入依據。
- 資料不足時，報告會在 `qa_warnings` 與資料完整度矩陣中留下可追溯提醒，避免 AI 將缺資料欄位硬補成投資結論。

## AI 投研 HTML 報告閱讀版優化（2026-05-24）

本次將所有透過 `research_center.report_builder.render_html()` 產出的 AI 投研 HTML 報告改為「閱讀版」渲染，不再只是把 Markdown 原文直接塞進網頁。

適用範圍：

- `/research`
- `/macro`
- `/theme`
- `/theme_radar`
- `/theme_flow`
- `/sector_strength`
- `/value_scan`
- `/report` 查詢既有報告時讀到的 HTML 檔

調整重點：

- 新增共用 `research_center/report_html_renderer.py`，統一處理 HTML 報告版型。
- Markdown 表格會轉成真正的 HTML `<table>`，含 `<thead>`、`<tbody>`、欄位標題與儲存格對應標籤。
- 桌機版表格維持表格閱讀方式；手機版會自動轉成卡片式列資料，避免橫向卷軸。
- HTML 預設顯示主報告；完整來源、資料品質、本地量化底稿、Metadata、QA 會放在獨立頁籤。
- `metadata.report_quality`、來源品質分數、資料完整度矩陣會在 HTML 的「資料品質」頁籤中集中顯示。
- `sources.json` 的完整來源清單會以卡片方式呈現，並保留 provider、provider_detail、日期、來源品質分數與網址。
- Markdown、JSON、sources.json 的原始輸出仍完整保留；本次只改善 HTML 閱讀體驗。

### HTML 長段落與粗體段落標題補強（2026-05-25）

- HTML 報告會保留主要段落節奏，不再把 AI 產出的多行內文全部合併成一大段。
- 行首粗體文字會被視為段落小標，例如 `**營收表現：**`、`**法人籌碼：**`，HTML 會轉成獨立小節標題與段落。
- 過長段落會依中文句號、問號、驚嘆號、分號等標點保守切分，避免手機與桌機閱讀時整段過長。
- 此調整只影響 HTML 閱讀版，不改 Markdown、JSON、來源清單與 AI prompt。
## /research 股票解析與結構化快取 fallback（2026-05-25）

- `/research` 個股解析會優先讀取本機 `stock_list.json`，可直接支援股票代號、完整 Yahoo symbol 與股票名稱，例如 `/research 光洋科 --deep --model deepseek`。
- 若本機清單已有股票資料，程式不會為了解析股票名稱而先呼叫 TWSE / TPEx 股票名稱 API，可降低公開 API 或網路暫時異常造成的指令失敗。
- 若即時結構化資料收集失敗，且近 7 天內存在 `.cache/research_structured/{日期}/{代號}.json`，`/research` 會改用最近快取繼續產出報告。
- 報告 JSON 會寫入 `structured_cache_fallback`，並在 `notes` 標示 fallback 日期與原始錯誤，避免使用者誤以為資料完全是當日即時抓取。
