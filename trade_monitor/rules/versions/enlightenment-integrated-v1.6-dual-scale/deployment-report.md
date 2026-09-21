# v1.6 Chrome dual-scale production deployment

## Result

Formal deployment succeeded and was read back. Automation `1-k` remains `PAUSED`; no monitoring or Telegram delivery was started by the deployment.

## Activated pair

- Automation execution version: `enlightenment-integrated-v1.6-dual-scale`
- Execution prompt SHA-256: `e93b35e5d22b36d630a8600e3efcbb0ed73dca52621c89684b4222ba968afc61`
- Local analysis version: `enlightenment-integrated-v1.6-dual-scale-analysis`
- Analysis prompt SHA-256: `913752a75c746404646d74343391d16c1a12cabdb7e42d04229690d731685972`
- Schema SHA-256: `9985f15c2ce426d73210a84bd334aca6d46804cc5caeccb3ceae4c9dc74c31d8`
- Formal scheduler config SHA-256: `af893b94e33d25ef335f28dafb0d6be80c30258c50d0c892f16d976c74f47018`
- Actual Automation TOML SHA-256 after update: `d8626a3eff97867a1d851207756b43c709d07f6c42b71ee5e3a1ccd25ae11db7`

## Preserved Automation fields

- Automation ID: `1-k`
- Name: `台指期1分K趨勢與交易機會監控`
- Kind: `heartbeat`
- RRULE: `FREQ=MINUTELY;INTERVAL=1`
- Target task: `01a05aa6-d062-7df3-88dd-8e3023be05f1`
- Status: `PAUSED`

## Runtime verification

- Chrome chart was visibly confirmed at DETAIL `180` before formal state initialization.
- Formal dual-scale state: `DETAIL`, `recovery_required=false`.
- `StockAiBot-TradeMonitorLocalClock`: `Running`, exactly one daemon.
- Isolated local scheduler check returned `paused`; it did not capture, analyze, or send Telegram.
- Full relevant test set: `146 passed`.
- All 15 manifest versions passed prompt SHA-256 and UTF-8 byte-length verification.

## Recovery

- Immutable restore point: `pre-dual-scale-v1.5-20260902`.
- Recovery must switch the Automation prompt and `trade_monitor/local_scheduler_config.json` as a pair while preserving the current Automation status.

## Deployment record

- Operation ID: `a3c51ef2-d7bb-4554-9ebc-df3726c51da4`
- Completed at: `2026-09-02T16:24:57.3570414+08:00`
- `active-version.json` and `activation-history.jsonl` were updated only after readback verification.
