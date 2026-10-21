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
- 凍結基底固定由 repository-relative `stock_data/versions/2026-09-30-v3/market-data` 解析，或由專用 `STOCK_AI_BOT_ETF_FROZEN_MARKET_DATA_ROOT` 覆寫；它不再受 `STOCK_AI_BOT_WORKDATA_ROOT` 影響。完整日資料先逐檔 hash 驗證該 registry/package，再接續可寫 workdata 中的 daily delta。
- 凍結基底截止日是 `2026-09-30`，禁止修改。之後的正式資料存放在 production profile 的 `market-data/etf-live/0050/daily-delta-v1/YYYY-MM-DD.json`。每一個日曆日都必須有一份 immutable ledger：交易日是 `OPEN` 並附完整 OHLCV；休市日是 `CLOSED` 且不含 bar。每份 ledger 都引用 content-addressed 的 TWSE 原始 response bytes：`STOCK_DAY`、官方休市表、`TWT48U_ALL` 與 ETF dividend list。載入時重算 receipt SHA、重解析來源並逐值核對 bar/calendar/corporate action；只有 URL 或任意 64 位字串不構成官方驗證。
- 執行價座標固定 `UNADJUSTED_MAINBOARD_OHLCV`；技術連續座標與 fixed initial stop 的企業行動換算由 accepted core 負責。
- 盤中或收盤 provisional bar 只讀 TWSE MIS 官方端點；不使用 Yahoo、不使用 `historical_scan`，也不回退到舊 `etf_0050_long_term_monitor_v1.py`。
- 每份事件保存 `source`、`price_basis`、`as_of`、`snapshot_id`、`snapshot_version` 與 `snapshot_sha256`。缺少、過期、日期不符或 hash 不符時產生 `DATA_UNAVAILABLE`，不產生交易推論。

凍結 final-v1.7 read-only replay gate 不是只查列數。accepted engine 每次只取得一個新 session；該日狀態產生後，立即經真實 service/store 跑 `12:45 → 13:55 → 選定重啟 → 22:00 → 次交易日 raw open shadow fill`，完成後 generator 才可揭露下一根 bar。它對 2006-01-02 至 2026-09-30 共 5,093 日執行 15,279 個 phase job，核對 durable state、event/outbox、pending replacement、restart 與 next-open schedule；固定 stop/MACD/regime 狀態串另以 hash 鎖定，再由 accepted cost/accounting replayer 逐欄比對：

| Artifact | Exact rows |
|---|---:|
| `next-session-open-daily-equity.csv` | 5093 |
| `next-session-open-transactions.csv` | 181 |
| `next-session-open-corporate-actions.csv` | 63 |
| `next-session-open-trade-details.csv` | 91 |

同時逐筆比對 183 筆 frozen signal、signal/fill dates、raw open/reference/fill、shares、commission、tax、cash、fixed stop、MACD exit reason、股息、split、每日 cash/shares/equity 與 91 筆 trade detail。任一欄差異即失敗；最新 service-level 稽核附件與 SHA/readback manifest 見 `docs/strategies/dual_ma/qa/0050-live-replay-parity-v3.json`、`0050-live-replay-parity-v3.manifest.json`。

## 正式資料更新與 freshness gate

可用下列命令人工建立 review-only staging bundle；此命令本身不安裝 production：

```powershell
python -m tools.etf_monitor_capture_daily_deltas `
  --from 2026-10-01 --through 2026-10-21 `
  --output D:\staging\0050\bundle-20261001-20261021
```

工具保存 exact response bytes，以 SHA-256 命名 receipt，從 receipt 解析 calendar/OHLCV/corporate action，再立即重讀驗算。人工審核 bundle manifest、每份 delta 與原文後，可逐日用唯新增工具安裝：

```powershell
python -m tools.etf_monitor_import_daily_delta --input D:\staging\0050\bundle-20261001-20261021\2026-10-21.json
```

啟用 monitor 後，同一個 BAT/Bot process 會在 startup 先補到前一日，並在 12:45/13:55 再確認前一日鏈、22:00 補當日完整資料。runtime 先把缺少的完整日期區間擷取到暫存 bundle，全部重讀驗證成功後，才把 raw receipts、每日 delta 與 bundle manifest 唯新增安裝到 production delta root；目的檔已存在且內容不同時拒絕覆寫，process 中斷後可冪等續跑。此自動路徑只接受無公司行動歧義的普通 OPEN/CLOSED 日；weekday 無 bar 且不在官方休市表、兩個公司行動來源不一致，或任何公司行動日都停止並要求 operator-reviewed evidence，絕不改用其他來源。

import 工具與 runtime 都會重新驗證 frozen registry 全部 artifact、base identity、receipt 原文 SHA、approved endpoint/parser、解析後 OHLC/calendar/corporate action、日期鏈與 content SHA。22:00 complete load 要求從 frozen cutoff 後到當日的每個日曆日都存在且當日是 OPEN；13:55 provisional load 要求鏈完整到前一日，再只追加同日 TWSE MIS quote。MIS 必須回 `rtcode=0000`/`OK`、正常盤狀態碼、同一交易日的 source clock 與最近成交時間；source clock 超過五分鐘或未確認收盤一律 fail-closed。任何缺日、stale、hash/identity/官方原文不符都回 `DATA_UNAVAILABLE`。

用 production BAT 相同 workdata env 做 read-only 解析 smoke：

```powershell
$env:STOCK_AI_BOT_WORKDATA_ROOT='D:\code\stock_ai_bot\.workdata'
$env:STOCK_AI_BOT_WORKDATA_PROFILE='prod'
$env:STOCK_AI_BOT_ETF_FROZEN_MARKET_DATA_ROOT='D:\code\stock_ai_bot\stock_data\versions\2026-09-30-v3\market-data'
python -m tools.etf_monitor_data_smoke --as-of 2026-09-30
```

隔離 QA 已分別驗證人工 bundle 與 runtime 自動安裝路徑；production 仍未執行安裝，`config/etf_monitor.json` 仍為 `enabled=false`，因此目前仍必須 fail-closed，不可宣稱正式補齊或可直接啟用。只有 00 核准啟用後，既有單一 Bot process 才會執行上述自動更新。

## 排程

所有時間固定 `Asia/Taipei`，且先經 ETF 專用的 TWSE official holiday receipt gate。gate 會保存 content-addressed 原文並驗證目標年份；官方 API 中斷或年份不涵蓋時回 `DATA_UNAVAILABLE`，不再沿用 legacy `is_possible_trading_day` 的空集合 fallback：

| 時間 | 階段 | 行為 |
|---|---|---|
| 12:45 | `preview_1245` | TWSE MIS 盤中 provisional bar；訊息明標預覽；不修改當日正式 `monitor_state`；若有前一交易日 pending，原子記錄其本日 raw-open shadow fill |
| 13:55 | `decision_1355` | 官方收盤後 provisional bar；只產生 `BUY_NEXT_OPEN`、`SELL_NEXT_OPEN`、`HOLD` |
| 22:00 | `reconciliation_2200` | 必須有當日 hash 驗證完整 snapshot；一致則記錄確認，差異則追加 correction；若資料不可用，立即 quarantine 13:55 pending，使次日開盤不能誤記成交 |

三個 callback 掛在同一個 JobQueue，但各有獨立 lock 與 timeout，並由既有 `Application.create_task` 啟動；不進入 legacy `ScheduledTaskService` 序列。`DATA_UNAVAILABLE` 的 job 記為可重試的 `FAILED`，同一 phase/date 以固定延遲安全重試；告警 event ID 與 outbox 保持冪等，資料恢復後才能把 job 標為 `SUCCEEDED`。因此一次暫時性故障不會永久吞掉 13:55/22:00 phase。

## 專用路由與 fail-closed

ETF 唯一允許的 target 是字串 `-5527055845`。每次 outbox 發送依序檢查：

1. outbox target 必須等於 ETF config target。
2. `getChat` 必須成功，回傳 ID 必須完全相同，型別必須是 group 或 supergroup。
3. `sendMessage` 只使用同一 ID；成功後保存 Telegram message ID。

`getChat` 或 route identity 在送出前失敗可安全標記 `RETRY`。一旦呼叫 `sendMessage`，若 exception、回覆缺 message ID，或 process 在 send 成功後、寫入 `DELIVERED` 前中斷，送達狀態一律標記 `DELIVERY_UNCERTAIN`，不自動重送。系統只提供 at-least-once 嘗試，不能宣稱 end-to-end exactly-once；operator 必須先到專用群組人工確認，再執行：

```powershell
python -m tools.etf_monitor_resolve_delivery --database <sqlite-path> --event-id <id> --mark-delivered --telegram-message-id <id>
# 或確認群組確實沒有該訊息後，才允許 retry：
python -m tools.etf_monitor_resolve_delivery --database <sqlite-path> --event-id <id> --confirm-not-delivered
```

不嘗試舊 `chat_id`、trade monitor 群組或任何 fallback。錯誤紀錄會遮蔽 token-like 字串。

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
  "data": {
    "frozen_market_data_relative_path": "stock_data/versions/2026-09-30-v3/market-data",
    "delta_relative_path": "market-data/etf-live/0050/daily-delta-v1"
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
| `pending_actions` | next-session-open 影子建議；未成交時 `fill_price` 為 NULL；只有完成 22:00 核對的 active pending 才可在下一交易日 raw open 記錄 fill；保存 quarantine、restore、cancel/supersede event、時間與原因 |
| `events` | stable event ID 的 append-only ledger |
| `outbox` | target、body hash、attempt、狀態、Telegram message ID |
| `corrections` | 原事件與追加更正事件的關聯 |
| `corporate_actions` | 股息、股票股利、split/reverse split、減資、面額變更與 rights 的 accepted ledger |
| `job_runs` | phase/date 冪等、成功、失敗與 interrupted 狀態 |
| `routing_smoke` | 每個 work-data profile 最多一次真實路由 smoke |

13:55 的 BUY/SELL 在 22:00 資料不可用時先標記 `QUARANTINED_RECONCILIATION_REQUIRED`，next-open settlement 只讀 `PENDING_NEXT_SESSION_OPEN`，所以不會把未確認訊號假裝成交。同日成功補跑後，相同指令標記 `RESTORED_AFTER_RECONCILIATION`；改為 HOLD 時取消；動作方向、`leg_id` 或 signal reference price 任一改變時，原 pending 被 supersede，並在同一 SQLite transaction 建立唯一新 pending。更正／恢復訊息具名顯示新舊狀態、動作、leg、參考價與 action ID。資料庫重啟後仍保留完整軌跡。

啟動時 `SENDING → DELIVERY_UNCERTAIN`、`RUNNING → INTERRUPTED`；只有 `PENDING` / `RETRY` 可以自動 claim。stable event ID 與 payload collision check 防止同 ID 異內容，但 Telegram 的外部送達歧義由上述人工流程處理。

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

正式 full-history parity（會執行 15,279 個 SQLite phase job，依機器可能需數分鐘；不送 Telegram）：

```powershell
$env:STOCK_AI_BOT_ETF_FROZEN_MARKET_DATA_ROOT='D:\code\stock_ai_bot\stock_data\versions\2026-09-30-v3\market-data'
python -m tools.etf_monitor_verify_replay
```

mock 測試全部通過後，如 00 判定安全且確實要做唯一一次真實路由 smoke，才可在 production work-data profile 執行：

```powershell
python tools/etf_monitor_routing_smoke.py --send --confirm-chat-id -5527055845
```

此工具只從既有 `config.json` 讀 token，先做 `getChat`，並在 `sendMessage` 前以 SQLite 原子保留唯一名額；訊息明標測試。同 profile 再執行會拒絕，即使第一次送出結果不明也不重送。一般開發、CI 與本次整合不執行真實發送。

## 啟用前清單

1. frozen ETF registry 的 current 0050 snapshot 存在，manifest 與所有 artifact hash 通過，且 full-history parity 為 `PASS_EXACT`。
2. 首次啟用前，以 production profile 做一次 read-only/isolated dry run，確認 runtime 可從 2026-10-01 補到前一日；普通日可自動 immutable install，遇公司行動或來源不一致必須停住並由 operator 建立 reviewed evidence。production 目前尚未執行此步。
3. Bot 已加入 `-5527055845`，且 `getChat` 回傳完全相同的 group/supergroup ID。
4. 聚焦測試與完整測試通過；未修改兩個 BAT 與舊群組設定。
5. 必要時由 00 執行最多一次 smoke。
6. 00 將 `enabled` 改為 `true` 後，才重啟既有單一 Bot process。
