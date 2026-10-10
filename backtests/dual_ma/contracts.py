from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .models import Bar, InstrumentType, decimal


STRATEGY_VERSION = "v0.4.1-frozen"
SPEC_SHA256 = "17f3e15b360eba03d9cbc47a5c1013f6ab4e86352cac8beaa87ad1d94fdc5c1a"
EXECUTION_MODEL = "EOD_CLOSE_PROXY_V1"
COST_MODEL = "CATHAY_WEB_028_TW_V1"
MATRIX_IDS = frozenset({"M1", "M2", "M3", "M4", "M5", "M6"})
SLIPPAGE_SCENARIOS = frozenset({0, 10, 20})


class ContractValidationError(ValueError):
    """Raised when a formal run or normalized daily-bar input is invalid."""


def _canonicalize(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        payload_method = getattr(value, "payload", None)
        value = payload_method() if callable(payload_method) else asdict(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ContractValidationError("canonical values must be finite")
        normalized = value.normalize()
        return "0" if normalized == 0 else format(normalized, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractValidationError("canonical values must be finite")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonicalize(item) for item in value), key=str)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ContractValidationError(
        f"unsupported canonical value type: {type(value).__name__}"
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    strategy_version: str
    spec_sha256: str
    git_commit: str
    universe_id: str
    data_snapshot_id: str
    data_start: date
    data_end: date
    data_hash: str
    execution_model: str
    cost_model: str
    matrix_id: str
    slippage_bps: int
    strategy_parameters: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required_text = (
            "run_id",
            "strategy_version",
            "spec_sha256",
            "git_commit",
            "universe_id",
            "data_snapshot_id",
            "data_hash",
            "execution_model",
            "cost_model",
            "matrix_id",
        )
        for name in required_text:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractValidationError(f"{name} is required and cannot be empty")
        if not isinstance(self.data_start, date) or not isinstance(self.data_end, date):
            raise ContractValidationError("data_start and data_end must be dates")
        if self.data_start > self.data_end:
            raise ContractValidationError("data_start cannot be after data_end")
        if self.strategy_version != STRATEGY_VERSION:
            raise ContractValidationError(
                f"strategy_version must be {STRATEGY_VERSION}"
            )
        if self.spec_sha256.lower() != SPEC_SHA256:
            raise ContractValidationError(f"spec_sha256 must be {SPEC_SHA256}")
        if self.execution_model != EXECUTION_MODEL:
            raise ContractValidationError(f"execution_model must be {EXECUTION_MODEL}")
        if self.cost_model != COST_MODEL:
            raise ContractValidationError(f"cost_model must be {COST_MODEL}")
        if self.matrix_id not in MATRIX_IDS:
            raise ContractValidationError("matrix_id must be one of M1..M6")
        if self.slippage_bps not in SLIPPAGE_SCENARIOS:
            raise ContractValidationError("slippage_bps must be 0, 10, or 20")
        object.__setattr__(self, "strategy_parameters", _freeze(self.strategy_parameters))
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        canonical_json(self.strategy_parameters)
        canonical_json(self.metadata)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "RunManifest":
        required = {
            "run_id",
            "strategy_version",
            "spec_sha256",
            "git_commit",
            "universe_id",
            "data_snapshot_id",
            "data_start",
            "data_end",
            "data_hash",
            "execution_model",
            "cost_model",
            "matrix_id",
            "slippage_bps",
        }
        missing = sorted(required - set(values))
        if missing:
            raise ContractValidationError(
                f"missing required manifest fields: {', '.join(missing)}"
            )
        allowed = required | {"strategy_parameters", "metadata"}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ContractValidationError(
                f"unknown manifest fields: {', '.join(unknown)}"
            )
        normalized = dict(values)
        for name in ("data_start", "data_end"):
            if isinstance(normalized[name], str):
                try:
                    normalized[name] = date.fromisoformat(normalized[name])
                except ValueError as exc:
                    raise ContractValidationError(
                        f"{name} must use ISO YYYY-MM-DD"
                    ) from exc
        return cls(**normalized)

    def payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy_version": self.strategy_version,
            "spec_sha256": self.spec_sha256,
            "git_commit": self.git_commit,
            "universe_id": self.universe_id,
            "data_snapshot_id": self.data_snapshot_id,
            "data_start": self.data_start,
            "data_end": self.data_end,
            "data_hash": self.data_hash,
            "execution_model": self.execution_model,
            "cost_model": self.cost_model,
            "matrix_id": self.matrix_id,
            "slippage_bps": self.slippage_bps,
            "strategy_parameters": self.strategy_parameters,
            "metadata": self.metadata,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.payload())

    def to_dict(self) -> dict[str, Any]:
        payload = _canonicalize(self.payload())
        payload["manifest_fingerprint"] = self.fingerprint
        return payload


@dataclass(frozen=True)
class InputAudit:
    symbol: str
    instrument_type: InstrumentType
    declared_data_start: date
    declared_data_end: date
    observed_start: date
    observed_end: date
    backtest_start: date
    backtest_end: date
    bar_count: int
    backtest_bar_count: int
    warmup_bar_count: int
    post_backtest_bar_count: int
    processed_bar_count: int
    required_warmup_bars: int
    has_required_warmup: bool
    covers_backtest_start: bool
    covers_backtest_end: bool
    input_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return _canonicalize(asdict(self))


def validate_daily_inputs(
    symbol: str,
    instrument_type: InstrumentType | str,
    bars: Sequence[Bar],
    manifest: RunManifest,
    *,
    backtest_start: date | None = None,
    backtest_end: date | None = None,
    required_warmup_bars: int = 143,
) -> InputAudit:
    if not isinstance(symbol, str) or not symbol.strip():
        raise ContractValidationError("symbol is required and cannot be empty")
    try:
        normalized_instrument = InstrumentType(instrument_type)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("instrument_type is required and invalid") from exc
    if not bars:
        raise ContractValidationError("at least one daily bar is required")
    if not isinstance(required_warmup_bars, int) or required_warmup_bars < 0:
        raise ContractValidationError("required_warmup_bars must be a non-negative integer")

    previous_date: date | None = None
    normalized_bars: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        if not isinstance(bar, Bar):
            raise ContractValidationError(
                "daily bars must be normalized Bar objects; source adapters are external"
            )
        if not isinstance(bar.date, date):
            raise ContractValidationError(f"bar {index} has an invalid date")
        if previous_date is not None and bar.date <= previous_date:
            raise ContractValidationError(
                "daily bar dates must be unique and strictly increasing"
            )
        previous_date = bar.date
        prices: dict[str, Decimal] = {}
        for name in ("open", "high", "low", "close"):
            try:
                price = decimal(getattr(bar, name))
            except Exception as exc:
                raise ContractValidationError(
                    f"bar {index} {name} must be numeric"
                ) from exc
            if not price.is_finite() or price <= 0:
                raise ContractValidationError(
                    f"bar {index} {name} must be finite and positive"
                )
            prices[name] = price
        if prices["low"] > min(prices["open"], prices["close"]):
            raise ContractValidationError(f"bar {index} low exceeds open/close")
        if prices["high"] < max(prices["open"], prices["close"]):
            raise ContractValidationError(f"bar {index} high is below open/close")
        if prices["low"] > prices["high"]:
            raise ContractValidationError(f"bar {index} low exceeds high")
        normalized_bars.append({"date": bar.date, **prices})

    observed_start = bars[0].date
    observed_end = bars[-1].date
    if observed_start < manifest.data_start or observed_end > manifest.data_end:
        raise ContractValidationError(
            "observed bars must stay within manifest data_start/data_end"
        )
    effective_start = backtest_start or manifest.data_start
    effective_end = backtest_end or manifest.data_end
    if not isinstance(effective_start, date) or not isinstance(effective_end, date):
        raise ContractValidationError("backtest_start and backtest_end must be dates")
    if effective_start > effective_end:
        raise ContractValidationError("backtest_start cannot be after backtest_end")
    if effective_start < manifest.data_start or effective_end > manifest.data_end:
        raise ContractValidationError(
            "backtest interval must stay within manifest data coverage"
        )

    warmup_count = sum(bar.date < effective_start for bar in bars)
    backtest_count = sum(effective_start <= bar.date <= effective_end for bar in bars)
    post_backtest_count = sum(bar.date > effective_end for bar in bars)
    if backtest_count == 0:
        raise ContractValidationError("no daily bars overlap the backtest interval")
    input_payload = {
        "symbol": symbol.strip(),
        "instrument_type": normalized_instrument,
        "declared_data_start": manifest.data_start,
        "declared_data_end": manifest.data_end,
        "backtest_start": effective_start,
        "backtest_end": effective_end,
        "required_warmup_bars": required_warmup_bars,
        "bars": [
            normalized
            for bar, normalized in zip(bars, normalized_bars)
            if bar.date <= effective_end
        ],
    }
    return InputAudit(
        symbol=symbol.strip(),
        instrument_type=normalized_instrument,
        declared_data_start=manifest.data_start,
        declared_data_end=manifest.data_end,
        observed_start=observed_start,
        observed_end=observed_end,
        backtest_start=effective_start,
        backtest_end=effective_end,
        bar_count=len(bars),
        backtest_bar_count=backtest_count,
        warmup_bar_count=warmup_count,
        post_backtest_bar_count=post_backtest_count,
        processed_bar_count=warmup_count + backtest_count,
        required_warmup_bars=required_warmup_bars,
        has_required_warmup=warmup_count >= required_warmup_bars,
        covers_backtest_start=observed_start <= effective_start <= observed_end,
        covers_backtest_end=observed_start <= effective_end <= observed_end,
        input_fingerprint=canonical_fingerprint(input_payload),
    )


def semantic_result_fingerprint(result: Any) -> str:
    if not is_dataclass(result):
        raise ContractValidationError("result fingerprint requires a dataclass result")
    result_payload = asdict(result)
    run_metadata = result_payload.get("run_metadata", {})
    semantic_payload = {
        "strategy_version": result_payload.get("strategy_version"),
        "spec_sha256": result_payload.get("spec_sha256"),
        "strategy_parameters": run_metadata.get("strategy_parameters", {}),
        "input_fingerprint": result_payload.get("input_fingerprint"),
        "symbol": result_payload.get("symbol"),
        "instrument_type": result_payload.get("instrument_type"),
        "matrix_id": result_payload.get("matrix_group"),
        "add_variant": result_payload.get("add_variant"),
        "exit_variant": result_payload.get("exit_variant"),
        "execution_model": run_metadata.get("execution_model"),
        "cost_model": run_metadata.get("cost_model"),
        "slippage_bps": result_payload.get("slippage_bps"),
        "assumptions": result_payload.get("assumptions"),
        "decisions": result_payload.get("decisions"),
        "transactions": result_payload.get("transactions"),
        "legs": result_payload.get("legs"),
    }
    return canonical_fingerprint(semantic_payload)
