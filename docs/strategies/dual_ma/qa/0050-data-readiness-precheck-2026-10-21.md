# 0050 production delta 資料就緒預檢（截至 2026-10-21）

## 結論

以正式 frozen registry `2005-01-03_2026-09-30_86d91332d197` 為基底，已把 `2026-10-01` 至目前最後一個已結束交易日 `2026-10-21` 的官方來源擷取到全新 review-only staging。21 份 calendar-day delta 與 4 份 raw receipt 全部通過 SHA、approved endpoint/parser、calendar、OHLCV、corporate-action source 與 content hash readback。

資料候選 bundle 完整，但這不等於 production 已就緒：本次沒有執行 importer，production 仍缺 `2026-10-01`，`enabled=false`，完整 repository suite 仍是 `NOT_GREEN`。不得自動匯入或啟用。

## Review-only capture

- Staging：`D:\code\_codex_worktrees\0050-live-monitor-data-readiness\stock_ai_bot\.workdata\review-only\0050-data-readiness-20261001-20261021\official-capture`
- Bundle manifest：4,842 bytes
- Bundle manifest file SHA-256：`4D62944CEFE9B21A47545BE17F3E742C0ECB92EDBD8E3CC2E09795A482497622`
- Bundle content SHA-256：`01b56245c71adfdd83445d679c31543b7badc97ada4d0e434ec9081c2248f009`
- OPEN：14 日
- CLOSED：7 日
- 公司行動／來源不一致／缺 bar／網路錯誤：無

OPEN 日：`10-01`、`10-02`、`10-05`、`10-06`、`10-07`、`10-08`、`10-12`、`10-13`、`10-14`、`10-15`、`10-16`、`10-19`、`10-20`、`10-21`。

CLOSED 日：`10-03`、`10-04`、`10-10`、`10-11`、`10-17`、`10-18` 為週末；`10-09` 為官方休市日。兩個公司行動來源在整段日期均為 0 筆，因此目前沒有需要另建 reviewed corporate-action event 的日期。

## 精確續跑步驟（尚未執行）

以下步驟只能在人工核對 4 份 receipt、21 份 delta 與 bundle manifest，並取得 00 明確授權後執行：

```powershell
$reviewRoot = 'D:\code\_codex_worktrees\0050-live-monitor-data-readiness\stock_ai_bot\.workdata\review-only\0050-data-readiness-20261001-20261021\official-capture'
$expectedBundleSha256 = '4D62944CEFE9B21A47545BE17F3E742C0ECB92EDBD8E3CC2E09795A482497622'
$actualBundleSha256 = (Get-FileHash -LiteralPath "$reviewRoot\bundle-manifest.json" -Algorithm SHA256).Hash
if ($actualBundleSha256 -ne $expectedBundleSha256) { throw 'review bundle hash changed' }

$env:STOCK_AI_BOT_WORKDATA_ROOT = 'D:\code\stock_ai_bot\.workdata'
$env:STOCK_AI_BOT_WORKDATA_PROFILE = 'prod'
$env:STOCK_AI_BOT_ETF_FROZEN_MARKET_DATA_ROOT = 'D:\code\stock_ai_bot\stock_data\versions\2026-09-30-v3\market-data'

Get-ChildItem -LiteralPath $reviewRoot -Filter '2026-??-??.json' |
  Sort-Object Name |
  ForEach-Object {
    python -m tools.etf_monitor_import_daily_delta --input $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "import stopped at $($_.Name)" }
  }

python -m tools.etf_monitor_data_smoke --as-of 2026-10-21
```

成功 readback 後仍保持 `enabled=false`。應先重跑受影響測試、正式 full-history parity，並由 00 disposition 完整 suite 的既有紅燈；啟用必須是另一個明確核准的變更。

## 完整 suite 失敗分類

fix6 完整 suite 為 2,202 passed、3 skipped、46 failed、24 errors。數量與既有結果相同只能證明「計數未增加」，不能證明零回歸。

| 類別 | Failed | Errors | 與本次 live ETF 變動關係 | 隔離環境判讀 |
|---|---:|---:|---|---|
| dual_ma sealed research packages/reports | 30 | 24 | 同屬 ETF/策略資料領域，但不直接覆蓋 startup isolation；不能以 monitoring PASS 取代 | 缺非版本化 `.workdata`、sealed package/report，且 locked hashes 不通；這 54 項在本隔離環境無法驗證 |
| AI workflow/cache | 5 | 0 | 未執行變更的 live ETF modules | 缺 cache/output 或外部 workflow 前提 |
| root `config.json` | 1 | 0 | 是原 bot 啟動前提，但不是 `etf_monitor.json` 或 fix5 registration | untracked `config.json` 不存在，無法在隔離環境驗證原 bot 完整設定 |
| MiniMax integration | 6 | 0 | 未執行變更的 live ETF modules | provider/MCP discovery 前提不可用 |
| trade_monitor version manifests | 4 | 0 | 未執行變更的 live ETF modules | 既有 version hash expectations 在此 checkout 不通 |
| 合計 | 46 | 24 | — | 完整 suite 維持 `NOT_GREEN` |

直接受影響的 ETF/Telegram 集合由 00 獨立驗證為 205 passed、1 skipped；fix5 full-history 5,093 日／15,279 phase replay 亦通過。這些證據支持本次 live monitor 路徑，但不解除上表無法驗證項目。

## Production 保持未變

- `2026-09-30` frozen cutoff smoke：PASS。
- `2026-10-01` production smoke：`DATA_UNAVAILABLE`，仍缺第一份 delta。
- Importer：未執行。
- `config/etf_monitor.json`：`enabled=false`。
- Bot／Telegram：未啟動、未發送。

`STOP_WAIT_00_ETF_MONITOR_QA`
