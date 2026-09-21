# n=2／n=3 causal pivot comparison

This file records the release-gate summary. The generated full evidence is:

- `D:\code\stock_ai_bot\trade_monitor\reports\anchor-n2-n3-2026-08.md`
- `D:\code\stock_ai_bot\trade_monitor\reports\anchor-n2-n3-2026-08.json`

| Metric | n=2 | n=3 |
|---|---:|---:|
| Chronological 1-minute bars | 22,791 | 22,791 |
| Local-confirmed pivots | 5,101 | 3,680 |
| Paired-confirmed pivots | 3,018 | 2,456 |
| Secondary pivots | 722 | 587 |
| Local pivots per 1,000 bars | 223.816 | 161.467 |
| Median confirmation delay | 2 bars | 3 bars |
| Mean confirmation delay | 9.837 bars | 8.778 bars |

Common local pivots: 3,680; n=2-only: 1,421; n=3-only: 0; Jaccard: 0.7214. This is a causal-structure comparison, not a profitability result. The RC keeps `n=2` for further forward observation because n=3 is a strict subset with one additional bar of median delay and the chosen Type 1 proxy did not improve.
