from scripts.hybrid_v3_mature_s1_consistency_v1 import stage_permission


def test_stage_permission_only_allows_mature_v2_core_trade() -> None:
    assert (
        stage_permission(
            {
                "permission": "TRADE",
                "route": "V2_CORE",
                "scenario": "MATURE_TREND_PULLBACK",
            }
        )
        == "TRADE"
    )
    assert (
        stage_permission(
            {
                "permission": "TRADE",
                "route": "V2_CORE",
                "scenario": "MACRO_COPY_RESONANCE",
            }
        )
        == "WAIT"
    )
    assert (
        stage_permission(
            {
                "permission": "TRADE",
                "route": "NEAR_PASS_MACRO_COPY",
                "scenario": "MATURE_TREND_PULLBACK",
            }
        )
        == "WAIT"
    )
    assert stage_permission({"permission": "REMOVE"}) == "REMOVE"
