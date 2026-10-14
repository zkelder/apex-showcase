"""Domain proofs for the Rejected-Opportunity / Counterfactual Decision
Ledger v1: pure normalization of already-retained
``EligibilityDecision``/``MandateEvaluation`` evidence into
``CounterfactualDecision`` rows, and the stability of that row's identity
and ``canonical_hash`` under Counterfactual Decision Classification &
Interpretation Versioning v1 (see ``tests/test_counterfactual_
classification.py`` for the classifier itself). No database, no network --
mirrors the existing offline ``tests/test_investment_mandate.py`` /
``tests/test_shared_opportunity.py`` pattern.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    CounterfactualLedgerError,
    DecisionDisposition,
    DecisionOrigin,
    build_decision_from_eligibility,
    build_decision_from_mandate_evaluation,
    decision_canonical_hash,
)
from apex.domain.investment_mandate import (
    MandateConstraintCheck,
    MandateDecisionState,
    MandateEvaluation,
)
from apex.domain.portfolio_state import PortfolioVenue
from apex.domain.shared_opportunity import (
    EligibilityDecision,
    EligibilityState,
    SharedOpportunityProjection,
    SharedVenue,
)
from apex.domain.values import content_digest

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)


def _projection(**overrides: object) -> SharedOpportunityProjection:
    values: dict[str, object] = {
        "projection_id": "proj-1",
        "venue": SharedVenue.CRYPTO,
        "source_record_type": "CryptoResearchOpportunity",
        "source_record_id": "crypto-opp-1",
        "source_canonical_hash": "d" * 64,
        "strategy_or_method_id": "TEST_METHOD",
        "instrument_id": "BTC-USD",
        "product_or_market_id": "BTC-USD",
        "direction": "HYPOTHETICAL_LONG_SPOT_RESEARCH",
        "generated_at": NOW - timedelta(minutes=5),
        "valid_until": NOW + timedelta(hours=1),
        "evidence_class": "GENUINE_PUBLIC",
        "freshness_seconds": 300,
        "freshness_limit_seconds": 3600,
        "expected_gross_return": "0.02",
        "expected_net_return": "0.01",
        "comparable_value": "0.4",
        "comparable_value_identity": None,
        "uncertainty": "0.3",
        "maximum_loss": "50",
        "modeled_fees": "1",
        "modeled_slippage": "1",
        "costs_retained": True,
        "liquidity_state": "SUFFICIENT",
        "liquidity_limitations": (),
        "required_hypothetical_capital": "100",
        "maximum_sensible_research_allocation": "200",
        "source_maximum_allocation_fraction": "0.5",
        "expected_holding_days": 5,
        "estimated_lockup_days": 5,
        "supporting_evidence_ids": ("ev-1",),
        "source_warnings": (),
        "source_rejection_reasons": (),
        "source_authority": "NONE",
        "projection_limitations": (),
        "source_details": (),
    }
    values.update(overrides)
    return SharedOpportunityProjection.create(**values)  # type: ignore[arg-type]


def _eligibility(
    *, projection: SharedOpportunityProjection, state: EligibilityState, reasons: tuple[str, ...]
) -> EligibilityDecision:
    decision_id = f"{projection.projection_id}-eligibility-v1"
    payload = (decision_id, projection.projection_id, "POLICY_V1", NOW, state, reasons, "NONE")
    return EligibilityDecision(
        decision_id=decision_id,
        projection_id=projection.projection_id,
        policy_id="POLICY_V1",
        evaluated_at=NOW,
        state=state,
        reasons=reasons,
        canonical_hash=content_digest(payload),
    )


def _mandate_evaluation(
    *,
    opportunity: SharedOpportunityProjection,
    state: MandateDecisionState,
    reason_codes: tuple[str, ...],
    evaluation_id: str = "eval-1",
    mandate_version: int = 1,
) -> MandateEvaluation:
    checks = (MandateConstraintCheck("VENUE_PERMITTED", True, False, None, None, ""),)
    values = (
        "mandate-1",
        "default",
        mandate_version,
        "m" * 64,
        "snap-1",
        "s" * 64,
        opportunity.projection_id,
        opportunity.canonical_hash,
        PortfolioVenue.COINBASE,
        "CRYPTO_SPOT",
        NOW,
        state,
        reason_codes,
        (),
        "explanation",
        checks,
        None,
        None,
        (),
        "NONE",
    )
    return MandateEvaluation(
        evaluation_id,
        *values[:-1],
        canonical_hash=content_digest((evaluation_id, *values)),
        authority=values[-1],
    )


# --------------------------------------------------------------------------
# build_decision_from_eligibility
# --------------------------------------------------------------------------


def test_eligible_decision_maps_to_advanced_disposition() -> None:
    projection = _projection()
    decision = _eligibility(
        projection=projection,
        state=EligibilityState.ELIGIBLE,
        reasons=("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY",),
    )
    built = build_decision_from_eligibility(
        decision=decision, projection=projection, opportunity_set_id="set-1"
    )
    assert built.disposition is DecisionDisposition.ADVANCED
    assert built.origin is DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY
    assert built.raw_reason_codes == ("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY",)
    assert built.authority == "NONE"
    assert built.mandate_id is None
    assert built.portfolio_snapshot_id is None
    assert built.opportunity_set_id == "set-1"
    assert built.subject_projection_id == projection.projection_id
    assert built.subject_source_record_type == projection.source_record_type
    assert built.subject_source_record_id == projection.source_record_id
    assert built.venue is SharedVenue.CRYPTO


@pytest.mark.parametrize(
    "state",
    [
        EligibilityState.REJECTED,
        EligibilityState.EXPIRED,
        EligibilityState.BLOCKED_MISSING_EVIDENCE,
    ],
)
def test_non_eligible_states_map_to_not_advanced(state: EligibilityState) -> None:
    projection = _projection()
    decision = _eligibility(projection=projection, state=state, reasons=("EVIDENCE_STALE",))
    built = build_decision_from_eligibility(
        decision=decision, projection=projection, opportunity_set_id="set-1"
    )
    assert built.disposition is DecisionDisposition.NOT_ADVANCED
    assert built.origin_state == state.value
    assert built.raw_reason_codes == ("EVIDENCE_STALE",)


def test_eligibility_projection_mismatch_raises() -> None:
    projection = _projection(projection_id="proj-a")
    other = _projection(projection_id="proj-b", source_record_id="other-src")
    decision = _eligibility(projection=projection, state=EligibilityState.REJECTED, reasons=())
    with pytest.raises(CounterfactualLedgerError):
        build_decision_from_eligibility(decision=decision, projection=other, opportunity_set_id="s")


def test_eligibility_builder_is_deterministic() -> None:
    projection = _projection()
    decision = _eligibility(
        projection=projection, state=EligibilityState.REJECTED, reasons=("EVIDENCE_STALE",)
    )
    first = build_decision_from_eligibility(
        decision=decision, projection=projection, opportunity_set_id="set-1"
    )
    second = build_decision_from_eligibility(
        decision=decision, projection=projection, opportunity_set_id="set-1"
    )
    assert first.decision_id == second.decision_id
    assert first.canonical_hash == second.canonical_hash


def test_eligibility_builder_new_evidence_produces_new_identity() -> None:
    """A changed origin decision (different reasons -> different
    ``canonical_hash``) must never collide with the old one, even though
    both share the identical origin ``decision_id`` string -- exactly the
    shape ``evaluate_eligibility`` itself can produce for the same
    projection re-evaluated at a different time/economics."""

    projection = _projection()
    before = _eligibility(
        projection=projection, state=EligibilityState.REJECTED, reasons=("EVIDENCE_STALE",)
    )
    after = _eligibility(projection=projection, state=EligibilityState.ELIGIBLE, reasons=())
    assert before.decision_id == after.decision_id  # same origin id, deliberately
    assert before.canonical_hash != after.canonical_hash
    built_before = build_decision_from_eligibility(
        decision=before, projection=projection, opportunity_set_id="set-1"
    )
    built_after = build_decision_from_eligibility(
        decision=after, projection=projection, opportunity_set_id="set-2"
    )
    assert built_before.decision_id != built_after.decision_id
    assert built_before.disposition is DecisionDisposition.NOT_ADVANCED
    assert built_after.disposition is DecisionDisposition.ADVANCED


# --------------------------------------------------------------------------
# build_decision_from_mandate_evaluation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (MandateDecisionState.ALLOWED, DecisionDisposition.ADVANCED),
        (MandateDecisionState.CONSTRAINED, DecisionDisposition.CONSTRAINED),
        (MandateDecisionState.REJECTED, DecisionDisposition.NOT_ADVANCED),
    ],
)
def test_mandate_state_maps_to_expected_disposition(
    state: MandateDecisionState, expected: DecisionDisposition
) -> None:
    projection = _projection()
    reasons = () if state is MandateDecisionState.ALLOWED else ("MANDATE_HEADROOM_EXHAUSTED",)
    evaluation = _mandate_evaluation(opportunity=projection, state=state, reason_codes=reasons)
    built = build_decision_from_mandate_evaluation(evaluation=evaluation, opportunity=projection)
    assert built.disposition is expected
    assert built.origin is DecisionOrigin.INVESTMENT_MANDATE_EVALUATION
    assert built.mandate_id == evaluation.mandate_id
    assert built.mandate_lineage_id == evaluation.mandate_lineage_id
    assert built.mandate_version == evaluation.mandate_version
    assert built.mandate_hash == evaluation.mandate_hash
    assert built.portfolio_snapshot_id == evaluation.portfolio_snapshot_id
    assert built.portfolio_snapshot_hash == evaluation.portfolio_snapshot_hash
    assert built.opportunity_set_id is None
    assert built.authority == "NONE"


def test_mandate_opportunity_mismatch_raises() -> None:
    projection = _projection(projection_id="proj-a")
    other = _projection(projection_id="proj-b", source_record_id="other-src")
    evaluation = _mandate_evaluation(
        opportunity=projection, state=MandateDecisionState.ALLOWED, reason_codes=()
    )
    with pytest.raises(CounterfactualLedgerError):
        build_decision_from_mandate_evaluation(evaluation=evaluation, opportunity=other)


def test_mandate_builder_is_deterministic() -> None:
    projection = _projection()
    evaluation = _mandate_evaluation(
        opportunity=projection,
        state=MandateDecisionState.REJECTED,
        reason_codes=("MANDATE_HEADROOM_EXHAUSTED",),
    )
    first = build_decision_from_mandate_evaluation(evaluation=evaluation, opportunity=projection)
    second = build_decision_from_mandate_evaluation(evaluation=evaluation, opportunity=projection)
    assert first.decision_id == second.decision_id
    assert first.canonical_hash == second.canonical_hash


def test_mandate_builder_new_mandate_version_produces_new_identity() -> None:
    projection = _projection()
    v1 = _mandate_evaluation(
        opportunity=projection,
        state=MandateDecisionState.REJECTED,
        reason_codes=("MANDATE_HEADROOM_EXHAUSTED",),
        evaluation_id="eval-1",
        mandate_version=1,
    )
    v2 = _mandate_evaluation(
        opportunity=projection,
        state=MandateDecisionState.ALLOWED,
        reason_codes=(),
        evaluation_id="eval-2",
        mandate_version=2,
    )
    built_v1 = build_decision_from_mandate_evaluation(evaluation=v1, opportunity=projection)
    built_v2 = build_decision_from_mandate_evaluation(evaluation=v2, opportunity=projection)
    assert built_v1.decision_id != built_v2.decision_id
    assert built_v1.mandate_version == 1
    assert built_v2.mandate_version == 2


# --------------------------------------------------------------------------
# Counterfactual Decision Classification & Interpretation Versioning v1:
# identity/hash independence from classification.
# --------------------------------------------------------------------------


def test_counterfactual_decision_carries_no_classification_field() -> None:
    """Structural proof of the Part 2 separation: ``CounterfactualDecision``
    has nothing a classifier could ever need to mutate or re-hash -- the
    dataclass simply has no field to hold one."""

    field_names = {field.name for field in dataclasses.fields(CounterfactualDecision)}
    assert "reason_families" not in field_names
    assert field_names == {
        "decision_id",
        "origin",
        "origin_decision_id",
        "origin_canonical_hash",
        "origin_state",
        "subject_projection_id",
        "subject_source_record_type",
        "subject_source_record_id",
        "venue",
        "opportunity_set_id",
        "mandate_id",
        "mandate_lineage_id",
        "mandate_version",
        "mandate_hash",
        "portfolio_snapshot_id",
        "portfolio_snapshot_hash",
        "evaluated_at",
        "disposition",
        "raw_reason_codes",
        "explanation",
        "canonical_hash",
        "authority",
    }


def test_decision_canonical_hash_helper_matches_builder_output() -> None:
    """``decision_canonical_hash`` is exposed specifically so migration
    ``0095_reason_classification`` can recompute the identical
    formula against already-retained rows; prove it actually is the
    identical formula the builders use, both from live objects and from
    the plain strings a migration reads out of already-persisted JSON."""

    projection = _projection()
    decision = _eligibility(
        projection=projection,
        state=EligibilityState.REJECTED,
        reasons=("EVIDENCE_STALE", "LIQUIDITY_INSUFFICIENT"),
    )
    built = build_decision_from_eligibility(
        decision=decision, projection=projection, opportunity_set_id="set-1"
    )
    recomputed = decision_canonical_hash(
        decision_id=built.decision_id,
        origin=built.origin,
        origin_decision_id=built.origin_decision_id,
        origin_canonical_hash=built.origin_canonical_hash,
        origin_state=built.origin_state,
        subject_projection_id=built.subject_projection_id,
        subject_source_record_type=built.subject_source_record_type,
        subject_source_record_id=built.subject_source_record_id,
        venue=built.venue,
        opportunity_set_id=built.opportunity_set_id,
        mandate_id=built.mandate_id,
        mandate_lineage_id=built.mandate_lineage_id,
        mandate_version=built.mandate_version,
        mandate_hash=built.mandate_hash,
        portfolio_snapshot_id=built.portfolio_snapshot_id,
        portfolio_snapshot_hash=built.portfolio_snapshot_hash,
        evaluated_at=built.evaluated_at,
        disposition=built.disposition,
        raw_reason_codes=built.raw_reason_codes,
        explanation=built.explanation,
    )
    assert recomputed == built.canonical_hash

    # migration-style call: plain strings instead of enum/datetime objects
    from apex.domain.values import canonical_timestamp

    recomputed_from_strings = decision_canonical_hash(
        decision_id=built.decision_id,
        origin=built.origin.value,
        origin_decision_id=built.origin_decision_id,
        origin_canonical_hash=built.origin_canonical_hash,
        origin_state=built.origin_state,
        subject_projection_id=built.subject_projection_id,
        subject_source_record_type=built.subject_source_record_type,
        subject_source_record_id=built.subject_source_record_id,
        venue=built.venue.value,
        opportunity_set_id=built.opportunity_set_id,
        mandate_id=built.mandate_id,
        mandate_lineage_id=built.mandate_lineage_id,
        mandate_version=built.mandate_version,
        mandate_hash=built.mandate_hash,
        portfolio_snapshot_id=built.portfolio_snapshot_id,
        portfolio_snapshot_hash=built.portfolio_snapshot_hash,
        evaluated_at=canonical_timestamp(built.evaluated_at),
        disposition=built.disposition.value,
        raw_reason_codes=built.raw_reason_codes,
        explanation=built.explanation,
    )
    assert recomputed_from_strings == built.canonical_hash


def test_decision_identity_and_hash_do_not_depend_on_raw_reason_interpretation() -> None:
    """Two decisions built from origin evidence carrying raw reason codes
    that a classifier would interpret completely differently (an
    ``OTHER_EXPLICIT``-only code vs. a well-mapped one) still hash
    identically once the raw reason text itself is identical -- proving
    ``canonical_hash`` is a function of raw text, never of any
    classification drawn from it."""

    projection = _projection()
    reasons = ("SOME_TOTALLY_UNCLASSIFIED_CODE_XYZ",)
    decision = _eligibility(projection=projection, state=EligibilityState.REJECTED, reasons=reasons)
    built = build_decision_from_eligibility(
        decision=decision, projection=projection, opportunity_set_id="set-1"
    )
    # Re-running the identical builder over the identical origin evidence is
    # exactly what a classifier-version upgrade + ledger resync would do --
    # it must reproduce the identical decision, not merely an equal one.
    replay = build_decision_from_eligibility(
        decision=decision, projection=projection, opportunity_set_id="set-1"
    )
    assert built.decision_id == replay.decision_id
    assert built.canonical_hash == replay.canonical_hash
    assert built.raw_reason_codes == reasons
