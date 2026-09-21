# 監控規則版本索引

本文件只提供版本導覽，不取代任何 manifest、雜湊或執行設定。正式版本的唯一權威是 `active-version.json`；回放預設值的唯一權威是 `trade_monitor_replay/config.py`。

更新日期：2026-09-21

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

## 回測版

| 類別 | 版本／位置 | 用途與狀態 |
|---|---|---|
| CLI 預設回放 | `formal-v2.1.8-replay-adapter-v3` | `trade_monitor_replay/config.py` 的預設 manifest；不是正式即時監控版本。 |
| 最新隔離候選 | `course-state-v2.1.8-replay-adapter-v34-long-only-q2-q4` | AI hybrid、多方 Q2／Q4 隔離研究；使用自己的 v34 schema，不會切換正式監控。 |
| 純 AI 基準 | `course-pure-ai-v1-long-only` | 獨立純 AI 長多基準，用於比較，不是正式監控。 |
| 其他 replay adapter | `trade_monitor_replay/rules/` 其餘版本 | 歷史比較與相容測試；必須用各自 manifest，不可只依資料夾名稱判定可用性。 |

### 已知回測契約待修

- `course-state-v2.1.8-replay-adapter-v8` 至 `v33-long-only` 共 26 個 package 綁定舊 `replay-semantic-v6.json` SHA-256，目前與檔案實際雜湊不符。修復前不得將這些版本用於正式驗證。
- `reports/course_backtest/2026-09-10/v2_core_reproducible_goal_v1` 的兩份歷史 execution manifest 與目前腳本／builder 不一致。報表資料保留原狀，等下次執行相關回測時依契約方式修復。
- 不得為了讓測試變綠而批次覆寫舊 manifest 雜湊。應還原對應舊 schema，或建立新 schema 版本並只讓新 manifest 綁定。

## 歷史封存版

- `versions/` 中除目前正式版外的版本，全部視為原地唯讀的歷史版本、RC／shadow 候選或部署還原點。
- 名稱以 `pre-` 開頭的目錄是不可變還原點。
- 名稱包含 `rc` 或 `shadow` 的目錄是隔離候選，不代表曾成為正式版。
- `legacy/` 保存版本化之前的舊規則；`source/` 保存整合來源與審查材料。
- 歷史版本目前不搬動、不重新命名、不刪除，避免破壞路徑引用、manifest 與 SHA-256 契約。

## 延後整理

以下工作等監控回測契約修正時一起處理：

> 使用者提醒：下次開始監控回測契約修正或根目錄整理前，先提醒根目錄的監控相容檔仍在保留中，再確認是否進行移除。

1. 修復 replay semantic v6 與舊 manifest 的版本關係。
2. 修復兩份 `v2_core` 歷史 execution manifest 的重現契約。
3. 完成依賴掃描後，再評估把歷史規則實體移入封存區。
4. 確認所有舊 Automation、文件與測試都已改用 `trade_monitor.*` 後，再移除根目錄的 `trade_monitor_analysis_adapter.py`、`trade_monitor_analysis_contract.py`、`trade_monitor_bridge.py`、`trade_monitor_resume_guard.py` 與舊 schema。
