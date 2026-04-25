# Telegram 台股策略機器人

這是一個以 Python 開發的台股 Telegram Bot，現有程式包含三條主要功能線：

1. 監控清單技術訊號
   針對 config.json 的 monitor_stocks 執行 21MA 突破、MACD 翻紅後回測突破、105MA 突破。
2. 全市場量化選股
   掃描全台上市櫃股票，依月營收與毛利率條件分組分級，輸出結構化報告。
3. 個股資料匯出
   將單一股票的價量、法人、融資融券、月營收、季財報彙整成 Excel。

本專案同時保留多個回測腳本，可單獨在命令列執行，用來驗證不同版本的策略與組合表現。

---

## 功能總覽

- Telegram Bot 指令管理
- monitor_stocks 名單管理
- 官方上市櫃股名與代碼同步
- 監控清單技術掃描
- 全市場選股掃描
- 個股資料匯出 Excel
- 多版本回測腳本
- Windows 一鍵啟動腳本

---

## 專案結構

### 核心程式

- main.py
  Telegram Bot 主程式，負責註冊指令、定時任務、啟動後初始掃描與訊息發送。
- stock_scanner.py
  全市場選股引擎，負責上市櫃名單同步、月營收抓取、價量快取、毛利率計算與分組分級。
- data_fetcher.py
  個股資料抓取層，整合 TWSE、TPEx、MOPS 與 Yahoo Finance，供 /export 使用。
- export_service.py
  匯出 Excel 的組裝層，生成 Price_History、Monthly_Revenue、Quarterly_Financials、Strategy_Summary 四個工作表。

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
- requirements.txt
  Python 相依套件。
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

---

## Telegram 指令

- /start
  顯示可用指令。
- /list
  查看目前 monitor_stocks 清單。
- /add 代碼 名稱
  將股票加入 monitor_stocks。若未提供名稱，會自動從官方股名資料補值。
- /del 代碼
  從 monitor_stocks 移除股票。
- /check
  對 monitor_stocks 執行技術面掃描。
- /scan
  對全市場上市櫃股票執行量化選股掃描。
- /export 代碼
  匯出單一股票的 Excel 資料包。

---

## 監控清單技術掃描

main.py 目前內建三種技術訊號，/check、啟動後初始掃描與每日定時掃描都會使用同一套邏輯。

### 策略 A：21MA 突破

- 使用 500 日日線資料
- 計算 21MA
- 條件為昨日收盤價低於 21MA，今日現價高於 21MA
- 訊息中會附上前三日最低價作為停損參考

### 策略 B：MACD 翻紅後回測突破

- 使用 21 / 55 / 55 參數計算 DIF、DEA、MACD Histogram
- 只在當前為紅柱時檢查
- 必須曾於紅柱期間回測 21MA
- 今日現價需突破紅柱區間前高，且站上 21MA

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
- Telegram 訊息有自動分段機制，避免單則超過平台長度限制

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
🔍 今日台股選股掃描報告
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
- 策略 B：MACD 翻紅回測再突破
- 固定停損與 5% 目標後跌破 MA21 出場

### backtest_v2.py

- 新增策略 C：綠柱中的 MA21 突破
- 策略 C 停損後，直到 MACD 再次翻紅前不再進場

### backtest_v3.py

- 支援 A、B、C 組合模式，例如 A+B、B+C
- 支援最大持股數限制
- 納入持有期間股息計算

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
- scan_settings
- monitor_stocks

範例：

```json
{
  "api_token": "YOUR_TELEGRAM_BOT_TOKEN",
  "chat_id": "YOUR_CHAT_ID",
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

---

## 已知限制

- 大量抓取仍依賴 Yahoo Finance，可用性受外部服務穩定度影響
- 上櫃歷史價量在目前實作中仍由 Yahoo Finance 補齊，不是全程官方來源
- 回測腳本屬研究工具，與 Telegram Bot 主流程分離，輸出格式與主系統不同
- multitasking.py 目前只有最小實作，並未實際被主程式使用

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

全市場掃描檢查：

```bash
python stock_scanner.py
```

---

## 維護建議

- 若 /scan 結果異常偏少，先檢查 .cache 與 stock_list.json 是否為最新資料
- 若 /export 某些欄位缺漏，優先確認官方端點是否暫時無資料或該公司尚未公告
- 若 Telegram 無法發送，先確認 config.json 的 chat_id 與 api_token 是否正確