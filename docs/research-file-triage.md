# Research File Triage

Date: 2026-09-21

This note records the remaining untracked research files after the product bot, trade monitor, replay, scan, Radar, chart, and research data pipeline commits were pushed.

The goal is to avoid committing the remaining files as one large, mixed change. Each family should be reviewed, moved, tested, and committed separately.

## Current Inventory

Untracked workspace shape:

| Area | Count | Notes |
|---|---:|---|
| `course_knowledge_base/` | 330 | Course source material, images, transcripts, and derived course notes. Contains many binary assets and should not be mixed into code commits. |
| `scripts/` | 323 | Mostly research and replay scripts. The biggest family is `v2_core`. |
| `tests/` | 226 | Tests matching the research script families. |
| `config/` | 21 | AI, hybrid, and calibration configs. |
| `docs/` | 19 | AI, hybrid, course, and trade-monitor research notes. |
| `tools/` | 1 | Benchmark utility. |

Script families:

| Family | Count | Suggested Handling |
|---|---:|---|
| `v2_core*` | 199 | Move as a dedicated `research_lab/v2_core/` package or keep under `scripts/` for one commit after tests are grouped. |
| `hybrid_v3*` | 57 | Treat as `research_lab/hybrid/` and commit with matching config/docs/tests only. |
| `course*` | 25 | Treat as course backtest tooling; keep separate from course source assets. |
| `hybrid_v4*` | 19 | Commit after `hybrid_v3` or keep as a separate next-generation hybrid family. |
| `formal_ai*` | 15 | Group with Enlightenment AI judgement/replay work, not with hybrid. |
| `enlightenment_ai*` | 2 | Group with AI judgement schemas/configs/docs/tests. |
| `validate_enlightenment*` | 2 | Group with Enlightenment AI validation. |
| Other singletons | 4 | Review individually before staging. |

Test families:

| Family | Count | Suggested Handling |
|---|---:|---|
| `test_v2_core*` | 137 | Pair with `v2_core*` scripts. |
| `test_hybrid_v3*` | 39 | Pair with `hybrid_v3*` scripts/configs/docs. |
| `test_course*` | 21 | Pair with `course*` scripts and selected docs. |
| `test_hybrid_v4*` | 10 | Pair with `hybrid_v4*` scripts/configs/docs. |
| `test_enlightenment*` | 5 | Pair with Enlightenment AI configs/docs/scripts. |
| `test_formal_ai*` | 5 | Pair with `formal_ai*` scripts. |
| `test_dual_ma*`, `test_kd_ma*` | 4 | These look closer to product technical strategy work; review separately from the research lab pile. |
| `fixtures/` | 1 | Keep only if the corresponding schema/test family is committed. |

## Recommended Commit Order

1. **Technical strategy leftovers**
   - Review `test_dual_ma*` and `test_kd_ma*`.
   - These may belong to the product bot because dual MA and KD MA logic already exists in `technical_scanner.py`.
   - If they pass independently, commit them before the larger research lab work.

2. **Enlightenment AI judgement package**
   - Configs: `config/enlightenment_ai_*`.
   - Docs: `docs/enlightenment-ai-*`.
   - Scripts: `scripts/enlightenment_ai_*`, `scripts/validate_enlightenment_ai_*`, relevant `formal_ai*` only if directly tied.
   - Tests: `tests/test_enlightenment*`, `tests/fixtures/enlightenment_ai_judgement_v1.example.json`.

3. **Course backtest package**
   - Scripts: `scripts/course_*`.
   - Tests: `tests/test_course*`.
   - Docs: `docs/integrated-course-daily-stock-rules-v2.md`.
   - Do not include the full `course_knowledge_base/` binary asset tree in this package.

4. **Hybrid monitoring packages**
   - Split `hybrid_v3` and `hybrid_v4` into separate commits.
   - Include matching `config/hybrid_*`, `docs/hybrid-*`, `scripts/hybrid_*`, and `tests/test_hybrid_*`.

5. **v2 core research package**
   - Largest and highest-risk set.
   - Consider moving to `research_lab/v2_core/` only after confirming imports and test discovery.
   - Commit after smaller families are stable.

6. **Course knowledge base assets**
   - Decide separately whether these belong in Git.
   - Prefer committing only curated Markdown summaries and source hashes.
   - Large images, `.docx`, and raw transcripts may need Git LFS or an external archive instead of normal Git history.

## Staging Rules

- Stage by family, never by top-level directory.
- Do not stage `course_knowledge_base/` together with executable code.
- Do not stage generated outputs unless they are small stable fixtures.
- Run `git diff --cached --check` before every commit.
- Run a staged secret scan before every commit.
- Run focused tests for the family being committed.

## Candidate Test Commands

```powershell
# Technical strategy leftovers.
.\.venv\Scripts\python.exe -m pytest tests\test_dual_ma_integration.py tests\test_dual_ma_structure.py tests\test_kd_ma_integration.py tests\test_kd_ma_strategy.py -q

# Enlightenment AI judgement.
.\.venv\Scripts\python.exe -m pytest tests\test_enlightenment_ai_judgement_contract.py tests\test_enlightenment_ai_rules_v2.py tests\test_enlightenment_ai_rules_v3.py tests\test_enlightenment_ai_small_test.py tests\test_enlightenment_ai_small_test_judgement_v2.py -q

# Course backtests.
.\.venv\Scripts\python.exe -m pytest tests\test_course_* -q

# Hybrid v3 research.
.\.venv\Scripts\python.exe -m pytest tests\test_hybrid_v3_* -q

# Hybrid v4 research.
.\.venv\Scripts\python.exe -m pytest tests\test_hybrid_v4_* -q

# v2 core research.
.\.venv\Scripts\python.exe -m pytest tests\test_v2_core_* -q
```

## Next Action

Start with the technical strategy leftovers because they are small and likely relate to already committed production functionality.

If those tests pass, create a small commit for the dual MA and KD MA test coverage before touching the larger AI research families.
