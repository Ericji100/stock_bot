import csv
import json
from datetime import date, timedelta
from decimal import Decimal

from backtests.dual_ma import etf_00635u_formal_v1 as formal
from backtests.dual_ma import etf_00635u_prep_v1 as prep


def test_manifest_role_adapter_and_formal_analysis_are_data_agnostic(tmp_path):
    etf = tmp_path / "etf.csv"
    gold = tmp_path / "gold.csv"
    start = date(2015, 4, 15)
    with etf.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "date", "open", "high", "low", "close", "volume",
            "official_session", "tradable", "tradability_reason",
            "price_rebase_factor", "share_factor", "cash_dividend_per_share",
            "dividend_event_id", "dividend_stage",
        ))
        writer.writeheader()
        for index in range(170):
            close = Decimal("20") + Decimal(index % 30) / Decimal("10")
            writer.writerow({
                "date": (start + timedelta(days=index)).isoformat(),
                "open": close, "high": close + 1, "low": close - 1, "close": close,
                "volume": 1000, "official_session": True, "tradable": True,
                "price_rebase_factor": 1, "share_factor": 1,
            })
    with gold.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "source_date", "available_date", "value", "source_timezone",
        ))
        writer.writeheader()
        for index in range(170):
            day = start + timedelta(days=index)
            writer.writerow({
                "source_date": day.isoformat(), "available_date": day.isoformat(),
                "value": 100 + index, "source_timezone": "America/New_York",
            })
    manifest = {
        "schema": prep.MANIFEST_SCHEMA,
        "symbol": "00635U",
        "listing_date": "2015-04-15",
        "immutable": True,
        "files": [
            {"role": "etf_daily_causal", "path": etf.name, "sha256": prep.sha256_file(etf)},
            {"role": "gold_er_official", "path": gold.name, "sha256": prep.sha256_file(gold)},
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    loaded = formal.load_formal_snapshot(manifest_path)
    assert len(loaded.indicator_rows) == 170
    assert loaded.indicator_rows[143].sma144 is not None
    analysis = formal.run_formal_analysis(loaded)
    assert len(analysis["lines"]) == 7
    assert len(analysis["active"]) == 4
    assert len(analysis["cost_sensitivity"]) == 12
