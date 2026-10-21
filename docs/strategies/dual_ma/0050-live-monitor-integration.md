# 0050 final-v1.7 ETF 即時監控整合

## 狀態與邊界

本模組是影子監控與建議系統，不連接券商、不送單、不推估未知成交價。`config/etf_monitor.json` 預設 `enabled: false`，合併程式不等於啟用正式排程；只有 00 在資料、群組與 QA 完成後才可改為 `true`。

它沿用既有啟動鏈：

```text
啟動機器人.bat
→ 啟動機器人_runner.bat
→ main.py
→ 既有 ApplicationBuilder / token / JobQueue / run_polling
```

沒有第二個 Bot、第二個 polling process、第二份 token 或新的啟動 BAT。舊 `config["chat_id"]`、`trade_monitor_chat_id`、指令、handler 與排程不變。

## Accepted 策略與資料契約

- 策略版本：`final-v1.7`。
- 策略核心 ID：`MA21_MA144_REGIME_FIXED_INITIAL_STOP_MACD_21_55_55`。
- live adapter 直接呼叫 `backtests/dual_ma/etf_0050_ma21_ma144_regime_v1.py` 的 `build_indicator_rows` 與 `build_regime_run`，不重寫近似 MA/MACD/stop 規則。
- final-v1.7 的研究主情境是 `NEXT_SESSION_OPEN`、20 bps。live 端保留訊號日，將 accepted `MOTHER` / `EXIT` 轉為 `BUY_NEXT_OPEN` / `SELL_NEXT_OPEN`；在真正的下一官方交易日開盤前，成交價永遠顯示「尚未成交」。
- 完整日資料只讀 `stock_ai_bot.data_sources.etf_snapshot_registry` 中已登錄且逐檔 hash 驗證的 package。
- 執行價座標固定 `UNADJUSTED_MAINBOARD_OHLCV`；技術連續座標與 fixed initial stop 的企業行動換算由 accepted core 負責。
- 盤中或收盤 provisional bar 只讀 TWSE MIS 官方端點；不使用 Yahoo、不使用 `historical_scan`，也不回退到舊 `etf_0050_long_term_monitor_v1.py`。
- 每份事件保存 `source`、`price_basis`、`as_of`、`snapshot_id`、`snapshot_version` 與 `snapshot_sha256`。缺少、過期、日期不符或 hash 不符時產生 `DATA_UNAVAILABLE`，不產生交易推論。

凍結 final-v1.7 read-only replay gate 會驗證：

| Artifact | Exact rows |
|---|---:|
| `next-session-open-daily-equity.csv` | 5093 |
| `next-session-open-transactions.csv` | 181 |
| `next-session-open-corporate-actions.csv` | 63 |
| `next-session-open-trade-details.csv` | 91 |

同時要求 `core-parity.json.status == PASS_EXACT`、strategy ID、`NEXT_SESSION_OPEN`、20 bps 與每筆 `signal_date < fill date` 全部維持不變。

## 排程

所有時間固定 `Asia/Taipei`，且先經台灣交易日判斷：

| 時間 | 階段 | 行為 |
|---|---|---|
| 12:45 | `preview_1245` | TWSE MIS 盤中 provisional bar；訊息明標預覽；不修改 `monitor_state` 或 pending action |
| 13:55 | `decision_1355` | 官方收盤後 provisional bar；只產生 `BUY_NEXT_OPEN`、`SELL_NEXT_OPEN`、`HOLD` |
| 22:00 | `reconciliation_2200` | 必須有當日 hash 驗證完整 snapshot；一致則記錄確認，差異則追加 correction 並保留原事件 |

三個 callback 掛在同一個 JobQueue，但各有獨立 lock 與 timeout，並由既有 `Application.create_task` 啟動；不進入 legacy `ScheduledTaskService` 序列。因此 13:50 午報不會阻塞 13:55 ETF 決策，ETF 工作也不會阻塞舊任務。

## 專用路由與 fail-closed

ETF 唯一允許的 target 是字串 `-5527055845`。每次 outbox 發送依序檢查：

1. outbox target 必須等於 ETF config target。
2. `getChat` 必須成功，回傳 ID 必須完全相同，型別必須是 group 或 supergroup。
3. `sendMessage` 只使用同一 ID；成功後保存 Telegram message ID。

任一步失敗都把同一事件標記為 `RETRY`；不嘗試舊 `chat_id`、trade monitor 群組或任何 fallback。錯誤紀錄會遮蔽 token-like 字串。

## 獨立設定

`config/etf_monitor.json` 不可包含 token、`api_token`、`bot_token` 或 `trade_monitor_chat_id`。Bot token 只由既有 `config.json` 的 `api_token` 進入既有 Application。

主要欄位：

```json
{
  "enabled": false,
  "mode": "shadow",
  "chat_id": "-5527055845",
  "symbol": "0050",
  "strategy": "final-v1.7",
  "timezone": "Asia/Taipei",
  "schedule": {
    "preview": "12:45",
    "decision": "13:55",
    "reconciliation": "22:00"
  },
  "daily_shadow_status": true,
  "require_get_chat": true
}
```

`daily_shadow_status=false` 只抑制 13:55 的 `HOLD` 訊息，仍保存正式策略狀態。BUY/SELL、資料警示與核對不受影響。

## SQLite 與重啟語意

資料庫位於 work-data profile 下的 `database/etf_monitor/0050-final-v1.7.sqlite3`，不是 temp/cache，也不提交 Git。production runner 已設定 `STOCK_AI_BOT_WORKDATA_PROFILE=prod`；開發與測試必須使用獨立 profile/path。

| Table | 用途 |
|---|---|
| `schema_meta` | migration version |
| `monitor_state` | position、entry、fixed initial stop、MACD armed、data version 與最後 core state |
| `pending_actions` | 尚未成交的 next-session-open 影子建議；`fill_price` 保持 NULL |
| `events` | stable event ID 的 append-only ledger |
| `outbox` | target、body hash、attempt、狀態、Telegram message ID |
| `corrections` | 原事件與追加更正事件的關聯 |
| `corporate_actions` | 股息、股票股利、split/reverse split、減資、面額變更與 rights 的 accepted ledger |
| `job_runs` | phase/date 冪等、成功、失敗與 interrupted 狀態 |
| `routing_smoke` | 每個 work-data profile 最多一次真實路由 smoke |

啟動時 `SENDING → RETRY`、`RUNNING → INTERRUPTED`，再嘗試同一 dedicated route 的 pending outbox。stable event ID 與 payload collision check 防止重複或同 ID 異內容。

## 訊息語意

所有正式訊息均以「0050 final-v1.7｜影子監控｜僅供策略觀察，非下單指示」開頭。BUY/SELL 範例包含：

```text
決策：BUY_NEXT_OPEN｜下一官方交易日開盤觀察買進
成交價：尚未成交，待下一官方交易日開盤；不預填未知價格。
```

離線案例使用「測試訊息｜非即時交易建議」，只寫入測試 event，不進入 Telegram outbox。覆蓋 BUY、SELL、HOLD、correction 與 restart recovery。

## 驗證

聚焦測試：

```powershell
python -m pytest -q `
  tests/monitoring/test_etf_monitor_core_replay.py `
  tests/monitoring/test_etf_monitor_store_routing.py `
  tests/monitoring/test_etf_monitor_service.py `
  tests/monitoring/test_etf_monitor_integration_data.py
```

mock 測試全部通過後，如 00 判定安全且確實要做唯一一次真實路由 smoke，才可在 production work-data profile 執行：

```powershell
python tools/etf_monitor_routing_smoke.py --send --confirm-chat-id -5527055845
```

此工具只從既有 `config.json` 讀 token，先做 `getChat`，並在 `sendMessage` 前以 SQLite 原子保留唯一名額；訊息明標測試。同 profile 再執行會拒絕，即使第一次送出結果不明也不重送。一般開發、CI 與本次整合不執行真實發送。

## 啟用前清單

1. production ETF registry 的 current 0050 snapshot 存在，manifest 與所有 artifact hash 通過。
2. 每日資料更新 SLA 能在 22:00 前提供當日完整 snapshot 與 corporate-action ledger。
3. Bot 已加入 `-5527055845`，且 `getChat` 回傳完全相同的 group/supergroup ID。
4. 聚焦測試與完整測試通過；未修改兩個 BAT 與舊群組設定。
5. 必要時由 00 執行最多一次 smoke。
6. 00 將 `enabled` 改為 `true` 後，才重啟既有單一 Bot process。
