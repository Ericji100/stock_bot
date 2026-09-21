"""Freeze the outcome-blind MATURE_TREND_PULLBACK stage-1 research track.

This is a study-scope split only.  It does not change the existing V1/V2/V3
rules, semantic prompt, atomic schema, deterministic V3 reducer, or execution
adapter.  Sampling metadata stays outside every AI-visible packet.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .hybrid_v3_consistency_v3 import (
        canonical_sha256,
        load_sampling_contract,
        validate_source_record,
    )
    from .hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        file_sha256,
        write_shards,
    )
except ImportError:  # pragma: no cover
    from scripts.hybrid_v3_consistency_v3 import (
        canonical_sha256,
        load_sampling_contract,
        validate_source_record,
    )
    from scripts.hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        file_sha256,
        write_shards,
    )


ROOT = Path(__file__).resolve().parents[1]
TRACK_VERSION = "hybrid-v3-mature-s1-research-track-v1"
FREEZE_VERSION = "hybrid-v3-mature-s1-execution-freeze-v1"
SELECTION_VERSION = "hybrid-v3-mature-s1-selection-v1"
PACKET_MANIFEST_VERSION = "hybrid-v3-mature-s1-packet-block-v1"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_SINGLE_ROUTE_RESEARCH_ONLY"
MODEL = "gpt-5.6-sol"
REASONING = "xhigh"

STAGE_PROTOCOL = ROOT / "config/hybrid_v3_mature_s1_stage_v2.json"
SOURCE_SAMPLING_CONTRACT = ROOT / "config/hybrid_multilabel_sampling_v3.json"
PROMPT = ROOT / "config/hybrid_semantic_prompt_v3.md"
SCHEMA = ROOT / "config/hybrid_atomic_semantics_v2.schema.json"
RESEARCH_PROTOCOL = ROOT / "config/hybrid_monitoring_research_protocol_v1.json"
LAUNCHER = ROOT / "scripts/hybrid_v3_codex_launcher_v2.py"
REVIEWER = ROOT / "scripts/hybrid_v3_codex_reviewer_v3.py"
RUNNER = ROOT / "scripts/hybrid_v3_atomic_runner_v2.py"
POLICY = ROOT / "scripts/hybrid_v3_atomic_policy_v3.py"
SHARDING = ROOT / "scripts/hybrid_v3_sharding_v2.py"
CONSISTENCY_EVALUATOR = ROOT / "scripts/hybrid_v3_mature_s1_consistency_v1.py"

SOURCE_STRATA = {
    "MATURE": "V2_CORE_OBJECTIVE_PROXY",
    "MACRO": "MACRO_COPY_OBJECTIVE_PROXY",
    "FRESH": "FRESH_Q1_OBJECTIVE_PROXY",
    "BEAR": "BEAR_REVERSAL_OBJECTIVE_PROXY",
    "REMOVE": "MACRO_DEFENSE_REMOVE_PROXY",
    "WAIT": "WAIT_POLICY_BOUNDARY",
}


class MatureS1TrackError(ValueError):
    """The stage source, sample, or frozen execution chain is not exact."""


class _Edge:
    __slots__ = ("to", "reverse", "capacity", "initial")

    def __init__(self, to: int, reverse: int, capacity: int) -> None:
        self.to = to
        self.reverse = reverse
        self.capacity = capacity
        self.initial = capacity


class _Dinic:
    def __init__(self, size: int) -> None:
        self.graph: list[list[_Edge]] = [[] for _ in range(size)]

    def add_edge(self, source: int, target: int, capacity: int) -> _Edge:
        forward = _Edge(target, len(self.graph[target]), capacity)
        reverse = _Edge(source, len(self.graph[source]), 0)
        self.graph[source].append(forward)
        self.graph[target].append(reverse)
        return forward

    def max_flow(self, source: int, sink: int) -> int:
        total = 0
        while True:
            level = [-1] * len(self.graph)
            level[source] = 0
            queue = deque([source])
            while queue:
                node = queue.popleft()
                for edge in self.graph[node]:
                    if edge.capacity and level[edge.to] < 0:
                        level[edge.to] = level[node] + 1
                        queue.append(edge.to)
            if level[sink] < 0:
                return total
            cursor = [0] * len(self.graph)

            def send(node: int, amount: int) -> int:
                if node == sink:
                    return amount
                while cursor[node] < len(self.graph[node]):
                    edge = self.graph[node][cursor[node]]
                    if edge.capacity and level[edge.to] == level[node] + 1:
                        pushed = send(edge.to, min(amount, edge.capacity))
                        if pushed:
                            edge.capacity -= pushed
                            self.graph[edge.to][edge.reverse].capacity += pushed
                            return pushed
                    cursor[node] += 1
                return 0

            while True:
                pushed = send(source, 10**9)
                if not pushed:
                    break
                total += pushed


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise MatureS1TrackError(f"expected JSON object: {path}")
    return value


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise MatureS1TrackError(f"expected JSON object at {path}:{line_number}")
            yield value


def _canonical_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _stage_protocol() -> dict[str, Any]:
    value = _read_json(STAGE_PROTOCOL)
    if value.get("stage_protocol_version") != "hybrid-v3-mature-s1-stage-v2":
        raise MatureS1TrackError("unexpected stage protocol version")
    if value.get("status") != "FINAL_RESEARCH_LOCKED":
        raise MatureS1TrackError("stage protocol is not locked")
    target = value.get("target") or {}
    if target.get("primary_scenario") != "MATURE_TREND_PULLBACK" or target.get(
        "trade_route"
    ) != "V2_CORE":
        raise MatureS1TrackError("stage target changed")
    sample = value.get("consistency_sample") or {}
    focus_order = list(sample.get("focus_order") or [])
    quotas = sample.get("quotas") or {}
    if list(quotas) != focus_order or sum(int(v) for v in quotas.values()) != int(
        sample.get("cases", -1)
    ):
        raise MatureS1TrackError("stage quotas do not exactly cover the case count")
    return value


def stage_focuses(record: Mapping[str, Any]) -> list[str]:
    """Derive sampling-only challenge roles from as-of objective facts."""

    eligible = set(record["eligible_sampling_strata"])
    facts = record["packet"]["objective_facts"]
    waits = set(facts.get("wait_boundary_reasons") or [])
    result: list[str] = []
    if SOURCE_STRATA["MATURE"] in eligible:
        result.append("MATURE_OBJECTIVE_PROXY")
    if SOURCE_STRATA["MACRO"] in eligible and SOURCE_STRATA["MATURE"] not in eligible:
        result.append("COMPETING_MACRO_ONLY")
    if SOURCE_STRATA["FRESH"] in eligible and SOURCE_STRATA["MATURE"] not in eligible:
        result.append("COMPETING_FRESH_ONLY")
    if SOURCE_STRATA["BEAR"] in eligible and SOURCE_STRATA["MATURE"] not in eligible:
        result.append("COMPETING_BEAR_ONLY")
    if SOURCE_STRATA["REMOVE"] in eligible:
        result.append("MACRO_DEFENSE_REMOVE")
    if "UP_ATTACK_WITHOUT_CAUSAL_STOP" in waits:
        result.append("WAIT_NO_CAUSAL_STOP")
    if "MATERIAL_OBJECTIVE_HYPOTHESIS_CONFLICT" in waits:
        result.append("WAIT_MATERIAL_CONFLICT")
    return result


def scan_source(
    source_path: Path, source_manifest_path: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = _read_json(source_manifest_path)
    if manifest.get("status") != "LOCKED_OUTCOME_BLIND":
        raise MatureS1TrackError("source is not locked outcome blind")
    required_flags = {
        "outcome_blind": True,
        "identity_visible": False,
        "performance_visible": False,
        "future_data_visible": False,
        "source_order_locked": True,
    }
    for key, expected in required_flags.items():
        if manifest.get(key) is not expected:
            raise MatureS1TrackError(f"source manifest changed {key}")
    if file_sha256(source_path) != manifest.get("artifact_sha256"):
        raise MatureS1TrackError("source artifact hash differs from manifest")
    source_contract = load_sampling_contract(SOURCE_SAMPLING_CONTRACT)
    records: list[dict[str, Any]] = []
    seen_review: set[str] = set()
    seen_ordinals: set[int] = set()
    for row in _jsonl(source_path):
        validate_source_record(row, source_contract)
        ordinal = int(row["source_ordinal"])
        review_id = str(row["review_id"])
        if ordinal in seen_ordinals or review_id in seen_review:
            raise MatureS1TrackError("source contains duplicate identity")
        seen_ordinals.add(ordinal)
        seen_review.add(review_id)
        records.append(
            {
                "source_ordinal": ordinal,
                "review_id": review_id,
                "anonymous_stock_id": row["anonymous_stock_id"],
                "packet_sha256": row["packet_sha256"],
                "as_of": row["packet"]["as_of"],
                "eligible_sampling_strata": list(row["eligible_sampling_strata"]),
                "eligible_stage_focuses": stage_focuses(row),
            }
        )
    expected_rows = int(manifest.get("review_points", -1))
    if len(records) != expected_rows or [r["source_ordinal"] for r in records] != list(
        range(expected_rows)
    ):
        raise MatureS1TrackError("source coverage is not exact")
    return records, manifest


def _rank(seed: str, record: Mapping[str, Any], suffix: str = "") -> str:
    return canonical_sha256(
        {
            "seed": seed,
            "source_ordinal": record["source_ordinal"],
            "review_id": record["review_id"],
            "packet_sha256": record["packet_sha256"],
            "suffix": suffix,
        }
    )


def allocate(records: Sequence[Mapping[str, Any]], protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    sample = protocol["consistency_sample"]
    seed = str(sample["seed"])
    focuses = list(sample["focus_order"])
    quotas = {key: int(sample["quotas"][key]) for key in focuses}
    candidates = [dict(row) for row in records if row["eligible_stage_focuses"]]
    stocks = sorted({str(row["anonymous_stock_id"]) for row in candidates})
    ordered = sorted(candidates, key=lambda row: (_rank(seed, row), row["source_ordinal"]))

    source_node = 0
    stock_offset = 1
    case_offset = stock_offset + len(stocks)
    focus_offset = case_offset + len(ordered)
    sink = focus_offset + len(focuses)
    network = _Dinic(sink + 1)
    stock_node = {stock: stock_offset + i for i, stock in enumerate(stocks)}
    focus_node = {focus: focus_offset + i for i, focus in enumerate(focuses)}
    for stock in stocks:
        network.add_edge(source_node, stock_node[stock], 1)
    edge_map: dict[str, list[tuple[str, _Edge]]] = {}
    for index, record in enumerate(ordered):
        node = case_offset + index
        network.add_edge(stock_node[str(record["anonymous_stock_id"])], node, 1)
        eligible = [focus for focus in focuses if focus in record["eligible_stage_focuses"]]
        eligible.sort(key=lambda focus: _rank(seed, record, focus))
        edge_map[str(record["review_id"])] = [
            (focus, network.add_edge(node, focus_node[focus], 1)) for focus in eligible
        ]
    for focus in focuses:
        network.add_edge(focus_node[focus], sink, quotas[focus])
    required = sum(quotas.values())
    achieved = network.max_flow(source_node, sink)
    if achieved != required:
        capacity = Counter(
            focus for row in candidates for focus in row["eligible_stage_focuses"]
        )
        raise MatureS1TrackError(
            f"stage allocation reached {achieved}/{required}; capacity={dict(capacity)}"
        )

    selected: list[dict[str, Any]] = []
    by_review = {str(row["review_id"]): row for row in ordered}
    for review_id, edges in edge_map.items():
        used = [focus for focus, edge in edges if edge.initial == 1 and edge.capacity == 0]
        if len(used) > 1:
            raise AssertionError("one case filled multiple stage quotas")
        if used:
            row = deepcopy(by_review[review_id])
            row["sampling_focus"] = used[0]
            selected.append(row)
    selected.sort(key=lambda row: (focuses.index(row["sampling_focus"]), row["source_ordinal"]))
    if len({row["anonymous_stock_id"] for row in selected}) != len(selected):
        raise MatureS1TrackError("stage sample repeats an anonymous stock")
    focus_counts = Counter(row["sampling_focus"] for row in selected)
    if dict(focus_counts) != quotas:
        raise MatureS1TrackError("stage sample did not fill exact quotas")
    months = Counter(str(row["as_of"])[:7] for row in selected)
    if len(months) < int(sample["minimum_distinct_months"]):
        raise MatureS1TrackError("stage sample has insufficient month coverage")
    if max(months.values(), default=0) > int(sample["maximum_cases_per_month"]):
        raise MatureS1TrackError("stage sample exceeds the frozen monthly concentration cap")
    return selected


def extract_packets(source_path: Path, selected: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    wanted = {str(row["review_id"]): row for row in selected}
    found: dict[str, dict[str, Any]] = {}
    for source in _jsonl(source_path):
        review_id = str(source.get("review_id") or "")
        if review_id not in wanted:
            continue
        expected = wanted[review_id]
        if source.get("packet_sha256") != expected.get("packet_sha256"):
            raise MatureS1TrackError("selected packet hash changed")
        packet = source.get("packet")
        if not isinstance(packet, dict) or canonical_sha256(packet) != expected["packet_sha256"]:
            raise MatureS1TrackError("selected packet content changed")
        found[review_id] = packet
    if set(found) != set(wanted):
        raise MatureS1TrackError("selected packets are missing from source")
    return [found[str(row["review_id"])] for row in selected]


def _component(name: str, path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MatureS1TrackError(f"missing component: {path}")
    return {
        "name": name,
        "relative_path": path.resolve().relative_to(ROOT.resolve()).as_posix(),
        "status": "FINAL",
        "sha256": file_sha256(path),
    }


def _verify_base_components(base_freeze: Mapping[str, Any]) -> list[dict[str, Any]]:
    paths = {
        "launcher": LAUNCHER,
        "reviewer": REVIEWER,
        "runner": RUNNER,
        "policy": POLICY,
        "prompt": PROMPT,
        "schema": SCHEMA,
        "protocol": RESEARCH_PROTOCOL,
    }
    frozen = {str(row.get("name")): row for row in base_freeze.get("v2_components") or []}
    result: list[dict[str, Any]] = []
    for name, path in paths.items():
        current = _component(name, path)
        prior = frozen.get(name)
        if not prior or prior.get("sha256") != current["sha256"]:
            raise MatureS1TrackError(f"base frozen component changed: {name}")
        result.append(current)
    return result


def prepare(
    *,
    source_path: Path,
    source_manifest_path: Path,
    base_track_manifest_path: Path,
    base_execution_freeze_path: Path,
    paused_run_root: Path,
    output_dir: Path,
    shard_count: int = 4,
) -> dict[str, Any]:
    protocol = _stage_protocol()
    records, source_manifest = scan_source(source_path, source_manifest_path)
    selected = allocate(records, protocol)
    packets = extract_packets(source_path, selected)
    output_dir = Path(output_dir).resolve()

    plan_core = {
        "selection_version": SELECTION_VERSION,
        "status": "LOCKED_OUTCOME_BLIND",
        "stage_protocol_sha256": file_sha256(STAGE_PROTOCOL),
        "source_artifact_sha256": file_sha256(source_path),
        "source_manifest_sha256": file_sha256(source_manifest_path),
        "source_rows": source_manifest["review_points"],
        "cases": len(selected),
        "one_case_per_anonymous_stock": True,
        "sampling_focus_is_validation_only": True,
        "identity_visible_inside_ai_packet": False,
        "future_or_performance_visible": False,
        "focus_counts": dict(Counter(row["sampling_focus"] for row in selected)),
        "month_counts": dict(sorted(Counter(str(row["as_of"])[:7] for row in selected).items())),
        "rows": selected,
        "rows_sha256": canonical_sha256(selected),
    }
    selection_plan = {**plan_core, "selection_sha256": canonical_sha256(plan_core)}
    selection_path = output_dir / "selection_plan.json"
    _publish_immutable(selection_path, canonical_json_bytes(selection_plan) + b"\n")

    packet_payload = _canonical_jsonl(packets)
    packet_path = output_dir / "primary_packets.jsonl"
    _publish_immutable(packet_path, packet_payload)
    mapping = [
        {
            "selected_ordinal": index,
            "source_ordinal": int(row["source_ordinal"]),
            "review_id": row["review_id"],
            "anonymous_stock_id": row["anonymous_stock_id"],
            "packet_sha256": row["packet_sha256"],
            "sampling_focus": row["sampling_focus"],
            "eligible_stage_focuses": row["eligible_stage_focuses"],
        }
        for index, row in enumerate(selected)
    ]
    packet_manifest_core = {
        "manifest_version": PACKET_MANIFEST_VERSION,
        "status": "LOCKED_OUTCOME_BLIND",
        "block_id": "PRIMARY_MATURE_S1",
        "rows": len(packets),
        "packets_canonical_jsonl_sha256": hashlib.sha256(packet_payload).hexdigest(),
        "sampling_metadata_inside_packet": False,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
        "mapping": mapping,
        "mapping_sha256": canonical_sha256(mapping),
    }
    packet_manifest = {
        **packet_manifest_core,
        "manifest_sha256": canonical_sha256(packet_manifest_core),
    }
    packet_manifest_path = output_dir / "primary_packets.manifest.json"
    _publish_immutable(packet_manifest_path, canonical_json_bytes(packet_manifest) + b"\n")

    base_track = _read_json(base_track_manifest_path)
    base_freeze = _read_json(base_execution_freeze_path)
    if base_track.get("track_version") != "hybrid-v3-research-track-v4-exact-evidence":
        raise MatureS1TrackError("base track is not exact-evidence V4")
    if base_freeze.get("strategy_or_gate_change") is not False:
        raise MatureS1TrackError("base freeze unexpectedly changed strategy or gates")
    runtime_components = _verify_base_components(base_freeze)
    paused_cases = len(list(Path(paused_run_root).glob("r*/s*/cases/*.json")))
    paused_attempts = len(list(Path(paused_run_root).glob("r*/s*/attempts/*/attempt_*.json")))
    freeze = {
        "freeze_version": FREEZE_VERSION,
        "status": "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION",
        "track_version": TRACK_VERSION,
        "track_status": TRACK_STATUS,
        "classification": "RESEARCH_BACKTEST_NOT_COURSE_VALIDATED",
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
        "old_ai_output_visible": False,
        "strategy_or_gate_change": False,
        "v1_v2_v3_rules_unchanged": True,
        "single_route_scope": {
            "primary_scenario": "MATURE_TREND_PULLBACK",
            "trade_route": "V2_CORE",
            "other_scenarios_are_negative_controls": True,
            "other_routes_suspended": True,
        },
        "stage_protocol_sha256": file_sha256(STAGE_PROTOCOL),
        "source_artifact_sha256": file_sha256(source_path),
        "source_manifest_sha256": file_sha256(source_manifest_path),
        "selection_plan_sha256": file_sha256(selection_path),
        "primary_packet_sha256": file_sha256(packet_path),
        "primary_packet_manifest_sha256": file_sha256(packet_manifest_path),
        "supersedes_no_strategy": True,
        "base_exact_evidence_track_sha256": file_sha256(base_track_manifest_path),
        "base_exact_evidence_freeze_sha256": file_sha256(base_execution_freeze_path),
        "paused_broad_track": {
            "status": "PAUSED_EXCLUDED_FROM_STAGE_CONCLUSION",
            "run_root": str(Path(paused_run_root).resolve()),
            "validated_case_files": paused_cases,
            "attempt_files": paused_attempts,
        },
        "v2_components": runtime_components,
        "stage_components": [
            _component("stage_protocol", STAGE_PROTOCOL),
            _component("source_sampling_contract", SOURCE_SAMPLING_CONTRACT),
            _component("track_builder", Path(__file__).resolve()),
            _component("sharding", SHARDING),
            _component("consistency_evaluator", CONSISTENCY_EVALUATOR),
        ],
        "execution_contract": {"model": MODEL, "reasoning_effort": REASONING},
    }
    freeze_path = output_dir / "research_execution_freeze.json"
    _publish_immutable(freeze_path, canonical_json_bytes(freeze) + b"\n")

    case_records = build_case_records(
        packets,
        source_manifest_sha256=file_sha256(packet_path),
        protocol_sha256=file_sha256(RESEARCH_PROTOCOL),
        prompt_sha256=file_sha256(PROMPT),
        schema_sha256=file_sha256(SCHEMA),
        execution_contract_sha256=file_sha256(freeze_path),
        model=MODEL,
        reasoning_effort=REASONING,
    )
    assignment = write_shards(case_records, output_dir / "primary_cases", shard_count=shard_count)
    assignment_path = output_dir / "primary_cases/assignment.manifest.json"
    track_core = {
        "track_version": TRACK_VERSION,
        "status": TRACK_STATUS,
        "classification": "RESEARCH_BACKTEST_NOT_COURSE_VALIDATED",
        "stage": "MATURE_TREND_PULLBACK_V2_CORE_ONLY",
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "cases": len(packets),
        "runs": int(protocol["consistency_sample"]["runs"]),
        "selection_plan_path": str(selection_path),
        "selection_plan_sha256": file_sha256(selection_path),
        "packet_path": str(packet_path),
        "packet_sha256": file_sha256(packet_path),
        "packet_manifest_path": str(packet_manifest_path),
        "packet_manifest_sha256": file_sha256(packet_manifest_path),
        "execution_freeze_path": str(freeze_path),
        "execution_freeze_sha256": file_sha256(freeze_path),
        "assignment_manifest_path": str(assignment_path),
        "assignment_manifest_sha256": file_sha256(assignment_path),
        "assignment_sha256": assignment["assignment_sha256"],
        "strategy_or_gate_change": False,
        "performance_must_remain_sealed_until_prior_gates_pass": True,
    }
    track = {**track_core, "track_manifest_sha256": canonical_sha256(track_core)}
    track_path = output_dir / "research_track_manifest.json"
    _publish_immutable(track_path, canonical_json_bytes(track) + b"\n")
    return track


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--base-track-manifest", type=Path, required=True)
    parser.add_argument("--base-execution-freeze", type=Path, required=True)
    parser.add_argument("--paused-run-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--shard-count", type=int, default=4)
    args = parser.parse_args()
    result = prepare(
        source_path=args.source.resolve(),
        source_manifest_path=args.source_manifest.resolve(),
        base_track_manifest_path=args.base_track_manifest.resolve(),
        base_execution_freeze_path=args.base_execution_freeze.resolve(),
        paused_run_root=args.paused_run_root.resolve(),
        output_dir=args.output_dir.resolve(),
        shard_count=args.shard_count,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
