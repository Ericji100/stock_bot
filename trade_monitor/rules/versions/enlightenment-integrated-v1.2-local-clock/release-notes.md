# enlightenment-integrated-v1.2-local-clock

- 本機常駐程序每分鐘第 05 秒開始處理最新已收盤 1 分 K。
- 固定裁切純圖表，右側委託、下單與帳務區不進入分析圖片。
- Codex CLI 使用 v1.1 完整交易規則與 v2 schema；v1.1 規則雜湊不符時 fail-safe。
- `analysis_adapter finalize` 仍是九欄訊息、交易憲法狀態與 Telegram 的唯一正式出口。
- Automation `1-k` 保留相同 ID、RRULE、target task 與 ACTIVE／PAUSED 控制，只改成 relay local outbox。
- 四型態、號盤、四象限、S／S／T／V、停損、R、一口單與通知內容規則沒有修改。
