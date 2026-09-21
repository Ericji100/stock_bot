from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, TextIO


RULES_ROOT = Path(__file__).resolve().parent / "rules"
MANIFEST_PATH = RULES_ROOT / "manifest.json"


class VersioningError(RuntimeError):
    pass


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VersioningError("rule manifest is unavailable or invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("versions"), list):
        raise VersioningError("rule manifest has an invalid shape")
    return payload


def version_record(version_id: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    data = manifest or load_manifest()
    for item in data["versions"]:
        if isinstance(item, dict) and item.get("version_id") == version_id:
            return item
    raise VersioningError(f"unknown rule version: {version_id}")


def verify_version(version_id: str) -> dict[str, Any]:
    record = version_record(version_id)
    prompt_path = RULES_ROOT / "versions" / version_id / "prompt.md"
    try:
        content = prompt_path.read_bytes()
    except OSError as exc:
        raise VersioningError(f"prompt file is unavailable: {version_id}") from exc
    actual = hashlib.sha256(content).hexdigest()
    expected = str(record.get("prompt_sha256") or "")
    size_expected = int(record.get("prompt_utf8_bytes") or -1)
    return {
        "version_id": version_id,
        "ok": actual == expected and len(content) == size_expected,
        "prompt_sha256": actual,
        "expected_prompt_sha256": expected,
        "prompt_utf8_bytes": len(content),
        "expected_prompt_utf8_bytes": size_expected,
    }


def compare_versions(from_version: str, to_version: str) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    for version_id in (from_version, to_version):
        path = RULES_ROOT / "versions" / version_id / "prompt.md"
        verification = verify_version(version_id)
        text = path.read_text(encoding="utf-8")
        rows[version_id] = {
            "verified": verification["ok"],
            "prompt_sha256": verification["prompt_sha256"],
            "prompt_utf8_bytes": verification["prompt_utf8_bytes"],
            "line_count": len(text.splitlines()),
        }
    return {
        "from_version": from_version,
        "to_version": to_version,
        "different": rows[from_version]["prompt_sha256"] != rows[to_version]["prompt_sha256"],
        "versions": rows,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only trade-monitor rule version verification")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--version", required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--from", dest="from_version", required=True)
    compare.add_argument("--to", dest="to_version", required=True)
    return parser


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    output = stdout or sys.stdout
    try:
        args = build_parser().parse_args(argv)
        if args.command == "list":
            manifest = load_manifest()
            payload = {
                "ok": True,
                "versions": [item["version_id"] for item in manifest["versions"]],
            }
        elif args.command == "verify":
            payload = verify_version(args.version)
        else:
            payload = {"ok": True, **compare_versions(args.from_version, args.to_version)}
        code = 0 if payload.get("ok", True) else 1
    except VersioningError as exc:
        code = 2
        payload = {"ok": False, "error": str(exc)}
    output.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
