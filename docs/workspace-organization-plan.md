# Workspace Organization Plan

This repository now contains two active systems:

1. `stock_ai_bot`: Telegram bot, stock scans, Radar, reports, data backfill, and scheduled jobs.
2. `trade_monitor`: intraday trade monitoring, rule versions, structured AI analysis, replay, and research experiments.

The goal of this plan is to keep those systems from blending together while preserving compatibility with existing commands.

## Current Boundaries

### Product bot

Keep production bot code in the existing top-level modules and `research_center/` until a larger package split is planned.

Examples:

- `main.py`
- `stock_scanner.py`
- `technical_scanner.py`
- `radar_service.py`
- `monitor_service.py`
- `scheduled_all_scan_prepare_service.py`
- `research_center/`

### Trade monitor runtime

Use `trade_monitor/` as the canonical package for monitoring runtime code.

Root-level files such as `trade_monitor_bridge.py`, `trade_monitor_analysis_adapter.py`, `trade_monitor_analysis_contract.py`, and `trade_monitor_resume_guard.py` should remain thin backward-compatible shims only. New code should import from `trade_monitor.*`.

Suggested ownership:

- `trade_monitor/bridge.py`: Telegram / process bridge.
- `trade_monitor/analysis_adapter.py`: AI analysis adapter.
- `trade_monitor/analysis_contract.py`: structured analysis contract.
- `trade_monitor/resume_guard.py`: resume and restart safety.
- `trade_monitor/local_scheduler.py`: local monitoring scheduler.
- `trade_monitor/schemas/`: schema versions.
- `trade_monitor/rules/`: active, legacy, and versioned rules.
- `trade_monitor/docs/`: monitoring-specific documentation.
- `trade_monitor/experiments/`: small experiments that are still directly related to monitor runtime.

Current migrated locations:

- Old root docs `docs/trade-monitor-structured-analysis-integration.md` and `docs/trade-monitor-telegram-bridge.md` now live under `trade_monitor/docs/`.
- Old clock-probe experiment files now live under `trade_monitor/experiments/clock_probe/`.
- Old root monitor tests now live under `tests/trade_monitor/`.

### Replay and research

Use `trade_monitor_replay/` for replay engines and deterministic / AI replay evaluation that still depends on monitor rules.

Suggested ownership:

- `trade_monitor_replay/runner.py`: replay orchestration.
- `trade_monitor_replay/rules/`: replay-specific rule manifests.
- `trade_monitor_replay/plans/`: replay cohorts and run plans.
- `tests/trade_monitor_replay/`: replay test suite.

`trade_monitor_replay/` is intentionally separate from `trade_monitor/`: replay code may simulate historical state, run deterministic or AI review loops, and keep many rule manifests that should not be imported by the live monitor runtime path.

### Research lab scripts

Large one-off research families should eventually move out of the root `scripts/` namespace into a clearer layout:

```text
research_lab/
  course/
    scripts/
    tests/
    docs/
  hybrid/
    scripts/
    tests/
    configs/
  v2_core/
    scripts/
    tests/
    fixtures/
```

Do not move these in the same commit as runtime changes. The script set is large and should be migrated by family with focused tests.

## Ignore Policy

The following are local or generated and should not be committed directly:

- `.codex_tmp/`
- `.v7bridge/`
- `.v7s1/`
- `outputs/`
- `scratch_*.py`
- `scratch_*.json`

If an output becomes an important artifact, promote a small, stable summary into `docs/`, `config/`, or a test fixture instead of committing the full generated output tree.

## Migration Order

1. Keep root-level `trade_monitor_*.py` files as compatibility shims.
2. Move monitor docs and clock-probe experiments under `trade_monitor/` when equivalent files exist there.
3. Keep replay logic under `trade_monitor_replay/`; do not mix it into bot runtime modules.
4. Split research scripts by family only after the monitor tree is stable.
5. Add lightweight test commands for each area:
   - Product bot core tests.
   - Trade monitor runtime tests.
   - Trade monitor replay tests.
   - Research lab tests.

## Test Commands

Use focused commands while reorganizing. This keeps cleanup commits small and avoids confusing product-bot regressions with monitor-only moves.

```powershell
# Trade monitor runtime and clock-probe experiment tests.
.\.venv\Scripts\python.exe -m pytest tests\trade_monitor trade_monitor\experiments\clock_probe -q

# Compatibility shims should continue to import while old automations exist.
.\.venv\Scripts\python.exe -c "import trade_monitor_bridge, trade_monitor_analysis_adapter, trade_monitor_analysis_contract, trade_monitor_resume_guard; import trade_monitor.bridge, trade_monitor.analysis_adapter, trade_monitor.analysis_contract, trade_monitor.resume_guard; print('trade monitor imports ok')"

# Trade monitor replay tests.
.\.venv\Scripts\python.exe -m pytest tests\trade_monitor_replay -q

# Replay CLI and import smoke checks.
.\.venv\Scripts\python.exe -m trade_monitor_replay --help
.\.venv\Scripts\python.exe -c "import trade_monitor_replay; import trade_monitor_replay.runner; import trade_monitor_replay.config; import trade_monitor_replay.rules; print('trade monitor replay imports ok')"
```

The monitor tests are pytest-style tests. `unittest discover` may report zero tests in these folders.

## Definition of Done for Cleanup Commits

Each cleanup commit should satisfy:

- No generated output directories staged.
- No secrets or private config staged.
- `git diff --check` passes.
- Relevant focused tests pass.
- Imports still work from old root-level compatibility entry points.
