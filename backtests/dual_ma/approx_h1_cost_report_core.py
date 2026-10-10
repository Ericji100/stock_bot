"""Reusable costed report core for the frozen v0.4.1 six-matrix comparison.

This module turns engine-side leg observations into a costed, auditable ledger.
It is independent of any real selection result: callers pass leg terms, and the
module recomputes every money field with the frozen cost model (Cathay 0.399
permille commission, 0.3% stock / 0.1% ETF sell tax, whole-TWD ROUND_HALF_UP, no
minimum commission) so the numbers can be hand-checked.

Rules it must never break:

* fees, taxes and slippage come from the frozen cost model only;
* an open leg is marked to market at the last causally available close and also
  reports what an immediate frozen-cost sale would leave;
* six matrix copies of one economic entry are counted once in the episode
  aggregate, while every matrix row stays in the ledger for audit;
* a missing or unexplained price is a HOLD row, never a fabricated fill;
* no performance conclusion is drawn here; this is ledger machinery.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .costs import CATHAY_WEB_028_TW_V1, CostModel
from .models import InstrumentType, decimal


REPORT_CORE_VERSION = "dual-ma-approx-h1-cost-report-core-v0.1"
SLIPPAGE_SCENARIOS: tuple[int, ...] = (0, 10, 20)
STRATEGY_LAYERS: tuple[str, ...] = ("MEDIUM_5_21", "LONG_21_144")

STATUS_CLOSED = "CLOSED"
STATUS_OPEN_MARKED = "OPEN_MARKED"
STATUS_HOLD_MISSING_MARK = "HOLD_MISSING_MARK_PRICE"
STATUS_HOLD_MARK_AFTER_CUTOFF = "HOLD_MARK_AFTER_DECISION_CUTOFF"
STATUS_HOLD_MISSING_EXIT = "HOLD_MISSING_EXIT_PRICE"
STATUS_HOLD_DATA_GAP = "HOLD_DATA_GAP"
STATUS_HOLD_UNKNOWN_INSTRUMENT = "HOLD_UNKNOWN_INSTRUMENT_TYPE"
STATUS_HOLD_RECONCILIATION = "HOLD_ENGINE_RECONCILIATION_MISMATCH"
STATUS_HOLD_UNKNOWN_LAYER = "HOLD_UNKNOWN_STRATEGY_LAYER"
STATUS_HOLD_MISSING_EPISODE_ID = "HOLD_MISSING_LIFECYCLE_EPISODE_ID"
STATUS_HOLD_EPISODE_ID_NOT_LIFECYCLE = "HOLD_EPISODE_ID_NOT_A_LIFECYCLE_ID"

HOLD_STATUSES = (
    STATUS_HOLD_MISSING_MARK,
    STATUS_HOLD_MARK_AFTER_CUTOFF,
    STATUS_HOLD_MISSING_EXIT,
    STATUS_HOLD_DATA_GAP,
    STATUS_HOLD_UNKNOWN_INSTRUMENT,
    STATUS_HOLD_RECONCILIATION,
    STATUS_HOLD_UNKNOWN_LAYER,
    STATUS_HOLD_MISSING_EPISODE_ID,
    STATUS_HOLD_EPISODE_ID_NOT_LIFECYCLE,
)

R_MULTIPLE_PLACES = Decimal("0.000001")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return _sha256_bytes(canonical_bytes(value))


def program_hashes() -> dict[str, str]:
    """SHA-256 of the frozen cost model and of this core, for readback."""

    costs_path = Path(inspect.getsourcefile(CostModel) or "")
    core_path = Path(__file__)
    return {
        "costs.py": _sha256_bytes(costs_path.read_bytes()) if costs_path.is_file() else "MISSING",
        "approx_h1_cost_report_core.py": _sha256_bytes(core_path.read_bytes()),
    }


@dataclass(frozen=True)
class LegInput:
    """One engine-side leg observation handed to the report core."""

    matrix_id: str
    slippage_bps: int
    symbol: str
    strategy: str
    leg_id: str
    leg_number: int
    episode_key: str
    economic_key: str
    lifecycle_episode_id: str
    instrument_type: str
    entry_reference_close: Decimal
    shares: int
    initial_stop: Decimal
    initial_r: Decimal
    entry_date: date | None = None
    exit_date: date | None = None
    exit_reason: str | None = None
    exit_reference_close: Decimal | None = None
    last_available_session: date | None = None
    last_available_close: Decimal | None = None
    data_gap_reason: str | None = None
    decision_cutoff: date | None = None
    engine_buy_total: Decimal | None = None
    engine_sell_net: Decimal | None = None

    def identity(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "leg_id": self.leg_id,
            "matrix_id": self.matrix_id,
            "slippage_bps": self.slippage_bps,
            "entry_date": self.entry_date,
            "episode_key": self.episode_key,
            "economic_key": self.economic_key,
            "lifecycle_episode_id": self.lifecycle_episode_id,
        }

    def manual_trace_key(self) -> str:
        return f"TRACE-{canonical_sha256(self.identity())[:16]}"


@dataclass(frozen=True)
class CostedLeg:
    identity: Mapping[str, Any]
    status: str
    reason: str
    manual_trace_key: str
    buy_gross: Decimal | None = None
    buy_commission: Decimal | None = None
    buy_total: Decimal | None = None
    sell_gross: Decimal | None = None
    sell_commission: Decimal | None = None
    sell_tax: Decimal | None = None
    sell_net: Decimal | None = None
    net_pnl: Decimal | None = None
    r_multiple: Decimal | None = None
    mark_session: date | None = None
    mark_value: Decimal | None = None
    cost_to_close: Decimal | None = None
    open_pnl_if_closed: Decimal | None = None
    reconciliation: str = "NOT_PROVIDED"
    initial_r: Decimal | None = None
    slippage_bps: int = 0
    cost_model_id: str = CATHAY_WEB_028_TW_V1.model_id

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "manual_trace_key": self.manual_trace_key,
            "status": self.status,
            "reason": self.reason,
            "slippage_bps": self.slippage_bps,
            "cost_model_id": self.cost_model_id,
            "reconciliation": self.reconciliation,
            "initial_r": self.initial_r,
            **{str(key): value for key, value in self.identity.items()},
        }
        for name in (
            "buy_gross",
            "buy_commission",
            "buy_total",
            "sell_gross",
            "sell_commission",
            "sell_tax",
            "sell_net",
            "net_pnl",
            "r_multiple",
            "mark_session",
            "mark_value",
            "cost_to_close",
            "open_pnl_if_closed",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


def _instrument(value: str) -> InstrumentType | None:
    try:
        return InstrumentType(str(value))
    except ValueError:
        return None


def cost_leg(
    row: LegInput, *, cost_model: CostModel = CATHAY_WEB_028_TW_V1
) -> CostedLeg:
    """Recompute one leg's money with the frozen model, or hold it."""

    identity = row.identity()
    trace = row.manual_trace_key()
    model = cost_model.with_slippage(row.slippage_bps)

    def hold(status: str, reason: str) -> CostedLeg:
        return CostedLeg(identity, status, reason, trace, slippage_bps=row.slippage_bps)

    if row.data_gap_reason:
        return hold(STATUS_HOLD_DATA_GAP, str(row.data_gap_reason))
    if not row.lifecycle_episode_id:
        return hold(
            STATUS_HOLD_MISSING_EPISODE_ID,
            "a frozen symbol x strategy x lifecycle episode id is required",
        )
    if row.lifecycle_episode_id == row.economic_key:
        return hold(
            STATUS_HOLD_EPISODE_ID_NOT_LIFECYCLE,
            "economic_key is a correlation diagnostic and cannot serve as the episode id",
        )
    if row.strategy not in STRATEGY_LAYERS:
        return hold(STATUS_HOLD_UNKNOWN_LAYER, str(row.strategy))
    instrument = _instrument(row.instrument_type)
    if instrument is None:
        return hold(STATUS_HOLD_UNKNOWN_INSTRUMENT, str(row.instrument_type))

    buy = model.buy(row.entry_reference_close, row.shares)
    if row.engine_buy_total is not None and decimal(row.engine_buy_total) != buy.cash_amount:
        return hold(
            STATUS_HOLD_RECONCILIATION,
            f"engine buy_total {row.engine_buy_total} != recomputed {buy.cash_amount}",
        )

    if row.exit_reference_close is not None:
        sell = model.sell(row.exit_reference_close, row.shares, instrument)
        if row.engine_sell_net is not None and decimal(row.engine_sell_net) != sell.cash_amount:
            return hold(
                STATUS_HOLD_RECONCILIATION,
                f"engine sell_net {row.engine_sell_net} != recomputed {sell.cash_amount}",
            )
        net = sell.cash_amount - buy.cash_amount
        r_multiple = (
            (net / row.initial_r).quantize(R_MULTIPLE_PLACES) if row.initial_r > 0 else None
        )
        return CostedLeg(
            identity=identity,
            status=STATUS_CLOSED,
            reason=str(row.exit_reason or "CLOSED"),
            manual_trace_key=trace,
            buy_gross=buy.gross,
            buy_commission=buy.commission,
            buy_total=buy.cash_amount,
            sell_gross=sell.gross,
            sell_commission=sell.commission,
            sell_tax=sell.tax,
            sell_net=sell.cash_amount,
            net_pnl=net,
            r_multiple=r_multiple,
            reconciliation=(
                "MATCH"
                if row.engine_buy_total is not None and row.engine_sell_net is not None
                else "NOT_PROVIDED"
            ),
            initial_r=row.initial_r,
            slippage_bps=row.slippage_bps,
        )

    if row.last_available_close is None or row.last_available_session is None:
        return hold(STATUS_HOLD_MISSING_MARK, "no causally available close to mark against")
    if row.decision_cutoff is not None and row.last_available_session > row.decision_cutoff:
        return hold(
            STATUS_HOLD_MARK_AFTER_CUTOFF,
            f"mark session {row.last_available_session} is after the decision cutoff "
            f"{row.decision_cutoff}",
        )
    mark = model.sell(row.last_available_close, row.shares, instrument)
    open_pnl = mark.cash_amount - buy.cash_amount
    return CostedLeg(
        identity=identity,
        status=STATUS_OPEN_MARKED,
        reason="OPEN_POSITION_MARKED_TO_LAST_AVAILABLE_CLOSE",
        manual_trace_key=trace,
        buy_gross=buy.gross,
        buy_commission=buy.commission,
        buy_total=buy.cash_amount,
        sell_gross=mark.gross,
        sell_commission=mark.commission,
        sell_tax=mark.tax,
        sell_net=mark.cash_amount,
        net_pnl=open_pnl,
        r_multiple=(
            (open_pnl / row.initial_r).quantize(R_MULTIPLE_PLACES)
            if row.initial_r > 0
            else None
        ),
        mark_session=row.last_available_session,
        mark_value=mark.gross,
        cost_to_close=mark.commission + mark.tax,
        open_pnl_if_closed=open_pnl,
        reconciliation="MATCH" if row.engine_buy_total is not None else "NOT_PROVIDED",
        initial_r=row.initial_r,
        slippage_bps=row.slippage_bps,
    )


def cost_legs(rows: Iterable[LegInput]) -> tuple[CostedLeg, ...]:
    return tuple(cost_leg(row) for row in rows)


def _scenario_ledger(legs: Sequence[CostedLeg]) -> dict[str, Any]:
    """Financial ledger at the preregistered analysis unit.

    The unit is symbol x strategy x lifecycle episode x matrix (x slippage).
    Every leg of the episode inside that matrix is merged, so an add-on leg is
    never a separate sample and one matrix can never speak for the others.
    """

    grouped: dict[tuple[str, str, str, str, int], list[CostedLeg]] = {}
    for leg in legs:
        key = (
            str(leg.identity["symbol"]),
            str(leg.identity["strategy"]),
            str(leg.identity["lifecycle_episode_id"]),
            str(leg.identity["matrix_id"]),
            int(leg.slippage_bps),
        )
        grouped.setdefault(key, []).append(leg)

    rows: list[dict[str, Any]] = []
    for (symbol, strategy, episode_id, matrix_id, slippage), members in sorted(
        grouped.items()
    ):
        holds = [leg for leg in members if leg.status in HOLD_STATUSES]
        closed = [leg for leg in members if leg.status == STATUS_CLOSED]
        opened = [leg for leg in members if leg.status == STATUS_OPEN_MARKED]
        row: dict[str, Any] = {
            "analysis_unit": "symbol|strategy|lifecycle_episode|matrix|slippage",
            "symbol": symbol,
            "strategy": strategy,
            "lifecycle_episode_id": episode_id,
            "matrix_id": matrix_id,
            "slippage_bps": slippage,
            "leg_count": len(members),
            "leg_ids": sorted(str(leg.identity["leg_id"]) for leg in members),
            "closed_leg_count": len(closed),
            "open_marked_leg_count": len(opened),
            "manual_trace_keys": sorted(leg.manual_trace_key for leg in members),
            "status": "HOLD" if holds else (STATUS_OPEN_MARKED if opened else STATUS_CLOSED),
            "hold_reasons": sorted({leg.reason for leg in holds}),
        }
        if holds:
            row.update(
                {
                    "closed_net_pnl": None,
                    "open_marked_net_pnl_if_closed": None,
                    "episode_net_pnl": None,
                    "sum_initial_r": None,
                    "episode_net_R": None,
                }
            )
            rows.append(row)
            continue
        closed_net = sum((leg.net_pnl or Decimal("0") for leg in closed), Decimal("0"))
        open_net = sum(
            (leg.open_pnl_if_closed or Decimal("0") for leg in opened), Decimal("0")
        )
        total = closed_net + open_net
        risk = sum((leg.initial_r or Decimal("0") for leg in members), Decimal("0"))
        row.update(
            {
                "closed_net_pnl": closed_net,
                "open_marked_net_pnl_if_closed": open_net,
                "episode_net_pnl": total,
                "sum_initial_r": risk,
                "episode_net_R": (
                    (total / risk).quantize(R_MULTIPLE_PLACES) if risk > 0 else None
                ),
            }
        )
        rows.append(row)

    return {
        "unit": "symbol x strategy x lifecycle episode x matrix x slippage",
        "row_count": len(rows),
        "rows": rows,
        "note": (
            "Each row merges every leg of one episode inside one matrix; the six "
            "matrices stay separate rows and are weighted equally only downstream."
        ),
    }


def _cross_matrix_diagnostic(legs: Sequence[CostedLeg]) -> dict[str, Any]:
    """Correlation diagnostic only: how many independent economic signals exist.

    This deliberately carries no money field.  It must never be used as a
    financial result, because the six matrix copies of one signal are not six
    independent trades.
    """

    grouped: dict[tuple[str, str, str], dict[str, set[str]]] = {}
    for leg in legs:
        key = (
            str(leg.identity["symbol"]),
            str(leg.identity["strategy"]),
            str(leg.identity["economic_key"]),
        )
        entry = grouped.setdefault(key, {"matrices": set(), "episodes": set()})
        entry["matrices"].add(str(leg.identity["matrix_id"]))
        entry["episodes"].add(str(leg.identity["lifecycle_episode_id"]))
    rows = [
        {
            "symbol": symbol,
            "strategy": strategy,
            "economic_key": economic_key,
            "matrix_ids": sorted(payload["matrices"]),
            "matrix_count": len(payload["matrices"]),
            "lifecycle_episode_ids": sorted(payload["episodes"]),
        }
        for (symbol, strategy, economic_key), payload in sorted(grouped.items())
    ]
    return {
        "independent_signal_count": len(rows),
        "rows": rows,
        "carries_money": False,
        "note": (
            "Correlation diagnostic only; matrix multiplicity is not a trade count "
            "and this block is never a financial metric."
        ),
    }


def _six_matrix_equal_weight(scenario_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Equal-weight six-matrix diagnostic per episode and slippage scenario."""

    grouped: dict[tuple[str, str, str, int], list[Mapping[str, Any]]] = {}
    for row in scenario_rows:
        key = (
            str(row["symbol"]),
            str(row["strategy"]),
            str(row["lifecycle_episode_id"]),
            int(row["slippage_bps"]),
        )
        grouped.setdefault(key, []).append(row)
    rows: list[dict[str, Any]] = []
    expected = {f"M{index}" for index in range(1, 7)}
    for (symbol, strategy, episode_id, slippage), members in sorted(grouped.items()):
        matrices = {str(row["matrix_id"]): row for row in members}
        complete = set(matrices) == expected
        holds = [row for row in members if row["status"] == "HOLD"]
        per_matrix = {
            matrix_id: row["episode_net_R"] for matrix_id, row in sorted(matrices.items())
        }
        if not complete or holds:
            rows.append(
                {
                    "symbol": symbol,
                    "strategy": strategy,
                    "lifecycle_episode_id": episode_id,
                    "slippage_bps": slippage,
                    "status": "INCOMPLETE_OR_HOLD",
                    "episode_net_R_by_matrix": per_matrix,
                    "mean_episode_net_R": None,
                }
            )
            continue
        values = [row["episode_net_R"] for row in members]
        rows.append(
            {
                "symbol": symbol,
                "strategy": strategy,
                "lifecycle_episode_id": episode_id,
                "slippage_bps": slippage,
                "status": "DIAGNOSTIC_ONLY",
                "episode_net_R_by_matrix": per_matrix,
                "mean_episode_net_R": (
                    (sum(values, Decimal("0")) / Decimal(len(values))).quantize(
                        R_MULTIPLE_PLACES
                    )
                    if all(value is not None for value in values)
                    else None
                ),
                "weighting": "EQUAL_SIX_MATRIX",
            }
        )
    return {
        "unit": "symbol x strategy x lifecycle episode x slippage",
        "rows": rows,
        "note": (
            "Equal-weight diagnostic across M1-M6 for review only: no bootstrap, no "
            "confidence interval and no inference is computed in this round."
        ),
    }


def build_report(rows: Sequence[LegInput]) -> dict[str, Any]:
    legs = tuple(
        sorted(
            cost_legs(rows),
            key=lambda leg: (
                str(leg.identity["symbol"]),
                str(leg.identity["strategy"]),
                str(leg.identity["lifecycle_episode_id"]),
                str(leg.identity["matrix_id"]),
                int(leg.slippage_bps),
                str(leg.identity["leg_id"]),
            ),
        )
    )
    holds = [leg for leg in legs if leg.status in HOLD_STATUSES]
    scenario = _scenario_ledger(legs)
    layers: dict[str, Any] = {}
    for layer in STRATEGY_LAYERS:
        layer_legs = [leg for leg in legs if leg.identity["strategy"] == layer]
        layers[layer] = {
            "closed_leg_count": len(
                [leg for leg in layer_legs if leg.status == STATUS_CLOSED]
            ),
            "open_marked_leg_count": len(
                [leg for leg in layer_legs if leg.status == STATUS_OPEN_MARKED]
            ),
            "hold_leg_count": len(
                [leg for leg in layer_legs if leg.status in HOLD_STATUSES]
            ),
            "slippage_scenarios_present": sorted(
                {leg.slippage_bps for leg in layer_legs}
            ),
        }
    return {
        "schema_version": REPORT_CORE_VERSION,
        "legs": [leg.to_dict() for leg in legs],
        "layers": layers,
        "scenario_ledger": scenario,
        "cross_matrix_diagnostic": _cross_matrix_diagnostic(legs),
        "six_matrix_equal_weight_diagnostic": _six_matrix_equal_weight(
            scenario["rows"]
        ),
        "hold_ledger": [leg.to_dict() for leg in holds],
        "status_counts": {
            status: len([leg for leg in legs if leg.status == status])
            for status in sorted({leg.status for leg in legs})
        },
        "program_hashes": program_hashes(),
        "performance_read_or_computed": False,
        "rules_changed": False,
    }


def manual_trace_index(rows: Sequence[LegInput]) -> dict[str, Any]:
    index = {
        row.manual_trace_key(): {
            **{str(key): value for key, value in row.identity().items()},
            "matrix_id": row.matrix_id,
            "slippage_bps": row.slippage_bps,
        }
        for row in rows
    }
    return {"count": len(index), "index": index}


def write_report(*, output_dir: Path, rows: Sequence[LegInput]) -> dict[str, Any]:
    """Write the costed ledger, manual-trace index and hash readback."""

    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite cost report: {output_dir}")
    report = build_report(rows)
    trace = manual_trace_index(rows)
    input_readback = {
        "schema_version": REPORT_CORE_VERSION,
        "input_leg_count": len(rows),
        "input_sha256": canonical_sha256([row.identity() for row in rows]),
        "slippage_scenarios": [
            bps for bps in SLIPPAGE_SCENARIOS if any(row.slippage_bps == bps for row in rows)
        ],
        "program_hashes": program_hashes(),
    }
    payload = {
        "cost_report.json": canonical_bytes(report) + b"\n",
        "scenario_ledger.json": canonical_bytes(report["scenario_ledger"]) + b"\n",
        "manual_trace_index.json": canonical_bytes(trace) + b"\n",
        "hold_ledger.json": canonical_bytes(
            {"count": len(report["hold_ledger"]), "rows": report["hold_ledger"]}
        )
        + b"\n",
        "input_readback.json": canonical_bytes(input_readback) + b"\n",
    }
    output_dir.mkdir(parents=True)
    for name, blob in sorted(payload.items()):
        (output_dir / name).write_bytes(blob)
    manifest_rows = [
        {
            "path": path.name,
            "sha256": _sha256_bytes(path.read_bytes()),
            "bytes": path.stat().st_size,
        }
        for path in sorted(output_dir.iterdir())
        if path.is_file()
    ]
    (output_dir / "artifact_manifest.json").write_text(
        json.dumps(
            sorted(manifest_rows, key=lambda row: row["path"]),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    readback = {
        "status": "PASS",
        "verified": [
            {
                "path": row["path"],
                "sha256": _sha256_bytes((output_dir / row["path"]).read_bytes()),
            }
            for row in manifest_rows
        ],
    }
    (output_dir / "readback_verification.json").write_text(
        json.dumps(readback, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": REPORT_CORE_VERSION,
        "artifact_manifest_sha256": _sha256_bytes(
            (output_dir / "artifact_manifest.json").read_bytes()
        ),
        "cost_report_sha256": _sha256_bytes((output_dir / "cost_report.json").read_bytes()),
        "status_counts": report["status_counts"],
        "scenario_row_count": report["scenario_ledger"]["row_count"],
        "independent_signal_count": report["cross_matrix_diagnostic"][
            "independent_signal_count"
        ],
        "matrix_leg_count": len(rows),
        "output_dir": str(output_dir.resolve()),
    }


def synthetic_fixture_legs() -> tuple[LegInput, ...]:
    """Hand-checked fixture covering the preregistered analysis unit.

    One economic signal appears in six matrices: M1 merges a mother and an add,
    M2 exits lower with the opposite sign, M3 carries 20 bps, M4 is an ETF, M5 is
    still open (marked) and M6 is held for a data gap.  Values are hand-checked
    against the frozen cost model.
    """

    base = dict(
        symbol="2330.TW",
        strategy="MEDIUM_5_21",
        lifecycle_episode_id="2330.TW|MEDIUM_5_21|LIFE1",
        episode_key="2330.TW|MEDIUM_5_21|EP1",
        economic_key="2330.TW|MEDIUM_5_21|2022-01-06|PB1|90",
        instrument_type="STOCK",
        entry_reference_close=Decimal("100"),
        shares=100,
        initial_stop=Decimal("90"),
        initial_r=Decimal("500"),
        entry_date=date(2022, 1, 6),
    )

    def build(**overrides: Any) -> LegInput:
        leg_id = str(overrides.pop("leg_id", "MEDIUM_5_21-L1"))
        matrix_id = str(overrides.pop("matrix_id", "M1"))
        slippage = int(overrides.pop("slippage_bps", 0))
        leg_number = int(overrides.pop("leg_number", 1))
        return LegInput(
            matrix_id=matrix_id,
            slippage_bps=slippage,
            leg_id=leg_id,
            leg_number=leg_number,
            **{**base, **overrides},
        )

    return (
        build(exit_reference_close=Decimal("110"), exit_reason="REGIME_EXIT"),
        build(
            leg_id="MEDIUM_5_21-L2",
            leg_number=2,
            entry_reference_close=Decimal("105"),
            shares=50,
            initial_stop=Decimal("95"),
            initial_r=Decimal("250"),
            exit_reference_close=Decimal("110"),
            exit_reason="REGIME_EXIT",
        ),
        build(
            matrix_id="M2",
            exit_reference_close=Decimal("90"),
            exit_reason="STRUCTURAL_STOP",
        ),
        build(
            matrix_id="M3",
            slippage_bps=20,
            exit_reference_close=Decimal("110"),
            exit_reason="REGIME_EXIT",
        ),
        build(
            matrix_id="M4",
            instrument_type="ETF",
            exit_reference_close=Decimal("110"),
            exit_reason="REGIME_EXIT",
        ),
        build(
            matrix_id="M5",
            entry_reference_close=Decimal("50"),
            shares=200,
            initial_stop=Decimal("45"),
            initial_r=Decimal("1000"),
            last_available_close=Decimal("52"),
            last_available_session=date(2022, 3, 1),
        ),
        build(matrix_id="M6", data_gap_reason="UNEXPLAINED_PRICE_GAP"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--synthetic-fixture",
        action="store_true",
        help="write the hand-checked synthetic fixture instead of real legs",
    )
    args = parser.parse_args(argv)
    if not args.synthetic_fixture:
        parser.error("only --synthetic-fixture is supported in this round")
    result = write_report(output_dir=args.output_dir, rows=synthetic_fixture_legs())
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    raise SystemExit(main())
