from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any


def decimal(value: Decimal | int | float | str) -> Decimal:
    """Convert market inputs without importing binary-float noise."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class StrategyKind(str, Enum):
    MEDIUM = "MEDIUM_5_21"
    LONG = "LONG_21_144"


class AddVariant(str, Enum):
    R1_AGGREGATE = "ADD_R1_AGGREGATE"
    SIGNAL_ONLY = "ADD_SIGNAL_ONLY"


class ExitVariant(str, Enum):
    DOW_TRAIL = "EXIT_DOW_TRAIL"
    R2_DOW_TRAIL = "EXIT_2R_DOW_TRAIL"
    R2_21MA_2CLOSE = "EXIT_2R_21MA_2CLOSE"


class InstrumentType(str, Enum):
    STOCK = "STOCK"
    ETF = "ETF"
    BENEFICIARY = "BENEFICIARY"


class SignalStatus(str, Enum):
    FILLED = "FILLED"
    NON_EXECUTABLE = "NON_EXECUTABLE"
    ADD_GATE_REJECTED = "ADD_GATE_REJECTED"
    MAX_LEGS_REJECTED = "MAX_LEGS_REJECTED"


class ExitReason(str, Enum):
    STRUCTURAL_STOP = "STRUCTURAL_STOP"
    PROFIT_EXIT = "PROFIT_EXIT"
    REGIME_EXIT = "REGIME_EXIT"


@dataclass(frozen=True)
class Bar:
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @classmethod
    def from_values(
        cls,
        day: date,
        open_: Decimal | int | float | str,
        high: Decimal | int | float | str,
        low: Decimal | int | float | str,
        close: Decimal | int | float | str,
    ) -> "Bar":
        return cls(day, decimal(open_), decimal(high), decimal(low), decimal(close))

    def __post_init__(self) -> None:
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("bar OHLC values are inconsistent")
        if self.low > self.high:
            raise ValueError("bar low cannot exceed high")


@dataclass(frozen=True)
class Pivot:
    kind: str
    value: Decimal
    zone_start_index: int
    zone_end_index: int
    confirmed_index: int
    zone_start_date: date
    zone_end_date: date
    confirmed_date: date


@dataclass(frozen=True)
class DowDefenseEvent:
    defense: Decimal
    pivot_low: Pivot
    prior_pivot_high: Pivot
    breakout_index: int
    breakout_date: date
    activated_index: int
    activated_date: date
    version: int = 0


@dataclass
class Pullback:
    start_index: int
    start_date: date
    low: Decimal
    event_id: str = ""


@dataclass
class Leg:
    leg_id: str
    symbol: str
    strategy: StrategyKind
    entry_date: date
    entry_reference_close: Decimal
    entry_fill_price: Decimal
    shares: int
    buy_gross: Decimal
    buy_commission: Decimal
    buy_total: Decimal
    initial_stop: Decimal
    initial_r: Decimal
    effective_stop: Decimal
    episode_id: str = ""
    leg_number: int = 0
    breakeven_stop: Decimal | None = None
    dow_stop: Decimal | None = None
    two_r_date: date | None = None
    exit_date: date | None = None
    exit_reason: ExitReason | None = None
    exit_reference_close: Decimal | None = None
    exit_fill_price: Decimal | None = None
    sell_gross: Decimal | None = None
    sell_commission: Decimal | None = None
    sell_tax: Decimal | None = None
    sell_net: Decimal | None = None
    net_pnl: Decimal | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_date is None


@dataclass
class BacktestResult:
    symbol: str
    add_variant: AddVariant
    exit_variant: ExitVariant
    slippage_bps: int
    matrix_group: str = ""
    instrument_type: InstrumentType = InstrumentType.STOCK
    strategy_version: str = ""
    spec_sha256: str = ""
    run_metadata: dict[str, Any] = field(default_factory=dict)
    input_audit: dict[str, Any] = field(default_factory=dict)
    manifest_fingerprint: str = ""
    input_fingerprint: str = ""
    semantic_result_fingerprint: str = ""
    decisions: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    assumptions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
