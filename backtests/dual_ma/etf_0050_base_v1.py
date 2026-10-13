"""0050 ETF five-strategy causal backtest adapter (02 research role).

The module intentionally separates three things that must never be blurred:

* signals use a causal, split-rebased coordinate;
* orders, fees and ETF transaction tax use aligned raw closes;
* corporate actions change holdings/cash but are not trades or P&L events.

Formal 2022--2026 output is gated by 01's ``READY_FOR_02.json`` package.  With
no verified package the CLI only writes a machine-readable wait checkpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .contracts import RunManifest
from .costs import CATHAY_WEB_028_TW_V1, CostModel
from .h1h2_long_fastma_extension_2022 import LongExtensionEntryEngine
from .h2_flat_reentry_2022h2 import ReentryAfterExitEngine
from .h2_ma144_risk_width_entry_2022h2 import RiskWidthEntryEngine
from .models import BacktestResult, Bar, InstrumentType, StrategyKind, decimal
from .paths import DUAL_MA_REPORT_ROOT, dev_run, report_path


SYMBOL = "0050"
YEARS = (2022, 2023, 2024, 2025, 2026)
SLIPPAGE_SCENARIOS_BPS = (0, 10, 20)
MAIN_SCENARIO_BPS = 20
PACKAGE_ROOT_V3 = dev_run("03-etf-research-package-v3")
PACKAGE_ROOT_DEFAULT = PACKAGE_ROOT_V3
REJECTED_PACKAGE_ROOTS = (
    dev_run("03-etf-research-package-v1"),
    dev_run("03-etf-research-package-v2"),
)
OUTPUT_ROOT_DEFAULT = report_path("etf", "0050", "base-v1")
READY_MARKER = "READY_FOR_02.json"
STOP_WAIT_01_ETF_PACKAGE_V3 = "STOP_WAIT_01_ETF_PACKAGE_V3"
# Kept as a compatibility name for callers; the value intentionally points at
# the stronger v2 gate and can never authorize the rejected v1 package.
STOP_WAIT_01_PACKAGE = STOP_WAIT_01_ETF_PACKAGE_V3
STOP_WAIT_00_QA = "STOP_WAIT_00_QA"
REPORT_SCHEMA = "dual-ma-0050-base-report-v1"


class ETF0050Error(RuntimeError):
    """Structural package, accounting, or acceptance-gate failure."""


class ETFPackageNotReady(ETF0050Error):
    """01 has not supplied a complete, hash-verified research package."""


@dataclass(frozen=True)
class SplitEvent:
    effective_date: date
    ratio: Decimal
    official_pre_split_reference: Decimal | None = None
    official_post_split_reference: Decimal | None = None
    exact_post_split_reference: Decimal | None = None
    tick_size: Decimal = Decimal("0.01")
    primary_source_url: str | None = None
    primary_source_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.ratio <= 0:
            raise ValueError("split ratio must be positive")


def validate_primary_split_evidence(event: SplitEvent) -> None:
    """Require exact first-party terms; market closes and tolerances are invalid."""

    required = (
        event.official_pre_split_reference,
        event.official_post_split_reference,
        event.primary_source_url,
        event.primary_source_sha256,
    )
    if any(value is None or value == "" for value in required):
        raise ETF0050Error("split lacks complete first-party event evidence")
    assert event.official_pre_split_reference is not None
    assert event.official_post_split_reference is not None
    exact = event.official_pre_split_reference / event.ratio
    declared_exact = event.exact_post_split_reference or exact
    if exact != declared_exact:
        raise ETF0050Error(
            "declared exact split reference does not conserve value at the ratio"
        )
    rounded = exact.quantize(event.tick_size, rounding=ROUND_HALF_UP)
    if rounded != event.official_post_split_reference:
        raise ETF0050Error("official split reference does not match half-up tick rounding")
    sha = str(event.primary_source_sha256)
    if len(sha) != 64 or any(character not in "0123456789abcdefABCDEF" for character in sha):
        raise ETF0050Error("split first-party evidence sha256 is malformed")


@dataclass(frozen=True)
class SplitAcceptance:
    status: str
    checks: Mapping[str, str]
    false_stop_count: int


def evaluate_split_acceptance(
    *,
    event: SplitEvent,
    official_sessions: Sequence[date],
    transactions: Sequence[Mapping[str, Any]],
    false_stop_count: int,
) -> SplitAcceptance:
    """Hard gate for the real event-day triad and cross-split replay."""

    validate_primary_split_evidence(event)
    ordered = sorted(set(official_sessions))
    try:
        index = ordered.index(event.effective_date)
    except ValueError as exc:
        raise ETF0050Error("split effective date is not an official session") from exc
    triad = index > 0 and index + 1 < len(ordered)
    forbidden = [
        row
        for row in transactions
        if row.get("date") == event.effective_date
        and str(row.get("role")) in {"SPLIT", "DIVIDEND", "CORPORATE_ACTION"}
    ]
    checks = {
        "event_minus_one_event_plus_one_present": "PASS" if triad else "FAIL",
        "first_party_exact_value_conservation": "PASS",
        "split_not_recorded_as_transaction": "PASS" if not forbidden else "FAIL",
        "split_induced_false_stops_zero": "PASS" if false_stop_count == 0 else "FAIL",
    }
    status = "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL"
    return SplitAcceptance(status, checks, false_stop_count)


@dataclass(frozen=True)
class CashDividendEvent:
    ex_date: date
    payment_date: date
    amount_per_share: Decimal

    def __post_init__(self) -> None:
        if self.payment_date < self.ex_date:
            raise ValueError("dividend payment cannot precede ex-date")
        if self.amount_per_share < 0:
            raise ValueError("cash dividend cannot be negative")


@dataclass
class SplitAwarePosition:
    """Economic state needed for an independent split hand-check."""

    shares: Decimal
    total_cost: Decimal
    per_share_cost: Decimal
    initial_stop: Decimal
    effective_stop: Decimal
    add_price: Decimal | None
    r_per_share: Decimal
    pending_thresholds: dict[str, Decimal] = field(default_factory=dict)
    cash_dividends: Decimal = Decimal("0")
    action_audit: list[dict[str, str]] = field(default_factory=list)
    transactions: list[dict[str, str]] = field(default_factory=list)

    def market_value(self, raw_price: Decimal) -> Decimal:
        return self.shares * raw_price

    def apply_split(self, event: SplitEvent) -> None:
        """Apply a split without creating a trade, dividend, gain, or loss."""

        before_cost = self.total_cost
        self.shares *= event.ratio
        self.per_share_cost /= event.ratio
        self.initial_stop /= event.ratio
        self.effective_stop /= event.ratio
        self.r_per_share /= event.ratio
        if self.add_price is not None:
            self.add_price /= event.ratio
        self.pending_thresholds = {
            name: value / event.ratio
            for name, value in self.pending_thresholds.items()
        }
        if self.total_cost != before_cost:
            raise AssertionError("a split must preserve total acquisition cost")
        self.action_audit.append(
            {
                "date": event.effective_date.isoformat(),
                "type": "SPLIT_REBASE_ONLY",
                "ratio": str(event.ratio),
            }
        )

    def apply_dividend(self, event: CashDividendEvent) -> Decimal:
        amount = self.shares * event.amount_per_share
        self.cash_dividends += amount
        self.action_audit.append(
            {
                "date": event.payment_date.isoformat(),
                "type": "CASH_DIVIDEND",
                "amount": str(amount),
            }
        )
        return amount


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    label: str
    strategy_kind: StrategyKind
    engine_kind: str


STRATEGY_SPECS: tuple[StrategySpec, ...] = (
    StrategySpec("A_MEDIUM", "基本版－中線", StrategyKind.MEDIUM, "BASIC"),
    StrategySpec("A_LONG", "基本版－長線", StrategyKind.LONG, "BASIC"),
    StrategySpec("B_MEDIUM", "7%風險寬度－中線", StrategyKind.MEDIUM, "RISK_WIDTH"),
    StrategySpec("B_LONG", "7%風險寬度－長線", StrategyKind.LONG, "RISK_WIDTH"),
    StrategySpec("C_LONG", "高於SMA21 5%－長線", StrategyKind.LONG, "EXTENSION"),
)


@dataclass(frozen=True)
class StrategyRunSlice:
    spec: StrategySpec
    result: BacktestResult
    decisions: tuple[Mapping[str, Any], ...]
    transactions: tuple[Mapping[str, Any], ...]
    legs: tuple[Any, ...]


def eligibility_sessions(
    year: int, official_sessions: Iterable[date], *, cutoff: date | None = None
) -> tuple[date, ...]:
    """0050 is eligible on every official tradable session in its cohort."""

    end = cutoff or date(year, 12, 31)
    return tuple(sorted({day for day in official_sessions if day.year == year and day <= end}))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ETF0050Error(f"cannot read valid JSON: {path}") from exc


def _verify_artifact_rows(root: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise ETF0050Error("READY package contains no artifact hash rows")
    for row in rows:
        relative = str(row.get("path") or row.get("relative_path") or "")
        if not relative:
            raise ETF0050Error("artifact row has no path")
        path = (root / relative).resolve()
        if root.resolve() not in path.parents and path != root.resolve():
            raise ETF0050Error(f"artifact escapes package root: {relative}")
        if not path.is_file():
            raise ETF0050Error(f"artifact is missing: {relative}")
        expected_bytes = row.get("bytes")
        if expected_bytes is not None and path.stat().st_size != int(expected_bytes):
            raise ETF0050Error(f"artifact byte mismatch: {relative}")
        expected_sha = str(row.get("sha256") or "").lower()
        if not expected_sha or _sha256(path) != expected_sha:
            raise ETF0050Error(f"artifact sha256 mismatch: {relative}")


def validate_ready_package(root: Path) -> Mapping[str, Any]:
    """Validate marker, manifest hashes, and readback before any formal run."""

    if root.resolve() != PACKAGE_ROOT_V3.resolve():
        raise ETFPackageNotReady(
            f"STOP_WAIT_01_ETF_PACKAGE_V3: rejected package root {root}"
        )
    marker_path = root / READY_MARKER
    if not marker_path.is_file():
        raise ETFPackageNotReady(
            f"STOP_WAIT_01_ETF_PACKAGE_V3: missing {marker_path}"
        )
    marker = _json(marker_path)
    if not isinstance(marker, Mapping):
        raise ETF0050Error("READY marker root must be an object")
    if marker.get("schema_version") != "dual-ma-etf-research-package-v3-ready-for-02":
        raise ETF0050Error("READY marker schema is not the adopted v3 schema")
    if marker.get("data_version") != "etf-research-package-v3":
        raise ETF0050Error("READY marker data_version is not adopted v3")
    if marker.get("ready") is not True:
        raise ETFPackageNotReady("STOP_WAIT_01_ETF_PACKAGE_V3: ready is not true")
    ready_checks = marker.get("ready_checks")
    required_checks = {
        "dividend_events_complete",
        "listing_date_verified",
        "price_series_complete",
        "split_first_party_verified",
    }
    if not isinstance(ready_checks, Mapping) or set(ready_checks) != required_checks:
        raise ETF0050Error("READY marker does not contain exactly four v3 ready_checks")
    if any(ready_checks[name] is not True for name in required_checks):
        raise ETFPackageNotReady("STOP_WAIT_01_ETF_PACKAGE_V3: a ready_check failed")
    status = str(
        marker.get("status")
        or marker.get("decision")
        or ("READY_FOR_02" if marker.get("ready") is True else "")
    )
    if status not in {"READY_FOR_02", "PASS"}:
        raise ETFPackageNotReady(
            f"STOP_WAIT_01_ETF_PACKAGE_V3: marker status={status!r}"
        )

    artifact_rows = marker.get("artifacts")
    if not isinstance(artifact_rows, list):
        consumer = marker.get("consumer_entry")
        if not isinstance(consumer, Mapping):
            raise ETF0050Error("READY marker has no consumer_entry")
        manifest_name = str(consumer.get("artifact_manifest") or "")
        manifest_path = root / manifest_name
        if not manifest_path.is_file():
            raise ETF0050Error("READY marker does not expose a usable artifact manifest")
        expected_manifest_sha = marker.get("manifest_sha256") or marker.get(
            "artifact_manifest_sha256"
        )
        if expected_manifest_sha and _sha256(manifest_path) != str(expected_manifest_sha).lower():
            raise ETF0050Error("artifact manifest sha256 mismatch")
        manifest = _json(manifest_path)
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("schema_version")
            != "dual-ma-etf-research-package-v3-manifest"
        ):
            raise ETF0050Error("artifact manifest schema is not adopted v3")
        if int(manifest.get("artifact_count", -1)) != len(manifest.get("artifacts") or []):
            raise ETF0050Error("artifact manifest count mismatch")
        if int(manifest.get("artifact_count", -1)) != 172:
            raise ETF0050Error("adopted v3 artifact count must be 172")
        artifact_rows = manifest.get("artifacts") if isinstance(manifest, Mapping) else None
    if not isinstance(artifact_rows, list):
        raise ETF0050Error("artifact manifest has no artifacts list")
    _verify_artifact_rows(root, artifact_rows)

    consumer = marker.get("consumer_entry")
    assert isinstance(consumer, Mapping)
    readback_name = str(consumer.get("readback") or "")
    readback_path = root / readback_name
    if not readback_path.is_file():
        raise ETF0050Error("READY package has no readback.json")
    expected_readback_sha = marker.get("readback_sha256")
    if expected_readback_sha and _sha256(readback_path) != str(expected_readback_sha).lower():
        raise ETF0050Error("readback sha256 mismatch")
    readback = _json(readback_path)
    if not isinstance(readback, Mapping) or str(readback.get("status")) != "PASS":
        raise ETF0050Error("package readback did not PASS")
    mismatches = readback.get("mismatches")
    if mismatches not in (None, [], {}):
        raise ETF0050Error("package readback contains mismatches")
    if readback.get("source_mismatches") not in (None, [], {}):
        raise ETF0050Error("package readback contains source mismatches")
    for name in (
        "artifact_hashes_verified",
        "source_hashes_verified",
        "ready_for_02",
    ):
        if readback.get(name) is not True:
            raise ETF0050Error(f"package readback {name} is not true")

    source_master_path = root / str(consumer.get("source_master_table") or "")
    source_master = _json(source_master_path)
    if (
        not isinstance(source_master, Mapping)
        or source_master.get("schema_version")
            != "dual-ma-etf-research-package-v3-source-master-table"
    ):
        raise ETF0050Error("source master schema is not adopted v3")
    source_rows = source_master.get("entries")
    if not isinstance(source_rows, list) or int(source_master.get("entry_count", -1)) != len(source_rows):
        raise ETF0050Error("source master count mismatch")
    if int(source_master.get("entry_count", -1)) != 162:
        raise ETF0050Error("adopted v3 source entry count must be 162")
    _verify_artifact_rows(root, source_rows)

    split = marker.get("split_pinning")
    if not isinstance(split, Mapping) or split.get("status") != "FIRST_PARTY_VERIFIED":
        raise ETF0050Error("split pinning is not first-party verified")
    exact = decimal(split.get("exact_reference_price"))
    official = decimal(split.get("official_reference_price"))
    ratio = decimal(split.get("ratio"))
    tick = split.get("tick_policy")
    if exact != Decimal("188.65") / ratio:
        raise ETF0050Error("split exact reference is not 188.65 divided by ratio")
    if not isinstance(tick, Mapping) or tick.get("rounding") != "half-up to 0.01":
        raise ETF0050Error("split tick policy is not the adopted half-up rule")
    if exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != official:
        raise ETF0050Error("split official reference fails exact half-up tick rounding")
    for lineage in (split.get("lineage") or {}).values():
        if not isinstance(lineage, Mapping):
            raise ETF0050Error("split lineage row is malformed")
        path = root / str(lineage.get("path") or "")
        if _sha256(path) != str(lineage.get("sha256") or "").lower():
            raise ETF0050Error("split lineage source sha256 mismatch")
    return marker


def build_strategy_engine(
    spec: StrategySpec,
    *,
    slippage_bps: int,
    gate_map: Mapping[date, Any],
    official_sessions: Sequence[date],
) -> Any:
    common = {"slippage_bps": slippage_bps, "kd_2r_death_cross_exit": False}
    if spec.engine_kind == "BASIC":
        return ReentryAfterExitEngine(**common)
    gated = {**common, "gate_map": gate_map, "official_sessions": official_sessions}
    if spec.engine_kind == "RISK_WIDTH":
        return RiskWidthEntryEngine(**gated)
    if spec.engine_kind == "EXTENSION":
        return LongExtensionEntryEngine(**gated)
    raise ETF0050Error(f"unknown engine kind: {spec.engine_kind}")


def run_five_strategy_interfaces(
    *,
    signal_bars: Sequence[Bar],
    raw_execution_bars: Sequence[Bar],
    split_events: Sequence[SplitEvent],
    gate_map: Mapping[date, Any],
    official_sessions: Sequence[date],
    manifest_factory: Callable[[str, int], RunManifest],
    slippage_bps: int,
    backtest_start: date,
    backtest_end: date,
    required_warmup_bars: int = 143,
    split_signal_coordinate_already_adjusted: bool = False,
) -> tuple[StrategyRunSlice, ...]:
    """Run five independent single-position strategies through the same API."""

    if [bar.date for bar in signal_bars] != [bar.date for bar in raw_execution_bars]:
        raise ETF0050Error("signal and raw execution bars are not date aligned")
    split_map = {event.effective_date: event.ratio for event in split_events}
    output: list[StrategyRunSlice] = []
    for spec in STRATEGY_SPECS:
        engine = build_strategy_engine(
            spec,
            slippage_bps=slippage_bps,
            gate_map=gate_map,
            official_sessions=official_sessions,
        )
        result = engine.run(
            SYMBOL,
            signal_bars,
            run_manifest=manifest_factory("M1", slippage_bps),
            instrument_type=InstrumentType.ETF,
            backtest_start=backtest_start,
            backtest_end=backtest_end,
            required_warmup_bars=required_warmup_bars,
            execution_bars=raw_execution_bars,
            split_ratios_by_date=split_map,
            split_signal_coordinate_already_adjusted=(
                split_signal_coordinate_already_adjusted
            ),
        )
        wanted = spec.strategy_kind.value
        decisions = tuple(row for row in result.decisions if row["strategy"] == wanted)
        transactions = tuple(
            row for row in result.transactions if row["strategy"] == wanted
        )
        legs = tuple(leg for leg in result.legs if leg.strategy == spec.strategy_kind)
        output.append(StrategyRunSlice(spec, result, decisions, transactions, legs))
    return tuple(output)


@dataclass(frozen=True)
class BuyHoldResult:
    reinvest: bool
    shares: Decimal
    residual_cash: Decimal
    dividends_received: Decimal
    ending_liquidation_value: Decimal
    transactions: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class OpenMark:
    raw_reference_price: Decimal
    shares: int
    hypothetical_sell_net: Decimal
    unrealized_total_return: Decimal
    unrealized_return_rate: Decimal


def mark_open_position(
    *,
    raw_reference_price: Decimal,
    shares: int,
    total_cost: Decimal,
    cash_dividends: Decimal = Decimal("0"),
    cost_model: CostModel = CATHAY_WEB_028_TW_V1,
) -> OpenMark:
    """Value an open holding separately; this never creates a realized trade."""

    sale = cost_model.sell(raw_reference_price, shares, InstrumentType.ETF)
    unrealized = sale.cash_amount + cash_dividends - total_cost
    return OpenMark(
        raw_reference_price,
        shares,
        sale.cash_amount,
        unrealized,
        unrealized / total_cost,
    )


def simulate_buy_and_hold(
    *,
    raw_close_by_date: Mapping[date, Decimal],
    initial_cash: Decimal,
    splits: Sequence[SplitEvent],
    dividends: Sequence[CashDividendEvent],
    reinvest: bool,
    cost_model: CostModel = CATHAY_WEB_028_TW_V1,
) -> BuyHoldResult:
    dates = sorted(raw_close_by_date)
    if not dates:
        raise ETF0050Error("buy-and-hold requires at least one raw close")
    first, last = dates[0], dates[-1]
    price = raw_close_by_date[first]
    shares = int((initial_cash / price).to_integral_value(rounding=ROUND_FLOOR))
    while shares > 0 and cost_model.buy(price, shares).cash_amount > initial_cash:
        shares -= 1
    if shares < 1:
        raise ETF0050Error("initial cash cannot buy one 0050 share")
    buy = cost_model.buy(price, shares)
    held = Decimal(shares)
    cash = initial_cash - buy.cash_amount
    received = Decimal("0")
    transactions: list[Mapping[str, Any]] = [
        {"date": first, "role": "BUY", "shares": shares, "cash": buy.cash_amount}
    ]
    actions: list[tuple[date, int, Any]] = []
    actions.extend((event.effective_date, 0, event) for event in splits)
    actions.extend((event.payment_date, 1, event) for event in dividends)
    for action_date, _, event in sorted(actions, key=lambda row: (row[0], row[1])):
        if action_date < first or action_date > last:
            continue
        if isinstance(event, SplitEvent):
            held *= event.ratio
            continue
        amount = held * event.amount_per_share
        received += amount
        cash += amount
        if reinvest:
            reinvest_price = raw_close_by_date.get(event.payment_date)
            if reinvest_price is None:
                raise ETF0050Error("dividend reinvestment date lacks a raw close")
            extra = int((cash / reinvest_price).to_integral_value(rounding=ROUND_FLOOR))
            while extra > 0 and cost_model.buy(reinvest_price, extra).cash_amount > cash:
                extra -= 1
            if extra:
                order = cost_model.buy(reinvest_price, extra)
                held += extra
                cash -= order.cash_amount
                transactions.append(
                    {
                        "date": event.payment_date,
                        "role": "DIVIDEND_REINVEST",
                        "shares": extra,
                        "cash": order.cash_amount,
                    }
                )
    liquidation = cost_model.sell(
        raw_close_by_date[last], int(held), InstrumentType.ETF
    ).cash_amount + cash
    return BuyHoldResult(
        reinvest, held, cash, received, liquidation, tuple(transactions)
    )


def fixed_detail_sample(rows: Sequence[Any], *, seed: int = 50) -> tuple[Any, ...]:
    if len(rows) <= 12:
        return tuple(rows)
    indices = sorted(random.Random(seed).sample(range(len(rows)), 12))
    return tuple(rows[index] for index in indices)


def render_year_report(year: int, payload: Mapping[str, Any]) -> str:
    """Fixed plain-language shell; callers must provide verified metrics."""

    if year not in YEARS:
        raise ETF0050Error(f"unsupported report year: {year}")
    if payload.get("status") != "PASS":
        raise ETF0050Error("refusing to render a performance report from unverified data")
    return "\n".join(
        [
            f"# 0050 雙均線基準回測｜{year}",
            "",
            "## 一、先講結論",
            str(payload["plain_conclusion"]),
            "",
            "## 二、比較表（買進持有、股利再投入、五個策略）",
            str(payload["comparison_table"]),
            "主表固定欄位：實現損益、未平倉評價、合計報酬、買進次數、每100個訊號、"
            "持倉峰值與日期、CAGR、最大單日回撤、報酬／風險、閒置現金、交易次數與成本。",
            "",
            "## 三、實現損益、未平倉市值與合計",
            str(payload["pnl_table"]),
            "",
            "## 四、報酬與風險",
            str(payload["risk_table"]),
            "",
            "## 五、交易成本、閒置現金與交易次數",
            str(payload["cost_table"]),
            "",
            "## 六、逐筆核對與抽樣",
            str(payload["detail_summary"]),
            "逐筆欄位：訊號日、成交日、raw 價格、股數、手續費、ETF 賣出稅、現金股利、"
            "實現報酬率；未平倉報酬另表，不混入實現損益。",
            "",
            "## 七、來源分組",
            str(payload.get("source_groups", "見 manifest 的來源分組與 SHA-256。")),
            "",
            "## 八、資料與因果限制",
            str(payload["causal_notes"]),
            "",
            f"報告格式：{REPORT_SCHEMA}；主情境：單邊滑價 {MAIN_SCENARIO_BPS} bps。",
            "",
        ]
    )


def render_overview_report(payload: Mapping[str, Any]) -> str:
    if payload.get("status") != "PASS":
        raise ETF0050Error("refusing to render an overview from unverified data")
    return "\n".join(
        [
            "# 0050 雙均線基準回測｜2022～2026 總覽",
            "",
            "## 一、五年總結",
            str(payload["plain_conclusion"]),
            "",
            "## 二、買進持有、股利再投入與五策略",
            str(payload["comparison_table"]),
            "",
            "## 三、2022 防守能力",
            str(payload["defense_2022"]),
            "",
            "## 四、多頭參與率",
            str(payload["bull_participation"]),
            "",
            "## 五、逐年穩定性、成本與閒置現金",
            str(payload["stability_table"]),
            "",
            "## 六、來源分組與因果限制",
            str(payload["source_groups"]),
            "",
            f"主情境為 {MAIN_SCENARIO_BPS} bps；0/10/20 bps 均保留機器明細。",
            "",
        ]
    )


def tree_hash(root: Path, *, exclude: Path | None = None) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    excluded = None if exclude is None else exclude.resolve()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        resolved = path.resolve()
        if excluded is not None and (resolved == excluded or excluded in resolved.parents):
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _write_bytes_preserving(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
        return
    path.write_bytes(payload)


def _write_json(path: Path, value: Any) -> None:
    _write_bytes_preserving(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def prepare_wait_checkpoint(
    *, package_root: Path, output_root: Path, old_run_root: Path
) -> Mapping[str, Any]:
    """Write only readiness artifacts; never fabricate annual performance."""

    before = tree_hash(old_run_root, exclude=output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "details").mkdir(exist_ok=True)
    resume = (
        "python -m backtests.dual_ma.etf_0050_base_v1 "
        f'--package-root "{package_root}" --output-root "{output_root}"'
    )
    checkpoint = {
        "schema_version": "dual-ma-0050-base-checkpoint-v1",
        "status": STOP_WAIT_01_ETF_PACKAGE_V3,
        "performance_reports_generated": False,
        "missing_gate": str(package_root / READY_MARKER),
        "resume_command": resume,
        "formal_run_preconditions": [
            "READY_FOR_02 marker, artifact SHA-256, and readback all pass",
            "2025 true-data split triad and cross-split hand-check pass",
            "split-induced false stops/exits/reentries equal zero",
            "00 separately adopts v3 and explicitly releases the end-to-end run",
        ],
        "post_release_execution": (
            "one uninterrupted end-to-end batch: 2022-2026, five strategies, two "
            "buy-and-hold baselines, yearly reports, five-year overview, source "
            "groups, trade details, manifest/readback, and tests; stop only for a "
            "contract or persistent safety blocker"
        ),
        "old_run_tree_sha256_before": before,
    }
    readme = "\n".join(
        [
            "# 0050 ETF base-v1",
            "",
            f"目前狀態：`{STOP_WAIT_01_ETF_PACKAGE_V3}`。",
            "",
            "01 的資料包尚未通過 READY/SHA/readback，因此這裡沒有任何年度績效數字，"
            "也沒有建立 2022～2026 報告。分割驗收未通過時同樣禁止正式跑表。",
            "",
            "恢復命令：",
            "",
            f"`{resume}`",
            "",
        ]
    )
    after = tree_hash(old_run_root, exclude=output_root)
    checkpoint["old_run_tree_sha256_after"] = after
    checkpoint["old_run_unchanged"] = before == after
    _write_bytes_preserving(output_root / "README.md", readme.encode("utf-8"))
    _write_json(output_root / "checkpoint.json", checkpoint)
    artifacts = []
    for name in ("README.md", "checkpoint.json"):
        path = output_root / name
        artifacts.append(
            {"path": name, "bytes": path.stat().st_size, "sha256": _sha256(path)}
        )
    manifest = {
        "schema_version": "dual-ma-0050-base-manifest-v1",
        "status": STOP_WAIT_01_ETF_PACKAGE_V3,
        "artifacts": artifacts,
        "annual_reports": [],
        "formal_performance_computed": False,
    }
    _write_json(output_root / "manifest.json", manifest)
    _verify_artifact_rows(output_root, artifacts)
    readback = {
        "schema_version": "dual-ma-0050-base-readback-v1",
        "status": "PASS" if before == after else "FAIL",
        "mismatches": [] if before == after else ["old run tree changed"],
    }
    _write_json(output_root / "readback.json", readback)
    return checkpoint


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, default=PACKAGE_ROOT_DEFAULT)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT_DEFAULT)
    parser.add_argument(
        "--old-run-root",
        type=Path,
        default=DUAL_MA_REPORT_ROOT,
    )
    parser.add_argument(
        "--released-by-00",
        action="store_true",
        help="required fail-closed gate after 00 independently adopts v3",
    )
    args = parser.parse_args(argv)
    try:
        validate_ready_package(args.package_root)
    except ETFPackageNotReady:
        checkpoint = prepare_wait_checkpoint(
            package_root=args.package_root,
            output_root=args.output_root,
            old_run_root=args.old_run_root,
        )
        print(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True))
        return 2
    # A package cannot self-authorize 00's independent acceptance.
    if not args.released_by_00:
        print(
            json.dumps(
                {
                    "status": STOP_WAIT_00_QA,
                    "formal_run_started": False,
                    "next_gate": "rerun only after 00 release with --released-by-00",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 3
    from .etf_0050_formal_v1 import run_formal_batch

    result = run_formal_batch(
        package_root=args.package_root,
        output_root=args.output_root,
        repo_root=Path(__file__).resolve().parents[2],
        released_by_00=True,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
