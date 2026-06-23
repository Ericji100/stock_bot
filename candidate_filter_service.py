from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_HARD_FILTER_SETTINGS = {
    "min_price": 10.0,
    "max_price": 80.0,
    "min_avg_volume_20d": 500.0,
    "min_monthly_revenue": 40_000_000.0,
}


@dataclass(frozen=True)
class BasicHardFilterResult:
    passed: bool
    reasons: tuple[str, ...] = ()


def resolve_hard_filter_settings(
    scan_settings: dict[str, Any] | None = None,
    *,
    defaults: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, float]:
    settings = dict(defaults or DEFAULT_HARD_FILTER_SETTINGS)
    for source in (scan_settings or {}, overrides or {}):
        for key in DEFAULT_HARD_FILTER_SETTINGS:
            if key not in source:
                continue
            try:
                settings[key] = float(source[key])
            except (TypeError, ValueError):
                continue
    return {key: float(settings[key]) for key in DEFAULT_HARD_FILTER_SETTINGS}


def apply_basic_hard_filter(
    *,
    price: Any,
    avg_volume_20d: Any,
    latest_monthly_revenue: Any,
    settings: dict[str, Any] | None = None,
    require_revenue: bool = True,
) -> BasicHardFilterResult:
    resolved = resolve_hard_filter_settings(settings)
    reasons: list[str] = []
    price_value = _to_float(price)
    avg_volume_value = _to_float(avg_volume_20d)
    revenue_value = _to_float(latest_monthly_revenue)

    if price_value is None:
        reasons.append("missing_price")
    elif not (resolved["min_price"] <= price_value <= resolved["max_price"]):
        reasons.append("price_out_of_range")

    if avg_volume_value is None:
        reasons.append("missing_avg_volume_20d")
    elif avg_volume_value < resolved["min_avg_volume_20d"]:
        reasons.append("avg_volume_20d_below_min")

    if require_revenue:
        if revenue_value is None:
            reasons.append("missing_monthly_revenue")
        elif revenue_value < resolved["min_monthly_revenue"]:
            reasons.append("monthly_revenue_below_min")

    return BasicHardFilterResult(passed=not reasons, reasons=tuple(reasons))


def hard_filter_display_text(settings: dict[str, Any] | None = None) -> str:
    resolved = resolve_hard_filter_settings(settings)
    return (
        f"股價 {_format_number(resolved['min_price'])}~{_format_number(resolved['max_price'])} / "
        f"均量 >= {_format_number(resolved['min_avg_volume_20d'])} / "
        f"月營收 >= {_format_money(resolved['min_monthly_revenue'])}"
    )


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_money(value: float) -> str:
    if value >= 100_000_000 and value % 100_000_000 == 0:
        return f"{int(value / 100_000_000)}億"
    if value >= 10_000 and value % 10_000 == 0:
        return f"{int(value / 10_000)}萬"
    return f"{value:,.0f}"
