# v1.7 anchor production deployment

## Result

Formal deployment succeeded and was read back. Automation `1-k` remains `PAUSED`; the deployment did not start chart capture, Codex analysis or Telegram delivery.

## Activated pair

- Automation execution version: `enlightenment-integrated-v1.7-anchor`
- Execution prompt SHA-256: `4ae7e7fdf6d84f7344607681cbb4f9709e5a2d6e3beddc7419134d12e01c774c`
- Local analysis version: `enlightenment-integrated-v1.7-anchor-analysis`
- Analysis prompt SHA-256: `9645fc981b8085733ec26b88b5b8643e4fba03dfe232ddd09fbd3c4cb13e9abf`
- Schema v5 SHA-256: `57b390c96331842a7bc2a883d5a65174771cda8cbcbc88b8393a41d3cb8b6264`
- Market structure state: v3 with internal `anchor_context`
- Formal scheduler config SHA-256: `e345ebd5b1250256bf25812d8e699ad39cb3d8bc0a18f0d65be63ea1bf29e726`
- Actual Automation TOML SHA-256 after update: `2da7c91a017eef76e2262349260404a5aff594c97296d651f9a1af04aa9314e8`

## Preserved Automation fields

- Automation ID: `1-k`
- Name: `台指期1分K趨勢與交易機會監控`
- Kind: `heartbeat`
- RRULE: `FREQ=MINUTELY;INTERVAL=1`
- Target task: `01a05aa6-d062-7df3-88dd-8e3023be05f1`
- Status: `PAUSED`

## Runtime and recovery

- `StockAiBot-TradeMonitorLocalClock` restarted with exactly one daemon and the verified v1.7 config.
- Because Automation is paused, the daemon remains idle and no Telegram message is sent.
- Isolated `once` verification returned `{"ok":true,"status":"paused"}` without scheduling a capture.
- Post-deployment monitoring test suite: **146 passed**.
- Immutable restore point: `pre-anchor-v1.6-20260902`.
- Recovery must switch Automation prompt and `trade_monitor/local_scheduler_config.json` as a pair while preserving the current Automation status.

## Deployment record

- Completed at: `2026-09-02T17:23:31.8313557+08:00`
- `active-version.json` and `activation-history.jsonl` were updated only after prompt, fields, scheduler config and Automation TOML read-back verification.

## Replay／Shadow completion and user-accepted promotion — 2026-09-02 18:10 +08:00

The earlier paused deployment was rolled back because the replay and isolated Shadow gates had not yet been run. After completing those gates, fixing schema／contract defects and recording the 65～76 second timing, the user explicitly accepted the latency／continuity limitation and requested formal promotion.

- Current execution prompt SHA-256: `62a373257941dc896224011abaaf9e0a8c330f1647c00b1dc34f1463673a431d`
- Current analysis prompt SHA-256: `b4d2492c9db9eaf396c9daf7f7d0b76e61d8f5e97c605224161b60632f60aebf`
- Current schema v5 SHA-256: `ed0a344ddc951adc0dbec41730883680c92262983f51ff4320f07fa93f1c49a8`
- Current scheduler config SHA-256: `1511e1b171b25692d7666b93e6f9dd06d912eca386e9820790f216321e760c29`
- Current Automation TOML SHA-256: `69a395850189f7edd2471c423eb51fdaa5997960a93336bd5bbf90c3883d6684`
- Automation ID／name／RRULE／target task preserved; status remains `PAUSED`.
- Windows task restarted with exactly one scheduler daemon.
- Isolated production-pair `once --dry-run` returned `status=paused`; no capture or Telegram delivery occurred.
- The known limit is accepted: a full analysis may cross the next minute and skip an intervening closed bar. It uses latest-available-bar semantics and is not a guaranteed every-bar feed.
