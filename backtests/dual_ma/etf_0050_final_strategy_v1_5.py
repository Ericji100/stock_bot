"""Publish the accepted 0050 report with app-safe navigation and Chinese charts.

This is a presentation-only revision of the sealed v1.3-r3 package.  It does
not replay signals, trades, accounting, or performance.  The report uses
pure Markdown numeric anchors because the Codex Markdown viewer displays raw
HTML anchors as text.  All five overview charts are rendered with Chinese
titles, legends, and axis descriptions.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import etf_0050_final_strategy_v1 as v12
from . import etf_0050_final_strategy_v1_3 as v13
from . import etf_0050_final_strategy_v1_4 as v14
from .etf_0050_base_v1 import ETF0050Error, tree_hash
from .etf_0050_formal_v2 import _artifact_rows, _verify_artifacts, _write_json
from .paths import dev_run, report_path


VERSION = "dual-ma-0050-final-strategy-v1.5"
SOURCE_RUN = v14.SOURCE_RUN
SOURCE_REPORT = v14.SOURCE_REPORT
SOURCE_RUN_MANIFEST = v14.SOURCE_RUN_MANIFEST
SOURCE_REPORT_MANIFEST = v14.SOURCE_REPORT_MANIFEST
SOURCE_READBACK = v14.SOURCE_READBACK
RUN_ROOT_DEFAULT = dev_run("02-etf-0050-final-strategy-v1-5")
REPORT_ROOT_DEFAULT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-5")

SECTION_NAV = tuple(
    (f"{index:02d}", heading)
    for index, (heading, _old_anchor) in enumerate(v14.SECTION_ANCHORS, start=1)
)
CHARTS = v14.CHARTS


def _add_axis_note(path: Path, note: str) -> None:
    """Add an unobtrusive Chinese axis description below the chart title."""
    text = path.read_text(encoding="utf-8")
    marker = "</svg>"
    if marker not in text:
        raise ETF0050Error(f"invalid SVG without closing tag: {path}")
    label = (
        '<text x="1206" y="52" text-anchor="end" font-size="13" '
        f'fill="#4b5563">{v14.html.escape(note)}</text>\n'
    )
    path.write_text(text.replace(marker, label + marker, 1), encoding="utf-8", newline="\n")


def _make_charts(root: Path) -> None:
    """Regenerate all five charts with Chinese reader-facing text."""
    v14._make_charts(root)

    daily = v14._read_csv(root / "main-comparison-daily.csv")
    grouped_equity: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for row in daily:
        grouped_equity[row["comparison_id"]].append(
            (date.fromisoformat(row["date"]), v14._f(row["equity"]))
        )
    v14._line_chart(
        root / "main-comparison-equity.svg",
        "0050 最終策略：五條比較線資產曲線（初始本金 50 萬元）",
        [
            (v13.NAMES[item], v14.COLORS[item], grouped_equity[item])
            for item in v12.MAIN_IDS
        ],
    )

    notes = {
        "main-comparison-equity.svg": "橫軸：日期｜縱軸：資產價值（元）",
        "main-comparison-drawdown.svg": "橫軸：日期｜縱軸：最大回落",
        "calendar-year-returns.svg": "橫軸：年度｜縱軸：年度報酬率",
        "execution-cost-sensitivity.svg": "橫軸：每邊滑價（基點）｜縱軸：年化報酬率",
        "parameter-robustness.svg": "橫軸：最大回落｜縱軸：年化報酬率",
    }
    for name, note in notes.items():
        _add_axis_note(root / name, note)


def _render_report(source_text: str, produced: str, attachments: Sequence[str]) -> str:
    """Render v1.4 content, then replace HTML anchors with pure Markdown."""
    text = v14._render_report(source_text, produced, attachments)
    text = re.sub(r'^<a id="[^"]+"></a>\n\n', "", text, flags=re.MULTILINE)

    for (heading, old_anchor), (number, expected_heading) in zip(
        v14.SECTION_ANCHORS, SECTION_NAV, strict=True
    ):
        if heading != expected_heading:
            raise ETF0050Error("section navigation order changed")
        text = text.replace(
            f"[{heading}](#{old_anchor})", f"[{number}｜{heading}](#{number})", 1
        )
        text = text.replace(
            f"## {heading}", f"## {number}\n\n### {heading}", 1
        )

    text = text.replace(
        "# 0050 最終策略定版報告｜正式 v1.4",
        "# 0050 最終策略定版報告｜正式 v1.5",
        1,
    )
    text = text.replace(
        "本版只修正閱讀導覽與圖表呈現",
        "本版修正 Codex 可點擊導覽，並將五張圖完整中文化",
        1,
    )
    text = text.replace(
        "python -m backtests.dual_ma.etf_0050_final_strategy_v1_4",
        "python -m backtests.dual_ma.etf_0050_final_strategy_v1_5",
    )
    text = text.replace(
        "tests/backtests/dual_ma/test_etf_0050_final_strategy_v1_4.py",
        "tests/backtests/dual_ma/test_etf_0050_final_strategy_v1_5.py",
    )
    text = text.replace("`v1-4` 結尾", "`v1-5` 結尾")
    return text


def _broken_links(root: Path) -> list[str]:
    broken: list[str] = []
    for md in root.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        headings = {
            v12._markdown_anchor(line.lstrip("#").strip())
            for line in text.splitlines()
            if line.startswith("#")
        }
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            target_name, marker, fragment = target.partition("#")
            if target_name.startswith(("http://", "https://")):
                continue
            target_path = md if not target_name else root / target_name
            if not target_path.is_file():
                broken.append(f"{md.name}->{target}")
                continue
            if marker and target_path.suffix.lower() == ".md" and fragment not in headings:
                broken.append(f"{md.name}->{target} (missing anchor)")
    return broken


def _contract_errors(text: str) -> list[str]:
    errors: list[str] = []
    if "狀態：`ACCEPT`" not in text:
        errors.append("accepted status missing")
    if re.search(r"<a\s+id=", text, flags=re.IGNORECASE):
        errors.append("raw HTML anchor remains")
    for number, heading in SECTION_NAV:
        if f"[{number}｜{heading}](#{number})" not in text:
            errors.append(f"toc link missing: {number}")
        if f"## {number}\n\n### {heading}" not in text:
            errors.append(f"pure Markdown anchor missing: {number}")
    for chart in CHARTS:
        if f"]({chart})" not in text:
            errors.append(f"chart not embedded: {chart}")
    if "候選狀態" in text or "STOP_WAIT_00_QA" in text:
        errors.append("stale candidate status remains")
    return errors


def _chart_language_errors(root: Path) -> list[str]:
    errors: list[str] = []
    forbidden = tuple(v12.MAIN_IDS) + ("SIGNAL_CLOSE", "NEXT_SESSION_OPEN")
    for name in CHARTS:
        text = (root / name).read_text(encoding="utf-8")
        visible_text = " ".join(re.findall(r"<text[^>]*>(.*?)</text>", text))
        if not re.search(r"[\u4e00-\u9fff]", visible_text):
            errors.append(f"chart has no Chinese text: {name}")
        for token in forbidden:
            if token in visible_text:
                errors.append(f"chart exposes internal English id: {name}:{token}")
    return errors


def run_study(
    *, run_root: Path = RUN_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT
) -> dict[str, Any]:
    if run_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite isolated v1.5 output")
    pins = (
        (SOURCE_RUN / "manifest.json", SOURCE_RUN_MANIFEST),
        (SOURCE_REPORT / "artifact-manifest.json", SOURCE_REPORT_MANIFEST),
        (SOURCE_RUN / "readback.json", SOURCE_READBACK),
        (SOURCE_REPORT / "readback.json", SOURCE_READBACK),
    )
    for path, expected in pins:
        if v14._sha(path) != expected:
            raise ETF0050Error(f"sealed v1.3-r3 pin changed: {path}")
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
    with tempfile.TemporaryDirectory(prefix="0050-final-v1-5-") as tmp:
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
            run_stage / "v1-5-input-manifest.json",
            {
                "version": VERSION,
                "status": "ACCEPT",
                "produced_at_taipei": produced_iso,
                "git_commit": git_commit,
                "source_run_manifest_sha256": SOURCE_RUN_MANIFEST,
                "source_report_manifest_sha256": SOURCE_REPORT_MANIFEST,
                "source_readback_sha256": SOURCE_READBACK,
                "scope": "presentation-only app-safe navigation and Chinese chart revision; no strategy, transaction, accounting, or performance changes",
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
        _write_json(
            run_stage / "readback.json",
            {"status": "PASS" if not run_bad else "FAIL", "mismatches": run_bad},
        )
        _write_json(
            run_stage / "QA_READY.json",
            {"status": "ACCEPT", "version": VERSION, "ready_for_claimed_acceptance": True},
        )
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
        _make_charts(report_stage)
        attachments = [
            "report.md",
            *sorted(path.name for path in report_stage.iterdir() if path.is_file()),
            "artifact-manifest.json",
            "readback.json",
            "checkpoint.json",
        ]
        source_text = (SOURCE_REPORT / "README.md").read_text(encoding="utf-8")
        report = _render_report(source_text, produced, attachments)
        (report_stage / "README.md").write_text(report, encoding="utf-8", newline="\n")
        (report_stage / "report.md").write_text(report, encoding="utf-8", newline="\n")
        for control in ("artifact-manifest.json", "readback.json", "checkpoint.json"):
            _write_json(report_stage / control, {"status": "BUILDING", "version": VERSION})
        errors = _contract_errors(report)
        broken = _broken_links(report_stage)
        chart_errors = _chart_language_errors(report_stage)
        report_excluded = {"artifact-manifest.json", "readback.json", "checkpoint.json"}
        report_artifacts = _artifact_rows(report_stage, report_excluded)
        missing = v12.research._readme_missing_artifacts(
            report_stage, [str(row["path"]) for row in report_artifacts]
        )
        if errors or broken or chart_errors or missing:
            raise ETF0050Error(
                f"report contract failed: {errors}; links={broken}; charts={chart_errors}; coverage={missing}"
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
        _write_json(
            report_stage / "readback.json",
            {"status": "PASS" if not report_bad else "FAIL", "mismatches": report_bad},
        )
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
            raise ETF0050Error("sealed v1.3-r3 source changed")
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
    report_manifest = json.loads(
        (report_root / "artifact-manifest.json").read_text(encoding="utf-8-sig")
    )
    run_bad = _verify_artifacts(run_root, run_manifest["artifacts"])
    report_bad = _verify_artifacts(report_root, report_manifest["artifacts"])
    text = (report_root / "README.md").read_text(encoding="utf-8")
    contract = _contract_errors(text)
    broken = _broken_links(report_root)
    chart_errors = _chart_language_errors(report_root)
    missing = v12.research._readme_missing_artifacts(
        report_root, [str(row["path"]) for row in report_manifest["artifacts"]]
    )
    inherited_ok = all(
        v14._sha(SOURCE_REPORT / name) == v14._sha(report_root / name)
        for name in (
            "main-comparison-summary.csv",
            "main-comparison-calendar-year.csv",
            "main-comparison-daily.csv",
            "strategy-transactions.csv",
            "execution-cost-sensitivity.csv",
            "parameter-results-243.csv",
        )
    )
    pins_ok = (
        v14._sha(SOURCE_RUN / "manifest.json") == SOURCE_RUN_MANIFEST
        and v14._sha(SOURCE_REPORT / "artifact-manifest.json") == SOURCE_REPORT_MANIFEST
    )
    charts_ok = all(
        (report_root / name).is_file() and (report_root / name).stat().st_size > 1000
        for name in CHARTS
    )
    status = "PASS" if not any(
        (run_bad, report_bad, contract, broken, chart_errors, missing)
    ) and inherited_ok and pins_ok and charts_ok else "FAIL"
    return {
        "status": status,
        "run_artifacts": len(run_manifest["artifacts"]),
        "report_artifacts": len(report_manifest["artifacts"]),
        "run_mismatches": run_bad,
        "report_mismatches": report_bad,
        "report_contract_errors": contract,
        "broken_links_or_anchors": broken,
        "chart_language_errors": chart_errors,
        "readme_missing_artifacts": missing,
        "key_data_artifacts_exact": inherited_ok,
        "source_pins": pins_ok,
        "charts_present": charts_ok,
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
