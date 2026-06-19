# 測試與驗收

本專案使用 pytest。一般修改完成後先跑相關 focused tests，再視影響範圍跑完整測試。

## 測試分層 Manifest

`tools/test_suite_manifest.py` 是機器可讀的測試分層清單，固定四層：

- `fast_unit`：本機快速單元測試，適合日常修改後先跑。
- `integration`：完整本機回歸測試。
- `live_source`：需要網路與外部資料來源的手動驗收。
- `ai_smoke`：可能消耗 AI / MCP 額度的手動 smoke test。

查詢分層內容：

```bash
python -c "from tools.test_suite_manifest import format_test_suite_manifest; print(format_test_suite_manifest())"
```

`live_source` 與 `ai_smoke` 必須維持 manual，不要放進一般 CI 或日常完整回歸。

## 基本測試

```bash
pytest
```

## 常用 focused tests

```bash
pytest tests/test_radar_service.py
pytest tests/test_research_center.py
pytest tests/test_theme_radar_feature.py
pytest tests/test_topic_maintain_service.py
pytest tests/test_backfill_service.py
pytest tests/test_data_fetcher_stock_resolution.py
pytest tests/test_technical_strategies.py
pytest tests/test_chip_strategies.py
```

## 題材與新聞

```bash
pytest tests/test_news_service.py
pytest tests/test_news_event_service.py
pytest tests/test_topic_maintain_service.py
pytest tests/test_topic_source_sync_service.py
pytest tests/test_topic_prompt_contracts.py
pytest tests/test_theme_report_context.py
```

## AI 投研與 Prompt 合約

```bash
pytest tests/test_prompt_contracts.py
pytest tests/test_report_schema.py
pytest tests/test_report_quality_service.py
pytest tests/test_value_scan_evidence_pack.py
pytest tests/test_rerating_snapshot_service.py
```

## 回補與快取

```bash
pytest tests/test_backfill_service.py
pytest tests/test_backfill_gap_service.py
pytest tests/test_backfill_scheduler_service.py
pytest tests/test_backfill_command.py
pytest tests/test_cache_utils.py
pytest tests/test_structured_cache.py
```

## MiniMax smoke tests

MiniMax 相關測試可能需要金鑰、MCP 或網路：

```bash
pytest tests/test_minimax_mcp_verify.py
pytest tests/test_minimax_integration.py
```

腳本：

```bash
python scripts/smoke_topic_maintain_minimax.py
python scripts/smoke_news_refresh.py
```

## `/value_scan` discovery dry-run

這個 smoke test 不呼叫 AI、不搜尋網路，只驗證 `/value_scan` discovery tasks、query log、`evidence_role` 與 discovery prompt 的 `evidence_usage` 規則是否正確產生：

```bash
python scripts/smoke_value_scan_discovery.py
python scripts/smoke_value_scan_discovery.py --command "/value_scan 精選選股 --deep --top 30"
```

## `/value_scan` 本地報告 smoke

這個 smoke test 不呼叫 AI、不搜尋網路，會用本地 fixture 跑完整 ResearchCenter 報告流程，驗證 Markdown / HTML / JSON artifacts、`search_query_log` 與 `ai_candidate_evidence_pack` 是否成功產出。輸出會寫到 `reports/_smoke_value_scan/`：

```bash
python scripts/smoke_value_scan_report_local.py
python scripts/smoke_value_scan_report_local.py --command "/value_scan 精選選股 --deep --top 3"
```

完成後可用報告覆蓋檢查工具確認推演骨架是否存在：

```bash
python tools/ai_report_coverage_check.py --root reports/_smoke_value_scan --limit 5 --coverage-only --out logs/ai_report_coverage_check/smoke_summary.md
```

`推演骨架` 欄位應為 `aligned`；若缺少市場故事、早期蛛絲馬跡、催化劑、缺少訊號、失敗條件或想像力結論，會列在 `缺少推演章節`。

## `/value_scan` Tavily 小流量 live smoke

這個 smoke test 會消耗少量 Tavily Search 額度：只跑 1 個 discovery task、1 條 query、1 個 search result，不呼叫 AI、不執行 Tavily Extract。輸出會寫到 `reports/_smoke_value_scan_live/`：

```bash
python scripts/smoke_value_scan_tavily_live.py
```

驗收重點：

- `search_query_log.task_count = 1`
- `search_query_log.providers` 有 Tavily provider entry
- `tavily_search_discovery.runs` 有狀態
- `sources` 至少落入 1 筆外部來源，或明確回報 quota / key / network 問題

## 文件修改驗收

README 或 docs 修改後至少檢查：

```bash
rg "docs/" README.md
rg "^#" README.md docs
```

確認 README 連結存在、章節清楚，且沒有把長篇歷史更新重新塞回主 README。
## Prompt 市場想像力合約

修改 AI prompt 或 `research_center/prompt_registry.py` 後，需確認報告型 AI 指令有載入 `embedded_market_imagination_rules.md`，且 source-only 模式沒有載入。相關測試：

```bash
python -m unittest tests.test_prompt_contracts.EmbeddedMarketImaginationPromptTests
python -m unittest tests.test_topic_prompt_contracts.TopicPromptContracts
```

同時需確認所有報告型 AI 指令有載入 `output_quality_rules.md`，Discovery prompt、News prompt、Topic prompt、Low model prompt 仍符合可解析與可維護規格：

```bash
python -m unittest tests.test_prompt_contracts tests.test_topic_prompt_contracts tests.test_ai_workflow_service
```
