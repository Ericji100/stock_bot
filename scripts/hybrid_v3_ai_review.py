"""Run frozen Codex semantic reviews for hybrid V3 consistency/full ledgers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from itertools import islice
from pathlib import Path
from typing import Any

try:
    from .hybrid_v3_policy import SCHEMA_PATH, validate_semantic
except ImportError:  # direct script execution
    from hybrid_v3_policy import SCHEMA_PATH, validate_semantic


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan/hybrid_monitoring_v1"
PROMPT_PATH = ROOT / "config/hybrid_semantic_prompt_v1.md"
PROTOCOL_PATH = ROOT / "config/hybrid_monitoring_protocol_v1.json"
FREEZE_PATH = SOURCE / "protocol_freeze_manifest.json"
EXECUTION_FREEZE_PATH = SOURCE / "execution_freeze_manifest.json"
MODEL = "gpt-5.6-sol"
REASONING = "xhigh"


def _assert_freeze() -> None:
    freeze = json.loads(FREEZE_PATH.read_text(encoding="utf-8-sig"))
    if freeze.get("model") != MODEL or freeze.get("reasoning_effort") != REASONING:
        raise ValueError("model settings differ from frozen manifest")
    for item in freeze.get("files") or []:
        path = Path(item["absolute_path"])
        if _sha(path) != item["sha256"]:
            raise ValueError(f"frozen file changed: {path}")
    for key in ("event_manifest", "policy_boundary_manifest"):
        item = freeze[key]
        if _sha(Path(item["path"])) != item["sha256"]:
            raise ValueError(f"frozen manifest changed: {item['path']}")


def _assert_execution_freeze() -> None:
    freeze = json.loads(EXECUTION_FREEZE_PATH.read_text(encoding="utf-8-sig"))
    if freeze.get("model") != MODEL or freeze.get("reasoning_effort") != REASONING:
        raise ValueError("execution freeze model/reasoning differs from reviewer")
    for item in freeze.get("files") or []:
        path = ROOT / item["relative_path"]
        if not path.exists() or _sha(path) != item["sha256"]:
            raise ValueError(f"execution-frozen file changed: {item['relative_path']}")
    for item in freeze.get("artifacts") or []:
        path = Path(item["absolute_path"])
        if not path.exists() or _sha(path) != item["sha256"]:
            raise ValueError(f"execution-frozen artifact changed: {path}")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _chunks(values, size: int):
    iterator = iter(values)
    while True:
        chunk = list(islice(iterator, size))
        if not chunk:
            return
        yield chunk


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")


def _batch_schema() -> dict[str, Any]:
    item = json.loads(SCHEMA_PATH.read_text(encoding="utf-8-sig"))
    defs = item.pop("$defs")
    item.pop("$schema", None)
    item.pop("$id", None)
    item.pop("title", None)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["batch_version", "decisions"],
        "properties": {
            "batch_version": {"const": "hybrid-common-structure-batch-v1"},
            "decisions": {"type": "array", "minItems": 1, "items": item},
        },
        "$defs": defs,
    }
    return _structured_schema(schema)


def _structured_schema(value: Any) -> Any:
    """Add explicit primitive types required by the Responses structured-output API."""
    if isinstance(value, list):
        return [_structured_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    unsupported = {"uniqueItems", "format"}
    output = {key: _structured_schema(item) for key, item in value.items() if key not in unsupported}
    if "type" not in output and "const" in output:
        constant = output["const"]
        output["type"] = "boolean" if isinstance(constant, bool) else "number" if isinstance(constant, (int, float)) else "string"
    if "type" not in output and "enum" in output and output["enum"]:
        members = output["enum"]
        if all(isinstance(item, str) for item in members):
            output["type"] = "string"
    return output


def _run_codex(packets: list[dict[str, Any]], timeout: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    system = PROMPT_PATH.read_text(encoding="utf-8-sig")
    prompt = (
        system
        + "\n\n以下每個匿名封包都要各輸出一筆 decisions，順序與 review_id 不得改變或遺漏。\n"
        + json.dumps(packets, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )
    with tempfile.TemporaryDirectory(prefix="hybrid-v3-codex-") as temporary:
        temp = Path(temporary)
        schema_path = temp / "schema.json"
        output_path = temp / "output.json"
        schema_path.write_text(json.dumps(_batch_schema(), ensure_ascii=False), encoding="utf-8")
        command = [
            "codex", "exec", "-", "--model", MODEL,
            "-c", f'model_reasoning_effort="{REASONING}"',
            "--sandbox", "read-only", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check", "--output-schema", str(schema_path),
            "--output-last-message", str(output_path), "--json", "--color", "never", "--cd", str(temp),
        ]
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["NO_COLOR"] = "1"
        started = time.perf_counter()
        completed = subprocess.run(command, input=prompt, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout, check=False, env=env)
        if completed.returncode != 0:
            raise RuntimeError(f"codex failed {completed.returncode}: stderr={(completed.stderr or '')[-2000:]} stdout={(completed.stdout or '')[-4000:]}")
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        decisions = payload.get("decisions") or []
        expected = [row["review_id"] for row in packets]
        actual = [row.get("review_id") for row in decisions]
        if actual != expected:
            raise ValueError(f"Codex output ids differ; expected={expected}, actual={actual}")
        errors = []
        for packet, decision in zip(packets, decisions):
            errors.extend(f"{packet['review_id']}: {error}" for error in validate_semantic(packet, decision))
        if errors:
            raise ValueError("; ".join(errors[:20]))
        return decisions, {"elapsed_seconds": round(time.perf_counter() - started, 3), "stdout_tail": completed.stdout[-1000:]}


def review(*, source: Path, output: Path, audit: Path, batch_size: int, timeout: int, max_batches: int | None) -> dict[str, Any]:
    _assert_freeze()
    _assert_execution_freeze()
    expected_ids = [row["review_id"] for row in _iter_jsonl(source)]
    expected_rows = len(expected_ids)
    existing_rows = list(_iter_jsonl(output)) if output.exists() else []
    existing_ids = [row.get("review_id") for row in existing_rows]
    if len(existing_ids) != len(set(existing_ids)):
        raise ValueError(f"duplicate review_id in resumable output: {output}")
    if existing_ids != expected_ids[:len(existing_ids)]:
        raise ValueError(f"resumable output is not an exact source prefix: {output}")
    existing = set(existing_ids)
    pending = (row for row in _iter_jsonl(source) if row["review_id"] not in existing)
    batches = _chunks(pending, batch_size)
    audit_rows = _jsonl(audit) if audit.exists() else []
    completed_batches_this_call = 0
    for ordinal, batch in enumerate(batches, start=1):
        if max_batches is not None and ordinal > max_batches:
            break
        last_error = None
        for attempt in range(1, 4):
            try:
                decisions, diagnostics = _run_codex(batch, timeout)
                for decision in decisions:
                    _append_jsonl(output, decision)
                audit_row = {
                    "batch_ordinal": len(audit_rows) + 1, "attempt": attempt,
                    "review_ids": [row["review_id"] for row in batch],
                    "input_sha256": hashlib.sha256(json.dumps(batch, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                    "model": MODEL, "reasoning_effort": REASONING,
                    "prompt_sha256": _sha(PROMPT_PATH), "schema_sha256": _sha(SCHEMA_PATH),
                    "protocol_sha256": _sha(PROTOCOL_PATH), **diagnostics,
                }
                _append_jsonl(audit, audit_row)
                audit_rows.append(audit_row)
                last_error = None
                break
            except Exception as exc:  # retries are preserved in audit after terminal failure
                last_error = f"{type(exc).__name__}: {exc}"
                failure = {
                    "event": "ATTEMPT_ERROR", "batch_ordinal": len(audit_rows) + 1,
                    "attempt": attempt, "review_ids": [row["review_id"] for row in batch],
                    "model": MODEL, "reasoning_effort": REASONING,
                    "prompt_sha256": _sha(PROMPT_PATH), "schema_sha256": _sha(SCHEMA_PATH),
                    "protocol_sha256": _sha(PROTOCOL_PATH), "error": last_error,
                }
                _append_jsonl(audit, failure)
                audit_rows.append(failure)
        if last_error:
            _append_jsonl(audit, {"event": "TERMINAL_ERROR", "batch_ordinal": len(audit_rows) + 1, "review_ids": [row["review_id"] for row in batch], "terminal_error": last_error, "model": MODEL, "reasoning_effort": REASONING})
            raise RuntimeError(last_error)
        completed_batches_this_call += 1
        print(json.dumps({"completed_batch": ordinal, "output": str(output), "rows": len(batch)}, ensure_ascii=False), flush=True)
    final_ids = [row.get("review_id") for row in _iter_jsonl(output)] if output.exists() else []
    completed = len(final_ids)
    exact_source_prefix = final_ids == expected_ids[:completed]
    if not exact_source_prefix:
        raise ValueError(f"output ceased to be an exact source prefix: {output}")
    result = {
        "source": str(source.resolve()), "source_sha256": _sha(source), "expected_rows": expected_rows,
        "completed_rows": completed, "completed_batches_this_call": completed_batches_this_call,
        "complete": completed == expected_rows and exact_source_prefix,
        "exact_source_prefix": exact_source_prefix, "output": str(output.resolve()),
        "output_sha256": _sha(output) if output.exists() else None, "audit": str(audit.resolve()),
        "model": MODEL, "reasoning_effort": REASONING, "prompt_sha256": _sha(PROMPT_PATH),
        "schema_sha256": _sha(SCHEMA_PATH), "protocol_sha256": _sha(PROTOCOL_PATH),
    }
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("consistency", "full"), required=True)
    parser.add_argument("--run", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-batches", type=int)
    args = parser.parse_args()
    if args.mode == "consistency":
        source = SOURCE / "consistency_sample.jsonl"
        output = SOURCE / "consistency" / f"run_{args.run}.jsonl"
        audit = SOURCE / "consistency" / f"run_{args.run}_audit.jsonl"
    else:
        source = SOURCE / "anonymous_policy_boundary_packets.jsonl"
        output = SOURCE / "semantic" / "full_ai_semantic_ledger.jsonl"
        audit = SOURCE / "semantic" / "full_ai_semantic_audit.jsonl"
    print(json.dumps(review(source=source, output=output, audit=audit, batch_size=args.batch_size, timeout=args.timeout, max_batches=args.max_batches), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
