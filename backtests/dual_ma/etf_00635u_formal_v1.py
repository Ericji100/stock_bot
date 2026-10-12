"""Manifest-bound formal adapter/orchestrator for 00635U.

The module performs no network access.  Its caller must first pass the 00 SHA
gate in ``etf_00635u_prep_v1``.  Synthetic tests exercise the same surfaces.
"""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from . import etf_00635u_research_v1 as research
from .etf_00635u_strategy_v1 import (
    ETFResearchConfig,
    EntryPolicy,
    ExecutionTiming,
    ExitReason,
    IndicatorRow,
    RawETFRow,
    StrategyVersion,
    build_causal_indicator_rows,
    run_strategy,
)


REPORT_NAME = "README.md"
TRADE_REPORT_NAME = "trade-details.md"
STOP_CODE = "STOP_WAIT_00_00635U_REPORT_QA"
HIGH_REGIME_STOP_CODE = "STOP_WAIT_00_00635U_HIGH_REGIME_REPORT_QA"
MACD_ONLY_STOP_CODE = "STOP_WAIT_00_00635U_MACD_ONLY_REPORT_QA"
MACD_ONLY_CONTRACT = Path(__file__).with_name("00635u_macd_only_contract_v1.md")

LINE_LABELS = {
    "STRATEGY_A_SIGNAL_CLOSE": "策略 A（訊號日收盤成交）",
    "STRATEGY_A_NEXT_SESSION_OPEN": "策略 A（次一交易日開盤成交）",
    "STRATEGY_B_SIGNAL_CLOSE": "策略 B（訊號日收盤成交）",
    "STRATEGY_B_NEXT_SESSION_OPEN": "策略 B（次一交易日開盤成交）",
    "00635U_BUY_HOLD_CASH": "00635U 買進持有（現金保留）",
    "00635U_BUY_HOLD_REINVEST": "00635U 買進持有（分配再投入）",
    "GOLD_ER_OFFICIAL_TRACKED_INDEX": "官方追蹤 Gold ER 指數",
    "TAIEX_TOTAL_RETURN_SUPPLEMENT": "臺灣加權股價報酬指數（補充）",
    "STRICT_HIGH_STRATEGY_A_SIGNAL_CLOSE": "高檔限定策略 A（訊號日收盤成交）",
    "STRICT_HIGH_STRATEGY_A_NEXT_SESSION_OPEN": "高檔限定策略 A（次一交易日開盤成交）",
    "STRICT_HIGH_STRATEGY_B_SIGNAL_CLOSE": "高檔限定策略 B（訊號日收盤成交）",
    "STRICT_HIGH_STRATEGY_B_NEXT_SESSION_OPEN": "高檔限定策略 B（次一交易日開盤成交）",
    "MACD_ONLY_STRATEGY_A_SIGNAL_CLOSE": "純 MACD 策略 A（訊號日收盤成交）",
    "MACD_ONLY_STRATEGY_A_NEXT_SESSION_OPEN": "純 MACD 策略 A（次一交易日開盤成交）",
    "MACD_ONLY_STRATEGY_B_SIGNAL_CLOSE": "純 MACD 策略 B（訊號日收盤成交）",
    "MACD_ONLY_STRATEGY_B_NEXT_SESSION_OPEN": "純 MACD 策略 B（次一交易日開盤成交）",
}


def _active_line_id(
    strategy: StrategyVersion,
    timing: ExecutionTiming,
    entry_policy: EntryPolicy,
) -> str:
    prefix = {
        EntryPolicy.ORIGINAL: "STRATEGY",
        EntryPolicy.STRICT_HIGH_ONLY: "STRICT_HIGH_STRATEGY",
        EntryPolicy.MACD_ONLY: "MACD_ONLY_STRATEGY",
    }[entry_policy]
    return f"{prefix}_{strategy.name}_{timing.value}"

ENTRY_REASON_LABELS = {
    "HIGH_REGIME_MA21_BREAKOUT": "高價格區間 MA21 新突破",
    "LOW_REGIME_MACD_GREEN_TO_RED": "低價格區間 MACD 綠翻紅",
    "MACD_ONLY_GREEN_TO_RED": "MACD 柱狀值新綠翻紅",
}

EXIT_REASON_LABELS = {
    "INITIAL_PROTECTIVE_STOP": "固定初始防守",
    "MACD_RED_TO_GREEN": "MACD 紅翻綠",
    "KD_2R_DEATH_CROSS_EXIT": "淨成本 2R 啟動後的 KD 新死叉",
}

FINMIND_SNAPSHOT_ID = "00635U_FINMIND_PRIMARY_20150415_20261008_V3_D6735B7A5FF5"
FINMIND_PACKAGE_MANIFEST_SHA256 = "d6735b7a5ff568081f7606e3e8db43aca96f385271858fd96f4cb612e8bcfb95"
FINMIND_PACKAGE_READBACK_SHA256 = "fcfd1b30c2db5f46ad8f552e3b1c534a001d385c7ce2e98d3a2a7bea401784d2"


def _line_label(line_id: str, *, include_id: bool = False) -> str:
    label = LINE_LABELS.get(line_id, line_id)
    return f"{label}（`{line_id}`）" if include_id else label


def _strategy_label(value: object) -> str:
    raw = getattr(value, "name", value)
    if raw in {"A", StrategyVersion.A.value}:
        return "策略 A"
    if raw in {"B", StrategyVersion.B.value}:
        return "策略 B"
    return f"策略 {raw}"


def _money(value: object, *, signed: bool = False) -> str:
    spec = "+,.2f" if signed else ",.2f"
    return f"{format(research.decimal(value), spec)} 元"


def _percent(value: object, *, signed: bool = False) -> str:
    spec = "+.2f" if signed else ".2f"
    return f"{format(research.decimal(value) * 100, spec)}%"


def _percentage_points(value: object) -> str:
    points = (research.decimal(value) * 100).quantize(Decimal("0.01"))
    if points == 0:
        return "0.00 百分點"
    return f"{format(points, '+.2f')} 百分點"


def _absolute_percentage_points(value: object) -> str:
    points = abs(research.decimal(value) * 100).quantize(Decimal("0.01"))
    return f"{format(points, '.2f')} 百分點"


def _reason_label(row: Mapping[str, Any]) -> str:
    role = row.get("role")
    if role == "ENTRY":
        code = str(row.get("entry_type") or "UNKNOWN_ENTRY")
        label = ENTRY_REASON_LABELS.get(code, "未識別的進場原因")
    elif role == "EXIT":
        code = str(row.get("exit_reason") or "UNKNOWN_EXIT")
        label = EXIT_REASON_LABELS.get(code, "未識別的出場原因")
    else:
        code = str(row.get("event") or role or "UNKNOWN_EVENT")
        label = "其他帳務事件"
    return f"{label}（`{code}`）"


def _synthetic_trade_scope_note(trades: Sequence[Mapping[str, Any]]) -> str:
    by_line: dict[str, dict[str, int]] = {}
    for row in trades:
        role = str(row.get("role"))
        counts = by_line.setdefault(str(row.get("line_id")), {"ENTRY": 0, "EXIT": 0})
        if role in counts:
            counts[role] += 1
    entry_counts = {counts["ENTRY"] for counts in by_line.values()}
    exit_counts = {counts["EXIT"] for counts in by_line.values()}
    if by_line and len(entry_counts) == len(exit_counts) == 1:
        entries = next(iter(entry_counts))
        exits = next(iter(exit_counts))
        rounds = min(entries, exits)
        note = (
            f"此合成夾具每條策略線只建立 {rounds} 個完成回合；"
            f"{len(by_line)} 條策略線 ×（{entries} 次進場（ENTRY）＋"
            f"{exits} 次出場（EXIT））＝{len(trades)} 列事件。"
            f"這不是 {len(trades)} 筆獨立交易，也不是 00635U 正式上市歷史交易。"
        )
    else:
        note = f"此合成夾具共有 {len(trades)} 列進出場事件，不代表正式上市歷史交易。"
    entries_rows = [row for row in trades if row.get("role") == "ENTRY"]
    exits_rows = [row for row in trades if row.get("role") == "EXIT"]
    low_only = entries_rows and all(
        row.get("entry_type") == "LOW_REGIME_MACD_GREEN_TO_RED"
        for row in entries_rows
    )
    macd_exit_only = exits_rows and all(
        row.get("exit_reason") == "MACD_RED_TO_GREEN" for row in exits_rows
    )
    if low_only and macd_exit_only:
        rounds_text = next(iter(entry_counts)) if len(entry_counts) == 1 else "全部"
        note += (
            f"每條策略線的 {rounds_text} 個回合全屬低價格區間 MACD 綠翻紅進場、"
            "MACD 紅翻綠出場，因此策略 A 與 B 在這份合成夾具中必然相同；"
            "這不是正式資料得出的 A／B 相同結論。高價格區間、固定初始防守、"
            "淨成本 2R 啟動、KD(9,9,55) 新死叉等差異分支，由專屬單元測試驗證，"
            f"未在這 {len(trades)} 列中展示。"
        )
    return note


def _trade_report(
    trades: Sequence[Mapping[str, Any]],
    *,
    synthetic: bool,
    macd_audit: Sequence[Mapping[str, Any]] = (),
) -> str:
    banner = (
        "> **合成測試資料／不是正式績效。** 交易筆數刻意縮小，只驗證管線與規則；"
        "不得解讀為 00635U 上市以來的正式交易紀錄。\n"
        "> **SYNTHETIC TEST ONLY。**"
        if synthetic else
        "> **FORMAL／正式資料閘門輸出。** 請搭配主報告與資料清單（manifest）驗證。"
    )
    title = (
        "# 00635U 因果回測｜合成測試逐筆明細（非正式績效）"
        if synthetic else
        "# 00635U 因果回測｜正式逐筆交易明細"
    )
    scope_note = _synthetic_trade_scope_note(trades) if synthetic else ""
    rows = []
    for row in trades:
        role = row.get("role")
        entry_or_exit = "進場" if role == "ENTRY" else "出場" if role == "EXIT" else "其他"
        source_or_reason = _reason_label(row)
        rows.append(
            f"| {_line_label(str(row['line_id']))} | {entry_or_exit} | "
            f"{row.get('signal_date', '—')} | {row.get('fill_date', '—')} | "
            f"{'訊號日收盤' if row.get('timing') == ExecutionTiming.SIGNAL_CLOSE.value else '次一交易日開盤'} | "
            f"{source_or_reason} | `{row.get('position_id', '—')}` | {int(row.get('shares', 0)):,} | "
            f"{_money(row.get('reference_price', 0))} | {_money(row.get('fill_price', 0))} | "
            f"{_money(row.get('commission', 0))} | {_money(row.get('tax', 0))} | "
            f"{_money(row['initial_stop']) if row.get('initial_stop') is not None else '—'} | "
            f"{_money(row['initial_r']) if row.get('initial_r') is not None else '—'} | "
            f"{row.get('kd_2r_arm_date') or '—'} | "
            f"{_money(row['net_pnl'], signed=True) if row.get('net_pnl') is not None else '—'} |"
        )
    body = "\n".join(rows) if rows else "| — | — | — | — | — | — | — | 0 | — | — | — | — | — | — | — | — |"
    audit_section = ""
    if macd_audit:
        audit_rows = "\n".join(
            f"| {_line_label(str(item['line_id']))} | `{item['position_id']}` | "
            f"{item['entry_signal_date']} | {item['entry_prev_histogram']} → {item['entry_current_histogram']} | "
            f"{item['negative_segment_start']}～{item['negative_segment_end']}／{item['negative_segment_low']} | "
            f"{_money(item['initial_stop'])} | {_money(item['technical_r_per_share'])} | "
            f"{_money(item['fee_adjusted_initial_r'])} | {_money(item['fee_adjusted_two_r_threshold'])} | "
            f"{item['two_r_first_reached_date'] or '—'} | {item['exit_signal_date'] or '—'} | "
            f"{item['exit_reason']} | "
            f"{item['exit_prev_histogram'] if item['exit_prev_histogram'] != '' else '—'} → "
            f"{item['exit_current_histogram'] if item['exit_current_histogram'] != '' else '—'} | "
            f"{item['exit_prev_k'] if item['exit_prev_k'] != '' else '—'}／{item['exit_prev_d'] if item['exit_prev_d'] != '' else '—'} → "
            f"{item['exit_current_k'] if item['exit_current_k'] != '' else '—'}／{item['exit_current_d'] if item['exit_current_d'] != '' else '—'} |"
            for item in macd_audit
        )
        audit_section = (
            "\n\n## 純 MACD 逐部位因果欄位\n\n"
            "MA21／MA144 僅為診斷欄位；完整原始值另見 [純 MACD 因果稽核](macd-only-audit.csv)。\n\n"
            "| 策略 | 部位 ID | 進場訊號日 | 進場柱狀值前→當 | 負柱區段／最低價 | 防守價 | 每股技術 R | 費後初始 R | 費後 2R | 首次達 2R | 出場訊號日 | 出場原因 | 出場柱狀值前→當 | KD 前 K／D→當 K／D |\n"
            "|---|---|---|---|---|---:|---:|---:|---:|---|---|---|---|---|\n"
            + audit_rows
            + "\n"
        )
    return (
        title + "\n\n[返回主報告](README.md)\n\n" + banner + "\n\n"
        + (f"> **本夾具的交易範圍：** {scope_note}\n\n" if scope_note else "")
        + "金額均為新臺幣；原始精確值保留於 [trade-details.csv](trade-details.csv)。\n\n"
        "| 策略 | 角色 | 訊號日 | 成交日 | 成交時點 | 進場來源／出場理由 | 部位 ID | 股數 | 參考價 | 成交價 | 手續費 | 稅 | 初始防守 | 初始 R | 2R 啟動日 | 淨損益 |\n"
        "|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|\n"
        + body + "\n" + audit_section
    )


@dataclass(frozen=True)
class FormalSnapshotData:
    raw_rows: tuple[RawETFRow, ...]
    indicator_rows: tuple[IndicatorRow, ...]
    gold_er: tuple[research.IndexObservation, ...] | None
    taiex_total_return: tuple[research.IndexObservation, ...] | None = None


def _bool(value: Any, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"invalid boolean: {value!r}")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_finmind_formal_snapshot(
    package_root: Path,
    output_root: Path,
    acceptance_audit: Path,
) -> dict[str, str]:
    """Build a hash-pinned formal adapter without mutating the accepted package."""

    from . import etf_00635u_prep_v1 as prep

    package_root = package_root.resolve()
    output_root = output_root.resolve()
    package_manifest_path = package_root / "package_manifest.json"
    package_readback_path = package_root / "package_readback.json"
    if prep.sha256_file(package_manifest_path) != FINMIND_PACKAGE_MANIFEST_SHA256:
        raise ValueError("accepted FinMind package manifest SHA mismatch")
    if prep.sha256_file(package_readback_path) != FINMIND_PACKAGE_READBACK_SHA256:
        raise ValueError("accepted FinMind package readback SHA mismatch")
    package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
    for item in package_manifest["artifacts"]:
        path = package_root / item["relative_path"]
        if path.stat().st_size != item["bytes"] or prep.sha256_file(path).upper() != item["sha256"]:
            raise ValueError(f"accepted package artifact mismatch: {item['relative_path']}")
    package_readback = json.loads(package_readback_path.read_text(encoding="utf-8"))
    if package_readback.get("status") != "PASS" or package_readback.get("mismatch_count") != 0:
        raise ValueError("accepted package readback is not PASS")
    prices = _read_csv(package_root / "candidate" / "finmind_00635U_unadjusted_ohlcv.csv")
    taiex = _read_csv(package_root / "candidate" / "finmind_taiex_total_return.csv")
    actions = _read_csv(package_root / "candidate" / "finmind_00635U_corporate_action_candidates.csv")
    suspensions = _read_csv(package_root / "candidate" / "finmind_00635U_suspension_candidates.csv")
    if len(prices) != 2804 or prices[0]["date"] != "2015-04-15" or prices[-1]["date"] != "2026-10-08":
        raise ValueError("FinMind 00635U price pin changed")
    if actions or suspensions:
        raise ValueError("formal adapter refuses undeclared corporate actions or suspensions")
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"refusing to overwrite formal snapshot: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    etf_rows = [{
        "date": row["date"], "open": row["open"], "high": row["high"],
        "low": row["low"], "close": row["close"], "volume": row["volume"],
        "official_session": True, "tradable": True, "tradability_reason": "",
        "price_rebase_factor": 1, "share_factor": 1,
        "cash_dividend_per_share": 0, "dividend_event_id": "", "dividend_stage": "",
    } for row in prices]
    taiex_rows = [{
        "source_date": row["date"], "available_date": row["date"],
        "value": row["price"], "source_timezone": "Asia/Taipei",
    } for row in taiex]
    prep._write_csv(output_root / "etf-daily-causal.csv", etf_rows)
    prep._write_csv(output_root / "taiex-total-return.csv", taiex_rows)
    shutil.copy2(package_manifest_path, output_root / "source-package-manifest.json")
    shutil.copy2(package_readback_path, output_root / "source-package-readback.json")
    shutil.copy2(acceptance_audit, output_root / "acceptance-audit.md")
    files = [
        ("etf_daily_causal", "etf-daily-causal.csv"),
        ("taiex_total_return", "taiex-total-return.csv"),
        ("source_package_manifest", "source-package-manifest.json"),
        ("source_package_readback", "source-package-readback.json"),
        ("acceptance_audit", "acceptance-audit.md"),
    ]
    manifest = {
        "schema": prep.MANIFEST_SCHEMA, "symbol": "00635U",
        "listing_date": "2015-04-15", "immutable": True,
        "data_classification": "FORMAL_ACCEPTED_SNAPSHOT",
        "snapshot_id": FINMIND_SNAPSHOT_ID,
        "source": "FINMIND_PRIMARY_WITH_DECLARED_OFFICIAL_GAPS",
        "source_package_manifest_sha256": FINMIND_PACKAGE_MANIFEST_SHA256,
        "source_package_readback_sha256": FINMIND_PACKAGE_READBACK_SHA256,
        "gold_er_status": "OMITTED_NO_SP_GSCI_GOLD_ER_OFFICIAL_HISTORY",
        "corporate_action_candidates": 0, "suspension_candidates": 0,
        "official_cross_confirmation": False,
        "files": [
            {"role": role, "path": name, "sha256": prep.sha256_file(output_root / name)}
            for role, name in files
        ],
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_bytes(prep._canonical_bytes(manifest) + b"\n")
    return {"manifest": str(manifest_path), "manifest_sha256": prep.sha256_file(manifest_path)}


def load_formal_snapshot(manifest_path: Path) -> FormalSnapshotData:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_role = {item.get("role"): item for item in manifest.get("files", [])}
    required = {"etf_daily_causal"}
    missing = sorted(required - set(by_role))
    if missing:
        raise ValueError(f"formal manifest missing roles: {missing}")
    root = manifest_path.parent
    raw_rows = []
    for item in _read_csv(root / by_role["etf_daily_causal"]["path"]):
        if Decimal(item.get("cash_dividend_per_share") or "0") != 0 and not item.get("dividend_stage"):
            raise ValueError("FORMAL rejects legacy unstaged cash dividend rows")
        raw_rows.append(RawETFRow(
            day=date.fromisoformat(item["date"]),
            open=None if item.get("open") in (None, "") else Decimal(item["open"]),
            high=Decimal(item["high"]), low=Decimal(item["low"]), close=Decimal(item["close"]),
            volume=Decimal(item.get("volume") or "0"),
            official_session=_bool(item.get("official_session"), True),
            tradable=_bool(item.get("tradable"), True),
            tradability_reason=item.get("tradability_reason", ""),
            price_rebase_factor=Decimal(item.get("price_rebase_factor") or "1"),
            share_factor=Decimal(item.get("share_factor") or "1"),
            cash_dividend_per_share=Decimal(item.get("cash_dividend_per_share") or "0"),
            dividend_event_id=item.get("dividend_event_id", ""),
            dividend_stage=item.get("dividend_stage", ""),
        ))
    gold = tuple(
        research.IndexObservation(
            source_date=date.fromisoformat(item["source_date"]),
            available_date=date.fromisoformat(item["available_date"]),
            value=Decimal(item["value"]),
            source_timezone=item["source_timezone"],
        )
        for item in _read_csv(root / by_role["gold_er_official"]["path"])
    ) if "gold_er_official" in by_role else None
    taiex = None
    if "taiex_total_return" in by_role:
        taiex = tuple(
            research.IndexObservation(
                source_date=date.fromisoformat(item["source_date"]),
                available_date=date.fromisoformat(item["available_date"]),
                value=Decimal(item["value"]),
                source_timezone=item["source_timezone"],
            )
            for item in _read_csv(root / by_role["taiex_total_return"]["path"])
        )
    return FormalSnapshotData(
        raw_rows=tuple(raw_rows),
        indicator_rows=tuple(build_causal_indicator_rows(raw_rows)),
        gold_er=gold,
        taiex_total_return=taiex,
    )


def run_formal_analysis(
    data: FormalSnapshotData,
    *,
    include_strict_high_variants: bool = False,
    include_macd_only_variants: bool = False,
) -> dict[str, Any]:
    if include_strict_high_variants and include_macd_only_variants:
        raise ValueError("study variants must run in separate isolated packs")
    rows = data.indicator_rows
    has_variant = include_strict_high_variants or include_macd_only_variants
    if data.gold_er is not None and not has_variant:
        lines = research.run_seven_lines(
            rows, data.gold_er, gold_alignment_policy="LAST_KNOWN_PUBLISHED"
        )
        gold_status = "AVAILABLE_OFFICIAL"
    else:
        raw_lines = []
        policies = (EntryPolicy.ORIGINAL,)
        if include_strict_high_variants:
            policies += (EntryPolicy.STRICT_HIGH_ONLY,)
        elif include_macd_only_variants:
            policies += (EntryPolicy.MACD_ONLY,)
        for entry_policy in policies:
            for strategy in StrategyVersion:
                for timing in ExecutionTiming:
                    result = run_strategy(
                        rows, ETFResearchConfig(
                            strategy=strategy, timing=timing, entry_policy=entry_policy
                        )
                    )
                    raw_lines.append(research.ComparisonLine(
                        _active_line_id(strategy, timing, entry_policy),
                        "ACTIVE_STRATEGY",
                        tuple((item["date"], item["equity"]) for item in result.state.daily),
                        {"trades": tuple(result.state.trades), "events": tuple(result.state.events)},
                    ))
        raw_lines.extend((
            research.run_buy_hold(rows, reinvest=False),
            research.run_buy_hold(rows, reinvest=True),
        ))
        common_dates = sorted(set.intersection(*(
            {day for day, _value in line.curve} for line in raw_lines
        )))
        lines = tuple(research.ComparisonLine(
            line.line_id,
            line.role,
            tuple(
                (day, research.INITIAL_CAPITAL * dict(line.curve)[day] / dict(line.curve)[common_dates[0]])
                for day in common_dates
            ),
            {
                **dict(line.audit),
                "comparison_start": common_dates[0],
                "comparison_end": common_dates[-1],
                "comparison_observations": len(common_dates),
                "rebased_to": research.INITIAL_CAPITAL,
            },
        ) for line in raw_lines)
        gold_status = "OMITTED_NO_SP_GSCI_GOLD_ER_OFFICIAL_HISTORY"
    line_metrics = {}
    for line in lines:
        metrics = research.performance_metrics(line.curve, line.audit.get("trades", ()))
        if line.role == "TRADABLE_PASSIVE_COMPARATOR":
            transactions = line.audit["transactions"]
            metrics.update({
                "closed_positions": 0,
                "open_positions": 1,
                "maximum_single_entry_cash": max(
                    (research.decimal(item["cash_amount"]) for item in transactions),
                    default=Decimal("0"),
                ),
            })
        line_metrics[line.line_id] = metrics
    active: dict[str, Any] = {}
    line_by_id = {line.line_id: line for line in lines}
    policies = (EntryPolicy.ORIGINAL,)
    if include_strict_high_variants:
        policies += (EntryPolicy.STRICT_HIGH_ONLY,)
    elif include_macd_only_variants:
        policies += (EntryPolicy.MACD_ONLY,)
    for entry_policy in policies:
      for strategy in StrategyVersion:
        for timing in ExecutionTiming:
            result = run_strategy(rows, ETFResearchConfig(
                strategy=strategy, timing=timing, entry_policy=entry_policy
            ))
            line_id = _active_line_id(strategy, timing, entry_policy)
            curve = tuple((item["date"], item["equity"]) for item in result.state.daily)
            metrics = research.performance_metrics(curve, result.state.trades)
            active[line_id] = {
                "result": result,
                "metrics": research.performance_metrics(curve, result.state.trades),
                "annual": research.annual_slices(curve),
                "rolling": {years: research.rolling_windows(curve, years) for years in (3, 5, 10)},
                "regime": research.regime_summary(result.state.daily),
                "stability": research.trade_stability(result.state.trades),
                "path": research.path_diagnostics(curve),
            }
            comparison_line = line_by_id[line_id]
            start = comparison_line.audit["comparison_start"]
            end = comparison_line.audit["comparison_end"]
            comparison_trades = [
                item for item in result.state.trades
                if start <= item["fill_date"] <= end
            ]
            active[line_id]["metrics"] = research.performance_metrics(
                comparison_line.curve, comparison_trades
            )
            active[line_id]["comparison_trades"] = comparison_trades
            line_metrics[line_id] = active[line_id]["metrics"]
    benchmark_ids = ["00635U_BUY_HOLD_CASH", "00635U_BUY_HOLD_REINVEST"]
    if gold_status == "AVAILABLE_OFFICIAL":
        benchmark_ids.append("GOLD_ER_OFFICIAL_TRACKED_INDEX")
    relative = {
        line_id: {
            benchmark_id: research.relative_to_benchmark(
                payload["metrics"], line_metrics[benchmark_id]
            )
            for benchmark_id in benchmark_ids
        }
        for line_id, payload in active.items()
    }
    taiex_line = None
    taiex_status = "HOLD_MISSING_TAIEX_TOTAL_RETURN_ROLE"
    if data.taiex_total_return is not None:
        taiex_line = research.run_index_line(
            data.taiex_total_return,
            [row.day for row in rows],
            line_id="TAIEX_TOTAL_RETURN_SUPPLEMENT",
            alignment_policy="LAST_KNOWN_PUBLISHED",
        )
        taiex_status = "AVAILABLE_SUPPLEMENT_ONLY"
    return {
        "lines": lines,
        "line_metrics": line_metrics,
        "active": active,
        "relative_benchmarks": relative,
        "cost_sensitivity": research.cost_sensitivity(
            rows,
            entry_policy=(
                EntryPolicy.STRICT_HIGH_ONLY
                if include_strict_high_variants else EntryPolicy.ORIGINAL
                if not include_macd_only_variants else EntryPolicy.MACD_ONLY
            ),
        ),
        "taiex_supplement": taiex_line,
        "taiex_status": taiex_status,
        "gold_status": gold_status,
        "include_strict_high_variants": include_strict_high_variants,
        "include_macd_only_variants": include_macd_only_variants,
    }


def low_regime_entry_exclusion_audit(
    data: FormalSnapshotData,
    analysis: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Trace original entries excluded by the strict contemporaneous MA gate."""

    rows_by_day = {row.day: row for row in data.indicator_rows}
    audit_rows: list[dict[str, Any]] = []
    for strategy in StrategyVersion:
        for timing in ExecutionTiming:
            original_id = _active_line_id(strategy, timing, EntryPolicy.ORIGINAL)
            strict_id = _active_line_id(strategy, timing, EntryPolicy.STRICT_HIGH_ONLY)
            original_trades = analysis["active"][original_id]["result"].state.trades
            strict_entry_dates = {
                item["signal_date"]
                for item in analysis["active"][strict_id]["result"].state.trades
                if item["role"] == "ENTRY"
            }
            exits = {
                item["position_id"]: item
                for item in original_trades if item["role"] == "EXIT"
            }
            for entry in original_trades:
                if entry["role"] != "ENTRY":
                    continue
                signal_row = rows_by_day[entry["signal_date"]]
                if signal_row.sma21 > signal_row.sma144:
                    continue
                exit_row = exits.get(entry["position_id"])
                audit_rows.append({
                    "signal_date": entry["signal_date"],
                    "strategy": strategy.name,
                    "timing": timing.value,
                    "original_line_id": original_id,
                    "strict_line_id": strict_id,
                    "original_entry_reason": entry["entry_type"],
                    "ma21": signal_row.sma21,
                    "ma144": signal_row.sma144,
                    "gate_relation": "EQUAL" if signal_row.sma21 == signal_row.sma144 else "LESS_THAN",
                    "excluded_from_strict_variant": entry["signal_date"] not in strict_entry_dates,
                    "original_position_id": entry["position_id"],
                    "original_fill_date": entry["fill_date"],
                    "original_exit_date": "" if exit_row is None else exit_row["fill_date"],
                    "original_exit_reason": "OPEN" if exit_row is None else exit_row["exit_reason"],
                    "original_net_pnl": "" if exit_row is None else exit_row["net_pnl"],
                    "causal_fields": "SIGNAL_DATE_MA21_MA144_ONLY",
                })
    return audit_rows


def _macd_only_ma_invariance_check(data: FormalSnapshotData) -> None:
    """Fail closed unless changing only MA diagnostics leaves decisions identical."""

    mutated = tuple(
        replace(
            row,
            sma21=(None if index % 2 else Decimal("1000000")),
            sma144=(None if index % 2 else Decimal("0.0001")),
        )
        for index, row in enumerate(data.indicator_rows)
    )
    ignored_trade_fields = {"entry_sma21_diagnostic", "entry_sma144_diagnostic"}
    daily_fields = (
        "date", "cash", "liquidation_value", "equity", "shares", "pending",
        "signal_ready", "signal_observable", "open_execution",
    )
    for strategy in StrategyVersion:
        for timing in ExecutionTiming:
            config = ETFResearchConfig(
                strategy=strategy,
                timing=timing,
                entry_policy=EntryPolicy.MACD_ONLY,
            )
            baseline = run_strategy(data.indicator_rows, config).state
            changed = run_strategy(mutated, config).state
            normalize_trades = lambda records: [
                {key: value for key, value in item.items() if key not in ignored_trade_fields}
                for item in records
            ]
            if normalize_trades(baseline.trades) != normalize_trades(changed.trades):
                raise AssertionError("MACD-only orders changed when only MA diagnostics changed")
            if baseline.events != changed.events:
                raise AssertionError("MACD-only events changed when only MA diagnostics changed")
            baseline_daily = [tuple(item[key] for key in daily_fields) for item in baseline.daily]
            changed_daily = [tuple(item[key] for key in daily_fields) for item in changed.daily]
            if baseline_daily != changed_daily:
                raise AssertionError("MACD-only account changed when only MA diagnostics changed")


def macd_only_audit(
    data: FormalSnapshotData,
    analysis: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build one reconstructable causal audit row per MACD-only position."""

    _macd_only_ma_invariance_check(data)
    observable = [
        row for row in data.indicator_rows
        if row.official_session and row.tradable and row.volume > 0
    ]
    rows_by_day = {row.day: row for row in observable}
    previous_by_day = {
        current.day: previous for previous, current in zip(observable, observable[1:])
    }
    output: list[dict[str, Any]] = []
    for strategy in StrategyVersion:
        for timing in ExecutionTiming:
            line_id = _active_line_id(strategy, timing, EntryPolicy.MACD_ONLY)
            result = analysis["active"][line_id]["result"]
            exits = {
                item["position_id"]: item
                for item in result.state.trades if item["role"] == "EXIT"
            }
            arm_events = {
                item["position_id"]: item
                for item in result.state.events
                if item["event"] == "KD_2R_PERMANENTLY_ARMED"
            }
            for entry in (item for item in result.state.trades if item["role"] == "ENTRY"):
                signal = rows_by_day[entry["signal_date"]]
                prior = previous_by_day[entry["signal_date"]]
                exit_trade = exits.get(entry["position_id"])
                exit_signal = (
                    None if exit_trade is None else rows_by_day[exit_trade["signal_date"]]
                )
                exit_prior = (
                    None if exit_trade is None else previous_by_day[exit_trade["signal_date"]]
                )
                arm = arm_events.get(entry["position_id"])
                expected_stop = min(
                    entry["negative_segment_low"],
                    prior.technical_low,
                    signal.technical_low,
                )
                entry_cross_valid = (
                    prior.macd_histogram <= 0 and signal.macd_histogram > 0
                )
                if not entry_cross_valid:
                    raise AssertionError("MACD-only entry is not a fresh green-to-red cross")
                if entry["negative_segment_low"] is None or entry["initial_stop"] != expected_stop:
                    raise AssertionError("MACD-only initial stop is not the negative-segment minimum")
                if entry["initial_r"] <= 0:
                    raise AssertionError("MACD-only initial R must be positive")
                exit_reason = "OPEN" if exit_trade is None else exit_trade["exit_reason"]
                macd_exit_cross = (
                    exit_trade is not None
                    and exit_prior.macd_histogram >= 0
                    and exit_signal.macd_histogram < 0
                )
                kd_exit_cross = (
                    exit_trade is not None
                    and exit_prior.k >= exit_prior.d
                    and exit_signal.k < exit_signal.d
                )
                if strategy == StrategyVersion.A and exit_reason == ExitReason.MACD.value and not macd_exit_cross:
                    raise AssertionError("strategy A exit is not a fresh red-to-green cross")
                if strategy == StrategyVersion.B and exit_reason == ExitReason.MACD.value:
                    raise AssertionError("strategy B must not use MACD exits")
                if strategy == StrategyVersion.B and exit_reason == ExitReason.KD_2R.value:
                    if arm is None or not kd_exit_cross or arm["date"] > exit_trade["signal_date"]:
                        raise AssertionError("strategy B KD exit lacks causal permanent 2R arming")
                output.append({
                    "line_id": line_id,
                    "strategy": strategy.name,
                    "timing": timing.value,
                    "position_id": entry["position_id"],
                    "entry_signal_date": entry["signal_date"],
                    "entry_fill_date": entry["fill_date"],
                    "entry_prev_histogram": prior.macd_histogram,
                    "entry_current_histogram": signal.macd_histogram,
                    "entry_cross_valid": entry_cross_valid,
                    "negative_segment_start": entry["negative_segment_start"],
                    "negative_segment_end": entry["negative_segment_end"],
                    "negative_segment_low": entry["negative_segment_low"],
                    "previous_day_low": prior.technical_low,
                    "signal_day_low": signal.technical_low,
                    "initial_stop": entry["initial_stop"],
                    "technical_entry": entry["technical_entry"],
                    "technical_r_per_share": entry["technical_r_per_share"],
                    "fee_adjusted_initial_r": entry["initial_r"],
                    "fee_adjusted_two_r_threshold": entry["initial_r"] * Decimal("2"),
                    "two_r_first_reached_date": "" if arm is None else arm["date"],
                    "two_r_net_pnl_at_arm": "" if arm is None else arm["net_pnl"],
                    "permanently_armed": arm is not None,
                    "exit_signal_date": "" if exit_trade is None else exit_trade["signal_date"],
                    "exit_fill_date": "" if exit_trade is None else exit_trade["fill_date"],
                    "exit_reason": exit_reason,
                    "exit_prev_histogram": "" if exit_prior is None else exit_prior.macd_histogram,
                    "exit_current_histogram": "" if exit_signal is None else exit_signal.macd_histogram,
                    "macd_new_red_to_green": macd_exit_cross,
                    "exit_prev_k": "" if exit_prior is None else exit_prior.k,
                    "exit_prev_d": "" if exit_prior is None else exit_prior.d,
                    "exit_current_k": "" if exit_signal is None else exit_signal.k,
                    "exit_current_d": "" if exit_signal is None else exit_signal.d,
                    "kd_new_death_cross": kd_exit_cross,
                    "entry_ma21_diagnostic": signal.sma21,
                    "entry_ma144_diagnostic": signal.sma144,
                    "ma_decision_role": "DIAGNOSTIC_ONLY_NOT_USED",
                    "ma_invariance_replay": "PASS_ORDERS_EVENTS_ACCOUNTS_IDENTICAL",
                })
    return output


def run_formal_pack(
    manifest_path: Path,
    output_root: Path,
    *,
    accepted_manifest_sha256: str,
    high_regime_study: bool = False,
    macd_only_study: bool = False,
) -> dict[str, Any]:
    from . import etf_00635u_prep_v1 as prep

    if high_regime_study and macd_only_study:
        raise ValueError("study variants must use separate output packs")
    input_manifest = prep.validate_formal_snapshot_gate(
        manifest_path,
        accepted_manifest_sha256=accepted_manifest_sha256,
        accepted_by="00",
    )
    input_classification = input_manifest.get(
        "data_classification", "FORMAL_ACCEPTED_SNAPSHOT"
    )
    synthetic_test = input_classification == "SYNTHETIC_TEST_ONLY"
    data = load_formal_snapshot(manifest_path)
    analysis = run_formal_analysis(
        data,
        include_strict_high_variants=high_regime_study,
        include_macd_only_variants=macd_only_study,
    )
    macd_audit_rows = macd_only_audit(data, analysis) if macd_only_study else []
    stop_code = (
        HIGH_REGIME_STOP_CODE if high_regime_study else
        MACD_ONLY_STOP_CODE if macd_only_study else STOP_CODE
    )
    study_entry_policy = (
        EntryPolicy.STRICT_HIGH_ONLY if high_regime_study else
        EntryPolicy.MACD_ONLY if macd_only_study else EntryPolicy.ORIGINAL
    )
    output_root.mkdir(parents=True, exist_ok=True)
    summary = [
        {"line_id": line_id, **metrics}
        for line_id, metrics in analysis["line_metrics"].items()
    ]
    prep._write_csv(output_root / "formal-summary.csv", summary)
    daily = [
        {"line_id": line.line_id, "date": day, "equity": equity}
        for line in analysis["lines"] for day, equity in line.curve
    ]
    prep._write_csv(output_root / "daily-equity.csv", daily)
    trades, events, annual, rolling = [], [], [], []
    macd_audit_by_position = {
        (row["line_id"], row["position_id"]): row for row in macd_audit_rows
    }
    for line_id, payload in analysis["active"].items():
        for row in payload["result"].state.trades:
            trade = {"line_id": line_id, **row}
            audit = macd_audit_by_position.get((line_id, row.get("position_id")))
            if audit is not None:
                trade.update({
                    f"macd_audit_{key}": value
                    for key, value in audit.items()
                    if key not in {"line_id", "strategy", "timing", "position_id"}
                })
            trades.append(trade)
        events.extend({"line_id": line_id, **row} for row in payload["result"].state.events)
        annual.extend({"line_id": line_id, **row} for row in payload["annual"])
        for years, records in payload["rolling"].items():
            rolling.extend({"line_id": line_id, **row} for row in records)
    if macd_only_study:
        macd_daily_accounts = [
            {"line_id": line_id, **row}
            for line_id, payload in analysis["active"].items()
            if line_id.startswith("MACD_ONLY_")
            for row in payload["result"].state.daily
        ]
        prep._write_csv(
            output_root / "macd-only-account-daily.csv", macd_daily_accounts
        )
    prep._write_csv(output_root / "trade-details.csv", trades or [{"status": "NO_TRADES"}])
    (output_root / TRADE_REPORT_NAME).write_text(
        _trade_report(trades, synthetic=synthetic_test, macd_audit=macd_audit_rows),
        encoding="utf-8",
    )
    prep._write_csv(output_root / "event-ledger.csv", events or [{"status": "NO_EVENTS"}])
    prep._write_csv(output_root / "annual-performance.csv", annual)
    prep._write_csv(output_root / "rolling-windows.csv", rolling)
    prep._write_csv(output_root / "cost-sensitivity.csv", analysis["cost_sensitivity"])
    index_line = next(
        (line for line in analysis["lines"] if line.line_id == "GOLD_ER_OFFICIAL_TRACKED_INDEX"),
        None,
    )
    prep._write_csv(
        output_root / "index-alignment-ledger.csv",
        list(index_line.audit["alignment_ledger"])
        if index_line is not None else
        [{"status": "OMITTED_NO_SP_GSCI_GOLD_ER_OFFICIAL_HISTORY"}],
    )
    relative_rows = [
        {"line_id": line_id, "benchmark_id": benchmark_id, **values}
        for line_id, benchmarks in analysis["relative_benchmarks"].items()
        for benchmark_id, values in benchmarks.items()
    ]
    prep._write_csv(output_root / "relative-benchmark.csv", relative_rows)
    taiex_rows = [{"status": analysis["taiex_status"]}]
    if analysis["taiex_supplement"] is not None:
        taiex_rows = [
            {"status": analysis["taiex_status"], "date": day, "equity": value}
            for day, value in analysis["taiex_supplement"].curve
        ]
    prep._write_csv(output_root / "taiex-supplement.csv", taiex_rows)
    exclusion_rows = (
        low_regime_entry_exclusion_audit(data, analysis)
        if high_regime_study else []
    )
    if high_regime_study:
        prep._write_csv(
            output_root / "low-regime-entry-exclusion-audit.csv",
            exclusion_rows or [{"status": "NO_EXCLUDED_ORIGINAL_ENTRIES"}],
        )
    if macd_only_study:
        prep._write_csv(
            output_root / "macd-only-audit.csv",
            macd_audit_rows or [{"status": "NO_MACD_ONLY_POSITIONS"}],
        )
        shutil.copyfile(MACD_ONLY_CONTRACT, output_root / "strategy-rule-contract.md")
    diagnostic_rows = []
    for line_id, payload in analysis["active"].items():
        line = next(item for item in analysis["lines"] if item.line_id == line_id)
        counterfactuals = {
            count: research.trade_removal_cash_counterfactual(
                line.curve, payload["comparison_trades"], count
            )
            for count in (1, 3, 5)
        }
        diagnostic_rows.append({
            "line_id": line_id, **payload["stability"], **payload["path"],
            **{
                f"remove_best_{count}_{key}": value
                for count, result in counterfactuals.items()
                for key, value in result.items()
            },
        })
    prep._write_csv(output_root / "path-and-concentration.csv", diagnostic_rows)
    fixed_periods = (
        ("2015_2019", date(2015, 4, 15), date(2019, 12, 31)),
        ("2020_2022", date(2020, 1, 1), date(2022, 12, 31)),
        ("2023_LATEST", date(2023, 1, 1), data.indicator_rows[-1].day),
    )
    fixed_rows = []
    for line in analysis["lines"]:
        for period_id, start, end in fixed_periods:
            fixed_rows.append({"line_id": line.line_id, "period_id": period_id, **research.slice_curve(line.curve, start, end)})
    prep._write_csv(output_root / "fixed-periods.csv", fixed_rows)
    grid_rows = []
    for strategy in StrategyVersion:
        for parameters in research.parameter_grid():
            variant = build_causal_indicator_rows(
                data.raw_rows,
                ma_fast=parameters[0], ma_slow=parameters[1],
                macd_fast=parameters[2], macd_slow=parameters[3], macd_signal=parameters[4],
            )
            result = run_strategy(
                variant,
                ETFResearchConfig(
                    strategy=strategy,
                    timing=ExecutionTiming.NEXT_SESSION_OPEN,
                    entry_policy=study_entry_policy,
                ),
            )
            curve = tuple((item["date"], item["equity"]) for item in result.state.daily)
            metrics = research.performance_metrics(curve, result.state.trades)
            rolling_5 = research.rolling_windows(curve, 5)
            valid_5 = [item["cagr"] for item in rolling_5 if item.get("status") == "OK"]
            grid_rows.append({
                "strategy": strategy.name,
                "ma_fast": parameters[0], "ma_slow": parameters[1],
                "macd_fast": parameters[2], "macd_slow": parameters[3], "macd_signal": parameters[4],
                "kd": "9/9/55", "two_r": "2R",
                "ending_equity": result.final_equity,
                "total_return": metrics["total_return"],
                "cagr": metrics["cagr"],
                "max_drawdown": metrics["max_drawdown"],
                "return_drawdown_ratio": (
                    None
                    if metrics["max_drawdown"] == 0
                    else metrics["cagr"] / abs(metrics["max_drawdown"])
                ),
                "rolling_5y_worst_cagr": min(valid_5) if valid_5 else None,
            })
    prep._write_csv(output_root / "parameter-grid.csv", grid_rows)
    grid_summary_rows = [
        {"strategy": strategy.name, **research.parameter_grid_summary([
            row for row in grid_rows if row["strategy"] == strategy.name
        ])}
        for strategy in StrategyVersion
    ]
    prep._write_csv(output_root / "parameter-grid-summary.csv", grid_summary_rows)
    core_rows, core_summary, core_ledger = [], [], []
    for strategy in StrategyVersion:
        allocation = research.run_core_allocation_study(
            data.indicator_rows,
            strategy=strategy,
            timing=ExecutionTiming.NEXT_SESSION_OPEN,
            entry_policy=study_entry_policy,
        )
        core_rows.extend({"strategy": strategy.name, **row} for row in allocation["daily"])
        core_summary.extend({"strategy": strategy.name, **row} for row in allocation["summary"])
        core_ledger.extend({"strategy": strategy.name, **row} for row in allocation["ledger"])
    prep._write_csv(output_root / "core-allocation-daily.csv", core_rows)
    prep._write_csv(output_root / "core-allocation-summary.csv", core_summary)
    prep._write_csv(output_root / "core-allocation-ledger.csv", core_ledger or [{"status": "NO_TRANSACTIONS"}])
    buy_hold_cash_line = next(
        line for line in analysis["lines"] if line.line_id == "00635U_BUY_HOLD_CASH"
    )
    chart_active_lines = (
        [line for line in analysis["lines"] if line.line_id.startswith("STRICT_HIGH_")]
        if high_regime_study else
        [line for line in analysis["lines"] if line.line_id.startswith("MACD_ONLY_")]
        if macd_only_study else
        list(analysis["lines"][:4])
    )
    main_chart_lines = [*chart_active_lines, buy_hold_cash_line]
    buy_hold_chart_label = "00635U 買進持有（現金保留／分配再投入，本輪重合）"
    equity_series = {
        (
            buy_hold_chart_label
            if line.line_id == "00635U_BUY_HOLD_CASH"
            else _line_label(line.line_id)
        ): [value for _day, value in line.curve]
        for line in main_chart_lines
    }
    drawdown_series = {}
    for line in main_chart_lines:
        peak, values = Decimal("0"), []
        for _day, value in line.curve:
            peak = max(peak, value)
            values.append(value / peak - 1 if peak else Decimal("0"))
        drawdown_series[
            buy_hold_chart_label
            if line.line_id == "00635U_BUY_HOLD_CASH"
            else _line_label(line.line_id)
        ] = values
    annual_series = {
        _line_label(line_id): [item["return"] for item in payload["annual"]]
        for line_id, payload in analysis["active"].items()
        if (
            line_id.startswith("STRICT_HIGH_") if high_regime_study else
            line_id.startswith("MACD_ONLY_") if macd_only_study else True
        )
    }
    rolling_series = {
        _line_label(line_id): [item["cagr"] for item in payload["rolling"][5] if item.get("status") == "OK"]
        for line_id, payload in analysis["active"].items()
        if (
            line_id.startswith("STRICT_HIGH_") if high_regime_study else
            line_id.startswith("MACD_ONLY_") if macd_only_study else True
        )
    }
    regime_series = {
        _line_label(line_id): [item["compounded_return"] for item in payload["regime"]]
        for line_id, payload in analysis["active"].items()
        if (
            line_id.startswith("STRICT_HIGH_") if high_regime_study else
            line_id.startswith("MACD_ONLY_") if macd_only_study else True
        )
    }
    trade_series = {
        _line_label(line_id): [item["net_pnl"] for item in payload["comparison_trades"] if item["role"] == "EXIT"]
        for line_id, payload in analysis["active"].items()
        if (
            line_id.startswith("STRICT_HIGH_") if high_regime_study else
            line_id.startswith("MACD_ONLY_") if macd_only_study else True
        )
    }
    cost_series = {
        _strategy_label(strategy): [
            item["ending_equity"] for item in analysis["cost_sensitivity"]
            if item["strategy"] == strategy.value and item["timing"] == ExecutionTiming.NEXT_SESSION_OPEN.value
        ] for strategy in StrategyVersion
    }
    grid_series = {
        _strategy_label(strategy): [item["ending_equity"] for item in grid_rows if item["strategy"] == strategy.name]
        for strategy in StrategyVersion
    }
    core_series = {
        _strategy_label(strategy): [item["ending_equity"] for item in core_summary if item["strategy"] == strategy.name]
        for strategy in StrategyVersion
    }
    daily_tick_labels = [row.day.isoformat() for row in data.indicator_rows]
    annual_tick_labels = [
        str(item["end"].year)
        for item in next(iter(analysis["active"].values()))["annual"]
    ]
    rolling_tick_labels = [
        f"{item['start'].year}–{item['end'].year}"
        for item in next(iter(analysis["active"].values()))["rolling"][5]
        if item.get("status") == "OK"
    ]
    trade_tick_labels = [
        str(index + 1) for index in range(max((len(values) for values in trade_series.values()), default=1))
    ]
    chart_specs = {
        "equity-chart.svg": ("equity_curve", "合成測試資產曲線（非正式績效）" if synthetic_test else "正式資產曲線", equity_series, "交易日期", "資產（元）", daily_tick_labels),
        "drawdown-chart.svg": ("drawdown", "合成測試回撤路徑（非正式績效）" if synthetic_test else "正式回撤路徑", drawdown_series, "交易日期", "回撤率", daily_tick_labels),
        "annual-chart.svg": ("annual_return", "合成測試逐年報酬（非正式績效）" if synthetic_test else "正式逐年報酬", annual_series, "年度", "報酬率", annual_tick_labels),
        "rolling-window-chart.svg": ("rolling_window", "五年滾動年化報酬", rolling_series, "窗口起訖年", "年化報酬率", rolling_tick_labels),
        "regime-chart.svg": ("regime", "市場狀態績效", regime_series, "價格區間", "複合報酬", ["高", "低"]),
        "trade-pnl-chart.svg": ("trade_pnl", "逐筆交易淨損益", trade_series, "交易序號", "淨損益（元）", trade_tick_labels),
        "cost-pressure-chart.svg": ("cost_pressure", "成本壓力測試", cost_series, "單邊滑價（基點）", "期末資產（元）", ["20", "40", "60"]),
        "core-allocation-chart.svg": ("core_allocation", "核心持有比例", core_series, "核心比例", "期末資產（元）", ["0%", "20%", "40%", "60%"]),
        "parameter-grid-chart.svg": ("parameter_grid", "243 格參數分布", grid_series, "參數格序號", "期末資產（元）", [str(index) for index in range(1, 244)]),
    }
    for name, (kind, title, source, x_label, y_label, x_tick_labels) in chart_specs.items():
        (output_root / name).write_text(
            prep._chart_svg(
                source, title=title, kind=kind, x_label=x_label,
                y_label=y_label, x_tick_labels=x_tick_labels,
            ),
            encoding="utf-8",
        )
    line_lookup = {line.line_id: line for line in analysis["lines"]}
    table = "\n".join(
        f"| {_line_label(row['line_id'], include_id=True)} | "
        f"{'次一交易日開盤' if 'NEXT_SESSION_OPEN' in row['line_id'] else '訊號日收盤' if 'SIGNAL_CLOSE' in row['line_id'] else '比較基準'} | "
        f"{line_lookup[row['line_id']].audit['comparison_start']}～{line_lookup[row['line_id']].audit['comparison_end']}（{int(line_lookup[row['line_id']].audit['comparison_observations']):,} 筆） | "
        f"{_money(row['ending_equity'])} | {_percent(row['total_return'])} | {_percent(row['cagr'])} | {_percent(row['max_drawdown'])} | "
        f"{int(row['profitable_years'])}／{int(row['losing_years'])}／{int(row['flat_years'])} 年 | "
        f"{int(row['closed_positions'] + row['open_positions'])}／{int(row['closed_positions'])}／{int(row['open_positions'])} 次 | {_money(row['maximum_single_entry_cash'])} |"
        for row in summary
    )

    annual_table_rows = []
    for line in analysis["lines"]:
        if line.role == "ACTIVE_STRATEGY":
            position_events = list(line.audit.get("trades", ()))
        elif line.role == "TRADABLE_PASSIVE_COMPARATOR":
            position_events = [
                {"role": "ENTRY", "fill_date": item["date"]}
                for item in line.audit.get("transactions", ())
                if item.get("role") == "CORE_ENTRY"
            ]
        else:
            position_events = []
        for annual_row in research.annual_slices(line.curve):
            year = annual_row["end"].year
            entries = sum(
                item.get("role") == "ENTRY" and item["fill_date"].year == year
                for item in position_events
            )
            exits = sum(
                item.get("role") == "EXIT" and item["fill_date"].year == year
                for item in position_events
            )
            open_positions = max(
                sum(
                    1 if item.get("role") == "ENTRY" else -1
                    for item in position_events
                    if item["fill_date"].year <= year
                ),
                0,
            )
            annual_table_rows.append(
                f"| {_line_label(line.line_id)} | {year} | "
                f"{_money(annual_row['starting_equity'])} | {_money(annual_row['ending_equity'])} | "
                f"{_percent(annual_row['return'])} | {_percent(annual_row['max_drawdown'])} | "
                f"{entries}／{exits}／{open_positions} 次 |"
            )
    annual_table = "\n".join(annual_table_rows)

    cost_table = "\n".join(
        f"| {_strategy_label(row['strategy'])} | "
        f"{'訊號日收盤' if row['timing'] == ExecutionTiming.SIGNAL_CLOSE.value else '次一交易日開盤'} | "
        f"{int(row['slippage_bps'])} 個基點 | {_money(row['ending_equity'])} | "
        f"{_percent(row['cagr'])} | {_percent(row['max_drawdown'])} | "
        f"{int(row['completed_rounds'])} 回合 |"
        for row in analysis["cost_sensitivity"]
    )

    fixed_table = "\n".join(
        f"| {_line_label(row['line_id'])} | {row['period_id']} | "
        + (
            f"{row['start']}～{row['end']} | {_money(row['starting_equity'])} | "
            f"{_money(row['ending_equity'])} | {_percent(row['return'])} | "
            f"{_percent(row['cagr'])} | {_percent(row['max_drawdown'])} |"
            if row["status"] == "OK" else
            "資料不足 | — | — | — | — | — |"
        )
        for row in fixed_rows
    )

    rolling_summary_rows = []
    for line in analysis["lines"]:
        for years in (3, 5, 10):
            valid = [
                item for item in research.rolling_windows(line.curve, years)
                if item.get("status") == "OK"
            ]
            if valid:
                cagrs = [research.decimal(item["cagr"]) for item in valid]
                drawdowns = [research.decimal(item["max_drawdown"]) for item in valid]
                rolling_summary_rows.append(
                    f"| {_line_label(line.line_id)} | {years} 年 | {len(valid):,} | "
                    f"{_percent(min(cagrs))} | {_percent(median(cagrs))} | "
                    f"{_percent(max(cagrs))} | {_percent(min(drawdowns))} |"
                )
            else:
                rolling_summary_rows.append(
                    f"| {_line_label(line.line_id)} | {years} 年 | 0 | 資料不足 | 資料不足 | 資料不足 | 資料不足 |"
                )
    rolling_summary_table = "\n".join(rolling_summary_rows)

    regime_table = "\n".join(
        f"| {_line_label(line_id)} | "
        f"{'高價格區間' if row['regime'] == 'HIGH' else '低價格區間'} | "
        f"{int(row['sessions']):,} 個交易日 | {_percent(row['compounded_return'])} |"
        for line_id, payload in analysis["active"].items()
        for row in payload["regime"]
    )

    concentration_table = "\n".join(
        f"| {_line_label(row['line_id'])} | {int(row['closed_trades'])} | "
        f"{_percent(row['win_rate'])} | "
        f"{_money(row['best_trade'], signed=True) if row['best_trade'] is not None else '無'} | "
        f"{_money(row['worst_trade'], signed=True) if row['worst_trade'] is not None else '無'} | "
        f"{_percent(row['top1_gain_concentration'])} | {_percent(row['top3_gain_concentration'])} | "
        f"{int(row['longest_consecutive_losses'])} 筆 | "
        f"{int(row['longest_underwater_calendar_days']):,} 天 |"
        for row in diagnostic_rows
    )
    removal_rows = []
    for row in diagnostic_rows:
        base = analysis["line_metrics"][row["line_id"]]
        removal_rows.append(
            f"| {_line_label(row['line_id'])} | 0 | 0 | {_money(base['ending_equity'])} | "
            f"{_percent(base['cagr'])} | {_percent(base['max_drawdown'])} | 原始結果 |"
        )
        for requested in (1, 3, 5):
            removal_rows.append(
                f"| {_line_label(row['line_id'])} | {requested} | "
                f"{int(row[f'remove_best_{requested}_removed_trades'])} | "
                f"{_money(row[f'remove_best_{requested}_ending_equity'])} | "
                f"{_percent(row[f'remove_best_{requested}_cagr'])} | "
                f"{_percent(row[f'remove_best_{requested}_max_drawdown'])} | "
                "僅在各筆出場後扣除其已實現現金淨損益，不重新計算後續部位大小 |"
            )
    removal_table = "\n".join(removal_rows)
    trade_scope_note = _synthetic_trade_scope_note(trades) if synthetic_test else ""
    section_titles = (
        ("conclusion", "01｜一頁白話結論"), ("performance", "02｜完整績效總表"),
        ("relative", "03｜相對基準"), ("rules", "04｜策略規則與成交版本"),
        ("annual", "05｜完整逐年績效"), ("trades", "06｜交易紀錄與資金投入"),
        ("execution", "07｜成交時間與交易成本"), ("periods", "08｜時期、滾動與狀態"),
        ("concentration", "09｜交易集中度與單筆穩定性"), ("grid", "10｜243 組參數穩健性"),
        ("core", "11｜核心持有比例研究"), ("practice", "12｜實務建議與監控"),
        ("qa", "13｜技術驗收與隔離重播"), ("attachments", "14｜完整附件目錄"),
    )
    attachments = (
        "formal-summary.csv", "relative-benchmark.csv", "daily-equity.csv",
        TRADE_REPORT_NAME, "trade-details.csv",
        "event-ledger.csv", "annual-performance.csv", "fixed-periods.csv", "rolling-windows.csv",
        "path-and-concentration.csv", "cost-sensitivity.csv", "parameter-grid.csv",
        "parameter-grid-summary.csv", "core-allocation-summary.csv", "core-allocation-daily.csv",
        "core-allocation-ledger.csv", "index-alignment-ledger.csv", "taiex-supplement.csv",
        "equity-chart.svg", "drawdown-chart.svg", "annual-chart.svg", "rolling-window-chart.svg",
        "regime-chart.svg", "trade-pnl-chart.svg", "cost-pressure-chart.svg",
        "core-allocation-chart.svg", "parameter-grid-chart.svg",
        "manifest.json", "readback.json",
    ) + (
        ("low-regime-entry-exclusion-audit.csv",) if high_regime_study else
        (
            "macd-only-audit.csv", "macd-only-account-daily.csv",
            "strategy-rule-contract.md",
        ) if macd_only_study else ()
    )
    study_active_ids = [
        key for key in analysis["active"]
        if (
            key.startswith("STRICT_HIGH_") if high_regime_study else
            key.startswith("MACD_ONLY_") if macd_only_study else
            not key.startswith(("STRICT_HIGH_", "MACD_ONLY_"))
        )
    ]
    next_open_ids = [key for key in study_active_ids if "NEXT_SESSION_OPEN" in key]
    best_ending = max(
        research.decimal(analysis["active"][key]["metrics"]["ending_equity"])
        for key in next_open_ids
    )
    best_next_ids = [
        key for key in next_open_ids
        if research.decimal(analysis["active"][key]["metrics"]["ending_equity"])
        == best_ending
    ]
    best_next_text = "、".join(_line_label(key) for key in best_next_ids)
    if len(best_next_ids) > 1:
        best_next_text += " 並列"

    annual_candidates = [
        (key, item)
        for key in next_open_ids
        for item in analysis["active"][key]["annual"]
    ]
    worst_annual_value = min(
        research.decimal(item["return"]) for _key, item in annual_candidates
    )
    worst_annual_ties = [
        (key, item) for key, item in annual_candidates
        if research.decimal(item["return"]) == worst_annual_value
    ]
    worst_annual_text = "、".join(
        f"{_line_label(key)}，{item['end'].year} 年"
        for key, item in worst_annual_ties
    )
    if len(worst_annual_ties) > 1:
        worst_annual_text += " 並列"

    rolling_candidates = [
        (key, item)
        for key in next_open_ids
        for item in analysis["active"][key]["rolling"][5]
        if item.get("status") == "OK"
    ]
    worst_rolling_value = (
        min(research.decimal(item["cagr"]) for _key, item in rolling_candidates)
        if rolling_candidates else None
    )
    worst_rolling_ties = [
        (key, item) for key, item in rolling_candidates
        if research.decimal(item["cagr"]) == worst_rolling_value
    ]
    cost20 = {item["strategy"]: item for item in analysis["cost_sensitivity"] if item["slippage_bps"] == 20 and item["timing"] == ExecutionTiming.NEXT_SESSION_OPEN.value}
    cost60 = {item["strategy"]: item for item in analysis["cost_sensitivity"] if item["slippage_bps"] == 60 and item["timing"] == ExecutionTiming.NEXT_SESSION_OPEN.value}
    practical_summary = "；".join(
        f"{_line_label(line_id)}：期末資產 {_money(analysis['line_metrics'][line_id]['ending_equity'])}、"
        f"總報酬 {_percent(analysis['line_metrics'][line_id]['total_return'])}、"
        f"年化報酬率 {_percent(analysis['line_metrics'][line_id]['cagr'])}、"
        f"最大回落 {_percent(analysis['line_metrics'][line_id]['max_drawdown'])}"
        for line_id in next_open_ids
    )
    cost_pressure = "；".join(
        f"{_strategy_label(key)} 的滑價由 20 提高到 60 個基點後，期末資產變動 "
        f"{_money(cost60[key]['ending_equity'] - cost20[key]['ending_equity'], signed=True)}"
        for key in sorted(cost20)
    )
    strategy_b_key = StrategyVersion.B.value
    strategy_b_cost_delta = (
        cost60[strategy_b_key]["ending_equity"]
        - cost20[strategy_b_key]["ending_equity"]
    )
    strategy_b_cost_note = (
        f"策略 B 的次一交易日開盤版本在 60 個基點時，期末資產相對 20 個基點"
        f"變動 {_money(strategy_b_cost_delta, signed=True)}，完成回合由 "
        f"{int(cost20[strategy_b_key]['completed_rounds'])} 降至 "
        f"{int(cost60[strategy_b_key]['completed_rounds'])}。這不是成本提高反而有利，也不能視為"
        "成本穩健；成本改變會改寫淨成本 2R 的啟動時點，進而改變後續 KD 出場與交易路徑，"
        "所以結果呈非單調，並顯示策略 B 對成本與路徑敏感"
    )
    grid_table = "\n".join(
        f"| {_strategy_label(row['strategy'])} | {_money(row['median_ending_equity'])} | "
        f"{_percent(row['positive_expectation_cell_ratio'])} | {_money(row['baseline_ending_equity'])} | "
        f"{_money(row['local_neighborhood_median'])} | "
        f"{_percent(row['rolling_5y_worst_cagr']) if row['rolling_5y_worst_cagr'] is not None else '資料不足'} |"
        for row in grid_summary_rows
    )
    core_table = "\n".join(
        f"| {_strategy_label(row['strategy'])} | {int(row['core_pct'])}% | "
        f"{_money(row['ending_equity'])} | {_percent(row['cagr'])} | {_percent(row['max_drawdown'])} |"
        for row in core_summary
    )
    relative_table = "\n".join(
        f"| {_line_label(row['line_id'])} | {_line_label(row['benchmark_id'])} | "
        f"{_money(row['ending_equity_difference'], signed=True)} | "
        f"{_percentage_points(row['cagr_difference'])} | {_percentage_points(row['max_drawdown_difference'])} |"
        for row in relative_rows
    )
    common_line = analysis["lines"][0]
    main_line_text = {6: "主六線", 7: "主七線"}.get(
        len(analysis["lines"]), f"主{len(analysis['lines'])}線"
    )
    comparator_count = len(analysis["lines"]) - len(analysis["active"])
    common_period = (
        f"{common_line.audit['comparison_start']} 至 {common_line.audit['comparison_end']}，"
        f"共 {int(common_line.audit['comparison_observations']):,} 筆共同觀測"
    )
    worst_rolling_text = (
        "資料不足"
        if worst_rolling_value is None else
        "、".join(
            f"{_line_label(key)}，{item['start']} 至 {item['end']}"
            for key, item in worst_rolling_ties
        )
        + (" 並列" if len(worst_rolling_ties) > 1 else "")
        + f"，年化報酬率 {_percent(worst_rolling_value)}"
    )
    taiex_text = (
        "資料角色缺少，補充比較維持暫緩"
        if analysis["taiex_status"] == "HOLD_MISSING_TAIEX_TOTAL_RETURN_ROLE"
        else f"資料可用，但只列補充附件，不納入{main_line_text}"
    )
    taiex_table = ""
    if analysis["taiex_supplement"] is not None:
        taiex_line = analysis["taiex_supplement"]
        taiex_metrics = research.performance_metrics(taiex_line.curve)
        taiex_comparison_ids = [
            "STRATEGY_A_NEXT_SESSION_OPEN",
            "STRATEGY_B_NEXT_SESSION_OPEN",
        ]
        if high_regime_study:
            taiex_comparison_ids.extend((
                "STRICT_HIGH_STRATEGY_A_NEXT_SESSION_OPEN",
                "STRICT_HIGH_STRATEGY_B_NEXT_SESSION_OPEN",
            ))
        elif macd_only_study:
            taiex_comparison_ids.extend((
                "MACD_ONLY_STRATEGY_A_NEXT_SESSION_OPEN",
                "MACD_ONLY_STRATEGY_B_NEXT_SESSION_OPEN",
            ))
        taiex_comparison_ids.append("00635U_BUY_HOLD_CASH")
        taiex_table = (
            "\n#### 臺灣加權股價報酬指數機會成本補充\n\n"
            "| 角色 | 實際期間（觀測數） | 期末資產 | 總報酬 | 年化報酬率 | 最大回落 |\n"
            "|---|---|---:|---:|---:|---:|\n"
            f"| 臺灣加權股價報酬指數（僅供機會成本補充） | "
            f"{taiex_line.curve[0][0]}～{taiex_line.curve[-1][0]}（{len(taiex_line.curve):,} 筆） | "
            f"{_money(taiex_metrics['ending_equity'])} | {_percent(taiex_metrics['total_return'])} | "
            f"{_percent(taiex_metrics['cagr'])} | {_percent(taiex_metrics['max_drawdown'])} |\n\n"
            "| 00635U 比較線 | 期末資產相對差 | 年化報酬率相對差 | 最大回落相對差 |\n"
            "|---|---:|---:|---:|\n"
            + "\n".join(
                f"| {_line_label(line_id)} | "
                f"{_money(analysis['line_metrics'][line_id]['ending_equity'] - taiex_metrics['ending_equity'], signed=True)} | "
                f"{_percentage_points(analysis['line_metrics'][line_id]['cagr'] - taiex_metrics['cagr'])} | "
                f"{_percentage_points(analysis['line_metrics'][line_id]['max_drawdown'] - taiex_metrics['max_drawdown'])} |"
                for line_id in taiex_comparison_ids
            )
            + "\n\n> 此表只衡量放棄臺股市場的機會成本；TAIEX 不是 Gold ER、不是黃金代理，"
            f"也不納入{main_line_text}。\n"
        )

    hold_metrics = analysis["line_metrics"]["00635U_BUY_HOLD_CASH"]
    a_line_id = _active_line_id(
        StrategyVersion.A, ExecutionTiming.NEXT_SESSION_OPEN, study_entry_policy
    )
    b_line_id = _active_line_id(
        StrategyVersion.B, ExecutionTiming.NEXT_SESSION_OPEN, study_entry_policy
    )
    a_metrics = analysis["line_metrics"][a_line_id]
    b_metrics = analysis["line_metrics"][b_line_id]
    policy_name = (
        "高檔限定" if high_regime_study else
        "純 MACD" if macd_only_study else "原策略"
    )
    policy_strategy_name = (
        "純 MACD 策略" if macd_only_study else f"{policy_name}策略"
    )
    active_hold_comparison = (
        f"00635U 買進持有期末資產 {_money(hold_metrics['ending_equity'])}。"
        f"{policy_strategy_name} A 次一交易日開盤版期末資產 {_money(a_metrics['ending_equity'])}，"
        f"相對持有差 {_money(a_metrics['ending_equity'] - hold_metrics['ending_equity'], signed=True)}，"
        f"年化報酬率差 {_percentage_points(a_metrics['cagr'] - hold_metrics['cagr'])}，"
        f"最大回落差 {_percentage_points(a_metrics['max_drawdown'] - hold_metrics['max_drawdown'])}；"
        f"{policy_strategy_name} B 次一交易日開盤版期末資產 {_money(b_metrics['ending_equity'])}，"
        f"相對持有差 {_money(b_metrics['ending_equity'] - hold_metrics['ending_equity'], signed=True)}，"
        f"年化報酬率差 {_percentage_points(b_metrics['cagr'] - hold_metrics['cagr'])}，"
        f"最大回落差 {_percentage_points(b_metrics['max_drawdown'] - hold_metrics['max_drawdown'])}"
    )
    if high_regime_study:
        closed_exclusions = [row for row in exclusion_rows if row["original_net_pnl"] != ""]
        wins = sum(research.decimal(row["original_net_pnl"]) > 0 for row in closed_exclusions)
        losses = sum(research.decimal(row["original_net_pnl"]) < 0 for row in closed_exclusions)
        equals = sum(row["gate_relation"] == "EQUAL" for row in exclusion_rows)
        policy_comparison = "；".join(
            f"{_line_label(strict_id)}相對{_line_label(original_id)}：期末資產差 "
            f"{_money(analysis['line_metrics'][strict_id]['ending_equity'] - analysis['line_metrics'][original_id]['ending_equity'], signed=True)}、"
            f"總報酬差 {_percentage_points(analysis['line_metrics'][strict_id]['total_return'] - analysis['line_metrics'][original_id]['total_return'])}、"
            f"最大回落差 {_percentage_points(analysis['line_metrics'][strict_id]['max_drawdown'] - analysis['line_metrics'][original_id]['max_drawdown'])}、"
            f"平均閒置現金差 {_money(
                sum((research.decimal(row['cash']) for row in analysis['active'][strict_id]['result'].state.daily), Decimal('0')) / len(analysis['active'][strict_id]['result'].state.daily)
                - sum((research.decimal(row['cash']) for row in analysis['active'][original_id]['result'].state.daily), Decimal('0')) / len(analysis['active'][original_id]['result'].state.daily),
                signed=True,
            )}"
            for original_id, strict_id in (
                (_active_line_id(strategy, ExecutionTiming.NEXT_SESSION_OPEN, EntryPolicy.ORIGINAL),
                 _active_line_id(strategy, ExecutionTiming.NEXT_SESSION_OPEN, EntryPolicy.STRICT_HIGH_ONLY))
                for strategy in StrategyVersion
            )
        )
        exclusion_summary = (
            f"低價格區間進場排除稽核（low-regime-entry-exclusion audit）共列 {len(exclusion_rows):,} 筆原策略被排除進場，"
            f"其中已結束 {len(closed_exclusions):,} 筆，原交易獲利 {wins:,} 筆、虧損 {losses:,} 筆；"
            f"歷史樣本 MA21 等於 MA144 的被排除原進場為 {equals:,} 筆。等號排除另由邊界單元測試證明。"
            f"{policy_comparison}"
        )
        original_a = analysis["line_metrics"]["STRATEGY_A_NEXT_SESSION_OPEN"]
        original_b = analysis["line_metrics"]["STRATEGY_B_NEXT_SESSION_OPEN"]
        a_gap = a_metrics["ending_equity"] - original_a["ending_equity"]
        b_gap = b_metrics["ending_equity"] - original_b["ending_equity"]
        research_finding = (
            f"新進場門檻共排除 {len(exclusion_rows):,} 筆原策略進場；這些原交易合計 {wins:,} 勝、{losses:,} 負。"
            f"但高檔限定策略 A、B 的次一交易日開盤版本仍分別比原版少 "
            f"{_money(abs(a_gap))}、{_money(abs(b_gap))}，年化報酬率分別下降 "
            f"{_absolute_percentage_points(a_metrics['cagr'] - original_a['cagr'])}、"
            f"{_absolute_percentage_points(b_metrics['cagr'] - original_b['cagr'])}，未改善相對買進持有的差距。"
            "限制新進場可降低部分低價格區間曝險，但同時增加閒置現金；本樣本不支持將它採納為替代主策略。"
            "這是歷史資料的研究結論，不是投資建議。"
        )
        deployment_decision = (
            "執行決策：本次只完成高檔限定進場門檻（entry gate）的隔離比較，不代表策略採納；"
            "在 00 嚴格 QA 前不得更新正式總目錄、部署、合併或推送。"
        )
    elif macd_only_study:
        a_original = analysis["line_metrics"]["STRATEGY_A_NEXT_SESSION_OPEN"]
        b_original = analysis["line_metrics"]["STRATEGY_B_NEXT_SESSION_OPEN"]
        a_delta = a_metrics["ending_equity"] - a_original["ending_equity"]
        b_delta = b_metrics["ending_equity"] - b_original["ending_equity"]
        macd_exits = sum(
            row["exit_reason"] == ExitReason.MACD.value for row in macd_audit_rows
        )
        kd_exits = sum(
            row["exit_reason"] == ExitReason.KD_2R.value for row in macd_audit_rows
        )
        stop_exits = sum(
            row["exit_reason"] == ExitReason.INITIAL_STOP.value for row in macd_audit_rows
        )
        armed_positions = sum(bool(row["permanently_armed"]) for row in macd_audit_rows)
        exclusion_summary = (
            f"純 MACD 因果稽核共列 {len(macd_audit_rows):,} 個部位；"
            f"MACD 新翻綠出場 {macd_exits:,} 筆、2R 啟動後 KD 新死叉出場 {kd_exits:,} 筆、"
            f"固定初始防守出場 {stop_exits:,} 筆，策略 B 曾永久啟動 2R 的部位 {armed_positions:,} 筆。"
            "逐筆均保存進場前後柱狀值、負柱區段、防守價、R、2R、KD 與 MA 診斷欄位；"
            "另以改寫全部 MA 診斷值的隔離重播證明訂單、事件與帳戶完全一致。"
        )
        research_finding = (
            f"純 MACD 策略 A、B 的次一交易日開盤版本相對原版期末資產分別變動 "
            f"{_money(a_delta, signed=True)}、{_money(b_delta, signed=True)}，年化報酬率分別變動 "
            f"{_percentage_points(a_metrics['cagr'] - a_original['cagr'])}、"
            f"{_percentage_points(b_metrics['cagr'] - b_original['cagr'])}。"
            f"{active_hold_comparison}。這是凍結歷史樣本的隔離研究結果，不是投資建議，"
            "也不代表採納或取代原策略。"
        )
        deployment_decision = (
            "執行決策：本次只完成純 MACD 規則的隔離比較；在 00 逐節嚴格 QA 前不得更新"
            "正式總目錄、部署、合併或推送，且樣本結果不自動轉化為策略採納。"
        )
    else:
        exclusion_summary = ""
        research_finding = ""
        deployment_decision = (
            "執行決策：兩個主動策略都不取代 00635U 買進持有。策略 A 只保留為降低回落的研究候選，"
            "在完成新增樣本與前瞻驗證前不部署；策略 B 因報酬落後且回落更深，不支持進入部署候選。"
        )
    gold_text = (
        "S&P GSCI Gold ER 官方歷史未取得，因此完全省略；未以 FinMind GoldPrice、ETF、TAIEX 或其他代理替代"
        if analysis["gold_status"] != "AVAILABLE_OFFICIAL" else
        "S&P GSCI Gold ER 官方歷史可用，僅作不可交易基準"
    )
    core_tradeoff = "；".join(
        f"{_strategy_label(strategy)}期末資產最高為核心 {int(max((row for row in core_summary if row['strategy'] == strategy.name), key=lambda row: row['ending_equity'])['core_pct'])}%；"
        f"回落最淺為核心 {int(max((row for row in core_summary if row['strategy'] == strategy.name), key=lambda row: row['max_drawdown'])['core_pct'])}%"
        for strategy in StrategyVersion
    )
    active_count = len(analysis["active"])
    strategy_scope_text = (
        "原策略四線與本次高檔限定四線"
        if high_regime_study else "四條主動策略"
    )
    if macd_only_study:
        strategy_scope_text = "原策略四線與本次純 MACD 四線"
    strict_rule_note = (
        "本次新策略的進場門檻（entry gate）只在訊號日嚴格 MA21 > MA144 時允許新進場；MA21 = MA144 與 MA21 < MA144 均不得進場，"
        "並取消低價格區間 MACD 翻紅進場。此條件只作用於新進場，持倉跨入 MA21 <= MA144 不會因此賣出；"
        if high_regime_study else
        "本次純 MACD 策略只接受 MACD(21,55,55) 柱狀值由前一可觀察交易日不大於零、到訊號日大於零的新翻紅交叉；"
        "MA21／MA144 只作診斷，不參與任何訂單、狀態、防守或成交；"
        if macd_only_study else ""
    )
    exclusion_sentence = exclusion_summary.rstrip("。")
    prose = {
        "conclusion": ("先用實務成交版本回答候選策略在完整成本後的表現。", f"比較{strategy_scope_text}、00635U 買進持有，再檢查最差年度、五年窗口與成本壓力。", practical_summary + f"；本次策略期末資產最高者為 {best_next_text}。{active_hold_comparison}。{exclusion_sentence}。最差年度為 {worst_annual_text}，年度報酬 {_percent(worst_annual_value)}；最差五年窗口：{worst_rolling_text}。{cost_pressure}。", research_finding if (high_regime_study or macd_only_study) else "主摘要同時呈現原策略、新策略與持有基準，不以單一有利指標判斷。", deployment_decision),
        "performance": (f"用同一共同期間比較{main_line_text}。", "每線重訂基準至 500,000.00 元並列實際起訖日與觀測數；缺少的官方基準不以代理補入。", f"{main_line_text}的共同期間為 {common_period}；{active_count} 條主動策略的交易數與最大投入直接來自逐筆帳本。{gold_text}。", "年化報酬率與最大回落必須一起看，不能只看期末資產。", "主表是後續相對、期間與壓力分析的唯一母表。"),
        "relative": ("逐策略比較可取得且符合契約的指定基準。", "計算相對 00635U 現金持有與分配再投入的期末資產差、年化報酬率差與最大回落差；官方 Gold ER 缺席時不計算。TAIEX 另作機會成本補充。", f"{active_count} 條策略共輸出 {len(relative_rows):,} 組主要相對比較；{gold_text}；臺灣加權股價報酬指數目前為：{taiex_text}。", "缺少官方 Gold ER 時，不能宣稱已完成對黃金市場基準的超額報酬比較；TAIEX 只能說明放棄臺股的機會成本。", "主要結論只依 00635U 可交易基準；Gold ER 比較維持未完成，TAIEX 不得被當成黃金代理。"),
        "rules": (("確認原策略、本次研究策略、策略 A／B 與兩種成交版本沒有混用。" if macd_only_study else "確認原策略、本次進場門檻、策略 A／B 與兩種成交版本沒有混用。"), ("逐項核對 MACD 新交叉、連續負柱區段固定防守、策略 A／B 出場差異、2R 與成交時點。" if macd_only_study else "逐項核對嚴格均線進場門檻、固定防守、策略 A／B 出場差異、停損優先順序與成交時點。"), strict_rule_note + "所有進場、2R 啟動、現金分配、公司行動與出場都有事件 ID 與部位 ID。" + exclusion_sentence + "。", (research_finding + "進場門檻只排除當下 MA21 <= MA144 的新進場，不使用未來資料，也不新增出場條件。") if high_regime_study else ("純 MACD 規則只使用當下與先前可觀察資料；MA 欄位只作診斷，改寫後的訂單、事件及帳戶重播完全一致。" if macd_only_study else "進場門檻只排除當下條件不符的新進場，不使用未來資料，也不新增出場條件。"), "規則改動必須另開版本並重跑全部附件。"),
        "annual": ("檢查獲利是否集中少數年份。", "連續帳戶以前一年度末為起點，首年用 500,000.00 元，完整納入首交易日。", f"實務成交線的最差年度為 {worst_annual_text}，年度報酬 {_percent(worst_annual_value)}；下表完整列出 {active_count} 條策略與 {comparator_count} 個主基準的所有年度。", "年度不重設本金；獲利年度次數不能取代最差年度幅度。", "若少數年度貢獻過大，實務採用需降權。"),
        "trades": ("讓讀者直接核對每筆投入與出場。", "交易表含訊號日、成交日、成交方式、部位／事件 ID、股數、價格、費稅、初始 R、2R 啟動日與淨損益。", (trade_scope_note + "可先閱讀中文明細，再用 CSV 與事件帳本重建。") if synthetic_test else f"本次輸出 {len(trades):,} 列進出場事件；可先閱讀中文明細，再用 CSV 與事件帳本重建。", "每次投入不是固定 500,000.00 元，而受可用現金、整股與 20.00% 初始風險上限約束。", "任何主表交易數都必須能由附件一對一重建。"),
        "execution": ("量化成交時間與成本敏感度。", "同時比較訊號日收盤／次一交易日開盤及 20／40／60 個基點，輸出期末資產、年化報酬率、最大回落與完成回合。", cost_pressure + "。" + strategy_b_cost_note + "。", "成本變動不只扣減每筆損益，也可能改變 2R 啟動、KD 出場與後續交易，因此非單調結果仍代表路徑敏感，不能解讀為成本有利。", "實務主口徑固定為次一個可成交交易日的開盤；策略 B 在成本壓力下不具部署穩健性。"),
        "periods": ("檢查固定時期、月末滾動窗口與市場狀態。", "固定期輸出期初／期末／總報酬／年化報酬率／最大回落；滾動 3／5／10 年採月末窗口，並另列高、低價格區間。", f"實務成交線的五年最差年化報酬率為：{worst_rolling_text}；下方三張表完整呈現固定期間、滾動摘要與高低價格區間。", "全期正報酬若伴隨長期負窗口，仍不能稱穩健。", "以最差窗口與低／高價格區間的差異限制部位。"),
        "concentration": ("檢查少數交易、連虧與水下期。", "輸出最佳與最差交易、最佳 1 筆／3 筆獲利集中度、最長連續虧損、水下期；移除最佳 1／3／5 筆時，只在原出場日扣除其已實現現金淨損益，不重新計算後續部位大小。", "下表列出每條策略的實際交易穩定度，以及移除最佳 0／1／3／5 筆後的期末資產、年化報酬率與最大回落；這是現金扣除敏感度，不冒充完整訊號重播。", "若扣除少數最佳交易後崩解，期望值證據偏弱。", "集中度過高時只列研究候選。"),
        "grid": ("檢查中心參數是否位於穩健區域。", "策略 A、策略 B 各 243 格輸出總報酬、年化報酬率、最大回落、報酬／回落比與五年最差，再彙總分位與鄰域。", "下表同時呈現中位數期末資產、正報酬格比例、中心格與鄰域中位數；所有金額與比例均使用人類可讀格式。", "看分布與中心鄰域，不挑單一最高格；KD 9/9/55 與 2R 不參與搜尋。", "中心若顯著弱於鄰域或正報酬格不足，結論降級。"),
        "core": ("評估核心持有 0／20／40／60%。", "核心與戰術各自保存現金、股數、清算值與資產，主方法不再平衡，並列成本與重建誤差。", core_tradeoff + "。完整風險與報酬數字列於下表。", "核心比例提高不必然同時改善報酬與回落；需用期末資產、年化報酬率和最大回落一起取捨。", "年度再平衡只能另列選項，不混入主結果。"),
        "practice": ("把研究轉成可執行限制。", "以原策略與 00635U 買進持有作門檻；同時監控可成交交易日、期貨轉倉、正逆價差、匯率、時區與追蹤缺口。", active_hold_comparison + "。" + exclusion_sentence + "。" + research_finding, research_finding if (high_regime_study or macd_only_study) else deployment_decision, "只有 00 完成嚴格 QA 後，才能決定是否進入下一階段；本報告本身不構成策略採納。"),
        "qa": ("證明資料、報告與附件可重現。", "正式執行入口會再次核對資料清單（manifest）本身的 SHA-256、資料格式與每個輸入檔案雜湊；輸出也附自己的資料清單與回讀驗證紀錄（readback）。", f"輸入資料清單 SHA-256 為 `{accepted_manifest_sha256}`；{main_line_text}共同期間已一致，兩個隔離目錄必須產生完全相同的位元組。", "不論從命令列或程式入口執行，只要資料、雜湊、連結或圖表語意有一項不符，就會停止而不產生可採用結果。", "00 只有在全部驗證通過後才可接受此研究包。"),
        "attachments": ("提供完整重建材料。", "逐檔列出主表、交易、事件、期間、參數、核心、對時、圖表與來源收據。", f"本次共有 {len(attachments)} 個可點入口。", "主文摘要不能取代 CSV 與資料清單（manifest）。", "00 以附件雜湊與隔離重播為最終依據。"),
    }
    if high_regime_study:
        entry_rule_bullet = (
            "\n- **本次高檔限定進場：** 只用訊號日當下資料；嚴格 `MA21 > MA144` 才能評估新進場。"
            "`MA21 == MA144` 或 `MA21 < MA144` 一律排除，且不再使用低價格區間 MACD 翻紅進場。"
            "舊版低區間訊號使用 MACD(21,55,55)，本次只保留作對照。"
            "高檔突破仍須前一日技術收盤不高於前一日 MA21、當日技術收盤高於當日 MA21。"
            "此進場門檻不作用於既有持倉或出場。\n"
            "- **舊版基準：** 保留原本 `MA21 >= MA144` 高區間突破與 `MA21 < MA144` 低區間 MACD 翻紅進場，僅供同包對照。\n"
        )
        rules_detail = "- **固定初始防守：** 取本次 MA21 拉回階段低點、前一交易日低點與訊號日低點三者最小值，而且必須嚴格低於技術進場價。\n- **策略 A：** 高價格區間先檢查固定初始防守，再檢查已啟動的 MACD 柱狀值新紅翻綠；固定防守優先。低價格區間只維護、不執行固定防守，沿用已啟動的 MACD 新紅翻綠出場。\n- **策略 B：** 只有高價格區間進場部位會從進場到離場持續執行固定防守；完整成本後達淨成本 2R 才永久啟動 KD(9,9,55)，啟動後只接受新死叉；固定防守優先。\n"
    elif macd_only_study:
        entry_rule_bullet = (
            "\n- **本次純 MACD 進場：** MACD 固定為 (21,55,55)；空倉時只有前一可觀察交易日柱狀值 `<= 0`、訊號日 `> 0` 的新翻紅交叉可進場。持續紅柱不重複進場，離場後必須等待下一個新交叉。\n"
            "- **均線角色：** MA21／MA144 只留作診斷與狀態分析，不參與進場、出場、防守、持倉狀態或成交。\n"
            "- **舊版基準：** 原策略 A／B 四線完全保留，僅供同包對照。\n"
        )
        rules_detail = (
            "- **固定初始防守：** 取緊接訊號前、連續 `histogram < 0` 負柱區段的所有日低價、前一交易日低價與訊號日低價之最小值。`histogram = 0` 中斷區段；無有效負柱區段或防守價不嚴格低於技術進場價時拒絕進場。防守價不移動、持倉全程有效，並優先於其他出場。\n"
            "- **策略 A：** 未觸及防守時，只接受柱狀值由前一交易日 `>= 0` 到當日 `< 0` 的新翻綠交叉出場；持續綠柱不重複觸發。\n"
            "- **策略 B：** 不使用 MACD 翻綠出場。完整費後達 2R 後永久啟動 KD(9,9,55)；2R 前死叉忽略且不追溯，啟動當日若正好形成新死叉可出場，否則等待其後第一個新死叉。\n"
        )
    else:
        entry_rule_bullet = "\n- **共同進場：** 只用 00635U 自有歷史計算 MA21、MA144 與 MACD(21,55,55)。`MA21 >= MA144`（等號歸高）為高價格區間：前一日技術收盤不高於前一日 MA21、當日技術收盤高於當日 MA21，才是新突破進場。`MA21 < MA144` 為低價格區間：MACD 柱狀值前一日不大於零、當日大於零，才是新綠翻紅進場。\n"
        rules_detail = "- **固定初始防守：** 取本次 MA21 拉回階段低點、前一交易日低點與訊號日低點三者最小值，而且必須嚴格低於技術進場價。\n- **策略 A：** 高價格區間先檢查固定初始防守，再檢查已啟動的 MACD 柱狀值新紅翻綠；固定防守優先。低價格區間只維護、不執行固定防守，沿用已啟動的 MACD 新紅翻綠出場。\n- **策略 B：** 只有高價格區間進場部位會從進場到離場持續執行固定防守；完整成本後達淨成本 2R 才永久啟動 KD(9,9,55)，啟動後只接受新死叉；固定防守優先。\n"
    extras = {
        "performance": "\n| 主線 | 成交方式 | 實際起訖日（觀測數） | 期末資產 | 總報酬 | 年化報酬率 | 最大回落 | 獲利／虧損／平盤年度 | 進場／出場／未平倉 | 最大投入 |\n|---|---|---|---:|---:|---:|---:|---:|---:|---:|\n" + table + ("\n\n![合成測試資產曲線](equity-chart.svg)\n" if synthetic_test else "\n\n![正式資產曲線](equity-chart.svg)\n") + "\n> 00635U 買進持有的現金保留與分配再投入兩線在本輪完全重合，主資產曲線與回撤圖以一條黑線呈現；並非未比較。\n",
        "relative": "\n| 策略 | 比較基準 | 期末資產差 | 年化報酬率差 | 最大回落差 |\n|---|---|---:|---:|---:|\n" + relative_table + "\n\n> 年化報酬率差與最大回落差均以百分點表示。\n" + taiex_table,
        "rules": entry_rule_bullet + rules_detail + "- **成交版本：** 進出同時點；同時計算訊號日收盤成交與次一個可成交交易日開盤成交，後者為實務主口徑。\n" + ("\n- [低區間進場排除稽核](low-regime-entry-exclusion-audit.csv)\n" if high_regime_study else "- [純 MACD 因果稽核](macd-only-audit.csv)\n- [純 MACD 規則契約](strategy-rule-contract.md)\n" if macd_only_study else ""),
        "annual": "\n#### 完整逐年績效表\n\n| 策略／基準 | 年度 | 年初資產 | 年末資產 | 年度報酬 | 年度最大回落 | 進場／出場／未平倉 |\n|---|---:|---:|---:|---:|---:|---:|\n" + annual_table + "\n\n![逐年報酬](annual-chart.svg)\n\n![回撤路徑](drawdown-chart.svg)\n",
        "trades": (f"\n> **本夾具的交易範圍：** {trade_scope_note}\n" if synthetic_test else "") + f"\n- [繁體中文逐筆交易明細]({TRADE_REPORT_NAME})\n- [逐筆交易明細 CSV](trade-details.csv)\n- [事件帳本 CSV](event-ledger.csv)\n" + ("- [低區間進場排除稽核](low-regime-entry-exclusion-audit.csv)\n" if high_regime_study else "- [純 MACD 因果稽核](macd-only-audit.csv)\n- [純 MACD 每日主帳](macd-only-account-daily.csv)\n" if macd_only_study else ""),
        "execution": "\n#### 完整成交時點與成本壓力表\n\n| 策略 | 成交時點 | 單邊滑價 | 期末資產 | 年化報酬率 | 最大回落 | 完成回合 |\n|---|---|---:|---:|---:|---:|---:|\n" + cost_table + "\n\n![成本壓力](cost-pressure-chart.svg)\n",
        "periods": "\n#### 固定期間表\n\n| 策略／基準 | 固定期間 | 實際起訖 | 期初資產 | 期末資產 | 總報酬 | 年化報酬率 | 最大回落 |\n|---|---|---|---:|---:|---:|---:|---:|\n" + fixed_table + "\n\n#### 3／5／10 年滾動摘要表\n\n| 策略／基準 | 窗口 | 有效窗口數 | 最差年化 | 中位年化 | 最佳年化 | 窗口最差回落 |\n|---|---:|---:|---:|---:|---:|---:|\n" + rolling_summary_table + "\n\n#### 高低價格區間表\n\n| 策略 | 價格區間 | 交易日數 | 區間複合報酬 |\n|---|---|---:|---:|\n" + regime_table + "\n\n![滾動窗口](rolling-window-chart.svg)\n\n![市場狀態](regime-chart.svg)\n",
        "concentration": "\n#### 交易穩定度表\n\n| 策略 | 已完成交易 | 勝率 | 最佳交易 | 最差交易 | 最佳 1 筆獲利集中度 | 最佳 3 筆獲利集中度 | 最長連續虧損 | 最長水下期 |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|\n" + concentration_table + "\n\n#### 移除最佳交易敏感度表\n\n| 策略 | 要求移除最佳筆數 | 實際移除筆數 | 期末資產 | 年化報酬率 | 最大回落 | 計算方式 |\n|---|---:|---:|---:|---:|---:|---|\n" + removal_table + "\n\n![逐筆交易淨損益](trade-pnl-chart.svg)\n",
        "grid": "\n| 策略 | 中位數期末資產 | 正報酬格比例 | 中心格期末資產 | 鄰域中位數 | 五年最差年化 |\n|---|---:|---:|---:|---:|---:|\n" + grid_table + "\n\n![243 格參數分布](parameter-grid-chart.svg)\n",
        "core": "\n| 策略 | 核心比例 | 期末資產 | 年化報酬率 | 最大回落 |\n|---|---:|---:|---:|---:|\n" + core_table + "\n\n![核心持有比例](core-allocation-chart.svg)\n",
        "attachments": "\n" + "\n".join(f"- [{name}]({name})" for name in attachments) + "\n",
    }
    sections = [
        prep._section(anchor, title, *prose[anchor]) + extras.get(anchor, "")
        for anchor, title in section_titles
    ]
    toc = "\n".join(
        f"- [{title}](#{prep._chapter_anchor(title)})" for _anchor, title in section_titles
    )
    status_banner = (
        "**合成測試資料／不是正式績效。** 交易筆數刻意縮小，只驗證管線與規則；所有數字均為合成測試值，不得解讀為 00635U 上市以來的正式結果。\n> **SYNTHETIC TEST ONLY。**"
        if synthetic_test else
        "**狀態：正式資料。** 輸入資料已通過 00 指定的資料清單（manifest）閘門；仍須完成本報告與附件驗收。"
    )
    report_title = (
        "# 00635U 黃金期貨 ETF 因果回測｜合成測試報告（非正式績效）"
        if synthetic_test else
        "# 00635U 黃金期貨 ETF 因果回測｜正式報告"
    )
    if high_regime_study:
        report_title = "# 00635U 黃金期貨 ETF｜MA21 > MA144 高檔限定進場隔離研究"
    elif macd_only_study:
        report_title = "# 00635U 黃金期貨 ETF｜純 MACD 進出場隔離研究"
    report = (
        report_title + "\n\n"
        f"> {status_banner}\n> 輸入資料清單（manifest）SHA-256：`{accepted_manifest_sha256}`。\n"
        f"> 凍結資料：`{input_manifest.get('snapshot_id', '未提供')}`；主來源為 FinMind。{gold_text}。\n"
        "> FinMind 股息／分割／停復牌候選均為 0 列，且未取得官方交叉確認；本報告未自行建立任何事件。\n\n"
        "## 閱讀順序\n\n" + toc + "\n\n" + "\n\n".join(sections)
        + f"\n\n`{stop_code}`\n"
    )
    (output_root / REPORT_NAME).write_text(report, encoding="utf-8")
    artifact_names = tuple(name for name in attachments if name not in {"manifest.json", "readback.json"}) + (REPORT_NAME,)
    manifest = {
        "schema": (
            "00635U_SYNTHETIC_FIXTURE_RUN_V1"
            if synthetic_test else
            (
                "00635U_STRICT_HIGH_REGIME_STUDY_V1" if high_regime_study else
                "00635U_MACD_ONLY_STUDY_V1" if macd_only_study else
                "00635U_FORMAL_RUN_V1"
            )
        ),
        "artifact_classification": (
            "SYNTHETIC_TEST_PACK" if synthetic_test else
            "ISOLATED_RESEARCH_CANDIDATE" if (high_regime_study or macd_only_study) else
            "FORMAL_RESEARCH_PACK"
        ),
        "publication_eligible": not synthetic_test and not high_regime_study and not macd_only_study,
        "intended_use": (
            "PIPELINE_AND_RULE_VALIDATION_ONLY"
            if synthetic_test else
            "STRICT_00_QA_BEFORE_ANY_PUBLICATION"
            if (high_regime_study or macd_only_study) else "FORMAL_RESEARCH_REVIEW"
        ),
        "input_manifest_sha256": accepted_manifest_sha256,
        "input_data_classification": input_classification,
        "snapshot_id": input_manifest.get("snapshot_id"),
        "source_package_manifest_sha256": input_manifest.get("source_package_manifest_sha256"),
        "source_package_readback_sha256": input_manifest.get("source_package_readback_sha256"),
        "gold_er_status": analysis["gold_status"],
        "files": [
            {"path": name, "sha256": prep.sha256_file(output_root / name)}
            for name in artifact_names
        ],
    }
    (output_root / "manifest.json").write_bytes(prep._canonical_bytes(manifest) + b"\n")
    readback = {
        "status": "PASS", "formal": not synthetic_test,
        "artifact_classification": manifest["artifact_classification"],
        "publication_eligible": manifest["publication_eligible"],
        "stop_code": stop_code,
        "manifest_sha256": prep.sha256_file(output_root / "manifest.json"),
        "files_verified": len(artifact_names),
    }
    (output_root / "readback.json").write_bytes(prep._canonical_bytes(readback) + b"\n")
    errors = (
        prep.validate_report_tree(output_root, report_name=REPORT_NAME)
        + prep.validate_report_schema(output_root, report_name=REPORT_NAME)
    )
    if errors:
        raise RuntimeError("; ".join(errors))
    return {
        "status": stop_code,
        "report": str(output_root / REPORT_NAME),
        "manifest_sha256": readback["manifest_sha256"],
    }
