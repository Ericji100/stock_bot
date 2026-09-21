# Backtests

This directory contains reproducible backtest scripts and their checked-in result
artifacts.

- Keep ad-hoc one-off experiments in `experiments/scratch/`.
- Keep large local-only generated reports under `reports/` or `outputs/`.
- Promote a backtest here only when the script and result files are useful as a
  stable reference.

## TMF August 2026

- `backtest_tmf_aug2026.py`: reproducible one-minute-bar backtest.
- `tmf_aug2026_backtest_results.json`: aggregate scenario results.
- `tmf_aug2026_backtest_trades.csv`: trade-level output.
- `tmf-2026-08-strategy-backtest.md`: interpretation, assumptions, and conclusions.

The script writes its default result files into this directory regardless of
the current working directory.
