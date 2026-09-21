# enlightenment-integrated-v1.1

唯一的規則變更：移除「每個期交所交易日最多三次模擬進場」的硬性上限。

- `simulated_entry_count` 繼續跨夜盤與日盤累計，僅供整日機會及復盤統計。
- 第四筆及後續不同 setup 的合格機會不再因每日筆數被否決。
- 每個原始 setup 最多一次重新進場、三次連續模擬停損冷卻30分鐘、狀態損壞 fail-safe、一口單管理、四種型態、九欄、Telegram 與 resume guard 均未調整。
- Schema 仍為 `trade-monitor-analysis-v2`，未增加或刪除分析欄位。
