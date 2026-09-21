import json
from datetime import date
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config/enlightenment_ai_judgement_v1.schema.json"
EXAMPLE_PATH = ROOT / "tests/fixtures/enlightenment_ai_judgement_v1.example.json"
CASES_PATH = ROOT / "config/enlightenment_ai_calibration_cases_v1.json"


STATUS_LABELS = {
    "WATCHING": "監控中",
    "STRUCTURE_MAPPED": "結構已辨識",
    "ARMED": "已建立進場計畫",
    "TRIGGERED": "已觸發進場",
    "REJECTED": "不合格",
    "INVALIDATED": "結構失效",
    "DATA_INSUFFICIENT": "資料不足",
    "OPEN": "持有中",
    "ADD_ARMED": "已建立加碼計畫",
    "EXIT_WARNING": "出場警戒",
    "STOPPED": "停損出場",
    "REENTRY_WATCHING": "再進場監控中",
    "CAMPAIGN_INVALIDATED": "大結構交易週期失效",
    "CLOSED": "交易已結束",
}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_schema_is_valid_and_accepts_example():
    schema = load(SCHEMA_PATH)
    example = load(EXAMPLE_PATH)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    validator.check_schema(schema)
    errors = sorted(validator.iter_errors(example), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)


def test_example_obeys_causal_and_status_semantics():
    example = load(EXAMPLE_PATH)
    assert example["data_quality"]["last_bar_date"] == example["as_of"]
    assert example["causal_attestation"]["latest_visible_bar"] == example["as_of"]
    assert example["causal_attestation"]["used_future_data"] is False
    assert example["causal_attestation"]["outcome_visible_to_ai"] is False
    assert example["decision"]["status_label_zh"] == STATUS_LABELS[example["decision"]["status"]]
    for item in example["evidence"]:
        assert date.fromisoformat(item["date"]) <= date.fromisoformat(example["as_of"])
    for anchor in example["anchors"]:
        assert date.fromisoformat(anchor["start_date"]) <= date.fromisoformat(example["as_of"])
        if anchor["end_date"] is not None:
            assert date.fromisoformat(anchor["end_date"]) <= date.fromisoformat(example["as_of"])


def test_triggered_has_direct_execution_semantics():
    example = load(EXAMPLE_PATH)
    triggered = json.loads(json.dumps(example))
    triggered["decision"].update(
        {
            "status": "TRIGGERED",
            "status_label_zh": "已觸發進場",
            "intent": "ENTER_MOTHER",
            "trigger_state": "CONFIRMED",
            "execution_rule": "NEXT_TRADING_DAY_OPEN",
            "entry_role": "MOTHER",
            "planned_nominal_twd": 10000,
        }
    )
    assert triggered["decision"]["trigger_state"] == "CONFIRMED"
    assert triggered["decision"]["execution_rule"] == "NEXT_TRADING_DAY_OPEN"
    assert triggered["decision"]["entry_role"] == "MOTHER"


def test_small_scale_stop_can_start_a_new_mother_episode():
    example = load(EXAMPLE_PATH)
    stopped = json.loads(json.dumps(example))
    stopped["decision"].update(
        {
            "status": "STOPPED",
            "status_label_zh": "停損出場",
            "intent": "EXIT",
            "trigger_state": "NONE",
            "execution_rule": "NEXT_TRADING_DAY_OPEN",
            "entry_role": "NONE",
            "planned_nominal_twd": 0,
            "campaign_id": "6282-UP-20250411",
            "trade_episode_id": "6282-UP-20250411-E01",
            "attempt_number": 1,
            "reentry_eligible": True,
            "reentry_reason": "小級防線失效，但大級向上定錨與大級防線尚未被破壞。",
        }
    )
    assert stopped["decision"]["reentry_eligible"] is True
    assert stopped["decision"]["campaign_id"]
    assert stopped["decision"]["trade_episode_id"]

    reentry = json.loads(json.dumps(stopped))
    reentry["decision"].update(
        {
            "status": "TRIGGERED",
            "status_label_zh": "已觸發進場",
            "intent": "ENTER_MOTHER",
            "trigger_state": "CONFIRMED",
            "entry_role": "MOTHER",
            "planned_nominal_twd": 10000,
            "trade_episode_id": "6282-UP-20250411-E02",
            "attempt_number": 2,
            "reentry_eligible": False,
            "reentry_reason": "新的獨立小級轉強已觸發，建立第二個母單回合。",
        }
    )
    assert reentry["decision"]["entry_role"] == "MOTHER"
    assert reentry["decision"]["trade_episode_id"] != stopped["decision"]["trade_episode_id"]
    assert reentry["decision"]["attempt_number"] == 2


def test_calibration_manifest_is_unique_causal_and_explicitly_in_sample():
    payload = load(CASES_PATH)
    ids = [item["id"] for item in payload["cases"]]
    assert len(ids) == len(set(ids))
    assert payload["rules"]["ai_receives_only"] == "ai_visible"
    assert payload["rules"]["evaluator_only_must_be_hidden"] is True
    assert payload["rules"]["all_cases_are_performance_holdout"] is False
    for item in payload["cases"]:
        date.fromisoformat(item["analysis_as_of"])
        assert item["is_performance_holdout"] is False
        assert item["ai_visible"]["required_questions"]
        assert "known_design_contamination" in item["evaluator_only"]
