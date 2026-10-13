"""Publish the accepted 0050 report with prominent Chinese trade details.

This is a presentation-only revision of the sealed v1.5 package.  Strategy
signals, transactions, accounting, charts, and performance remain unchanged.
The revision adds a human-readable Traditional Chinese trade ledger and places
prominent links to all trade-detail artifacts in section 06.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import etf_0050_final_strategy_v1 as v12
from . import etf_0050_final_strategy_v1_4 as v14
from . import etf_0050_final_strategy_v1_5 as v15
from .etf_0050_base_v1 import ETF0050Error, tree_hash
from .etf_0050_formal_v2 import _artifact_rows, _verify_artifacts, _write_json
from .paths import dev_run, report_path


VERSION = "dual-ma-0050-final-strategy-v1.6"
SOURCE_RUN = dev_run("02-etf-0050-final-strategy-v1-5")
SOURCE_REPORT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-5")
SOURCE_RUN_MANIFEST = "03E87905B464C38EB850BA11F076FF660D1EC46324F116F2E5ED8CFD6F0D4555"
SOURCE_REPORT_MANIFEST = "13694EBD39DD4F117BA13EF5849DBDFED608FDBEF40C8570E228BD513EE7038E"
SOURCE_READBACK = "366FDCC7BB4C554F467C0942E9B85C652F7C37B82A070E1C3878634586751C94"
RUN_ROOT_DEFAULT = dev_run("02-etf-0050-final-strategy-v1-6")
REPORT_ROOT_DEFAULT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-6")

TIMING_NAMES = {
    "SIGNAL_CLOSE": "訊號當日收盤成交",
    "NEXT_SESSION_OPEN": "次一交易日開盤成交",
}
EXIT_NAMES = {
    "INITIAL_PROTECTIVE_STOP": "固定初始防守價出場",
    "MACD_RED_TO_GREEN": "MACD 柱狀值跌破 0 出場",
    "": "截止日仍持有",
}
TRADE_LINKS = (
    ("繁體中文逐筆交易明細", "trade-details.md"),
    ("兩種成交版本合併逐筆 CSV", "strategy-trade-details.csv"),
    ("訊號當日收盤版逐筆 CSV", "signal-close-trade-details.csv"),
    ("次一交易日開盤版逐筆 CSV", "next-session-open-trade-details.csv"),
    ("完整買賣與現金事件流水", "strategy-transactions.csv"),
)


def _decimal(value: str) -> Decimal:
    return Decimal(value or "0")


def _money(value: str) -> str:
    return f"{_decimal(value):,.2f}"


def _price(value: str) -> str:
    return "—" if value == "" else f"{_decimal(value):,.4f}".rstrip("0").rstrip(".")


def _percent(value: str) -> str:
    return "—" if value == "" else f"{_decimal(value) * 100:.2f}%"


def _render_trade_details(rows: Sequence[Mapping[str, str]], produced: str) -> str:
    grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        timing = row["execution_timing"]
        if timing not in TIMING_NAMES:
            raise ETF0050Error(f"unknown execution timing: {timing}")
        grouped[timing].append(row)

    lines = [
        "# 0050 最終策略逐筆交易明細",
        "",
        f"產出時間：{produced}",
        "",
        "本表使用與正式報告完全相同的交易資料，分開列出兩種成交版本。金額單位為新臺幣；報酬率、最大有利變動及最大不利變動均以該筆買進總成本為基礎。",
        "",
        f"合計 {len(rows)} 筆：訊號當日收盤版 {len(grouped['SIGNAL_CLOSE'])} 筆；次一交易日開盤版 {len(grouped['NEXT_SESSION_OPEN'])} 筆。",
        "",
        "出場原因說明：`固定初始防守價出場`＝價格觸及進場時固定的防守價；`MACD 柱狀值跌破 0 出場`＝已啟用出場條件後，MACD 柱狀值由非負轉為負值；`截止日仍持有`＝截至 2026-09-30 尚未賣出，損益按截止日價格估算。",
        "",
    ]
    header = (
        "|序號|狀態|進場日|進場成交價|進場股數|出場日|出場成交價／期末估值|出場原因|"
        "買進總成本|賣出費用與稅|持有期間股息|淨損益／估算|報酬率／估算|最大有利變動|最大不利變動|初始防守價|"
    )
    separator = "|---:|---|---|---:|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    for timing in ("SIGNAL_CLOSE", "NEXT_SESSION_OPEN"):
        items = grouped[timing]
        lines.extend(
            [
                f"## {TIMING_NAMES[timing]}",
                "",
                f"共 {len(items)} 筆；其中已賣出 {sum(bool(row['exit_date']) for row in items)} 筆，截止日仍持有 {sum(not row['exit_date'] for row in items)} 筆。",
                "",
                header,
                separator,
            ]
        )
        for index, row in enumerate(items, start=1):
            exit_reason = EXIT_NAMES.get(row["exit_reason"], row["exit_reason"] or "截止日仍持有")
            sell_cost = _decimal(row["sell_commission"]) + _decimal(row["sell_tax"])
            lines.append(
                "|" + "|".join(
                    (
                        str(index),
                        row["status_zh"],
                        row["entry_date"],
                        _price(row["entry_fill_price"]),
                        f"{int(_decimal(row['entry_shares'])):,}",
                        row["exit_date"] or "—",
                        _price(row["exit_fill_or_mark_price"]),
                        exit_reason,
                        _money(row["buy_total"]),
                        f"{sell_cost:,.2f}",
                        _money(row["cash_dividends"]),
                        _money(row["net_pnl_or_estimate"]),
                        _percent(row["return_rate_or_estimate"]),
                        _percent(row["mfe"]),
                        _percent(row["mae"]),
                        _price(row["initial_stop"]),
                    )
                ) + "|"
            )
        lines.extend(("",))
    lines.extend(
        (
            "## 原始資料",
            "",
            "- [兩種成交版本合併逐筆 CSV](strategy-trade-details.csv)",
            "- [訊號當日收盤版逐筆 CSV](signal-close-trade-details.csv)",
            "- [次一交易日開盤版逐筆 CSV](next-session-open-trade-details.csv)",
            "- [完整買賣與現金事件流水](strategy-transactions.csv)",
            "",
        )
    )
    return "\n".join(lines)


def _render_report(source_text: str, produced: str, attachments: Sequence[str]) -> str:
    text = re.sub(
        r"^# .*?$",
        "# 0050 最終策略定版報告｜正式 v1.6",
        source_text,
        count=1,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^產出時間：.*?$", f"產出時間：{produced}", text, count=1, flags=re.MULTILINE
    )
    text = re.sub(
        r"^狀態：`ACCEPT`.*?$",
        "狀態：`ACCEPT`。本版新增繁體中文逐筆交易明細與第 06 章直接入口；策略、交易、帳務、圖表及績效數值完全沿用已驗收 v1.5。",
        text,
        count=1,
        flags=re.MULTILINE,
    )

    section_start = text.index("## 06\n")
    section_end = text.index("## 07\n", section_start)
    section = text[section_start:section_end]
    links = "\n".join(f"- [{label}]({target})" for label, target in TRADE_LINKS)
    details = (
        "### 逐筆交易明細\n\n"
        "下列明細均來自本報告已驗收的原始交易帳。中文閱讀版將每一筆進場、出場、股數、費稅、股息、淨損益、報酬率、最大有利／不利變動與初始防守價並排呈現。\n\n"
        + links
        + "\n\n"
    )
    marker = "### 白話結論"
    if marker not in section:
        raise ETF0050Error("section 06 has no plain-language conclusion marker")
    section = section.replace(marker, details + marker, 1)
    text = text[:section_start] + section + text[section_end:]

    attachment_heading = "## 14\n\n### 完整附件目錄"
    attachment_start = text.index(attachment_heading)
    attachment_links = "\n".join(f"- [{name}]({name})" for name in attachments)
    text = text[:attachment_start] + attachment_heading + "\n\n" + attachment_links + "\n"
    text = text.replace(
        "python -m backtests.dual_ma.etf_0050_final_strategy_v1_5",
        "python -m backtests.dual_ma.etf_0050_final_strategy_v1_6",
    )
    text = text.replace(
        "tests/backtests/dual_ma/test_etf_0050_final_strategy_v1_5.py",
        "tests/backtests/dual_ma/test_etf_0050_final_strategy_v1_6.py",
    )
    text = text.replace("`v1-5` 結尾", "`v1-6` 結尾")
    return text


def _trade_detail_errors(root: Path) -> list[str]:
    errors: list[str] = []
    rows = v14._read_csv(root / "strategy-trade-details.csv")
    text = (root / "trade-details.md").read_text(encoding="utf-8")
    expected = {"SIGNAL_CLOSE": 92, "NEXT_SESSION_OPEN": 91}
    actual = {timing: sum(row["execution_timing"] == timing for row in rows) for timing in expected}
    if len(rows) != 183:
        errors.append(f"combined trade row count changed: {len(rows)}")
    if actual != expected:
        errors.append(f"trade timing counts changed: {actual}")
    markdown_rows = sum(
        1 for line in text.splitlines() if re.match(r"^\|\d+\|", line)
    )
    if markdown_rows != len(rows):
        errors.append(f"Chinese trade table row count mismatch: {markdown_rows} != {len(rows)}")
    for label, target in TRADE_LINKS:
        if f"[{label}]({target})" not in text and target != "trade-details.md":
            errors.append(f"trade detail source link missing: {target}")
    return errors


def _contract_errors(text: str) -> list[str]:
    errors = v15._contract_errors(text)
    if "# 0050 最終策略定版報告｜正式 v1.6" not in text:
        errors.append("v1.6 title missing")
    if "### 逐筆交易明細" not in text:
        errors.append("prominent trade detail subsection missing")
    for label, target in TRADE_LINKS:
        if f"[{label}]({target})" not in text:
            errors.append(f"prominent trade link missing: {target}")
    return errors


def run_study(
    *, run_root: Path = RUN_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT
) -> dict[str, Any]:
    if run_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite isolated v1.6 output")
    pins = (
        (SOURCE_RUN / "manifest.json", SOURCE_RUN_MANIFEST),
        (SOURCE_REPORT / "artifact-manifest.json", SOURCE_REPORT_MANIFEST),
        (SOURCE_RUN / "readback.json", SOURCE_READBACK),
        (SOURCE_REPORT / "readback.json", SOURCE_READBACK),
    )
    for path, expected in pins:
        if v14._sha(path) != expected:
            raise ETF0050Error(f"sealed v1.5 pin changed: {path}")
    before = {
        str(SOURCE_RUN): tree_hash(SOURCE_RUN),
        str(SOURCE_REPORT): tree_hash(SOURCE_REPORT),
    }
    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        encoding="utf-8",
    ).strip()
    produced_iso = datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
    produced = datetime.fromisoformat(produced_iso).strftime("%Y-%m-%d %H:%M:%S（台北）")
    with tempfile.TemporaryDirectory(prefix="0050-final-v1-6-") as tmp:
        stage = Path(tmp)
        run_stage = stage / "run"
        report_stage = stage / "report"
        run_stage.mkdir()
        report_stage.mkdir()
        v14._copy_files(
            SOURCE_RUN,
            run_stage,
            {"manifest.json", "readback.json", "checkpoint.json", "QA_READY.json"},
        )
        _write_json(
            run_stage / "v1-6-input-manifest.json",
            {
                "version": VERSION,
                "status": "ACCEPT",
                "produced_at_taipei": produced_iso,
                "git_commit": git_commit,
                "source_run_manifest_sha256": SOURCE_RUN_MANIFEST,
                "source_report_manifest_sha256": SOURCE_REPORT_MANIFEST,
                "source_readback_sha256": SOURCE_READBACK,
                "scope": "presentation-only Chinese trade-detail revision; no strategy, transaction, accounting, chart, or performance changes",
            },
        )
        run_excluded = {"manifest.json", "readback.json", "checkpoint.json", "QA_READY.json"}
        run_artifacts = _artifact_rows(run_stage, run_excluded)
        _write_json(
            run_stage / "manifest.json",
            {
                "status": "ACCEPT",
                "version": VERSION,
                "produced_at_taipei": produced_iso,
                "git_commit": git_commit,
                "artifact_count": len(run_artifacts),
                "artifacts": run_artifacts,
            },
        )
        run_bad = _verify_artifacts(run_stage, run_artifacts)
        _write_json(run_stage / "readback.json", {"status": "PASS" if not run_bad else "FAIL", "mismatches": run_bad})
        _write_json(run_stage / "QA_READY.json", {"status": "ACCEPT", "version": VERSION, "ready_for_claimed_acceptance": True})
        _write_json(
            run_stage / "checkpoint.json",
            {
                "status": "ACCEPT",
                "version": VERSION,
                "manifest_sha256": v14._sha(run_stage / "manifest.json"),
                "readback_sha256": v14._sha(run_stage / "readback.json"),
                "safe_next_step": "use as the formal 0050 report entry",
            },
        )
        if run_bad:
            raise ETF0050Error(f"run readback failed: {run_bad}")

        v14._copy_files(
            SOURCE_REPORT,
            report_stage,
            {"README.md", "report.md", "artifact-manifest.json", "readback.json", "checkpoint.json"},
        )
        trade_rows = v14._read_csv(report_stage / "strategy-trade-details.csv")
        (report_stage / "trade-details.md").write_text(
            _render_trade_details(trade_rows, produced), encoding="utf-8", newline="\n"
        )
        attachments = [
            "report.md",
            *sorted(path.name for path in report_stage.iterdir() if path.is_file()),
            "artifact-manifest.json",
            "readback.json",
            "checkpoint.json",
        ]
        report = _render_report(
            (SOURCE_REPORT / "README.md").read_text(encoding="utf-8"),
            produced,
            attachments,
        )
        (report_stage / "README.md").write_text(report, encoding="utf-8", newline="\n")
        (report_stage / "report.md").write_text(report, encoding="utf-8", newline="\n")
        for control in ("artifact-manifest.json", "readback.json", "checkpoint.json"):
            _write_json(report_stage / control, {"status": "BUILDING", "version": VERSION})
        errors = _contract_errors(report)
        broken = v15._broken_links(report_stage)
        trade_errors = _trade_detail_errors(report_stage)
        chart_errors = v15._chart_language_errors(report_stage)
        report_excluded = {"artifact-manifest.json", "readback.json", "checkpoint.json"}
        report_artifacts = _artifact_rows(report_stage, report_excluded)
        missing = v12.research._readme_missing_artifacts(
            report_stage, [str(row["path"]) for row in report_artifacts]
        )
        if errors or broken or trade_errors or chart_errors or missing:
            raise ETF0050Error(
                f"report contract failed: {errors}; links={broken}; trades={trade_errors}; charts={chart_errors}; coverage={missing}"
            )
        _write_json(
            report_stage / "artifact-manifest.json",
            {
                "status": "ACCEPT",
                "version": VERSION + "-report",
                "produced_at_taipei": produced_iso,
                "source_report_manifest_sha256": SOURCE_REPORT_MANIFEST,
                "artifact_count": len(report_artifacts),
                "artifacts": report_artifacts,
            },
        )
        report_bad = _verify_artifacts(report_stage, report_artifacts)
        _write_json(report_stage / "readback.json", {"status": "PASS" if not report_bad else "FAIL", "mismatches": report_bad})
        _write_json(
            report_stage / "checkpoint.json",
            {
                "status": "ACCEPT",
                "version": VERSION,
                "artifact_manifest_sha256": v14._sha(report_stage / "artifact-manifest.json"),
                "readback_sha256": v14._sha(report_stage / "readback.json"),
                "safe_next_step": "use as the formal 0050 report entry",
            },
        )
        if report_bad:
            raise ETF0050Error(f"report readback failed: {report_bad}")
        after = {
            str(SOURCE_RUN): tree_hash(SOURCE_RUN),
            str(SOURCE_REPORT): tree_hash(SOURCE_REPORT),
        }
        if before != after:
            raise ETF0050Error("sealed v1.5 source changed")
        run_root.parent.mkdir(parents=True, exist_ok=True)
        report_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(run_stage, run_root)
        shutil.copytree(report_stage, report_root)
    return {
        "status": "ACCEPT",
        "run_root": str(run_root),
        "report_root": str(report_root),
        "run_manifest_sha256": v14._sha(run_root / "manifest.json"),
        "report_manifest_sha256": v14._sha(report_root / "artifact-manifest.json"),
        "readback_sha256": v14._sha(run_root / "readback.json"),
        "source_trees_unchanged": True,
    }


def validate(
    *, run_root: Path = RUN_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT
) -> dict[str, Any]:
    run_manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8-sig"))
    report_manifest = json.loads((report_root / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
    run_bad = _verify_artifacts(run_root, run_manifest["artifacts"])
    report_bad = _verify_artifacts(report_root, report_manifest["artifacts"])
    text = (report_root / "README.md").read_text(encoding="utf-8")
    contract = _contract_errors(text)
    broken = v15._broken_links(report_root)
    trade_errors = _trade_detail_errors(report_root)
    chart_errors = v15._chart_language_errors(report_root)
    missing = v12.research._readme_missing_artifacts(
        report_root, [str(row["path"]) for row in report_manifest["artifacts"]]
    )
    exact_names = (
        "main-comparison-summary.csv",
        "main-comparison-calendar-year.csv",
        "main-comparison-daily.csv",
        "strategy-transactions.csv",
        "strategy-trade-details.csv",
        *v15.CHARTS,
    )
    inherited_ok = all(
        v14._sha(SOURCE_REPORT / name) == v14._sha(report_root / name) for name in exact_names
    )
    pins_ok = (
        v14._sha(SOURCE_RUN / "manifest.json") == SOURCE_RUN_MANIFEST
        and v14._sha(SOURCE_REPORT / "artifact-manifest.json") == SOURCE_REPORT_MANIFEST
    )
    status = "PASS" if not any(
        (run_bad, report_bad, contract, broken, trade_errors, chart_errors, missing)
    ) and inherited_ok and pins_ok else "FAIL"
    return {
        "status": status,
        "run_artifacts": len(run_manifest["artifacts"]),
        "report_artifacts": len(report_manifest["artifacts"]),
        "run_mismatches": run_bad,
        "report_mismatches": report_bad,
        "report_contract_errors": contract,
        "broken_links_or_anchors": broken,
        "trade_detail_errors": trade_errors,
        "chart_language_errors": chart_errors,
        "readme_missing_artifacts": missing,
        "key_data_and_charts_exact": inherited_ok,
        "source_pins": pins_ok,
        "network_used": False,
        "writes_performed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("run", "validate"), nargs="?", default="run")
    parser.add_argument("--run-root", type=Path, default=RUN_ROOT_DEFAULT)
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT_DEFAULT)
    args = parser.parse_args(argv)
    result = (
        validate(run_root=args.run_root, report_root=args.report_root)
        if args.mode == "validate"
        else run_study(run_root=args.run_root, report_root=args.report_root)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"PASS", "ACCEPT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
