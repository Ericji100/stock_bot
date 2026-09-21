# v1.6 Chrome dual-scale RC

- 每分鐘 DETAIL 固定約 180 根 K，繼續由本機第 05 秒先擷取。
- 每 15 分鐘可由 Automation 使用 Chrome 控制建立約 296 根 K 的 OVERVIEW；重大候選或持倉事件可延後最多 2 分鐘。
- 296 擷取與 180 恢復必須在同一個 Chrome 控制呼叫中完成，先恢復再分析已保留的 OVERVIEW 圖；影子實測離開 DETAIL 約 1～2 秒。
- 新增原子 `dual_scale_state`、OVERVIEW 租約、恢復確認、20 分鐘摘要時效與錯位／逾時 fail-safe。
- OVERVIEW 只更新大趨勢、波段階段、象限、大級防線與位置；DETAIL 仍獨占進場、停損與最新 K 觸發。
- 不使用 Computer Use、Windows UI Automation、SendInput 或 PowerShell 控制滑鼠鍵盤。
- 沿用 v4 schema、四種型態、交易憲法、固定九欄及 Telegram／Codex canonical 雙模式。
- 本版本只供影子驗證，未切換正式 Automation、正式 scheduler config、Telegram 或 active-version。
