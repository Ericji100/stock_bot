"""Compare Stage B1a1 smoke rounds and treat preserved invalid raw as terminal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import v2_core_stage_b1a1_smoke_compare_v2 as base
from scripts.v2_core_stage_b1a1_candidate_validator_v2 import load_json
from scripts.v2_core_stage_b1a_candidate_validator_v1 import sha256_file


def find_invalid_artifacts(
    *, manifest: dict[str, Any], run_directory: Path
) -> list[dict[str, Any]]:
    invalid: list[dict[str, Any]] = []
    for row in manifest["rows"]:
        review_id = row["review_id"]
        for round_number in range(1, manifest["required_rounds"] + 1):
            path = (
                run_directory
                / f"round_{round_number}"
                / "stage_b1a1"
                / f"{review_id}.invalid.raw"
            )
            if path.is_file():
                invalid.append(
                    {
                        "case_round": f"round_{round_number}:{review_id}",
                        "path": path.as_posix(),
                        "sha256": sha256_file(path),
                        "size_bytes": path.stat().st_size,
                    }
                )
    return invalid


def compare_smoke(
    *, manifest_path: Path, artifact_dir: Path, run_dir: Path | None = None
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    run_directory = run_dir or artifact_dir / manifest["run_directory"]
    report = base.compare_smoke(
        manifest_path=manifest_path,
        artifact_dir=artifact_dir,
        run_dir=run_directory,
    )
    invalid = find_invalid_artifacts(
        manifest=manifest,
        run_directory=run_directory,
    )
    report["invalid_artifacts"] = invalid
    if invalid:
        errors = set(report.get("errors", []))
        errors.update(
            f"ARTIFACT:{row['case_round']}:INVALID_RAW_PRESENT:{row['sha256']}"
            for row in invalid
        )
        report.update(
            {
                "status": "SMOKE_PROTOCOL_FAILED（Smoke協定失敗）",
                "error_count": len(errors),
                "errors": sorted(errors),
                "metrics_published": False,
                "metrics": None,
            }
        )
    return report


def render_markdown(report: dict[str, Any]) -> str:
    text = base.render_markdown(report).rstrip()
    invalid = report.get("invalid_artifacts", [])
    if invalid:
        lines = [text, "", "## 不合法原始輸出", ""]
        for row in invalid:
            lines.append(
                f"- `{row['case_round']}`：SHA-256 `{row['sha256']}`，"
                f"{row['size_bytes']} bytes；不得修補、重試或轉用。"
            )
        return "\n".join(lines) + "\n"
    return text + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = compare_smoke(
        manifest_path=args.manifest,
        artifact_dir=args.artifact_dir,
        run_dir=args.run_dir,
    )
    if args.json_output:
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.markdown_output:
        args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] != "SMOKE_PROTOCOL_FAILED（Smoke協定失敗）" else 1


if __name__ == "__main__":
    raise SystemExit(main())
