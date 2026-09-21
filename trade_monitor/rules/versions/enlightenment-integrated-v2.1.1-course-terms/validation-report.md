# v2.1.1 驗證報告

- 變更範圍：使用者可見術語與提示詞一致性。
- 交易規則差異：無。
- Schema 差異：無，沿用 `trade-monitor-analysis-v8`。
- 狀態機差異：無，沿用 `market_structure_state.version=6`。
- 顯示層與版本測試：24 項通過。
- 完整 `tests/trade_monitor`：236 項通過。
- Automation 反向讀取：提示詞 SHA-256、ID、RRULE、target task 與 PAUSED 狀態均相符。
