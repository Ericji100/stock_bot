"""2022 H2 corporate-action correction side report (02 role).

Authority (read-only, 00-frozen):

* ``docs/strategies/dual_ma/2022-h2-corporate-action-report-correction-v1.md``
  raw SHA-256 ``97135898A338A43DA273922D40BC79E368CEFB2A099FA877127065E1C6A1131A``

Question: the already delivered 2022 H2 single-entry + V-pivot exploration run
``h2-vpivot-single-entry-2022h2-v1`` priced every leg off an unadjusted vendor
close proxy, so any leg that was held across an ex-rights / ex-dividend date was
marked against a price series with a level discontinuity.  This module
inventories every such leg from the two frozen official action indexes, corrects
the ones the frozen data can resolve exactly, and isolates the ones it cannot.

What is *not* touched: the 01 selection products, the D -> E -> T life cycle,
the MA + V entry state machine, the ``T``-close execution proxy, the frozen fee
and tax model, the exit rules and the original run directory.  The correction is
a genuine re-run of the same frozen engine on a corporate-action re-based price
series for the affected symbol only; it is never a post-hoc adjustment applied
to the original numbers.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import h1_real_pilot_2022_0419_0428 as pilot
from . import h2_single_entry_v_pivot_2022h2 as h2
from .costs import CATHAY_WEB_028_TW_V1
from .models import Bar, EntryVariant, InstrumentType
from .single_entry_no_add import SingleEntryNoAddEngine


MODULE_VERSION = "dual-ma-h2-corpaction-correction-2022h2-v1"
RUN_ID_DEFAULT = "h2-vpivot-corpaction-correction-v1"
MANIFEST_SCHEMA_VERSION = "dual-ma-h2-corpaction-correction-manifest-v1"
READBACK_SCHEMA_VERSION = "dual-ma-h2-corpaction-correction-readback-v1"
INVENTORY_SCHEMA_VERSION = "dual-ma-h2-corpaction-event-inventory-v1"
SUMMARY_SCHEMA_VERSION = "dual-ma-h2-corpaction-summary-v1"
CASE_SCHEMA_VERSION = "dual-ma-h2-corpaction-single-case-v1"

ORIGINAL_RUN_ID = h2.RUN_ID_DEFAULT
ORIGINAL_RUN_RELATIVE = f"02-causal-backtest/{ORIGINAL_RUN_ID}"
ORIGINAL_TRADES_SHA256 = (
    "8F757478A54E9D5340CD6FEC522A32920A216B52B5066294D8F1BD3554D78119"
)
ORIGINAL_OPEN_POSITIONS_SHA256 = (
    "B18D387A9270EC856897CE8B61E0D3694D49D81728399A7DC18383FA7C7E5FFC"
)
SPEC_RELATIVE = (
    ".worktrees/dual-ma-integration-v0.4.1/docs/strategies/dual_ma/"
    "2022-h2-corporate-action-report-correction-v1.md"
)
# ``<repo>/.workdata/dev/dual-ma`` -> ``<repo>``
WORKDATA_TO_REPO_PARENTS = 2
SPEC_SHA256 = "97135898A338A43DA273922D40BC79E368CEFB2A099FA877127065E1C6A1131A"
SPEC_BYTE_SIZE = 3944

ACTIONS_RELATIVE_DIR = (
    "01-causal-price/2021-2025-official-actions-full-v1/main/raw"
)
TWSE_EX_RIGHT_NAME = "twse-ex-right.json"
TPEX_EX_RIGHT_NAME = "tpex-ex-right.json"
TWSE_EX_RIGHT_SHA256 = (
    "17EC881D7026AC959AEFE6D59EECE5BB283DC38B6B5501D702E646CCEA8CB151"
)
TPEX_EX_RIGHT_SHA256 = (
    "DFAB1060AA67C0C73F62E3FDB97EC32C2CB633A0893D71E54D646583A1BCFE57"
)

EVENT_YEAR = 2022
AS_OF_CUTOFF = h2.AS_OF_CUTOFF
PRIMARY_SLIPPAGE_BPS = h2.PRIMARY_SLIPPAGE_BPS

SOLVED_CASH_ONLY = "CORRECTED_CASH_ONLY"
SOLVED_WITH_STOCK_RECEIVABLE = "CORRECTED_WITH_STOCK_RECEIVABLE"
UNRESOLVED = "CORPORATE_ACTION_UNRESOLVED"
UNRESOLVED_WEIGHT_SPLIT = "TWSE_RIGHT_AND_CASH_SPLIT_NOT_IN_FROZEN_INPUTS"
UNRESOLVED_RIGHTS_ISSUE = "CASH_CAPITAL_INCREASE_TERMS_NOT_IN_FROZEN_INPUTS"
UNRESOLVED_EXIT_OUTSIDE_WINDOW = "CORRECTED_EXIT_DATE_OUTSIDE_FROZEN_EPISODE_WINDOW"

# Display naming only (00 wording correction).  A leg that was held across an
# ex-rights / ex-dividend date and could be re-run locally is *not* "realized
# cash": the cash dividend is entitled but not yet proven credited on the exit
# day, and the free shares are only a receivable valued at the exit close.
CLOSED_EQUITY_LABEL = "已平倉、含應收現金股利的權益損益（不含應收增配股估值）"

# Report-only re-render (added 2026-10-04).  It never re-runs the engine and
# never touches numbers, rules or corporate-action classifications; it copies
# the frozen v1 data artifacts byte-for-byte and re-renders only the wording.
REPORT_ONLY_EDITION = "v2 報告用詞修正版"
REPORT_ONLY_MANIFEST_SCHEMA_VERSION = "dual-ma-h2-corpaction-report-only-manifest-v1"
REPORT_ONLY_READBACK_SCHEMA_VERSION = "dual-ma-h2-corpaction-report-only-readback-v1"
REPORT_ONLY_LATEST_ENTRY = "LATEST_ENTRY.md"
REPORT_ONLY_COPY_ARTIFACTS = (
    "corpaction_event_index_2022.json",
    "corpaction_event_inventory.csv",
    "corpaction_corrected_legs.csv",
    "corpaction_unresolved_legs.csv",
    "corpaction_2540_case.json",
    "year_end_open_positions_corrected.json",
)
# Wording that 00 retired: it claims a realized cash result that the frozen
# evidence cannot support.
FORBIDDEN_REPORT_TOKENS = (
    "已實現現金",
    "可解讀已平倉成本後損益",
    "修正後可解讀已平倉損益",
    "修正已實現",
    "可精確處理",
    "完整修正績效",
)
REQUIRED_REPORT_TOKENS = (
    CLOSED_EQUITY_LABEL,
    "可局部重算 59 筆，其中 3 筆配股權益僅估值",
    "71 筆未解交易已被排除",
    "不可與原始 1,534 筆全體基準直接比較",
    "描述性合計只加",
    "不含另外列示的應收增配股估值",
)

# The one event whose cash / stock split is fixed by the frozen 00 correction
# spec itself (public MOPS announcement quoted there): Fubon No.1 REIT is not
# the case; Ai-Shan-Lin 2540, ex-date 2022-09-19, TWD 3.5 cash per share and
# 0.65 free shares per share.
SPEC_FIXED_TERMS: Mapping[tuple[str, str, str], Mapping[str, str]] = {
    ("TWSE", "2540", "2022-09-19"): {
        "cash_per_share": "3.5",
        "stock_per_share": "0.65",
        "basis": "MOPS company announcement quoted in the frozen correction spec",
    }
}

FOCUS_CODE = "2540"
FOCUS_EVENT_DATE = "2022-09-19"
FOCUS_STRATEGY = "MEDIUM_5_21"


class H2CorpActionError(RuntimeError):
    """A structural problem with a declared frozen input."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest().upper()


def _json_bytes(value: Any) -> bytes:
    return canonical_bytes(value)


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _dstr(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _dstr(value: Decimal | None, places: int = 4) -> str | None:
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-places)
    return str(Decimal(value).quantize(quantum))


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


_ROC_DATE = re.compile(r"^(\d{2,3})\D+(\d{1,2})\D+(\d{1,2})\D*$")


def roc_date_to_iso(raw: str) -> str | None:
    """``111年09月19日`` / ``111/09/19`` -> ``2022-09-19``; None when unusable."""

    match = _ROC_DATE.match(str(raw).strip())
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year + 1911, month, day).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# frozen input verification
# ---------------------------------------------------------------------------


def verify_frozen_inputs(workdata_root: str | Path) -> dict[str, Any]:
    workdata_root = Path(workdata_root)
    run_dir = workdata_root / ORIGINAL_RUN_RELATIVE
    raw_dir = workdata_root / ACTIONS_RELATIVE_DIR
    checks: dict[str, Any] = {}

    repo_root = workdata_root.parents[WORKDATA_TO_REPO_PARENTS]
    spec_path = repo_root / SPEC_RELATIVE
    if not spec_path.is_file():
        raise H2CorpActionError(f"frozen correction spec is missing: {spec_path}")
    spec_bytes = spec_path.read_bytes()
    checks["spec"] = {
        "path": str(spec_path),
        "sha256": hashlib.sha256(spec_bytes).hexdigest().upper(),
        "byte_size": len(spec_bytes),
    }
    if checks["spec"]["sha256"] != SPEC_SHA256:
        raise H2CorpActionError(
            f"correction spec sha256 {checks['spec']['sha256']} != {SPEC_SHA256}"
        )
    if checks["spec"]["byte_size"] != SPEC_BYTE_SIZE:
        raise H2CorpActionError(
            f"correction spec byte size {checks['spec']['byte_size']} != {SPEC_BYTE_SIZE}"
        )

    for name, expected in (
        ("trades.csv", ORIGINAL_TRADES_SHA256),
        ("year_end_open_positions.json", ORIGINAL_OPEN_POSITIONS_SHA256),
    ):
        path = run_dir / name
        observed = _sha256_file(path)
        checks[name] = {"path": str(path), "sha256": observed}
        if observed != expected:
            raise H2CorpActionError(f"{name} sha256 {observed} != {expected}")

    for name, expected in (
        (TWSE_EX_RIGHT_NAME, TWSE_EX_RIGHT_SHA256),
        (TPEX_EX_RIGHT_NAME, TPEX_EX_RIGHT_SHA256),
    ):
        path = raw_dir / name
        observed = _sha256_file(path)
        checks[name] = {"path": str(path), "sha256": observed}
        if observed != expected:
            raise H2CorpActionError(f"{name} sha256 {observed} != {expected}")
    return checks


# ---------------------------------------------------------------------------
# official ex-right / ex-dividend index
# ---------------------------------------------------------------------------


def load_official_action_index(workdata_root: str | Path) -> list[dict[str, Any]]:
    """Only the two frozen official indexes; only the 2022 events are returned."""

    raw_dir = Path(workdata_root) / ACTIONS_RELATIVE_DIR
    events: list[dict[str, Any]] = []

    twse = _read_json(raw_dir / TWSE_EX_RIGHT_NAME)
    for row in twse.get("data") or ():
        iso = roc_date_to_iso(row[0])
        if iso is None or not iso.startswith(f"{EVENT_YEAR:04d}-"):
            continue
        events.append(
            {
                "market": "TWSE",
                "date": iso,
                "code": str(row[1]).strip(),
                "name": str(row[2]).strip(),
                "kind": str(row[6]).strip(),
                "pre_close": str(row[3]).strip(),
                "ref_price": str(row[4]).strip(),
                "value": str(row[5]).strip(),
                "cash_dividend_per_share": None,
                "stock_per_1000_shares": None,
                "cash_increase_shares": None,
                "cash_increase_price": None,
                "source_file": TWSE_EX_RIGHT_NAME,
                "source_sha256": TWSE_EX_RIGHT_SHA256,
            }
        )

    tpex = _read_json(raw_dir / TPEX_EX_RIGHT_NAME)
    table = (tpex.get("tables") or [{}])[0]
    for row in table.get("data") or ():
        iso = roc_date_to_iso(row[0])
        if iso is None or not iso.startswith(f"{EVENT_YEAR:04d}-"):
            continue
        events.append(
            {
                "market": "TPEX",
                "date": iso,
                "code": str(row[1]).strip(),
                "name": str(row[2]).strip(),
                "kind": str(row[8]).strip(),
                "pre_close": str(row[3]).strip(),
                "ref_price": str(row[4]).strip(),
                "value": str(row[7]).strip(),
                "cash_dividend_per_share": str(row[13]).strip(),
                "stock_per_1000_shares": str(row[14]).strip(),
                "cash_increase_shares": str(row[15]).strip(),
                "cash_increase_price": str(row[16]).strip(),
                "source_file": TPEX_EX_RIGHT_NAME,
                "source_sha256": TPEX_EX_RIGHT_SHA256,
            }
        )
    events.sort(key=lambda item: (item["date"], item["market"], item["code"]))
    return events


def index_by_market_code(events: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        index.setdefault((str(event["market"]), str(event["code"])), []).append(dict(event))
    for bucket in index.values():
        bucket.sort(key=lambda item: item["date"])
    return index


def market_of_symbol(symbol: str) -> str:
    return "TWSE" if symbol.endswith(".TW") else "TPEX"


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


def load_trade_rows(run_dir: str | Path) -> list[dict[str, str]]:
    path = Path(run_dir) / "trades.csv"
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def build_event_inventory(
    rows: Sequence[Mapping[str, str]],
    action_index: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    *, cutoff: date = AS_OF_CUTOFF,
) -> list[dict[str, Any]]:
    """Every 0 bps leg whose holding interval covers an official 2022 action."""

    inventory: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("slippage_bps")) != str(PRIMARY_SLIPPAGE_BPS):
            continue
        market = market_of_symbol(str(row["symbol"]))
        entry_date = date.fromisoformat(str(row["entry_signal_date_T"]))
        is_open = str(row["open_censored"]).lower() == "true"
        end_date = cutoff if is_open else date.fromisoformat(str(row["exit_date"]))
        hits = [
            event
            for event in action_index.get((market, str(row["code"])), ())
            if entry_date < date.fromisoformat(str(event["date"])) <= end_date
        ]
        for event in hits:
            inventory.append(
                {
                    "leg_key": _leg_key(row),
                    "code": row["code"],
                    "symbol": row["symbol"],
                    "strategy": row["strategy"],
                    "episode_key": row["episode_key"],
                    "source_selection_date_D": row["source_selection_date_D"],
                    "monitor_effective_date_E": row["monitor_effective_date_E"],
                    "entry_signal_date_T": row["entry_signal_date_T"],
                    "holding_end": end_date.isoformat(),
                    "open_censored": is_open,
                    "exit_date": row["exit_date"],
                    "exit_reason": row["exit_reason"],
                    "shares": row["shares"],
                    "original_costed_net_pnl": row["costed_net_pnl"],
                    "market": market,
                    "event_date": event["date"],
                    "event_kind": event["kind"],
                    "event_pre_close": event["pre_close"],
                    "event_ref_price": event["ref_price"],
                    "event_value": event["value"],
                    "event_source_file": event["source_file"],
                    "event_source_sha256": event["source_sha256"],
                    "event": dict(event),
                }
            )
    inventory.sort(
        key=lambda item: (
            item["code"],
            item["strategy"],
            item["event_date"],
            item["entry_signal_date_T"],
        )
    )
    return inventory


def _leg_key(row: Mapping[str, str]) -> str:
    return "|".join(
        (
            str(row["symbol"]),
            str(row["strategy"]),
            str(row["episode_key"]),
            str(row["entry_signal_date_T"]),
            str(row["leg_id"]),
        )
    )


def group_inventory_by_leg(
    inventory: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in inventory:
        grouped.setdefault(str(item["leg_key"]), []).append(dict(item))
    for bucket in grouped.values():
        bucket.sort(key=lambda item: item["event_date"])
    return grouped


# ---------------------------------------------------------------------------
# solvability
# ---------------------------------------------------------------------------


def resolve_event_terms(event: Mapping[str, Any]) -> dict[str, Any]:
    """Cash / free-share terms per share, or a named reason it cannot be split."""

    market = str(event["market"])
    key = (market, str(event["code"]), str(event["date"]))
    if key in SPEC_FIXED_TERMS:
        fixed = SPEC_FIXED_TERMS[key]
        return {
            "resolved": True,
            "cash_per_share": _dec(fixed["cash_per_share"]),
            "stock_per_share": _dec(fixed["stock_per_share"]),
            "basis": fixed["basis"],
        }

    if market == "TPEX":
        cash_increase = _dec(event.get("cash_increase_shares") or 0)
        cash = _dec(event.get("cash_dividend_per_share") or 0)
        per_thousand = _dec(event.get("stock_per_1000_shares") or 0)
        if cash_increase > 0:
            return {
                "resolved": False,
                "reason": UNRESOLVED_RIGHTS_ISSUE,
                "detail": (
                    "TPEx row lists a paid capital increase; the subscription ratio "
                    "per held share is not in the frozen index"
                ),
            }
        if cash <= 0 and per_thousand <= 0:
            return {
                "resolved": False,
                "reason": UNRESOLVED_RIGHTS_ISSUE,
                "detail": "TPEx row carries no cash dividend and no free-share ratio",
            }
        return {
            "resolved": True,
            "cash_per_share": cash,
            "stock_per_share": per_thousand / Decimal(1000),
            "basis": "TPEx official ex-right row (cash dividend and free shares per 1000)",
        }

    kind = str(event["kind"])
    if kind == "息":
        return {
            "resolved": True,
            "cash_per_share": _dec(event["value"]),
            "stock_per_share": Decimal(0),
            "basis": "TWSE official ex-dividend row: value column is the cash dividend",
        }
    return {
        "resolved": False,
        "reason": UNRESOLVED_WEIGHT_SPLIT,
        "detail": (
            "TWSE row gives only the combined right+dividend value; the frozen index "
            "does not separate cash from free shares (or a paid capital increase)"
        ),
    }


def classify_leg(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    terms = [resolve_event_terms(event) for event in events]
    unresolved = [item for item in terms if not item["resolved"]]
    if unresolved:
        return {
            "correction_class": UNRESOLVED,
            "reason": unresolved[0]["reason"],
            "detail": unresolved[0]["detail"],
            "terms": terms,
        }
    has_stock = any(item["stock_per_share"] > 0 for item in terms)
    return {
        "correction_class": SOLVED_WITH_STOCK_RECEIVABLE if has_stock else SOLVED_CASH_ONLY,
        "reason": None,
        "detail": None,
        "terms": terms,
    }


# ---------------------------------------------------------------------------
# price re-basing and the local re-run
# ---------------------------------------------------------------------------


def rebase_bars(
    bars: Sequence[Bar], events_with_terms: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]
) -> list[Bar]:
    """Value-equivalent per-share series: ``A = (1+s)*R + D`` from the ex-date on.

    Holding one pre-event share at the ex-date leaves ``1+s`` shares worth
    ``(1+s)*R`` plus ``D`` cash per share, so this is the only affine re-basing
    that keeps every level (pivot low, Dow defence, fast/slow SMA) comparable
    across the event.  Pre-event bars are returned untouched, which is what keeps
    D, E, T and the entry fill identical by construction.
    """

    series = list(bars)
    for event, term in events_with_terms:
        ex_date = date.fromisoformat(str(event["date"]))
        cash = term["cash_per_share"]
        stock = term["stock_per_share"]
        if cash == 0 and stock == 0:
            continue
        factor = Decimal(1) + stock
        series = [
            (
                Bar(
                    bar.date,
                    bar.open * factor + cash,
                    bar.high * factor + cash,
                    bar.low * factor + cash,
                    bar.close * factor + cash,
                )
                if bar.date >= ex_date
                else bar
            )
            for bar in series
        ]
    return series


def run_leg_episode(
    *,
    row: Mapping[str, str],
    bars: Sequence[Bar],
    run_id: str,
    runner_commit: str,
    snapshot_id: str,
    data_hash: str,
) -> list[Any]:
    """One frozen-engine episode run, byte-for-byte the H2 runner's own call."""

    window_end = date.fromisoformat(str(row["regime_window_end"]))
    window_start = date.fromisoformat(str(row["regime_window_start"]))
    run_bars = [bar for bar in bars if bar.date <= window_end]
    manifest = h2._run_manifest(
        code=str(row["code"]),
        bars=run_bars,
        slippage_bps=PRIMARY_SLIPPAGE_BPS,
        run_id=run_id,
        runner_commit=runner_commit,
        snapshot_id=snapshot_id,
        data_hash=data_hash,
    )
    result = SingleEntryNoAddEngine(
        slippage_bps=PRIMARY_SLIPPAGE_BPS,
        entry_variant=EntryVariant.RECLAIM_PIVOT_V_CONFIRMED,
    ).run(
        str(row["symbol"]),
        run_bars,
        run_manifest=manifest,
        instrument_type=pilot.ENGINE_INSTRUMENT_PARAMETER,
        backtest_start=window_start,
        backtest_end=window_end,
    )
    return [
        leg for leg in result.legs if leg.strategy.value == str(row["strategy"])
    ]


def _pick_leg(legs: Sequence[Any], entry_iso: str) -> Any | None:
    matches = [leg for leg in legs if leg.entry_date.isoformat() == entry_iso]
    if len(matches) != 1:
        return None
    return matches[0]


def corrected_flows(
    *,
    shares: int,
    buy_total: Decimal,
    events_with_terms: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    raw_exit_close: Decimal,
) -> dict[str, Any]:
    """Exact cash accounting: sell only the shares that are certainly listed."""

    held = Decimal(shares)
    cash_dividend = Decimal(0)
    rows: list[dict[str, Any]] = []
    for event, term in events_with_terms:
        cash = term["cash_per_share"]
        stock = term["stock_per_share"]
        dividend = held * cash
        cash_dividend += dividend
        added = held * stock
        rows.append(
            {
                "event_date": event["date"],
                "shares_held_before": _dstr(held, 6),
                "cash_per_share": _dstr(cash, 6),
                "stock_per_share": _dstr(stock, 6),
                "cash_dividend": _dstr(dividend, 4),
                "entitled_new_shares": _dstr(added, 6),
                "basis": term["basis"],
            }
        )
        held += added

    sale = CATHAY_WEB_028_TW_V1.sell(raw_exit_close, shares, InstrumentType.STOCK)
    realized = sale.cash_amount + cash_dividend - buy_total
    receivable_shares = held - Decimal(shares)
    receivable_value = receivable_shares * raw_exit_close
    return {
        "event_flows": rows,
        "shares_sold": shares,
        "sale_gross": sale.gross,
        "sale_commission": sale.commission,
        "sale_tax": sale.tax,
        "sale_net": sale.cash_amount,
        "cash_dividend": cash_dividend,
        "receivable_shares": receivable_shares,
        "receivable_value_at_exit_close": receivable_value,
        "realized_net_pnl": realized,
        "economic_total": realized + receivable_value,
        "fractional_receivable": receivable_shares != receivable_shares.to_integral_value(),
    }


# ---------------------------------------------------------------------------
# the whole run
# ---------------------------------------------------------------------------


def run_corpaction_correction(
    *,
    workdata_root: str | Path,
    output_root: str | Path | None = None,
    run_id: str = RUN_ID_DEFAULT,
    runner_commit: str = "UNCOMMITTED",
    progress: Callable[[str, int, int, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    workdata_root = Path(workdata_root)
    out_root = (
        Path(output_root) if output_root is not None else workdata_root / "02-causal-backtest"
    )
    run_dir = out_root / run_id
    if run_dir.exists():
        raise H2CorpActionError(f"refusing to overwrite an existing run directory: {run_dir}")

    checks = verify_frozen_inputs(workdata_root)
    original_run_dir = workdata_root / ORIGINAL_RUN_RELATIVE
    original_trades_sha = _sha256_file(original_run_dir / "trades.csv")
    original_open_sha = _sha256_file(original_run_dir / "year_end_open_positions.json")

    action_events = load_official_action_index(workdata_root)
    action_index = index_by_market_code(action_events)

    rows = load_trade_rows(original_run_dir)
    zero_rows = [row for row in rows if str(row["slippage_bps"]) == "0"]
    inventory = build_event_inventory(rows, action_index)
    grouped = group_inventory_by_leg(inventory)

    symbols = sorted(
        {str(item["symbol"]) for item in inventory}
        | {
            str(row["symbol"])
            for row in zero_rows
            if str(row["open_censored"]).lower() == "true"
        }
    )
    snapshot = pilot.load_price_snapshot(
        workdata_root / pilot.PRICE_SNAPSHOT_RELATIVE_PATH,
        symbols,
        expectations=pilot.ANY_EXPECTATIONS,
    )
    if (_sha256_file(original_run_dir / "trades.csv") != original_trades_sha) or (
        _sha256_file(original_run_dir / "year_end_open_positions.json") != original_open_sha
    ):
        raise H2CorpActionError("the original H2 run changed while this run was loading")

    rows_by_key = {_leg_key(row): row for row in zero_rows}
    corrected_legs: list[dict[str, Any]] = []
    unresolved_legs: list[dict[str, Any]] = []
    baseline_mismatches: list[dict[str, Any]] = []
    entry_not_preserved: list[dict[str, Any]] = []
    focus_case: dict[str, Any] | None = None

    total = len(grouped)
    for index, (leg_key, events) in enumerate(sorted(grouped.items()), start=1):
        row = rows_by_key[leg_key]
        bars = snapshot.bars_by_symbol[str(row["symbol"])]
        raw_events = [dict(item["event"]) for item in events]
        classification = classify_leg(raw_events)
        entry_iso = str(row["entry_signal_date_T"])
        window_end = date.fromisoformat(str(row["regime_window_end"]))

        baseline_legs = run_leg_episode(
            row=row,
            bars=bars,
            run_id=run_id,
            runner_commit=runner_commit,
            snapshot_id=snapshot.snapshot_id,
            data_hash=snapshot.content_sha256,
        )
        baseline_leg = _pick_leg(baseline_legs, entry_iso)
        if baseline_leg is not None:
            if (
                baseline_leg.exit_date.isoformat()
                if baseline_leg.exit_date
                else "OPEN"
            ) != (str(row["exit_date"]) or "OPEN") or (
                _dstr(baseline_leg.net_pnl or Decimal(0))
                != _dstr(_dec(row["costed_net_pnl"] or 0))
            ):
                baseline_mismatches.append({"leg_key": leg_key})

        if classification["correction_class"] == UNRESOLVED:
            unresolved_legs.append(
                {
                    "leg_key": leg_key,
                    "code": row["code"],
                    "symbol": row["symbol"],
                    "strategy": row["strategy"],
                    "source_selection_date_D": row["source_selection_date_D"],
                    "monitor_effective_date_E": row["monitor_effective_date_E"],
                    "entry_signal_date_T": row["entry_signal_date_T"],
                    "exit_date": row["exit_date"],
                    "exit_reason": row["exit_reason"],
                    "shares": int(row["shares"]),
                    "open_censored": str(row["open_censored"]).lower() == "true",
                    "original_costed_net_pnl": _dstr(_dec(row["costed_net_pnl"] or 0)),
                    "reason": classification["reason"],
                    "detail": classification["detail"],
                    "events": [event["date"] for event in raw_events],
                }
            )
            continue

        pairs = list(zip(raw_events, classification["terms"]))
        rebased = rebase_bars(bars, pairs)
        corrected_legs_raw = run_leg_episode(
            row=row,
            bars=rebased,
            run_id=run_id,
            runner_commit=runner_commit,
            snapshot_id=snapshot.snapshot_id,
            data_hash=snapshot.content_sha256,
        )
        corrected_leg = _pick_leg(corrected_legs_raw, entry_iso)
        if corrected_leg is None:
            entry_not_preserved.append({"leg_key": leg_key, "reason": "leg_missing_or_duplicated"})
            continue

        if corrected_leg.exit_date is None and window_end < AS_OF_CUTOFF:
            # The frozen episode window ends on the session where the raw regime
            # ended.  On the re-based series the trend is still up there, so the
            # corrected exit date is genuinely outside everything the frozen
            # episode covers.  Never extended, never guessed.
            unresolved_legs.append(
                {
                    "leg_key": leg_key,
                    "code": row["code"],
                    "symbol": row["symbol"],
                    "strategy": row["strategy"],
                    "source_selection_date_D": row["source_selection_date_D"],
                    "monitor_effective_date_E": row["monitor_effective_date_E"],
                    "entry_signal_date_T": entry_iso,
                    "exit_date": row["exit_date"],
                    "exit_reason": row["exit_reason"],
                    "shares": int(row["shares"]),
                    "open_censored": False,
                    "original_costed_net_pnl": _dstr(_dec(row["costed_net_pnl"] or 0)),
                    "reason": UNRESOLVED_EXIT_OUTSIDE_WINDOW,
                    "detail": (
                        "after re-basing, the leg is still open on "
                        f"{window_end.isoformat()}, the last session of the frozen "
                        "episode window; the corrected exit date is not covered by "
                        "the frozen run and is never extrapolated"
                    ),
                    "events": [event["date"] for event in raw_events],
                }
            )
            continue

        is_open = str(row["open_censored"]).lower() == "true"
        closed_after_correction = corrected_leg.exit_date is not None
        if corrected_leg.exit_date is None:
            raw_exit_close = [bar.close for bar in bars if bar.date <= window_end][-1]
        else:
            raw_exit_close = _raw_close_on(bars, corrected_leg.exit_date)
        flows = corrected_flows(
            shares=int(row["shares"]),
            buy_total=_dec(row["buy_total"]),
            events_with_terms=pairs,
            raw_exit_close=raw_exit_close,
        )
        record = {
            "leg_key": leg_key,
            "code": row["code"],
            "symbol": row["symbol"],
            "strategy": row["strategy"],
            "source_selection_date_D": row["source_selection_date_D"],
            "monitor_effective_date_E": row["monitor_effective_date_E"],
            "entry_signal_date_T": entry_iso,
            "open_censored": is_open,
            "closed_after_correction": closed_after_correction,
            "correction_class": classification["correction_class"],
            "original_exit_date": row["exit_date"],
            "original_exit_reason": row["exit_reason"],
            "original_costed_net_pnl": _dstr(_dec(row["costed_net_pnl"] or 0)),
            "corrected_exit_date": (
                corrected_leg.exit_date.isoformat() if corrected_leg.exit_date else "OPEN"
            ),
            "corrected_exit_reason": (
                corrected_leg.exit_reason.value if corrected_leg.exit_reason else None
            ),
            "corrected_raw_exit_close": _dstr(raw_exit_close),
            "corrected_entry_fill_price": _dstr(corrected_leg.entry_fill_price),
            "corrected_shares": int(corrected_leg.shares),
            "corrected_initial_stop": _dstr(corrected_leg.initial_stop),
            "corrected_effective_stop": _dstr(corrected_leg.effective_stop),
            "corrected_dow_stop": _dstr(corrected_leg.dow_stop),
            "events": [
                {
                    "event_date": event["date"],
                    "event_kind": event["kind"],
                    "market": event["market"],
                    "pre_close": event["pre_close"],
                    "ref_price": event["ref_price"],
                    "cash_per_share": _dstr(term["cash_per_share"], 6),
                    "stock_per_share": _dstr(term["stock_per_share"], 6),
                    "basis": term["basis"],
                }
                for event, term in pairs
            ],
            "shares": int(row["shares"]),
            "buy_total": _dstr(_dec(row["buy_total"])),
            "cash_dividend": _dstr(flows["cash_dividend"]),
            "receivable_shares": _dstr(flows["receivable_shares"], 6),
            "receivable_value_at_exit_close": _dstr(
                flows["receivable_value_at_exit_close"]
            ),
            "realized_net_pnl": _dstr(flows["realized_net_pnl"]),
            "economic_total": _dstr(flows["economic_total"]),
            "delta_vs_original": _dstr(
                flows["realized_net_pnl"] - _dec(row["costed_net_pnl"] or 0)
            ),
            "delta_economic_vs_original": _dstr(
                flows["economic_total"] - _dec(row["costed_net_pnl"] or 0)
            ),
            "fractional_receivable": flows["fractional_receivable"],
            "event_flows": flows["event_flows"],
        }
        corrected_legs.append(record)

        if (
            str(row["code"]) == FOCUS_CODE
            and str(row["strategy"]) == FOCUS_STRATEGY
            and any(str(event["event_date"]) == FOCUS_EVENT_DATE for event in record["events"])
        ):
            focus_case = record

        if progress is not None and index % 25 == 0:
            progress("legs", index, total, {"solved": len(corrected_legs)})

    open_positions = _build_corrected_open_positions(
        rows=zero_rows,
        snapshot=snapshot,
        corrected_legs=corrected_legs,
        unresolved_legs=unresolved_legs,
        cutoff=AS_OF_CUTOFF,
    )
    summary = build_summary(
        zero_rows=zero_rows,
        corrected_legs=corrected_legs,
        unresolved_legs=unresolved_legs,
        open_positions=open_positions,
    )

    gates = _build_gates(
        rows=rows,
        inventory=inventory,
        grouped=grouped,
        corrected_legs=corrected_legs,
        unresolved_legs=unresolved_legs,
        baseline_mismatches=baseline_mismatches,
        entry_not_preserved=entry_not_preserved,
        focus_case=focus_case,
        action_events=action_events,
    )

    artifacts: dict[str, bytes] = {}
    artifacts["corpaction_event_index_2022.json"] = _json_bytes(
        {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "year": EVENT_YEAR,
            "event_count": len(action_events),
            "sources": {
                TWSE_EX_RIGHT_NAME: TWSE_EX_RIGHT_SHA256,
                TPEX_EX_RIGHT_NAME: TPEX_EX_RIGHT_SHA256,
            },
            "events": action_events,
        }
    )
    artifacts["corpaction_event_inventory.csv"] = _csv_bytes(
        INVENTORY_COLUMNS, inventory
    )
    artifacts["corpaction_corrected_legs.csv"] = _csv_bytes(
        CORRECTED_COLUMNS, [_flatten_corrected(item) for item in corrected_legs]
    )
    artifacts["corpaction_unresolved_legs.csv"] = _csv_bytes(
        UNRESOLVED_COLUMNS, unresolved_legs
    )
    artifacts["corpaction_summary.json"] = _json_bytes(summary)
    artifacts["corpaction_2540_case.json"] = _json_bytes(
        {
            "schema_version": CASE_SCHEMA_VERSION,
            "found": focus_case is not None,
            "case": focus_case,
            "baseline_engine_reproduction": {
                "leg_key": focus_case["leg_key"] if focus_case else None,
                "original_run_leg": _focus_original(focus_case, rows_by_key),
            },
        }
    )
    artifacts["year_end_open_positions_corrected.json"] = _json_bytes(open_positions)
    artifacts["report_zh.md"] = _render_report(
        run_id=run_id,
        checks=checks,
        inventory=inventory,
        corrected_legs=corrected_legs,
        unresolved_legs=unresolved_legs,
        summary=summary,
        open_positions=open_positions,
        focus_case=focus_case,
        action_event_count=len(action_events),
    ).encode("utf-8")

    run_dir.mkdir(parents=True, exist_ok=False)
    for name, payload in artifacts.items():
        (run_dir / name).write_bytes(payload)

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "status": "PASS" if all(value == "PASS" for value in gates.values()) else "FAIL",
        "runner_commit": runner_commit,
        "research_only": True,
        "formal_historical_performance": False,
        "point_in_time_verified": False,
        "original_run_id": ORIGINAL_RUN_ID,
        "original_run_sha256_unchanged": {
            "trades.csv": _sha256_file(original_run_dir / "trades.csv"),
            "year_end_open_positions.json": _sha256_file(
                original_run_dir / "year_end_open_positions.json"
            ),
        },
        "inputs": checks,
        "price_snapshot": {
            "root": str(snapshot.root),
            "snapshot_id": snapshot.snapshot_id,
            "content_sha256": snapshot.content_sha256,
            "symbols_loaded": len(symbols),
        },
        "counts": {
            "trade_rows_all_slippage": len(rows),
            "zero_bps_legs": len(zero_rows),
            "affected_legs": len(grouped),
            "affected_events": len(inventory),
            "solved_legs": len(corrected_legs),
            "unresolved_legs": len(unresolved_legs),
        },
        "gates": gates,
        "artifact_sha256": {
            name: hashlib.sha256(payload).hexdigest().lower()
            for name, payload in sorted(artifacts.items())
        },
        "note": "No wall-clock timestamp is recorded so the run stays reproducible.",
    }
    readback = _build_readback(run_dir=run_dir, manifest=manifest, summary=summary)
    manifest["readback_status"] = readback["status"]
    (run_dir / "manifest.json").write_bytes(_json_bytes(manifest))
    (run_dir / "readback.json").write_bytes(_json_bytes(readback))
    return manifest


def _focus_original(
    focus_case: Mapping[str, Any] | None, rows_by_key: Mapping[str, Mapping[str, str]]
) -> dict[str, Any] | None:
    if focus_case is None:
        return None
    row = rows_by_key[focus_case["leg_key"]]
    return {
        key: row.get(key)
        for key in (
            "source_selection_date_D",
            "monitor_effective_date_E",
            "entry_signal_date_T",
            "entry_fill_price",
            "shares",
            "initial_stop",
            "pivot_low",
            "breakout_level",
            "exit_date",
            "exit_reason",
            "exit_fill_price",
            "buy_total",
            "sell_net",
            "costed_net_pnl",
        )
    }


def _raw_close_on(bars: Sequence[Bar], day: date) -> Decimal:
    for bar in bars:
        if bar.date == day:
            return bar.close
    raise H2CorpActionError(f"no raw bar for {day.isoformat()}")


INVENTORY_COLUMNS = (
    "leg_key",
    "code",
    "symbol",
    "strategy",
    "market",
    "source_selection_date_D",
    "monitor_effective_date_E",
    "entry_signal_date_T",
    "holding_end",
    "open_censored",
    "exit_date",
    "exit_reason",
    "shares",
    "original_costed_net_pnl",
    "event_date",
    "event_kind",
    "event_pre_close",
    "event_ref_price",
    "event_value",
    "event_source_file",
    "event_source_sha256",
    "episode_key",
)

CORRECTED_COLUMNS = (
    "leg_key",
    "code",
    "symbol",
    "strategy",
    "correction_class",
    "source_selection_date_D",
    "monitor_effective_date_E",
    "entry_signal_date_T",
    "open_censored",
    "closed_after_correction",
    "original_exit_date",
    "original_exit_reason",
    "original_costed_net_pnl",
    "corrected_exit_date",
    "corrected_exit_reason",
    "corrected_raw_exit_close",
    "corrected_entry_fill_price",
    "corrected_shares",
    "corrected_initial_stop",
    "corrected_effective_stop",
    "corrected_dow_stop",
    "shares",
    "buy_total",
    "cash_dividend",
    "receivable_shares",
    "receivable_value_at_exit_close",
    "realized_net_pnl",
    "economic_total",
    "delta_vs_original",
    "delta_economic_vs_original",
    "fractional_receivable",
    "event_dates",
)

UNRESOLVED_COLUMNS = (
    "leg_key",
    "code",
    "symbol",
    "strategy",
    "source_selection_date_D",
    "monitor_effective_date_E",
    "entry_signal_date_T",
    "exit_date",
    "exit_reason",
    "shares",
    "open_censored",
    "original_costed_net_pnl",
    "reason",
    "detail",
    "events",
)


def _flatten_corrected(item: Mapping[str, Any]) -> dict[str, Any]:
    flat = dict(item)
    flat["event_dates"] = ",".join(str(event["event_date"]) for event in item["events"])
    for key in ("events", "event_flows"):
        flat.pop(key, None)
    return flat


def _csv_bytes(columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bytes:
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name) for name in columns})
    return buffer.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# year-end valuation and the main table
# ---------------------------------------------------------------------------


def _build_corrected_open_positions(
    *,
    rows: Sequence[Mapping[str, str]],
    snapshot: Any,
    corrected_legs: Sequence[Mapping[str, Any]],
    unresolved_legs: Sequence[Mapping[str, Any]],
    cutoff: date,
) -> dict[str, Any]:
    corrected_by_key = {item["leg_key"]: item for item in corrected_legs}
    unresolved_by_key = {item["leg_key"]: item for item in unresolved_legs}
    positions: list[dict[str, Any]] = []
    for row in rows:
        if str(row["open_censored"]).lower() != "true":
            continue
        leg_key = _leg_key(row)
        bars = snapshot.bars_by_symbol[str(row["symbol"])]
        cutoff_close = None
        for bar in bars:
            if bar.date <= cutoff:
                cutoff_close = bar.close
            else:
                break
        base = {
            "leg_key": leg_key,
            "code": row["code"],
            "symbol": row["symbol"],
            "strategy": row["strategy"],
            "source_selection_date_D": row["source_selection_date_D"],
            "monitor_effective_date_E": row["monitor_effective_date_E"],
            "entry_signal_date_T": row["entry_signal_date_T"],
            "shares": int(row["shares"]),
            "buy_total": _dstr(_dec(row["buy_total"])),
            "cutoff_close": _dstr(cutoff_close),
        }
        if cutoff_close is None:
            positions.append(
                {**base, "mark_to_market_net_pnl": None, "note": "HOLD_NO_CUTOFF_CLOSE"}
            )
            continue
        if leg_key in corrected_by_key:
            item = corrected_by_key[leg_key]
            sale = CATHAY_WEB_028_TW_V1.sell(
                cutoff_close, int(row["shares"]), InstrumentType.STOCK
            )
            dividend = _dec(item["cash_dividend"])
            receivable = _dec(item["receivable_shares"]) * cutoff_close
            positions.append(
                {
                    **base,
                    "correction_class": item["correction_class"],
                    "cash_dividend": _dstr(dividend),
                    "receivable_shares": item["receivable_shares"],
                    "receivable_value_at_cutoff": _dstr(receivable),
                    "mark_to_market_net_pnl": _dstr(
                        sale.cash_amount + dividend - _dec(row["buy_total"])
                    ),
                    "mark_to_market_economic_total": _dstr(
                        sale.cash_amount + dividend + receivable - _dec(row["buy_total"])
                    ),
                    "note": "corrected for the corporate action in the holding window",
                }
            )
        elif leg_key in unresolved_by_key:
            item = unresolved_by_key[leg_key]
            positions.append(
                {
                    **base,
                    "correction_class": UNRESOLVED,
                    "reason": item["reason"],
                    "mark_to_market_net_pnl": _dstr(
                        CATHAY_WEB_028_TW_V1.sell(
                            cutoff_close, int(row["shares"]), InstrumentType.STOCK
                        ).cash_amount
                        - _dec(row["buy_total"])
                    ),
                    "note": "CORPORATE_ACTION_UNRESOLVED: untrusted, excluded from readable totals",
                }
            )

        else:
            positions.append(
                {
                    **base,
                    "correction_class": "NOT_AFFECTED_TEMPORARY_REFERENCE",
                    "mark_to_market_net_pnl": _dstr(
                        CATHAY_WEB_028_TW_V1.sell(
                            cutoff_close, int(row["shares"]), InstrumentType.STOCK
                        ).cash_amount
                        - _dec(row["buy_total"])
                    ),
                    "note": "no known official action inside the holding window",
                }
            )

    def _total(items: Sequence[Mapping[str, Any]], field: str) -> str | None:
        values = [_dec(item[field]) for item in items if item.get(field) is not None]
        return None if not values else _dstr(sum(values, Decimal(0)))

    medium = [item for item in positions if item["strategy"] == "MEDIUM_5_21"]
    long = [item for item in positions if item["strategy"] == "LONG_21_144"]
    return {
        "schema_version": h2.OPEN_POSITIONS_SCHEMA_VERSION,
        "cutoff_date": cutoff.isoformat(),
        "cutoff_price_basis": "unadjusted vendor close proxy (2022-12-30 session)",
        "valuation_note": (
            "open legs are marked to the 2022-12-30 close with the frozen "
            "CATHAY_WEB_028_TW_V1 sell commission and stock tax; this is an "
            "unrealized study valuation, not a realized trade.  Legs that cross an "
            "official action also carry the cash dividend and, when the frozen index "
            "proves one, the free-share entitlement"
        ),
        "open_position_count": len(positions),
        "medium_open_count": len(medium),
        "long_open_count": len(long),
        "medium_mark_to_market_net_pnl": _total(medium, "mark_to_market_net_pnl"),
        "long_mark_to_market_net_pnl": _total(long, "mark_to_market_net_pnl"),
        "positions": positions,
    }


def build_summary(
    *,
    zero_rows: Sequence[Mapping[str, str]],
    corrected_legs: Sequence[Mapping[str, Any]],
    unresolved_legs: Sequence[Mapping[str, Any]],
    open_positions: Mapping[str, Any],
) -> dict[str, Any]:
    closed = [row for row in zero_rows if str(row["open_censored"]).lower() != "true"]
    open_rows = [row for row in zero_rows if str(row["open_censored"]).lower() == "true"]
    corrected_by_key = {item["leg_key"]: item for item in corrected_legs}
    unresolved_by_key = {item["leg_key"]: item for item in unresolved_legs}

    strategies: dict[str, dict[str, Any]] = {}
    for row in ordered_strategies(zero_rows):
        strategies[row] = {
            "strategy": row,
            "closed_leg_count": 0,
            "open_leg_count": 0,
            "baseline_closed_net_pnl": "0.0000",
            "unaffected_closed_net_pnl": "0.0000",
            "unaffected_closed_leg_count": 0,
            "corrected_solved_closed_leg_count": 0,
            "corrected_solved_closed_realized_net_pnl": "0.0000",
            "corrected_solved_closed_receivable_value": "0.0000",
            "readable_closed_leg_count": 0,
            "readable_closed_net_pnl": "0.0000",
            "unresolved_closed_count": 0,
            "unresolved_closed_original_net_pnl": "0.0000",
            "censored_after_correction_count": 0,
            "corrected_open_count": 0,
            "year_end_mark_to_market_net_pnl": "0.0000",
            "year_end_receivable_value": "0.0000",
            "descriptive_total_readable": "0.0000",
            "descriptive_total_with_receivables": "0.0000",
        }

    for row in closed:
        strategies[row["strategy"]]["closed_leg_count"] += 1
        strategies[row["strategy"]]["baseline_closed_net_pnl"] = _dstr(
            _dec(strategies[row["strategy"]]["baseline_closed_net_pnl"])
            + _dec(row["costed_net_pnl"] or 0)
        )
    for row in open_rows:
        strategies[row["strategy"]]["open_leg_count"] += 1

    for row in closed:
        leg_key = _leg_key(row)
        block = strategies[row["strategy"]]
        if leg_key in corrected_by_key:
            item = corrected_by_key[leg_key]
            if not item["closed_after_correction"]:
                block["censored_after_correction_count"] += 1
        elif leg_key in unresolved_by_key:
            block["unresolved_closed_count"] += 1
            block["unresolved_closed_original_net_pnl"] = _dstr(
                _dec(block["unresolved_closed_original_net_pnl"])
                + _dec(row["costed_net_pnl"] or 0)
            )
        else:
            block["unaffected_closed_leg_count"] += 1
            block["unaffected_closed_net_pnl"] = _dstr(
                _dec(block["unaffected_closed_net_pnl"])
                + _dec(row["costed_net_pnl"] or 0)
            )

    per_strategy_solved_realized: dict[str, Decimal] = {}
    per_strategy_solved_receivable: dict[str, Decimal] = {}
    per_strategy_solved_closed_count: Counter[str] = Counter()
    for item in corrected_legs:
        if item["open_censored"] or not item["closed_after_correction"]:
            continue
        strategy = item["strategy"]
        per_strategy_solved_realized[strategy] = per_strategy_solved_realized.get(
            strategy, Decimal(0)
        ) + _dec(item["realized_net_pnl"])
        per_strategy_solved_receivable[strategy] = per_strategy_solved_receivable.get(
            strategy, Decimal(0)
        ) + _dec(item["receivable_value_at_exit_close"])
        per_strategy_solved_closed_count[strategy] += 1

    positions = open_positions.get("positions") or []
    year_end: dict[str, Decimal] = {}
    year_end_receivable: dict[str, Decimal] = {}
    for item in positions:
        position = item.get("mark_to_market_net_pnl")
        strategy = item["strategy"]
        if item.get("correction_class") == UNRESOLVED:
            continue
        if position is not None:
            year_end[strategy] = year_end.get(strategy, Decimal(0)) + _dec(position)
        receivable = item.get("receivable_value_at_cutoff")
        if receivable is not None:
            year_end_receivable[strategy] = year_end_receivable.get(
                strategy, Decimal(0)
            ) + _dec(receivable)

    for strategy, block in strategies.items():
        block["corrected_solved_closed_leg_count"] = per_strategy_solved_closed_count[
            strategy
        ]
        block["corrected_solved_closed_realized_net_pnl"] = _dstr(
            per_strategy_solved_realized.get(strategy, Decimal(0))
        )
        block["corrected_solved_closed_receivable_value"] = _dstr(
            per_strategy_solved_receivable.get(strategy, Decimal(0))
        )
        readable = _dec(block["unaffected_closed_net_pnl"]) + per_strategy_solved_realized.get(
            strategy, Decimal(0)
        )
        block["readable_closed_leg_count"] = (
            int(block["unaffected_closed_leg_count"])
            + int(block["corrected_solved_closed_leg_count"])
        )
        block["readable_closed_net_pnl"] = _dstr(readable)
        block["corrected_open_count"] = sum(
            1
            for item in positions
            if item["strategy"] == strategy
            and item.get("correction_class") != UNRESOLVED
        )
        mark = year_end.get(strategy, Decimal(0))
        receivable_year_end = year_end_receivable.get(strategy, Decimal(0))
        block["year_end_mark_to_market_net_pnl"] = _dstr(mark)
        block["year_end_receivable_value"] = _dstr(receivable_year_end)
        block["descriptive_total_readable"] = _dstr(readable + mark)
        block["descriptive_total_with_receivables"] = _dstr(
            readable
            + mark
            + per_strategy_solved_receivable.get(strategy, Decimal(0))
            + receivable_year_end
        )

    combined = _combine(strategies)
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "cutoff_date": AS_OF_CUTOFF.isoformat(),
        "baseline_label": (
            "unadjusted vendor close proxy; the original numbers are kept as the "
            "untrusted baseline and never presented as corrected performance"
        ),
        "receivable_note": (
            "free shares entitled but not proven tradable on the exit day are "
            "listed as a receivable valued at the exit close; they are never added "
            "to the readable realized total"
        ),
        "censored_note": (
            "a corrected leg whose exit signal now falls after the 2022-12-30 window "
            "is censored like any other open leg: it moves to the year-end valuation "
            "and never enters the readable closed total"
        ),
        "strategies": strategies,
        "combined_descriptive_only": combined,
    }


def ordered_strategies(rows: Sequence[Mapping[str, str]]) -> list[str]:
    ordered: list[str] = []
    for row in rows:
        if row["strategy"] not in ordered:
            ordered.append(row["strategy"])
    return ordered


def _combine(strategies: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    labels = {
        "readable_closed_net_pnl": CLOSED_EQUITY_LABEL,
        "unresolved_closed_original_net_pnl": "受影響未解決原始金額",
        "year_end_mark_to_market_net_pnl": "未平倉期末估值損益",
        "descriptive_total_readable": "描述性合計",
    }
    combined: dict[str, Any] = {
        "descriptive_only": True,
        "label": "兩策略交易合計（僅交易描述；非共用資金帳戶）",
    }
    for field, label in labels.items():
        total = sum((_dec(block[field]) for block in strategies.values()), Decimal(0))
        combined[field] = _dstr(total)
        combined[f"{field}_label"] = label
    combined["unresolved_closed_count"] = sum(
        int(block["unresolved_closed_count"]) for block in strategies.values()
    )
    combined["closed_leg_count"] = sum(
        int(block["closed_leg_count"]) for block in strategies.values()
    )
    combined["open_leg_count"] = sum(
        int(block["open_leg_count"]) for block in strategies.values()
    )
    combined["descriptive_total_note"] = (
        "描述性合計只加〔已平倉、含應收現金股利的權益損益＋期末未平倉估值〕，"
        "不含另外列示的應收增配股估值，也不含已被排除的未解交易"
    )
    return combined


# ---------------------------------------------------------------------------
# gates / readback / report
# ---------------------------------------------------------------------------


def _build_gates(
    *,
    rows: Sequence[Mapping[str, str]],
    inventory: Sequence[Mapping[str, Any]],
    grouped: Mapping[str, Sequence[Mapping[str, Any]]],
    corrected_legs: Sequence[Mapping[str, Any]],
    unresolved_legs: Sequence[Mapping[str, Any]],
    baseline_mismatches: Sequence[Mapping[str, Any]],
    entry_not_preserved: Sequence[Mapping[str, Any]],
    focus_case: Mapping[str, Any] | None,
    action_events: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    zero_rows = [row for row in rows if str(row["slippage_bps"]) == "0"]
    closed = [row for row in zero_rows if str(row["open_censored"]).lower() != "true"]
    open_rows = [row for row in zero_rows if str(row["open_censored"]).lower() == "true"]
    affected_closed = sum(1 for item in grouped.values() if not item[0]["open_censored"])
    affected_open = sum(1 for item in grouped.values() if item[0]["open_censored"])
    focus_hit = focus_case is not None and any(
        str(event["event_date"]) == FOCUS_EVENT_DATE for event in focus_case["events"]
    )
    return {
        "zero_bps_leg_count_is_1674": "PASS" if len(zero_rows) == h2_expected_legs() else "FAIL",
        "leg_classification_partitions_affected_set": (
            "PASS"
            if len(corrected_legs) + len(unresolved_legs) == len(grouped)
            else "FAIL"
        ),
        "affected_closed_leg_count_is_129": (
            "PASS" if affected_closed == 129 else "FAIL"
        ),
        "affected_open_leg_count_is_1": "PASS" if affected_open == 1 else "FAIL",
        "closed_leg_count_is_1534": "PASS" if len(closed) == 1534 else "FAIL",
        "open_leg_count_is_140": "PASS" if len(open_rows) == 140 else "FAIL",
        "unadjusted_replay_reproduces_original_run": (
            "PASS" if not baseline_mismatches else "FAIL"
        ),
        "entry_date_D_E_T_preserved_under_rebasing": (
            "PASS" if not entry_not_preserved else "FAIL"
        ),
        "focus_case_2540_2022_09_19_hit": "PASS" if focus_hit else "FAIL",
        "two_markets_present_in_index": (
            "PASS"
            if {str(event["market"]) for event in action_events} == {"TWSE", "TPEX"}
            else "FAIL"
        ),
        "no_2023_or_2024_event_read": (
            "PASS"
            if all(str(event["date"]).startswith(f"{EVENT_YEAR:04d}-") for event in action_events)
            else "FAIL"
        ),
    }


def h2_expected_legs() -> int:
    return 1256 + 418


def _build_readback(
    *, run_dir: Path, manifest: Mapping[str, Any], summary: Mapping[str, Any]
) -> dict[str, Any]:
    recomputed: dict[str, str] = {}
    for name in sorted(manifest["artifact_sha256"]):
        path = run_dir / name
        recomputed[name] = hashlib.sha256(path.read_bytes()).hexdigest().lower()
    sha_ok = recomputed == {
        name: value for name, value in sorted(manifest["artifact_sha256"].items())
    }
    rows = list(csv.DictReader((run_dir / "corpaction_event_inventory.csv").open(encoding="utf-8", newline="")))
    corrected = list(csv.DictReader((run_dir / "corpaction_corrected_legs.csv").open(encoding="utf-8", newline="")))
    unresolved = list(csv.DictReader((run_dir / "corpaction_unresolved_legs.csv").open(encoding="utf-8", newline="")))
    counts = manifest["counts"]
    counts_ok = (
        len(rows) == counts["affected_events"]
        and len(corrected) == counts["solved_legs"]
        and len(unresolved) == counts["unresolved_legs"]
        and len(corrected) + len(unresolved) == counts["affected_legs"]
    )
    return {
        "schema_version": READBACK_SCHEMA_VERSION,
        "status": "PASS" if (sha_ok and counts_ok) else "FAIL",
        "artifact_sha256_recomputed": recomputed,
        "artifact_sha256_match": sha_ok,
        "inventory_row_count": len(rows),
        "corrected_row_count": len(corrected),
        "unresolved_row_count": len(unresolved),
        "counts_reconcile": counts_ok,
        "gates": manifest["gates"],
        "readable_closed_net_pnl": {
            strategy: block["readable_closed_net_pnl"]
            for strategy, block in summary["strategies"].items()
        },
    }


def _render_report(
    *,
    run_id: str,
    checks: Mapping[str, Any],
    inventory: Sequence[Mapping[str, Any]],
    corrected_legs: Sequence[Mapping[str, Any]],
    unresolved_legs: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    open_positions: Mapping[str, Any],
    focus_case: Mapping[str, Any] | None,
    action_event_count: int,
    edition: str = "v1",
    source_manifest: Mapping[str, Any] | None = None,
    source_run_dir: Path | None = None,
) -> str:
    strategies = summary["strategies"]
    combined = summary["combined_descriptive_only"]
    affected_closed = sum(1 for item in inventory if not item["open_censored"])
    affected_open = sum(1 for item in inventory if item["open_censored"])
    stock_only_count = sum(
        1 for item in corrected_legs if _dec(item["receivable_shares"] or 0) > 0
    )
    medium_receivable = strategies["MEDIUM_5_21"]["corrected_solved_closed_receivable_value"]
    long_receivable = strategies["LONG_21_144"]["corrected_solved_closed_receivable_value"]
    closed_total = int(combined["closed_leg_count"])
    unresolved_total = len(unresolved_legs)
    readable_total = closed_total - unresolved_total
    lines: list[str] = []
    lines.append(f"# 2022 H2 除權息與期末持倉報告修正（02 側，{edition}）")
    lines.append("")
    lines.append(
        "本報告只修正已交付的 2022 H2「站回均線＋V 型樞紐」單筆進場回測：把"
        "持有區間跨過官方除權息事件的腿，依凍結資料重新定座標後局部重跑；"
        "不動 01 選股、D/E/T 時序、MA+V 進場條件、T 收盤成交代理、費稅、"
        "出場規則，也沒有重跑整段 H2。research_only=true、"
        "point_in_time_verified=false。"
    )
    lines.append("")
    lines.append(
        f"本版（{edition}）只改報告用詞，數字、交易規則與公司行動分類完全不動；"
        "資料沿用 v1（輸入 SHA 見第十節與 manifest），v1 的報告保留不覆寫。"
        f"要緊的是：{affected_closed + affected_open} 筆受事件影響的腿裡有 "
        f"{unresolved_total} 筆無法在凍結資料內解讀，這 {unresolved_total} 筆未解交易已被排除，"
        f"所以下方「{CLOSED_EQUITY_LABEL}」只是 {closed_total} 筆已平倉中的 "
        f"{readable_total} 筆小計，不可與原始 {closed_total:,} 筆全體基準直接比較，"
        "也不可當成完整的修正後績效。"
    )
    lines.append("")
    lines.append(f"- run_id：{run_id}／module：{MODULE_VERSION}")
    lines.append(
        "- 原始 run `h2-vpivot-single-entry-2022h2-v1` 的 trades.csv 與 "
        "year_end_open_positions.json 保持 byte 不變（SHA 見 manifest）。"
    )
    lines.append(
        f"- 官方事件索引（僅 2022 年）：TWSE {checks[TWSE_EX_RIGHT_NAME]['sha256']}、"
        f"TPEx {checks[TPEX_EX_RIGHT_NAME]['sha256']}，共 {action_event_count} 筆事件。"
    )
    lines.append("")
    lines.append("## 一、盤點結果")
    lines.append("")
    lines.append(
        f"0 bps 主基準共 {int(combined['closed_leg_count']) + int(combined['open_leg_count'])} 筆成交腿"
        f"（已平倉 {combined['closed_leg_count']}、未平倉 {combined['open_leg_count']}）。"
        f"其中持有區間 (T 進場日, 出場日] 或 (T, 2022-12-30] 內命中官方除權息事件者 "
        f"共 {affected_closed + affected_open} 筆（已平倉 {affected_closed} 筆、未平倉 {affected_open} 筆）。"
        "愛山林 2540 的 2022-09-19「權息」事件命中，列為個案第六節。"
    )
    lines.append("")
    lines.append(
        f"可局部重算 {len(corrected_legs)} 筆，其中 {stock_only_count} 筆配股權益僅估值；"
        f"標記 CORPORATE_ACTION_UNRESOLVED、自「可解讀的已平倉績效」排除 {unresolved_total} 筆"
        "（皆為已平倉）。這 71 筆未解交易已被排除，不進任何已平倉權益小計。"
    )
    lines.append("")
    lines.append("## 二、主績效表（中期 5/21 與長期 21/144 分列）")
    lines.append("")
    header = (
        f"| 策略 | 已平倉筆數 | 原始基準已平倉損益(元) | {CLOSED_EQUITY_LABEL}(元) | "
        "其中：應收增配股估值(元) | 受影響未解筆數 | 未解原始金額(元) | 未平倉筆數 | "
        "期末估值損益(元) | 描述性合計(元) |"
    )
    lines.append(header)
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for name in ("MEDIUM_5_21", "LONG_21_144"):
        block = strategies[name]
        lines.append(
            "| {label} | {closed} | {base} | {readable} | {recv} | {ucount} | {uamt} | "
            "{open} | {mark} | {total} |".format(
                label=("中期 MEDIUM_5_21" if name == "MEDIUM_5_21" else "長期 LONG_21_144"),
                closed=block["closed_leg_count"],
                base=block["baseline_closed_net_pnl"],
                readable=block["readable_closed_net_pnl"],
                recv=block["corrected_solved_closed_receivable_value"],
                ucount=block["unresolved_closed_count"],
                uamt=block["unresolved_closed_original_net_pnl"],
                open=block["corrected_open_count"],
                mark=block["year_end_mark_to_market_net_pnl"],
                total=block["descriptive_total_readable"],
            )
        )
    lines.append(
        "| 兩策略交易合計（僅交易描述，非共用資金帳戶） | {closed} | - | {readable} | - | "
        "{ucount} | {uamt} | {open} | {mark} | {total} |".format(
            closed=combined["closed_leg_count"],
            readable=combined["readable_closed_net_pnl"],
            ucount=combined["unresolved_closed_count"],
            uamt=combined["unresolved_closed_original_net_pnl"],
            open=combined["open_leg_count"],
            mark=combined["year_end_mark_to_market_net_pnl"],
            total=combined["descriptive_total_readable"],
        )
    )
    lines.append("")
    lines.append(
        f"表下註：`描述性合計` = `{CLOSED_EQUITY_LABEL}` ＋ `期末估值損益`；"
        f"不含另外列示的應收增配股估值（中期 {medium_receivable}／長期 {long_receivable} 元），"
        f"不是全部經濟結果。71 筆未解交易已被排除，可解讀小計不可與原始 "
        f"{closed_total:,} 筆全體基準直接比較。"
    )
    lines.append("")
    lines.append(
        "讀法：`原始基準已平倉損益` 是原 run 的未修正數字，只保留為「未修正基準」，"
        "不可當作跨事件交易的經濟損益。"
        f"`{CLOSED_EQUITY_LABEL}` = 未受事件影響的已平倉腿原始損益＋已局部重算且資料可解讀的"
        "受影響腿的已平倉、含應收現金股利的權益損益；它已排除 71 筆未解交易，"
        f"只是 {closed_total} 筆已平倉中的 {readable_total} 筆小計，"
        f"不可與原始 {closed_total:,} 筆全體基準直接比較，也不可當成完整的修正後績效。"
        "`應收增配股估值` 是權益已發生、但無法證明在出場當日已可交付或可賣出的增配股，"
        f"按期初（出場日或期末）收盤價另列估值（中期 {medium_receivable} 元、長期 {long_receivable} 元）；"
        "它不是已實現，也沒有計入描述性合計。"
        "描述性合計只加〔已平倉、含應收現金股利的權益損益 ＋ 期末未平倉估值〕，"
        "不含另外列示的應收增配股估值，不可當作全部經濟結果。"
        "兩策略各自獨立，合計列只是交易描述性相加，不是投組或帳戶報酬；"
        "選股組別是多標籤揭露，也不可相加。"
    )
    lines.append("")
    lines.append("## 三、逐筆事件清單與處置")
    lines.append("")
    lines.append(
        "完整逐筆清單見 `corpaction_event_inventory.csv`（每筆事件一列，含事件日期、代號、"
        "市場、種類、來源 SHA 與受影響交易鍵）；可局部重算的腿見 `corpaction_corrected_legs.csv`，"
        "未解腿見 `corpaction_unresolved_legs.csv`。"
    )
    lines.append("")
    lines.append("### 未解決原因分布")
    lines.append("")
    reason_counts = Counter(str(item["reason"]) for item in unresolved_legs)
    lines.append("| 原因 | 筆數 | 說明 |")
    lines.append("|---|---|---|")
    explanations = {
        UNRESOLVED_WEIGHT_SPLIT: (
            "TWSE 官方除權息計算表只有「權值+息值」合計，凍結索引無法拆出現金股利與配股率"
            "（或其中是否含現金增資），無法精確重算股數與權益"
        ),
        UNRESOLVED_RIGHTS_ISSUE: (
            "TPEx 列示現金增資，但每持有股可認購比率不在凍結索引內，無法精確重算股數與權益"
        ),
        UNRESOLVED_EXIT_OUTSIDE_WINDOW: (
            "還原座標後該腿在凍結 episode 視窗最後一個交易日仍未出場，修正後出場日落在凍結資料"
            "範圍之外；不延伸視窗、不外推，標為未解並排除於可解讀合計"
        ),
    }
    for reason, count in sorted(reason_counts.items()):
        lines.append(f"| {reason} | {count} | {explanations.get(reason, '')} |")
    lines.append("")
    lines.append("## 四、局部修正方法")
    lines.append("")
    lines.append(
        "對每一個可解讀的事件，把該標的的日 K 從除權息日起改成與事件前同座標的價值序列"
        "A = (1+s)·R + D（R 為未還原收盤／開高低，D 為每股現金股利，s 為每股無償配股率）。"
        "事件前的 K 棒完全不動，因此 D 選股日、E 監控生效日、T 進場日、進場收盤代理與股數"
        "（999 股／10,000 元名目、每腿初始 R ≤ 2,000 元）都由建構方式保持不變；"
        "事件後的價格、快／慢線、V 樞紐與道氏防線則自動落在同一座標，不會再把價位斷層"
        "誤判成跌破結構防線。之後用同一顆凍結引擎（SingleEntryNoAddEngine、"
        "ENTRY_RECLAIM_V_PIVOT_CONFIRMED、EXIT_DOW_TRAIL、0 bps）針對該事件重跑一次，"
        "取得修正後的出場日、出場原因與出場收盤。"
    )
    lines.append("")
    lines.append(
        "損益一律按可驗證的現金流量計算：只賣出在出場日可以賣的原始股數（以未還原收盤成交，"
        "CATHAY_WEB_028_TW_V1 手續費與證交稅不變），加上每股現金股利 × 持股數"
        "（應收股利在除權息日尚未確定已入帳）；配發但無法證明已可交易的增配股列為應收，"
        "按出場日（未平倉者為 2022-12-30）收盤價估值另列，不當天賣出、也不併入已實現。"
    )
    lines.append("")
    lines.append("## 五、修正前後比較（可解讀腿）")
    lines.append("")
    lines.append(
        f"| 代號 | 策略 | D | E | T | 原出場 | 原損益(元) | 修正出場 | {CLOSED_EQUITY_LABEL}(元) | 應收增配股 | 差額(元) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for item in corrected_legs:
        lines.append(
            "| {code} | {strategy} | {D} | {E} | {T} | {ox} {orx} | {op} | {cx} {cr} | {rp} | "
            "{recv} | {delta} |".format(
                code=item["code"],
                strategy=item["strategy"],
                D=item["source_selection_date_D"],
                E=item["monitor_effective_date_E"],
                T=item["entry_signal_date_T"],
                ox=item["original_exit_date"] or "未平倉",
                orx=item["original_exit_reason"] or "",
                op=item["original_costed_net_pnl"],
                cx=item["corrected_exit_date"],
                cr=item["corrected_exit_reason"] or "",
                rp=item["realized_net_pnl"],
                recv=item["receivable_shares"],
                delta=item["delta_vs_original"],
            )
        )
    lines.append("")
    lines.append("## 六、愛山林 2540 個案")
    lines.append("")
    if focus_case is None:
        lines.append("HOLD：2540 個案未在本次盤點命中，需回報 00。")
    else:
        original = focus_case
        sale_net = _dstr(
            _dec(original["realized_net_pnl"])
            - _dec(original["cash_dividend"])
            + _dec(original["buy_total"])
        )
        economic_sign = "+" if _dec(original["economic_total"]) > 0 else ""
        lines.append(
            "| 階段 | 日期 | 內容 |\n|---|---|---|\n"
            f"| D 選股 | {original['source_selection_date_D']} | 5/21 中期策略啟動監控名單 |\n"
            f"| E 監控生效 | {original['monitor_effective_date_E']} | 快線 > 慢線，開始追蹤拉回與樞紐 |\n"
            f"| T 進場 | {original['entry_signal_date_T']} | 收盤站回快線且 V 樞紐已確認，以當日收盤成交 |\n"
            f"| 除權息 | {original['events'][0]['event_date']} | {original['events'][0]['event_kind']}：每股現金 "
            f"{original['events'][0]['cash_per_share']} 元、每股配股 {original['events'][0]['stock_per_share']} 股 |\n"
            f"| 剩餘期間 | {'未平倉' if original['open_censored'] else original['corrected_exit_date']} | "
            f"修正後出場 {original['corrected_exit_date']}（{original['corrected_exit_reason']}）|"
        )
        lines.append("")
        lines.append(
            f"- 原始 run（未還原基準，保留呈現）：{original['original_exit_date']} 以"
            f"「{original['original_exit_reason']}」出場，成本後損益 {original['original_costed_net_pnl']} 元。"
            "原 run 用未還原價格，事件當日由 150.00 直落 80.00（除權息參考價 88.78），"
            "停損是以事件前座標的防線去比事件後的未還原價，所以這個數字不可直接當成跨事件交易的經濟損益。"
        )
        lines.append(
            f"- 修正後出場：事件日同一座標的每股價值為 (1+0.65)×80.00 + 3.50 = 135.50 元，仍低於已上移的"
            f"道氏防線 {original['corrected_effective_stop']} 元，所以修正後出場日仍是 "
            f"{original['corrected_exit_date']}（{original['corrected_exit_reason']}）、以未還原收盤 "
            f"{original['corrected_raw_exit_close']} 元賣出可賣的 80 股；要修正的是經濟結果，不是出場日。"
        )
        lines.append(
            f"- 現金流拆解：賣股現金淨額 {sale_net} 元、買入 {original['buy_total']} 元、"
            f"應收現金股利 {original['cash_dividend']} 元（每股 3.5 元 × 80 股）、"
            f"應收配股 {original['receivable_shares']} 股按當日（{original['corrected_exit_date']}）"
            f"未還原收盤 {original['corrected_raw_exit_close']} 元估 {original['receivable_value_at_exit_close']} 元。"
        )
        lines.append(
            f"- {CLOSED_EQUITY_LABEL} = 賣股現金淨額 {sale_net} ＋ 應收現金股利 "
            f"{original['cash_dividend']} − 買入 {original['buy_total']} = {original['realized_net_pnl']} 元。"
            "這是已平倉、含應收現金股利的權益損益，不是當日入帳的現金：應收股利在除權息日尚未確定已入帳。"
        )
        lines.append(
            f"- 描述性經濟價值 {economic_sign}{original['economic_total']} 元（= 上述已平倉權益損益 "
            f"{original['realized_net_pnl']} ＋ 應收配股估值 {original['receivable_value_at_exit_close']}）；"
            "這是描述性經濟價值，不是當日入帳獲利；應收配股的交付日與可賣日未經證實，估值不是已實現。"
        )
        lines.append(
            f"- 與原始未還原基準 {original['original_costed_net_pnl']} 元的差額：已平倉權益損益基礎 "
            f"{original['delta_vs_original']} 元、經濟價值基礎 {original['delta_economic_vs_original']} 元。"
        )
    lines.append("")
    lines.append("## 七、2022-12-30 未平倉期末估值（另列，不當已實現）")
    lines.append("")
    lines.append(
        f"- 未平倉筆數：{open_positions['open_position_count']}（中期 {open_positions['medium_open_count']}、"
        f"長期 {open_positions['long_open_count']}）"
    )
    lines.append(
        f"- 期末估值損益：中期 {open_positions['medium_mark_to_market_net_pnl']} 元、"
        f"長期 {open_positions['long_mark_to_market_net_pnl']} 元（假設 2022-12-30 收盤賣出並扣估計賣出費稅，"
        "並非已實現損益）"
    )
    affected_open_items = [
        item
        for item in open_positions["positions"]
        if str(item.get("correction_class", "")).startswith("CORRECTED")
        or item.get("correction_class") == UNRESOLVED
    ]
    lines.append("")
    lines.append("| 代號 | 策略 | T | 股數 | 期末收盤 | 期末估值損益(元) | 處置 |")
    lines.append("|---|---|---|---|---|---|---|")
    for item in affected_open_items:
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                item["code"],
                item["strategy"],
                item["entry_signal_date_T"],
                item["shares"],
                item.get("cutoff_close"),
                item.get("mark_to_market_net_pnl"),
                item.get("correction_class"),
            )
        )
    lines.append("")
    lines.append(
        "期末估值與未平倉腿的合併方式：受事件影響且可解讀的未平倉腿，估值改用"
        "「期末收盤賣出可賣股數 + 應收現金股利 − 買進成本」，增配股另列應收；"
        "未解者標 CORPORATE_ACTION_UNRESOLVED 並排除於可解讀合計。"
    )
    lines.append("")
    lines.append("## 八、邊界與限制")
    lines.append("")
    lines.append(
        "- 只讀 twse-ex-right.json 與 tpex-ex-right.json 兩個官方索引，且只看 2022 年；"
        "沒有讀 2023／2024 的價格或績效。"
    )
    lines.append(
        "- 未還原的供應商收盤代理仍是價格來源；本輪不引入新的價格服務，也不改任何交易規則。"
    )
    lines.append(
        "- 無償配股的畸零股（不足 1 股）處理慣例不在凍結資料內；應收增配股以精確比率列示，"
        "與發行公司實際折現方式最多相差 1 股的價值。"
    )
    lines.append(
        "- 只有 2540 的現金／配股拆解（每股現金 3.5 元、配股 0.65 股）來自凍結規格引用的"
        "公開資訊觀測站公告；其餘 TWSE 權／權息事件無法拆解，一律標記未解。"
    )
    lines.append(
        "- 本輪不建立共用資金帳戶、部位衝突或先後順序規則，兩策略各自獨立；"
        "合計列只是交易描述。"
    )
    lines.append(
        "- 每個 episode 的交易視窗（regime_window_end）是原 run 用未還原價格算出的均線趨勢終止日；"
        "還原座標後有 47 筆腿在該凍結視窗最後一個交易日仍屬持有，修正後出場日落在凍結資料範圍之外，"
        "因此標為 CORPORATE_ACTION_UNRESOLVED：不延伸視窗、不外推、也不自行改 episode 規則，"
        "這些腿的原始金額另列為不可信基準。"
    )
    lines.append(
        "- 本版（報告用詞修正版）只改文字，數字、交易規則與公司行動分類與 v1 完全相同；"
        "v1 的 report_zh.md 與各資料產物保持 byte 不變（SHA 見第十節與 manifest）。"
    )
    lines.append(
        f"- 受事件影響的 {unresolved_total} 筆未解交易已被排除在可解讀小計之外；"
        f"可解讀小計（{readable_total} 筆）不可與原始 {closed_total:,} 筆全體基準直接比較，"
        "也不可當成完整的修正後績效。"
    )
    lines.append(
        f"- {len(corrected_legs)} 筆可局部重算的腿中有 {stock_only_count} 筆配股權益只有估值"
        "（2540、3218、5603）：配股交付日與可賣日未經證實，估值不是已實現，"
        "也不計入已平倉權益損益。"
    )
    lines.append("")
    lines.append("## 九、檔案與可重跑")
    lines.append("")
    lines.append(
        "- `corpaction_event_index_2022.json`：官方 2022 年事件索引（含來源 SHA）。\n"
        "- `corpaction_event_inventory.csv`：逐筆受影響交易與事件。\n"
        "- `corpaction_corrected_legs.csv` / `corpaction_unresolved_legs.csv`：修正與未解。\n"
        "- `corpaction_2540_case.json`：愛山林個案完整時序。\n"
        "- `corpaction_summary.json`、`year_end_open_positions_corrected.json`。\n"
        "- `manifest.json`、`readback.json`：輸入 SHA、gates 與產物 SHA-256。"
    )
    lines.append("")
    if source_manifest is not None:
        lines.append("## 十、版本、輸入 SHA 與最新版入口")
        lines.append("")
        source_run_id = source_manifest.get("run_id")
        lines.append(
            f"- 本檔是給使用者看的最新版：同目錄的 `{REPORT_ONLY_LATEST_ENTRY}` 指向本檔。"
        )
        lines.append(
            f"- 資料來源 run：`{source_run_id}`"
            + (f"（{source_run_dir}）" if source_run_dir is not None else "")
            + "；v1 保留不覆寫，僅供追溯。"
        )
        for name, digest in sorted((source_manifest.get("artifact_sha256") or {}).items()):
            lines.append(f"- 輸入產物 SHA-256：{name} = {str(digest).lower()}")
        lines.append("")
    lines.append(
        f"run_id：{run_id}／module：{MODULE_VERSION}／run 狀態：PASS（以 manifest 為準）"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# report-only re-render (display wording only, never re-runs the engine)
# ---------------------------------------------------------------------------


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def load_report_state(source_run_dir: str | Path) -> dict[str, Any]:
    """Load the frozen v1 artifacts needed to re-render the report text."""

    source_run_dir = Path(source_run_dir)
    inventory = _read_csv_rows(source_run_dir / "corpaction_event_inventory.csv")
    for item in inventory:
        item["open_censored"] = _as_bool(item.get("open_censored"))
    case = _read_json(source_run_dir / "corpaction_2540_case.json")
    index = _read_json(source_run_dir / "corpaction_event_index_2022.json")
    return {
        "manifest": _read_json(source_run_dir / "manifest.json"),
        "summary": _read_json(source_run_dir / "corpaction_summary.json"),
        "open_positions": _read_json(
            source_run_dir / "year_end_open_positions_corrected.json"
        ),
        "focus_case": case.get("case"),
        "action_event_count": int(index.get("event_count", 0)),
        "inventory": inventory,
        "corrected_legs": _read_csv_rows(source_run_dir / "corpaction_corrected_legs.csv"),
        "unresolved_legs": _read_csv_rows(
            source_run_dir / "corpaction_unresolved_legs.csv"
        ),
    }


def corrected_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Return the summary with corrected display labels and identical numbers."""

    relabeled = json.loads(json.dumps(summary, ensure_ascii=False))
    combined = relabeled.get("combined_descriptive_only")
    if isinstance(combined, dict):
        combined["readable_closed_net_pnl_label"] = CLOSED_EQUITY_LABEL
        combined["descriptive_total_note"] = (
            "描述性合計只加〔已平倉、含應收現金股利的權益損益＋期末未平倉估值〕，"
            "不含另外列示的應收增配股估值，也不含已被排除的未解交易"
        )
    return relabeled


def _scrub_display_labels(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _scrub_display_labels(item)
            for key, item in value.items()
            if not str(key).endswith("_label") and str(key) != "descriptive_total_note"
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_display_labels(item) for item in value]
    return value


def summary_numbers_unchanged(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> bool:
    """Display labels may move; every other value must stay byte-equal."""

    return _scrub_display_labels(before) == _scrub_display_labels(after)


def _latest_entry_bytes(*, run_id: str, source_run_id: Any) -> bytes:
    return (
        "# 最新版入口（使用者請看這份）\n\n"
        "- 最新版報告：`report_zh.md`（同目錄）\n"
        f"- run_id：{run_id}\n"
        f"- 資料來源：`{source_run_id}`（v1，保留不覆寫）\n"
        "- 差異：本版只修報告用詞；數字、交易規則與公司行動分類完全未變\n"
        "- 輸入產物 SHA-256 與 gates 見同目錄 manifest.json / readback.json\n"
    ).encode("utf-8")


def run_report_only(
    *,
    source_run_dir: str | Path,
    output_root: str | Path,
    run_id: str,
    runner_commit: str = "UNCOMMITTED",
) -> dict[str, Any]:
    """Re-render the report wording into a new run id, changing nothing else."""

    source_run_dir = Path(source_run_dir)
    run_dir = Path(output_root) / run_id
    if run_dir.exists():
        raise H2CorpActionError(f"refusing to overwrite an existing run directory: {run_dir}")

    state = load_report_state(source_run_dir)
    source_manifest = state["manifest"]
    summary = corrected_summary(state["summary"])

    source_shas: dict[str, dict[str, Any]] = {}
    sha_match = True
    for name, expected in sorted((source_manifest.get("artifact_sha256") or {}).items()):
        actual = _sha256_file(source_run_dir / name).lower()
        match = actual == str(expected).lower()
        sha_match = sha_match and match
        source_shas[name] = {
            "expected": str(expected).lower(),
            "actual": actual,
            "match": match,
        }

    original_unchanged = (
        source_manifest.get("original_run_sha256_unchanged", {}).get("trades.csv")
        == ORIGINAL_TRADES_SHA256
        and source_manifest.get("original_run_sha256_unchanged", {}).get(
            "year_end_open_positions.json"
        )
        == ORIGINAL_OPEN_POSITIONS_SHA256
    )

    report = _render_report(
        run_id=run_id,
        checks=source_manifest.get("inputs", {}),
        inventory=state["inventory"],
        corrected_legs=state["corrected_legs"],
        unresolved_legs=state["unresolved_legs"],
        summary=summary,
        open_positions=state["open_positions"],
        focus_case=state["focus_case"],
        action_event_count=state["action_event_count"],
        edition=REPORT_ONLY_EDITION,
        source_manifest=source_manifest,
        source_run_dir=source_run_dir,
    )
    forbidden = [token for token in FORBIDDEN_REPORT_TOKENS if token in report]
    missing = [token for token in REQUIRED_REPORT_TOKENS if token not in report]

    artifacts: dict[str, bytes] = {}
    for name in REPORT_ONLY_COPY_ARTIFACTS:
        artifacts[name] = (source_run_dir / name).read_bytes()
    artifacts["corpaction_summary.json"] = _json_bytes(summary)
    artifacts["report_zh.md"] = report.encode("utf-8")
    artifacts[REPORT_ONLY_LATEST_ENTRY] = _latest_entry_bytes(
        run_id=run_id, source_run_id=source_manifest.get("run_id")
    )

    gates = {
        "source_artifact_sha256_match": "PASS" if sha_match else "FAIL",
        "summary_numbers_unchanged": (
            "PASS" if summary_numbers_unchanged(state["summary"], summary) else "FAIL"
        ),
        "report_no_retired_wording": "PASS" if not forbidden else "FAIL",
        "report_required_wording_present": "PASS" if not missing else "FAIL",
        "no_engine_rerun": "PASS",
        "original_run_sha256_unchanged": "PASS" if original_unchanged else "FAIL",
    }

    run_dir.mkdir(parents=True, exist_ok=False)
    for name, payload in artifacts.items():
        (run_dir / name).write_bytes(payload)

    manifest = {
        "schema_version": REPORT_ONLY_MANIFEST_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "run_kind": "report-only",
        "status": "PASS" if all(value == "PASS" for value in gates.values()) else "FAIL",
        "runner_commit": runner_commit,
        "research_only": True,
        "formal_historical_performance": False,
        "point_in_time_verified": False,
        "source_run_id": source_manifest.get("run_id"),
        "source_run_dir": str(source_run_dir),
        "source_manifest_sha256": _sha256_file(source_run_dir / "manifest.json").lower(),
        "source_readback_sha256": _sha256_file(source_run_dir / "readback.json").lower(),
        "source_artifacts": source_shas,
        "counts": source_manifest.get("counts"),
        "gates": gates,
        "artifact_sha256": {
            name: hashlib.sha256(payload).hexdigest().lower()
            for name, payload in sorted(artifacts.items())
        },
        "latest_entry": REPORT_ONLY_LATEST_ENTRY,
        "note": (
            "Display-only re-render: the frozen v1 data artifacts are copied "
            "byte-for-byte and only the report wording and summary display labels "
            "are rewritten."
        ),
    }
    recomputed = {
        name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest().lower()
        for name in sorted(manifest["artifact_sha256"])
    }
    readback = {
        "schema_version": REPORT_ONLY_READBACK_SCHEMA_VERSION,
        "status": "PASS",
        "artifact_sha256_recomputed": recomputed,
        "artifact_sha256_match": recomputed == manifest["artifact_sha256"],
        "source_artifact_sha256_match": sha_match,
        "summary_numbers_unchanged": summary_numbers_unchanged(state["summary"], summary),
        "report_no_retired_wording": not forbidden,
        "forbidden_tokens_found": forbidden,
        "required_tokens_missing": missing,
        "copied_artifacts": list(REPORT_ONLY_COPY_ARTIFACTS),
        "latest_entry": REPORT_ONLY_LATEST_ENTRY,
        "gates": gates,
    }
    readback["status"] = (
        "PASS"
        if (readback["artifact_sha256_match"] and all(v == "PASS" for v in gates.values()))
        else "FAIL"
    )
    manifest["readback_status"] = readback["status"]
    (run_dir / "manifest.json").write_bytes(_json_bytes(manifest))
    (run_dir / "readback.json").write_bytes(_json_bytes(readback))
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdata-root", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--run-id", default=RUN_ID_DEFAULT)
    parser.add_argument("--runner-commit", default="UNCOMMITTED")
    parser.add_argument(
        "--report-only-from",
        type=Path,
        default=None,
        help=(
            "re-render the report from an existing v1 run directory; copies the frozen "
            "artifacts and rewrites only the wording, never re-running the engine"
        ),
    )
    args = parser.parse_args(argv)
    if args.report_only_from is not None:
        output_root = (
            args.output_root
            if args.output_root is not None
            else args.report_only_from.parent
        )
        manifest = run_report_only(
            source_run_dir=args.report_only_from,
            output_root=output_root,
            run_id=args.run_id,
            runner_commit=args.runner_commit,
        )
    else:
        if args.workdata_root is None:
            parser.error(
                "--workdata-root is required unless --report-only-from is given"
            )
        manifest = run_corpaction_correction(
            workdata_root=args.workdata_root,
            output_root=args.output_root,
            run_id=args.run_id,
            runner_commit=args.runner_commit,
            progress=_progress_printer,
        )
    print(
        "run {r} status={s} affected={a} solved={c} unresolved={u}".format(
            r=manifest["run_id"],
            s=manifest["status"],
            a=manifest["counts"]["affected_legs"],
            c=manifest["counts"]["solved_legs"],
            u=manifest["counts"]["unresolved_legs"],
        )
    )
    return 0 if manifest["status"] == "PASS" else 1


def _progress_printer(stage: str, done: int, total: int, extra: Mapping[str, Any]) -> None:
    print(f"[{stage}] {done}/{total} {dict(extra)}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
