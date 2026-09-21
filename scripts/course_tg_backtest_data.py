"""Freeze price inputs and corporate-action evidence for the TG universe."""
from __future__ import annotations

import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_corporate_action_data import RUN, digest, fetch_yahoo, read, save  # noqa: E402


CATALOG = ROOT / "reports/course_backtest/2026-09-05/tg_selection_catalog_v1/catalog.json"
OUTPUT = ROOT / "reports/course_backtest/2026-09-05/tg_strategy_entry_matrix_v1"


def _stock_map() -> dict[str, dict]:
    return {
        str(row["code"]): row
        for row in read(ROOT / "stock_list.json").get("stocks", []) if row.get("code")
    }


def find_cache(code: str) -> Path:
    matches = sorted((ROOT / ".cache/technical_daily").glob(f"{code}_*.csv"))
    if len(matches) != 1:
        raise FileNotFoundError(f"{code}: expected one technical cache, found {len(matches)}")
    return matches[0]


def build(*, workers: int = 4) -> dict:
    catalog = read(CATALOG)
    old_manifest = read(RUN / "input_manifest.json")
    old = {str(row["code"]): row for row in old_manifest["items"]}
    stocks = _stock_map()
    source_dir = OUTPUT / "sources/prices"
    source_dir.mkdir(parents=True, exist_ok=True)
    items = []
    added = []
    for code in catalog["monitor_codes"]:
        if code in old:
            items.append({**old[code], "coverage_source": "RADAR_CORPORATE_ACTION_V3"})
            continue
        stock = stocks[code]
        source = find_cache(code)
        destination = source_dir / source.name
        if not destination.exists() or digest(destination) != digest(source):
            shutil.copy2(source, destination)
        item = {
            "code": code,
            "name": str(stock.get("name") or code),
            "symbol": str(stock.get("symbol") or source.stem.replace("_", ".")),
            "market": str(stock.get("market") or ("TPEX" if source.stem.endswith("_TWO") else "TWSE")),
            "original_price_path": str(source.resolve()),
            "frozen_price_path": str(destination.resolve()),
            "price_sha256": digest(destination),
            "coverage_source": "TG_ADDED_CORPORATE_ACTION_V1",
        }
        items.append(item)
        added.append(item)

    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_yahoo, item): item for item in added}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                future.result()
            except Exception as exc:  # pragma: no cover - surfaced in manifest
                errors.append({"code": futures[future]["code"], "error": str(exc)})
            if number % 10 == 0 or number == len(futures):
                print(f"TG added Yahoo {number}/{len(futures)} errors={len(errors)}", flush=True)
    manifest = {
        "method_version": "course-tg-backtest-data-v1",
        "catalog_path": str(CATALOG.resolve()),
        "as_of": catalog["window"]["end"],
        "cohort_count": len(items),
        "reused_corrected_count": len(items) - len(added),
        "newly_frozen_count": len(added),
        "fetch_errors": errors,
        "items": sorted(items, key=lambda row: row["code"]),
    }
    save(OUTPUT / "input_manifest.json", manifest)
    if errors:
        raise RuntimeError(f"corporate-action fetch failures: {len(errors)}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    manifest = build(workers=args.workers)
    print(f"TG universe frozen: {manifest['cohort_count']} stocks; new={manifest['newly_frozen_count']}")


if __name__ == "__main__":
    main()
