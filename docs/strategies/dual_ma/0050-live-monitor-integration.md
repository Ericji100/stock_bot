# 0050 final-v1.7 ETF 即時監控整合

## 狀態與邊界

2026-10-23 14:34 本機運行更新：使用者已另行明確授權啟用影子監控。已補齊並驗證10/22行情，正式Bot經原BAT啟動，本機設定為 `enabled=true`、`mode=shadow`；新群組啟用確認訊息由Telegram確認送達（message_id=9638）。10/23官方日線於擷取時尚未公布，沒有造價或補零；下一個預定核對時段是當日22:00，尚待實際時段觀察。本次沒有向舊群組發測試，亦未中斷02。完整本機證據見 `.workdata/reports/dual-ma/qa/0050-shadow-activation-qa00-20261023/QA_zh.md`。此次啟用設定尚未另行commit／push，以下整合時保持關閉及較早開發紀錄均為歷史狀態。

2026-10-23 更新：已依使用者授權整合回 `main`，監控仍保持關閉。本次只核准程式整合與離線驗收，不包含重新發送 Telegram、重啟正式 Bot 或啟用 ETF 排程；啟用必須另經使用者明確授權。下列較早日期的開發／啟用檢查紀錄保留作歷史證據，不代表本次自動取得啟用權限。

本次 [main 整合、離線驗收及清理紀錄](qa/0050-main-disabled-integration-2026-10-23.md) 已保存：相關測試323項通過／1項跳過、8項獨立驗收通過、5093日完整歷史逐欄一致；13個03工作樹已移除，7個測試暫存根因執行政策限制仍保留並具名列出。

本模組是影子監控與建議系統，不連接券商、不送單、不推估未知成交價。`config/etf_monitor.json` 預設 `enabled: false`，合併程式不等於啟用正式排程；只有使用者另行明確授權，且資料、群組與 QA 完成後才可改為 `true`。

目前狀態必須分開閱讀（截至 2026-10-22）：production 已唯新增安裝並逐檔 readback `2026-10-01` 至 `2026-10-21` 的 21 份 daily delta 與 4 份 receipt，`2026-10-01`／`2026-10-21` read-only smoke 均通過；尚未存在的 `2026-10-22` delta 會正確回 `DATA_UNAVAILABLE`。fix5 的 ETF/Telegram 受影響測試與 full-history causal parity 是既有順序路徑證據；本修正版處理 preview 寫入、漏晚間確認、漏真正次日執行、22:00 才首次產生或更正指令，以及事件／成交已持久化後 delivery 中斷重試等 P1 邊界。完整 repository test suite 仍為 `NOT_GREEN`；最近同短路徑基準為 2,206 passed、3 skipped、42 failed、24 errors。00 已確認其狀態集合與先前基準相同，歸因包含 checkout CRLF 的 raw-byte SHA、未 mock 的 MiniMax 設定、缺少 `config.json`、既有 topic raw 品質與外部研究 fixtures／舊 contract；這些不屬於本修正版範圍，也沒有用真密鑰或修改無關來源製造假綠。`enabled=false`，因此本文件不宣稱全庫綠燈或可啟用。

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
- final-v1.7 的研究主情境是 `NEXT_SESSION_OPEN`、20 bps。live 端保留訊號日，將 accepted `MOTHER` / `EXIT` 轉為 `BUY_NEXT_OPEN` / `SELL_NEXT_OPEN`；22:00 成功核對時以官方日曆固定並保存真正的 next-session date。即使 13:55 未執行或原決策是 HOLD，22:00 首次產生／更正出的 BUY/SELL 也必須建立具自身成功 confirmation/correction link 的完整 pending。只有對應同一指令的成功事件與 job、calendar pin、該日已驗證 raw open 同時存在才記 shadow fill；在此之前內部 fill 欄保持 NULL，對外只顯示「參考價」，不把參考價描述成已成交價。
- 凍結基底固定由 repository-relative `stock_data/versions/2026-09-30-v3/market-data` 解析，或由專用 `STOCK_AI_BOT_ETF_FROZEN_MARKET_DATA_ROOT` 覆寫；它不再受 `STOCK_AI_BOT_WORKDATA_ROOT` 影響。完整日資料先逐檔 hash 驗證該 registry/package，再接續可寫 workdata 中的 daily delta。
- 凍結基底截止日是 `2026-09-30`，禁止修改。之後的正式資料存放在 production profile 的 `market-data/etf-live/0050/daily-delta-v1/YYYY-MM-DD.json`。每一個日曆日都必須有一份 immutable ledger：交易日是 `OPEN` 並附完整 OHLCV；休市日是 `CLOSED` 且不含 bar。每份 ledger 都引用 content-addressed 的 TWSE 原始 response bytes：`STOCK_DAY`、官方休市表、`TWT48U_ALL` 與 ETF dividend list。載入時重算 receipt SHA、重解析來源並逐值核對 bar/calendar/corporate action；只有 URL 或任意 64 位字串不構成官方驗證。
- 執行價座標固定 `UNADJUSTED_MAINBOARD_OHLCV`；技術連續座標與 fixed initial stop 的企業行動換算由 accepted core 負責。
- 盤中或收盤 provisional bar 只讀 TWSE MIS 官方端點；不使用 Yahoo、不使用 `historical_scan`，也不回退到舊 `etf_0050_long_term_monitor_v1.py`。
- 每份事件保存 `source`、`price_basis`、`as_of`、`snapshot_id`、`snapshot_version` 與 `snapshot_sha256`。缺少、過期、日期不符或 hash 不符時產生 `DATA_UNAVAILABLE`，不產生交易推論。

凍結 final-v1.7 read-only replay gate 不是只查列數。accepted engine 每次只取得一個新 session；該日狀態產生後，立即經真實 service/store 跑 `12:45 → 13:55 → 選定重啟 → 22:00 → 次交易日 raw open shadow fill`，完成後 generator 才可揭露下一根 bar。它對 2006-01-02 至 2026-09-30 共 5,093 日執行 15,279 個 phase job，核對 durable state、event/outbox、pending replacement、restart 與 next-open schedule；固定 stop/MACD/regime 狀態串另以 hash 鎖定，再由 accepted cost/accounting replayer 逐欄比對。這個順序 replay 沒有涵蓋「22:00 整段未執行」或「真正 next session 未觀察、隔多日才恢復」的漏時段，因此不能取代本文件後述的跨日停機反例：

| Artifact | Exact rows |
|---|---:|
| `next-session-open-daily-equity.csv` | 5093 |
| `next-session-open-transactions.csv` | 181 |
| `next-session-open-corporate-actions.csv` | 63 |
| `next-session-open-trade-details.csv` | 91 |

同時逐筆比對 183 筆 frozen signal、signal/fill dates、raw open/reference/fill、shares、commission、tax、cash、fixed stop、MACD exit reason、股息、split、每日 cash/shares/equity 與 91 筆 trade detail。任一欄差異即失敗；fix5 service-level 稽核附件與 SHA/readback manifest 見 `docs/strategies/dual_ma/qa/0050-live-replay-parity-v5.json`、`0050-live-replay-parity-v5.manifest.json`。舊 v5 是當時狀態的歷史證據，不改寫；其中 affected gates 與 full-history parity 通過，不代表漏時段反例或完整 repository suite 已綠。

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

啟用 monitor 後，同一個 BAT/Bot process 先以 `PENDING` 註冊 ETF runtime；既有 post-init 只建立背景 startup task 就返回，原 bot 開始 polling 不等待 backfill。背景 task 依序做 restart recovery、補到前一日的官方 daily delta、專用群組 route verification 與 outbox drain；四者全部成功後才切為 `READY`。`PENDING` 期間三個 ETF jobs 一律 no-op，不會把未完成 backfill 的狀態假裝成正常決策。recovery、backfill 或 route 任一步例外／逾時，runtime 會切為 `DISABLED`、從 `bot_data` 移除並撤銷可取得的 ETF jobs；即使有無法撤銷的 callback，也因找不到同一個 `READY` runtime 而 no-op。這些 ETF 故障不阻止原 bot 啟動，也不回退到舊群組。

背景 backfill 會把缺少的完整日期區間擷取到暫存 bundle，全部重讀驗證成功後，才把 raw receipts、每日 delta 與 bundle manifest 唯新增安裝到 production delta root；目的檔已存在且內容不同時拒絕覆寫，process 中斷後可冪等續跑。舊版留下的 partial delta 或 receipt 會先視為需要 recapture，再由 immutable installer quarantine、重建及全鏈 readback；不同的有效 immutable delta 仍拒絕覆寫。此自動路徑只接受無公司行動歧義的普通 OPEN/CLOSED 日；weekday 無 bar 且不在官方休市表、兩個公司行動來源不一致，或任何公司行動日都停止並要求 operator-reviewed evidence，絕不改用其他來源。

import 工具與 runtime 都會重新驗證 frozen registry 全部 artifact、base identity、receipt 原文 SHA、approved endpoint/parser、解析後 OHLC/calendar/corporate action、日期鏈與 content SHA。22:00 complete load 要求從 frozen cutoff 後到當日的每個日曆日都存在且當日是 OPEN；13:55 provisional load 要求鏈完整到前一日，再只追加同日 TWSE MIS quote。MIS 必須回 `rtcode=0000`/`OK`、正常盤狀態碼、同一交易日的 source clock 與最近成交時間；source clock 超過五分鐘或未確認收盤一律 fail-closed。任何缺日、stale、hash/identity/官方原文不符都回 `DATA_UNAVAILABLE`。

用 production BAT 相同 workdata env 做 read-only 解析 smoke：

```powershell
$env:STOCK_AI_BOT_WORKDATA_ROOT='D:\code\stock_ai_bot\.workdata'
$env:STOCK_AI_BOT_WORKDATA_PROFILE='prod'
$env:STOCK_AI_BOT_ETF_FROZEN_MARKET_DATA_ROOT='D:\code\stock_ai_bot\stock_data\versions\2026-09-30-v3\market-data'
python -m tools.etf_monitor_data_smoke --as-of 2026-09-30
```

隔離 QA 已分別驗證人工 bundle 與 runtime 自動安裝路徑；production `2026-10-01` 至 `2026-10-21` 已安裝並由 00 獨立核對 25 files／109,636 bytes 與 staging/manifest 完全一致，兩端日期 smoke 均通過。`2026-10-22` 在 delta 尚未安裝時回 `DATA_UNAVAILABLE` exit 2 是預期 fail-closed。`config/etf_monitor.json` 仍為 `enabled=false`；資料補齊本身不等於跨日成交阻擋已解除或可直接啟用，仍須本修正版與剩餘 QA 經 00 核准。

## 排程

所有時間固定 `Asia/Taipei`，且先經 ETF 專用的 TWSE official holiday receipt gate。gate 會保存 content-addressed 原文並驗證目標年份；官方 API 中斷或年份不涵蓋時回 `DATA_UNAVAILABLE`，不再沿用 legacy `is_possible_trading_day` 的空集合 fallback：

| 時間 | 階段 | 行為 |
|---|---|---|
| 12:45 | `preview_1245` | TWSE MIS 盤中 provisional bar；訊息明標預覽；可寫 preview event／健康／outbox，但不修改正式 `monitor_state`、pending、fill 或 corporate-action ledger |
| 13:55 | `decision_1355` | 官方收盤後 provisional bar；先處理「已成功晚間核對且固定 next-session date 已到」的 pending：只能使用該固定日期的已驗證 raw open；缺該日資料即具名 `DATA_UNAVAILABLE`、保持 pending，不以恢復日開盤替代。之後只產生 `BUY_NEXT_OPEN`、`SELL_NEXT_OPEN`、`HOLD` |
| 22:00 | `reconciliation_2200` | 必須有當日 hash 驗證完整 snapshot；一致則記錄 confirmation link，差異則追加 correction 並原子替換 pending；13:55 缺席或 HOLD、但 EOD 首次形成 BUY/SELL 時，也建立完整 EOD pending。BUY/SELL 同時用官方日曆固定 `expected_fill_date`。資料不可用則 quarantine 13:55 pending；整段未執行也因缺成功 job/evidence 而不可成交 |

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
| `pending_actions` | next-session-open 影子建議；未成交時 `fill_price` 為 NULL；保存官方固定的 `expected_fill_date`／calendar evidence、成功 22:00 confirmation/correction link，以及 quarantine、restore、cancel/supersede event、時間與原因。只有這些證據與 `SUCCEEDED` reconciliation job 全部一致，才能用固定日期的 raw open 記錄 fill |
| `events` | stable event ID 的 append-only ledger |
| `outbox` | target、body hash、attempt、狀態、Telegram message ID |
| `corrections` | 原事件與追加更正事件的關聯 |
| `corporate_actions` | 股息、股票股利、split/reverse split、減資、面額變更與 rights 的 accepted ledger |
| `job_runs` | phase/date 冪等、成功、失敗與 interrupted 狀態 |
| `routing_smoke` | 每個 work-data profile 最多一次真實路由 smoke |

13:55 的 BUY/SELL 若 22:00 根本未執行，pending 沒有 `reconciled_by_event_id`／`expected_fill_date`，不可成交；22:00 有執行但資料不可用時則標記 `QUARANTINED_RECONCILIATION_REQUIRED`。成功補跑後，相同指令標記 `RESTORED_AFTER_RECONCILIATION`；改為 HOLD 時取消；動作方向、`leg_id` 或 signal reference price 任一改變時，原 pending 被 supersede，並在同一 SQLite transaction 建立唯一 replacement pending。settlement 會再次核對 confirmation/correction event、完整 snapshot、對應 action、`job_runs=SUCCEEDED`、官方 calendar pin 與固定 `expected_fill_date`；恢復時即使已跨多日，也只補讀該日 raw open，不能改用恢復日開盤。若該日 raw open 尚不可得，phase 以 `ETFMONITORNEXTSESSIONOPENUNAVAILABLE` fail-closed，pending 的 fill 欄保持 NULL。更正／恢復訊息具名顯示新舊狀態、動作、leg、參考價與 action ID；資料庫重啟後仍保留完整軌跡。

背景 startup recovery 會把既有 `SENDING → DELIVERY_UNCERTAIN`、`RUNNING → INTERRUPTED`；只有 `PENDING` / `RETRY` 可以自動 claim。此 recovery 與首次 backfill 不阻塞原 bot polling，但在全部 startup gates 完成前 ETF runtime 保持 `PENDING`、所有 ETF jobs no-op。stable event ID 與 payload collision check 仍嚴格防止同 ID 異內容，不能用覆寫或略過碰撞來完成重試。若 EOD transition event 已 append、或 next-open fill 已寫入後才發生 delivery 中斷，重試會分別使用事件內的 durable transition plan、或同一 snapshot 的 durable fill projection 重建原 payload；pending 轉換冪等、成交不重複。Telegram 的外部送達歧義仍由上述人工流程處理。

## 訊息語意

所有對外訊息固定使用名稱「0050 趨勢策略」。內部策略 ID、訊號列舉、資料版本、事件編號與雜湊只保留於 SQLite／稽核 payload，不出現在群組正文。對外不顯示 `final-v1.7`、內部 BUY/SELL/HOLD 代碼、Regime、armed、snapshot、action ID 或其他實作欄位；這只改名稱與文案，不改 accepted 策略、成交時點、風控、持倉、重試或冪等邏輯。

正式進場範例：

```text
0050 趨勢策略｜觸發進場
訊息性質：收盤後正式決策
訊號時間：YYYY-MM-DD 13:55
進場參考價：xx.xx 元
初始防守價：xx.xx 元
策略安排：下一交易日開盤進場
```

正式訊息仍以 `Asia/Taipei` 計算與排程，但公開時間不重複顯示時區後綴。進出場訊息不另顯示「成交狀態」或重複的下單提醒；價格欄固定稱為「進場參考價」／「出場參考價」，不得描述成已成交價。正式出場以「0050 趨勢策略｜觸發出場」開頭，顯示出場參考價、由真實內部原因翻譯的中文出場原因與下一交易日開盤出場；未知原因會停止 render，不把內部代碼或臆測原因送出。沒有新進出場時，依真實策略持倉顯示「持有觀察」或「空手等待」。晚間更正以白話描述實際轉換，例如「收盤資料確認後，取消先前進場訊號，維持空手」；不顯示 event/action/leg 識別碼。

12:45 訊息標題固定「0050 趨勢策略｜盤中預覽」，正文明示「非正式訊號」且沒有下一交易日執行安排，不能冒充 13:55 正式決策。資料異常只顯示中文警示，不產生偽交易訊號；詳細原因保留於內部稽核。離線案例使用「模擬測試｜非真實交易訊號」，正式 runtime 的 offline test event 仍不進入 Telegram outbox。

## 驗證

聚焦測試：

```powershell
python -m pytest -q `
  tests/monitoring/test_etf_monitor_core_replay.py `
  tests/monitoring/test_etf_monitor_store_routing.py `
  tests/monitoring/test_etf_monitor_service.py `
  tests/monitoring/test_etf_monitor_fill_gate_matrix.py `
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
2. Production profile 的 `2026-10-01` 至 `2026-10-21` 已依 reviewed bundle 唯新增安裝並 readback；後續啟用前仍須 read-only smoke 到最近一個已完成交易日。普通日可自動 immutable install，遇公司行動或來源不一致必須停住並由 operator 建立 reviewed evidence。
3. Bot 已加入 `-5527055845`，且 `getChat` 回傳完全相同的 group/supergroup ID。
4. 受影響測試與 preview／漏確認／漏次日執行、完整 12 格 EOD transition restart，以及 fill 後 delivery 中斷重試反例必須通過。完整 repository suite 必須轉綠，或由 00 對每個剩餘失敗做正式 disposition；目前仍有 42 failed、24 errors，雖已由 00 證實為既有且與本修改無關，仍須保留 `NOT_GREEN` 標示。兩個 BAT 與舊群組設定不得修改。
5. 必要時由 00 執行最多一次 smoke。
6. 00 將 `enabled` 改為 `true` 後，才重啟既有單一 Bot process。

## 2026-10-27 即時報價檢查修正

正常盤中回應的 `mt` 可以非零，不再把 `mt=000000` 當作唯一准入條件；`ts` 仍必須表示實際交易。來源維持同一個 TWSE MIS `getStockInfo.jsp`，並依證交所網頁的請求方式加入時間參數，避免中介快取重用舊報價。當 `z` 暫缺時，只能使用同一份回應內 `trade.z` 與其自己的 `trade.t`；不取試撮價、五檔、昨收或其他資料來源。

仍須通過股票身分、同日日期、來源時鐘、實際成交時間、有限正數及 OHLC 一致性檢查。盤中成交過期、未有實際成交或缺價時保持 `DATA_UNAVAILABLE`；正式判讀須有已收盤的實際成交證據，不能把收盤前成交誤認為收盤價。進出場與部位策略核心沒有修改。

SQLite schema 6 新增 `data_failures`：每次失敗分別保存 phase、交易日、exception type、具體 reason code、message、官方來源、查詢／成交欄位、觀測時間與回應 SHA。`job_runs.error` 同時保留該次具體原因。這份逐次稽核與既有固定 ID 告警分開，所以重試遇到不同原因仍能留證，且不改舊告警 payload、不重複發送 Telegram。修正前未保存的詳細原因不會事後猜測回填。

本次測試及當日官方報價的預覽／正式流程驗證紀錄位於 `.workdata/qa/etf-monitor-quote-20261027-v1/`；實測使用隔離資料庫、router=None，不向群組發送測試訊息。監控與排程測試的預設健康檔寫入也導向各測試的暫存目錄，避免影響正在執行的 Bot。

### 通知中的價格與來源（2026-10-27）

盤中預覽列出「盤中現價」，正式與晚間確認列出「收盤價」；使用本次策略輸入的原始價格，不使用還原後指標價格或買賣滑價。官方即時行情只列一個「價格時間」，結合實際成交日期與時刻；只有日資料時列「價格日期」，不補造成交時刻。訊號時間仍是原定判讀時點，與價格時間分開。官方查詢時間不是新成交時間，不顯示為「行情更新」；新事件另以 `data.quote_evidence` 保存來源時鐘、實際成交時間、觀測時間與原件 SHA。重試保留同事件原始證據，既有無此欄位的紀錄也不回填改寫。

盤中資料失敗只表示「本次無法形成盤中預覽」，不表示停用之後的正式判讀。資料異常列出「無可用報價」、「價格時間未能驗證」與預定官方來源，不把未通過檢查的價格當成現價。已送出的通知與稽核不回寫；額外發送須有使用者明確授權，不能以格式變更自動重發既有訊號。新格式驗證見 `.workdata/qa/etf-monitor-message-20261027-v1/`；精簡通知、正式重送授權及 main 發布驗證另存 `.workdata/qa/etf-monitor-release-20261027-v1/`。

### 進出場條件說明（2026-10-27）

使用者授權在預覽、正式與晚間確認的進出場通知顯示 MA21／MA144 分流、實際觸發條件與數值。MA 為簡單日均線；MACD 參數為 21、55、55，柱狀值顯示前一交易日及本次值，極小非零值用科學記號避免捨入成零。預覽用「暫時符合」並等待收盤，正式訊息用「收盤確認」。這些說明只讀 accepted core 的決策、日列與事件，不重新產生策略訊號，也不改既有事件、持倉、發送紀錄或核心 payload。

- MA21 ≥ MA144：進場說明是前一交易日價格 ≤ 前一交易日 MA21、本次價格 > 本次 MA21；顯示本次價格及兩條均線。
- MA21 < MA144：進場說明是 MACD 柱狀值由 ≤ 0 轉為 > 0。
- 價格防守出場只在 MA21 ≥ MA144 執行；顯示價格 < 固定初始防守價。SELL 的持倉已清空，防守價從相同日期與 leg 的實際出場事件讀回，不能取別筆持倉或歷史事件；缺證據顯示「—」，不造值。
- MACD 出場顯示持倉自進場起曾出現非負柱狀值、本次由 ≥ 0 轉為 < 0；低分流另明示不執行價格防守出場。這個條件不保證獲利，不一律稱停利。

情境通知須先明確交代歷史／展示範圍，與今日實際訊號分開；原策略核心、官方行情來源及其他機器人保持原行為。驗證與此次授權發送的收據另存 `.workdata/qa/etf-monitor-conditions-20261027-v1/`。
