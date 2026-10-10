"""Publish the accepted 0050 report with bidirectional document links.

This is a navigation-only revision of the sealed v1.6 package.  It preserves
all strategy, transaction, accounting, chart, and performance artifacts while
adding clear links between the main report and the Chinese trade ledger.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from . import etf_0050_final_strategy_v1 as v12
from . import etf_0050_final_strategy_v1_4 as v14
from . import etf_0050_final_strategy_v1_5 as v15
from . import etf_0050_final_strategy_v1_6 as v16
from .etf_0050_base_v1 import ETF0050Error, tree_hash
from .etf_0050_formal_v2 import _artifact_rows, _verify_artifacts, _write_json
from .paths import dev_run, report_path


VERSION = "dual-ma-0050-final-strategy-v1.7"
SOURCE_RUN = dev_run("02-etf-0050-final-strategy-v1-6")
SOURCE_REPORT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-6")
SOURCE_RUN_MANIFEST = "3453215D77A11CF02F395A04EE64C5725E65D35F0D9FA0EFA5C9DA905B4415D4"
SOURCE_REPORT_MANIFEST = "D782FB18D6766C96DD20D5166B776D27275BC66032A28A676119A3C736ADA0E7"
SOURCE_READBACK = "366FDCC7BB4C554F467C0942E9B85C652F7C37B82A070E1C3878634586751C94"
RUN_ROOT_DEFAULT = dev_run("02-etf-0050-final-strategy-v1-7")
REPORT_ROOT_DEFAULT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-7")

MAIN_TO_TRADES = "[繁體中文逐筆交易明細](trade-details.md)"
TRADES_TO_MAIN = "[返回 0050 最終策略 v1.7 主報告](README.md)"


def _render_main_report(source_text: str, produced: str, attachments: Sequence[str]) -> str:
    text = re.sub(
        r"^# .*?$",
        "# 0050 最終策略定版報告｜正式 v1.7",
        source_text,
        count=1,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^產出時間：.*?$", f"產出時間：{produced}", text, count=1, flags=re.MULTILINE
    )
    text = re.sub(
        r"^狀態：`ACCEPT`.*?$",
        "狀態：`ACCEPT`。本版補齊主報告與繁體中文逐筆交易明細的雙向連結；策略、交易、帳務、圖表及績效數值完全沿用已驗收 v1.6。",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    attachment_heading = "## 14\n\n### 完整附件目錄"
    attachment_start = text.index(attachment_heading)
    links = "\n".join(f"- [{name}]({name})" for name in attachments)
    text = text[:attachment_start] + attachment_heading + "\n\n" + links + "\n"
    text = text.replace(
        "python -m backtests.dual_ma.etf_0050_final_strategy_v1_6",
        "python -m backtests.dual_ma.etf_0050_final_strategy_v1_7",
    )
    text = text.replace(
        "tests/backtests/dual_ma/test_etf_0050_final_strategy_v1_6.py",
        "tests/backtests/dual_ma/test_etf_0050_final_strategy_v1_7.py",
    )
    text = text.replace("`v1-6` 結尾", "`v1-7` 結尾")
    return text


def _render_trade_details(source_text: str, produced: str) -> str:
    text = re.sub(
        r"^產出時間：.*?$", f"產出時間：{produced}", source_text, count=1, flags=re.MULTILINE
    )
    title_end = text.index("\n", text.index("# 0050 最終策略逐筆交易明細")) + 1
    intro_link = f"\n{TRADES_TO_MAIN}\n"
    text = text[:title_end] + intro_link + text[title_end:]
    text = text.rstrip() + f"\n\n---\n\n{TRADES_TO_MAIN}\n"
    return text


def _contract_errors(main_text: str, trade_text: str) -> list[str]:
    errors = v15._contract_errors(main_text)
    if "# 0050 最終策略定版報告｜正式 v1.7" not in main_text:
        errors.append("v1.7 title missing")
    if MAIN_TO_TRADES not in main_text:
        errors.append("main report does not link to trade details")
    if trade_text.count(TRADES_TO_MAIN) < 2:
        errors.append("trade details need top and bottom links back to main report")
    return errors


def run_study(
    *, run_root: Path = RUN_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT
) -> dict[str, Any]:
    if run_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite isolated v1.7 output")
    pins = (
        (SOURCE_RUN / "manifest.json", SOURCE_RUN_MANIFEST),
        (SOURCE_REPORT / "artifact-manifest.json", SOURCE_REPORT_MANIFEST),
        (SOURCE_RUN / "readback.json", SOURCE_READBACK),
        (SOURCE_REPORT / "readback.json", SOURCE_READBACK),
    )
    for path, expected in pins:
        if v14._sha(path) != expected:
            raise ETF0050Error(f"sealed v1.6 pin changed: {path}")
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
    with tempfile.TemporaryDirectory(prefix="0050-final-v1-7-") as tmp:
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
            run_stage / "v1-7-input-manifest.json",
            {
                "version": VERSION,
                "status": "ACCEPT",
                "produced_at_taipei": produced_iso,
                "git_commit": git_commit,
                "source_run_manifest_sha256": SOURCE_RUN_MANIFEST,
                "source_report_manifest_sha256": SOURCE_REPORT_MANIFEST,
                "source_readback_sha256": SOURCE_READBACK,
                "scope": "navigation-only bidirectional document-link revision; no strategy, transaction, accounting, chart, or performance changes",
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
            {
                "README.md",
                "report.md",
                "trade-details.md",
                "artifact-manifest.json",
                "readback.json",
                "checkpoint.json",
            },
        )
        trade_text = _render_trade_details(
            (SOURCE_REPORT / "trade-details.md").read_text(encoding="utf-8"), produced
        )
        (report_stage / "trade-details.md").write_text(
            trade_text, encoding="utf-8", newline="\n"
        )
        attachments = [
            "report.md",
            *sorted(path.name for path in report_stage.iterdir() if path.is_file()),
            "artifact-manifest.json",
            "readback.json",
            "checkpoint.json",
        ]
        main_text = _render_main_report(
            (SOURCE_REPORT / "README.md").read_text(encoding="utf-8"), produced, attachments
        )
        (report_stage / "README.md").write_text(main_text, encoding="utf-8", newline="\n")
        (report_stage / "report.md").write_text(main_text, encoding="utf-8", newline="\n")
        for control in ("artifact-manifest.json", "readback.json", "checkpoint.json"):
            _write_json(report_stage / control, {"status": "BUILDING", "version": VERSION})

        errors = _contract_errors(main_text, trade_text)
        broken = v15._broken_links(report_stage)
        trade_errors = v16._trade_detail_errors(report_stage)
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
            raise ETF0050Error("sealed v1.6 source changed")
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
    main_text = (report_root / "README.md").read_text(encoding="utf-8")
    trade_text = (report_root / "trade-details.md").read_text(encoding="utf-8")
    contract = _contract_errors(main_text, trade_text)
    broken = v15._broken_links(report_root)
    trade_errors = v16._trade_detail_errors(report_root)
    chart_errors = v15._chart_language_errors(report_root)
    missing = v12.research._readme_missing_artifacts(
        report_root, [str(row["path"]) for row in report_manifest["artifacts"]]
    )
    changed_names = {"README.md", "report.md", "trade-details.md"}
    inherited_names = [
        row["path"] for row in report_manifest["artifacts"] if row["path"] not in changed_names
    ]
    inherited_ok = all(
        (SOURCE_REPORT / name).is_file()
        and v14._sha(SOURCE_REPORT / name) == v14._sha(report_root / name)
        for name in inherited_names
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
        "unchanged_artifacts_exact": inherited_ok,
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
