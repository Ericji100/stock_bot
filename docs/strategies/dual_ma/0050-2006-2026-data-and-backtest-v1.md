# 0050 2006～2026 資料延伸與同方法回測契約 v1

## 目標與順序

將 0050 正式回測期間延伸為 2006 年第一個經驗證的交易日～2026-09-30，從 500,000 元開始使用單一連續帳戶；年度只是同一條資產曲線的切片，不重設本金、持倉或策略狀態。本契約一次涵蓋資料與回測，但分兩道驗收：02 完成資料包後 `STOP_WAIT_00_DATA_QA` 並主動回報 00；只有 00 明確採納資料包，02 才執行正式績效；最後 `STOP_WAIT_00_QA` 並主動回報 00。兩階段均為大批完整執行，不按月／年停等。

完整 2005 年原價與事件資料原則上只供 SMA144、MACD(21,55,55) 等指標暖機；若取得來源不足，應具名 HOLD 並由 00 決定最早可交易日，不得用未來資料或假值補暖機。正式起日從 2006 年交易日曆決定，不猜測公曆首日是否交易。2026 年報告明示只截至 9 月 30 日。

## 唯一已採納基準

- 已採納 2016 暖機／2017～2026 資料包：`D:\code\stock_ai_bot\.workdata\dev\dual-ma\04-etf-0050-research-package-2016-2026-v1`；`READY_FOR_02.json` SHA-256 `D55FD56FFC01F3F130A83FCDC26E4D5A02ACCC617E815528C3364AAF27E5F9DC`；`artifact_manifest.json` SHA-256 `71D10A719A60F4D03D6D4CCBF3CC54FDCECC251037A61EF319D895B82DB1AD57`。
- 已採納 2017～2026 固定初始防守＋MACD 比較報告：[Git 保存副本](../../research/dual_ma/0050/accepted-history/2017-2026-ma21-fixed-stop-macd-v1-1/README.md)（原執行位置：`.workdata/reports/dual-ma/etf/0050/experiments/2017-2026-ma21-fixed-stop-macd-v1-1`）；`artifact-manifest.json` SHA-256 `CA2F0CA9EB287E00B4B33840BD5D3CAE543D40ED59378B57A8846AAF2331BBCE`；程式基準 commit `f8a69f5de2917bac7d02bbadb1a1694d24e7e74a`。
- 固定格式：`D:\code\_codex_worktrees\dual-ma-backtest\stock_ai_bot\docs\strategies\dual_ma\standard-backtest-report-v2.md`；SHA-256 `9576B92D4CE5BE8C46ED08173DA1416F744EE8A1D73AA25277C943600FAE4B3A`。
- 2017～2026 的原資料包、run、報告、README、正式 main 及現有 dirty/untracked 檔一律唯讀。不得修改舊產物以求 parity。

## 02 工作區與隔離輸出

- worktree：`D:\code\_codex_worktrees\dual-ma-backtest\stock_ai_bot`；branch：`codex/dual-ma-backtest-2022h1`。開始先記錄 HEAD、`git status` 與非本輪既有變更；保留它們。
- 本輪使用者特別授權 02 建立 0050 ETF 專用資料延伸；不得順便改 01 的股票選股來源、名單或資料包。
- 新資料包：`D:\code\stock_ai_bot\.workdata\dev\dual-ma\02-etf-0050-research-package-2005-2026-v1`。
- 新回測 run：`D:\code\stock_ai_bot\.workdata\dev\dual-ma\02-etf-0050-2006-2026-fixed-methods-v1`。
- 新報告單一主目錄：原執行位置為 `.workdata/reports/dual-ma/etf/0050/experiments/2006-2026-fixed-methods-v1`；長期保存入口為 [Git 保存副本](../../research/dual_ma/0050/accepted-history/2006-2026-fixed-methods-v1/README.md)，使用相對連結連到全部報告與附件。
- 只改 `backtests/dual_ma/**`、必要的 0050 專用 runner／tests／小型 fixtures 和上述新隔離輸出。跨越所有權、修改共用掃描器或交易規則文件前先回報 00。禁止 main/live/Telegram/部署/正式監控。

## Phase 1：資料包

1. 先以小型來源能力 probe 確認 2005 暖機與 2006～2015 原價 OHLCV、除息／配息／發放、分割及交易日曆可取得的日期與欄位，再大批抓取；不可假設 FinMind 付費層一定涵蓋所有早期資料。短期付費資料若可取，優先一次封存；官方第一方資料用於原價與事件驗證／補缺。來源策略、失敗碼和版本寫入 manifest；付費或免費來源切換不得靜默改語意。API token 不得進命令列、log、receipt 或產物。
2. 每個實際來源保留 URL／參數、取得時間、HTTP 狀態、原始 bytes、receipt、SHA-256 和重讀驗證。原價成交序列只能用未還原 OHLCV；調整價只作交叉驗證。技術序列由當日可知的已驗事件建立因果連續座標，不得直接把事後還原價當成交價或未驗訊號。
3. 原始股利依事件實際權利、除息、發放時點記帳；現金與再投入兩種全程持有口徑分開。2025-06-18 已驗 4:1 分割時，股數 ×4、每股成本與防守價 ÷4、總成本守恆，分割本身零交易。早期若另有公司行動，以當時官方事件與 FinMind 交叉驗證，不可憑價格跳變推造事件。
4. 保留逐交易日 coverage、差異與最小具名缺口；空回應、欄位異動、403／429、來源不符、SHA 錯誤皆 fail-closed。實作離線可重播的 bootstrap、冪等重跑、斷點續跑及 validate-only；不得為完成率造零、插值、前後填值或排除難驗年度。
5. 2016～2026 與既有採納資料包交集逐日核對原價、事件及因果技術座標；差異必須逐項說明且不覆寫既有包。資料包交付 source inventory、coverage、事件 ledger、raw/receipt、manifest、readback、QA fixture、繁中操作說明與精確續跑命令。未解資料列 `PARTIAL/HOLD`，不得宣稱完整可回測。
6. 完成後停止在 `STOP_WAIT_00_DATA_QA`，主動向 00 回報 commit、diff、測試、raw／receipt 數與 SHA、讀回結果、具名缺口和任何規格歧義。未收到 00 的明確資料採納訊息，不得啟動正式績效或讀取本輪策略結果。

## Phase 2：完整同方法回測

使用經 00 採納的新資料包、相同正式期間與 500,000 元本金，沿用已採納程式與參數，不看結果調參。比較且只比較：

1. 基本版－長線。
2. MA21 突破＋移動道氏防線＋MACD(21,55,55) 版本。
3. MA21 突破＋固定初始防守停損＋MACD(21,55,55) 版本。
4. 0050 全程持有，股利留現金。
5. 0050 全程持有，股利再投入。

各版本沿用既有進場、加碼、停損／停利優先順序、同日不得重進、現金約束、原價成交、費稅及 0／10／20 bps 假設；主文仍以 20 bps 呈現。新固定防守版不得啟用或繼承移動道氏防線；防守價只在分割等單位變更時等比例換算。截止日仍持有不強制賣出，按原價收盤及假設賣出成本估值，與已實現損益分列。不得把不完整或未經驗收的資料日期偷偷納入績效。

以 `standard-backtest-report-v2.md` 的節序和白話繁中用語產出 `README.md`、`report.md`、`shared-account-report.md`、`benchmark-risk-report.md`、`trade-details.md`，以及完整 `trade-details.csv`、`transactions.csv`、`daily-equity.csv`、`calendar-year-summary.csv`、`scenario-summary.csv`、`corporate-actions.csv`、`benchmark.csv`、進場機會與 MACD／停損事件稽核、資產與回落 SVG、input/artifact manifest、readback、checkpoint 與 parity。主目錄的 `README.md` 必須連到每一份，連結存在性逐一驗證；零交易或缺資料年份要白話說明原因。

## 相容性、QA 與停止條件

- 2006 起跑的新連續帳戶，2017 年之後可能帶著不同本金／持倉；不能拿新報告的 2017～2026 切片硬要求與原十年報告金額相同。另用新資料包、500,000 元、2017-01-03 起跑、同截止日執行隔離 compatibility replay，逐筆、逐年及資產曲線與已採納十年 run 比對；若有已採納資料修訂，列明差異，不為 parity 改新資料。
- 00 資料 QA：來源 inventory、早期真實 OHLCV／事件抽樣、raw／receipt/SHA 全量 readback、完整交易日曆、2005 暖機排除、早期股利、2025 分割、舊交集、離線重播、fail-closed、舊產物 byte 不變。缺陷修正後才採納。
- 00 回測 QA：diff／所有權與完整測試、五法 × 成本情境、2006 起始資產、逐年連續性、逐筆進出與原因、MACD／停損事件因果、股利／分割、費稅、每日資產與大盤比較、逐筆獲利率、全量 SHA/readback、所有連結及舊產物 byte 不變；若不通過退回修正。02 回報不等於 00 採納。
- 最新人類停止線為 Codex `usedPercent >= 98`（剩餘 <=2%）。觸線、模型／rate 不可用、持續來源封鎖、輸入 SHA 改變或規則／來源語意衝突時，在安全原子邊界保存 checkpoint、manifest、SHA、已跑測試、背景程序停止證據與精確續跑命令後 STOP，不自行換模型或偷改來源。一般中途 checkpoint 不停等 QA。
- 02 兩階段完成時均須依使用者明確授權主動回報 00；只有 00 正式驗收後才可對使用者稱資料或回測定版。不得因為績效好壞改規則、排除年份或覆寫舊報告。
