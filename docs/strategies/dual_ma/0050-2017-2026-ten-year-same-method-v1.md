# 0050 2017～2026 十年同方法回測契約 v1

## 1. 目標

以既有已採納的 0050 長期監控生命週期、成交成本、公司行動處理及固定報告格式，建立一份不覆寫舊產物的 2017～2026 連續帳戶回測。

- 正式績效期間：2017-01-03 ～ 2026-09-30。
- 技術指標暖機：2016-01-04 ～ 2016-12-30，只供指標計算，不得計入績效或成交。
- 起始本金：500,000 元。
- 帳戶跨年度連續，不做年度重設、不追加本金、不借款。
- 2026 是截至 2026-09-30 的部分年度，報告必須明示。
- 主文成本情境：20 bps；底層保留 0／10／20 bps。

本契約分成 Phase 1 資料包與 Phase 2 回測報告。Phase 1 的 `READY_FOR_02` 經 00 驗收前，Phase 2 不得啟動正式績效。

## 2. 已釘選基準

### 2.1 舊版資料與報告

- 舊資料包 READY：`D:\code\stock_ai_bot\.workdata\dev\dual-ma\03-etf-research-package-v3\READY_FOR_02.json`
  - SHA-256：`8339B3322E7D51EE9906ECF4CB04A09D4AA76735D1F390555894733805683CD5`
- 舊報告 QA：[Git 保存副本](../../research/dual_ma/0050/accepted-history/base-v4/QA_ACCEPTED.json)（原執行位置：`.workdata/reports/dual-ma/etf/0050/base-v4/QA_ACCEPTED.json`）
  - SHA-256：`D130EBF069E98C5204A7F572BD1119BC6A3DE852CACA9C873E551575B2E46827`
- 固定報告模板：`docs/strategies/dual_ma/standard-backtest-report-v2.md`
  - SHA-256：`9576B92D4CE5BE8C46ED08173DA1416F744EE8A1D73AA25277C943600FAE4B3A`

舊資料包、舊 run 與 `base-v4` 報告必須 byte 不變。

### 2.2 FinMind 付費來源實測

2026-10-07 已用本機 `config/secrets.json` 的 `finmind_api_key` 驗證；不得把 token 寫入命令列、日誌、manifest、receipt 或產物。

- 會員：Sponsor；API limit 6,000/hour。
- `TaiwanStockPrice`，0050，2016-01-01 ～ 2026-09-30：2,612 rows，2016-01-04 ～ 2026-09-30。
- `TaiwanStockPriceAdj`，同區間：2,612 rows；此為付費限定資料，只能作交叉驗證，不可冒充原價成交資料。
- `TaiwanStockDividend`：21 rows，2016-08-03 ～ 2026-07-27。
- `TaiwanStockDividendResult`：21 rows，2016-07-28 ～ 2026-07-21。
- `TaiwanStockSplitPrice`：0050 共有 1 row，2025-06-18 分割。

若正式抓取與上述 probe 不同，保留實際 raw 與差異說明；不得為符合筆數而刪補資料。

## 3. Phase 1：01 十年資料包

### 3.1 工作區與輸出

- worktree：`D:\code\_codex_worktrees\dual-ma-selection\stock_ai_bot`
- branch：`codex/dual-ma-selection-2022h1`
- 新輸出：`D:\code\stock_ai_bot\.workdata\dev\dual-ma\04-etf-0050-research-package-2016-2026-v1`
- 可修改：01 所有權下的新 runner、adapter、tests、操作文件及新版本化輸出。
- 禁止修改：舊 v1/v2/v3 資料包、舊索引、既有三個 dirty 檔、main、live、Telegram、部署與正式監控。

### 3.2 資料來源與用途

1. 原價 OHLCV：優先封存 TWSE `STOCK_DAY` 2016-01 ～ 2020-12；2021～2026 重用舊 v3 已驗 raw。FinMind `TaiwanStockPrice` 作完整性與數值交叉驗證。
2. 付費還原價：封存 `TaiwanStockPriceAdj` raw/receipt/SHA，只作獨立連續性、分割與缺口檢查；不得直接用於成交或不經驗證地作為技術訊號。
3. 配息：優先使用 TWSE e添富可取得的 2016～2026 第一方事件；以 FinMind `TaiwanStockDividend` 及 `TaiwanStockDividendResult` 補充／交叉驗證。每筆分開保存除息日、基準日、發放日、每單位金額及來源 SHA。
4. 分割：沿用已驗 2025-06-18 4:1 第一方事件，並以 `TaiwanStockSplitPrice` 交叉驗證。不得新增交易。
5. 大盤基準在 Phase 2 由 02 延伸 TWSE 發行量加權股價報酬指數至 2017～2026；不得用價格指數冒充含息報酬指數。

### 3.3 兩條價格序列

- 成交序列：實際未還原 OHLCV；所有買賣、費稅、滑價與成交量判斷使用此序列。
- 技術序列：由截至當日已知且已驗的公司行動建立因果連續座標。分割前後須連續；vendor adjusted price 只能核對，不能取代因果序列。

分割時：股數乘 4；每股成本、停損、加碼價與 R 尺度除 4；總成本不變；分割日 transaction count 必須為 0。歷史股利維持事件當時每單位原始口徑。

### 3.4 Phase 1 交付與 gate

- 原始來源、receipt、source master、URL/params/status/retrieved_at/bytes/SHA。
- 2016～2026 原價、因果技術序列、股利事件與分割事件。
- coverage、artifact manifest、readback、READY_FOR_02、繁中摘要。
- 2016 暖機與 2017 正式績效日期邊界 fixture。
- 2025 分割跨越 fixture。
- 相同輸入冪等、斷點續跑、validate-only、空回應／欄位異動／403／429／SHA mismatch fail-closed 測試。
- 2021～2026 與舊 v3 的相同日期原價／事件交集必須逐筆核對；不一致要具名差異，不得覆寫舊 v3。
- 完成狀態只能是 `STOP_WAIT_00_QA`；未通過 00 QA 不得稱資料包完成。

## 4. Phase 2：02 十年回測與固定報告

### 4.1 工作區與輸出

- worktree：`D:\code\_codex_worktrees\dual-ma-backtest\stock_ai_bot`
- branch：`codex/dual-ma-backtest-2022h1`
- 新 run：`D:\code\stock_ai_bot\.workdata\dev\dual-ma\02-etf-0050-2017-2026-same-method-v1`
- 正式報告：[Git 保存副本](../../research/dual_ma/0050/accepted-history/2017-2026-base-v1/report.md)（原執行位置：`.workdata/reports/dual-ma/etf/0050/2017-2026-base-v1`）
- 舊 run 與 `base-v4` 必須 byte 不變。

### 4.2 固定方法

沿用已採納 0050 生命週期，不改規則：

1. 基本版－中線。
2. 基本版－長線。
3. 7% 風險寬度－中線。
4. 7% 風險寬度－長線。
5. 144 日線向上＋風險寬度 7%＋高於 21 日線 5%－長線。
6. 0050 全程持有，股利留現金。
7. 0050 全程持有，股利再投入。

起始前使用 2016 暖機；2017-01-03 才可建立交易。維持單一持倉、不可同日重進、可於新 regime 再次進場、原加碼／停損／結構退出／成本／現金限制不變。

### 4.3 固定報告

逐節對齊 `standard-backtest-report-v2.md`，至少包含：

- `report.md`
- `shared-account-report.md`
- `benchmark-risk-report.md`
- `trade-details.md` / `trade-details.csv`
- `scenario-summary.csv`
- `calendar-year-summary.csv`
- `daily-equity.csv`
- `transactions.csv`
- `corporate-actions.csv`
- `signal-opportunity-audit.csv`
- 資產曲線、回落曲線、manifest、readback、checkpoint。

主文使用白話中文；沒有交易的年度／策略必須說明沒有交易機會與被哪一道條件擋下。尚未賣出部位使用 2026-09-30 官方收盤價扣除預估賣出成本估值，與已實現損益分列。

### 4.4 相容性與完成 gate

十年主 run 從 2017 起累積資金與持倉，2022～2026 的帳戶結果不要求與 2022 起跑的 `base-v4` 相同。

但必須另做一個不覆寫的 compatibility replay：使用新資料包、從 2022-01-03、500,000 元起跑，逐交易、逐年、最終資產與舊 `base-v4` 核對。差異只能來自具名的已採納資料更正；否則 fail-closed。

完成後只能 `STOP_WAIT_00_QA`，由 00 驗：

- diff／所有權／dirty 檔與舊 run hash。
- 關鍵與完整測試。
- 全量 artifact SHA/readback。
- 2016 暖機不計績效。
- 分割日零交易、股數／成本／停損／配息正確。
- 每年期初等於前一年期末。
- 每種方法逐筆交易、費稅與原價可重算。
- compatibility replay 與 `base-v4`。
- 報告格式、連結、圖表與人眼可讀性。

## 5. 執行紀律

- 不以績效結果改規則、資料或期間。
- 不讀結果後換參數。
- 缺資料具名 `PARTIAL/HOLD`，不得造零或假補。
- 未指定新的額度停止線；若模型／rate 不可用、來源持續封鎖、輸入 SHA 改變或發生語意衝突，在安全原子邊界保存 checkpoint 後停止，不自行切換模型或來源語意。
- 01／02 依序各做一個完整批次，中途一般 checkpoint 不停等 00；各階段末才集中 QA。
