# 系統架構收斂與共用資料格式優化規劃

本文規劃如何在不大幅重構、不改變既有 Telegram 顯示、不新增分散系統的前提下，逐步收斂現有股票系統的資料層、候選股格式、報告格式、新聞事件、指令執行結果與 artifact 管理。

核心目標不是增加新功能，而是讓現有功能共用同一套資料底座，降低重複整理、重複轉換、重複判斷，讓後續的早期異動、題材推論、驗證、追蹤都能站在同一份結構化資料上。

## 一、背景與問題

目前系統已具備完整功能線：

- `/scan`：技術、籌碼、營收、精選交叉比對。
- `/radar`：候選股雷達與 AI 短評。
- `/value_scan`：候選池重估與深度篩選。
- `/research`：單股研究。
- `/theme_radar`、`/theme`、`/theme_flow`：題材、供應鏈、主題分析。
- `/morning`、`/noon`：市場摘要。
- `/backfill`、定期任務、資料健檢、圖表、匯出。

目前真正的風險不是功能不足，而是幾條資料線逐漸分散：

- 各指令可能各自整理候選股欄位。
- 技術、籌碼、營收、新聞、題材、AI search 的資料摘要格式不完全一致。
- 報告 JSON 雖然已存在，但不同報告的 metadata、候選股、證據、風險欄位仍可更一致。
- 新聞目前容易停留在摘要或 prompt 材料，還沒有完全變成可被本地規則與 AI 共用的事件資料。
- Telegram handler、排程、健檢、API 對成功、失敗、略過的狀態表達不完全一致。
- `.cache`、`logs`、`reports` 有不同用途，但可刪除性、保存天數、正式產物與測試產物的界線還可以更清楚。

## 二、收斂原則

本次優化應遵守以下原則：

1. 不做一次性大重構。
2. 不改變既有 Telegram 使用者看到的主要文字格式，除非必要。
3. 不重做資料抓取邏輯，優先包裝現有輸出。
4. 不新增分散資料檔，優先沿用現有 `.cache`、`reports`、`database`、`artifact_registry`。
5. 先新增共用 schema，再逐步讓既有功能附上共用格式。
6. 舊介面保留相容，避免影響既有指令。
7. 所有共用格式都要能服務三件事：
   - 本地規則判斷。
   - AI prompt 輸入。
   - 後續回測、健檢、追蹤。

## 三、目標架構

理想資料流如下：

```text
資料抓取 / 快取
  -> DataSourceSummary
  -> CandidateSnapshot / NewsEvent / ThemeSignal
  -> ReportMetadata
  -> Markdown / HTML / JSON / Telegram Summary
  -> ArtifactRegistry / Database / 後續追蹤
```

指令不應各自重新定義資料格式，而是盡量使用共用結構：

```text
/scan
/radar
/value_scan
/research
/theme_radar
/theme
/theme_flow
        -> 共用 CandidateSnapshot / DataSourceSummary / ReportMetadata
```

## 四、優先優化項目

### 1. 統一 CandidateSnapshot 候選股格式

這是最高優先順序，因為整個系統的選股、雷達、研究、題材推論，都圍繞候選股運作。

建議建立共用候選股格式，位置可優先考慮：

- `research_center/models.py`
- 或新增小型 schema 模組，例如 `research_center/shared_models.py`

建議欄位：

```text
schema_version
code
name
market
symbol
source_command
source_strategy
source_pool
signal_date
data_date
signal_type
signal_strength
stage
technical_signals
chip_signals
revenue_signals
theme_signals
news_signals
early_stage_flags
overheat_flags
risk_flags
local_scores
evidence_refs
raw_snapshot_ref
created_at
```

其中 `stage` 建議統一為：

```text
early_single_signal
cross_confirmed
momentum_confirmed
overheated
watch_only
```

這樣可以同時支援：

- 早期單點異動型。
- 交叉確認型。
- 已過熱但仍需觀察型。
- 僅作為題材或供應鏈線索的觀察型。

預期效益：

- `/scan` 輸出可以保留原報告，同時附上結構化候選股。
- `/value_scan` 不必重新理解不同來源候選池。
- `/research` 可直接知道個股是從哪個策略、哪天、什麼訊號進入觀察。
- `/theme_radar` 可以引用 candidate pool 作為觀察線索，不必新增獨立檔案。
- 未來做 5/20/60 日追蹤時，有統一資料源。

### 2. 統一 DataSourceSummary 資料來源摘要

目前系統有多個資料來源：

- TWSE / TPEx 官方資料。
- Yahoo Finance。
- FinMind。
- Fugle。
- MOPS。
- 本地 `.cache`。
- 新聞資料庫。
- AI search / web fetch。
- 題材庫與公司知識庫。

建議每次資料包或報告都附上標準資料來源摘要。

建議欄位：

```text
schema_version
data_type
provider
source_name
source_path_or_url
as_of_date
fetch_time
status
row_count
fallback_used
fallback_chain
missing_fields
warning_flags
freshness
diagnostics
```

`status` 建議統一為：

```text
ok
partial
missing
stale
fallback
error
skipped
```

預期效益：

- 台指期、股價、籌碼、營收錯誤時，可以直接追來源，不需要靠報告文字猜。
- AI 報告可以知道資料邊界，但不會把報告塞滿日期提醒。
- 非 AI 健檢可以用結構化狀態判斷，不必解析文字。
- `/morning`、`/noon`、`/backfill` 可以共用資料新鮮度與略過原因。

### 3. 統一 ReportMetadata / Report JSON 格式

目前 `research_center` 已有：

- `ReportArtifacts`
- `report_builder.py`
- `report_html_renderer.py`
- `report_validator.py`
- `report_quality_service.py`
- `database.py`

方向正確，建議下一步是讓所有重要報告都有同一層 metadata。

建議每份 report JSON 固定包含：

```text
schema_version
metadata
command_request
data_source_summary
candidate_snapshot
local_scores
scenario
evidence
news_events
theme_context
risk_flags
model_diagnostics
markdown
telegram_summary
artifacts
created_at
```

`metadata` 建議包含：

```text
report_id
report_type
command
target
report_date
data_date
model
mode
source_pool
ai_used
fallback_reason
```

預期效益：

- `/research`、`/theme_radar`、`/value_scan` 的產物可比較。
- Telegram、HTML、JSON 從同一份資料來，不會各自生成不同結論。
- 報告品質檢查與健檢可直接掃 JSON。
- 後續要做劇本追蹤、候選股表現追蹤，可直接讀 metadata。

### 4. 將新聞收斂成 NewsEvent 共用事件格式

新聞不應只作為摘要或 AI prompt 文字。建議先轉為結構化事件，讓本地分析和 AI 都可使用。

建議欄位：

```text
schema_version
news_id
title
published_at
source
url
related_symbols
related_topics
event_type
signal_role
heat_level
is_catalyst
is_counter_evidence
is_overheat_risk
summary
evidence_text
confidence
created_at
```

`signal_role` 建議統一為：

```text
theme_clue
catalyst
counter_evidence
overheat_risk
background_noise
```

`event_type` 可逐步整理為：

```text
order
revenue
earnings
mops_announcement
supply_chain
capacity
product
policy
industry_trend
market_heat
risk_event
rumor_or_unverified
```

預期效益：

- 少量高品質新聞可被視為題材線索。
- 新聞爆量可被視為過熱風險，而不是單純加分。
- `/research` 能自然輸出催化、反證、過熱。
- `/theme_radar` 能更穩定追蹤題材擴散。
- 本地規則可以先標示事件角色，再交給 AI 推論。

### 5. 統一 CommandResult 指令執行結果

目前 Telegram handler、排程、API、健檢工具對成功、失敗、略過的判斷可能分散。

建議建立共用指令結果格式，接在現有 `research_center/command_runtime_service.py` 或相關 runtime service 上。

建議欄位：

```text
schema_version
command
args
status
reason
message
data_date
artifacts
warnings
errors
runtime_seconds
created_at
```

`status` 建議統一為：

```text
success
skipped
partial
failed
timeout
cancelled
```

預期效益：

- `/morning`、`/noon`、`/scan`、`/backfill`、定期任務可一致判斷。
- 健檢工具不必靠文字猜結果。
- Telegram 顯示可保留現有格式，內部狀態更乾淨。
- 長時間任務、排隊、取消、重試可統一追蹤。

### 6. 收斂 cache / logs / reports / artifacts 管理規則

目前空間主要集中在：

- `.cache`
- `logs`
- `reports`

建議明確定義用途：

```text
.cache      可重建資料快取
reports     正式報告產物
logs        執行紀錄、debug、AI prompt、稽核
database    長期索引與事件
backup      手動或系統備份
```

每種 artifact 應有：

```text
artifact_type
purpose
is_rebuildable
is_user_visible
retention_days
cleanup_policy
registry_required
```

可沿用並強化：

- `research_center/artifact_registry.py`

預期效益：

- 清理不再靠人工猜。
- 正式報告與測試紀錄分清楚。
- 長期運行不會讓 logs 和 reports 無限制膨脹。
- 需要追溯時仍找得到正式產物。

## 五、分階段執行計畫

### Phase 1：建立共用 schema，不改現有行為

目標：

- 定義 `CandidateSnapshot`。
- 定義 `DataSourceSummary`。
- 定義 `ReportMetadata`。
- 定義 `NewsEvent`。
- 定義 `CommandResult`。

建議做法：

1. 先檢查現有 `research_center/models.py` 是否適合加入。
2. 若 `models.py` 已太雜，可新增 `research_center/shared_models.py`。
3. 以 dataclass 或 TypedDict 形式建立輕量 schema。
4. 加上轉 dict helper，方便寫入 JSON。
5. 不改 Telegram 顯示，不改現有指令結果。

驗收：

- 新 schema 有單元測試。
- 舊測試通過。
- 不影響任何既有指令。

### Phase 2：讓 /scan、/radar、/value_scan 附上 CandidateSnapshot

目標：

- 不重做選股邏輯。
- 將既有候選股轉換成共用 `CandidateSnapshot`。

涉及模組可能包含：

- `stock_scanner.py`
- `technical_scanner.py`
- `curated_scan_service.py`
- `radar_service.py`
- `research_center/recent_scans.py`
- `research_center/data_services.py`

建議做法：

1. 寫 adapter，不直接大改原本 scanner。
2. 從既有候選股 dict / dataclass 轉成 `CandidateSnapshot`。
3. 在 report JSON 或 cache summary 中附上 `candidate_snapshot`。
4. Telegram 文字先維持原樣。

驗收：

- `/scan` 產物可以讀到 candidate snapshot。
- `/radar` 可以讀最近 snapshot。
- `/value_scan` 可以從候選池保留來源與訊號。

### Phase 3：統一 report metadata 與 data source summary

目標：

- 讓 `/research`、`/value_scan`、`/theme_radar` 報告 JSON 有一致外層欄位。

涉及模組可能包含：

- `research_center/report_builder.py`
- `research_center/orchestrator.py`
- `research_center/ai_data_center.py`
- `research_center/report_validator.py`
- `research_center/database.py`

建議做法：

1. 在 `write_report_artifacts()` 集中補 metadata。
2. 讓現有 structured data 中的資料狀態統一映射到 `DataSourceSummary`。
3. 不移除舊欄位，先新增標準欄位。
4. 更新報告 validator，要求標準欄位存在。

驗收：

- 新舊報告格式相容。
- AI 報告 JSON 包含 `metadata`、`data_source_summary`。
- 測試覆蓋缺欄位時的 fallback。

### Phase 4：將新聞分類轉成 NewsEvent

目標：

- 新聞不只摘要，而是成為可供本地規則與 AI 共用的事件資料。

涉及模組可能包含：

- `research_center/news_service.py`
- `research_center/news_repository.py`
- `research_center/news_categories.py`
- `research_center/news_context_service.py`
- `research_center/news_event_service.py`
- `research_center/theme_radar_service.py`

建議做法：

1. 盤點現有 `news_event_service.py` 是否已有可沿用格式。
2. 將新聞分類結果映射成 `NewsEvent`。
3. 加入 `signal_role` 與 `heat_level`。
4. `/research`、`/theme_radar` 先讀 NewsEvent，再組 prompt。

驗收：

- 新聞可標示題材線索、催化、反證、過熱、背景雜訊。
- 新聞爆量不再只被視為正面加分。
- AI prompt 中能看到事件角色與邊界。

### Phase 5：統一 CommandResult 與健檢

目標：

- 讓指令執行狀態可被排程、健檢、API 共同使用。

涉及模組可能包含：

- `main.py`
- `research_center/telegram_handlers.py`
- `research_center/command_runtime_service.py`
- `research_center/scheduled_task_service.py`
- `research_center/system_health_service.py`

建議做法：

1. 建立 `CommandResult`。
2. 先讓非 AI 指令與排程回傳結構化結果。
3. Telegram 仍使用 `message` 顯示。
4. 健檢工具直接讀 `status`、`reason`、`artifacts`。

驗收：

- `/morning`、`/noon`、`/backfill` 有結構化 status。
- 非交易日或資料未完成能標示 `skipped`，不是被當成失敗。
- 健檢可不用文字解析判斷結果。

### Phase 6：artifact retention 與清理政策

目標：

- 建立可重複執行的清理規則，不誤刪正式資料。

涉及模組可能包含：

- `research_center/artifact_registry.py`
- `research_center/system_health_service.py`
- 新增工具可考慮 `tools/artifact_cleanup_plan.py`

建議做法：

1. 先只做 dry-run 報告。
2. 列出可清、建議保留、需要確認三類。
3. 將測試 logs、AI audit logs、正式 reports 分類。
4. 後續再允許安全清理。

驗收：

- 可產出清理計畫。
- 不會列入 `config.json`、`portfolio.json`、`database`、`data` 等敏感資料。
- 清理前可預估釋放空間。

## 六、非目標

本計畫不包含：

- 重寫所有 scanner。
- 重做資料抓取 provider。
- 改變 Telegram 指令名稱。
- 改變使用者看到的主要報告版型。
- 直接刪除舊報告或舊 cache。
- 一次性搬移所有歷史資料。
- 強迫所有舊報告立即轉成新格式。

## 七、測試與驗收策略

每一階段都應至少包含：

1. 單元測試。
2. 舊測試完整通過。
3. 代表性指令 smoke test。
4. 產物檢查。
5. 不改變 Telegram 主要文字輸出的確認。

建議驗證指令：

```bash
python -B -m unittest discover tests
python -B -m compileall research_center main.py stock_scanner.py radar_service.py
```

若涉及實際指令，應使用既有 mock 或安全模式先測：

```text
/scan 小樣本
/radar 使用最近快取
/value_scan 使用最近掃描結果
/research 2330 使用 mock 或低成本模式
/theme_radar 使用既有題材快取
/morning
/noon
/backfill today
```

## 八、風險與控制

| 風險 | 控制方式 |
| --- | --- |
| 大重構導致指令壞掉 | 先新增 schema 和 adapter，不直接替換舊流程 |
| Telegram 輸出改變造成使用不習慣 | 初期只改 JSON / internal metadata，不改文字 |
| 資料格式太理想化，現有資料塞不進去 | 欄位允許 optional，缺資料標 `missing` |
| AI prompt 變更造成報告品質波動 | 先在 structured data 補欄位，再逐步改 prompt |
| 清理規則誤刪重要資料 | 先 dry-run，不自動刪 |

## 九、建議優先順序

建議實際執行順序：

1. `CandidateSnapshot`
2. `DataSourceSummary`
3. `ReportMetadata`
4. `NewsEvent`
5. `CommandResult`
6. artifact retention / cleanup policy

最小可行成果：

```text
先完成 CandidateSnapshot + DataSourceSummary + ReportMetadata，
並讓 /scan、/radar、/value_scan、/research、/theme_radar 的 JSON 產物能附上這三種共用資料。
```

這樣就能先達到架構收斂的核心效果，而不必一次改完整個系統。

## 十、目前最小落地狀態

第一階段採取 adapter 方式落地，避免改動既有資料抓取、排序與 Telegram 顯示。

已落地的核心格式：

- `CandidateSnapshot`
- `DataSourceSummary`
- `ReportMetadata`
- `NewsEvent`
- `CommandResult`

目前接入點：

- `research_center.models`：定義共用 schema。
- `research_center.convergence_service`：集中負責從既有 dict / dataclass 轉成共用格式。
- `research_center.data_services.collect_structured_data()`：資料收集完成後，將共用欄位附到 `structured_data`，供 AI prompt / high model input package 使用。
- `research_center.report_builder.build_report_json()`：所有 AI 報告 JSON 產物固定輸出：
  - `schema_version`
  - `report_metadata`
  - `data_source_summary`
  - `candidate_snapshot`
- `research_center.recent_scans.save_recent_scan_result()`：`/scan` 類結果快取會保存 `candidate_snapshot`。
- `radar_service`：`/radar` 快取與 `reports/radar/.../radar_candidates.json` 會保存 `candidate_snapshot`。

相容性策略：

- 不移除舊欄位。
- 不改變 Telegram 主要顯示。
- 不重做資料抓取 provider。
- 不改變現有排序與篩選邏輯。
- 先讓 JSON / cache 產物具備共用格式，後續再逐步讓更多 prompt 與健檢流程優先讀取共用欄位。

## 十一、可作為執行目標的指令

可直接將以下內容作為後續開發目標：

```text
目標：從現有系統架構出發，朝向收斂方向優化共用資料層與輸出格式，避免各指令各自整理資料、候選股與報告格式。

執行內容：
1. 先審視現有 /scan、/radar、/value_scan、/research、/theme_radar 的資料流、候選股格式、報告 JSON、cache 與 artifact 輸出。
2. 在不重做既有功能、不改變 Telegram 主要顯示格式的前提下，建立共用 schema：
   - CandidateSnapshot
   - DataSourceSummary
   - ReportMetadata
   - NewsEvent
   - CommandResult
3. 優先完成 CandidateSnapshot、DataSourceSummary、ReportMetadata 三個核心格式。
4. 以 adapter 方式將既有 /scan、/radar、/value_scan 的候選股轉成 CandidateSnapshot，保留舊資料結構相容。
5. 在 report JSON 中補上標準 metadata、data_source_summary、candidate_snapshot，但不要移除舊欄位。
6. 讓 /research、/theme_radar、/value_scan 能優先讀取共用資料格式作為 AI 輸入來源，避免各自重組資料。
7. 盤點新聞資料流，將既有新聞分類逐步映射成 NewsEvent，支援題材線索、催化事件、反證、過熱風險、背景雜訊。
8. 檢查 CommandResult 是否可接入現有 command_runtime_service、scheduled_task_service、system_health_service，先提出最小修改方案。
9. 更新或新增測試，確認：
   - 舊指令相容。
   - Telegram 主要輸出不變。
   - JSON 產物包含新共用欄位。
   - /scan、/radar、/value_scan、/research、/theme_radar 可共用 CandidateSnapshot / DataSourceSummary / ReportMetadata。
10. 更新 README 或 docs，說明新的共用資料格式與後續擴充方式。

限制：
- 不做一次性大重構。
- 不重寫資料抓取 provider。
- 不改變既有 Telegram 指令名稱。
- 不新增分散資料檔，優先使用現有 .cache、reports、database、artifact_registry。
- 不刪除舊欄位，先新增標準欄位並保留相容。
- 每一階段完成後都要跑相關測試與完整測試。

完成標準：
- 至少 CandidateSnapshot、DataSourceSummary、ReportMetadata 已落地。
- /scan、/radar、/value_scan、/research、/theme_radar 的主要 JSON 產物可讀到共用欄位。
- 舊 Telegram 顯示不受影響。
- 完整測試通過。
- docs 更新完成。
```
