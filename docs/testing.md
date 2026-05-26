# 測試與驗收

本專案使用 pytest。一般修改完成後先跑相關 focused tests，再視影響範圍跑完整測試。

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

## 文件修改驗收

README 或 docs 修改後至少檢查：

```bash
rg "docs/" README.md
rg "^#" README.md docs
```

確認 README 連結存在、章節清楚，且沒有把長篇歷史更新重新塞回主 README。
