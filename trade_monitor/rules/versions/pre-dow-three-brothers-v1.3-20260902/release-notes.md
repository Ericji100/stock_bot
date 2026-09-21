# pre-dow-three-brothers-v1.3-20260902

這是道氏三兄弟正式整合前的不可變還原點。

- Automation `1-k`：`ACTIVE`，`FREQ=MINUTELY;INTERVAL=1`。
- 執行版本：`enlightenment-integrated-v1.3-dual-mode`。
- 本機分析規則：`enlightenment-integrated-v1.1`。
- schema：`trade-monitor-analysis-v2`。
- Windows 工作排程：`StockAiBot-TradeMonitorLocalClock`，建立備份時為 Running。
- 內容包含完整執行 prompt、Automation TOML 快照、分析 prompt、schema、scheduler config 與工作排程摘要。

回復時仍須透過 Codex Automation update 切換 prompt；`automation.toml` 只供核對，不可直接覆寫正式設定。
