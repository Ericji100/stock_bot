from scripts.hybrid_v3_packet_preflight_v1 import question_count


def test_question_count_covers_global_and_candidate_atoms() -> None:
    packet = {
        "question_manifest": {
            "global_question_ids": ["G1", "G2"],
            "anchor_candidates": [
                {"subject_ref": "A:1", "required_question_ids": ["A1", "A2", "A3"]}
            ],
            "relation_candidates": [
                {"subject_ref": "R:1", "required_question_ids": ["R1"]}
            ],
            "stop_candidates": [],
        }
    }
    assert question_count(packet) == 6
