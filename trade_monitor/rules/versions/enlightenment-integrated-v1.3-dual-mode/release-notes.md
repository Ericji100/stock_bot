# enlightenment-integrated-v1.3-dual-mode

- `NOTIFY`：Telegram 與 Codex 任務都接收 finalize 產生的完整九欄 canonical message。
- `DONT_NOTIFY`：Telegram 與 Codex 任務都接收 finalize 產生的固定短版 canonical message。
- event ID 在完整／短版訊息選定後才計算，bridge、outbox 與 heartbeat 不得再摘要或換字。
- `force_notify=true` 仍使用完整九欄與「監控已恢復」標示。
- 四型態、號盤、四象限、S／S／T／V、交易憲法、停損、R 與一口單規則未修改；分析規則仍是 `enlightenment-integrated-v1.1`。
