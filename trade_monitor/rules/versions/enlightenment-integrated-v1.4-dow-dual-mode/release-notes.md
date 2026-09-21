# enlightenment-integrated-v1.4-dow-dual-mode

- Automation 執行與傳輸層沿用 `enlightenment-integrated-v1.3-dual-mode`。
- 本機讀秒仍在每分鐘第 05 秒開始；Windows 工作排程名稱與啟動方式不變。
- heartbeat 仍只 relay local outbox，不回退成 Chrome 直接分析。
- `NOTIFY` 使用完整九欄；`DONT_NOTIFY` 使用固定短版；Telegram 與 Codex 任務共用 finalize 選定的同一份 canonical message。
- 實際分析規則改為 `enlightenment-integrated-v1.4-dow-analysis`，SHA-256 為 `dec982c6fce9c5e4f64dbd23a5f9053b53b90911f77cabcc5d5e4867f0075aa0`。
- schema 升至 `trade-monitor-analysis-v3`，新增內部 `market_structure_state`；adapter／contract 同步升級且保留 v2 payload 相容。
- Telegram bridge、resume guard、一口單與固定九欄可見格式不變。
