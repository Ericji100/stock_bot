# Telegram 台股策略機器人

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
  全市場技術面選股引擎，負責硬篩、日 K 快取、MA / MACD / KD 指標計算與交叉訊號判斷；MACD 回測突破與背離 Zone 函式保留，但目前在技術面選股中暫停執行。
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
- config/theme_supply_chain.json
  題材供應鏈設定檔，目前內建 AI 伺服器、半導體、重電、機器人等 profile，可持續擴充。
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
  停止目前聊天室正在執行中的耗時任務，例如 /check、/scan 選股、/export、/stock_chart、/tmf_chart、/morning、/noon。

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
  產出單一個股研究報告。支援 `--source-only`、`--score`、`--deep`、`--date YYYY-MM-DD`。
- /macro [市場] [主題]
  產出宏觀市場研究報告。支援 `--source-only`、`--brief`、`--deep`、`--date YYYY-MM-DD`。
- /theme 題材
  產出題材研究報告。支援 `--source-only`、`--deep`、`--date YYYY-MM-DD`、`--top N`。
- /value_scan [候選池]
  產出價值重估掃描報告。支援 `--source-only`、`--deep`、`--date YYYY-MM-DD`、`--top N`。
- /report latest
  查詢最近一次 AI 投研報告；也可用 `/report 2330 latest`、`/report macro latest` 等方式查詢。

AI 投研報告會保存到 `reports/`，並寫入 `database/stock_research.db`。Telegram 預設只回覆摘要並傳送 Markdown / HTML 檔，JSON 留在本地供 API 或後續 Agent 使用。

AI 投研指令範例：

```text
/research 2330
/research 台積電 --deep
/research 6217 --source-only --date 2026-01-07
/research 6217 --score
/macro 台股 AI --deep
/theme AI伺服器 --top 20
/value_scan 精選選股 --top 30 --deep
/report latest
```
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

- 使用者輸入 /scan 後，機器人會先顯示 Inline Keyboard，不會立刻直接全掃。
- 可選策略如下：

```text
請選擇選股掃描策略：
1. 財報營收選股
2. 60 日法人動態選股
3. 投信認養股
4. 法人持股比例增加
5. 每週大戶持股選股
6. 技術面選股
7. 全部執行
8. 精選選股
```

- 點擊單一策略時，只執行該策略並回傳對應報告。
- 點擊全部執行時，會依序回傳財報營收報告、4 份籌碼報告與技術面選股報告。
- 點擊精選選股時，會同時執行技術面、營收財報與四個法人大戶策略，並以技術面正面訊號作為主要分類，只列出同時命中營收財報或法人大戶 2 個以上策略的股票。
- `/scan` 可接日期參數，例如 `/scan 2026-05-05`、`/scan 2026/05/05`、`/scan 20260505`、`/scan 5/5`；選單送出後，法人選股、技術面選股與精選選股都會使用該日期作為目標資料日期。
- 日期參數不可晚於今天。若未輸入日期，系統預設使用 `get_tw_today()` 的今天日期。
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

- **參數設定**：MACD (21,55,55)、KD (9,9,55)、均線 (21MA/105MA)。
- **目前執行的正面訊號**：均線突破 (21MA/105MA)、MACD/KD 黃金交叉。
- **目前執行的負面訊號**：MACD/KD 死亡交叉。
- **暫停策略**：MACD 回測突破、MACD/KD 低檔背離、MACD/KD 高檔背離目前保留程式碼但不執行；之後可透過 technical_scanner.py 的開關恢復。
- **操作方式**：輸入 `/scan` 後，於選單中點擊「技術面選股」。
- **日 K 來源順序**：先讀 `.cache/technical_daily` 本機快取，缺資料才抓 Yahoo Finance；Yahoo 無資料時改用 Fugle，成功後寫回快取。
- **指定日期**：可用 `/scan 2026-05-05` 先指定目標日期，再點擊「技術面選股」；若查詢過去日期，本機快取只要已涵蓋該日就會直接使用，不受 12 小時 TTL 限制。
- **MACD 回測突破保留邏輯**：目前暫停執行，但函式已調整為「今日第一次突破回測前高」：紅柱區間內最近一次低點 <= MA21，取回測前高作為關卡，昨日收盤 <= 關卡、今日收盤 > 關卡且今日收盤 > MA21。
- **背離保留邏輯**：背離使用 MACD 柱狀圖或 KD 交叉區間切分，透過 pandas shift() 遮罩建立 Zone，再比較最近兩段區間的價格與指標極值；目前暫停執行。

### 精選選股

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
  依來源網域分成 Level 1 到 Level 5，避免論壇或未具名消息被當成事實。
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
- `/theme` 已加入 `config/theme_supply_chain.json` 供應鏈 profile，可依題材匹配關鍵字、產業與供應鏈節點；仍需後續補公司產品、客戶、營收占比與供應鏈關係資料庫。
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

---

## 監控清單技術掃描

monitor_service.py 目前內建 21MA 突破、MACD 翻紅後回測突破、105MA 突破三種技術訊號；/check、啟動後初始掃描與每日定時掃描會使用 collect_monitor_signals() 掛入的訊號，main.py 只負責觸發與發送 Telegram 訊息。

目前實際啟用的監控訊號為 21MA 突破與 105MA 突破；MACD 翻紅後回測突破函式保留在 monitor_service.py，但尚未掛入 collect_monitor_signals() 執行流程。

### 策略 A：21MA 突破

- 使用 500 日日線資料
- 計算 21MA
- 條件為昨日收盤價低於 21MA，今日現價高於 21MA
- 訊息中會附上前三日最低價作為停損參考

### 策略 B：MACD 翻紅後回測突破

- 使用 21 / 55 / 55 參數計算 DIF、DEA、MACD Histogram
- 只在當前為紅柱時檢查
- 找出當前紅柱區間內最近一次低點 <= MA21 的回測日
- 取該回測日之前的最高價作為突破關卡
- 昨日收盤需 <= 回測前高，今日現價需 > 回測前高，且今日現價 > MA21
- 此邏輯已同步到 backtest_v1.py、backtest_v2.py、backtest_v3.py、backtest_v4.py 的策略 B，但監控端目前尚未啟用此訊號

### 策略 C：105MA 突破

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

### backtest_v1.py

- 策略 A：MA21 突破
- 策略 B：MACD 翻紅回測再突破，已同步為「今日第一次突破回測前高」邏輯
- 固定停損與 5% 目標後跌破 MA21 出場

### backtest_v2.py

- 新增策略 C：綠柱中的 MA21 突破
- 策略 C 停損後，直到 MACD 再次翻紅前不再進場

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
- `/value_scan` 已加入交叉驗證框架，但公告、法人報告摘要、財報細項仍需要更完整的來源匯入後，分數可信度才會更高。
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

本次針對上一輪剩餘的三個重點繼續開發：`/macro` 官方資料接入、公司知識庫擴充、`/value_scan` 公告與財報細項交叉驗證。

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
- `company_knowledge.json` 已擴充，但仍是人工 starter database，後續要持續補每家公司產品、客戶分類、營收占比與證據來源。
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

### 新增與調整內容

- `config/prompts/`
  - `base.md`：所有 AI 投研指令共用規則，包含資料可信度 Level 1～5、不得捏造、資料不足需明示、論壇只能作情緒參考、禁止保證獲利與自動下單等限制。
  - `research_summary.md`、`research_score.md`、`research_deep.md`：對應 `/research` 一般、評分、深度模式。
  - `macro.md`、`macro_deep.md`：對應 `/macro` 一般、brief、deep 模式。
  - `theme.md`、`theme_deep.md`：對應 `/theme` 一般、深度模式。
  - `value_scan.md`、`value_scan_deep.md`：對應 `/value_scan` 一般、深度模式。
  - `source_only_summary.md`、`telegram_summary.md`：保留資料彙整與 Telegram 摘要用途。
- `config/scoring/`
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
- `/macro` 的正式台指選擇權 IV、完整期貨籌碼與法人資金流，仍需要官方可穩定下載來源或付費資料源。
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
- 本地評分引擎
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
  - 驗證本地評分會寫入 JSON `scores`。
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

### 仍屬資料源或長期維護限制

- 本地評分引擎已依規格建立，但沒有資料的項目會保守給分；例如 CAGR、護城河、完整產品營收占比仍需要資料庫補齊。
- QA validator 會檢查並提示缺漏，但目前不會自動二次呼叫 Gemini 重寫報告，以避免增加 token 與成本。
- 正式台指選擇權 IV、完整期貨籌碼、完整法人報告內容仍需要穩定官方/付費/授權資料源。

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

- 單股研究新增 `TDCC 籌碼集中度`、`估值安全邊際`、`毛利率快取驗證` 三個本地評分項。
- 價值重估候選股合成分改為：重估分 60%、證據覆蓋分 25%、TDCC 10%、估值 5%。
- `value_validation` 新增 TDCC、官方估值、毛利率快取三項 evidence coverage 檢查。

限制說明：

- 免費來源皆為 best-effort；官方網站改版、阻擋、欄位變更或無資料時，系統會回傳 `unavailable`、`empty` 或 `official_reference`，不會中斷報告產出。
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

本地 fallback 報告已不再只顯示前 12 項評分，會列出完整本地評分項。JSON 仍保留完整 `scores` 與 `buy_rating`。

### 論壇來源 CMD 進度

PTT、Dcard、Mobile01 搜尋會在 CMD 顯示：

- 搜尋開始
- 成功來源數
- notes 數量
- individual failure note / blocked / unavailable reason

論壇資料仍是 Level 4，只能作市場情緒參考。

### `/value_scan` 候選名單流程

`/value_scan` 現在將「名單來源」、「分析數量」與「分析模式」分離。

名單來源：

- 精選選股名單：調用 `stock_scanner.scan_tw_market()` 取得候選名單，再做本地重估排序。
- 全市場初篩：讀取上市櫃 universe 後做本地重估排序。
- 我的持股：讀取 `portfolio.json`。
- 自訂股票清單：使用者輸入股票代號清單。
- 最近掃描結果：目前若沒有已保存掃描結果，會提示先執行 `/scan` 或改用其他來源。

分析數量：

- 前 10 名：本地初篩排序後，只送前 10 檔給 Gemini。
- 前 30 名：本地初篩排序後，只送前 30 檔給 Gemini。
- 全部：以 `--top 9999` 執行，可能較慢且消耗較多 AI 額度。

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
2. 選分析數量：前 10 名、前 30 名、全部。
3. 選分析模式：一般重估、深度重估、只看資料來源。
4. 選資料日期：最新日期、指定日期。

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

選定後，系統會用該掃描結果的股票名單做本地重估排序，再依「前 10 / 前 30 / 全部」送 Gemini 分析。

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

這些資料使用 Yahoo Finance 公開市場 proxy，搭配既有 TWSE、TAIFEX、Global VIX、台股指數與類股流動性 proxy。正式 IV、日內選擇權、完整籌碼與逐產業法人資金流仍需官方穩定檔或付費資料。

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
- 本地評分仍維持保守，不會只因 AI 推測就提高分數。

下一步若要更嚴格，可把 knowledge draft 審核後轉入 SQLite company knowledge table，再讓本地評分引擎直接讀取結構化欄位。

### 本次驗證

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_free_sources tests.test_research_center_new_features
.\.venv\Scripts\python.exe -B -c "import main; import research_center.api_app as api; from research_center.telegram_handlers import AI_CALLBACK_PREFIX; from research_center.recent_scans import extract_stock_codes; print('imports ok', bool(api.app), AI_CALLBACK_PREFIX, extract_stock_codes('2330 6217'))"
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
- 已加入 `/research --score` 的本地推薦買入評分 `buy_rating`，以 1 到 5 分呈現，並在 JSON 與 fallback Markdown 中保留。
- fallback Markdown 已改成列出完整評分項目，不再只顯示前 12 項；JSON 仍保留完整 `scores`。
- `/value_scan` 已支援精選選股名單、全市場初篩、我的持股、自訂股票清單、最近掃描結果等股票池，並先做本地初篩排序，再依前 10、前 30 或全部送入 AI 分析。
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
- 正式台指選擇權 IV、完整波動率曲面、逐日或盤中選擇權資料，通常需要 TAIFEX 正式資料、資料商或付費來源。
- 完整期貨籌碼、逐產業法人資金流、長期可回測資金流資料，目前免費來源只能 best-effort 補 proxy，還不到正式量化資料庫等級。
- 授權法人報告、券商研究摘要與內部投研內容不能直接抓取或重製，只能引用公開新聞、法說會、公告與公開資料。
- 公司產品、客戶、供應鏈、營收占比、護城河、轉型效益等資料，可以透過 Gemini Search 輔助整理，但仍需要長期審核後沉澱成知識庫，不能完全依賴單次搜尋。
- 嚴格歷史回測級 `--date` 需要保存「每個來源在當時可見的版本」。目前程式已具備 snapshot 架構，但必須靠未來每日/每次報告持續累積資料，才會逐漸接近完整防偷看未來。

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
- `--top 9999` 代表盡量輸出全部候選；若候選池是全市場，仍可能花很久。建議全市場深度重估先用前 10 或前 30，持股或自訂清單再使用全部。

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

## AI 投研 Gemini Search discovery 與本地評分邏輯修正（2026-05-07）

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

另外同步修正 `/research` 一般模式的本地評分邏輯：

- `/research 代號` 一般模式不再產生完整 17 項本地量化評分。
- `/research 代號 --score` 與 `/research 代號 --deep` 才會產生 17 項本地評分，並交給 AI 依規格整理、補充來源與保守判讀。
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

新規則：

1. `/value_scan 股票池 --top N` 代表先從股票池取出最多 N 檔候選股。
2. 排名表必須列出這 N 檔候選股。
3. `六、個股重估分析` 也必須逐檔分析這 N 檔。
4. 不得只分析第一名，不得使用「以某檔為例」或「其餘略」取代逐檔分析。
5. 每一檔候選股都需包含：舊市場標籤、新市場標籤、重估證據、營收與財報驗證、法人籌碼與技術確認、是否只是蹭題材、重估分數、未來 1～3 個月觀察重點、風險與反證。

程式保底機制：

- `config/prompts/value_scan.md` 與 `config/prompts/value_scan_deep.md` 已強制 AI 逐檔分析所有候選股。
- `research_center/report_builder.py` 會在 `/value_scan` 報告尾端自動附加 `完整候選股逐檔重估分析`，直接依據本次實際送入 AI 的 `structured_data.candidates` 產生。
- `research_center/report_validator.py` 會檢查 `/value_scan` 報告是否缺少候選股逐檔分析，並在 QA 提醒中標出。

這代表：即使 Gemini 正文只寫第一名，新報告仍會由程式自動補上所有候選股的逐檔分析底稿。

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

### 搜尋流程

非歷史日期、非 source-only 的 AI 投研指令會先建立同一批 discovery tasks，然後執行：

1. Gemini Search / Grounding 多段短 prompt。
2. MiniMax Search：Serper Google Search 取得搜尋結果。
3. Jina Reader 讀取搜尋結果 URL 的正文內容。
4. MiniMax-M2.7 對 Serper/Jina 內容做來源摘要。
5. Gemini Search 來源、MiniMax Search 來源、本地資料來源會合併去重，再送入正式分析 prompt。

若 Jina 額度用完或網頁讀取失敗，程式會保留 Serper snippet 作為低深度來源，並在 CMD 顯示失敗訊息，不中斷報告產出。

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
5. 新增 Serper `site:` 搜尋 fallback。
   - 若 PTT、Dcard、Mobile01、理財寶直接抓取失敗或無結果，會使用 Serper Google Search 搜尋對應站內結果。
   - 例如：`site:ptt.cc/bbs/Stock 5425 台半`。
   - 若 Serper API Key 未設定，CMD 會顯示 fallback 略過原因。

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

調整前：

1. 選股票名單來源：我的持股。
2. 再選要分析幾檔：前 10 / 前 30 / 全部。
3. 再選分析模式與日期。

調整後：

1. 選股票名單來源：我的持股。
2. 系統直接預設分析全部持股，等同 `--top 9999`。
3. 直接進入分析模式選單。

原因：我的持股通常本來就是小型清單，額外詢問前 10 / 前 30 / 全部實用性低，且容易誤以為只會分析部分持股。

保留不變：

- 精選選股名單、全市場初篩、最近掃描結果仍保留「前 10 / 前 30 / 全部」選單，因為這些股票池可能很大，需要先限制候選名單以節省時間與 token。
- 自訂股票清單也預設分析全部股票，等同 `--top 9999`。

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

調整後 HTML 預設顯示「主報告」頁籤，讓打開檔案時先看到摘要、主要章節、評分/水位/結論等正文內容。

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
.\.venv\Scripts\python.exe -B -m unittest tests.test_minimax_integration tests.test_report_schema tests.test_research_center_new_features tests.test_prompt_contracts tests.test_telegram_menus tests.test_free_sources
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
    .\.venv\Scripts\python.exe -B -m unittest tests.test_minimax_integration tests.test_report_schema tests.test_research_center_new_features tests.test_prompt_contracts tests.test_telegram_menus tests.test_free_sources

結果：33 tests OK。
