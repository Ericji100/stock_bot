"""Causal dual-moving-average research backtest core (frozen spec v0.4.1)."""

from .contracts import (
    COST_MODEL,
    EXECUTION_MODEL,
    SPEC_SHA256,
    STRATEGY_VERSION,
    ContractValidationError,
    InputAudit,
    RunManifest,
    canonical_fingerprint,
    semantic_result_fingerprint,
    validate_daily_inputs,
)
from .costs import CATHAY_WEB_028_TW_V1, CostModel, size_entry
from .engine import DualMABacktestEngine, run_matrix
from .models import (
    AddVariant,
    BacktestResult,
    Bar,
    ExitReason,
    ExitVariant,
    InstrumentType,
    SignalStatus,
    StrategyKind,
)

__all__ = [
    "AddVariant",
    "BacktestResult",
    "Bar",
    "CATHAY_WEB_028_TW_V1",
    "COST_MODEL",
    "ContractValidationError",
    "CostModel",
    "DualMABacktestEngine",
    "ExitReason",
    "ExitVariant",
    "EXECUTION_MODEL",
    "InputAudit",
    "InstrumentType",
    "RunManifest",
    "SPEC_SHA256",
    "SignalStatus",
    "STRATEGY_VERSION",
    "StrategyKind",
    "canonical_fingerprint",
    "semantic_result_fingerprint",
    "run_matrix",
    "size_entry",
    "validate_daily_inputs",
]
