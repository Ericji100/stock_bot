# Codex 台指期監控 Telegram Bridge

`trade_monitor.bridge` 將 Codex 台指期監控產生的同一份 canonical message 轉為 Telegram entities、分段並交給既有 Bot 發送。Bridge 不分析或改寫盤勢，也不 import、啟動或輪詢 `main.py`。

每個 Telegram 訊息分段會由 bridge 自動加上 `🔥 ` 前綴，方便在群組中辨識台指期監控通知。圖示只屬於 Telegram 傳輸外觀，不加入 canonical message、Codex 對話或 event ID；監控端不得自行重複加入火焰。

## 設定

`config.json` 支援以下向後相容欄位：

```json
{
  "trade_monitor_telegram_enabled": false,
  "trade_monitor_chat_id": null,
  "trade_monitor_send_quiet_status": false
}
```

- `trade_monitor_telegram_enabled`：未設定時視為 `false`。設為 `false` 可立即停用同步。
- `trade_monitor_chat_id`：台指期監控專用群組 ID。未設定或為 `null` 時才回退使用既有 `chat_id`。
- `trade_monitor_send_quiet_status`：預設 `false`；此時 `DONT_NOTIFY` 不會推送。設為 `true` 才允許推送靜默狀態。
- `api_token` 與既有 Bot 共用，只能保存在本機 `config.json`，不得放進提示詞、命令列或日誌。

新部署建議明確填寫 `trade_monitor_chat_id`，避免意外回退到既有聊天室。`config.json` 已由 `.gitignore` 排除。

## CLI 與 stdin

訊息必須透過 UTF-8 standard input 傳入；命令列只放事件編號與決策：

```powershell
Get-Content -LiteralPath .\canonical-message.txt -Raw -Encoding UTF8 |
  python -m trade_monitor.bridge --event-id <SHA256_EVENT_ID> --decision NOTIFY
```

自動化端建議使用 `subprocess.run` 並明確關閉 stdin：

```python
completed = subprocess.run(
    [python_executable, "-m", "trade_monitor.bridge", "--event-id", event_id, "--decision", decision],
    input=canonical_message,
    text=True,
    encoding="utf-8",
    cwd=r"D:\code\stock_ai_bot",
    capture_output=True,
    timeout=45,
    check=False,
)
```

Telegram 只收到 stdin 的 message body，不會收到 heartbeat XML、decision 或 automation ID。

## Dry run

`--dry-run` 只解析 `**粗體**`、建立 UTF-16 Telegram entities、分段及檢查長度，不建立 Bot、不呼叫 Telegram，也不寫入去重狀態：

```powershell
Get-Content -LiteralPath .\canonical-message.txt -Raw -Encoding UTF8 |
  python -m trade_monitor.bridge --event-id <SHA256_EVENT_ID> --decision NOTIFY --dry-run
```

stdout 只會輸出一行不含 Token 或 Chat ID 的 JSON。

## event_id 與去重

事件 ID 是以下四個值以換行連接後的 UTF-8 SHA-256：

1. `automation_id`
2. `latest_closed_bar_time`
3. `decision`
4. canonical message 完整內容

程式亦提供 `trade_monitor.bridge.generate_event_id(...)` 供監控端使用。同一個成功送達的 event ID 再次執行時會回傳 `duplicate`，不重新推送；不同 event ID 可正常發送。

去重狀態位於 `.runtime/trade_monitor_delivery_state.json`，鎖檔位於同目錄。狀態檔只保存 event ID、UTC 時間、狀態、chunk count 與必要錯誤碼，不保存訊息、Token、Chat ID或 Telegram message ID；寫入使用臨時檔加原子替換，並以 Windows／POSIX 檔案鎖避免同時傳送競態。

## 關機或中斷後恢復通知

`trade_monitor.resume_guard` 只追蹤監控執行連續性，不參與盤勢分析。每輪分析開始前執行：

```powershell
python -m trade_monitor.resume_guard begin --gap-seconds 180
```

stdout JSON 的 `force_notify=true` 表示沒有前次成功紀錄、距離前次成功執行已超過 180 秒，或先前的恢復通知尚未成功送達。這只會把該輪最終傳輸決策提升為 `NOTIFY`，不得更改趨勢、型態、觸發、停損、風險或禁止條件。圖表可讀時發送既有欄位順序的完整快照；圖表不可讀時至少發送「監控已恢復」及目前中斷狀態。

每輪分析與傳輸處理完成後，使用 `begin` 回傳的 run ID 執行：

```powershell
python -m trade_monitor.resume_guard complete --run-id <RUN_ID> --resume-delivered true
```

只有恢復輪的 bridge 回傳 `sent` 或 `duplicate` 時，`resume-delivered` 才能設為 `true`；Telegram 失敗、disabled、skipped、逾時或結果不明確時必須使用 `false`，讓下一輪繼續強制通知。一般連續監控輪不改變原本的 `NOTIFY`／`DONT_NOTIFY` 判斷。

執行狀態保存在 `.runtime/trade_monitor_runtime_state.json`，只包含 UTC 時間、run ID、恢復待送旗標及版本，不保存分析訊息、Token、Chat ID或個人財務資料。狀態寫入同樣使用檔案鎖與原子替換。關機期間不補跑每分鐘歷史分析；重啟後的第一個實際監控週期負責發送恢復快照。

## 狀態與 exit code

- `0`：`sent`、`duplicate`、`dry_run` 或正常 `skipped`。
- `1`：Telegram 實際發送失敗。
- `2`：設定、輸入、狀態檔或鎖定錯誤。

成功 JSON 會包含 `chunk_count` 與 `message_ids` 陣列。失敗資訊已清除 Bot Token 與完整 Chat ID。

## 失敗行為與限制

每個 Telegram chunk 包含第一次在內總共最多嘗試三次；已成功的 chunk 不會因後續 chunk 失敗而在同一次執行中重送。只有所有 chunk 成功才將事件記為 `sent`。

Telegram 沒有接受本機 event ID。若 API 已接受訊息但回應在網路上遺失，重試可能造成部分重複；若多分段訊息在部分成功後失敗，下一輪重跑同一事件也可能重複已成功段落。這是 best-effort 去重，並非 exactly-once delivery。

Bridge 失敗不得阻止 Codex 顯示 canonical message。自動化應保留同一個 message 變數，在 Telegram 失敗時只於 Codex 顯示內容後追加：

```text
**TG傳送失敗：下一輪仍會繼續監控。**
```

此狀態尾註不屬於 canonical message，也不回送 Telegram。

## 安全手動測試

先執行 `--dry-run` 並確認單一 chunk，再換一個全新 event ID 執行一次正式命令。若正式命令逾時或結果不明確，不要以新的 event ID 重送；先檢查 Telegram 群組與去重狀態。

測試訊息不得包含帳號、餘額、持倉、損益、保證金、Token 或 Chat ID。停用同步只需把 `trade_monitor_telegram_enabled` 設回 `false`。
