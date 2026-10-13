#!/usr/bin/env python3
"""Build and validate the fixed dual-MA backtest report catalog."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO_ROOT / ".workdata" / "reports" / "dual-ma"
DEFAULT_REGISTRY = DEFAULT_ROOT / "report-catalog.json"
DEFAULT_OUTPUT = DEFAULT_ROOT / "README.md"
TAIPEI_TZ = timezone(timedelta(hours=8))
TIMESTAMP_FIELDS = (
    "completed_at",
    "completed_at_utc",
    "generated_at",
    "generated_at_utc",
    "created_at",
    "created_at_utc",
    "updated_at",
    "updated_at_utc",
    "finished_at",
    "finished_at_utc",
    "sealed_at",
    "sealed_at_utc",
    "timestamp",
)
TIMESTAMP_FILES = (
    "checkpoint.json",
    "input-manifest.json",
    "input_manifest.json",
    "artifact-manifest.json",
    "artifact_manifest.json",
    "readback.json",
)

FILE_LABELS = {
    "README.md": "閱讀入口",
    "report.md": "主報告",
    "2022-2026-overview.md": "總覽",
    "2022-2026-source-strategy-overview.md": "來源×策略總覽",
    "shared-account-report.md": "共享帳戶報告",
    "benchmark-risk-report.md": "基準與風險",
    "trade-details.md": "逐筆交易（易讀版）",
    "trade-details.csv": "逐筆交易 CSV",
    "transactions.csv": "完整交易流水",
    "daily-equity.csv": "每日資產",
    "calendar-year-summary.csv": "逐年摘要",
    "scenario-summary.csv": "情境摘要",
    "all-scenarios-summary.csv": "全情境摘要",
    "all-scenarios-trade-details.csv": "全情境逐筆交易",
    "all-scenarios-transactions.csv": "全情境交易流水",
    "all-scenarios-daily-equity.csv": "全情境每日資產",
    "all-scenarios-corporate-actions.csv": "全情境公司行動",
    "signal-opportunity-audit.csv": "訊號機會稽核",
    "macd-event-audit.csv": "MACD／停損事件稽核",
    "fixed-initial-stop-macd-event-audit.csv": "固定初始停損事件稽核",
    "corporate-actions.csv": "股息與分割",
    "benchmark.csv": "基準資料",
    "equity-curves.svg": "資產曲線",
    "drawdown-curves.svg": "回落曲線",
    "input-manifest.json": "輸入清單",
    "input_manifest.json": "輸入清單",
    "artifact-manifest.json": "產物 SHA 清單",
    "artifact_manifest.json": "產物 SHA 清單",
    "readback.json": "讀回驗證",
    "report-parity.json": "報告相容性驗證",
    "QA_ACCEPTED.json": "QA 採納證據",
    "checkpoint.json": "重跑檢查點",
}

PRIORITY = [
    "README.md",
    "report.md",
    "2022-2026-overview.md",
    "2022-2026-source-strategy-overview.md",
    "shared-account-report.md",
    "benchmark-risk-report.md",
    "trade-details.md",
    "trade-details.csv",
    "transactions.csv",
    "daily-equity.csv",
    "calendar-year-summary.csv",
    "scenario-summary.csv",
    "all-scenarios-summary.csv",
    "all-scenarios-trade-details.csv",
    "all-scenarios-transactions.csv",
    "all-scenarios-daily-equity.csv",
    "signal-opportunity-audit.csv",
    "macd-event-audit.csv",
    "fixed-initial-stop-macd-event-audit.csv",
    "corporate-actions.csv",
    "all-scenarios-corporate-actions.csv",
    "benchmark.csv",
    "equity-curves.svg",
    "drawdown-curves.svg",
    "input-manifest.json",
    "input_manifest.json",
    "artifact-manifest.json",
    "artifact_manifest.json",
    "readback.json",
    "report-parity.json",
    "QA_ACCEPTED.json",
    "checkpoint.json",
]


def load_registry(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def rel_link(path: Path, output: Path) -> str:
    return Path(path).relative_to(output.parent).as_posix()


def md_link(label: str, path: Path, output: Path) -> str:
    safe_label = label.replace("|", "\\|")
    return f"[{safe_label}]({rel_link(path, output)})"


def discover_report_dirs(root: Path) -> set[str]:
    discovered: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or "history" in path.relative_to(root).parts:
            continue
        if path.name in {"README.md", "report.md"} or path.name.endswith("overview.md"):
            rel = path.parent.relative_to(root).as_posix()
            discovered.add(rel if rel != "." else ".")
    return discovered


def ordered_files(directory: Path) -> list[Path]:
    files = [path for path in directory.iterdir() if path.is_file()]
    rank = {name: index for index, name in enumerate(PRIORITY)}
    return sorted(files, key=lambda path: (rank.get(path.name, len(rank)), path.name.lower()))


def file_label(path: Path) -> str:
    if path.name in FILE_LABELS:
        return FILE_LABELS[path.name]
    if re.fullmatch(r"20\d{2}-report\.md", path.name):
        return f"{path.name[:4]} 年報告"
    return path.name


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(TAIPEI_TZ)


def report_produced_time(entry: dict, directory: Path) -> tuple[datetime, str]:
    explicit = parse_timestamp(entry.get("produced_at"))
    if explicit is not None:
        return explicit, "登錄時間"

    candidates: list[tuple[datetime, str]] = []
    for name in TIMESTAMP_FILES:
        path = directory / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for field in TIMESTAMP_FIELDS:
            parsed = parse_timestamp(payload.get(field))
            if parsed is not None:
                candidates.append((parsed, f"{name}:{field}"))
    if candidates:
        return max(candidates, key=lambda item: item[0])

    entry_file = directory / entry["entry"]
    modified = datetime.fromtimestamp(entry_file.stat().st_mtime, tz=TAIPEI_TZ)
    return modified, "主報告檔案時間"


def formatted_produced_time(entry: dict, directory: Path) -> str:
    produced, source = report_produced_time(entry, directory)
    return f"{produced:%Y-%m-%d %H:%M}（{source}）"


def validate_registry(registry: dict, root: Path) -> list[str]:
    errors: list[str] = []
    entries = registry.get("entries", [])
    ids: set[str] = set()
    directories: set[str] = set()
    for entry in entries:
        entry_id = entry.get("id", "")
        directory_rel = entry.get("directory", "")
        if not entry_id or entry_id in ids:
            errors.append(f"重複或缺少 id：{entry_id!r}")
        ids.add(entry_id)
        if not directory_rel or directory_rel in directories:
            errors.append(f"重複或缺少 directory：{directory_rel!r}")
        directories.add(directory_rel)
        directory = root if directory_rel == "." else root / directory_rel
        if not directory.is_dir():
            errors.append(f"目錄不存在：{directory_rel}")
            continue
        entry_file = directory / entry.get("entry", "")
        if not entry_file.is_file():
            errors.append(f"主入口不存在：{entry_file}")

    ignored = set(registry.get("ignored_directories", []))
    unknown = discover_report_dirs(root) - directories - ignored
    for directory in sorted(unknown):
        errors.append(f"發現尚未登錄的報告目錄：{directory}")
    return errors


def build_readme(registry: dict, root: Path, output: Path) -> str:
    entries = registry["entries"]
    by_id = {entry["id"]: entry for entry in entries}
    lines = [
        "# 雙均線與 0050 回測報告總目錄",
        "",
        "> 這是所有回測與研究報告的固定入口。正式採納、新策略實驗、執行分析與歷史版本分開列示；舊產物保留原位，不搬移、不覆寫。",
        "",
        "## 快速入口",
        "",
        "|報告|期間|產出時間（台北）|用途|",
        "|---|---|---|---|",
    ]
    for entry_id in registry.get("quick_entries", []):
        entry = by_id[entry_id]
        directory = root if entry["directory"] == "." else root / entry["directory"]
        lines.append(
            f"|{md_link(entry['title'], directory / entry['entry'], output)}|"
            f"{entry['period']}|{formatted_produced_time(entry, directory)}|{entry['summary']}|"
        )

    lines.extend(
        [
            "",
            "## 怎麼使用這份目錄",
            "",
            "- **正式採納**：目前可當作基準或正式比較使用的報告。",
            "- **策略實驗**：規則已完整重播並保留附件，但是否納入正式策略仍需由你決定。",
            "- **分析與執行研究**：用來回答成交、資金、候選排序等問題，不等於正式策略績效。",
            "- **歷史／已取代**：只供追溯，不應拿來代替新版報告。",
            "- 有「閱讀入口」時先從它開始；逐筆交易、每日資產、逐年摘要、事件稽核與 SHA/readback 都列在同一項下。",
            "- 產出時間一律換算為台北時間；優先採 manifest／checkpoint 的明確紀錄，缺少紀錄的舊報告會標示為「主報告檔案時間」。",
            "",
        ]
    )

    for section in registry["sections"]:
        section_entries = [entry for entry in entries if entry["section"] == section["id"]]
        lines.extend([f"## {section['title']}", "", section["description"], ""])
        if section.get("compact", False):
            lines.extend(["|報告|期間|產出時間（台北）|狀態|取代版本|", "|---|---|---|---|---|"])
            for entry in section_entries:
                directory = root if entry["directory"] == "." else root / entry["directory"]
                replaced = entry.get("replaced_by", "—")
                if replaced in by_id:
                    target = by_id[replaced]
                    target_dir = root if target["directory"] == "." else root / target["directory"]
                    replaced = md_link(target["title"], target_dir / target["entry"], output)
                lines.append(
                    f"|{md_link(entry['title'], directory / entry['entry'], output)}|"
                    f"{entry['period']}|{formatted_produced_time(entry, directory)}|{entry['status']}|{replaced}|"
                )
            lines.append("")
            continue

        for entry in section_entries:
            directory = root if entry["directory"] == "." else root / entry["directory"]
            entry_file = directory / entry["entry"]
            lines.extend(
                [
                    f"### {entry['title']}",
                    "",
                    f"- 期間：{entry['period']}",
                    f"- 產出時間（台北）：{formatted_produced_time(entry, directory)}",
                    f"- 狀態：{entry['status']}",
                    f"- 內容：{entry['summary']}",
                    f"- 主入口：{md_link(file_label(entry_file), entry_file, output)}",
                ]
            )
            if entry.get("show_files", False):
                links = []
                for path in ordered_files(directory):
                    if path == entry_file:
                        continue
                    links.append(md_link(file_label(path), path, output))
                if links:
                    lines.append(f"- 完整附件：{' ｜ '.join(links)}")
            lines.append("")

    lines.extend(
        [
            "## 固定年度入口與報告規格",
            "",
            "- [2022 年](2022-report.md) ｜ [2023 年](2023-report.md) ｜ [2024 年](2024-report.md) ｜ [2025 年](2025-report.md) ｜ [2026 年 1～9 月](2026-report.md)",
            "- [固定報告格式 v2（目前預設）](report-template-v2.md)",
            "- [舊固定報告格式 v1（歷史留存）](report-template-v1.md)",
            "",
            "`details` 內的完整逐筆附件使用硬連結連到已採納的版本化報告，不會重複占用一份檔案空間。原始版本化產物仍保留原位。",
            "",
            "除非另有指定，往後回測一律採用 v2：單一帳戶只投入一次本金、跨年連續操作，年度資料只從同一條資產曲線切片，不做年度重置。",
            "",
            "## 新報告如何納入",
            "",
            "1. 新報告維持獨立版本目錄，主報告與附件不覆寫舊版本。",
            "2. 在 `report-catalog.json` 增加一筆登錄。",
            "3. 執行 `python scripts/dual_ma_report_catalog.py build` 更新本頁。",
            "4. 執行 `python scripts/dual_ma_report_catalog.py validate`；遺漏報告目錄、失效入口或壞連結會直接失敗。",
            "",
        ]
    )
    return "\n".join(lines)


def validate_readme_links(output: Path) -> list[str]:
    errors: list[str] = []
    if not output.is_file():
        return [f"總目錄不存在：{output}"]
    text = output.read_text(encoding="utf-8-sig")
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#")):
            continue
        path_text = unquote(target.split("#", 1)[0])
        path = (output.parent / Path(path_text)).resolve()
        if not path.exists():
            errors.append(f"README 壞連結：{target}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "validate"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    registry = load_registry(args.registry)
    errors = validate_registry(registry, args.root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if args.command == "build":
        content = build_readme(registry, args.root, args.output)
        args.output.write_text(content, encoding="utf-8", newline="\n")
        print(f"BUILT: {args.output}")

    link_errors = validate_readme_links(args.output)
    if link_errors:
        for error in link_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"VALID: {len(registry['entries'])} reports; all catalog links resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
