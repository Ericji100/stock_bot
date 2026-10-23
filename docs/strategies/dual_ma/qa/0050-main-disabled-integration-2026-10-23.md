# 0050 趨勢策略：main 整合驗收與清理

日期：2026-10-23（Asia/Taipei）。裁決：**准予整合，監控維持關閉**。13個開發工作樹已清除；7個測試暫存根的刪除受執行政策阻擋，尚未清除。

## 使用者要求與實際結果

- 已將03最後接受版本 `49e1a0469b266829e345f74d9bdd3ba3d9d3e765` 整合到本機 `main`；合併提交 `4d433e66e33e002888a010294f7c933782f1f738`，基底 `6130baa56c3e92279ab5cb7ac3a1871028891051`。
- `config/etf_monitor.json` 的 `enabled=false`、`mode=shadow` 保持不變。專用群組固定 `-5527055845`，沒有第二份 Bot token、第二個 polling 或啟動 BAT。
- 本次沒有向新舊群組發送 Telegram、啟動正式 Bot、重啟 BAT、啟用 ETF 排程或 push 遠端。日後啟用必須另經使用者授權。
- 02的二十年研究不在整合／清理範圍，未要求暫停或改動其工作樹、輸入及回測。

## 原選股功能與群組的獨立驗收

以實際啟動環境 `.venv/Scripts/python.exe` 在禁止外網、隔離workdata的測試環境執行：

| 驗收 | 結果 |
|---|---|
| ETF、Telegram、既有選股排程等受影響回歸 | 323通過、1跳過、0失敗／錯誤 |
| 00另外撰寫的獨立關閉狀態／原app驗收 | 8通過，清理後再跑8項亦全通過 |
| 整份 `main.py` 語法結構 | 扣除新增ETF import及2個hook後，與整合前相同 |
| 實際main/post_init函式的模擬註冊 | 原11個每日、3個重複、1個啟動job及全部handler一致 |
| 監控關閉時 | 不建立ETF runtime、資料庫或排程；仍為單一Application／polling |
| 缺設定、損壞設定、錯群組、第二token | 隔離ETF、不影響原app、不發送、不回退原群組 |
| 原群組設定、BAT及使用者檔案 | SHA讀回完全一致 |

沒有為了測試而真的啟動正式BAT。測試執行實際啟動函式並以mock取代Telegram及外部服務；BAT原文另以hash驗證。網路封鎖只允許Windows asyncio內部socketpair建立；既有測試的外部連線嘗試被拒絕，沒有實際Telegram送出。

原有5個modified及595個untracked檔案均保持原樣；不將它們混入整合提交或為了乾淨git status而刪除。正式資料的current指標未改。

## 完整歷史與服務生命週期

重新由本次main程式執行2006-01-02～2026-09-30凍結歷史，共5093日／15279個時段job，結果 `PASS_EXACT`：

- 183筆訊號／次日執行排程、181列交易帳、5093列每日淨值、63列公司行動帳及91列交易明細逐欄一致。
- 235次重啟查核通過；策略狀態、持倉、停損、MACD、日期、價格、成本、現金及待執行指令差異全部0。
- 未來資料暴露0，Telegram嘗試0。
- 逐日狀態SHA256：`e8ab3587332175596c1381193ff0b29ea97a53a2d3d4cdc5e56e4c4a9310101f`。

這證明指定歷史及測試案例一致，不是未來獲利保證，也不等於正式監控已開啟。跨日停機、重試、資料失效、冪等與路由等邊界另由上述受影響回歸涵蓋。

## 清理與可恢復性

只清理03監控開發的13個工作樹，不包含原0050策略研究、其他ETF或02工作。恢復資料先驗證，再移除實體：

- 建立13個 `refs/codex/cleanup-snapshots/03-*` Git恢復參照；45個modified/untracked路徑的內容逐一驗證，沒有改動原index。
- 開發資料庫、紀錄、回執及必要ignored證據按內容去重壓縮成 `03-ignored-recovery.zip`，Python／pytest等可重建快取不保留。3864個檔案引用、2197個內容blob，原202302364 bytes壓成6231780 bytes；CRC與全部內容SHA讀回通過。
- ZIP SHA256：`4166da4c44751349a27d4c7e3a38f2ec98c350c3caae7c1bfc1d5b9fa3e41c3b`。
- 13個工作樹及13個外層空目錄已實際移除，對應Git工作樹註冊剩餘0。正式程式、文件、策略規則、凍結報告與驗收證據保留。
- managed archive當時只回queued且實體仍在；該歷史記錄未改寫。可靠恢復依上述實際驗證的Git參照與ZIP，不把queued回覆當作已完成備份／刪除。

必要時可由 `git worktree add --detach <新路徑> refs/codex/cleanup-snapshots/03-<名稱>` 恢復原始碼；ZIP內manifest提供各路徑到內容blob的映射。不要只解壓ZIP就以為它含完整Git原始碼。

### 尚未刪除的測試暫存

以下7根共201273603 bytes（約192 MiB）。原生PowerShell刪除在執行前遭政策阻擋，沒有刪除，也未改用其他工具繞過，因此不宣稱暫存清理全部完成：

- `C:/Users/紀成達/AppData/Local/Temp/etf-causal-service-replay-6e6ipt82`
- `C:/Users/紀成達/AppData/Local/Temp/etf-causal-service-replay-agfswq_k`
- `C:/Users/紀成達/AppData/Local/Temp/etf-causal-service-replay-bl72s1bq`
- `C:/Users/紀成達/AppData/Local/Temp/etf-causal-service-replay-co9n63jl`
- `C:/Users/紀成達/AppData/Local/Temp/etf-causal-service-replay-i1xtd75f`
- `D:/code/stock_ai_bot/.workdata/q03-int-tmp`
- `D:/code/stock_ai_bot/.workdata/reports/dual-ma/qa/0050-main-disabled-integration-qa00-20261023/postmerge-test-tmp`

前5根為歷史離線重播資料庫，後2根為本次測試fixture；不是正式持倉資料庫。精確根、大小及原始清冊保留於本機證據根。

## 證據、重現與限制

機器摘要：[整合與清理稽核](0050-main-disabled-integration-2026-10-23.json)；[SHA清冊](0050-main-disabled-integration-2026-10-23.manifest.json)。完整本機證據根：

`D:/code/stock_ai_bot/.workdata/reports/dual-ma/qa/0050-main-disabled-integration-qa00-20261023`

該根包含：`full-history-replay.json`、測試XML、`initial-user-state.json`、`evidence.json`、`git-recovery-snapshots.json`、`ignored-recovery-audit.json`、`git-worktree-removal-results.json`、`empty-parent-cleanup-results.json`、`test-temp-cleanup-results.json`及恢復ZIP。

在專案根以 `.venv/Scripts/python.exe -B <證據根>/run_offline.py pytest <證據根>/test_main_disabled_independent.py -q` 可重跑獨立驗收；完整歷史用同一wrapper的 `replay stock_data/versions/2026-09-30-v3/market-data`；最後以 `finalize_integration.py` 核對原檔、恢復參照、程式SHA與清理結果。

本次只重跑受影響範圍，不宣稱整份repository全綠。歷史完整測試基準仍有42 failures／24 errors，先前歸因及限制保留，沒有用修改無關檔案、真密鑰或外部連線製造假綠。測試工具曾因Windows內部socketpair封鎖與過長暫存路徑產生失敗；只修正測試封鎖器與縮短basetemp後，產品及案例未改而全部通過，初次失敗XML亦保留。
