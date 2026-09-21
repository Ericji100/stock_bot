"""FINAL Candidate2 multi-label, outcome-blind holdout allocation.

This module is deliberately separate from the FINAL V2 consistency selector.
An input case may be eligible for several objective challenge strata, but the
allocator assigns it to exactly one ``sampling_focus`` slot across the primary
block and both reserve blocks.  A focus is sampling metadata only: it never
changes the packet, removes alternative hypotheses, or predicts the AI/policy
decision.
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from copy import deepcopy
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "config" / "hybrid_multilabel_sampling_v3.json"
SELECTOR_VERSION = "hybrid-v3-consistency-v3-multilabel"
SELECTOR_STATUS = "FINAL"
ASSIGNMENT_ALGORITHM = "GLOBAL_CASE_DISJOINT_DETERMINISTIC_MAX_FLOW_SEEDED_ORDER"

STRATA = (
    "V2_CORE_OBJECTIVE_PROXY",
    "MACRO_COPY_OBJECTIVE_PROXY",
    "FRESH_Q1_OBJECTIVE_PROXY",
    "BEAR_REVERSAL_OBJECTIVE_PROXY",
    "MACRO_DEFENSE_REMOVE_PROXY",
    "WAIT_POLICY_BOUNDARY",
)
STRATA_ORDER = STRATA
FORMAL_QUOTAS = {
    "V2_CORE_OBJECTIVE_PROXY": 20,
    "MACRO_COPY_OBJECTIVE_PROXY": 15,
    "FRESH_Q1_OBJECTIVE_PROXY": 15,
    "BEAR_REVERSAL_OBJECTIVE_PROXY": 15,
    "MACRO_DEFENSE_REMOVE_PROXY": 20,
    "WAIT_POLICY_BOUNDARY": 35,
}
WAIT_BOUNDARY_REASONS = (
    "INITIAL_SELECTION_BOUNDARY",
    "RESELECTION_BOUNDARY",
    "UP_ATTACK_WITHOUT_CAUSAL_STOP",
    "UP_ATTACK_WITH_INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
    "MATERIAL_OBJECTIVE_HYPOTHESIS_CONFLICT",
)
SCENARIO_FOR_STRATUM = {
    "V2_CORE_OBJECTIVE_PROXY": "MATURE_TREND_PULLBACK",
    "MACRO_COPY_OBJECTIVE_PROXY": "MACRO_COPY_RESONANCE",
    "FRESH_Q1_OBJECTIVE_PROXY": "FRESH_Q1_EXPANSION",
    "BEAR_REVERSAL_OBJECTIVE_PROXY": "BEAR_REVERSAL_LEFT_RIGHT",
}
REQUIRED_ROW_FIELDS = frozenset(
    {
        "source_ordinal",
        "review_id",
        "anonymous_stock_id",
        "eligible_sampling_strata",
        "eligible_sampling_strata_sha256",
        "primary_sampling_focus",
        "packet_sha256",
        "packet",
    }
)
PACKET_SAMPLING_FIELDS = frozenset(
    {"eligible_sampling_strata", "primary_sampling_focus", "sampling_stratum", "sampling_focus"}
)
FORBIDDEN_OUTCOME_KEYS = frozenset(
    {
        "outcome",
        "mfe",
        "mae",
        "pnl",
        "profit",
        "forward_return",
        "future_return",
        "return_after",
        "exit_date",
        "exit_price",
    }
)


class SamplingContractError(ValueError):
    """The Candidate2 source or sampling contract is not valid."""


class SamplingCapacityError(SamplingContractError):
    """The source cannot fill every case-disjoint primary/reserve quota."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key).lower()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _validate_contract_value(contract: Mapping[str, Any]) -> None:
    if contract.get("contract_version") != "hybrid-v3-multilabel-sampling-v3":
        raise SamplingContractError("unexpected Candidate2 sampling contract version")
    if contract.get("status") not in {"DRAFT_NOT_FINAL", "FINAL"}:
        raise SamplingContractError("sampling contract status must be DRAFT_NOT_FINAL or FINAL")
    if tuple(contract.get("strata_order") or ()) != STRATA:
        raise SamplingContractError("sampling contract strata_order is not canonical")
    quotas = contract.get("quotas_per_block")
    if not isinstance(quotas, Mapping) or tuple(quotas) != STRATA:
        raise SamplingContractError("quotas_per_block must contain the canonical six strata in order")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in quotas.values()):
        raise SamplingContractError("every per-block quota must be a positive integer")
    if dict(quotas) != FORMAL_QUOTAS:
        raise SamplingContractError("Candidate2 must preserve the original six challenge quotas")
    if contract.get("reserve_block_count") != 2:
        raise SamplingContractError("Candidate2 requires exactly two reserve blocks")
    if set(contract.get("required_source_row_fields") or ()) != REQUIRED_ROW_FIELDS:
        raise SamplingContractError("required_source_row_fields differs from the Candidate2 row contract")
    if set(contract.get("packet_sampling_fields_forbidden") or ()) != PACKET_SAMPLING_FIELDS:
        raise SamplingContractError("packet_sampling_fields_forbidden differs from the Candidate2 contract")
    allowed_wait = contract.get("allowed_wait_boundary_reasons")
    if allowed_wait != list(WAIT_BOUNDARY_REASONS):
        raise SamplingContractError("allowed_wait_boundary_reasons differs from the Candidate2 enum")
    if contract.get("allocation_policy") != ASSIGNMENT_ALGORITHM:
        raise SamplingContractError("allocation_policy differs from the Candidate2 selector")


def load_sampling_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    contract = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate_contract_value(contract)
    return contract


def expected_eligible_sampling_strata(
    packet: Mapping[str, Any], contract: Mapping[str, Any]
) -> list[str]:
    """Recompute objective, multi-label eligibility without reading AI output."""

    facts = packet.get("objective_facts")
    if not isinstance(facts, Mapping):
        raise SamplingContractError("packet objective_facts must be an object")
    hypotheses = facts.get("scenario_hypotheses")
    sufficiency = facts.get("data_sufficiency_by_route")
    if not isinstance(hypotheses, Mapping) or not isinstance(sufficiency, Mapping):
        raise SamplingContractError("packet needs scenario_hypotheses and data_sufficiency_by_route")

    eligible: set[str] = set()
    for stratum, scenario in SCENARIO_FOR_STRATUM.items():
        rows = hypotheses.get(scenario)
        route = sufficiency.get(scenario)
        if not isinstance(rows, list) or not isinstance(route, Mapping):
            raise SamplingContractError(f"missing objective route shape for {scenario}")
        if rows and route.get("status") is True:
            eligible.add(stratum)

    if facts.get("parent_campaign_invalidated") is True or facts.get("macro_defense_alert") is True:
        eligible.add("MACRO_DEFENSE_REMOVE_PROXY")

    wait_reasons = facts.get("wait_boundary_reasons")
    if wait_reasons is None:
        wait_reasons = []
    if not isinstance(wait_reasons, list) or len(wait_reasons) != len(set(wait_reasons)):
        raise SamplingContractError("wait_boundary_reasons must be a unique list")
    allowed_wait = set(contract["allowed_wait_boundary_reasons"])
    if any(reason not in allowed_wait for reason in wait_reasons):
        raise SamplingContractError("wait_boundary_reasons contains an unknown objective reason")
    if wait_reasons:
        eligible.add("WAIT_POLICY_BOUNDARY")
    return [stratum for stratum in STRATA if stratum in eligible]


def validate_source_record(record: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
    if set(record) != REQUIRED_ROW_FIELDS:
        raise SamplingContractError("Candidate2 source row fields are not exact")
    if isinstance(record.get("source_ordinal"), bool) or not isinstance(record.get("source_ordinal"), int):
        raise SamplingContractError("source_ordinal must be an integer")
    if record["source_ordinal"] < 0:
        raise SamplingContractError("source_ordinal must be non-negative")
    if not isinstance(record.get("review_id"), str) or not record["review_id"]:
        raise SamplingContractError("review_id must be a non-empty string")
    if not isinstance(record.get("anonymous_stock_id"), str) or not record["anonymous_stock_id"]:
        raise SamplingContractError("anonymous_stock_id must be a non-empty string")
    if not _is_sha256(record.get("packet_sha256")):
        raise SamplingContractError("packet_sha256 must be a lowercase SHA-256")
    packet = record.get("packet")
    if not isinstance(packet, Mapping):
        raise SamplingContractError("packet must be an object")
    if record["packet_sha256"] != canonical_sha256(packet):
        raise SamplingContractError("packet_sha256 does not match the canonical packet")
    if packet.get("review_id") != record["review_id"]:
        raise SamplingContractError("packet review_id differs from its source envelope")
    if packet.get("anonymous_stock_id") != record["anonymous_stock_id"]:
        raise SamplingContractError("packet anonymous_stock_id differs from its source envelope")
    if PACKET_SAMPLING_FIELDS.intersection(_walk_keys(packet)):
        raise SamplingContractError("AI-visible packet must not contain sampling metadata")
    if FORBIDDEN_OUTCOME_KEYS.intersection(_walk_keys(packet)):
        raise SamplingContractError("AI-visible packet contains an outcome/future field")
    facts = packet.get("objective_facts") or {}
    if facts.get("performance_used_for_ordering_or_truncation") is not False:
        raise SamplingContractError("objective facts must attest that performance was not used")
    as_of = packet.get("as_of")
    try:
        cutoff = date.fromisoformat(str(as_of))
    except ValueError as exc:
        raise SamplingContractError("packet as_of must be an ISO date") from exc
    if facts.get("causal_cutoff_as_of") != as_of:
        raise SamplingContractError("objective causal cutoff must equal packet as_of")
    evidence = packet.get("evidence")
    if not isinstance(evidence, list):
        raise SamplingContractError("packet evidence must be a list")
    for item in evidence:
        if not isinstance(item, Mapping) or not isinstance(item.get("date"), str):
            raise SamplingContractError("every evidence item must have an ISO date")
        try:
            evidence_date = date.fromisoformat(item["date"])
        except ValueError as exc:
            raise SamplingContractError("every evidence item must have an ISO date") from exc
        if evidence_date > cutoff:
            raise SamplingContractError("packet evidence date exceeds as_of")

    eligible = record.get("eligible_sampling_strata")
    if not isinstance(eligible, list) or not eligible or len(eligible) != len(set(eligible)):
        raise SamplingContractError("eligible_sampling_strata must be a non-empty unique list")
    canonical = [stratum for stratum in STRATA if stratum in set(eligible)]
    if eligible != canonical or any(stratum not in STRATA for stratum in eligible):
        raise SamplingContractError("eligible_sampling_strata must use canonical enum order")
    expected = expected_eligible_sampling_strata(packet, contract)
    if eligible != expected:
        raise SamplingContractError("eligible_sampling_strata differs from objective packet facts")
    if record.get("eligible_sampling_strata_sha256") != canonical_sha256(eligible):
        raise SamplingContractError("eligible_sampling_strata_sha256 does not match eligible_sampling_strata")
    if record.get("primary_sampling_focus") not in eligible:
        raise SamplingContractError("primary_sampling_focus must belong to eligible_sampling_strata")


def validate_source_records(
    records: Sequence[Mapping[str, Any]], contract: Mapping[str, Any]
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for record in records:
        validate_source_record(record, contract)
        normalized.append(dict(record))
    ordinals = [row["source_ordinal"] for row in normalized]
    review_ids = [row["review_id"] for row in normalized]
    if len(ordinals) != len(set(ordinals)):
        raise SamplingContractError("Candidate2 source has duplicate source_ordinal")
    if len(review_ids) != len(set(review_ids)):
        raise SamplingContractError("Candidate2 source has duplicate review_id")
    if ordinals != sorted(ordinals):
        raise SamplingContractError("Candidate2 source rows must be in source_ordinal order")
    return normalized


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
        size = len(self.graph)
        while True:
            level = [-1] * size
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
            cursor = [0] * size

            def send(node: int, amount: int) -> int:
                if node == sink:
                    return amount
                while cursor[node] < len(self.graph[node]):
                    edge = self.graph[node][cursor[node]]
                    if edge.capacity and level[node] + 1 == level[edge.to]:
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


def _seed_rank(seed: str, record: Mapping[str, Any], suffix: str = "") -> str:
    return canonical_sha256(
        {
            "seed": seed,
            "review_id": record["review_id"],
            "packet_sha256": record["packet_sha256"],
            "suffix": suffix,
        }
    )


def _allocate(
    records: Sequence[dict[str, Any]], contract: Mapping[str, Any]
) -> dict[str, list[tuple[dict[str, Any], str]]]:
    seed = str(contract["seed"])
    quotas = {stratum: int(contract["quotas_per_block"][stratum]) for stratum in STRATA}
    block_ids = ["PRIMARY", "RESERVE_01", "RESERVE_02"]
    buckets = [(block, stratum) for block in block_ids for stratum in STRATA]
    ordered_records = sorted(
        records,
        key=lambda row: (_seed_rank(seed, row), row["source_ordinal"], row["review_id"]),
    )

    source = 0
    case_offset = 1
    bucket_offset = case_offset + len(ordered_records)
    sink = bucket_offset + len(buckets)
    network = _Dinic(sink + 1)
    bucket_node = {bucket: bucket_offset + index for index, bucket in enumerate(buckets)}
    case_edges: dict[str, list[tuple[tuple[str, str], _Edge]]] = {}

    for index, record in enumerate(ordered_records):
        node = case_offset + index
        network.add_edge(source, node, 1)
        candidates = [
            bucket for bucket in buckets if bucket[1] in record["eligible_sampling_strata"]
        ]
        candidates.sort(key=lambda bucket: _seed_rank(seed, record, f"{bucket[0]}::{bucket[1]}"))
        edges: list[tuple[tuple[str, str], _Edge]] = []
        for bucket in candidates:
            edges.append((bucket, network.add_edge(node, bucket_node[bucket], 1)))
        case_edges[record["review_id"]] = edges
    for bucket in buckets:
        network.add_edge(bucket_node[bucket], sink, quotas[bucket[1]])

    required = sum(quotas.values()) * len(block_ids)
    achieved = network.max_flow(source, sink)
    if achieved != required:
        raise SamplingCapacityError(
            f"global case-disjoint allocation reached {achieved} of {required} required slots"
        )

    allocated: dict[str, list[tuple[dict[str, Any], str]]] = {block: [] for block in block_ids}
    for record in ordered_records:
        used = [bucket for bucket, edge in case_edges[record["review_id"]] if edge.initial == 1 and edge.capacity == 0]
        if len(used) > 1:
            raise AssertionError("one Candidate2 case was allocated more than once")
        if used:
            block, focus = used[0]
            allocated[block].append((record, focus))
    return allocated


def _case_identity(record: Mapping[str, Any], focus: str) -> dict[str, Any]:
    return {
        "source_ordinal": record["source_ordinal"],
        "review_id": record["review_id"],
        "anonymous_stock_id": record["anonymous_stock_id"],
        "packet_sha256": record["packet_sha256"],
        "eligible_sampling_strata": list(record["eligible_sampling_strata"]),
        "eligible_sampling_strata_sha256": record["eligible_sampling_strata_sha256"],
        "primary_sampling_focus": record["primary_sampling_focus"],
        "sampling_focus": focus,
    }


def _block(block_id: str, rows: Sequence[tuple[dict[str, Any], str]]) -> dict[str, Any]:
    identities = [_case_identity(record, focus) for record, focus in rows]
    identities.sort(key=lambda row: (STRATA.index(row["sampling_focus"]), row["source_ordinal"]))
    return {
        "block_id": block_id,
        "case_count": len(identities),
        "sampling_focus_counts": dict(
            sorted(Counter(row["sampling_focus"] for row in identities).items())
        ),
        "rows": identities,
        "rows_sha256": canonical_sha256(identities),
    }


def build_multilabel_holdout_plan(
    records: Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, Any] | None = None,
    source_artifact_sha256: str | None = None,
    source_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Build deterministic, case-disjoint primary plus two reserve blocks."""

    contract_value = deepcopy(dict(contract)) if contract is not None else load_sampling_contract()
    _validate_contract_value(contract_value)
    normalized = validate_source_records(records, contract_value)
    block_count = 1 + int(contract_value["reserve_block_count"])
    capacity = {
        stratum: sum(stratum in row["eligible_sampling_strata"] for row in normalized)
        for stratum in STRATA
    }
    required_capacity = {
        stratum: int(contract_value["quotas_per_block"][stratum]) * block_count
        for stratum in STRATA
    }
    deficient = {
        stratum: {"eligible": capacity[stratum], "required": required_capacity[stratum]}
        for stratum in STRATA
        if capacity[stratum] < required_capacity[stratum]
    }
    if deficient:
        raise SamplingCapacityError(
            "per-stratum capacity is below quota * three blocks: " + canonical_json(deficient)
        )
    if source_artifact_sha256 is not None and not _is_sha256(source_artifact_sha256):
        raise SamplingContractError("source_artifact_sha256 must be a lowercase SHA-256")
    if source_manifest_sha256 is not None and not _is_sha256(source_manifest_sha256):
        raise SamplingContractError("source_manifest_sha256 must be a lowercase SHA-256")

    allocated = _allocate(normalized, contract_value)
    blocks = [_block(block_id, allocated[block_id]) for block_id in ("PRIMARY", "RESERVE_01", "RESERVE_02")]
    all_review_ids = [row["review_id"] for block in blocks for row in block["rows"]]
    if len(all_review_ids) != len(set(all_review_ids)):
        raise AssertionError("Candidate2 primary/reserve allocation is not case-disjoint")
    quota_expected = dict(contract_value["quotas_per_block"])
    for block in blocks:
        if block["sampling_focus_counts"] != quota_expected:
            raise AssertionError("Candidate2 max flow did not fill exact per-block quotas")
        for row in block["rows"]:
            if row["sampling_focus"] not in row["eligible_sampling_strata"]:
                raise AssertionError("sampling_focus was not eligible for its selected case")

    source_identity = [
        {
            "source_ordinal": row["source_ordinal"],
            "review_id": row["review_id"],
            "anonymous_stock_id": row["anonymous_stock_id"],
            "packet_sha256": row["packet_sha256"],
            "eligible_sampling_strata": row["eligible_sampling_strata"],
            "eligible_sampling_strata_sha256": row["eligible_sampling_strata_sha256"],
            "primary_sampling_focus": row["primary_sampling_focus"],
        }
        for row in normalized
    ]
    plan = {
        "plan_version": "hybrid-v3-multilabel-holdout-plan-v3",
        "status": (
            "LOCKED"
            if contract_value.get("status") == "FINAL" and SELECTOR_STATUS == "FINAL"
            else "DRAFT_NOT_FINAL"
        ),
        "selector_version": SELECTOR_VERSION,
        "selector_status": SELECTOR_STATUS,
        "contract_version": contract_value["contract_version"],
        "contract_sha256": canonical_sha256(contract_value),
        "seed": contract_value["seed"],
        "allocation_policy": contract_value["allocation_policy"],
        "sampling_focus_is_validation_only": True,
        "alternative_hypotheses_are_unchanged": True,
        "source_record_count": len(normalized),
        "source_identity_sha256": canonical_sha256(source_identity),
        "source_artifact_sha256": source_artifact_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "block_count": block_count,
        "cases_per_block": sum(contract_value["quotas_per_block"].values()),
        "eligible_capacity_by_stratum": capacity,
        "required_capacity_by_stratum": required_capacity,
        "primary": blocks[0],
        "reserve_blocks": blocks[1:],
    }
    plan["plan_sha256"] = canonical_sha256(plan)
    return plan


def validate_multilabel_holdout_plan(
    plan: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, Any] | None = None,
    source_artifact_sha256: str | None = None,
    source_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Recompute and byte-semantically verify a Candidate2 allocation plan."""

    expected = build_multilabel_holdout_plan(
        records,
        contract=contract,
        source_artifact_sha256=source_artifact_sha256,
        source_manifest_sha256=source_manifest_sha256,
    )
    if canonical_json(plan) != canonical_json(expected):
        raise SamplingContractError("Candidate2 holdout plan differs from deterministic recomputation")
    return {
        "valid": True,
        "plan_sha256": expected["plan_sha256"],
        "source_record_count": expected["source_record_count"],
        "allocated_cases": expected["cases_per_block"] * expected["block_count"],
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise SamplingContractError(f"invalid JSONL at line {line_number}") from exc
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    contract = load_sampling_contract(args.contract)
    records = _read_jsonl(args.source)
    manifest = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    if manifest.get("outcome_blind") is not True:
        raise SamplingContractError("Candidate2 source manifest must be outcome_blind")
    if manifest.get("identity_visible") is not False or manifest.get("performance_visible") is not False:
        raise SamplingContractError("Candidate2 source manifest exposes identity or performance")
    if manifest.get("future_data_visible") is not False:
        raise SamplingContractError("Candidate2 source manifest exposes future data")
    if manifest.get("review_points") != len(records):
        raise SamplingContractError("Candidate2 source manifest row count differs from source")
    source_hash = file_sha256(args.source)
    artifact_hash = manifest.get("artifact_sha256")
    if artifact_hash != source_hash:
        raise SamplingContractError("Candidate2 source artifact hash differs from manifest")
    plan = build_multilabel_holdout_plan(
        records,
        contract=contract,
        source_artifact_sha256=source_hash,
        source_manifest_sha256=file_sha256(args.source_manifest),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if args.output.exists() and args.output.read_bytes() != payload:
        raise SamplingContractError("refusing to overwrite a different Candidate2 holdout plan")
    args.output.write_bytes(payload)
    print(canonical_json({
        "status": plan["status"],
        "cases_per_block": plan["cases_per_block"],
        "block_count": plan["block_count"],
        "plan_sha256": plan["plan_sha256"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
