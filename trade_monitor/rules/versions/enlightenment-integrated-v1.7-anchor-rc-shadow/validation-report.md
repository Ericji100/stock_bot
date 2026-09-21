# v1.7 anchor RC replay and isolated Shadow validation

Validated at: 2026-09-02 (Asia/Taipei)

## Gate result

- RC status: `rc_shadow_validated_user_accepted_latency`
- Promotion eligibility: **accepted by the user with the measured latency／continuity limitation**
- Formal active version: `enlightenment-integrated-v1.7-anchor`
- Automation `1-k`: `PAUSED`
- Formal scheduler config after user-approved promotion: v1.7／schema v5／market structure state v3
- Shadow transport: `--dry-run` with Telegram disabled
- Formal chart capture, formal market state and formal outbox: not used by Shadow

The causal-state and contract defects found during Shadow were corrected and the final isolated cycle persisted a legal v3 state. Every measured full Codex analysis exceeded one minute, so the current single-worker scheduler cannot guarantee one analysis for every closed 1-minute K. On 2026-09-02 the user explicitly accepted this limitation and instructed formal promotion; this acceptance changes the deployment gate, not the measured result.

## Chronological n=2／n=3 replay

Source: `C:\Users\紀成達\Downloads\tmf_chart_b7d18f803ee54d838cc3194ef647a8bb.html`

- Dataset: 22,791 chronological 1-minute bars across 40 day／night sessions in 2026-08.
- Replay discipline: each bar only used information available at that point; no future bar was used to confirm an earlier pivot prematurely.

| Metric | n=2 | n=3 |
|---|---:|---:|
| Local-confirmed pivots | 5,101 | 3,680 |
| Paired-confirmed pivots | 3,018 | 2,456 |
| Secondary pivots | 722 | 587 |
| Local pivots per 1,000 bars | 223.816 | 161.467 |
| Median confirmation delay | 2 bars | 3 bars |
| Mean confirmation delay | 9.837 bars | 8.778 bars |
| Same-direction extreme replacements | 452 | 334 |
| Type 1 proxy warnings | 173 | 129 |
| Type 1 proxy false-warning rate | 0.9557 | 0.9646 |

Overlap: 3,680 common local pivots, 1,421 n=2-only pivots, zero n=3-only pivots; Jaccard 0.7214. Under this definition, n=3 is a strict subset of n=2 and adds one bar of median confirmation delay. The structural proxy did not show a lower false-warning rate for n=3, so the RC keeps fixed `n=2` for forward observation.

Segment density (local pivots per 1,000 bars):

| Segment | n=2 | n=3 |
|---|---:|---:|
| Day first hour | 225.000 | 164.167 |
| Day general | 223.544 | 159.674 |
| Night first hour | 212.500 | 153.333 |
| Night general | 224.679 | 162.436 |

This replay compares causal pivot mechanics only. It is not a profitability backtest; the Type 1 proxy is not a win rate and does not include four-pattern admission, costs, slippage, exits or R management. Full generated evidence is in `trade_monitor/reports/anchor-n2-n3-2026-08.md` and `.json`.

## Isolated Shadow observations

Five consecutive pre-contract-fix cycles:

| Closed K | Analysis seconds | Total seconds | Telegram |
|---|---:|---:|---|
| 17:42 | 75.955 | 76.563 | dry_run |
| 17:44 | 74.480 | 75.077 | dry_run |
| 17:45 | 71.012 | 71.582 | dry_run |
| 17:46 | 69.862 | 70.450 | dry_run |
| 17:48 | 64.876 | 65.444 | dry_run |

- Mean analysis time: 71.237 seconds; median: 71.012 seconds.
- Closed bars 17:43 and 17:47 were not processed in sequence because the preceding cycle crossed the next minute.
- These cycles exposed a semantic error: no causal anchor existed, yet the model proposed a transition／Q4 candidate. Contract validation was tightened so an empty anchor set requires `UNDEFINED`, null secondary and an empty eliminated set.

Contract follow-up:

- The next cycle correctly returned no anchor and `UNDEFINED`, but used `setup.stage=NO_CHASE` without an actual setup ID. Finalize rejected it and persisted no market-structure state.
- Runtime instructions and tests now require `stage=NONE` with null setup identity when no setup exists. `NO_CHASE` is reserved for an already identified setup whose stable identity and causal timestamps remain present.
- Final clean isolated cycle: latest closed K 18:01; analysis 71.834 seconds; total 72.501 seconds; Telegram `dry_run`; schema／contract accepted; v3 state atomically persisted; anchors empty; working and primary quadrant `UNDEFINED`; setup pattern／stage `NONE`.

## Compatibility defects found and corrected

1. Codex Structured Output rejected schema v5 keywords `allOf`, conditional `if／then／else`, `not` and `uniqueItems`. These unsupported keywords were removed from the output schema; equivalent lifecycle and setup invariants remain enforced by the Python contract.
2. No-anchor quadrant candidates are now fail-safe rejected.
3. No-setup `NO_CHASE` output is now fail-safe rejected and the v3 runtime instruction explicitly maps no setup to `NONE`.

Current schema v5 SHA-256: `ed0a344ddc951adc0dbec41730883680c92262983f51ff4320f07fa93f1c49a8`.

## Automated verification

- `python -m pytest tests/trade_monitor -q`: **149 passed**
- Existing nine-section renderer order: unchanged
- Canonical message／event ID／bridge dry-run／resume guard tests: passed
- v2 formal compatibility and v3 opt-in persistence tests: passed
- RC and formal-candidate version hash checks: passed

## Accepted limitation and follow-up

The user accepted the 65～76 second Shadow timing and requested formal promotion. Production may therefore run with latest-available-bar semantics and can skip an intervening closed bar when the previous analysis crosses the next minute. A future performance improvement may use deterministic local pivot／anchor preprocessing, a materially smaller model payload, or a scheduler design that queues bars without silently skipping them; none of those follow-ups changes the current trading rules unless separately approved.
