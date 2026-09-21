# 本機讀秒觸發正式執行層

## 目標

本機常駐程序每分鐘第 `05` 秒開始處理上一根已收盤 1 分 K。`05` 秒是擷取與分析的開始時間；Codex 完成結構化分析通常仍需約 20～45 秒，不代表五秒內完成結果。

## 正式資料流

```text
Windows 本機時鐘 :05
  → 固定裁切前景 Google 遠端桌面中的純圖表
  → 圖表像素與尺寸驗證
  → resume guard／latest-closed-K prepare
  → Codex CLI 唯讀圖片分析（依 local_scheduler_config 綁定的版本化規則、schema 與 prompt SHA-256）
  → analysis adapter finalize
  ├─→ 既有 Telegram bridge（canonical message）
  └─→ local outbox
        → Automation 1-k heartbeat 只負責將相同 canonical message relay 到原任務
```

本機程序只在 Automation `1-k` 的實際狀態為 `ACTIVE` 時擷取與分析；`PAUSED` 時保持待命，因此沿用原本的啟停入口。

## 隱私與唯讀邊界

- `chart_capture.ps1` 只把固定圖表矩形寫入 PNG；右側委託、下單與帳務區不在裁切範圍。
- 每張圖需通過尺寸、黑底比例與 K 線色彩抽樣驗證，否則 fail-safe，不交給 Codex 判斷方向。
- 不移動滑鼠、不點擊、不輸入、不切換商品或週期。
- 執行期圖片與狀態都在 `.runtime/trade_monitor/`；只保留最近五張正式擷取圖。

## 防重複與中斷

- `local_scheduler.lock` 保證本機只存在一個 daemon。
- `resume_guard` lease 防止同一輪重疊。
- adapter 依最新已收盤 K、圖片雜湊與 context id 防止重算。
- Telegram bridge 依 canonical event id 去重。
- outbox 使用 `peek`／`ack`，同一事件不會在 Codex 任務重複顯示。
- 同一作業錯誤五分鐘內只詳細通知一次，避免錯誤洗版。

## 手動驗證

只跑一次且不發 Telegram：

```powershell
python -m trade_monitor.local_scheduler `
  --runtime-root .runtime\trade_monitor\local_scheduler_manual_test `
  once --dry-run
```

連續兩分鐘在第 05 秒觸發且不發 Telegram：

```powershell
python -m trade_monitor.local_scheduler `
  --runtime-root .runtime\trade_monitor\local_scheduler_clock_test `
  daemon --cycles 2 --dry-run
```

查看待 relay 事件：

```powershell
python -m trade_monitor.local_outbox peek
```

## Windows 啟動方式

正式 Windows 工作排程只負責在使用者登入時啟動一個隱藏的常駐程序：

```powershell
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass `
  -File D:\code\stock_ai_bot\trade_monitor\start_local_scheduler.ps1
```

常駐程序本身再依本機時鐘對齊每分鐘第 05 秒。不要另外建立每分鐘 Windows 工作排程，否則會增加程序重疊風險。

## 操作限制

- `local_scheduler_config.json` 成組鎖定正式分析規則版本、UTF-8 SHA-256、schema 與內部狀態開關；規則或 schema 配對遭非預期修改時 fail-safe 停止分析。
- Automation heartbeat 不再自行擷取、分析或發 TG，只 relay outbox；Telegram 的即時性由本機程序提供，Codex 任務顯示仍受 heartbeat 喚醒時間影響。
- Automation execution prompt 與 local scheduler config 必須成組切換；不能只更新其中一邊。v1.3 綁定 v1.1＋v2，v1.4-dow 綁定 v1.4-dow-analysis＋v3。
- 本層保留第 05 秒喚醒、雙模式 canonical message 與固定九欄；實際交易判斷由目前 config 綁定的分析規則決定。
