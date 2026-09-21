# v1.7 anchor production validation status

Updated: 2026-09-02 (Asia/Taipei)

## Result

- Status: `production_release_user_accepted_latency`
- Formal eligibility: **promoted after explicit acceptance of the measured latency**
- Current formal version: `enlightenment-integrated-v1.7-anchor`
- Automation `1-k`: `PAUSED`

v1.7 was initially packaged and briefly deployed while paused, but the required chronological n=2／n=3 replay and isolated Shadow gate had not yet been completed. It was therefore rolled back to v1.6 and read-back verified before any active monitoring or Telegram delivery occurred.

The subsequently completed replay supports retaining n=2 for forward observation but does not prove profitability. Isolated Shadow then found and corrected Structured Output compatibility, no-anchor quadrant and no-setup stage defects. The final corrected cycle passed schema／contract validation and persisted legal state v3.

Measured full analysis took 64.876～75.955 seconds in the initial sequence and 71.834 seconds in the final clean cycle. The single-worker process can therefore skip an intervening closed bar. The user explicitly accepted this known limitation on 2026-09-02 and instructed formal promotion; the version uses latest-available-bar semantics rather than promising every-bar continuity.

Verification:

- `python -m pytest tests/trade_monitor -q`: **149 passed**
- Formal v1.7 Automation prompt SHA-256: `62a373257941dc896224011abaaf9e0a8c330f1647c00b1dc34f1463673a431d`
- Formal v1.7 scheduler config SHA-256: `1511e1b171b25692d7666b93e6f9dd06d912eca386e9820790f216321e760c29`
- Actual Automation TOML SHA-256 after promotion: `69a395850189f7edd2471c423eb51fdaa5997960a93336bd5bbf90c3883d6684`

See the RC `validation-report.md` and `n2-n3-comparison.md` for evidence. Automation remains PAUSED after promotion; deployment did not authorize starting monitoring.
