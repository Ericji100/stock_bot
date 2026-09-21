# Hybrid Monitoring V2 readiness ledger

版本：`hybrid-monitoring-v2-readiness-v1`
狀態：`IN_PROGRESS_NOT_AUTHORIZED_FOR_PERFORMANCE`
更新日：2026-09-08

本檔只追蹤「混合式正式監控機制」達成狀態；不得把測試通過、單案 pilot 或研究校準誤稱為正式 V3 績效。

## 不可跨越的順序

1. 完成並驗證 DRAFT 元件。
2. 產生完整 outcome-blind review-point universe。
3. 抽取並由人類先凍結 blinded course gold rubric。
4. 凍結 protocol／prompt／schema／程式／模型／來源／樣本及所有 SHA-256。
5. 執行 120 案三輪一致性；任何 fail 都建立新協定版本並從 freeze 前重來。
6. 通過後才執行完整共同結構 AI ledger，鎖定 ledger SHA-256。
7. ledger 鎖定後才解封身分及期後行情，先回放 V3。
8. V3 達初步可行門檻後，才以同一 ledger 回放策略 V1／V2。

## 正確性層級

| 層級 | 驗證內容 | 目前狀態 |
|---|---|---|
| L0 `CAUSAL_DATA_INTEGRITY` | 所有資料及 evidence 日期不晚於 `as_of`；無身分、舊 AI、MFE／MAE／損益／期後公司行動 | 測試通過；正式全量尚未產生 |
| L1 `DETERMINISTIC_COURSE_INVARIANT` | 控制點確認時序、candidate/ref、stop 歸屬及 gate／permission 硬規則 | 測試與 1 案 pilot 通過；正式樣本尚未執行 |
| L2 `RESEARCH_CALIBRATION` | 重現使用者／課程已明示的結構觀點，不作績效證據 | 8 案快照已重建；0/20 人工 rubric 凍結，`FAIL_CLOSED` |
| L3 `BLINDED_COURSE_GOLD_HOLDOUT` | 固定抽樣後，人類只看匿名 as-of 封包與課程條款先凍結 gold | 工具完成；正式母體、抽樣及人工標註未完成 |
| L4 `THREE_RUN_REPEATABILITY` | 同一 120 案獨立三輪；permission／scenario-phase／critical atoms／action signature | 未開始 |
| L5 `FORWARD_PERFORMANCE_VALIDATION` | 完整 ledger 鎖定後的歷史回放與未來影子監控 | 禁止讀取／未開始 |

## 2026-09-08 Candidate2 全量前置稽核

- 已完成 1,029 檔、151,804 股票日的 outcome-blind 全量建置，得到 7,180 個匿名 review points；來源序號 missing／overlap／unexpected 均為 0。
- `FRESH_Q1_OBJECTIVE_PROXY（新生定錨客觀候選）` 為 0，低於主樣本加兩組備援所需的 45；正式抽樣器已 fail-closed，沒有建立 holdout plan，AI 呼叫數為 0。
- 原因是 Candidate2 把任一歷史父代／修正配對錯接成當前 trigger 的有效複製背景，使 Fresh 永久不可達，且 Mature／Macro 候選過寬。
- Candidate2 保留為 `SUPERSEDED_BEFORE_AI（AI 前淘汰）` 的不可變失敗證據；不得看績效後重標，修正必須另開版本並重跑相同 outcome-blind 稽核。

## 元件與執行狀態

| 項目 | 目前證據 | 尚缺 |
|---|---|---|
| V1 保護 | 既有 protocol 與 execution manifest SHA 重驗未變 | 正式 V2 freeze 時再次重驗 |
| Objective engine | 因果 HH／HL／LL／LH、Dow、左右、候選關係與 stop 已實作 | 統一 FINAL metadata；全量 coverage |
| Atomic AI contract | subject-specific manifest、34 critical questions、PASS／FAIL／UNKNOWN | 正式 gold 與三輪結果 |
| V3 reducer | AI 不決定 scenario／phase／route／permission／trade | 全量 ledger 與 replay 稽核 |
| Packet builder | 151,804 股票日掃描、lossless lazy、3 shard、immutable merge、防未來洩漏 | 升 FINAL 後正式建置 1,029 檔 |
| Reviewer | 真實 106 verdict pilot schema／invariant 通過，約 710 秒 | formal hash/audit hardening、更多 operational pilot |
| Research calibration | 8 個 production-equivalent forced snapshots 與 immutable manifest | 補至 20；人類 rubric 先凍結 |
| Blinded gold | 固定 seed/objective strata/exclusion/fail-closed 工具 | 正式 review universe；至少 20 案／4 strata；人工 gold |
| Consistency evaluator | 三輪保守合併、critical atom 與 anti-all-WAIT 指標 | correctness 四道 gate 真正接入 overall pass；正式 120 案 |
| Formal freeze | fail-closed validator 與 protected strategy hashes | 所有元件 FINAL、review count、gold/sample/run manifests |
| V3 performance | 尚未讀取 | FIXED／ADD2、逐筆 MD、完整統計與五檔稽核 |
| V1／V2 comparison | 尚未執行 | 僅在 V3 通過後，用同一 ledger 回放 |

## 已知限制

- 本輪仍採 `LEGACY_RADIUS_3_10_NOT_COURSE_L1_L2` 相容樞紐基線，不得宣稱是課程 L1／L2 唯一定義。
- 1 案 operational pilot 只證明執行可行，不代表語意正確率、穩定率或投資績效。
- 既有台半、百容、康舒等案例已知後續走勢，只能作研究校準，不得作樣本外績效或獨立 gold。
- 任何未完成人工 rubric、hash、coverage 或正式 freeze 的階段，一律不得解封未來績效。
