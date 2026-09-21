# v1.6 Chrome dual-scale RC validation

## Result

RC shadow validation passed. The version remains undeployed: formal Automation `1-k`, formal local scheduler config, Telegram delivery, and `active-version.json` were not switched.

## Automated validation

- `python -m pytest tests/trade_monitor trade_monitor/experiments/clock_probe/test_clock_probe.py -q`
- Result: `145 passed`.
- All 14 manifest versions passed prompt SHA-256 and UTF-8 byte-length verification.
- RC execution prompt embeds the RC analysis prompt exactly.
- v1.5 sections 2 through 14, which contain the trading logic, are byte-for-byte identical in the RC analysis prompt.
- RC schema is byte-for-byte identical to formal `analysis-v4.json`.
- Coordinator tests cover:
  - initial UNKNOWN fail-safe;
  - quarter-hour scheduling;
  - critical-event deferral up to two minutes;
  - overview lease blocking detail capture;
  - same-owner completion and abort;
  - lease expiry recovery;
  - atomic state replacement;
  - stale and future overview rejection;
  - corrupt-state fail-safe;
  - fresh overview injection into local Codex analysis;
  - absence of account, Token and Chat ID fields.

## Live Chrome shadow cycle

- Controlled the already connected Google Remote Desktop Chrome tab only.
- Did not use Computer Use, Windows UI Automation, SendInput, a data box, K-bar hover, product controls, timeframe controls, order controls, or account areas.
- The chart initially showed size about `325`.
- DETAIL at `180` was visually confirmed at the right edge and showed approximately 180 active one-minute bars.
- OVERVIEW at `296` was visually confirmed at the right edge and showed approximately 296 active one-minute bars.
- A first deliberately separated cycle showed that interpreting while the chart remained at 296 is too slow and risks overlapping the next minute.
- The corrected cycle performed `180 → 296 → OVERVIEW screenshot → 180 → recovery screenshot` inside one Chrome-control call. Both screenshots were returned, and the visible taskbar advanced from approximately `15:51:58` to `15:51:59`, so the chart was away from DETAIL for about one to two seconds.
- The final visible chart was restored to `180` and remained aligned to the latest right-edge K.
- An isolated runtime state accepted the overview summary as `FRESH` and did not touch formal runtime state.

## Formal-state verification

- Active version: `enlightenment-integrated-v1.5-scenario-dual-mode`.
- Automation status recorded by active version: `PAUSED`.
- Formal scheduler config SHA-256 remains `a12cb456833d51d80bf734778ff404bee2c85263ab5b04536d63c42ed49a7617`.
- Actual Automation TOML SHA-256 remains `0161b24e23e44c941c01583b2237cfd439c08077eaf7c5043614d4b46c91c4b8`.
- No Telegram message was sent by this validation.

## Remaining deployment gate

Before production activation, run several consecutive quarter-hour heartbeat cycles using the RC execution prompt while Automation remains PAUSED or in an isolated test task. Confirm that every cycle returns to 180 before the next local `:05` DETAIL capture. Only after that should the formal Automation prompt and formal local scheduler config be switched as a verified pair.
