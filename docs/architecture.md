# 系統架構總覽

本文件描述目前系統的主要共用層與資料流。README 保留入口與索引，細節以本文件為準。

## 主要入口

| 入口 | 角色 |
|---|---|
| `main.py` | Telegram Bot、排程、手動指令入口 |
| `research_center/orchestrator.py` | 投研指令主流程、AI 呼叫、報告產出 |
| `research_center/data_services.py` | `/research`、`/value_scan`、`/macro`、`/theme` 等結構化資料收集 |
| `backfill_service.py` | 回補資料、快取暖機、回補 marker |
| `stock_scanner.py` / `technical_scanner.py` / `chip_strategies.py` | 選股、技術面、籌碼資料來源與策略 |

## 共用服務層

| 共用層 | 主要檔案 | 用途 |
|---|---|---|
| Command Runtime / Scheduled Task | `research_center/command_runtime_service.py`、`research_center/scheduled_task_service.py`、`progress_logger.py`、`main.py` | 統一任務狀態、stop event、進度訊息、任務鎖、timeout 掃描、錯誤分類、報告路徑、定時任務排隊、背景任務鎖與啟動排程摘要 |
| Resource Guard | `research_center/resource_guard_service.py`、`research_center/scheduled_task_service.py` | 限制背景任務資源池並提供資源使用 snapshot；只治理背景並行，不改手動指令、prompt、評分、排序或報告內容 |
| Data Source Gateway | `research_center/data_source_gateway.py`、既有 `data_source_manager.py`、`finmind_client.py`、`fugle_data.py`、`price_fallbacks.py` | 封裝多資料源 fallback、健康事件、來源嘗試紀錄、來源冷卻與 quota snapshot；價量 fallback policy 會保留 gateway attempts |
| Cache / Artifact Registry | `research_center/artifact_registry.py` | 登記報告、Feature Pack、backfill marker 等資料產物的 schema、日期、來源、完整度、可用性；也可只讀盤點 `.cache/`、`reports/`、`database/`、manual 產物 |
| Backfill DAG | `research_center/backfill_dag_service.py`、`backfill_service.py` | 描述回補節點、依賴與節點執行事件，讓 marker 顯示回補到底補了哪些資料、哪些節點略過或失敗 |
| Entity Resolver | `research_center/entity_resolver.py` | 統一股票代號、上市櫃 suffix、公司名稱、題材別名、產業別名與供應鏈節點解析 |
| Data Gap / Quality | `research_center/data_gap_service.py`、`research_center/data_gap_refill_service.py`、`research_center/report_quality_service.py`、`research_center/evidence_pack_service.py` | 全指令共用資料缺口、缺口補抓協調、證據包、必要欄位清單與報告品質診斷 |
| Structured Error / Health | `research_center/error_classification_service.py` | 統一 network、quota、parse、cache、AI timeout、報告產出錯誤分類 |
| System Health Snapshot | `research_center/system_health_service.py` | 聚合 Command Runtime、Data Source Gateway snapshot、Artifact Registry 統計，供狀態查詢與除錯使用 |
| Prompt Bundle | `research_center/prompt_manifest_service.py`、`prompt/manifest.json` | 紀錄每個指令使用的 prompt、rules、scoring 文件與版本 |
| Event Context | `research_center/event_context_service.py`、`research_center/event_store.py`、新聞/題材服務 | 將新聞、來源事件、題材事件整理成可共用事件上下文 |
| Shared Feature Pack | `research_center/stock_feature_pack_service.py` | 將指令需要的核心資料整理成共用資料包，供 AI、報告與後續指令調度 |

## 投研資料流

```text
Telegram/CMD 指令
  -> command_parser
  -> collect_structured_data
  -> date context / news context / news events
  -> entity resolver / event context
  -> company knowledge / feature pack
  -> data gap summary
  -> data gap refill（best-effort，僅調用既有服務）
  -> data gap / evidence pack / data inventory
  -> prompt registry + prompt manifest
  -> AI workflow / segmented analysis
  -> report quality
  -> report artifacts + artifact registry
  -> database events / snapshots
```

## 回補資料流

```text
/backfill 或排程
  -> 建立候選池
  -> 市場基礎資料
  -> 技術面快取
  -> 籌碼與 TDCC 快取
  -> 財報與毛利率快取
  -> 精選選股快取
  -> 投研結構化快取
  -> backfill marker
  -> backfill DAG summary
  -> backfill DAG events
```

## 維護原則

1. 新增資料來源時，優先接到 Data Source Gateway 或既有來源管理器，不要在指令中直接散寫 fallback。
2. 新增報告資料時，優先接到 Feature Pack、Data Gap Refill、Evidence Pack 與 Report Quality。
3. 新增 prompt 時，同步更新 `prompt/manifest.json` 或 `research_center/prompt_manifest_service.py`。
4. 新增快取或報告產物時，優先登記到 Artifact Registry；若是既有資料目錄，先用 artifact inventory 盤點，不要直接刪改。
5. 新增狀態查詢或除錯資訊時，優先聚合到 System Health Snapshot，不要在各指令中散寫健康摘要。
6. 新增題材、公司、供應鏈資料時，優先讓 Entity Resolver 或事件層可讀取。
7. 新增錯誤處理時，優先使用 `error_classification_service.py` 的分類，不要只留下裸 `except Exception`。
8. README 只放入口、常用指令與索引；架構細節集中維護在本文件。

## 資料缺口補抓策略

`research_center/data_gap_refill_service.py` 是投研資料缺口補抓協調層。它只在 `collect_structured_data()` 收到既有 structured data 後執行，並只調用既有資料服務補缺口，不改 AI prompt、評分權重、資料排序、報告格式或資料來源優先順序。

補抓範圍：

- `/research`、`/research --deep`、`/research --source-only`：只補目標股票。
- `/value_scan`、`/value_scan --deep`、單股 value scan：只補實際送入 AI 的 `ai_candidates`；單股模式只補該股票。
- `/theme`、`/theme_radar`：補題材 / topic context 與新聞上下文，不對全市場逐檔補資料。
- `/macro`：只補總經 / 市場公開資料與新聞上下文，不補個股毛利率、籌碼或財報。

補抓結果會寫入 `structured_data["data_gap_refill"]`，包含補抓前後缺口數、嘗試項目、成功 / 跳過 / 失敗狀態與原因。資料源冷卻、quota 不足、逾時或解析失敗時只記錄原因，不中斷報告產出；補不到的欄位仍保留 `missing_data_status`，不得填入假資料。
