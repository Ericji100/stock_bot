"""Validate every outcome-blind V3 packet before any formal AI run.

The preflight supplies a fail-closed UNKNOWN verdict for every requested atom.
That makes schema and cross-document packet validation executable without
making a course judgment or opening identity/future performance.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping

try:
    from .hybrid_v3_atomic_policy_v2 import validate_atomic as default_validate_atomic
    from .hybrid_v3_sharding_v2 import canonical_sha256
except ImportError:  # direct script import
    from scripts.hybrid_v3_atomic_policy_v2 import validate_atomic as default_validate_atomic
    from scripts.hybrid_v3_sharding_v2 import canonical_sha256


PREFLIGHT_VERSION = "hybrid-v3-packet-preflight-v1"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def question_count(packet: Mapping[str, Any]) -> int:
    manifest = packet["question_manifest"]
    return len(manifest["global_question_ids"]) + sum(
        len(entry["required_question_ids"])
        for group in ("anchor_candidates", "relation_candidates", "stop_candidates")
        for entry in manifest[group]
    )


def _visible_ref(packet: Mapping[str, Any], preferred: str | None = None) -> str:
    refs = {str(row.get("ref")) for row in packet.get("evidence") or [] if row.get("ref")}
    if preferred and preferred in refs:
        return preferred
    if not refs:
        raise ValueError(f"packet {packet.get('review_id')} has no visible evidence ref")
    return sorted(refs)[0]


def _unknown(ref: str) -> dict[str, Any]:
    return {
        "result": "UNKNOWN",
        "supporting_evidence_refs": [ref],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": ["OTHER_REQUIRED_EVIDENCE"],
        "reason_code": "REQUIRED_EVIDENCE_MISSING",
    }


def fail_closed_output(packet: Mapping[str, Any]) -> dict[str, Any]:
    manifest = packet["question_manifest"]
    fallback_ref = _visible_ref(packet)
    candidates: dict[str, list[dict[str, Any]]] = {}
    for group in ("anchor_candidates", "relation_candidates", "stop_candidates"):
        candidates[group] = []
        for entry in manifest[group]:
            subject_ref = str(entry["subject_ref"])
            evidence_ref = _visible_ref(packet, subject_ref)
            candidates[group].append(
                {
                    "subject_ref": subject_ref,
                    "answers": {
                        str(question_id): _unknown(evidence_ref)
                        for question_id in entry["required_question_ids"]
                    },
                }
            )
    return {
        "schema_version": "hybrid-atomic-semantics-v2",
        "protocol_version": "hybrid-monitoring-protocol-v2",
        "contract_status": "FINAL",
        **{
            key: packet[key]
            for key in (
                "review_id",
                "anonymous_stock_id",
                "as_of",
                "input_packet_sha256",
                "question_manifest_sha256",
                "evidence_catalog_sha256",
            )
        },
        "candidate_answers": candidates,
        "global_answers": {
            str(question_id): _unknown(fallback_ref)
            for question_id in manifest["global_question_ids"]
        },
        "causal_attestation": {
            "answered_complete_manifest": True,
            "decided_permission": False,
            "identity_visible": False,
            "invented_candidate_ref": False,
            "invented_evidence_ref": False,
            "issued_trade_instruction": False,
            "latest_visible_bar": packet["as_of"],
            "performance_visible": False,
            "selected_phase": False,
            "selected_route": False,
            "selected_scenario": False,
            "used_future_data": False,
        },
    }


def run_preflight(source: Path, *, validate_fn=default_validate_atomic) -> dict[str, Any]:
    errors: Counter[str] = Counter()
    invalid_rows: list[dict[str, Any]] = []
    total = zero_question = 0
    with Path(source).open(encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            packet = record.get("packet", record)
            total += 1
            zero_question += int(question_count(packet) == 0)
            try:
                validation_errors = list(validate_fn(packet, fail_closed_output(packet)))
            except Exception as exc:  # preflight must record, never skip, a malformed packet
                validation_errors = [f"preflight exception: {type(exc).__name__}: {exc}"]
            if validation_errors:
                errors.update(validation_errors)
                invalid_rows.append(
                    {
                        "source_ordinal": record.get("source_ordinal"),
                        "review_id": packet.get("review_id"),
                        "as_of": packet.get("as_of"),
                        "question_count": question_count(packet),
                        "errors": validation_errors,
                    }
                )
    report = {
        "preflight_version": PREFLIGHT_VERSION,
        "status": "PASS" if not invalid_rows else "FAIL",
        "classification": "OUTCOME_BLIND_PACKET_LEGALITY_ONLY",
        "source": str(Path(source).resolve()),
        "source_sha256": file_sha256(source),
        "identity_visible": False,
        "future_performance_visible": False,
        "records": total,
        "zero_question_records": zero_question,
        "valid_records": total - len(invalid_rows),
        "invalid_records": len(invalid_rows),
        "error_counts": dict(sorted(errors.items())),
        "invalid_rows": invalid_rows,
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# V3 outcome-blind packet preflight",
        "",
        f"- Status: `{report['status']}`",
        f"- Records: {report['records']}",
        f"- Valid: {report['valid_records']}",
        f"- Invalid: {report['invalid_records']}",
        f"- Zero-question records: {report['zero_question_records']}",
        f"- Source SHA-256: `{report['source_sha256']}`",
        "- Identity visible: no",
        "- Future/performance visible: no",
        "",
        "## Error counts",
        "",
    ]
    if report["error_counts"]:
        lines.extend(
            f"- `{error}`: {count}" for error, count in report["error_counts"].items()
        )
    else:
        lines.append("- None")
    lines.extend(["", "## Invalid rows", ""])
    if not report["invalid_rows"]:
        lines.append("- None")
    else:
        lines.extend(
            [
                "| source ordinal | review id | as of | questions | errors |",
                "| ---: | --- | --- | ---: | --- |",
            ]
        )
        for row in report["invalid_rows"]:
            text = "<br>".join(str(value) for value in row["errors"])
            lines.append(
                f"| {row['source_ordinal']} | `{row['review_id']}` | {row['as_of']} | "
                f"{row['question_count']} | {text} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args()
    validate_fn = default_validate_atomic
    if args.policy is not None:
        spec = importlib.util.spec_from_file_location("hybrid_v3_preflight_policy", args.policy)
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot load policy: {args.policy}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        validate_fn = getattr(module, "validate_atomic")
    report = run_preflight(args.source, validate_fn=validate_fn)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output_md.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "records": report["records"],
                "invalid_records": report["invalid_records"],
                "report_sha256": report["report_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
