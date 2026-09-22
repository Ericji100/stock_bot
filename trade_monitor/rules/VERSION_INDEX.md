# 監控規則版本索引

本文件只提供正式監控版本導覽，不取代任何 manifest、雜湊或執行設定。正式版本的唯一權威是 `active-version.json`。

更新日期：2026-09-22

## 目前正式版

| 項目 | 目前值 | 權威來源 |
|---|---|---|
| 監控版本 | `enlightenment-integrated-v2.1.8-anchor-origin` | `active-version.json` |
| 分析規則 | `enlightenment-integrated-v2.1.8-anchor-origin-analysis` | `active-version.json` |
| Schema／contract | `trade-monitor-analysis-v8`／`8` | `active-version.json` |
| Market structure state | `6` | `active-version.json` |
| Automation | `1-k`，狀態 `ACTIVE` | `active-version.json` |
| 本機 scheduler | 指向 v2.1.8 analysis prompt 與 schema | `trade_monitor/local_scheduler_config.json` |
| 還原點 | `enlightenment-integrated-v2.1.7-context-consistency` | `active-version.json` |

正式執行只使用上述版本。切換版本時仍須依 `README.md` 的成組部署、雜湊驗證及回復流程處理，不可只改本索引。

## 開發中回測

台指期一分 K、因果回放、AI hybrid 與 replay schema 契約已完整移至 `codex/tmf-1m-monitor`（`D:\code\_codex_worktrees\tmf-1m-monitor\stock_ai_bot`）。`main` 不包含回放引擎、回放測試或實驗規則；這些內容完成正式驗證前不得寫回正式工作區。

## 歷史封存版

- `versions/` 中除目前正式版外的版本，全部視為原地唯讀的歷史版本、RC／shadow 候選或部署還原點。
- 名稱以 `pre-` 開頭的目錄是不可變還原點。
- 名稱包含 `rc` 或 `shadow` 的目錄是隔離候選，不代表曾成為正式版。
- `legacy/` 保存版本化之前的舊規則；`source/` 保存整合來源與審查材料。
- 歷史版本目前不搬動、不重新命名、不刪除，避免破壞路徑引用、manifest 與 SHA-256 契約。

## 已完成相容整理

- 2026-09-22 已確認正式 Automation `1-k`、本機 scheduler、現行文件與測試使用 `trade_monitor.*` 及版本化 schema。
- 根目錄舊模組入口與重複的 v1 schema 已移至 `archive/legacy/trade_monitor_root_compat/`，只供歷史還原與稽核。

## 延後整理

以下正式相容整理仍需另案處理：

1. 完成依賴掃描後，再評估把正式監控的歷史規則實體移入封存區。
