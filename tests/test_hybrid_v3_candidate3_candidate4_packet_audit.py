import json
from copy import deepcopy

from scripts.hybrid_v3_candidate3_candidate4_packet_audit import (
    audit,
    canonical_sha256,
    main,
)


def _review_id(number: int) -> str:
    return f"D-{number:024x}"


def _stock_id(number: int) -> str:
    return f"S-{number:016x}"


def _window(scale: str, *, end_date: str = "2023-01-05", bar_count: int = 4) -> dict:
    return {
        "scale": scale,
        "direction": "UP",
        "selection_policy": "LATEST_TWO_COMPLETED_SAME_DIRECTION_LEGACY_PIVOT_LEGS",
        "baseline": {
            "start_date": "2023-01-02",
            "end_date": "2023-01-03",
            "start_ref": f"PIVOT:{scale}:LOW:2023-01-02:2023-01-02",
            "end_ref": f"PIVOT:{scale}:HIGH:2023-01-03:2023-01-03",
            "start_price": 10.0,
            "end_price": 11.0,
            "bar_count": 2,
            "price_change_pct": 10.0,
            "slope_pct_per_bar": 10.0,
            "mean_true_range_pct": 2.0,
            "realized_close_volatility_pct": 1.0,
        },
        "current": {
            "start_date": "2023-01-04",
            "end_date": end_date,
            "start_ref": f"PIVOT:{scale}:LOW:2023-01-04:2023-01-04",
            "end_ref": f"PIVOT:{scale}:HIGH:{end_date}:{end_date}",
            "start_price": 11.0,
            "end_price": 12.0,
            "bar_count": bar_count,
            "price_change_pct": 9.0909,
            "slope_pct_per_bar": 3.0303,
            "mean_true_range_pct": 2.2,
            "realized_close_volatility_pct": 1.1,
        },
        "baseline_available_on": "2023-01-03",
        "current_available_on": end_date,
        "available_on": end_date,
    }


def _row(
    number: int,
    *,
    builder_version: str = "hybrid-v3-atomic-packets-v4",
    as_of: str = "2023-01-06",
) -> dict:
    review_id = _review_id(number)
    anonymous_stock_id = _stock_id(number)
    windows = {"LARGE": _window("LARGE"), "SMALL": _window("SMALL")}
    evidence = [
        {
            "ref": "BAR:2023-01-06",
            "kind": "BAR",
            "date": "2023-01-06",
            "values": {"close": 12.0},
        },
        {
            "ref": "COMPARISON_WINDOW:LARGE:fixture",
            "kind": "CAUSAL_COMPARISON_WINDOW",
            "date": "2023-01-05",
            "values": deepcopy(windows["LARGE"]),
        },
    ]
    manifest = {
        "anchor_candidates": [],
        "relation_candidates": [],
        "stop_candidates": [],
        "global_question_ids": ["LARGE_TREND_STRENGTH_INCREASED"],
    }
    packet = {
        "review_id": review_id,
        "anonymous_stock_id": anonymous_stock_id,
        "as_of": as_of,
        "question_manifest": manifest,
        "evidence": evidence,
        "objective_facts": {
            "builder_version": builder_version,
            "causal_cutoff_as_of": as_of,
            "working_comparison_windows": windows,
            "performance_used_for_ordering_or_truncation": False,
            "ai_visible_evidence_sha256": canonical_sha256(evidence),
            "stable_fact": "UNCHANGED",
        },
        "question_manifest_sha256": canonical_sha256(manifest),
        "evidence_catalog_sha256": canonical_sha256(evidence),
    }
    packet["input_packet_sha256"] = canonical_sha256(packet)
    return {
        "source_ordinal": number - 1,
        "review_id": review_id,
        "anonymous_stock_id": anonymous_stock_id,
        "eligible_sampling_strata": ["WAIT_POLICY_BOUNDARY"],
        "eligible_sampling_strata_sha256": canonical_sha256(["WAIT_POLICY_BOUNDARY"]),
        "primary_sampling_focus": "WAIT_POLICY_BOUNDARY",
        "packet_sha256": canonical_sha256(packet),
        "packet": packet,
    }


def _rehash(row: dict) -> dict:
    packet = row["packet"]
    packet["question_manifest_sha256"] = canonical_sha256(packet["question_manifest"])
    packet["evidence_catalog_sha256"] = canonical_sha256(packet["evidence"])
    packet["objective_facts"]["ai_visible_evidence_sha256"] = canonical_sha256(
        packet["evidence"]
    )
    packet.pop("input_packet_sha256", None)
    packet["input_packet_sha256"] = canonical_sha256(packet)
    row["packet_sha256"] = canonical_sha256(packet)
    return row


def _write_jsonl(path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_only_builder_version_and_hash_cascade_pass_and_cli_writes_reports(tmp_path) -> None:
    old_rows = [_row(1), _row(2)]
    new_rows = []
    for old in old_rows:
        new = deepcopy(old)
        new["packet"]["objective_facts"]["builder_version"] = "hybrid-v3-atomic-packets-v5"
        new_rows.append(_rehash(new))

    old_path = tmp_path / "candidate3.jsonl"
    new_path = tmp_path / "candidate4.jsonl"
    output_json = tmp_path / "audit.json"
    output_md = tmp_path / "audit.md"
    _write_jsonl(old_path, old_rows)
    _write_jsonl(new_path, new_rows)

    report = audit(old_path, new_path)
    assert report["status"] == "PASS", (
        report["fail_reasons"], report["substantive_case_differences"]
    )
    assert report["performance_or_future_fields_read"] is False
    assert report["summary"]["expected_only_cases"] == 2
    assert report["summary"]["substantive_difference_cases"] == 0
    assert report["summary"]["builder_version_change_cases"] == 2
    assert report["summary"]["input_packet_sha256_change_cases"] == 2

    assert (
        main(
            [
                "--old",
                str(old_path),
                "--new",
                str(new_path),
                "--output-json",
                str(output_json),
                "--output-md",
                str(output_md),
            ]
        )
        == 0
    )
    published = json.loads(output_json.read_text(encoding="utf-8"))
    assert published["status"] == "PASS"
    assert published["performance_or_future_fields_read"] is False
    markdown = output_md.read_text(encoding="utf-8")
    assert "Status: `PASS`" in markdown
    assert "Performance or future fields read: `false`" in markdown


def test_missing_v4_to_v5_builder_transition_fails(tmp_path) -> None:
    old = _row(1)
    new = deepcopy(old)
    old_path = tmp_path / "old.jsonl"
    new_path = tmp_path / "new.jsonl"
    _write_jsonl(old_path, [old])
    _write_jsonl(new_path, [new])
    report = audit(old_path, new_path)
    assert report["status"] == "FAIL"
    assert report["summary"]["invalid_builder_version_transition_cases"] == 1
    assert "BUILDER_VERSION_TRANSITION_INVALID" in report["fail_reasons"]


def test_reports_coverage_order_identity_question_evidence_and_comparison_differences(
    tmp_path,
) -> None:
    old_a, old_b, old_c = _row(1), _row(2), _row(3)
    new_a = deepcopy(old_a)
    new_a["packet"]["question_manifest"]["global_question_ids"].append(
        "LARGE_VOLATILITY_EXPANDED"
    )
    new_a["packet"]["objective_facts"]["working_comparison_windows"]["LARGE"][
        "current"
    ]["bar_count"] = 3
    new_a["packet"]["evidence"][0]["values"]["close"] = 12.5
    _rehash(new_a)

    new_b = deepcopy(old_b)
    replacement_stock = _stock_id(99)
    new_b["anonymous_stock_id"] = replacement_stock
    new_b["packet"]["anonymous_stock_id"] = replacement_stock
    _rehash(new_b)
    new_d = _row(4)

    old_path = tmp_path / "old.jsonl"
    new_path = tmp_path / "new.jsonl"
    _write_jsonl(old_path, [old_a, old_b, old_c])
    _write_jsonl(new_path, [new_b, new_a, new_d])

    report = audit(old_path, new_path)
    assert report["status"] == "FAIL"
    assert report["performance_or_future_fields_read"] is False
    assert report["summary"]["added_review_ids"] == 1
    assert report["summary"]["deleted_review_ids"] == 1
    assert report["order"]["changed"] is True
    assert report["summary"]["identity_or_as_of_change_cases"] == 1
    assert report["summary"]["question_manifest_change_cases"] == 1
    assert report["summary"]["evidence_change_cases"] == 1
    assert report["summary"]["comparison_leg_change_cases"] == 1
    assert "QUESTION_MANIFEST_CHANGED" in report["fail_reasons"]
    assert "EVIDENCE_CHANGED" in report["fail_reasons"]
    assert "COMPARISON_LEGS_CHANGED" in report["fail_reasons"]
    case_a = next(
        case for case in report["substantive_case_differences"] if case["review_id"] == _review_id(1)
    )
    assert case_a["comparison_legs"]["scales"][0]["scale"] == "LARGE"
    assert case_a["question_manifest"]["difference_paths"]
    assert case_a["evidence"]["changed_refs"]


def test_one_bar_comparison_contract_repair_is_scoped_and_passes(tmp_path) -> None:
    old = _row(1)
    old_window = old["packet"]["objective_facts"]["working_comparison_windows"]["SMALL"]
    old_window["current"]["bar_count"] = 1
    old_window["current"]["start_date"] = old_window["current"]["end_date"]
    old_window["current"]["start_ref"] = old_window["current"]["end_ref"]
    old["packet"]["evidence"].append(
        {
            "ref": f"COMPARISON_WINDOW:SMALL:{canonical_sha256(old_window)[:16]}",
            "kind": "CAUSAL_COMPARISON_WINDOW",
            "date": "2023-01-05",
            "values": deepcopy(old_window),
        }
    )
    _rehash(old)

    new = deepcopy(old)
    new["packet"]["objective_facts"]["builder_version"] = "hybrid-v3-atomic-packets-v5"
    new_window = _window("SMALL", end_date="2023-01-05", bar_count=4)
    new["packet"]["objective_facts"]["working_comparison_windows"]["SMALL"] = new_window
    new["packet"]["evidence"] = [
        row
        for row in new["packet"]["evidence"]
        if row.get("kind") != "CAUSAL_COMPARISON_WINDOW"
        or not str(row.get("ref")).startswith("COMPARISON_WINDOW:SMALL:")
    ]
    new["packet"]["evidence"].append(
        {
            "ref": f"COMPARISON_WINDOW:SMALL:{canonical_sha256(new_window)[:16]}",
            "kind": "CAUSAL_COMPARISON_WINDOW",
            "date": "2023-01-05",
            "values": deepcopy(new_window),
        }
    )
    new["eligible_sampling_strata"] = ["MACRO_COPY_OBJECTIVE_PROXY"]
    new["eligible_sampling_strata_sha256"] = canonical_sha256(
        new["eligible_sampling_strata"]
    )
    new["primary_sampling_focus"] = "MACRO_COPY_OBJECTIVE_PROXY"
    _rehash(new)

    old_path = tmp_path / "old.jsonl"
    new_path = tmp_path / "new.jsonl"
    _write_jsonl(old_path, [old])
    _write_jsonl(new_path, [new])
    report = audit(old_path, new_path)
    assert report["substantive_case_differences"][0]["one_bar_contract_repair"][
        "eligible"
    ], json.dumps(
        report["substantive_case_differences"][0]["one_bar_contract_repair"],
        ensure_ascii=False,
        indent=2,
    )
    assert report["status"] == "PASS", (
        report["fail_reasons"], report["substantive_case_differences"]
    )
    assert report["summary"]["scoped_one_bar_contract_repair_cases"] == 1
    assert report["summary"]["unexpected_substantive_difference_cases"] == 0


def test_one_bar_repair_allows_only_direct_endpoint_pivot_and_bar_closure(tmp_path) -> None:
    old = _row(1)
    old_window = old["packet"]["objective_facts"]["working_comparison_windows"]["SMALL"]
    old_window["current"]["bar_count"] = 1
    old_window["current"]["start_date"] = old_window["current"]["end_date"]
    old_window["current"]["start_ref"] = old_window["current"]["end_ref"]
    old["packet"]["evidence"].append(
        {
            "ref": f"COMPARISON_WINDOW:SMALL:{canonical_sha256(old_window)[:16]}",
            "kind": "CAUSAL_COMPARISON_WINDOW",
            "date": "2023-01-05",
            "values": deepcopy(old_window),
        }
    )
    old["packet"]["objective_facts"]["ai_visible_bar_count"] = 1
    _rehash(old)

    new = deepcopy(old)
    new["packet"]["objective_facts"]["builder_version"] = "hybrid-v3-atomic-packets-v5"
    new_window = _window("SMALL", end_date="2023-01-05", bar_count=4)
    new["packet"]["objective_facts"]["working_comparison_windows"]["SMALL"] = new_window
    new["packet"]["evidence"] = [
        row
        for row in new["packet"]["evidence"]
        if row.get("kind") != "CAUSAL_COMPARISON_WINDOW"
        or not str(row.get("ref")).startswith("COMPARISON_WINDOW:SMALL:")
    ]
    endpoint_ref = new_window["baseline"]["start_ref"]
    new["packet"]["evidence"].extend(
        [
            {
                "ref": "BAR:2023-01-02",
                "kind": "BAR",
                "date": "2023-01-02",
                "values": {"close": 10.0},
            },
            {
                "ref": endpoint_ref,
                "kind": "CONFIRMED_PIVOT",
                "date": "2023-01-02",
                "values": {
                    "source_date": "2023-01-02",
                    "confirmation_date": "2023-01-02",
                },
            },
            {
                "ref": f"COMPARISON_WINDOW:SMALL:{canonical_sha256(new_window)[:16]}",
                "kind": "CAUSAL_COMPARISON_WINDOW",
                "date": "2023-01-05",
                "values": deepcopy(new_window),
            },
        ]
    )
    new["packet"]["objective_facts"]["ai_visible_bar_count"] = 2
    _rehash(new)

    old_path = tmp_path / "old.jsonl"
    new_path = tmp_path / "new.jsonl"
    _write_jsonl(old_path, [old])
    _write_jsonl(new_path, [new])
    report = audit(old_path, new_path)
    repair = report["substantive_case_differences"][0]["one_bar_contract_repair"]
    assert report["status"] == "PASS", report["fail_reasons"]
    assert repair["comparison_dependency_evidence_only"] is True
    assert repair["ai_visible_bar_count_consistent"] is True

    unrelated = deepcopy(new)
    unrelated["packet"]["evidence"].append(
        {
            "ref": "PIVOT:SMALL:LOW:2022-12-01:2022-12-02",
            "kind": "CONFIRMED_PIVOT",
            "date": "2022-12-02",
            "values": {
                "source_date": "2022-12-01",
                "confirmation_date": "2022-12-02",
            },
        }
    )
    _rehash(unrelated)
    _write_jsonl(new_path, [unrelated])
    rejected = audit(old_path, new_path)
    rejected_repair = rejected["substantive_case_differences"][0][
        "one_bar_contract_repair"
    ]
    assert rejected["status"] == "FAIL"
    assert rejected_repair["comparison_dependency_evidence_only"] is False


def test_future_and_row_packet_identity_guards_fail_closed(tmp_path) -> None:
    old = _row(1)
    bad = deepcopy(old)
    bad["anonymous_stock_id"] = _stock_id(88)
    bad["packet"]["evidence"][0]["date"] = "2023-01-07"
    bad["packet"]["evidence"][0]["ref"] = "BAR:2023-01-07"
    _rehash(bad)

    old_path = tmp_path / "old.jsonl"
    new_path = tmp_path / "new.jsonl"
    _write_jsonl(old_path, [old])
    _write_jsonl(new_path, [bad])

    report = audit(old_path, new_path)
    assert report["status"] == "FAIL"
    assert report["guards"]["new"]["status"] == "FAIL"
    errors = report["guards"]["new"]["examples"][0]["errors"]
    assert any("row anonymous_stock_id differs from packet" in error for error in errors)
    assert any("future date" in error for error in errors)
    assert "IDENTITY_OR_FUTURE_GUARD_FAILURE" in report["fail_reasons"]
