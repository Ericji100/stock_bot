from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def inventory(report_root: Path) -> dict[str, Any]:
    report_root = report_root.resolve()
    reports: list[dict[str, Any]] = []
    for directory in sorted(path for path in report_root.rglob("*") if path.is_dir()):
        entries = {path.name for path in directory.iterdir() if path.is_file()}
        if not entries.intersection({"README.md", "report.md", "artifact-manifest.json"}):
            continue
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        tree_digest = hashlib.sha256()
        for path in files:
            relative = path.relative_to(directory).as_posix()
            tree_digest.update(relative.encode("utf-8"))
            tree_digest.update(b"\0")
            tree_digest.update(bytes.fromhex(_sha256(path)))
        reports.append(
            {
                "relative_path": directory.relative_to(report_root).as_posix(),
                "file_count": len(files),
                "bytes": sum(path.stat().st_size for path in files),
                "tree_sha256": tree_digest.hexdigest().upper(),
                "entrypoints": sorted(
                    name for name in ("README.md", "report.md") if name in entries
                ),
                "artifact_manifest": (
                    _sha256(directory / "artifact-manifest.json")
                    if "artifact-manifest.json" in entries
                    else None
                ),
            }
        )
    return {
        "schema_version": "stock-ai-bot-etf-report-inventory-v1",
        "policy": "PRESERVE_ALL_REPORTS_IN_PLACE",
        "report_root": str(report_root),
        "report_count": len(reports),
        "reports": reports,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a read-only ETF report inventory")
    parser.add_argument("report_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    payload = inventory(args.report_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "PASS", "report_count": payload["report_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
