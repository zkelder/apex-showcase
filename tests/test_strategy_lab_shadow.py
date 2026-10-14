from __future__ import annotations

from apex.adapters.strategy_lab_shadow_synthetic import run_strategy_lab_shadow_demo


def test_shadow_demo_is_forward_only_and_operator_gated(tmp_path) -> None:
    result = run_strategy_lab_shadow_demo(artifact_root=tmp_path / "artifacts")
    assert result["wrong_confirmation_rejected"] is True
    assert result["forward_predictions"] == 120
    assert result["resolved_predictions"] == 120
    assert result["challenger_recommendation"] == "ELIGIBLE_FOR_CHAMPION_REVIEW"
    assert result["champion_assignment"] == "ACTIVE"
    assert result["pause_resume_revoke"] == ("PAUSED", "ACTIVE", "REVOKED")
    assert result["paused_blocks_inference"] is True
    assert result["revoked_blocks_inference"] is True
    assert result["retire_state"] == "RETIRED"
    assert result["retired_blocks_inference"] is True
    assert result["replay_conflict_rejected"] is True
    assert result["candidate_b_state"] == "ELIGIBLE_FOR_SHADOW_REVIEW"
    assert result["candidate_b_resolved_predictions"] == 120
    assert result["candidate_b_recommendation"] == "REJECT_CHALLENGER"
    assert result["candidate_b_drift_state"] == "DEGRADED"
    assert result["champion_unchanged_after_b"] is True
    assert result["artifact_integrity_failed"] is True
    assert result["artifact_invalidated_state"] == "INVALIDATED"
    assert result["artifact_invalidated_predictions"] == 0
    assert result["provider_calls"] == 0
    assert result["opportunity_proposals"] == 0
    assert result["financial_mutations"] == 0
    assert result["strategy_authority"] == 0
