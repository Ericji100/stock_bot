# enlightenment-integrated-v1

- 保留 Chrome 唯讀、停用資訊框、快速模式、latest-closed-K 時間錨定、固定九欄、Telegram canonical message、resume guard 與原有四種型態。
- 將號盤、四象限與 S／S／T／V 嵌入同一市場狀態與型態准入流程，不增加第五種型態或平行答案。
- 新增 v2 內部 `constitution_event`；renderer 不顯示第十欄。
- 新增持久化期交所交易日三次上限、每 setup 一次重進、連續三停損、30 分鐘冷卻、返場重新確認、模擬持倉識別與事件去重。
- resume guard 新增 180 秒有效租約，重疊回合回傳 `busy`，逾期才允許接手。
- 根目錄 Python 入口與 v1 schema 保留作舊版提示詞相容；正式版使用 `trade_monitor.*` 與 v2 schema。
- `trade_monitor_send_quiet_status` 等既有 Bot 設定未更改；因此 Codex heartbeat 的安靜顯示與 Telegram 全量推送差異仍取決於現有設定。
