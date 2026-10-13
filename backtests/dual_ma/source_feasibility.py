from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from stock_ai_bot.selection.historical_universe_service import (
    OfficialUniverseSources,
    load_official_universe_sources,
)


STUDY_START = date(2021, 1, 1)
STUDY_END = date(2022, 12, 30)


@dataclass(frozen=True)
class MarketTransition:
    symbol: str
    pre_mainboard_market: str
    pre_mainboard_start: date
    mainboard_market: str
    mainboard_start: date
    official_transition_url: str
    official_pre_mainboard_url: str | None = None


TRANSITIONS: dict[str, MarketTransition] = {
    "4923.TWO": MarketTransition(
        symbol="4923.TWO",
        pre_mainboard_market="TPEX_ESB",
        pre_mainboard_start=date(2010, 6, 30),
        mainboard_market="TPEX",
        mainboard_start=date(2022, 3, 14),
        official_transition_url="https://www.tpex.org.tw/storage/eb_data/11103/11100541251.html",
    ),
    "6763.TWO": MarketTransition(
        symbol="6763.TWO",
        pre_mainboard_market="TPEX_ESB",
        pre_mainboard_start=date(2020, 4, 9),
        mainboard_market="TPEX",
        mainboard_start=date(2022, 3, 15),
        official_transition_url="https://www.tpex.org.tw/storage/eb_data/11103/11100013181.html",
    ),
    "6829.TWO": MarketTransition(
        symbol="6829.TWO",
        pre_mainboard_market="TPEX_ESB",
        pre_mainboard_start=date(2021, 9, 29),
        mainboard_market="TPEX",
        mainboard_start=date(2022, 3, 11),
        official_transition_url="https://www.tpex.org.tw/storage/eb_data/11103/11100012462.html",
        official_pre_mainboard_url="https://www.tpex.org.tw/storage/eb_data/11009/11001014911.html",
    ),
    "6585.TW": MarketTransition(
        symbol="6585.TW",
        pre_mainboard_market="TPEX_ESB",
        pre_mainboard_start=date(2016, 9, 8),
        mainboard_market="TWSE",
        mainboard_start=date(2022, 5, 20),
        official_transition_url="https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d69a3d6cf9019b8dcf835106aa.pdf",
        official_pre_mainboard_url="https://www.tpex.org.tw/storage/eb_data/10508/10501014021.htm",
    ),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _listed_on(row: Mapping[str, Any], market: str) -> date:
    key = "DateOfListing" if market == "TPEX" else "上市日期"
    raw = str(row.get(key) or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"invalid {market} listing date: {raw!r}")
    return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))


def derive_market_identity_rows(
    symbols: Sequence[str], sources: OfficialUniverseSources
) -> list[dict[str, Any]]:
    tpex = {
        str(row.get("SecuritiesCompanyCode") or "").strip(): row
        for row in sources.tpex_current
    }
    twse = {
        str(row.get("公司代號") or "").strip(): row
        for row in sources.twse_current
    }
    output: list[dict[str, Any]] = []
    for symbol in sorted(set(symbols)):
        code, suffix = symbol.split(".", 1)
        expected_market = "TWSE" if suffix == "TW" else "TPEX"
        index = twse if expected_market == "TWSE" else tpex
        row = index.get(code)
        if row is None:
            raise ValueError(f"official current-market identity missing: {symbol}")
        listed_on = _listed_on(row, expected_market)
        transition = TRANSITIONS.get(symbol)
        if transition is not None and transition.mainboard_start != listed_on:
            raise ValueError(
                f"transition date disagrees with official identity for {symbol}: "
                f"{transition.mainboard_start} != {listed_on}"
            )
        output.append(
            {
                "symbol": symbol,
                "code": code,
                "v1_suffix_market": expected_market,
                "official_current_market": expected_market,
                "official_mainboard_start": listed_on.isoformat(),
                "historical_route": "TRANSITION" if transition else "STABLE_MAINBOARD",
                "pre_mainboard_market": transition.pre_mainboard_market if transition else "",
                "pre_mainboard_ohlc_eligible": False if transition else "",
                "route_decision": (
                    "EXCLUDE_PRE_MAINBOARD_NO_COMPARABLE_OHLC"
                    if transition
                    else "INCLUDE_FULL_STUDY_WINDOW"
                ),
                "transition_evidence": transition.official_transition_url if transition else "",
                "official_identity_snapshot_id": sources.source_id,
            }
        )
    return output


def route_segments(identity_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for row in identity_rows:
        symbol = str(row["symbol"])
        market = str(row["official_current_market"])
        transition = TRANSITIONS.get(symbol)
        if transition is None:
            segments.append(
                {
                    "symbol": symbol,
                    "from": STUDY_START.isoformat(),
                    "through": STUDY_END.isoformat(),
                    "historical_market": market,
                    "price_action": "INCLUDE_OFFICIAL_MAINBOARD_OHLC",
                    "reason": "official mainboard listing predates study window",
                    "official_evidence": "official identity snapshot",
                }
            )
            continue
        if transition.pre_mainboard_start > STUDY_START:
            segments.append(
                {
                    "symbol": symbol,
                    "from": STUDY_START.isoformat(),
                    "through": (transition.pre_mainboard_start - timedelta(days=1)).isoformat(),
                    "historical_market": "NOT_EXCHANGE_TRADED",
                    "price_action": "EXCLUDE",
                    "reason": "no official exchange trading period",
                    "official_evidence": transition.official_pre_mainboard_url or "PENDING",
                }
            )
        esb_start = max(STUDY_START, transition.pre_mainboard_start)
        segments.append(
            {
                "symbol": symbol,
                "from": esb_start.isoformat(),
                "through": (transition.mainboard_start - timedelta(days=1)).isoformat(),
                "historical_market": transition.pre_mainboard_market,
                "price_action": "EXCLUDE",
                "reason": "TPEx states ESB has no open or close; average/last price cannot be mapped to OHLC",
                "official_evidence": transition.official_transition_url,
            }
        )
        segments.append(
            {
                "symbol": symbol,
                "from": transition.mainboard_start.isoformat(),
                "through": STUDY_END.isoformat(),
                "historical_market": transition.mainboard_market,
                "price_action": "INCLUDE_OFFICIAL_MAINBOARD_OHLC",
                "reason": "first comparable official mainboard OHLC segment",
                "official_evidence": transition.official_transition_url,
            }
        )
    return segments


def _v1_impact(
    identity_rows: Sequence[Mapping[str, Any]],
    input_manifest: Mapping[str, Any],
    input_root: Path,
    issue_rows: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    prices = {str(row["symbol"]): row for row in input_manifest["prices"]}
    issues_by_symbol: dict[str, list[Mapping[str, str]]] = {}
    for issue in issue_rows:
        issues_by_symbol.setdefault(str(issue["symbol"]), []).append(issue)
    output: list[dict[str, Any]] = []
    for identity in identity_rows:
        symbol = str(identity["symbol"])
        transition = TRANSITIONS.get(symbol)
        if transition is None:
            continue
        price_info = prices[symbol]
        with (input_root / str(price_info["path"])).open(encoding="utf-8-sig", newline="") as handle:
            price_rows = list(csv.DictReader(handle))
        before = [row for row in price_rows if date.fromisoformat(row["date"]) < transition.mainboard_start]
        after = [row for row in price_rows if date.fromisoformat(row["date"]) >= transition.mainboard_start]
        selections_before = 0
        for selection in input_manifest["selections"]:
            effective = selection.get("effective_on")
            if not effective or date.fromisoformat(str(effective)) >= transition.mainboard_start:
                continue
            selections_before += sum(
                str(candidate.get("symbol")) == symbol
                for candidate in selection["candidates"]
            )
        issues = issues_by_symbol.get(symbol, [])
        output.append(
            {
                "symbol": symbol,
                "mainboard_start": transition.mainboard_start.isoformat(),
                "v1_rows_removed_pre_mainboard": len(before),
                "v1_rows_retained_on_after_mainboard": len(after),
                "v1_invalid_ohlc_removed_pre_mainboard": sum(
                    date.fromisoformat(row["date"]) < transition.mainboard_start
                    for row in issues
                ),
                "v1_invalid_ohlc_on_after_mainboard": sum(
                    date.fromisoformat(row["date"]) >= transition.mainboard_start
                    for row in issues
                ),
                "selection_events_before_mainboard": selections_before,
                "expected_effect": "natural missing/warmup until 144 comparable mainboard bars",
            }
        )
    return output


def build_source_feasibility_checkpoint(
    *,
    issue_csv: Path,
    input_manifest_path: Path,
    official_universe_source_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite checkpoint: {output_dir}")
    issue_rows = list(csv.DictReader(issue_csv.open(encoding="utf-8-sig", newline="")))
    symbols = sorted({str(row["symbol"]) for row in issue_rows})
    if len(symbols) != 135:
        raise ValueError(f"expected 135 affected symbols, found {len(symbols)}")
    sources = load_official_universe_sources(official_universe_source_path)
    identity_rows = derive_market_identity_rows(symbols, sources)
    transitions = [row for row in identity_rows if row["historical_route"] == "TRANSITION"]
    if {row["symbol"] for row in transitions} != set(TRANSITIONS):
        raise ValueError("unexpected set of 2021-2022 market transitions")
    input_manifest = json.loads(input_manifest_path.read_text(encoding="utf-8-sig"))

    output_dir.mkdir(parents=True)
    identity_path = output_dir / "market_identity_135.csv"
    routes_path = output_dir / "market_route_segments.csv"
    impact_path = output_dir / "transition_v1_impact.csv"
    _write_csv(identity_path, identity_rows)
    _write_csv(routes_path, route_segments(identity_rows))
    _write_csv(
        impact_path,
        _v1_impact(identity_rows, input_manifest, input_manifest_path.parent, issue_rows),
    )

    feasibility = {
        "schema_version": "dual-ma-2022-source-feasibility-v1",
        "status": "HOLD_PENDING_LICENSED_HISTORICAL_OHLC",
        "scope": {"from": STUDY_START.isoformat(), "through": STUDY_END.isoformat()},
        "affected_symbols": len(symbols),
        "stable_mainboard_symbols": len(identity_rows) - len(transitions),
        "market_transition_symbols": len(transitions),
        "market_transition_list": [row["symbol"] for row in transitions],
        "price_basis": "official unadjusted mainboard OHLC; adj_close intentionally absent",
        "esb_decision": {
            "eligible": False,
            "reason": (
                "TPEx states that the negotiated ESB market has no opening or closing price. "
                "Weighted average or last price must not be mapped to close, and open must not be synthesized."
            ),
            "official_faq": "https://www.tpex.org.tw/zh-tw/about/company/faq.html",
        },
        "source_inventory": [
            {
                "source": "Government Data Open Platform / TWSE STOCK_DAY_ALL",
                "authority": "official open data",
                "license": "Open Government Data License 1.0 with attribution",
                "historical_2021_2022_coverage": False,
                "decision": "INSUFFICIENT_CURRENT_DAY_ONLY",
                "url": "https://data.gov.tw/dataset/11549",
            },
            {
                "source": "TWSE historical website query",
                "authority": "official website",
                "license": "website terms prohibit unapproved automated download",
                "historical_2021_2022_coverage": True,
                "decision": "NOT_APPROVED_FOR_BULK_AUTOMATION",
                "url": "https://www.twse.com.tw/zh/trading/historical/stock-day.html",
            },
            {
                "source": "TPEx historical website query",
                "authority": "official website",
                "license": "historical bulk-use permission not established; paid Data E-Shop exists",
                "historical_2021_2022_coverage": True,
                "decision": "NOT_APPROVED_FOR_BULK_AUTOMATION",
                "url": "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/stock-pricing.html",
            },
            {
                "source": "TWSE / TPEx Data E-Shop",
                "authority": "official contracted data product",
                "license": "eligible after account purchase and acceptance of internal-use terms",
                "historical_2021_2022_coverage": True,
                "decision": "PREFERRED_PENDING_USER_PROVISION",
                "url": "https://eshop.twse.com.tw/zh/",
            },
            {
                "source": "User-provided licensed vendor export",
                "authority": "third party",
                "license": "must be evidenced and permit internal research/reproducible snapshot",
                "historical_2021_2022_coverage": None,
                "decision": "ALTERNATIVE_PENDING_LICENSE_PROOF",
                "url": None,
            },
        ],
        "required_next_input": (
            "Licensed official Data E-Shop files for 2021-01-01 through 2022-12-30, "
            "or an explicitly approved vendor export plus its license/basis documentation."
        ),
        "prohibited_repairs": [
            "interpolation",
            "OHLC envelope repair",
            "mapping ESB average or last price to close",
            "synthesizing ESB open or close",
            "mixing adjusted close with unadjusted official OHLC",
        ],
        "official_identity_source": {
            "path": str(official_universe_source_path.resolve()),
            "source_id": sources.source_id,
            "content_sha256": sources.content_sha256,
            "file_sha256": sha256_file(official_universe_source_path),
        },
        "v1_input_manifest": {
            "path": str(input_manifest_path.resolve()),
            "input_manifest_id": input_manifest["input_manifest_id"],
            "file_sha256": sha256_file(input_manifest_path),
        },
        "issue_ledger": {
            "path": str(issue_csv.resolve()),
            "rows": len(issue_rows),
            "file_sha256": sha256_file(issue_csv),
        },
    }
    feasibility_path = output_dir / "source_feasibility.json"
    _write_json(feasibility_path, feasibility)

    report = f"""# 雙均線 2022 官方來源可行性與市場轉換 checkpoint

狀態：`{feasibility['status']}`

## 結論

- 135 檔 OHLC 異常股票均可在凍結的官方公司資料中確認目前市場身分。
- 131 檔於整個研究窗維持正式上櫃；4 檔在 2022 年由興櫃轉正式市場：`4923.TWO`、`6763.TWO`、`6829.TWO`、`6585.TW`。
- 興櫃不能銜接為本策略日 K。櫃買中心明示興櫃沒有開盤價、收盤價，故均價／最後成交價不得映射成 close，也不得合成 open。
- 上述 4 檔只從正式上市／上櫃日開始納入官方 OHLC；此前資料排除，並讓 MA144 暖機不足自然呈現。
- 政府資料開放平臺的合法開放資料只提供當期日資料，無法重建 2021–2022；官網歷史查詢雖有資料，但未取得批次自動下載授權。正式 input-v2 目前必須 HOLD。

## 解鎖條件

提供 2021-01-01 至 2022-12-30 的 TWSE／TPEx 官方 Data E-Shop 內部使用檔案，或提供具明確內部研究授權、同一未還原 OHLC 基準的供應商匯出與授權證據。取得後才會進行 10 筆異常交叉驗證、建立不可變 input-v2、雙跑與 28 案稽核。

## 產物

- `market_identity_135.csv`：135 檔目前市場與正式掛牌日。
- `market_route_segments.csv`：逐檔、逐日期區段的納入／排除路由。
- `transition_v1_impact.csv`：4 檔轉板股票在 v1 的預估移除列與選股事件影響。
- `source_feasibility.json`：授權、口徑、來源候選與 blocker 的機器可讀紀錄。
"""
    report_path = output_dir / "checkpoint_report.md"
    report_path.write_text(report, encoding="utf-8")

    artifacts = []
    for path in sorted(output_dir.iterdir()):
        if path.name == "artifact_manifest.json":
            continue
        artifacts.append(
            {
                "path": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    artifact_manifest = {
        "schema_version": "dual-ma-artifact-manifest-v1",
        "status": feasibility["status"],
        "artifacts": artifacts,
    }
    _write_json(output_dir / "artifact_manifest.json", artifact_manifest)
    return feasibility
