"""Domain + application proofs for Counterfactual Decision Follow-Up
Observation v1: deterministic due-state, the two pure builders (crypto price
movement, Kalshi market status/settlement), stable content-addressed
identity independent of classifier version and wall-clock capture time, and
the planning/bounded-sync orchestration services against in-memory fakes.
No database, no network -- mirrors the existing offline
``tests/test_counterfactual_ledger.py``/``tests/test_counterfactual_
classification.py`` pattern.  See ``tests/persistence/
test_postgres_counterfactual_followup.py`` for real-Postgres persistence/
idempotency/point-in-time proofs.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from apex.application.counterfactual_followup import (
    CryptoPriceLookup,
    FollowUpScanPosition,
    FollowUpSourceUnavailable,
    KalshiMarketRead,
    PlanFollowUpObservations,
    SyncFollowUpObservations,
)
from apex.application.kalshi_resolution import InMemoryKalshiResolutionFactRepository
from apex.domain.counterfactual_classification import build_classification
from apex.domain.counterfactual_followup import (
    COUNTERFACTUAL_FOLLOWUP_OBSERVATION_V1,
    CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE,
    CRYPTO_DAILY_CLOSE_LIMITATION,
    CRYPTO_DECISION_INPUT_REFERENCE_NOT_ENDPOINT_LIMITATION,
    CRYPTO_DECISION_INPUT_REFERENCE_PERMANENTLY_UNAVAILABLE,
    CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1,
    CRYPTO_FOLLOWUP_BASELINE_EVIDENCE_INELIGIBLE,
    CRYPTO_FOLLOWUP_DECISION_INPUT_EVIDENCE_INELIGIBLE,
    CRYPTO_FOLLOWUP_TARGET_EVIDENCE_INELIGIBLE,
    CRYPTO_FOLLOWUP_V3_FACT_ON_NON_V3_POLICY,
    CRYPTO_FOLLOWUP_V3_MEASUREMENT_AS_OF_PRECEDES_WINDOW,
    CRYPTO_FOLLOWUP_V3_REQUIRED_FACT_MISSING,
    CRYPTO_FOLLOWUP_V3_VENUE_UNSUPPORTED,
    CRYPTO_FOLLOWUP_V3_WINDOW_PRECEDES_ANCHOR,
    CRYPTO_FOLLOWUP_V3_WINDOW_SPAN_INVALID,
    CRYPTO_FOLLOWUP_V3_WINDOW_START_NOT_DERIVED,
    CRYPTO_MEASUREMENT_AS_OF_BOUNDED_LIMITATION,
    CRYPTO_V3_WINDOW_END_MEASUREMENT_UNAVAILABLE,
    CURRENT_EVIDENCE_ELIGIBILITY_POLICY_VERSION,
    CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION,
    CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE,
    CURRENT_FOLLOWUP_POLICY_VERSIONS,
    FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_POLICY_V1,
    FOLLOWUP_POLICY_V2,
    FOLLOWUP_POLICY_V3,
    FOLLOWUP_POLICY_V3_VENUES,
    FOLLOWUP_V3_POLICY_VERSION_UNSUPPORTED_FOR_VENUE,
    KNOWN_EVIDENCE_ELIGIBILITY_POLICY_VERSIONS,
    KNOWN_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSIONS,
    KNOWN_FOLLOWUP_POLICY_VERSIONS,
    RETURN_QUANTUM,
    SHORT_HORIZON_DAYS,
    CounterfactualFollowUpError,
    FollowUpBaselineKind,
    FollowUpDueState,
    FollowUpEvidenceDefectType,
    FollowUpEvidenceQualityVerdict,
    FollowUpHorizonKind,
    FollowUpObservationType,
    assess_followup_evidence_quality,
    build_crypto_price_movement_observation,
    build_kalshi_market_status_observation,
    current_followup_policy_venues,
    current_followup_policy_version,
    daily_close_bucket_for_boundary,
    determine_followup_due_state,
    evidence_quality_assessment_id,
    first_daily_close_boundary_at_or_after,
    followup_observation_canonical_hash,
    followup_observation_id,
    followup_policy_version_supports_venue,
    forward_measurement_window,
    is_evidence_eligible_for_downstream_attribution,
    is_followup_evidence_eligible,
    observation_canonical_hash_matches,
)
from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    DecisionDisposition,
    DecisionOrigin,
)
from apex.domain.crypto_market_pulse import completed_utc_day
from apex.domain.kalshi_resolution import KalshiResolutionState, build_kalshi_resolution_fact
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import content_digest, deterministic_id

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _decision(**overrides: object) -> CounterfactualDecision:
    values: dict[str, object] = {
        "decision_id": "cf-decision-1",
        "origin": DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY,
        "origin_decision_id": "proj-1-eligibility-v1",
        "origin_canonical_hash": "a" * 64,
        "origin_state": "REJECTED",
        "subject_projection_id": "proj-1",
        "subject_source_record_type": "CryptoResearchOpportunity",
        "subject_source_record_id": "crypto-opp-1",
        "venue": SharedVenue.CRYPTO,
        "opportunity_set_id": "set-1",
        "mandate_id": None,
        "mandate_lineage_id": None,
        "mandate_version": None,
        "mandate_hash": None,
        "portfolio_snapshot_id": None,
        "portfolio_snapshot_hash": None,
        "evaluated_at": NOW,
        "disposition": DecisionDisposition.NOT_ADVANCED,
        "raw_reason_codes": ("EVIDENCE_STALE",),
        "explanation": "test fixture",
        "canonical_hash": "b" * 64,
        "authority": "NONE",
    }
    values.update(overrides)
    return CounterfactualDecision(**values)  # type: ignore[arg-type]


def _projection(**overrides: object) -> SharedOpportunityProjection:
    values: dict[str, object] = {
        "projection_id": "proj-1",
        "venue": SharedVenue.CRYPTO,
        "source_record_type": "CryptoResearchOpportunity",
        "source_record_id": "crypto-opp-1",
        "source_canonical_hash": "d" * 64,
        "strategy_or_method_id": "TEST_METHOD",
        "instrument_id": "BTC",
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
        "expected_holding_days": 30,
        "estimated_lockup_days": 30,
        "supporting_evidence_ids": ("ev-1",),
        "source_warnings": (),
        "source_rejection_reasons": (),
        "source_authority": "NONE",
        "projection_limitations": (),
        "source_details": (),
    }
    values.update(overrides)
    return SharedOpportunityProjection.create(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# determine_followup_due_state: pure, total, deterministic
# --------------------------------------------------------------------------


def test_due_state_short_horizon_not_yet_due_before_target() -> None:
    result = determine_followup_due_state(
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_anchor_at=NOW,
        expected_holding_days=None,
        as_of=NOW + timedelta(hours=1),
        already_observed=False,
    )
    assert result.state is FollowUpDueState.NOT_YET_DUE
    assert result.target_time == NOW + timedelta(days=SHORT_HORIZON_DAYS)


def test_due_state_short_horizon_due_at_exact_target() -> None:
    result = determine_followup_due_state(
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_anchor_at=NOW,
        expected_holding_days=None,
        as_of=NOW + timedelta(days=SHORT_HORIZON_DAYS),
        already_observed=False,
    )
    assert result.state is FollowUpDueState.DUE
    assert result.horizon_days == SHORT_HORIZON_DAYS


def test_due_state_strategy_horizon_unsupported_without_declared_holding_days() -> None:
    result = determine_followup_due_state(
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
        horizon_anchor_at=NOW,
        expected_holding_days=None,
        as_of=NOW,
        already_observed=False,
    )
    assert result.state is FollowUpDueState.UNSUPPORTED
    assert result.target_time is None
    assert result.horizon_days is None
    assert "STRATEGY_HORIZON_REQUIRES_SUBJECT_DECLARED_HOLDING_DAYS" in result.reasons


def test_due_state_strategy_horizon_uses_subject_declared_holding_days() -> None:
    result = determine_followup_due_state(
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
        horizon_anchor_at=NOW,
        expected_holding_days=30,
        as_of=NOW + timedelta(days=30),
        already_observed=False,
    )
    assert result.state is FollowUpDueState.DUE
    assert result.horizon_days == 30
    assert result.target_time == NOW + timedelta(days=30)


def test_due_state_negative_declared_holding_days_is_unsupported() -> None:
    result = determine_followup_due_state(
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
        horizon_anchor_at=NOW,
        expected_holding_days=-1,
        as_of=NOW,
        already_observed=False,
    )
    assert result.state is FollowUpDueState.UNSUPPORTED


def test_due_state_already_observed_reports_observed_not_due_or_unavailable() -> None:
    """Part 11: not-yet-due is not treated as unavailable, and (symmetrically)
    an already-retained observation must never be reported as DUE again --
    OBSERVED is its own terminal state, distinct from both."""

    result = determine_followup_due_state(
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_anchor_at=NOW,
        expected_holding_days=None,
        as_of=NOW + timedelta(days=5),
        already_observed=True,
    )
    assert result.state is FollowUpDueState.OBSERVED


def test_due_state_is_deterministic_pure_function() -> None:
    kwargs = dict(
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_anchor_at=NOW,
        expected_holding_days=None,
        as_of=NOW,
        already_observed=False,
    )
    assert determine_followup_due_state(**kwargs) == determine_followup_due_state(**kwargs)


def test_due_state_origin_evidence_missing_anchor_is_unsupported_not_a_crash() -> None:
    """Pre-deployment staleness-semantics audit: ``horizon_anchor_at`` may be
    ``None`` -- exactly the case where no subject projection is retained so
    there is no ``generated_at`` to anchor an ``ORIGIN_EVIDENCE`` row to.
    Reported as a purely structural ``UNSUPPORTED``, never an exception --
    mirrors the existing missing-``expected_holding_days`` case exactly."""

    result = determine_followup_due_state(
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_anchor_at=None,
        expected_holding_days=None,
        as_of=NOW,
        already_observed=False,
    )
    assert result.state is FollowUpDueState.UNSUPPORTED
    assert result.target_time is None
    assert result.horizon_days is None
    assert "HORIZON_ANCHOR_NOT_AVAILABLE" in result.reasons


# --------------------------------------------------------------------------
# Stable identity (Part 10/13)
# --------------------------------------------------------------------------


def test_followup_observation_id_stable_for_identical_inputs() -> None:
    first = followup_observation_id(
        decision_id="d1",
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    second = followup_observation_id(
        decision_id="d1",
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    assert first == second


def test_followup_observation_id_differs_by_horizon_kind() -> None:
    short = followup_observation_id(
        decision_id="d1",
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    strategy = followup_observation_id(
        decision_id="d1",
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    assert short != strategy


def test_followup_observation_id_differs_by_decision() -> None:
    one = followup_observation_id(
        decision_id="d1",
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    two = followup_observation_id(
        decision_id="d2",
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    assert one != two


def test_followup_observation_id_independent_of_classifier_version() -> None:
    """Part 13, proved structurally: the identity function takes no
    classifier-related argument at all, so a decision classified under both
    REASON_CLASSIFIER_V1 and REASON_CLASSIFIER_V2 still resolves to exactly
    one followup observation id per horizon."""

    decision = _decision()
    classification_v1 = build_classification(
        decision=decision, classified_at=NOW, classifier_version="REASON_CLASSIFIER_V1"
    )
    classification_v2 = build_classification(
        decision=decision, classified_at=NOW, classifier_version="REASON_CLASSIFIER_V2"
    )
    assert classification_v1.classification_id != classification_v2.classification_id
    observation_id = followup_observation_id(
        decision_id=decision.decision_id,
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    # neither classification row references, nor is referenced by, the
    # observation id -- there is no shared field to even compare.
    assert not hasattr(classification_v1, "observation_id")
    assert observation_id.startswith("decision_followup_observation_")


# --------------------------------------------------------------------------
# Pre-deployment staleness-semantics audit: FollowUpBaselineKind identity/
# hash-shape backward compatibility.  ``DECISION_TIME`` must reproduce
# exactly what this repository's identity/hash formulas computed before this
# axis existed -- not "coincidentally the same", but *structurally*
# incapable of differing, because it is the one value that never widens
# either payload.  ``ORIGIN_EVIDENCE`` is the only value that ever does.
# --------------------------------------------------------------------------


def test_followup_observation_id_decision_time_reproduces_pre_baseline_kind_formula() -> None:
    """Golden-value regression: the exact formula this repository's identity
    function used before ``FollowUpBaselineKind`` existed (a bare 4-element
    tuple with no baseline-kind component at all) -- every already-retained
    row in the live database (all ``DECISION_TIME`` in kind, see
    ``FollowUpBaselineKind``'s own docstring) must keep resolving to this
    exact id forever."""

    pre_existing_formula_id = deterministic_id(
        "decision_followup_observation",
        (COUNTERFACTUAL_FOLLOWUP_OBSERVATION_V1, FOLLOWUP_POLICY_V1, "d1", "SHORT_HORIZON"),
    )
    current_id = followup_observation_id(
        decision_id="d1",
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    assert current_id == pre_existing_formula_id


def test_followup_observation_id_differs_by_baseline_kind_only_for_origin_evidence() -> None:
    decision_time_id = followup_observation_id(
        decision_id="d1",
        policy_version=FOLLOWUP_POLICY_V2,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    origin_evidence_id = followup_observation_id(
        decision_id="d1",
        policy_version=FOLLOWUP_POLICY_V2,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
    )
    assert decision_time_id != origin_evidence_id
    # DECISION_TIME reproduces the bare-formula id regardless of which
    # policy version is otherwise identical to the golden-value test above.
    assert decision_time_id == deterministic_id(
        "decision_followup_observation",
        (COUNTERFACTUAL_FOLLOWUP_OBSERVATION_V1, FOLLOWUP_POLICY_V2, "d1", "SHORT_HORIZON"),
    )


# --------------------------------------------------------------------------
# build_crypto_price_movement_observation
# --------------------------------------------------------------------------


def _crypto_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "decision": _decision(),
        "horizon_kind": FollowUpHorizonKind.SHORT_HORIZON,
        "baseline_kind": FollowUpBaselineKind.DECISION_TIME,
        "horizon_days": 1,
        "baseline_time": NOW,
        "target_time": NOW + timedelta(days=1),
        "subject_instrument_id": "BTC",
        "subject_product_or_market_id": "BTC-USD",
        "price_at_baseline": Decimal("100"),
        "price_at_baseline_source_observation_id": "crypto-obs-1",
        "price_at_baseline_evidence_class": EvidenceClass.GENUINE_PUBLIC,
        "price_at_target": Decimal("110"),
        "price_at_target_source_observation_id": "crypto-obs-2",
        "price_at_target_evidence_class": EvidenceClass.HISTORICAL_PUBLIC,
        "observed_at": NOW + timedelta(days=2),
    }
    values.update(overrides)
    return values


def test_build_crypto_observation_computes_defined_subsequent_return() -> None:
    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    assert obs.subsequent_return == Decimal("0.1")
    assert obs.reference_price_at_baseline == Decimal("100")
    assert obs.reference_price_at_target == Decimal("110")
    assert obs.observation_type is FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT
    assert obs.authority == "NONE"


def test_build_crypto_observation_provenance_follows_target_read() -> None:
    """Part 6: reuses EvidenceClass; a CRYPTO observation's provenance
    carries through the *target* bucket's own evidence class (the follow-up
    fact), not the decision-time reference read."""

    obs = build_crypto_price_movement_observation(
        **_crypto_kwargs(
            price_at_baseline_evidence_class=EvidenceClass.GENUINE_PUBLIC,
            price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        )
    )
    assert obs.provenance is EvidenceClass.HISTORICAL_PUBLIC


def test_build_crypto_observation_no_kalshi_fields_populated() -> None:
    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    assert obs.market_status_at_target is None
    assert obs.yes_bid_at_target is None
    assert obs.yes_ask_at_target is None
    assert obs.settlement_result is None
    assert obs.settlement_source is None


def test_build_crypto_observation_target_time_independent_of_observed_at() -> None:
    """Part 5/10: observation capture time never redefines target time, and
    is excluded from identity/hash -- replaying with a later observed_at
    reproduces the identical id/hash (idempotent replay)."""

    target = NOW + timedelta(days=1)
    early = build_crypto_price_movement_observation(
        **_crypto_kwargs(target_time=target, observed_at=NOW + timedelta(days=1))
    )
    late = build_crypto_price_movement_observation(
        **_crypto_kwargs(target_time=target, observed_at=NOW + timedelta(days=30))
    )
    assert early.target_time == late.target_time == target
    assert early.observed_at != late.observed_at
    assert early.observation_id == late.observation_id
    assert early.canonical_hash == late.canonical_hash


def test_build_crypto_observation_changed_evidence_changes_hash() -> None:
    """Part 18: changed underlying follow-up evidence produces a different
    canonical_hash under the identical observation_id -- the persistence
    layer's conflict-detection idiom depends on this."""

    baseline = build_crypto_price_movement_observation(**_crypto_kwargs())
    changed = build_crypto_price_movement_observation(
        **_crypto_kwargs(price_at_target=Decimal("999"))
    )
    assert baseline.observation_id == changed.observation_id
    assert baseline.canonical_hash != changed.canonical_hash


def test_build_crypto_observation_rejects_non_positive_reference_price() -> None:
    with pytest.raises(CounterfactualFollowUpError):
        build_crypto_price_movement_observation(**_crypto_kwargs(price_at_baseline=Decimal("0")))
    with pytest.raises(CounterfactualFollowUpError):
        build_crypto_price_movement_observation(**_crypto_kwargs(price_at_target=Decimal("-5")))


def test_build_crypto_observation_quantizes_non_terminating_return() -> None:
    """Regression: raw ``Decimal`` division on a price pair whose ratio does
    not terminate cleanly (e.g. 1/3) exceeds ``domain.values.parse_decimal``'s
    18-significant-digit cap under Python's default 28-digit division
    context, which genuine acceptance against real retained crypto prices
    hit immediately -- ``subsequent_return`` must always be quantized to the
    same 8-fractional-digit convention every other derived research ratio in
    Apex already uses."""

    obs = build_crypto_price_movement_observation(
        **_crypto_kwargs(price_at_baseline=Decimal("3"), price_at_target=Decimal("1"))
    )
    assert obs.subsequent_return == Decimal("-0.66666667")
    # must not raise when hashed/canonicalized -- this is exactly what failed
    # before the fix, inside followup_observation_canonical_hash.
    assert len(obs.canonical_hash) == 64


@pytest.mark.parametrize(
    "disposition",
    [
        DecisionDisposition.ADVANCED,
        DecisionDisposition.CONSTRAINED,
        DecisionDisposition.NOT_ADVANCED,
    ],
)
def test_build_crypto_observation_supports_every_disposition(
    disposition: DecisionDisposition,
) -> None:
    """Part 3: both advanced and non-advanced cohorts (and CONSTRAINED) are
    equally followable -- nothing here special-cases disposition."""

    decision = _decision(disposition=disposition)
    obs = build_crypto_price_movement_observation(**_crypto_kwargs(decision=decision))
    assert obs.disposition is disposition


# --------------------------------------------------------------------------
# CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
# Remediation v1 -- fail-closed allowlist, defense-in-depth builder guard,
# baseline+target lineage visibility, and the retroactive assessment
# builder.  Regression letters A-G below refer to the milestone's own
# lettered regression-test requirements.
# --------------------------------------------------------------------------


def test_eligibility_allowlist_is_exactly_genuine_and_historical_public() -> None:
    """Locks the allowlist's exact membership -- a future addition to
    ``EvidenceClass`` shows up here as a required, reviewed test change,
    never a silent widening.  ``LIVE`` is deliberately absent (final-review
    correction, same milestone): it has zero producers anywhere in this
    repository, for any evidence domain, and ``domain.research_evidence``'s
    own module docstring names it in the same breath as ``PAPER`` and as an
    "authority" -- an execution/runtime concept, not a confirmed evidence-
    genuineness one -- so it is excluded until a real producer exists and a
    reviewed audit confirms otherwise (see this test's own sibling below and
    the allowlist's own module-level rationale)."""

    assert {
        EvidenceClass.GENUINE_PUBLIC,
        EvidenceClass.HISTORICAL_PUBLIC,
    } == FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES


def test_is_followup_evidence_eligible_true_for_every_allowlisted_class() -> None:
    assert is_followup_evidence_eligible(EvidenceClass.GENUINE_PUBLIC) is True
    assert is_followup_evidence_eligible(EvidenceClass.HISTORICAL_PUBLIC) is True


def test_is_followup_evidence_eligible_false_for_fixture() -> None:
    """The confirmed DOGE-USD contamination vector."""

    assert is_followup_evidence_eligible(EvidenceClass.FIXTURE) is False


def test_is_followup_evidence_eligible_false_for_live_pending_a_real_producer() -> None:
    """Final-review correction (same milestone): ``LIVE`` was originally
    allowed, reasoned as "the single most genuine class conceivable" -- a
    final-review audit found that reasoning ungrounded (zero producers
    anywhere in this repository, and ``domain.research_evidence``'s own
    module docstring pairs "LIVE authority" directly with "PAPER authority",
    i.e. an execution-authority concept) and excluded it until a real
    producer exists and its semantics are confirmed to denote evidence
    genuineness rather than execution authority.  Locked here exactly like
    ``FIXTURE``/``SANDBOX``/``PAPER`` are locked ineligible below -- never a
    silent future re-admission."""

    assert is_followup_evidence_eligible(EvidenceClass.LIVE) is False


def test_is_followup_evidence_eligible_fails_closed_for_never_reviewed_classes() -> None:
    """Regression G: ``SANDBOX``/``PAPER`` stand in for a genuinely future,
    never-yet-reviewed ``EvidenceClass`` member -- both are real, already-
    declared enum values that no adapter in this repository has ever
    produced and that were never explicitly added to the allowlist. A
    denylist shape (``!= EvidenceClass.FIXTURE``) would silently admit
    them; the allowlist must not."""

    assert is_followup_evidence_eligible(EvidenceClass.SANDBOX) is False
    assert is_followup_evidence_eligible(EvidenceClass.PAPER) is False


def test_build_crypto_observation_rejects_ineligible_baseline_evidence_class() -> None:
    """Regression D (baseline side) + defense-in-depth: the one real
    repository already filters ineligible batches before ranking (see
    ``tests/persistence/test_postgres_counterfactual_followup.py``), but the
    pure builder does not rely on its only caller behaving."""

    with pytest.raises(
        CounterfactualFollowUpError, match=CRYPTO_FOLLOWUP_BASELINE_EVIDENCE_INELIGIBLE
    ):
        build_crypto_price_movement_observation(
            **_crypto_kwargs(price_at_baseline_evidence_class=EvidenceClass.FIXTURE)
        )


def test_build_crypto_observation_rejects_ineligible_target_evidence_class() -> None:
    """Regression D (target side): ineligible target evidence must also
    block creation, not merely ineligible baseline evidence."""

    with pytest.raises(
        CounterfactualFollowUpError, match=CRYPTO_FOLLOWUP_TARGET_EVIDENCE_INELIGIBLE
    ):
        build_crypto_price_movement_observation(
            **_crypto_kwargs(price_at_target_evidence_class=EvidenceClass.FIXTURE)
        )


def test_build_crypto_observation_rejects_ineligible_evidence_before_price_math() -> None:
    """The eligibility check is not merely cosmetic -- it fires even when
    the ineligible price would otherwise compute a plausible-looking
    return, exactly the DOGE-USD incident's own shape (a well-formed
    positive Decimal, wrong only because of where it came from)."""

    with pytest.raises(
        CounterfactualFollowUpError, match=CRYPTO_FOLLOWUP_BASELINE_EVIDENCE_INELIGIBLE
    ):
        build_crypto_price_movement_observation(
            **_crypto_kwargs(
                price_at_baseline=Decimal("189.504"),
                price_at_baseline_evidence_class=EvidenceClass.FIXTURE,
                price_at_target=Decimal("0.07215"),
                price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            )
        )


def test_build_crypto_observation_carries_distinct_baseline_and_target_evidence_class() -> None:
    """Regression E: mixed lineage is visibly distinguishable on the
    retained observation -- ``provenance`` alone (target only) could make a
    fixture-derived baseline look like ordinary ``HISTORICAL_PUBLIC``
    evidence; ``baseline_evidence_class``/``target_evidence_class`` cannot."""

    obs = build_crypto_price_movement_observation(
        **_crypto_kwargs(
            price_at_baseline_evidence_class=EvidenceClass.GENUINE_PUBLIC,
            price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        )
    )
    assert obs.baseline_evidence_class is EvidenceClass.GENUINE_PUBLIC
    assert obs.target_evidence_class is EvidenceClass.HISTORICAL_PUBLIC
    assert obs.provenance is EvidenceClass.HISTORICAL_PUBLIC
    assert obs.baseline_evidence_class is not obs.target_evidence_class
    assert obs.evidence_eligibility_policy_version == CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1


def test_build_crypto_observation_baseline_evidence_class_participates_in_hash() -> None:
    """A changed baseline evidence class alone (identical prices, identical
    everything else) must change ``canonical_hash`` -- it is genuinely part
    of the measured payload, not merely descriptive metadata."""

    genuine = build_crypto_price_movement_observation(
        **_crypto_kwargs(price_at_baseline_evidence_class=EvidenceClass.GENUINE_PUBLIC)
    )
    historical = build_crypto_price_movement_observation(
        **_crypto_kwargs(price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC)
    )
    assert genuine.observation_id == historical.observation_id
    assert genuine.canonical_hash != historical.canonical_hash


def test_build_kalshi_observation_baseline_evidence_class_is_none_target_is_genuine_public() -> (
    None
):
    """KALSHI has no baseline batch lineage concept at all (Part: see
    ``DecisionFollowUpObservation.baseline_evidence_class``'s own
    docstring) -- ``target_evidence_class`` is always exactly
    ``provenance``, ``EvidenceClass.GENUINE_PUBLIC``."""

    obs = build_kalshi_market_status_observation(**_kalshi_kwargs())
    assert obs.baseline_evidence_class is None
    assert obs.target_evidence_class is EvidenceClass.GENUINE_PUBLIC
    assert obs.provenance is EvidenceClass.GENUINE_PUBLIC
    assert obs.evidence_eligibility_policy_version is None


# --------------------------------------------------------------------------
# Generation-semantics versioning + canonical-hash legacy round-trip
# (final-review additions, same milestone).
# --------------------------------------------------------------------------


def test_evidence_eligibility_policy_version_constants_are_stable() -> None:
    assert KNOWN_EVIDENCE_ELIGIBILITY_POLICY_VERSIONS == (CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1,)
    assert CURRENT_EVIDENCE_ELIGIBILITY_POLICY_VERSION == CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1


def _pre_milestone_hash_payload(**overrides: object) -> tuple[object, ...]:
    """Reconstructs exactly the fixed-length payload
    ``followup_observation_canonical_hash`` computed on ``main`` (f1f3e9b),
    before this milestone's ``baseline_evidence_class``/``evidence_
    eligibility_policy_version`` axes existed -- used only to prove the
    patched function still reproduces byte-identical hashes for a call that
    omits both new keyword arguments (i.e. every real retained row)."""

    values: dict[str, object] = {
        "kind": COUNTERFACTUAL_FOLLOWUP_OBSERVATION_V1,
        "observation_id": "obs-legacy",
        "decision_id": "dec-legacy",
        "decision_canonical_hash": "a" * 64,
        "venue": SharedVenue.CRYPTO,
        "disposition": DecisionDisposition.NOT_ADVANCED,
        "policy_version": FOLLOWUP_POLICY_V1,
        "horizon_kind": FollowUpHorizonKind.SHORT_HORIZON,
        "horizon_days": 1,
        "decision_evaluated_at": NOW,
        "target_time": NOW,
        "observation_type": FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT,
        "subject_instrument_id": "BTC",
        "subject_product_or_market_id": "BTC-USD",
        "provenance": EvidenceClass.HISTORICAL_PUBLIC,
        "source_lineage": ("crypto-obs-1",),
        "data_limitations": (),
        "reference_price_at_baseline": Decimal("100"),
        "reference_price_at_target": Decimal("105"),
        "subsequent_return": Decimal("0.05"),
        "market_status_at_target": None,
        "yes_bid_at_target": None,
        "yes_ask_at_target": None,
        "settlement_result": None,
        "settlement_source": None,
        "authority": "NONE",
    }
    values.update(overrides)
    return (
        values["kind"],
        values["observation_id"],
        values["decision_id"],
        values["decision_canonical_hash"],
        values["venue"],
        values["disposition"],
        values["policy_version"],
        values["horizon_kind"],
        values["horizon_days"],
        values["decision_evaluated_at"],
        values["target_time"],
        values["observation_type"],
        values["subject_instrument_id"],
        values["subject_product_or_market_id"],
        values["provenance"],
        values["source_lineage"],
        values["data_limitations"],
        values["reference_price_at_baseline"],
        values["reference_price_at_target"],
        values["subsequent_return"],
        values["market_status_at_target"],
        values["yes_bid_at_target"],
        values["yes_ask_at_target"],
        values["settlement_result"],
        values["settlement_source"],
        values["authority"],
    )


def _hash_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "observation_id": "obs-legacy",
        "decision_id": "dec-legacy",
        "decision_canonical_hash": "a" * 64,
        "venue": SharedVenue.CRYPTO,
        "disposition": DecisionDisposition.NOT_ADVANCED,
        "policy_version": FOLLOWUP_POLICY_V1,
        "horizon_kind": FollowUpHorizonKind.SHORT_HORIZON,
        "horizon_days": 1,
        "decision_evaluated_at": NOW,
        "target_time": NOW,
        "observation_type": FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT,
        "subject_instrument_id": "BTC",
        "subject_product_or_market_id": "BTC-USD",
        "provenance": EvidenceClass.HISTORICAL_PUBLIC,
        "source_lineage": ("crypto-obs-1",),
        "data_limitations": (),
        "reference_price_at_baseline": Decimal("100"),
        "reference_price_at_target": Decimal("105"),
        "subsequent_return": Decimal("0.05"),
        "market_status_at_target": None,
        "yes_bid_at_target": None,
        "yes_ask_at_target": None,
        "settlement_result": None,
        "settlement_source": None,
        "baseline_kind": FollowUpBaselineKind.DECISION_TIME,
        "baseline_time": NOW,
    }
    values.update(overrides)
    return values


def test_canonical_hash_legacy_call_reproduces_pre_milestone_formula_byte_for_byte() -> None:
    """The decisive regression test for the final-review-confirmed defect:
    the milestone's original draft appended ``baseline_evidence_class``
    to the hash payload *unconditionally*, which changed every one of the
    92 real retained rows' recomputed hash even though the parameter
    defaults to ``None`` -- a payload append changes the tuple's length
    regardless of the appended value.  A call that omits both new keyword
    arguments entirely (i.e. every call site that predates this milestone,
    and every KALSHI call site of any age) must reproduce the exact fixed
    tuple ``content_digest`` was always given before this milestone
    existed."""

    kwargs = _hash_kwargs()
    pre_milestone = content_digest(_pre_milestone_hash_payload())
    patched_omitting_new_kwargs = followup_observation_canonical_hash(**kwargs)
    assert patched_omitting_new_kwargs == pre_milestone


def test_canonical_hash_baseline_evidence_class_participates_only_when_present() -> None:
    kwargs = _hash_kwargs()
    without = followup_observation_canonical_hash(**kwargs)
    with_baseline_class = followup_observation_canonical_hash(
        **kwargs, baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC
    )
    assert without != with_baseline_class


def test_canonical_hash_evidence_eligibility_policy_version_participates_only_when_present() -> (
    None
):
    kwargs = _hash_kwargs()
    without = followup_observation_canonical_hash(**kwargs)
    with_policy_version = followup_observation_canonical_hash(
        **kwargs, evidence_eligibility_policy_version=CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1
    )
    assert without != with_policy_version


def test_canonical_hash_real_crypto_build_round_trips_via_observation_canonical_hash_matches() -> (
    None
):
    """``observation_canonical_hash_matches`` (Section 6) must accept every
    genuine, untampered observation this module's own builders produce --
    both venues."""

    crypto_obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    kalshi_obs = build_kalshi_market_status_observation(**_kalshi_kwargs())
    assert observation_canonical_hash_matches(crypto_obs) is True
    assert observation_canonical_hash_matches(kalshi_obs) is True


def test_observation_canonical_hash_matches_false_when_tampered() -> None:
    """A hand-constructed, mismatched ``canonical_hash`` (standing in for a
    corrupted or tampered retained row) must be detected, never silently
    trusted."""

    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    tampered = replace(obs, canonical_hash="f" * 64)
    assert observation_canonical_hash_matches(tampered) is False


def test_assess_evidence_quality_marks_tampered_observation_ineligible_before_venue_check() -> None:
    """Section 6: a hash mismatch is checked first, before even the KALSHI
    always-eligible short-circuit -- an observation whose hash does not
    match its own retained fields can no longer be trusted for *any* fact,
    including ``venue``."""

    kalshi_obs = build_kalshi_market_status_observation(**_kalshi_kwargs())
    tampered = replace(kalshi_obs, canonical_hash="f" * 64)
    assessment = assess_followup_evidence_quality(
        observation=tampered,
        baseline_evidence_class=None,
        target_evidence_class=None,
        lineage_resolved=False,
        assessed_at=NOW,
    )
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert assessment.evidence_defect_type is FollowUpEvidenceDefectType.OBSERVATION_HASH_INVALID


# --------------------------------------------------------------------------
# is_evidence_eligible_for_downstream_attribution -- the centralized,
# fail-closed downstream contract (Section 4, final-review addition).
# --------------------------------------------------------------------------


def test_downstream_attribution_rejects_unassessed() -> None:
    """``None`` (no assessment row -- either never run, or a legacy row
    that predates the concept entirely) is UNASSESSED and fail-closed
    ineligible -- no legacy-row grandfathering."""

    assert is_evidence_eligible_for_downstream_attribution(None) is False


def test_downstream_attribution_accepts_eligible() -> None:
    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    assessment = assess_followup_evidence_quality(
        observation=obs,
        baseline_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    assert is_evidence_eligible_for_downstream_attribution(assessment) is True


def test_downstream_attribution_rejects_ineligible() -> None:
    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    assessment = assess_followup_evidence_quality(
        observation=obs,
        baseline_evidence_class=EvidenceClass.FIXTURE,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert is_evidence_eligible_for_downstream_attribution(assessment) is False


def test_downstream_attribution_legacy_row_gets_no_transition_grandfathering() -> None:
    """The exact confirmed DOGE-USD shape: a legacy row (``baseline_
    evidence_class=None``, predates this milestone) with no assessment
    computed yet must be treated exactly like any other ``UNASSESSED`` row
    -- never silently admitted merely because it is old.  Mirrors ``domain.
    counterfactual_followup``'s own explicit transition-rule docstring:
    there is no separate legacy carve-out because none is needed."""

    legacy_shaped = replace(
        build_crypto_price_movement_observation(**_crypto_kwargs()),
        baseline_evidence_class=None,
        evidence_eligibility_policy_version=None,
    )
    # No assessment has been computed for this row yet -- exactly "legacy
    # row, nobody has run followup-evidence-quality-assess against it yet".
    assert is_evidence_eligible_for_downstream_attribution(None) is False
    # And even once assessed, only a genuine ELIGIBLE verdict passes -- the
    # legacy shape earns no special exemption from the same rule.
    assessment = assess_followup_evidence_quality(
        observation=legacy_shaped,
        baseline_evidence_class=EvidenceClass.FIXTURE,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    assert is_evidence_eligible_for_downstream_attribution(assessment) is False


# --------------------------------------------------------------------------
# assess_followup_evidence_quality -- the pure retroactive assessment
# builder (additive remediation for already-retained observations).
# --------------------------------------------------------------------------


def test_assess_evidence_quality_eligible_when_both_sides_allowlisted() -> None:
    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    assessment = assess_followup_evidence_quality(
        observation=obs,
        baseline_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    assert assessment.evidence_defect_type is FollowUpEvidenceDefectType.NONE
    assert assessment.observation_id == obs.observation_id
    assert assessment.observation_canonical_hash == obs.canonical_hash
    assert assessment.assessment_policy_version == FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1


def test_assess_evidence_quality_ineligible_when_baseline_is_fixture() -> None:
    """The exact confirmed DOGE-USD shape: a genuine target, a synthetic
    ``FIXTURE`` baseline."""

    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    assessment = assess_followup_evidence_quality(
        observation=obs,
        baseline_evidence_class=EvidenceClass.FIXTURE,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert (
        assessment.evidence_defect_type is FollowUpEvidenceDefectType.BASELINE_EVIDENCE_INELIGIBLE
    )


def test_assess_evidence_quality_ineligible_when_target_is_fixture() -> None:
    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    assessment = assess_followup_evidence_quality(
        observation=obs,
        baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        target_evidence_class=EvidenceClass.FIXTURE,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert assessment.evidence_defect_type is FollowUpEvidenceDefectType.TARGET_EVIDENCE_INELIGIBLE


def test_assess_evidence_quality_ineligible_when_both_sides_fixture() -> None:
    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    assessment = assess_followup_evidence_quality(
        observation=obs,
        baseline_evidence_class=EvidenceClass.FIXTURE,
        target_evidence_class=EvidenceClass.FIXTURE,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert (
        assessment.evidence_defect_type is FollowUpEvidenceDefectType.BOTH_SIDES_EVIDENCE_INELIGIBLE
    )


def test_assess_evidence_quality_fails_closed_when_lineage_unresolvable() -> None:
    """An unprovable lineage (e.g. a ``source_lineage`` id no longer
    resolving to any retained batch) must never be treated as eligible
    merely because it could not be checked."""

    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    assessment = assess_followup_evidence_quality(
        observation=obs,
        baseline_evidence_class=None,
        target_evidence_class=None,
        lineage_resolved=False,
        assessed_at=NOW,
    )
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert assessment.evidence_defect_type is FollowUpEvidenceDefectType.LINEAGE_NOT_RESOLVABLE


def test_assess_evidence_quality_kalshi_always_eligible() -> None:
    """KALSHI is always genuinely eligible -- this function never consults
    ``lineage_resolved``/evidence-class parameters for it."""

    obs = build_kalshi_market_status_observation(**_kalshi_kwargs())
    assessment = assess_followup_evidence_quality(
        observation=obs,
        baseline_evidence_class=None,
        target_evidence_class=None,
        lineage_resolved=False,
        assessed_at=NOW,
    )
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    assert assessment.evidence_defect_type is FollowUpEvidenceDefectType.NONE


def test_assess_evidence_quality_is_idempotent_across_replays() -> None:
    """Re-running the identical assessment policy against an unchanged
    observation always reproduces the identical ``assessment_id``/
    ``canonical_hash`` -- mirrors ``domain.counterfactual_classification``'s
    own idempotent-replay guarantee."""

    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    first = assess_followup_evidence_quality(
        observation=obs,
        baseline_evidence_class=EvidenceClass.FIXTURE,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    second = assess_followup_evidence_quality(
        observation=obs,
        baseline_evidence_class=EvidenceClass.FIXTURE,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW + timedelta(days=30),
    )
    assert first.assessment_id == second.assessment_id
    assert first.canonical_hash == second.canonical_hash
    assert first.assessed_at != second.assessed_at


def test_assess_evidence_quality_id_is_stable_across_known_policy_versions() -> None:
    # Securities Follow-Up Temporal Integrity v1 added a SECURITIES-only V2;
    # the all-venue baseline constant deliberately stays V1.
    assert KNOWN_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSIONS == (
        FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        "FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2",
    )
    assert CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION == FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1
    first = evidence_quality_assessment_id(
        observation_id="obs-1", assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1
    )
    second = evidence_quality_assessment_id(
        observation_id="obs-1", assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1
    )
    different_observation = evidence_quality_assessment_id(
        observation_id="obs-2", assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1
    )
    assert first == second
    assert first != different_observation


# --------------------------------------------------------------------------
# build_kalshi_market_status_observation
# --------------------------------------------------------------------------


def _kalshi_decision(**overrides: object) -> CounterfactualDecision:
    values: dict[str, object] = {
        "venue": SharedVenue.KALSHI,
        "subject_source_record_type": "ExhaustiveEventSetEvaluation",
        "subject_source_record_id": "kalshi-eval-1",
    }
    values.update(overrides)
    return _decision(**values)


def _kalshi_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "decision": _kalshi_decision(),
        "horizon_kind": FollowUpHorizonKind.SHORT_HORIZON,
        "horizon_days": 1,
        "target_time": NOW + timedelta(days=1),
        "subject_instrument_id": "KX-EVENT",
        "subject_product_or_market_id": "KX-EVENT:COMPLETE_SET",
        "market_status": "open",
        "yes_bid": Decimal("0.45"),
        "yes_ask": Decimal("0.48"),
        "settlement_result": None,
        "settlement_source": None,
        "source_endpoint_identity": "kalshi-public-event-followup-v1",
        "observed_at": NOW + timedelta(days=1),
    }
    values.update(overrides)
    return values


def test_build_kalshi_observation_prompt_read_has_no_lateness_limitation() -> None:
    obs = build_kalshi_market_status_observation(**_kalshi_kwargs())
    assert obs.market_status_at_target == "open"
    assert obs.data_limitations == ()
    assert obs.provenance is EvidenceClass.GENUINE_PUBLIC


def test_build_kalshi_observation_late_unsettled_read_is_flagged() -> None:
    """Part 5/6: a live read fetched well after the target time must not be
    mistaken for a faithful point-in-time answer unless it is a permanent
    settlement fact."""

    obs = build_kalshi_market_status_observation(
        **_kalshi_kwargs(
            target_time=NOW + timedelta(days=1),
            observed_at=NOW + timedelta(days=10),
            settlement_result=None,
        )
    )
    assert "LIVE_READ_AFTER_TARGET_TIME_NOT_A_POINT_IN_TIME_QUOTE" in obs.data_limitations


def test_build_kalshi_observation_late_settled_read_is_not_flagged() -> None:
    """Settlement is a permanent fact once true -- observing it late does not
    make it any less faithful (Part 8)."""

    obs = build_kalshi_market_status_observation(
        **_kalshi_kwargs(
            target_time=NOW + timedelta(days=1),
            observed_at=NOW + timedelta(days=10),
            market_status="settled",
            settlement_result="YES",
            settlement_source="KALSHI_PUBLIC_RESULT_FIELD",
        )
    )
    assert obs.data_limitations == ()
    assert obs.settlement_result == "YES"


def test_build_kalshi_observation_never_infers_settlement_from_price() -> None:
    """Part 8: settlement must come from genuine source truth, never be
    inferred -- proved by construction: the builder has no price-to-
    settlement logic at all, it only ever echoes ``settlement_result`` back."""

    obs = build_kalshi_market_status_observation(
        **_kalshi_kwargs(yes_bid=Decimal("0.99"), yes_ask=Decimal("0.99"), settlement_result=None)
    )
    assert obs.settlement_result is None


def test_build_kalshi_observation_no_crypto_fields_populated() -> None:
    obs = build_kalshi_market_status_observation(**_kalshi_kwargs())
    assert obs.reference_price_at_baseline is None
    assert obs.reference_price_at_target is None
    assert obs.subsequent_return is None


def test_build_kalshi_observation_baseline_kind_is_always_decision_time() -> None:
    """A Kalshi observation only ever has one structurally possible kind
    (Part: pre-deployment staleness-semantics audit; ``FollowUpBaselineKind``
    docstring) -- proved here regardless of which policy version is passed,
    not just asserted in the builder's own docstring."""

    v1 = build_kalshi_market_status_observation(**_kalshi_kwargs(policy_version=FOLLOWUP_POLICY_V1))
    v2 = build_kalshi_market_status_observation(**_kalshi_kwargs(policy_version=FOLLOWUP_POLICY_V2))
    assert v1.baseline_kind is FollowUpBaselineKind.DECISION_TIME
    assert v2.baseline_kind is FollowUpBaselineKind.DECISION_TIME
    assert v1.baseline_time == v1.decision_evaluated_at
    assert v2.baseline_time == v2.decision_evaluated_at


def test_build_kalshi_observation_requires_aware_timestamps() -> None:
    with pytest.raises(CounterfactualFollowUpError):
        build_kalshi_market_status_observation(
            **_kalshi_kwargs(observed_at=datetime(2026, 8, 13, 12, 0))  # naive
        )


def test_build_kalshi_observation_additional_limitations_pass_through() -> None:
    obs = build_kalshi_market_status_observation(
        **_kalshi_kwargs(
            additional_limitations=("MULTI_LEG_EVENT_PRICE_NOT_ATTRIBUTABLE_TO_ONE_MARKET",)
        )
    )
    assert "MULTI_LEG_EVENT_PRICE_NOT_ATTRIBUTABLE_TO_ONE_MARKET" in obs.data_limitations


# --------------------------------------------------------------------------
# Counterfactual Crypto Follow-Up Point-in-Time Semantics v2: policy
# versioning at the domain layer -- identity/coexistence/Kalshi
# non-regression.  See the "Application layer" section below for the
# hindsight-leakage regression itself (that lives in
# ``SyncFollowUpObservations._build_crypto``, not in these pure builders).
# --------------------------------------------------------------------------


def test_current_followup_policy_selection_is_per_venue_and_known() -> None:
    """Counterfactual Forward Outcome Temporal Integrity v1 (acceptance
    A17).  The former single global ``CURRENT_FOLLOWUP_POLICY_VERSION`` is
    gone; what replaces it is an explicit per-venue selection.  Every
    selected version must still be a *known* one, none may have silently
    regressed to the permanently-frozen V1, and the two venues whose
    follow-up behavior did not change must still be on exactly the version
    they were on before this milestone."""

    assert set(CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE) == set(SharedVenue)
    assert all(
        version in KNOWN_FOLLOWUP_POLICY_VERSIONS
        for version in CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE.values()
    )
    assert FOLLOWUP_POLICY_V1 not in CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE.values()
    assert current_followup_policy_version(SharedVenue.CRYPTO) == FOLLOWUP_POLICY_V3
    # Deliberately unchanged -- neither venue's follow-up behavior changed,
    # so neither is semantically relabelled.
    assert current_followup_policy_version(SharedVenue.KALSHI) == FOLLOWUP_POLICY_V2
    assert current_followup_policy_version(SharedVenue.SECURITIES) == FOLLOWUP_POLICY_V2
    assert CURRENT_FOLLOWUP_POLICY_VERSIONS == (FOLLOWUP_POLICY_V2, FOLLOWUP_POLICY_V3)
    assert current_followup_policy_venues(FOLLOWUP_POLICY_V3) == (SharedVenue.CRYPTO,)
    assert current_followup_policy_venues(FOLLOWUP_POLICY_V2) == (
        SharedVenue.KALSHI,
        SharedVenue.SECURITIES,
    )
    # An honest empty answer, not an error: V1 is still fully valid and
    # still explicitly requestable, it is simply not current for any venue.
    assert current_followup_policy_venues(FOLLOWUP_POLICY_V1) == ()


def test_followup_policy_v3_is_structurally_crypto_only() -> None:
    """Acceptance A13 (domain half).  Restated independently at the database
    by migration 0113's ``ck_decision_followup_v3_crypto_only``."""

    assert frozenset({SharedVenue.CRYPTO}) == FOLLOWUP_POLICY_V3_VENUES
    assert followup_policy_version_supports_venue(
        policy_version=FOLLOWUP_POLICY_V3, venue=SharedVenue.CRYPTO
    )
    assert not followup_policy_version_supports_venue(
        policy_version=FOLLOWUP_POLICY_V3, venue=SharedVenue.KALSHI
    )
    assert not followup_policy_version_supports_venue(
        policy_version=FOLLOWUP_POLICY_V3, venue=SharedVenue.SECURITIES
    )
    # Every earlier version stays valid for every venue, forever.
    for version in (FOLLOWUP_POLICY_V1, FOLLOWUP_POLICY_V2):
        assert all(
            followup_policy_version_supports_venue(policy_version=version, venue=venue)
            for venue in SharedVenue
        )


def test_followup_observation_id_differs_by_policy_version() -> None:
    v1_id = followup_observation_id(
        decision_id="d1",
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    v2_id = followup_observation_id(
        decision_id="d1",
        policy_version=FOLLOWUP_POLICY_V2,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    assert v1_id != v2_id


def test_build_crypto_observation_defaults_to_policy_v1() -> None:
    """Regression: adding the ``policy_version`` parameter must not change
    any existing call site's behavior -- an un-parametrized call reproduces
    FOLLOWUP_POLICY_V1 forever, by construction (Part: V1 immutability;
    "do NOT rewrite FOLLOWUP_POLICY_V1 semantics")."""

    obs = build_crypto_price_movement_observation(**_crypto_kwargs())
    assert obs.policy_version == FOLLOWUP_POLICY_V1


def test_build_crypto_observation_rejects_v1_with_origin_evidence_baseline_kind() -> None:
    """FOLLOWUP_POLICY_V1 never had a baseline-kind axis at all -- every V1
    row is, structurally, DECISION_TIME. Rejected at the source (this
    builder), not merely avoided by the application layer's own
    orchestration never constructing the combination."""

    with pytest.raises(CounterfactualFollowUpError):
        build_crypto_price_movement_observation(
            **_crypto_kwargs(
                policy_version=FOLLOWUP_POLICY_V1,
                baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
            )
        )


def test_build_kalshi_observation_defaults_to_policy_v1() -> None:
    obs = build_kalshi_market_status_observation(**_kalshi_kwargs())
    assert obs.policy_version == FOLLOWUP_POLICY_V1


def test_build_crypto_observation_v2_stamps_distinct_identity_from_v1() -> None:
    """Part: same decision_id remains stable; policy versions have distinct
    observation identities."""

    v1 = build_crypto_price_movement_observation(
        **_crypto_kwargs(policy_version=FOLLOWUP_POLICY_V1)
    )
    v2 = build_crypto_price_movement_observation(
        **_crypto_kwargs(policy_version=FOLLOWUP_POLICY_V2)
    )
    assert v1.policy_version == FOLLOWUP_POLICY_V1
    assert v2.policy_version == FOLLOWUP_POLICY_V2
    assert v1.observation_id != v2.observation_id
    assert v1.canonical_hash != v2.canonical_hash
    assert v1.decision_id == v2.decision_id
    assert v1.decision_canonical_hash == v2.decision_canonical_hash


def test_v1_and_v2_crypto_observations_coexist_in_repository_for_same_decision_and_horizon() -> (
    None
):
    """Proves Decision D/V1/SHORT_HORIZON and Decision D/V2/SHORT_HORIZON are
    separate immutable records that both point to the same stable decision
    event, with no identity conflict."""

    repository = _FakeRepository()
    v1 = build_crypto_price_movement_observation(
        **_crypto_kwargs(policy_version=FOLLOWUP_POLICY_V1)
    )
    v2 = build_crypto_price_movement_observation(
        **_crypto_kwargs(policy_version=FOLLOWUP_POLICY_V2)
    )
    assert repository.save_observation(observation=v1) is False  # genuine new write
    assert repository.save_observation(observation=v2) is False  # also a genuine new write
    assert repository.observation(v1.observation_id) == v1
    assert repository.observation(v2.observation_id) == v2


def test_build_kalshi_observation_v2_identical_to_v1_except_version_fields() -> None:
    """Part: Kalshi non-regression, proved with a test rather than merely
    asserted in a docstring -- every objective fact is byte-for-byte
    identical between policy versions; only policy_version/observation_id/
    canonical_hash (a pure function of policy_version) differ."""

    v1 = build_kalshi_market_status_observation(**_kalshi_kwargs(policy_version=FOLLOWUP_POLICY_V1))
    v2 = build_kalshi_market_status_observation(**_kalshi_kwargs(policy_version=FOLLOWUP_POLICY_V2))
    assert v1.policy_version == FOLLOWUP_POLICY_V1
    assert v2.policy_version == FOLLOWUP_POLICY_V2
    assert v1.observation_id != v2.observation_id
    assert v1.canonical_hash != v2.canonical_hash
    assert v1.decision_id == v2.decision_id
    assert v1.horizon_days == v2.horizon_days
    assert v1.target_time == v2.target_time
    assert v1.market_status_at_target == v2.market_status_at_target
    assert v1.yes_bid_at_target == v2.yes_bid_at_target
    assert v1.yes_ask_at_target == v2.yes_ask_at_target
    assert v1.settlement_result == v2.settlement_result
    assert v1.settlement_source == v2.settlement_source
    assert v1.data_limitations == v2.data_limitations
    assert v1.provenance == v2.provenance
    assert v1.reference_price_at_baseline is None
    assert v2.reference_price_at_baseline is None
    assert v1.baseline_kind is v2.baseline_kind is FollowUpBaselineKind.DECISION_TIME


# --------------------------------------------------------------------------
# Application layer: PlanFollowUpObservations / SyncFollowUpObservations
# against in-memory fakes.  No database, no network.
# --------------------------------------------------------------------------


class _FakeDecisionSource:
    def __init__(self, decisions: tuple[CounterfactualDecision, ...]) -> None:
        self._decisions = decisions

    def all_decisions(self) -> tuple[CounterfactualDecision, ...]:
        return self._decisions


class _FakeSubjectSource:
    def __init__(self, projections: dict[str, SharedOpportunityProjection]) -> None:
        self._projections = projections

    def projection(self, projection_id: str) -> SharedOpportunityProjection | None:
        return self._projections.get(projection_id)


class _FakeCryptoPriceSource:
    def __init__(
        self,
        closes: dict[tuple[str, datetime], CryptoPriceLookup],
        completed_at: dict[tuple[str, datetime], datetime] | None = None,
    ) -> None:
        self._closes = closes
        # Counterfactual Forward Outcome Temporal Integrity v1: when Apex
        # genuinely came to possess each seeded bucket.  Optional and empty
        # by default so every pre-existing test is unchanged (an unmapped
        # bucket is treated as always-available); a test that cares about
        # the availability bound supplies it explicitly.
        self._completed_at = completed_at or {}
        self.measurement_as_of_calls: list[datetime] = []
        self.call_count = 0
        # (method_name, product_id, timestamp) per call -- lets tests assert
        # *which* query FOLLOWUP_POLICY_V1/V2 actually issued, not just how
        # many times something was called.
        self.calls: list[tuple[str, str, datetime]] = []

    def daily_close(self, *, product_id: str, bucket_start: datetime) -> CryptoPriceLookup | None:
        self.call_count += 1
        self.calls.append(("daily_close", product_id, bucket_start))
        return self._closes.get((product_id, bucket_start))

    def daily_close_as_of(
        self, *, product_id: str, bucket_start: datetime, measurement_as_of: datetime
    ) -> CryptoPriceLookup | None:
        """Real-shaped fake: mirrors ``adapters.counterfactual_followup_
        postgres._CryptoFollowUpPriceSource.daily_close_as_of`` -- an exact
        bucket match, never a nearest-available substitution.  Availability
        (``completed_at <= measurement_as_of``) is modelled by
        ``_FakeCryptoPriceSource``'s optional ``completed_at`` map rather
        than by re-deriving it from ``bucket_start``, exactly as the real
        implementation refuses to."""

        self.call_count += 1
        self.calls.append(("daily_close_as_of", product_id, bucket_start))
        self.measurement_as_of_calls.append(measurement_as_of)
        lookup = self._closes.get((product_id, bucket_start))
        if lookup is None:
            return None
        completed_at = self._completed_at.get((product_id, bucket_start))
        if completed_at is not None and completed_at > measurement_as_of:
            return None
        return lookup

    def latest_close_before(self, *, product_id: str, before: datetime) -> CryptoPriceLookup | None:
        """Real-shaped fake: mirrors ``adapters.counterfactual_followup_postgres.
        _CryptoFollowUpPriceSource.latest_close_before`` exactly -- latest
        retained bucket strictly before ``before``, never at or after it."""

        self.call_count += 1
        self.calls.append(("latest_close_before", product_id, before))
        candidates = [
            (bucket, lookup)
            for (candidate_product, bucket), lookup in self._closes.items()
            if candidate_product == product_id and bucket < before
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]


class _FakeKalshiSource:
    def __init__(
        self,
        reads: dict[str, KalshiMarketRead] | None = None,
        error: str | None = None,
        error_request_count: int = 2,
    ) -> None:
        self._reads = reads or {}
        self._error = error
        self._error_request_count = error_request_count
        self.call_count = 0

    def read_market_status(
        self,
        *,
        subject_source_record_type: str,
        instrument_id: str,
        product_or_market_id: str,
        timeout_seconds: int,
    ) -> KalshiMarketRead:
        self.call_count += 1
        if self._error is not None:
            raise FollowUpSourceUnavailable(self._error, request_count=self._error_request_count)
        return self._reads[instrument_id]


class _FakeRepository:
    def __init__(self) -> None:
        self._rows: dict[str, object] = {}
        self._cursors: dict[str, FollowUpScanPosition] = {}

    def observation(self, observation_id: str):  # type: ignore[no-untyped-def]
        return self._rows.get(observation_id)

    def save_observation(self, *, observation) -> bool:  # type: ignore[no-untyped-def]
        existing = self._rows.get(observation.observation_id)
        if existing is not None:
            if existing.canonical_hash != observation.canonical_hash:
                raise CounterfactualFollowUpError("FAKE_REPOSITORY_CONFLICT")
            return True
        self._rows[observation.observation_id] = observation
        return False

    def scan_position(self, *, policy_version: str) -> FollowUpScanPosition | None:
        return self._cursors.get(policy_version)

    def save_scan_position(self, *, policy_version: str, position: FollowUpScanPosition) -> None:
        self._cursors[policy_version] = position


def test_plan_reports_due_not_yet_due_and_unsupported_without_writing_or_fetching() -> None:
    crypto_decision = _decision(decision_id="d-crypto", evaluated_at=NOW - timedelta(days=40))
    kalshi_decision = _kalshi_decision(
        decision_id="d-kalshi", evaluated_at=NOW - timedelta(hours=2)
    )
    decisions = _FakeDecisionSource((crypto_decision, kalshi_decision))
    subjects = _FakeSubjectSource(
        {
            "proj-1": _projection(expected_holding_days=30),
        }
    )
    plan = PlanFollowUpObservations(decisions, subjects, _FakeCryptoPriceSource({})).execute(
        as_of=NOW
    )
    # crypto decision: SHORT_HORIZON due (40 days > 1), STRATEGY_HORIZON due (40 days > 30)
    # kalshi decision: subject_projection_id "proj-1" resolves for both (shared fixture);
    # SHORT_HORIZON not yet due (2 hours < 1 day), STRATEGY_HORIZON not yet due (2h < 30d)
    assert plan.considered_decisions == 2
    assert plan.considered_pairs == 4
    counts = dict(plan.state_counts)
    assert counts.get("DUE", 0) == 2
    assert counts.get("NOT_YET_DUE", 0) == 2


def test_plan_reports_unsupported_when_subject_missing_expected_holding_days() -> None:
    decision = _decision(evaluated_at=NOW - timedelta(days=1))
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": _projection(expected_holding_days=None)})
    plan = PlanFollowUpObservations(decisions, subjects, _FakeCryptoPriceSource({})).execute(
        as_of=NOW
    )
    counts = dict(plan.state_counts)
    assert counts.get("UNSUPPORTED", 0) == 1
    assert counts.get("DUE", 0) == 1  # the SHORT_HORIZON pair


def test_plan_reports_origin_evidence_unsupported_when_no_projection_retained() -> None:
    """Pre-deployment staleness-semantics audit: an ORIGIN_EVIDENCE pair has
    no ``generated_at`` to anchor to at all when the subject projection
    itself was never retained -- reported as UNSUPPORTED, never a crash.
    DECISION_TIME is unaffected (its own anchor, decision.evaluated_at, is
    always available)."""

    decision = _decision(evaluated_at=NOW - timedelta(days=40))
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({})  # "proj-1" never resolves
    plan = PlanFollowUpObservations(decisions, subjects, _FakeCryptoPriceSource({})).execute(
        as_of=NOW, policy_version=FOLLOWUP_POLICY_V2
    )
    # SHORT_HORIZON/DECISION_TIME: DUE. SHORT_HORIZON/ORIGIN_EVIDENCE: UNSUPPORTED
    # (no anchor). STRATEGY_HORIZON/{ORIGIN_EVIDENCE,DECISION_TIME}: both
    # UNSUPPORTED (no expected_holding_days -- also unreachable via projection).
    assert plan.considered_pairs == 4
    counts = dict(plan.state_counts)
    assert counts.get("DUE", 0) == 1
    assert counts.get("UNSUPPORTED", 0) == 3
    origin_evidence_entries = [
        entry
        for entry in plan.entries
        if entry.baseline_kind is FollowUpBaselineKind.ORIGIN_EVIDENCE
    ]
    assert len(origin_evidence_entries) == 2
    assert all(entry.state is FollowUpDueState.UNSUPPORTED for entry in origin_evidence_entries)
    short_horizon_origin_evidence = next(
        entry
        for entry in origin_evidence_entries
        if entry.horizon_kind is FollowUpHorizonKind.SHORT_HORIZON
    )
    assert "HORIZON_ANCHOR_NOT_AVAILABLE" in short_horizon_origin_evidence.reasons


def test_plan_baseline_kinds_crypto_gets_both_kalshi_always_decision_time_under_v2() -> None:
    crypto_decision = _decision(decision_id="d-crypto", evaluated_at=NOW - timedelta(days=40))
    kalshi_decision = _kalshi_decision(
        decision_id="d-kalshi", evaluated_at=NOW - timedelta(days=40)
    )
    decisions = _FakeDecisionSource((crypto_decision, kalshi_decision))
    subjects = _FakeSubjectSource(
        {"proj-1": _projection(expected_holding_days=None, generated_at=NOW - timedelta(days=40))}
    )
    plan = PlanFollowUpObservations(decisions, subjects, _FakeCryptoPriceSource({})).execute(
        as_of=NOW, policy_version=FOLLOWUP_POLICY_V2
    )
    crypto_baseline_kinds = {
        entry.baseline_kind for entry in plan.entries if entry.venue is SharedVenue.CRYPTO
    }
    kalshi_baseline_kinds = {
        entry.baseline_kind for entry in plan.entries if entry.venue is SharedVenue.KALSHI
    }
    assert crypto_baseline_kinds == {
        FollowUpBaselineKind.ORIGIN_EVIDENCE,
        FollowUpBaselineKind.DECISION_TIME,
    }
    assert kalshi_baseline_kinds == {FollowUpBaselineKind.DECISION_TIME}


def test_sync_creates_crypto_observation_from_local_retained_bars_only() -> None:
    decision = _decision(evaluated_at=NOW - timedelta(days=40))
    projection = _projection(expected_holding_days=30)
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    decision_bucket = (decision.evaluated_at).replace(hour=0, minute=0, second=0, microsecond=0)
    closes = {
        ("BTC-USD", decision_bucket): CryptoPriceLookup(
            bucket_start=decision_bucket,
            close=Decimal("100"),
            source_observation_id="c1",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        ("BTC-USD", decision_bucket + timedelta(days=1)): CryptoPriceLookup(
            bucket_start=decision_bucket + timedelta(days=1),
            close=Decimal("101"),
            source_observation_id="c2",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        ("BTC-USD", decision_bucket + timedelta(days=30)): CryptoPriceLookup(
            bucket_start=decision_bucket + timedelta(days=30),
            close=Decimal("150"),
            source_observation_id="c3",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
    }
    crypto_prices = _FakeCryptoPriceSource(closes)
    kalshi_source = _FakeKalshiSource(error="SHOULD_NEVER_BE_CALLED_FOR_CRYPTO")
    repository = _FakeRepository()
    result = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, kalshi_source, repository
    ).execute(as_of=NOW, sample_limit=10)
    assert result.created_count == 2  # SHORT_HORIZON + STRATEGY_HORIZON
    assert result.provider_request_count == 0
    assert kalshi_source.call_count == 0


def test_sync_is_idempotent_on_replay_and_makes_no_second_provider_request() -> None:
    decision = _kalshi_decision(evaluated_at=NOW - timedelta(days=2))
    projection = _projection(
        venue=SharedVenue.KALSHI,
        instrument_id="KX-EVENT",
        product_or_market_id="KX-EVENT:COMPLETE_SET",
        expected_holding_days=None,
    )
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    crypto_prices = _FakeCryptoPriceSource({})
    kalshi_source = _FakeKalshiSource(
        reads={
            "KX-EVENT": KalshiMarketRead(
                market_status="open",
                yes_bid=Decimal("0.4"),
                yes_ask=Decimal("0.45"),
                settlement_result=None,
                settlement_source=None,
                source_endpoint_identity="kalshi-public-event-followup-v1",
                request_count=1,
            )
        }
    )
    repository = _FakeRepository()
    first = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, kalshi_source, repository
    ).execute(as_of=NOW, sample_limit=10)
    assert first.created_count == 1  # only SHORT_HORIZON is supported (no expected_holding_days)
    assert kalshi_source.call_count == 1
    second = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, kalshi_source, repository
    ).execute(as_of=NOW + timedelta(hours=1), sample_limit=10)
    assert second.created_count == 0
    assert second.skipped_already_observed_count == 1
    # the already-OBSERVED horizon must never be re-fetched from the provider
    assert kalshi_source.call_count == 1


# ---------------------------------------------------------------------------
# Kalshi Settlement & Resolution Evidence Maturation v1: the authoritative-
# gating rule applied through the real orchestration path
# (``SyncFollowUpObservations._read_kalshi_with_resolution_gate``), not just
# the pure builder unit tests above.
# ---------------------------------------------------------------------------


def _single_market_kalshi_decision(**overrides: object) -> CounterfactualDecision:
    values: dict[str, object] = {"subject_source_record_type": "BinaryComplementEvaluation"}
    values.update(overrides)
    return _kalshi_decision(**values)


def test_kalshi_sync_never_trusts_a_provisional_result_even_when_provider_supplies_one() -> None:
    """Governing Constraint, end to end: a ``determined`` (not yet
    ``finalized``) read that already carries a raw ``result`` must still
    produce ``settlement_result=None`` on the retained observation -- the
    exact regression ``_settlement_result``'s previous, ungated behavior
    risked."""

    decision = _single_market_kalshi_decision(evaluated_at=NOW - timedelta(days=2))
    projection = _projection(
        venue=SharedVenue.KALSHI,
        instrument_id="KX-MARKET-1",
        product_or_market_id="KX-MARKET-1",
        expected_holding_days=None,
    )
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    kalshi_source = _FakeKalshiSource(
        reads={
            "KX-MARKET-1": KalshiMarketRead(
                market_status="determined",
                yes_bid=None,
                yes_ask=None,
                settlement_result="YES",
                settlement_source="KALSHI_PUBLIC_RESULT_FIELD",
                source_endpoint_identity="kalshi-public-market-followup-v1",
                request_count=1,
                market_ticker="KX-MARKET-1",
                event_ticker=None,
                settlement_value_dollars=None,
                settlement_ts=None,
                close_time=NOW - timedelta(days=2),
            )
        }
    )
    resolution_facts = InMemoryKalshiResolutionFactRepository()
    result = SyncFollowUpObservations(
        decisions,
        subjects,
        _FakeCryptoPriceSource({}),
        kalshi_source,
        _FakeRepository(),
        resolution_facts=resolution_facts,
    ).execute(as_of=NOW, sample_limit=10)
    assert result.created_count == 1
    observation = next(iter(result.entries)).observation_id
    assert observation is not None
    # A determined-but-not-finalized fact is still persisted (it is genuine
    # evidence -- just not authoritative), but carries no resolved result.
    fact = resolution_facts.latest_fact(market_ticker="KX-MARKET-1", as_of=NOW)
    assert fact is not None
    assert fact.resolution_state is KalshiResolutionState.RESULT_DETERMINED
    assert fact.resolved_result is None
    assert fact.raw_result == "YES"


def test_kalshi_sync_passes_through_finalized_settlement_result() -> None:
    decision = _single_market_kalshi_decision(evaluated_at=NOW - timedelta(days=2))
    projection = _projection(
        venue=SharedVenue.KALSHI,
        instrument_id="KX-MARKET-2",
        product_or_market_id="KX-MARKET-2",
        expected_holding_days=None,
    )
    subjects = _FakeSubjectSource({"proj-1": projection})
    kalshi_source = _FakeKalshiSource(
        reads={
            "KX-MARKET-2": KalshiMarketRead(
                market_status="finalized",
                yes_bid=None,
                yes_ask=None,
                settlement_result="YES",
                settlement_source="KALSHI_PUBLIC_RESULT_FIELD",
                source_endpoint_identity="kalshi-public-market-followup-v1",
                request_count=1,
                market_ticker="KX-MARKET-2",
                event_ticker=None,
                settlement_value_dollars=Decimal("1.0000"),
                settlement_ts=NOW - timedelta(days=1),
                close_time=NOW - timedelta(days=2),
            )
        }
    )
    resolution_facts = InMemoryKalshiResolutionFactRepository()
    result = SyncFollowUpObservations(
        _FakeDecisionSource((decision,)),
        subjects,
        _FakeCryptoPriceSource({}),
        kalshi_source,
        _FakeRepository(),
        resolution_facts=resolution_facts,
    ).execute(as_of=NOW, sample_limit=10)
    assert result.created_count == 1
    fact = resolution_facts.latest_fact(market_ticker="KX-MARKET-2", as_of=NOW)
    assert fact is not None
    assert fact.resolution_state is KalshiResolutionState.FINALIZED
    assert fact.resolved_result == "YES"


def test_kalshi_sync_skips_live_request_when_already_finalized_and_cached() -> None:
    """Avoid hammering the provider (Part 6): once a market's resolution
    fact is already retained and FINALIZED as of this cycle's ``as_of``, a
    fresh sync must answer entirely from retained evidence -- the injected
    Kalshi source must never be called at all."""

    decision = _single_market_kalshi_decision(evaluated_at=NOW - timedelta(days=2))
    projection = _projection(
        venue=SharedVenue.KALSHI,
        instrument_id="KX-MARKET-3",
        product_or_market_id="KX-MARKET-3",
        expected_holding_days=None,
    )
    subjects = _FakeSubjectSource({"proj-1": projection})
    resolution_facts = InMemoryKalshiResolutionFactRepository()
    pre_seeded = build_kalshi_resolution_fact(
        market_ticker="KX-MARKET-3",
        event_ticker=None,
        provider_status="finalized",
        raw_result="no",
        settlement_value_dollars="1.0000",
        settlement_ts=NOW - timedelta(days=1),
        close_time=NOW - timedelta(days=2),
        source_endpoint_identity="kalshi-public-market-followup-v1",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        captured_at=NOW - timedelta(hours=1),
        run_id="pre-seeded-run",
    )
    resolution_facts.record(fact=pre_seeded)
    kalshi_source = _FakeKalshiSource(error="SHOULD_NEVER_BE_CALLED_WHEN_ALREADY_FINALIZED")
    result = SyncFollowUpObservations(
        _FakeDecisionSource((decision,)),
        subjects,
        _FakeCryptoPriceSource({}),
        kalshi_source,
        _FakeRepository(),
        resolution_facts=resolution_facts,
    ).execute(as_of=NOW, sample_limit=10)
    assert result.created_count == 1
    assert kalshi_source.call_count == 0
    assert result.provider_request_count == 0


def test_kalshi_sync_cached_finalized_fact_with_no_result_stamps_no_settlement_source() -> None:
    """A FINALIZED fact can still legitimately carry no result (the
    provider genuinely never sent one) -- the cache-hit branch must never
    stamp a settlement source for a result that isn't actually there,
    mirroring the live-read branch and the established adapter
    convention exactly."""

    decision = _single_market_kalshi_decision(evaluated_at=NOW - timedelta(days=2))
    projection = _projection(
        venue=SharedVenue.KALSHI,
        instrument_id="KX-MARKET-NO-RESULT",
        product_or_market_id="KX-MARKET-NO-RESULT",
        expected_holding_days=None,
    )
    subjects = _FakeSubjectSource({"proj-1": projection})
    resolution_facts = InMemoryKalshiResolutionFactRepository()
    pre_seeded = build_kalshi_resolution_fact(
        market_ticker="KX-MARKET-NO-RESULT",
        event_ticker=None,
        provider_status="finalized",
        raw_result=None,
        settlement_value_dollars=None,
        settlement_ts=None,
        close_time=NOW - timedelta(days=2),
        source_endpoint_identity="kalshi-public-market-followup-v1",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        captured_at=NOW - timedelta(hours=1),
        run_id="pre-seeded-run",
    )
    resolution_facts.record(fact=pre_seeded)
    kalshi_source = _FakeKalshiSource(error="SHOULD_NEVER_BE_CALLED_WHEN_ALREADY_FINALIZED")
    repository = _FakeRepository()
    result = SyncFollowUpObservations(
        _FakeDecisionSource((decision,)),
        subjects,
        _FakeCryptoPriceSource({}),
        kalshi_source,
        repository,
        resolution_facts=resolution_facts,
    ).execute(as_of=NOW, sample_limit=10)
    assert result.created_count == 1
    assert kalshi_source.call_count == 0
    observation_id = next(iter(result.entries)).observation_id
    assert observation_id is not None
    observation = repository.observation(observation_id)
    assert observation is not None
    assert observation.settlement_result is None
    assert observation.settlement_source is None


def test_kalshi_sync_multi_leg_read_is_unaffected_by_resolution_gate() -> None:
    """A multi-leg event read (no single ``market_ticker``) has no ticker to
    key a resolution fact by -- it must flow through exactly as before this
    milestone, and must never persist a resolution fact."""

    decision = _kalshi_decision(
        evaluated_at=NOW - timedelta(days=2), subject_source_record_type="ThresholdBundleEvaluation"
    )
    projection = _projection(
        venue=SharedVenue.KALSHI,
        instrument_id="KX-EVENT-MULTI",
        product_or_market_id="KX-A+KX-B",
        expected_holding_days=None,
    )
    subjects = _FakeSubjectSource({"proj-1": projection})
    kalshi_source = _FakeKalshiSource(
        reads={
            "KX-EVENT-MULTI": KalshiMarketRead(
                market_status="MIXED:finalized,active",
                yes_bid=None,
                yes_ask=None,
                settlement_result="KX-A=YES",
                settlement_source="KALSHI_PUBLIC_RESULT_FIELD",
                source_endpoint_identity="kalshi-public-event-followup-v1",
                request_count=1,
                source_limitations=("MULTI_LEG_EVENT_PRICE_NOT_ATTRIBUTABLE_TO_ONE_MARKET",),
                # market_ticker deliberately left at its default (``None``):
                # this is the real shape ``_read_event``'s multi-leg branch
                # produces.
            )
        }
    )
    resolution_facts = InMemoryKalshiResolutionFactRepository()
    result = SyncFollowUpObservations(
        _FakeDecisionSource((decision,)),
        subjects,
        _FakeCryptoPriceSource({}),
        kalshi_source,
        _FakeRepository(),
        resolution_facts=resolution_facts,
    ).execute(as_of=NOW, sample_limit=10)
    assert result.created_count == 1
    assert resolution_facts.facts_for_ticker(market_ticker="KX-EVENT-MULTI") == ()


def test_kalshi_sync_second_decision_on_same_ticker_reuses_cached_finalized_fact() -> None:
    """Demonstrates the design's core value proposition concretely: two
    different decisions referencing the identical Kalshi market only ever
    cost one genuine live request between them."""

    first_decision = _single_market_kalshi_decision(
        decision_id="d-1", subject_projection_id="proj-1", evaluated_at=NOW - timedelta(days=5)
    )
    second_decision = _single_market_kalshi_decision(
        decision_id="d-2", subject_projection_id="proj-2", evaluated_at=NOW - timedelta(days=2)
    )
    projection_1 = _projection(
        projection_id="proj-1",
        venue=SharedVenue.KALSHI,
        instrument_id="KX-SHARED",
        product_or_market_id="KX-SHARED",
        expected_holding_days=None,
    )
    projection_2 = replace(projection_1, projection_id="proj-2")
    subjects = _FakeSubjectSource({"proj-1": projection_1, "proj-2": projection_2})
    kalshi_source = _FakeKalshiSource(
        reads={
            "KX-SHARED": KalshiMarketRead(
                market_status="finalized",
                yes_bid=None,
                yes_ask=None,
                settlement_result="NO",
                settlement_source="KALSHI_PUBLIC_RESULT_FIELD",
                source_endpoint_identity="kalshi-public-market-followup-v1",
                request_count=1,
                market_ticker="KX-SHARED",
                event_ticker=None,
                settlement_value_dollars=Decimal("0.0000"),
                settlement_ts=NOW - timedelta(days=1),
                close_time=NOW - timedelta(days=5),
            )
        }
    )
    resolution_facts = InMemoryKalshiResolutionFactRepository()
    result = SyncFollowUpObservations(
        _FakeDecisionSource((first_decision, second_decision)),
        subjects,
        _FakeCryptoPriceSource({}),
        kalshi_source,
        _FakeRepository(),
        resolution_facts=resolution_facts,
    ).execute(as_of=NOW, sample_limit=10)
    assert result.created_count == 2
    # Exactly one genuine live request served both decisions.
    assert kalshi_source.call_count == 1
    assert result.provider_request_count == 1


def test_kalshi_sync_point_in_time_future_capture_cannot_leak_into_earlier_evaluation() -> None:
    """Part 9, end to end: a resolution fact genuinely captured after the
    current cycle's own ``as_of`` (e.g. by a later, independent cycle
    replaying an earlier one, or a clock/ordering edge case) must not be
    surfaced -- the live source must still be consulted, and its own
    (earlier, non-terminal) answer must be what actually gets used."""

    decision = _single_market_kalshi_decision(evaluated_at=NOW - timedelta(days=2))
    projection = _projection(
        venue=SharedVenue.KALSHI,
        instrument_id="KX-MARKET-4",
        product_or_market_id="KX-MARKET-4",
        expected_holding_days=None,
    )
    subjects = _FakeSubjectSource({"proj-1": projection})
    resolution_facts = InMemoryKalshiResolutionFactRepository()
    future_finalized = build_kalshi_resolution_fact(
        market_ticker="KX-MARKET-4",
        event_ticker=None,
        provider_status="finalized",
        raw_result="yes",
        settlement_value_dollars="1.0000",
        settlement_ts=NOW + timedelta(hours=2),
        close_time=NOW - timedelta(days=2),
        source_endpoint_identity="kalshi-public-market-followup-v1",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        captured_at=NOW + timedelta(hours=1),  # captured strictly after this cycle's as_of
        run_id="later-cycle-run",
    )
    resolution_facts.record(fact=future_finalized)
    kalshi_source = _FakeKalshiSource(
        reads={
            "KX-MARKET-4": KalshiMarketRead(
                market_status="closed",
                yes_bid=None,
                yes_ask=None,
                settlement_result=None,
                settlement_source=None,
                source_endpoint_identity="kalshi-public-market-followup-v1",
                request_count=1,
                market_ticker="KX-MARKET-4",
                event_ticker=None,
                settlement_value_dollars=None,
                settlement_ts=None,
                close_time=NOW - timedelta(days=2),
            )
        }
    )
    result = SyncFollowUpObservations(
        _FakeDecisionSource((decision,)),
        subjects,
        _FakeCryptoPriceSource({}),
        kalshi_source,
        _FakeRepository(),
        resolution_facts=resolution_facts,
    ).execute(as_of=NOW, sample_limit=10)
    assert result.created_count == 1
    # The live source WAS consulted (the future fact could not answer this
    # cycle's as_of-bounded lookup) ...
    assert kalshi_source.call_count == 1
    # ... and a genuinely new, still-non-terminal fact was persisted at
    # this cycle's own as_of, sitting *alongside* (never overwriting) the
    # later-captured finalized one.
    still_visible = resolution_facts.latest_fact(market_ticker="KX-MARKET-4", as_of=NOW)
    assert still_visible is not None
    assert still_visible.resolution_state is KalshiResolutionState.CLOSED_AWAITING_RESULT
    assert still_visible.resolved_result is None
    # The future fact remains retained, untouched, and becomes visible again
    # to a later evaluation whose own as_of has genuinely passed it.
    later_evaluation = resolution_facts.latest_fact(
        market_ticker="KX-MARKET-4", as_of=NOW + timedelta(hours=3)
    )
    assert later_evaluation is not None
    assert later_evaluation.resolution_fact_id == future_finalized.resolution_fact_id


def test_sync_respects_bounded_sample_limit() -> None:
    decisions_list = tuple(
        _decision(
            decision_id=f"d-{i}",
            subject_projection_id=f"proj-{i}",
            evaluated_at=NOW - timedelta(days=40),
        )
        for i in range(5)
    )
    projections = {
        f"proj-{i}": _projection(projection_id=f"proj-{i}", expected_holding_days=30)
        for i in range(5)
    }
    decisions = _FakeDecisionSource(decisions_list)
    subjects = _FakeSubjectSource(projections)
    decision_bucket = (NOW - timedelta(days=40)).replace(hour=0, minute=0, second=0, microsecond=0)
    closes = {
        ("BTC-USD", decision_bucket): CryptoPriceLookup(
            bucket_start=decision_bucket,
            close=Decimal("100"),
            source_observation_id="c1",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        ("BTC-USD", decision_bucket + timedelta(days=1)): CryptoPriceLookup(
            bucket_start=decision_bucket + timedelta(days=1),
            close=Decimal("101"),
            source_observation_id="c2",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        ("BTC-USD", decision_bucket + timedelta(days=30)): CryptoPriceLookup(
            bucket_start=decision_bucket + timedelta(days=30),
            close=Decimal("150"),
            source_observation_id="c3",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
    }
    crypto_prices = _FakeCryptoPriceSource(closes)
    kalshi_source = _FakeKalshiSource(error="unused")
    repository = _FakeRepository()
    result = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, kalshi_source, repository
    ).execute(as_of=NOW, sample_limit=3)
    assert result.genuine_attempts == 3
    assert result.created_count == 3


def test_sync_reports_blocked_unavailable_when_crypto_bucket_not_retained() -> None:
    decision = _decision(evaluated_at=NOW - timedelta(days=2))
    projection = _projection(expected_holding_days=None)
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    crypto_prices = _FakeCryptoPriceSource({})  # nothing retained
    kalshi_source = _FakeKalshiSource(error="unused")
    repository = _FakeRepository()
    result = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, kalshi_source, repository
    ).execute(as_of=NOW, sample_limit=10)
    assert result.blocked_count == 1
    assert result.created_count == 0
    assert result.entries[0].outcome == "BLOCKED_UNAVAILABLE"
    assert result.entries[0].reasons


def test_sync_reports_blocked_unavailable_when_kalshi_source_fails() -> None:
    decision = _kalshi_decision(evaluated_at=NOW - timedelta(days=2))
    projection = _projection(
        venue=SharedVenue.KALSHI,
        instrument_id="KX-EVENT",
        product_or_market_id="KX-EVENT:COMPLETE_SET",
        expected_holding_days=None,
    )
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    crypto_prices = _FakeCryptoPriceSource({})
    kalshi_source = _FakeKalshiSource(error="KALSHI_PUBLIC_READ_FAILED simulated")
    repository = _FakeRepository()
    result = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, kalshi_source, repository
    ).execute(as_of=NOW, sample_limit=10)
    assert result.blocked_count == 1
    # a genuine failure still discloses how many bounded requests were
    # actually made before giving up (Part 16) -- not just "1 per attempt".
    assert result.provider_request_count == 2


@pytest.mark.parametrize(
    "disposition",
    [
        DecisionDisposition.ADVANCED,
        DecisionDisposition.CONSTRAINED,
        DecisionDisposition.NOT_ADVANCED,
    ],
)
def test_sync_supports_every_disposition_cohort(disposition: DecisionDisposition) -> None:
    """Part 3: survivor-bias control -- the sync path itself never filters
    by disposition."""

    decision = _decision(evaluated_at=NOW - timedelta(days=2), disposition=disposition)
    projection = _projection(expected_holding_days=None)
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    decision_bucket = decision.evaluated_at.replace(hour=0, minute=0, second=0, microsecond=0)
    closes = {
        ("BTC-USD", decision_bucket): CryptoPriceLookup(
            bucket_start=decision_bucket,
            close=Decimal("100"),
            source_observation_id="c1",
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
        ),
        ("BTC-USD", decision_bucket + timedelta(days=1)): CryptoPriceLookup(
            bucket_start=decision_bucket + timedelta(days=1),
            close=Decimal("102"),
            source_observation_id="c2",
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
        ),
    }
    crypto_prices = _FakeCryptoPriceSource(closes)
    kalshi_source = _FakeKalshiSource(error="unused")
    repository = _FakeRepository()
    result = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, kalshi_source, repository
    ).execute(as_of=NOW, sample_limit=10)
    assert result.created_count == 1
    assert result.entries[0].outcome == "CREATED"


def test_sync_rejects_negative_sample_limit() -> None:
    decisions = _FakeDecisionSource(())
    subjects = _FakeSubjectSource({})
    with pytest.raises(ValueError):
        SyncFollowUpObservations(
            decisions, subjects, _FakeCryptoPriceSource({}), _FakeKalshiSource(), _FakeRepository()
        ).execute(as_of=NOW, sample_limit=-1)


# --------------------------------------------------------------------------
# Counterfactual Crypto Follow-Up Point-in-Time Semantics v2: the
# hindsight-leakage regression itself, plus the application-layer
# coexistence/backlog/idempotency/non-regression proofs that depend on it.
# --------------------------------------------------------------------------


def test_sync_rejects_unknown_policy_version() -> None:
    decisions = _FakeDecisionSource(())
    subjects = _FakeSubjectSource({})
    with pytest.raises(CounterfactualFollowUpError):
        SyncFollowUpObservations(
            decisions, subjects, _FakeCryptoPriceSource({}), _FakeKalshiSource(), _FakeRepository()
        ).execute(as_of=NOW, policy_version="FOLLOWUP_POLICY_DOES_NOT_EXIST")


def test_plan_rejects_unknown_policy_version() -> None:
    decisions = _FakeDecisionSource(())
    subjects = _FakeSubjectSource({})
    with pytest.raises(CounterfactualFollowUpError):
        PlanFollowUpObservations(decisions, subjects, _FakeCryptoPriceSource({})).execute(
            as_of=NOW, policy_version="FOLLOWUP_POLICY_DOES_NOT_EXIST"
        )


def test_plan_considered_pairs_is_per_policy_version_not_cumulative() -> None:
    """Must not report decisions x policy as though it were more decisions:
    running the identical plan under two different policy versions each
    independently reports the same, unchanged considered_decisions/
    considered_pairs -- nothing sums them together."""

    decisions_list = tuple(
        _decision(
            decision_id=f"d-{i}",
            subject_projection_id=f"proj-{i}",
            evaluated_at=NOW - timedelta(days=40),
        )
        for i in range(3)
    )
    projections = {
        f"proj-{i}": _projection(projection_id=f"proj-{i}", expected_holding_days=30)
        for i in range(3)
    }
    decisions = _FakeDecisionSource(decisions_list)
    subjects = _FakeSubjectSource(projections)
    crypto_prices = _FakeCryptoPriceSource({})
    plan_v1 = PlanFollowUpObservations(decisions, subjects, crypto_prices).execute(
        as_of=NOW, policy_version=FOLLOWUP_POLICY_V1
    )
    plan_v2 = PlanFollowUpObservations(decisions, subjects, crypto_prices).execute(
        as_of=NOW, policy_version=FOLLOWUP_POLICY_V2
    )
    assert plan_v1.considered_decisions == plan_v2.considered_decisions == 3
    # V1 never had a baseline-kind axis: 3 decisions x 2 horizon kinds = 6,
    # unchanged forever. V2 CRYPTO gets both FollowUpBaselineKind values
    # (Part: pre-deployment staleness-semantics audit): 3 decisions x 2
    # horizon kinds x 2 baseline kinds = 12 -- genuinely more considered
    # pairs, not a double-count of the same question; each pair answers a
    # materially different research question (see FollowUpBaselineKind).
    assert plan_v1.considered_pairs == 6
    assert plan_v2.considered_pairs == 12
    assert plan_v1.policy_version == FOLLOWUP_POLICY_V1
    assert plan_v2.policy_version == FOLLOWUP_POLICY_V2


def test_v1_observation_does_not_satisfy_v2_due_backlog() -> None:
    """Existing V1 observations must not falsely satisfy V2 completion."""

    decision = _decision(evaluated_at=NOW - timedelta(days=40))
    projection = _projection(
        expected_holding_days=None,  # STRATEGY_HORIZON unsupported; keep it simple
        generated_at=decision.evaluated_at - timedelta(hours=1),  # fresh, non-stale
    )
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    # A genuinely retained close strictly before the (shared, gap_days == 0)
    # boundary -- otherwise V2's own SHORT_HORIZON pairs below would resolve
    # PERMANENTLY_UNAVAILABLE (Counterfactual Follow-Up Starvation Fix v1)
    # rather than the genuinely retryable DUE this test means to exercise;
    # see ``test_sync_v1_reproduces_known_wrong_crypto_baseline_and_v2_
    # corrects_it`` for the dedicated point-in-time proof this fixture is
    # deliberately not trying to duplicate.
    decision_bucket = completed_utc_day(decision.evaluated_at)
    crypto_prices = _FakeCryptoPriceSource(
        {
            ("BTC-USD", decision_bucket - timedelta(days=1)): CryptoPriceLookup(
                bucket_start=decision_bucket - timedelta(days=1),
                close=Decimal("100"),
                source_observation_id="c-baseline",
                evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            ),
        }
    )
    v1_observation_id = followup_observation_id(
        decision_id=decision.decision_id,
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    plan_v1 = PlanFollowUpObservations(decisions, subjects, crypto_prices).execute(
        as_of=NOW, already_observed={v1_observation_id}, policy_version=FOLLOWUP_POLICY_V1
    )
    assert dict(plan_v1.state_counts).get("OBSERVED", 0) == 1
    plan_v2 = PlanFollowUpObservations(decisions, subjects, crypto_prices).execute(
        as_of=NOW, already_observed={v1_observation_id}, policy_version=FOLLOWUP_POLICY_V2
    )
    assert dict(plan_v2.state_counts).get("OBSERVED", 0) == 0
    # SHORT_HORIZON/ORIGIN_EVIDENCE and SHORT_HORIZON/DECISION_TIME both DUE
    # (fresh, non-stale projection -- as_of is far past both horizons);
    # STRATEGY_HORIZON stays UNSUPPORTED under both baseline kinds.
    assert dict(plan_v2.state_counts).get("DUE", 0) == 2
    assert dict(plan_v2.state_counts).get("UNSUPPORTED", 0) == 2


def test_sync_v1_reproduces_known_wrong_crypto_baseline_and_v2_corrects_it() -> None:
    """The central hindsight-leakage regression this milestone exists to
    fix.  Traced and reproduced against real retained decisions (e.g. a
    genuine ZEC-USD decision: V1 stamped 470.06; the decision's own
    economics actually priced against 467.04, the prior completed UTC day
    relative to the *subject's* own generation, not the decision's own
    evaluation day).  This fixture reproduces that exact shape:
    ``decision.evaluated_at`` and ``projection.generated_at`` fall on the
    same calendar day (the common, real-production case -- both stamped
    within the same operating cycle), so FOLLOWUP_POLICY_V1's
    ``completed_utc_day(decision.evaluated_at)`` bucket is the *decision
    day itself* (a same-day close that only existed because a bar for that
    day was already retained by the time this test's fake evaluates it --
    in real production, one that only became retained after the projection
    was generated) while FOLLOWUP_POLICY_V2 correctly steps back to the
    prior completed day relative to the projection's own generation.

    Because ``decision.evaluated_at``/``projection.generated_at`` fall on
    the same calendar day (gap_days == 0, the common real-production case --
    see this milestone's own read-only audit of all 312 real retained CRYPTO
    decisions: 100% of the 25 that already have retained V1 observations
    have gap_days == 0), V2's ``ORIGIN_EVIDENCE`` and ``DECISION_TIME``
    baseline kinds resolve to numerically *identical* reference
    prices/targets here -- this is exactly what the gap=0 case is supposed
    to look like (see ``test_sync_v2_baseline_anchors_on_subject_generation_
    not_decision_evaluation_when_stale`` below for the gap>0 case where they
    genuinely diverge).  What is compared against V1 below is specifically
    V2's ``DECISION_TIME`` row -- the corrected version of what V1 was
    always *trying* to compute (target anchored to the decision, baseline no
    longer hindsight-leaking) -- with V2's ``ORIGIN_EVIDENCE`` row checked
    separately to confirm it is independently retained too."""

    day = datetime(2026, 8, 12, tzinfo=UTC)
    decision_evaluated_at = day + timedelta(hours=23, minutes=25)
    projection_generated_at = day + timedelta(
        hours=20, minutes=5
    )  # same day, matches real production
    decision = _decision(evaluated_at=decision_evaluated_at)
    projection = _projection(generated_at=projection_generated_at, expected_holding_days=None)
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    closes = {
        # correct V2 baseline (both baseline kinds, gap_days == 0 here):
        # prior completed day relative to completed_utc_day(projection.
        # generated_at) == completed_utc_day(decision.evaluated_at) == day
        ("BTC-USD", day - timedelta(days=1)): CryptoPriceLookup(
            bucket_start=day - timedelta(days=1),
            close=Decimal("100"),
            source_observation_id="c-prior-completed-day",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        # known-wrong V1 baseline: completed_utc_day(decision.evaluated_at) == day
        # -- a same-day close that was never genuinely known at generation time
        ("BTC-USD", day): CryptoPriceLookup(
            bucket_start=day,
            close=Decimal("105"),
            source_observation_id="c-same-day-as-decision",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        ("BTC-USD", day + timedelta(days=1)): CryptoPriceLookup(
            bucket_start=day + timedelta(days=1),
            close=Decimal("110"),
            source_observation_id="c-target-day",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
    }
    as_of = decision_evaluated_at + timedelta(days=1)
    repository = _FakeRepository()

    v1_prices = _FakeCryptoPriceSource(closes)
    v1_result = SyncFollowUpObservations(
        decisions, subjects, v1_prices, _FakeKalshiSource(error="unused"), repository
    ).execute(as_of=as_of, sample_limit=10, policy_version=FOLLOWUP_POLICY_V1)
    assert v1_result.created_count == 1
    assert v1_result.entries[0].observation_id is not None
    v1_observation = repository.observation(v1_result.entries[0].observation_id)
    assert v1_observation is not None

    v2_prices = _FakeCryptoPriceSource(closes)
    v2_result = SyncFollowUpObservations(
        decisions, subjects, v2_prices, _FakeKalshiSource(error="unused"), repository
    ).execute(as_of=as_of, sample_limit=10, policy_version=FOLLOWUP_POLICY_V2)
    # Both FollowUpBaselineKind rows for this one decision/horizon are
    # genuinely due and buildable (gap_days == 0 -- see docstring).
    assert v2_result.created_count == 2
    v2_decision_time = repository.observation(
        followup_observation_id(
            decision_id=decision.decision_id,
            policy_version=FOLLOWUP_POLICY_V2,
            horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
            baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        )
    )
    v2_origin_evidence = repository.observation(
        followup_observation_id(
            decision_id=decision.decision_id,
            policy_version=FOLLOWUP_POLICY_V2,
            horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
            baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
        )
    )
    assert v2_decision_time is not None and v2_origin_evidence is not None

    # V1 reproduces its known-wrong historical baseline exactly (never
    # silently corrected) ...
    assert v1_observation.reference_price_at_baseline == Decimal("105")
    # ... V2's DECISION_TIME row -- the corrected version of what V1 was
    # always trying to compute -- selects the correct point-in-time baseline ...
    assert v2_decision_time.reference_price_at_baseline == Decimal("100")
    assert v2_decision_time.baseline_kind is FollowUpBaselineKind.DECISION_TIME
    # ... and V2's ORIGIN_EVIDENCE row is independently retained, numerically
    # identical here only because gap_days == 0 in this fixture ...
    assert v2_origin_evidence.reference_price_at_baseline == Decimal("100")
    assert v2_origin_evidence.baseline_kind is FollowUpBaselineKind.ORIGIN_EVIDENCE
    # ... target price *lookup mechanism* (exact bucket, fail closed) is
    # unchanged, but V1 and V2 now genuinely ask for different buckets: V1
    # still asks for completed_utc_day(target_time) == Aug 13 (c-target-day,
    # 110) -- but V2 derives its target bucket from where its own baseline
    # candle actually landed (Aug 11) plus horizon_days (1), landing on Aug
    # 12 (c-same-day-as-decision, 105) instead -- Part:
    # target-bucket-from-baseline-candle.  Using Aug 13 for V2 too (the old,
    # buggy behavior) would silently compare closes 2 calendar days apart
    # for a nominal "SHORT_HORIZON = 1 day" observation ...
    assert v1_observation.reference_price_at_target == Decimal("110")
    assert v2_decision_time.reference_price_at_target == Decimal("105")
    assert v2_origin_evidence.reference_price_at_target == Decimal("105")
    # ... horizon days are unchanged everywhere ...
    assert v1_observation.horizon_days == v2_decision_time.horizon_days == 1
    assert v2_origin_evidence.horizon_days == 1
    # ... V1's and V2-DECISION_TIME's target_time are identical (both anchor
    # to decision.evaluated_at) ...
    assert v1_observation.target_time == v2_decision_time.target_time
    # ... but V2-ORIGIN_EVIDENCE's target_time is genuinely different (it
    # anchors to projection.generated_at instead) -- landing in the same
    # calendar bucket here only because gap_days == 0, not because the two
    # instants are the same ...
    assert v2_origin_evidence.target_time != v1_observation.target_time
    assert v2_origin_evidence.target_time == projection_generated_at + timedelta(days=1)
    assert v2_decision_time.target_time == decision_evaluated_at + timedelta(days=1)
    # ... same stable decision identity, distinct observation identity ...
    assert v1_observation.decision_id == v2_decision_time.decision_id == decision.decision_id
    assert v2_origin_evidence.decision_id == decision.decision_id
    assert v1_observation.decision_canonical_hash == v2_decision_time.decision_canonical_hash
    assert v1_observation.observation_id != v2_decision_time.observation_id
    assert v2_decision_time.observation_id != v2_origin_evidence.observation_id
    assert v1_observation.policy_version == FOLLOWUP_POLICY_V1
    assert (
        v2_decision_time.policy_version == v2_origin_evidence.policy_version == (FOLLOWUP_POLICY_V2)
    )
    # ... subsequent_return is recomputed from authoritative market
    # evidence, never carried over from V1's own stored return ...
    expected_v1_return = ((Decimal("110") - Decimal("105")) / Decimal("105")).quantize(
        RETURN_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    expected_v2_return = ((Decimal("105") - Decimal("100")) / Decimal("100")).quantize(
        RETURN_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    assert v1_observation.subsequent_return == expected_v1_return
    assert v2_decision_time.subsequent_return == expected_v2_return
    assert v2_origin_evidence.subsequent_return == expected_v2_return
    assert v1_observation.subsequent_return != v2_decision_time.subsequent_return
    assert v1_observation.authority == "NONE"
    assert v2_decision_time.authority == v2_origin_evidence.authority == "NONE"
    # ... and V2's own *baseline* query (hindsight-leakage regression:
    # strictly before, never on-or-after) is asked with the boundary itself
    # as ``before`` -- and (proven above via reference_price_at_baseline ==
    # 100, the Aug 11 candle, not Aug 12's 105) never resolves to a bucket
    # at or after it, even though ``daily_close`` legitimately *does* get
    # asked for that same Aug 12 boundary separately, on the *target* side
    # (below) -- two different queries, two different properties.
    boundary = completed_utc_day(projection_generated_at)
    assert ("latest_close_before", "BTC-USD", boundary) in v2_prices.calls
    # V1's own query, symmetrically, never touches the new V2 method at all.
    assert all(call[0] == "daily_close" for call in v1_prices.calls)
    # ... daily-candle horizon/bucket-correctness regression: V1's target
    # bucket is the naive completed_utc_day(target_time) == day + 1 (Aug
    # 13); V2's is the baseline candle's own bucket_start + horizon_days ==
    # (day - 1) + 1 == day (Aug 12) -- one full day earlier than the naive
    # computation would have given, which is exactly what keeps V2's
    # baseline-to-target interval at a genuine 1 day instead of 2.
    assert ("daily_close", "BTC-USD", day + timedelta(days=1)) in v1_prices.calls
    assert ("daily_close", "BTC-USD", day) in v2_prices.calls
    assert ("daily_close", "BTC-USD", day + timedelta(days=1)) not in v2_prices.calls


def test_sync_v2_baseline_anchors_on_subject_generation_not_decision_evaluation_when_stale() -> (
    None
):
    """Proves FOLLOWUP_POLICY_V2's ``ORIGIN_EVIDENCE`` baseline is not merely
    "decision.evaluated_at minus one day" (a bare date-subtraction shortcut
    this milestone's own original audit declined to adopt without proof):
    when a decision is evaluated against a SharedOpportunityProjection that
    has gone stale (evaluated several days after the projection's own
    generation -- reachable in real production: EVIDENCE_STALE is a
    rejection *reason*, never a barrier to a CounterfactualDecision being
    retained, per the "both cohorts, always" design choice), the correct
    ``ORIGIN_EVIDENCE`` baseline still comes from the *subject's* own
    generation day, never the decision's (much later) evaluation day.

    This is also the direct real-data-shaped regression for this
    milestone's own pre-deployment staleness-semantics audit's central
    finding: because ``ORIGIN_EVIDENCE``'s ``target_time`` now moves *with*
    its baseline (anchored to ``projection.generated_at``, not
    ``decision.evaluated_at``) while ``DECISION_TIME``'s stays anchored to
    the decision, the two baseline kinds' ``subsequent_return`` for the
    identical (decision, horizon) pair are computed over genuinely different
    calendar windows here -- proven by construction to even disagree in
    *sign*, exactly as observed against a real retained decision during this
    milestone's audit (see the milestone's own acceptance report)."""

    projection_generated_at = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)
    decision_evaluated_at = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)  # 4 days later -- stale
    decision = _decision(evaluated_at=decision_evaluated_at)
    projection = _projection(generated_at=projection_generated_at, expected_holding_days=None)
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    closes = {
        # ORIGIN_EVIDENCE baseline: prior completed day relative to the
        # *projection's* own generation (Jul 31), not the decision's
        # evaluation (Aug 4) -- what a bare "decision.evaluated_at minus one
        # day" formula would have wrongly selected instead.
        ("BTC-USD", datetime(2026, 7, 31, tzinfo=UTC)): CryptoPriceLookup(
            bucket_start=datetime(2026, 7, 31, tzinfo=UTC),
            close=Decimal("50"),
            source_observation_id="c-origin-baseline",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        # ORIGIN_EVIDENCE target (Part: target-bucket-from-baseline-candle):
        # the *baseline* candle's own bucket_start (Jul 31) + horizon_days
        # (1) -> Aug 1 bucket -- not a re-derived
        # completed_utc_day(projection_generated_at + 1 day) (that would be
        # Aug 2, one calendar day further from the baseline than
        # horizon_days actually calls for).
        ("BTC-USD", datetime(2026, 8, 1, tzinfo=UTC)): CryptoPriceLookup(
            bucket_start=datetime(2026, 8, 1, tzinfo=UTC),
            close=Decimal("51"),
            source_observation_id="c-origin-target",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        # DECISION_TIME baseline: prior completed day relative to the
        # *decision's* own evaluation (Aug 5) -- genuinely correct for this
        # different question, not "wrong" (V1 tried to answer this same
        # question but leaked hindsight by using the exact same-day bucket
        # instead of the latest-retained-before query used here).
        ("BTC-USD", datetime(2026, 8, 4, tzinfo=UTC)): CryptoPriceLookup(
            bucket_start=datetime(2026, 8, 4, tzinfo=UTC),
            close=Decimal("999"),
            source_observation_id="c-decision-baseline",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        # DECISION_TIME target (Part: target-bucket-from-baseline-candle):
        # the *baseline* candle's own bucket_start (Aug 4) + horizon_days
        # (1) -> Aug 5 bucket -- not Aug 6 (completed_utc_day(decision_
        # evaluated_at + 1 day)), for the identical reason as above.
        ("BTC-USD", datetime(2026, 8, 5, tzinfo=UTC)): CryptoPriceLookup(
            bucket_start=datetime(2026, 8, 5, tzinfo=UTC),
            close=Decimal("60"),
            source_observation_id="c-decision-target",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
    }
    crypto_prices = _FakeCryptoPriceSource(closes)
    repository = _FakeRepository()
    result = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, _FakeKalshiSource(error="unused"), repository
    ).execute(
        as_of=decision_evaluated_at + timedelta(days=2),
        sample_limit=10,
        policy_version=FOLLOWUP_POLICY_V2,
    )
    assert result.created_count == 2

    origin_evidence = repository.observation(
        followup_observation_id(
            decision_id=decision.decision_id,
            policy_version=FOLLOWUP_POLICY_V2,
            horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
            baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
        )
    )
    decision_time = repository.observation(
        followup_observation_id(
            decision_id=decision.decision_id,
            policy_version=FOLLOWUP_POLICY_V2,
            horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
            baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        )
    )
    assert origin_evidence is not None and decision_time is not None

    assert origin_evidence.reference_price_at_baseline == Decimal("50")
    assert origin_evidence.reference_price_at_target == Decimal("51")
    # target_time itself is still the raw anchor + horizon_days (unaffected
    # by which bucket the *price* was actually read from).
    assert origin_evidence.target_time == projection_generated_at + timedelta(days=1)

    assert decision_time.reference_price_at_baseline == Decimal("999")
    assert decision_time.reference_price_at_target == Decimal("60")
    assert decision_time.target_time == decision_evaluated_at + timedelta(days=1)

    # The central pre-deployment finding: the two rows disagree even in
    # *sign* here -- a decision-relative reading (heavy losses, since the
    # stale evidence's own "current price" reading had already moved from
    # 50 to 999 by the time Apex evaluated the decision) versus an
    # origin-relative reading (a small genuine gain over the strategy's own
    # declared horizon) are simply different, both legitimate, facts -- never
    # collapsed into one ambiguous number.
    assert origin_evidence.subsequent_return > 0
    assert decision_time.subsequent_return < 0
    assert origin_evidence.subsequent_return != decision_time.subsequent_return
    expected_origin_return = ((Decimal("51") - Decimal("50")) / Decimal("50")).quantize(
        RETURN_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    expected_decision_return = ((Decimal("60") - Decimal("999")) / Decimal("999")).quantize(
        RETURN_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    assert origin_evidence.subsequent_return == expected_origin_return
    assert decision_time.subsequent_return == expected_decision_return
    # Daily-candle horizon/bucket-correctness regression: each row's target
    # bucket is its own baseline candle's bucket_start + horizon_days, never
    # the one-day-later bucket a naive completed_utc_day(target_time)
    # re-derivation would have picked.
    assert (
        "daily_close",
        "BTC-USD",
        datetime(2026, 8, 1, tzinfo=UTC),
    ) in crypto_prices.calls
    assert (
        "daily_close",
        "BTC-USD",
        datetime(2026, 8, 2, tzinfo=UTC),
    ) not in crypto_prices.calls
    assert (
        "daily_close",
        "BTC-USD",
        datetime(2026, 8, 5, tzinfo=UTC),
    ) in crypto_prices.calls
    assert (
        "daily_close",
        "BTC-USD",
        datetime(2026, 8, 6, tzinfo=UTC),
    ) not in crypto_prices.calls


def test_sync_v2_crypto_creates_from_local_bars_zero_requests_and_is_idempotent() -> None:
    """``generated_at`` is set exactly equal to ``decision.evaluated_at``
    here (gap_days == 0), so ``ORIGIN_EVIDENCE`` and ``DECISION_TIME``
    resolve to numerically identical baselines/targets per horizon kind --
    2 horizon kinds x 2 baseline kinds = 4 genuinely distinct retained rows
    (Part: pre-deployment staleness-semantics audit), not 2."""

    decision = _decision(evaluated_at=NOW - timedelta(days=40))
    projection = _projection(expected_holding_days=30, generated_at=NOW - timedelta(days=40))
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    generated_boundary = completed_utc_day(NOW - timedelta(days=40))
    closes = {
        # Baseline candle for both horizon kinds (gap_days == 0, so both
        # baseline kinds resolve identically here).
        ("BTC-USD", generated_boundary - timedelta(days=1)): CryptoPriceLookup(
            bucket_start=generated_boundary - timedelta(days=1),
            close=Decimal("100"),
            source_observation_id="c1",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        # SHORT_HORIZON target (Part: target-bucket-from-baseline-candle):
        # baseline candle's bucket_start + 1 day == generated_boundary
        # itself -- not generated_boundary + 1 (the naive completed_utc_day
        # (target_time) computation V1/pre-fix-V2 would have used).
        ("BTC-USD", generated_boundary): CryptoPriceLookup(
            bucket_start=generated_boundary,
            close=Decimal("101"),
            source_observation_id="c2",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        # STRATEGY_HORIZON (30 days) target: baseline candle's bucket_start +
        # 30 days == generated_boundary + 29 -- not generated_boundary + 30.
        ("BTC-USD", generated_boundary + timedelta(days=29)): CryptoPriceLookup(
            bucket_start=generated_boundary + timedelta(days=29),
            close=Decimal("150"),
            source_observation_id="c3",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
    }
    crypto_prices = _FakeCryptoPriceSource(closes)
    kalshi_source = _FakeKalshiSource(error="SHOULD_NEVER_BE_CALLED_FOR_CRYPTO")
    repository = _FakeRepository()
    result = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, kalshi_source, repository
    ).execute(as_of=NOW, sample_limit=10, policy_version=FOLLOWUP_POLICY_V2)
    assert result.created_count == 4  # 2 horizon kinds x 2 baseline kinds
    assert result.policy_version == FOLLOWUP_POLICY_V2
    assert result.provider_request_count == 0
    assert kalshi_source.call_count == 0
    # 4 genuinely distinct observation ids -- one per (horizon_kind,
    # baseline_kind) combination.
    created_ids = {entry.observation_id for entry in result.entries if entry.outcome == "CREATED"}
    assert len(created_ids) == 4
    # Baseline-side resolution: once per pair (4 total), all resolving to
    # the identical boundary here since generated_at == decision.evaluated_at.
    decision_side_calls = [call for call in crypto_prices.calls if call[0] == "latest_close_before"]
    assert len(decision_side_calls) == 4
    assert all(call[2] == generated_boundary for call in decision_side_calls)
    # ... target-side resolution stays the unchanged *mechanism* (exact
    # bucket) but is now asked for the baseline candle's own bucket_start +
    # horizon_days, not a boundary re-derived from target_time -- for
    # SHORT_HORIZON that lands exactly *on* generated_boundary (one bucket
    # earlier than the pre-fix computation would have asked for).
    target_side_calls = [call for call in crypto_prices.calls if call[0] == "daily_close"]
    assert len(target_side_calls) == 4
    assert (
        sum(1 for call in target_side_calls if call[2] == generated_boundary) == 2
    )  # 2 baseline kinds
    assert (
        sum(1 for call in target_side_calls if call[2] == generated_boundary + timedelta(days=29))
        == 2
    )

    second = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, kalshi_source, repository
    ).execute(as_of=NOW + timedelta(hours=1), sample_limit=10, policy_version=FOLLOWUP_POLICY_V2)
    assert second.created_count == 0
    assert second.skipped_already_observed_count == 4
    assert kalshi_source.call_count == 0


def test_sync_kalshi_v2_matches_v1_non_regression_end_to_end() -> None:
    """Part: Kalshi non-regression, this time through the full
    ``SyncFollowUpObservations`` orchestration (not just the pure builder) --
    identical provider read, identical objective facts, identical request
    accounting, differing only in policy_version/identity."""

    decision = _kalshi_decision(evaluated_at=NOW - timedelta(days=2))
    projection = _projection(
        venue=SharedVenue.KALSHI,
        instrument_id="KX-EVENT",
        product_or_market_id="KX-EVENT:COMPLETE_SET",
        expected_holding_days=None,
    )
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    read = KalshiMarketRead(
        market_status="open",
        yes_bid=Decimal("0.4"),
        yes_ask=Decimal("0.45"),
        settlement_result=None,
        settlement_source=None,
        source_endpoint_identity="kalshi-public-event-followup-v1",
        request_count=1,
    )

    v1_repository = _FakeRepository()
    v1_kalshi = _FakeKalshiSource(reads={"KX-EVENT": read})
    v1_result = SyncFollowUpObservations(
        decisions, subjects, _FakeCryptoPriceSource({}), v1_kalshi, v1_repository
    ).execute(as_of=NOW, sample_limit=10, policy_version=FOLLOWUP_POLICY_V1)

    v2_repository = _FakeRepository()
    v2_kalshi = _FakeKalshiSource(reads={"KX-EVENT": read})
    v2_result = SyncFollowUpObservations(
        decisions, subjects, _FakeCryptoPriceSource({}), v2_kalshi, v2_repository
    ).execute(as_of=NOW, sample_limit=10, policy_version=FOLLOWUP_POLICY_V2)

    assert v1_result.created_count == v2_result.created_count == 1
    assert v1_result.provider_request_count == v2_result.provider_request_count == 1
    assert v1_kalshi.call_count == v2_kalshi.call_count == 1
    assert v1_result.entries[0].observation_id is not None
    assert v2_result.entries[0].observation_id is not None
    v1_observation = v1_repository.observation(v1_result.entries[0].observation_id)
    v2_observation = v2_repository.observation(v2_result.entries[0].observation_id)
    assert v1_observation is not None and v2_observation is not None
    assert v1_observation.policy_version == FOLLOWUP_POLICY_V1
    assert v2_observation.policy_version == FOLLOWUP_POLICY_V2
    assert v1_observation.observation_id != v2_observation.observation_id
    assert v1_observation.market_status_at_target == v2_observation.market_status_at_target
    assert v1_observation.yes_bid_at_target == v2_observation.yes_bid_at_target
    assert v1_observation.yes_ask_at_target == v2_observation.yes_ask_at_target
    assert v1_observation.settlement_result == v2_observation.settlement_result
    assert v1_observation.data_limitations == v2_observation.data_limitations
    assert (
        v1_observation.baseline_kind
        is v2_observation.baseline_kind
        is (FollowUpBaselineKind.DECISION_TIME)
    )


def test_sync_v1_and_v2_coexist_for_same_decision_neither_blocks_the_other() -> None:
    """Decision D/V1/SHORT_HORIZON and Decision D/V2/SHORT_HORIZON must be
    independently retainable: syncing under one version is never mistaken
    for "already observed" under the other, and a genuine second attempt is
    always made.  V2 additionally retains two rows to V1's one (Part:
    pre-deployment staleness-semantics audit) -- ``generated_at`` is set
    fresh/aligned (same calendar day as the decision) so both baseline kinds
    resolve identically here; the point of this test is id-namespace
    isolation, not baseline-kind divergence (see the dedicated tests above
    for that)."""

    decision = _decision(evaluated_at=NOW - timedelta(days=2))
    projection = _projection(
        expected_holding_days=None, generated_at=decision.evaluated_at - timedelta(hours=1)
    )
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    decision_bucket = decision.evaluated_at.replace(hour=0, minute=0, second=0, microsecond=0)
    closes = {
        ("BTC-USD", decision_bucket - timedelta(days=1)): CryptoPriceLookup(
            bucket_start=decision_bucket - timedelta(days=1),
            close=Decimal("90"),
            source_observation_id="c0",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        ("BTC-USD", decision_bucket): CryptoPriceLookup(
            bucket_start=decision_bucket,
            close=Decimal("100"),
            source_observation_id="c1",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        ("BTC-USD", decision_bucket + timedelta(days=1)): CryptoPriceLookup(
            bucket_start=decision_bucket + timedelta(days=1),
            close=Decimal("102"),
            source_observation_id="c2",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
    }
    repository = _FakeRepository()
    kalshi_source = _FakeKalshiSource(error="unused")

    v1 = SyncFollowUpObservations(
        decisions, subjects, _FakeCryptoPriceSource(closes), kalshi_source, repository
    ).execute(as_of=NOW, sample_limit=10, policy_version=FOLLOWUP_POLICY_V1)
    assert v1.created_count == 1

    # A V2 sync immediately after must not see V1's row as already observed.
    v2 = SyncFollowUpObservations(
        decisions, subjects, _FakeCryptoPriceSource(closes), kalshi_source, repository
    ).execute(as_of=NOW, sample_limit=10, policy_version=FOLLOWUP_POLICY_V2)
    assert v2.created_count == 2  # ORIGIN_EVIDENCE + DECISION_TIME
    assert v2.skipped_already_observed_count == 0

    # And a repeated V1 sync must symmetrically not see V2's rows either.
    v1_again = SyncFollowUpObservations(
        decisions, subjects, _FakeCryptoPriceSource(closes), kalshi_source, repository
    ).execute(as_of=NOW, sample_limit=10, policy_version=FOLLOWUP_POLICY_V1)
    assert v1_again.created_count == 0
    assert v1_again.skipped_already_observed_count == 1


# --------------------------------------------------------------------------
# Counterfactual Follow-Up Starvation Fix v1 -- regression, transient-retry,
# and boundedness proofs.  Reproduces the *actual* confirmed defect's shape
# (read-only audit: 40 permanently-blocked CRYPTO SHORT_HORIZON pairs sort
# oldest-due-first and, before this fix, consumed the entire bounded
# ``sample_limit`` every cycle, forever) entirely against in-memory fakes --
# no database, no network (Part: "do not depend only on the live DB for
# this test").  See ``tests/persistence/
# test_postgres_counterfactual_followup_recurring.py`` for the companion
# real-Postgres proof of the specific "earliest containing observation
# completed_at is later than the allowed anchor" availability-as-of shape.
# --------------------------------------------------------------------------

PRODUCTION_SAMPLE_LIMIT = 25  # mirrors COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
PRODUCTION_CANDIDATE_SCAN_LIMIT = (
    2000  # mirrors COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_POLICY_VERSION
)
FLOOR_DAY = NOW - timedelta(days=40)  # Apex's own first retained CRYPTO day, in this fixture
LATER_DAY = FLOOR_DAY + timedelta(days=5)  # genuinely later, genuinely satisfiable decisions


def _permanent_cohort(
    count: int, *, expected_holding_days: int | None = None
) -> tuple[tuple[CounterfactualDecision, ...], dict[str, SharedOpportunityProjection]]:
    """``count`` genuine-shaped CRYPTO decisions evaluated on Apex's own
    first retained day, each with a real, retained subject projection but
    *zero* retained BTC-USD evidence anywhere before that day -- exactly
    the real, confirmed Jul-30 cohort's shape (no candidate bucket exists
    at all, the simplest sufficient condition for ``CryptoPriceSource.
    latest_close_before`` to return ``None`` -- see ``tests/persistence/
    test_postgres_counterfactual_followup_recurring.py`` for the more
    specific "candidate exists but was captured too late" availability-as-
    of shape). ``generated_at`` matches ``evaluated_at`` (gap_days == 0) so
    ``ORIGIN_EVIDENCE`` and ``DECISION_TIME`` share the identical
    unsatisfiable boundary, both permanently blocked identically."""

    decisions = tuple(
        _decision(
            decision_id=f"perm-{i:03d}",
            subject_projection_id=f"perm-proj-{i:03d}",
            evaluated_at=FLOOR_DAY,
        )
        for i in range(count)
    )
    projections = {
        f"perm-proj-{i:03d}": _projection(
            projection_id=f"perm-proj-{i:03d}",
            generated_at=FLOOR_DAY,
            expected_holding_days=expected_holding_days,
        )
        for i in range(count)
    }
    return decisions, projections


def _later_satisfiable_crypto(
    count: int,
) -> tuple[
    tuple[CounterfactualDecision, ...],
    dict[str, SharedOpportunityProjection],
    dict[tuple[str, datetime], CryptoPriceLookup],
]:
    """``count`` CRYPTO decisions evaluated genuinely later than the
    permanent cohort (so they always sort after it, oldest-due-first),
    each with a genuinely retained, genuinely resolvable baseline and
    target bar -- SHORT_HORIZON only (``expected_holding_days=None``),
    mirroring ``_permanent_cohort``'s own shape so the only variable
    between the two cohorts is baseline availability, nothing else."""

    decisions = tuple(
        _decision(
            decision_id=f"later-crypto-{i:03d}",
            subject_projection_id=f"later-crypto-proj-{i:03d}",
            evaluated_at=LATER_DAY,
        )
        for i in range(count)
    )
    projections = {
        f"later-crypto-proj-{i:03d}": _projection(
            projection_id=f"later-crypto-proj-{i:03d}",
            generated_at=LATER_DAY,
            expected_holding_days=None,
        )
        for i in range(count)
    }
    decision_bucket = completed_utc_day(LATER_DAY)
    closes = {
        ("BTC-USD", decision_bucket - timedelta(days=1)): CryptoPriceLookup(
            bucket_start=decision_bucket - timedelta(days=1),
            close=Decimal("100"),
            source_observation_id="later-crypto-baseline",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        ("BTC-USD", decision_bucket): CryptoPriceLookup(
            bucket_start=decision_bucket,
            close=Decimal("105"),
            source_observation_id="later-crypto-target",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
    }
    return decisions, projections, closes


def _later_satisfiable_kalshi(
    count: int,
) -> tuple[
    tuple[CounterfactualDecision, ...],
    dict[str, SharedOpportunityProjection],
    dict[str, KalshiMarketRead],
]:
    """``count`` KALSHI decisions evaluated genuinely later than the
    permanent CRYPTO cohort, each independently, genuinely resolvable via
    the (fake, zero-network) Kalshi source -- SHORT_HORIZON only, exactly
    mirroring ``_later_satisfiable_crypto``'s own shape."""

    decisions = tuple(
        _kalshi_decision(
            decision_id=f"later-kalshi-{i:03d}",
            subject_projection_id=f"later-kalshi-proj-{i:03d}",
            evaluated_at=LATER_DAY,
        )
        for i in range(count)
    )
    projections = {
        f"later-kalshi-proj-{i:03d}": _projection(
            projection_id=f"later-kalshi-proj-{i:03d}",
            venue=SharedVenue.KALSHI,
            instrument_id=f"KX-LATER-{i:03d}",
            product_or_market_id=f"KX-LATER-{i:03d}:COMPLETE_SET",
            expected_holding_days=None,
        )
        for i in range(count)
    }
    reads = {
        f"KX-LATER-{i:03d}": KalshiMarketRead(
            market_status="open",
            yes_bid=Decimal("0.40"),
            yes_ask=Decimal("0.45"),
            settlement_result=None,
            settlement_source=None,
            source_endpoint_identity="kalshi-public-event-followup-v1",
            request_count=1,
        )
        for i in range(count)
    }
    return decisions, projections, reads


# Counterfactual Follow-Up Long-Run Fairness v1 -- an even earlier day than
# ``FLOOR_DAY`` so a seeded-OBSERVED cohort always sorts first, ahead of
# both ``_permanent_cohort`` and ``_not_yet_due_cohort``.
SEED_DAY = FLOOR_DAY - timedelta(days=5)


def _seed_observed_cohort(
    count: int,
) -> tuple[
    tuple[CounterfactualDecision, ...],
    dict[str, SharedOpportunityProjection],
    dict[tuple[str, datetime], CryptoPriceLookup],
]:
    """``count`` CRYPTO decisions genuinely resolvable and evaluated before
    everything else in a long-run fixture -- meant to be synced to
    completion *before* the bounded cycles under test begin, so their
    SHORT_HORIZON pairs are already-retained ``OBSERVED`` fact, not merely
    "would resolve if attempted".  Exactly ``_later_satisfiable_crypto``'s
    own shape, at a different (earliest) day and id prefix, so both can
    appear in the same fixture without colliding."""

    decisions = tuple(
        _decision(
            decision_id=f"seed-{i:03d}",
            subject_projection_id=f"seed-proj-{i:03d}",
            evaluated_at=SEED_DAY,
        )
        for i in range(count)
    )
    projections = {
        f"seed-proj-{i:03d}": _projection(
            projection_id=f"seed-proj-{i:03d}", generated_at=SEED_DAY, expected_holding_days=None
        )
        for i in range(count)
    }
    decision_bucket = completed_utc_day(SEED_DAY)
    closes = {
        ("BTC-USD", decision_bucket - timedelta(days=1)): CryptoPriceLookup(
            bucket_start=decision_bucket - timedelta(days=1),
            close=Decimal("50"),
            source_observation_id="seed-baseline",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        ("BTC-USD", decision_bucket): CryptoPriceLookup(
            bucket_start=decision_bucket,
            close=Decimal("55"),
            source_observation_id="seed-target",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
    }
    return decisions, projections, closes


def _not_yet_due_cohort(
    count: int,
) -> tuple[tuple[CounterfactualDecision, ...], dict[str, SharedOpportunityProjection]]:
    """``count`` CRYPTO decisions shaped like ``_permanent_cohort`` (same
    day, same zero-retained-evidence baseline -- SHORT_HORIZON pairs are
    therefore also ``PERMANENTLY_UNAVAILABLE``) but with a declared holding
    period long enough that their STRATEGY_HORIZON pairs stay ``NOT_YET_
    DUE`` for the entire span of any cycle count a test could plausibly
    run.  The third non-actionable-prefix state Long-Run Fairness v1 must
    not starve behind, alongside ``PERMANENTLY_UNAVAILABLE`` and already
    ``OBSERVED`` (Part: the required invariant names all three)."""

    decisions = tuple(
        _decision(
            decision_id=f"notdue-{i:03d}",
            subject_projection_id=f"notdue-proj-{i:03d}",
            evaluated_at=FLOOR_DAY,
        )
        for i in range(count)
    )
    projections = {
        f"notdue-proj-{i:03d}": _projection(
            projection_id=f"notdue-proj-{i:03d}",
            generated_at=FLOOR_DAY,
            expected_holding_days=99999,
        )
        for i in range(count)
    }
    return decisions, projections


def test_sync_starvation_regression_permanent_cohort_never_starves_later_satisfiable_work() -> None:
    """The actual confirmed starvation bug, reproduced and proven fixed.

    BEFORE-FIX SHAPE (conceptual proof -- not executed; the pre-fix
    algorithm no longer exists in this codebase to run directly, exactly
    because leaving it reachable would reintroduce the defect): the old
    algorithm charged one genuine attempt against ``sample_limit`` for
    *every* ``DUE`` pair alike, oldest-due-first, whether or not that pair
    could ever succeed. With a 40-pair permanent cohort sorting first and
    ``sample_limit == 25``, the entire budget is consumed by permanent
    blockers alone, before a single later decision is ever reached."""

    permanent_decisions, permanent_projections = _permanent_cohort(20)
    permanent_pair_count = len(permanent_decisions) * 2  # SHORT_HORIZON x 2 baseline kinds
    assert permanent_pair_count == 40
    assert permanent_pair_count > PRODUCTION_SAMPLE_LIMIT
    naive_pre_fix_attempted = min(permanent_pair_count, PRODUCTION_SAMPLE_LIMIT)
    naive_pre_fix_created = 0  # every one of the first 25 oldest-due pairs is a permanent blocker
    assert naive_pre_fix_attempted == PRODUCTION_SAMPLE_LIMIT
    assert naive_pre_fix_created == 0

    later_crypto_decisions, later_crypto_projections, later_crypto_closes = (
        _later_satisfiable_crypto(3)
    )
    later_kalshi_decisions, later_kalshi_projections, later_kalshi_reads = (
        _later_satisfiable_kalshi(3)
    )

    decisions = _FakeDecisionSource(
        (*permanent_decisions, *later_crypto_decisions, *later_kalshi_decisions)
    )
    subjects = _FakeSubjectSource(
        {**permanent_projections, **later_crypto_projections, **later_kalshi_projections}
    )
    crypto_prices = _FakeCryptoPriceSource(dict(later_crypto_closes))  # nothing for FLOOR_DAY
    kalshi_source = _FakeKalshiSource(reads=later_kalshi_reads)
    repository = _FakeRepository()

    # AFTER-FIX SHAPE: run the real, fixed algorithm.
    result = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, kalshi_source, repository
    ).execute(
        as_of=NOW,
        sample_limit=PRODUCTION_SAMPLE_LIMIT,
        candidate_scan_limit=PRODUCTION_CANDIDATE_SCAN_LIMIT,
        policy_version=FOLLOWUP_POLICY_V2,
    )

    # 1. Permanent blockers cost zero attempt/provider budget, always.
    assert result.permanently_unavailable_count == 40
    assert result.genuine_attempts == 9  # 3 crypto x 2 baseline kinds + 3 kalshi x 1 baseline kind
    assert result.provider_request_count == 3  # one bounded Kalshi GET per later KALSHI decision

    # 2. Later, genuinely satisfiable CRYPTO work progresses.
    later_crypto_created = {
        entry.decision_id
        for entry in result.entries
        if entry.outcome == "CREATED" and entry.venue is SharedVenue.CRYPTO
    }
    assert later_crypto_created == {item.decision_id for item in later_crypto_decisions}

    # 3. Later, genuinely satisfiable KALSHI work progresses -- one venue's
    # permanently-unavailable backlog cannot starve the other's.
    later_kalshi_created = {
        entry.decision_id
        for entry in result.entries
        if entry.outcome == "CREATED" and entry.venue is SharedVenue.KALSHI
    }
    assert later_kalshi_created == {item.decision_id for item in later_kalshi_decisions}

    # 4. created_count == genuine_attempts: every genuine attempt spent
    # actually succeeded (zero transient blocks in this fixture) -- the
    # entire budget went to real progress, none of it wasted on permanent
    # blockers.
    assert result.created_count == 9
    assert result.blocked_count == 0

    # 5. No permanently-unavailable pair ever became a fabricated row.
    permanent_decision_ids = {item.decision_id for item in permanent_decisions}
    assert not permanent_decision_ids & {
        entry.decision_id for entry in result.entries if entry.observation_id
    }
    for entry in result.entries:
        if entry.outcome == "BLOCKED_PERMANENTLY_UNAVAILABLE":
            assert entry.decision_id in permanent_decision_ids
            assert entry.observation_id is None
            assert entry.reasons == (CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE,)

    # 6. Bounded: total examination never exceeds the (generous, production-
    # mirroring) candidate_scan_limit, and stays a small, exact, explainable
    # number -- not an unbounded full-table scan.  20 permanent decisions x
    # 4 pairs each (2 horizon kinds x 2 baseline kinds) + 3 later-crypto x 4
    # + 3 later-kalshi x 2 (KALSHI never gets ORIGIN_EVIDENCE) == 80+12+6.
    assert result.candidates_examined == 98
    assert (
        result.candidates_examined <= result.candidate_scan_limit == PRODUCTION_CANDIDATE_SCAN_LIMIT
    )

    # 7. Repeat "next cycle": the identical permanent cohort is honestly
    # re-derived as permanently unavailable again (never durably retained,
    # never magically resolved, never re-charged against budget), while the
    # now-already-observed later work costs nothing either -- a genuine,
    # indefinitely stable, non-starved steady state.
    second = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, kalshi_source, repository
    ).execute(
        as_of=NOW + timedelta(hours=1),
        sample_limit=PRODUCTION_SAMPLE_LIMIT,
        candidate_scan_limit=PRODUCTION_CANDIDATE_SCAN_LIMIT,
        policy_version=FOLLOWUP_POLICY_V2,
    )
    assert second.permanently_unavailable_count == 40
    assert second.created_count == 0
    assert second.skipped_already_observed_count == 9
    assert second.genuine_attempts == 0


def test_sync_candidate_scan_limit_is_a_bounded_secondary_safety_net() -> None:
    """Part 7 (boundedness).  ``candidate_scan_limit`` is a secondary
    wall-clock safety net, never the mechanism that keeps a permanently-
    unavailable pair from starving later work -- the regression test above
    already proves that holds at the generous production default, without
    ever approaching this bound.  This test proves the safety net itself is
    real and mechanically bounded: with a scan limit deliberately set below
    a (deliberately oversized) permanent cohort's own pair count, the sync
    stops examining further candidates once the limit is reached and
    honestly reports exactly how much it looked at -- it never claims to
    have looked at more than it did, and every candidate it did examine
    within the bound is still classified correctly."""

    # expected_holding_days set (not None) so STRATEGY_HORIZON is also DUE
    # and also permanent -- every one of the 4 pairs per decision is
    # examined-and-permanent, keeping the arithmetic exact.
    decisions, projections = _permanent_cohort(50, expected_holding_days=30)
    decisions_source = _FakeDecisionSource(decisions)
    subjects = _FakeSubjectSource(projections)
    crypto_prices = _FakeCryptoPriceSource({})  # nothing retained anywhere -- all permanent
    kalshi_source = _FakeKalshiSource(error="unused")
    repository = _FakeRepository()

    result = SyncFollowUpObservations(
        decisions_source, subjects, crypto_prices, kalshi_source, repository
    ).execute(
        as_of=NOW,
        sample_limit=PRODUCTION_SAMPLE_LIMIT,
        candidate_scan_limit=10,
        policy_version=FOLLOWUP_POLICY_V2,
    )
    assert result.candidates_examined == 10
    assert result.candidate_scan_limit == 10
    assert result.genuine_attempts == 0
    assert result.created_count == 0
    assert (
        result.permanently_unavailable_count == 10
    )  # every examined candidate was DUE and permanent


def test_sync_transient_crypto_target_gap_is_retryable_not_permanent() -> None:
    """Part 5 (transient retry).  A CRYPTO *target* bucket -- resolved via
    ``CryptoPriceSource.daily_close``'s exact-match lookup, never the
    availability-gated ``latest_close_before`` -- can legitimately not
    exist yet on one cycle and genuinely exist on a later one, once the
    ordinary recurring ``CRYPTO_UNIVERSE_REFRESH`` capture catches up.
    Proves: cycle N fabricates no observation and leaves the pair
    retryable (transient ``BLOCKED_UNAVAILABLE``, never ``PERMANENTLY_
    UNAVAILABLE``); cycle N+1, against the same repository, creates the
    correct genuine observation from the newly-retained bar."""

    decision = _decision(evaluated_at=NOW - timedelta(days=2))
    projection = _projection(
        expected_holding_days=None,
        generated_at=decision.evaluated_at,  # gap_days == 0
    )
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"proj-1": projection})
    decision_bucket = completed_utc_day(decision.evaluated_at)
    # The baseline is genuinely retained and resolvable; only the *target*
    # bucket (decision_bucket + 1 day) is missing on cycle N.
    baseline_only = {
        ("BTC-USD", decision_bucket - timedelta(days=1)): CryptoPriceLookup(
            bucket_start=decision_bucket - timedelta(days=1),
            close=Decimal("100"),
            source_observation_id="c-baseline",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
    }
    repository = _FakeRepository()
    kalshi_source = _FakeKalshiSource(error="unused")

    cycle_n = SyncFollowUpObservations(
        decisions, subjects, _FakeCryptoPriceSource(dict(baseline_only)), kalshi_source, repository
    ).execute(as_of=NOW, sample_limit=10, policy_version=FOLLOWUP_POLICY_V2)
    assert cycle_n.created_count == 0
    assert cycle_n.blocked_count == 2  # ORIGIN_EVIDENCE + DECISION_TIME, both gap_days == 0
    assert cycle_n.permanently_unavailable_count == 0
    assert len(cycle_n.entries) == 2
    for entry in cycle_n.entries:
        assert entry.outcome == "BLOCKED_UNAVAILABLE"  # transient, never PERMANENTLY_UNAVAILABLE
        assert entry.reasons == ("CRYPTO_TARGET_BUCKET_NOT_YET_RETAINED",)
    assert repository._rows == {}  # nothing fabricated

    # Cycle N+1: the ordinary recurring CRYPTO_UNIVERSE_REFRESH task has now
    # genuinely captured the target day's close -- a *new* retained bar,
    # never a backfill this sync itself invents.
    caught_up = dict(baseline_only)
    caught_up[("BTC-USD", decision_bucket)] = CryptoPriceLookup(
        bucket_start=decision_bucket,
        close=Decimal("103"),
        source_observation_id="c-target-caught-up",
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
    )
    cycle_n_plus_1 = SyncFollowUpObservations(
        decisions, subjects, _FakeCryptoPriceSource(caught_up), kalshi_source, repository
    ).execute(as_of=NOW + timedelta(hours=1), sample_limit=10, policy_version=FOLLOWUP_POLICY_V2)
    assert cycle_n_plus_1.created_count == 2
    assert cycle_n_plus_1.blocked_count == 0
    assert cycle_n_plus_1.permanently_unavailable_count == 0
    for entry in cycle_n_plus_1.entries:
        observation = repository.observation(entry.observation_id)
        assert observation is not None
        assert observation.reference_price_at_baseline == Decimal("100")
        assert observation.reference_price_at_target == Decimal("103")


# --------------------------------------------------------------------------
# Counterfactual Follow-Up Long-Run Fairness v1 -- final-review audit of the
# starvation fix above found that ``candidate_scan_limit`` alone (however
# generous) is not itself a starvation-freedom guarantee: it bounds *every*
# outcome a scan reports, not only genuine attempts, and the scan always
# restarted at the single oldest decision's first candidate with nothing to
# remember where the previous cycle stopped.  These tests prove the durable,
# pair-granular scan cursor (``FollowUpScanPosition``) closes that gap.
# --------------------------------------------------------------------------


def test_sync_candidate_scan_limit_one_still_reaches_later_work_across_cycles() -> None:
    """The literal counterexample: ``candidate_scan_limit=1`` and the single
    oldest candidate permanently unavailable -- can *any* later cycle ever
    reach a later, genuinely satisfiable pair?  Before Long-Run Fairness v1:
    no, never, on any finite number of cycles (proven directly against this
    exact fixture in this milestone's own audit -- the scan unconditionally
    restarted at the same oldest decision's same first candidate every
    time, forever).  After: yes, and not merely "eventually" -- the
    candidate-granular cursor guarantees exactly one candidate of forward
    progress per cycle regardless of outcome, so the exact cycle each
    candidate is reached is fully determined by this fixture's own shape,
    asserted below precisely rather than discovered by polling."""

    permanent_decisions, permanent_projections = _permanent_cohort(1)
    later_crypto_decisions, later_crypto_projections, later_crypto_closes = (
        _later_satisfiable_crypto(1)
    )
    decisions = _FakeDecisionSource((*permanent_decisions, *later_crypto_decisions))
    subjects = _FakeSubjectSource({**permanent_projections, **later_crypto_projections})
    crypto_prices = _FakeCryptoPriceSource(dict(later_crypto_closes))
    kalshi_source = _FakeKalshiSource({})
    repository = _FakeRepository()
    sync = SyncFollowUpObservations(decisions, subjects, crypto_prices, kalshi_source, repository)

    # The permanent decision contributes exactly 4 candidates in
    # deterministic order -- SHORT_HORIZON x {ORIGIN_EVIDENCE, DECISION_TIME}
    # (both permanently unavailable: zero retained BTC-USD evidence before
    # FLOOR_DAY) then STRATEGY_HORIZON x {ORIGIN_EVIDENCE, DECISION_TIME}
    # (both unsupported: expected_holding_days is None) -- so with
    # candidate_scan_limit=1, cycles 1-4 each examine exactly one of those,
    # and only cycles 5-6 can even look at the later decision's own two
    # SHORT_HORIZON candidates (its own STRATEGY_HORIZON pair is unsupported
    # too, never reached by this fixture's 6-cycle span).
    expected = [
        {"permanently_unavailable_count": 1, "skipped_unsupported_count": 0, "created_count": 0},
        {"permanently_unavailable_count": 1, "skipped_unsupported_count": 0, "created_count": 0},
        {"permanently_unavailable_count": 0, "skipped_unsupported_count": 1, "created_count": 0},
        {"permanently_unavailable_count": 0, "skipped_unsupported_count": 1, "created_count": 0},
        {"permanently_unavailable_count": 0, "skipped_unsupported_count": 0, "created_count": 1},
        {"permanently_unavailable_count": 0, "skipped_unsupported_count": 0, "created_count": 1},
    ]
    for cycle_index, expected_result in enumerate(expected, start=1):
        result = sync.execute(
            as_of=NOW + timedelta(hours=cycle_index),
            sample_limit=25,
            candidate_scan_limit=1,
            policy_version=FOLLOWUP_POLICY_V2,
        )
        assert result.candidates_examined == 1, f"cycle {cycle_index}"
        for field, value in expected_result.items():
            assert getattr(result, field) == value, f"cycle {cycle_index} field {field}"

    # Genuinely created, never fabricated: both retained rows belong to the
    # later decision; the permanently-unavailable decision never gained one.
    later_decision_id = later_crypto_decisions[0].decision_id
    permanent_decision_id = permanent_decisions[0].decision_id
    retained_decision_ids = {observation.decision_id for observation in repository._rows.values()}
    assert retained_decision_ids == {later_decision_id}
    assert permanent_decision_id not in retained_decision_ids
    assert len(repository._rows) == 2  # ORIGIN_EVIDENCE + DECISION_TIME, both SHORT_HORIZON


def test_sync_long_run_prefix_starvation_freedom_across_repeated_cycles() -> None:
    """The required invariant, proven directly against a deterministic
    fixture (never merely by setting the test's own limit above the fixture
    size): a ``candidate_scan_limit`` smaller than the combined count of
    oldest-sorting non-actionable candidates -- a mix of already-``OBSERVED``
    (seeded before the bounded cycles under test begin), ``PERMANENTLY_
    UNAVAILABLE``, and ``NOT_YET_DUE`` for a very long declared horizon --
    with later, genuinely satisfiable CRYPTO and KALSHI work sorting after
    all of them, across many repeated recurring-cycle-shaped calls against
    the same repository.  Proves every later-satisfiable pair is eventually
    created, the permanently-blocked/not-yet-due cohorts never are, and the
    scan never once exceeds its bound doing it."""

    seed_decisions, seed_projections, seed_closes = _seed_observed_cohort(2)
    permanent_decisions, permanent_projections = _permanent_cohort(15)
    not_yet_due_decisions, not_yet_due_projections = _not_yet_due_cohort(5)
    later_crypto_decisions, later_crypto_projections, later_crypto_closes = (
        _later_satisfiable_crypto(3)
    )
    later_kalshi_decisions, later_kalshi_projections, later_kalshi_reads = (
        _later_satisfiable_kalshi(3)
    )
    repository = _FakeRepository()

    # Seed first, alone: resolves the seed cohort's SHORT_HORIZON pairs to
    # genuine OBSERVED fact (and advances the cursor past them) *before* the
    # bounded cycles under test ever run -- proving the required invariant
    # needs already-resolved oldest work in the mix too, not only permanent/
    # not-yet-due work that was never attempted at all.
    seed_result = SyncFollowUpObservations(
        _FakeDecisionSource(seed_decisions),
        _FakeSubjectSource(seed_projections),
        _FakeCryptoPriceSource(dict(seed_closes)),
        _FakeKalshiSource({}),
        repository,
    ).execute(
        as_of=NOW, sample_limit=100, candidate_scan_limit=100, policy_version=FOLLOWUP_POLICY_V2
    )
    assert seed_result.created_count == 4  # 2 decisions x {ORIGIN_EVIDENCE, DECISION_TIME}

    all_decisions = _FakeDecisionSource(
        (
            *seed_decisions,
            *permanent_decisions,
            *not_yet_due_decisions,
            *later_crypto_decisions,
            *later_kalshi_decisions,
        )
    )
    all_subjects = _FakeSubjectSource(
        {
            **seed_projections,
            **permanent_projections,
            **not_yet_due_projections,
            **later_crypto_projections,
            **later_kalshi_projections,
        }
    )
    crypto_prices = _FakeCryptoPriceSource({**seed_closes, **later_crypto_closes})
    kalshi_source = _FakeKalshiSource(later_kalshi_reads)
    sync = SyncFollowUpObservations(
        all_decisions, all_subjects, crypto_prices, kalshi_source, repository
    )

    # Oldest-sorting non-actionable prefix: seed (8, already resolved above)
    # + permanent (15 x 4 = 60) + not-yet-due (5 x 4 = 20) = 88 candidates
    # that are never a fresh genuine attempt -- deliberately larger than
    # candidate_scan_limit below, and larger than any single cycle's bound,
    # so reaching the later cohorts genuinely requires this fix's cursor.
    candidate_scan_limit = 20
    assert candidate_scan_limit < 88

    total_created = 0
    for cycle_index in range(1, 16):
        result = sync.execute(
            as_of=NOW + timedelta(hours=cycle_index),
            sample_limit=100,
            candidate_scan_limit=candidate_scan_limit,
            policy_version=FOLLOWUP_POLICY_V2,
        )
        assert result.candidates_examined == candidate_scan_limit, f"cycle {cycle_index}"
        total_created += result.created_count

    # All 9 genuinely satisfiable later pairs (3 CRYPTO x 2 baseline kinds +
    # 3 KALSHI x 1 baseline kind) were eventually created.
    assert total_created == 9
    later_crypto_ids = {item.decision_id for item in later_crypto_decisions}
    later_kalshi_ids = {item.decision_id for item in later_kalshi_decisions}
    permanent_ids = {item.decision_id for item in permanent_decisions}
    not_yet_due_ids = {item.decision_id for item in not_yet_due_decisions}
    retained_decision_ids = {observation.decision_id for observation in repository._rows.values()}

    # Every later-satisfiable decision got at least one retained row.
    assert later_crypto_ids <= retained_decision_ids
    assert later_kalshi_ids <= retained_decision_ids
    # Neither the permanently-blocked nor the not-yet-due cohort ever
    # produced a fabricated row -- proven, not merely assumed, after 15
    # repeated cycles against the same repository.
    assert not permanent_ids & retained_decision_ids
    assert not not_yet_due_ids & retained_decision_ids
    # Exactly the seed (4) + later-crypto (3 x 2) + later-kalshi (3 x 1) rows
    # -- nothing more, nothing fabricated.
    assert len(repository._rows) == 4 + 6 + 3


def test_sync_permanently_unavailable_classification_is_per_product_not_global_floor() -> None:
    """Final-review Section 4 audit: the ``PERMANENTLY_UNAVAILABLE`` proof
    (``domain.counterfactual_followup.FollowUpDueState.
    PERMANENTLY_UNAVAILABLE``) is a per-*product* argument ("once no
    retained batch for *this* product satisfies the gate as of *this*
    boundary, none ever will"), never a claim that CRYPTO's entire
    permanently-blocked population is capped at whatever it was on Apex's
    own first retained CRYPTO day.  Apex's tracked crypto universe is
    re-ranked by volume every ``CRYPTO_UNIVERSE_REFRESH`` cycle (``domain.
    crypto_universe.materialize_universe``) -- a product ineligible today
    can become newly eligible, and therefore newly captured for the first
    time, long after Apex's own overall first retained day.  Proven here
    directly: a second product (``SOL-USD``) whose own first-ever retained
    batch lands 30 days after ``FLOOR_DAY`` (Apex's BTC-USD floor, deeply
    retained and irrelevant to a SOL-USD lookup) still produces a genuine,
    new ``PERMANENTLY_UNAVAILABLE`` instance for a decision evaluated at or
    before *that* product's own floor -- a cohort member that could not
    exist if the proof secretly depended on one global date."""

    late_onboarded_boundary = FLOOR_DAY + timedelta(days=30)  # SOL-USD's own first capture
    decision = _decision(
        decision_id="late-onboard-decision",
        subject_projection_id="late-onboard-proj",
        evaluated_at=late_onboarded_boundary,
    )
    projection = _projection(
        projection_id="late-onboard-proj",
        product_or_market_id="SOL-USD",
        generated_at=late_onboarded_boundary,
        expected_holding_days=None,
    )
    closes = {
        # BTC-USD has deep, genuinely retained history back to Apex's own
        # overall floor -- present in this fixture specifically to prove it
        # is irrelevant: a SOL-USD lookup must never be satisfied by another
        # product's evidence.
        ("BTC-USD", FLOOR_DAY): CryptoPriceLookup(
            bucket_start=FLOOR_DAY,
            close=Decimal("100"),
            source_observation_id="btc-long-history",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
        # SOL-USD's only retained candle is dated *after* this decision's
        # own boundary (its own first-ever capture landed one day too late
        # for this exact decision) -- zero qualifying evidence before the
        # boundary, exactly the simplest sufficient PERMANENTLY_UNAVAILABLE
        # condition, now for a product onboarded 30 days into Apex's life.
        ("SOL-USD", late_onboarded_boundary + timedelta(days=1)): CryptoPriceLookup(
            bucket_start=late_onboarded_boundary + timedelta(days=1),
            close=Decimal("20"),
            source_observation_id="sol-first-ever-batch",
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        ),
    }
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource({"late-onboard-proj": projection})
    crypto_prices = _FakeCryptoPriceSource(closes)
    repository = _FakeRepository()
    result = SyncFollowUpObservations(
        decisions, subjects, crypto_prices, _FakeKalshiSource({}), repository
    ).execute(
        as_of=late_onboarded_boundary + timedelta(days=2),
        sample_limit=25,
        candidate_scan_limit=25,
        policy_version=FOLLOWUP_POLICY_V2,
    )
    assert result.candidates_examined == 4  # SHORT_HORIZON x2 + STRATEGY_HORIZON x2
    # SHORT_HORIZON x {ORIGIN_EVIDENCE, DECISION_TIME}: both genuinely
    # permanently unavailable, 30 days after Apex's own BTC-USD floor.
    assert result.permanently_unavailable_count == 2
    assert result.skipped_unsupported_count == 2  # STRATEGY_HORIZON, expected_holding_days is None
    assert result.created_count == 0
    assert repository._rows == {}


# ==========================================================================
# Counterfactual Forward Outcome Temporal Integrity v1 -- FOLLOWUP_POLICY_V3.
#
# Acceptance ids (A1..A20) refer to
# ``docs/COUNTERFACTUAL_FORWARD_OUTCOME_TEMPORAL_INTEGRITY_V1.md``'s own
# acceptance matrix; the database-level halves of A11/A12/A13 and all of
# A21..A23 live in ``tests/persistence`` where a real schema exists to
# enforce them.
# ==========================================================================

# An intraday anchor, deliberately: every defect FOLLOWUP_POLICY_V3 corrects
# is invisible for an anchor that happens to fall exactly on a UTC midnight.
V3_ANCHOR = datetime(2026, 8, 2, 3, 3, tzinfo=UTC)
V3_WINDOW_START = datetime(2026, 8, 3, tzinfo=UTC)  # W0: first boundary AT OR AFTER the anchor
V3_WINDOW_END = datetime(2026, 8, 4, tzinfo=UTC)  # W1: W0 + 1 day
V3_START_BUCKET = datetime(2026, 8, 2, tzinfo=UTC)  # close represents W0
V3_END_BUCKET = datetime(2026, 8, 3, tzinfo=UTC)  # close represents W1
V3_DECISION_INPUT_BUCKET = datetime(2026, 8, 1, tzinfo=UTC)
V3_MEASUREMENT_AS_OF = datetime(2026, 8, 5, tzinfo=UTC)


def _v3_crypto_kwargs(**overrides: object) -> dict[str, object]:
    """``_crypto_kwargs`` with every FOLLOWUP_POLICY_V3 fact supplied and
    mutually consistent -- W0/W1 really are the window the anchor implies,
    and the decision-input reference really is a different price from both
    endpoints (so a test can prove it never leaks into the return)."""

    values = _crypto_kwargs(
        policy_version=FOLLOWUP_POLICY_V3,
        baseline_time=V3_WINDOW_START,
        target_time=V3_WINDOW_END,
        horizon_days=1,
        price_at_baseline=Decimal("90"),
        price_at_target=Decimal("99"),
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=V3_MEASUREMENT_AS_OF,
        measurement_as_of=V3_MEASUREMENT_AS_OF,
        policy_anchor_at=V3_ANCHOR,
        decision_input_reference_price=Decimal("80"),
        decision_input_reference_bucket_start=V3_DECISION_INPUT_BUCKET,
        decision_input_source_observation_id="crypto-obs-decision-input",
        decision_input_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        decision=_decision(evaluated_at=V3_ANCHOR),
    )
    values.update(overrides)
    return values


# -- A1/A2/A3: the economic-window contract ---------------------------------


def test_forward_window_start_is_the_first_boundary_at_or_after_an_intraday_anchor() -> None:
    """A1.  The direct correction to FOLLOWUP_POLICY_V2's defect: a ceiling,
    never ``completed_utc_day``'s floor.  For this anchor V2 measured from
    ``2026-08-02T00:00Z`` -- 3h03m of genuinely *pre*-anchor movement inside
    a number presented as a subsequent return."""

    assert first_daily_close_boundary_at_or_after(V3_ANCHOR) == V3_WINDOW_START
    assert completed_utc_day(V3_ANCHOR) == datetime(2026, 8, 2, tzinfo=UTC)
    assert first_daily_close_boundary_at_or_after(V3_ANCHOR) > completed_utc_day(V3_ANCHOR)
    assert first_daily_close_boundary_at_or_after(
        datetime(2026, 8, 2, 21, 40, tzinfo=UTC)
    ) == datetime(2026, 8, 3, tzinfo=UTC)


def test_forward_window_start_equals_an_anchor_already_on_a_boundary() -> None:
    """A2.  A ceiling must not push an anchor that is *already* a daily-close
    boundary forward by a whole day -- that would be a different defect in
    the opposite direction."""

    midnight = datetime(2026, 8, 2, tzinfo=UTC)
    assert first_daily_close_boundary_at_or_after(midnight) == midnight
    window = forward_measurement_window(policy_anchor_at=midnight, horizon_days=1)
    assert window.window_start == midnight
    assert window.window_end == datetime(2026, 8, 3, tzinfo=UTC)
    assert window.window_start_bucket == datetime(2026, 8, 1, tzinfo=UTC)
    assert window.window_end_bucket == midnight


def test_forward_window_never_precedes_its_anchor_and_spans_exactly_the_horizon() -> None:
    """A1 + A3, over a deterministic sweep rather than a single chosen
    example: the two properties the whole milestone rests on.
    ``window_start >= anchor`` means no pre-anchor movement can enter the
    measured interval; the exact span means a "SHORT_HORIZON = 1 day"
    observation genuinely measures one day, never one-plus-a-labeling-
    offset.  The offsets deliberately include 0 (an anchor exactly on a
    boundary), one second past a boundary, one second before the next one,
    and several whole days out."""

    offsets = (0, 1, 59, 3600, 43200, 86399, 86400, 86401, 200_000, 1_000_000, 9_999_999)
    horizons = (0, 1, 2, 3, 7, 14, 30)
    for offset_seconds in offsets:
        for horizon_days in horizons:
            _assert_forward_window_properties(offset_seconds, horizon_days)


def _assert_forward_window_properties(offset_seconds: int, horizon_days: int) -> None:
    anchor = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=offset_seconds)
    window = forward_measurement_window(policy_anchor_at=anchor, horizon_days=horizon_days)
    assert window.window_start >= anchor
    assert window.window_start - anchor < timedelta(days=1)
    assert window.window_end - window.window_start == timedelta(days=horizon_days)
    # The two buckets actually read are exactly ``horizon_days`` apart too --
    # the labeling offset appears on both sides and cancels, which is the
    # property FOLLOWUP_POLICY_V2's target-bucket derivation lost.
    assert window.window_end_bucket - window.window_start_bucket == timedelta(days=horizon_days)
    assert daily_close_bucket_for_boundary(window.window_start) == window.window_start_bucket
    assert daily_close_bucket_for_boundary(window.window_end) == window.window_end_bucket


def test_forward_measurement_window_rejects_a_negative_horizon() -> None:
    with pytest.raises(CounterfactualFollowUpError) as error:
        forward_measurement_window(policy_anchor_at=V3_ANCHOR, horizon_days=-1)
    assert "CRYPTO_FORWARD_WINDOW_HORIZON_DAYS_INVALID" in str(error.value)


# -- A11/A13/A15: the builder's V3 contract ---------------------------------


def test_v3_observation_retains_every_required_fact_and_only_measures_the_window() -> None:
    """A15.  The decision-input reference (80) is retained as disclosure and
    is *not* an endpoint: the return is computed purely from the two window
    closes (90 -> 99 == +10%).  Under FOLLOWUP_POLICY_V2 that 80 *was* the
    baseline endpoint, which is exactly what made a V2 return blend
    pre-anchor drift into a post-anchor measurement."""

    obs = build_crypto_price_movement_observation(**_v3_crypto_kwargs())
    assert obs.policy_version == FOLLOWUP_POLICY_V3
    assert obs.subsequent_return == Decimal("0.1")
    assert obs.reference_price_at_baseline == Decimal("90")
    assert obs.reference_price_at_target == Decimal("99")
    assert obs.decision_input_reference_price == Decimal("80")
    assert obs.decision_input_reference_bucket_start == V3_DECISION_INPUT_BUCKET
    assert obs.decision_input_source_observation_id == "crypto-obs-decision-input"
    assert obs.decision_input_evidence_class is EvidenceClass.HISTORICAL_PUBLIC
    assert obs.measurement_as_of == V3_MEASUREMENT_AS_OF
    assert obs.policy_anchor_at == V3_ANCHOR
    # A1, restated on the retained row itself: a reader can verify the
    # no-pre-anchor-movement property without re-deriving anything.
    assert obs.baseline_time >= obs.policy_anchor_at
    assert obs.target_time - obs.baseline_time == timedelta(days=obs.horizon_days)
    assert CRYPTO_MEASUREMENT_AS_OF_BOUNDED_LIMITATION in obs.data_limitations
    assert CRYPTO_DECISION_INPUT_REFERENCE_NOT_ENDPOINT_LIMITATION in obs.data_limitations
    # A16: baseline staleness is derivable from the anchor plus the retained
    # source bucket -- never redundantly persisted as a duration.
    decision_input_close_represents = (
        obs.decision_input_reference_bucket_start + timedelta(days=1)
        if obs.decision_input_reference_bucket_start is not None
        else None
    )
    assert decision_input_close_represents == datetime(2026, 8, 2, tzinfo=UTC)
    assert obs.policy_anchor_at - decision_input_close_represents == timedelta(hours=3, minutes=3)
    # The decision-input batch is retained in its own dedicated field, and
    # deliberately NOT appended to ``source_lineage`` -- see the builder's
    # own comment for why doing so would make that tuple's documented
    # positional contract ambiguous for a ``horizon_days == 0`` row.
    assert obs.source_lineage == ("crypto-obs-1", "crypto-obs-2")
    assert obs.decision_input_source_observation_id == "crypto-obs-decision-input"


@pytest.mark.parametrize(
    "missing",
    [
        "measurement_as_of",
        "policy_anchor_at",
        "decision_input_reference_price",
        "decision_input_reference_bucket_start",
        "decision_input_source_observation_id",
        "decision_input_evidence_class",
    ],
)
def test_v3_rejects_a_missing_required_fact(missing: str) -> None:
    """A11 (domain half).  All six are an all-or-nothing contract -- any one
    of them missing must fail at the source, never produce a half-specified
    V3 row that the hash block and the database check would then disagree
    about."""

    with pytest.raises(CounterfactualFollowUpError) as error:
        build_crypto_price_movement_observation(**_v3_crypto_kwargs(**{missing: None}))
    assert CRYPTO_FOLLOWUP_V3_REQUIRED_FACT_MISSING in str(error.value)


@pytest.mark.parametrize("policy_version", [FOLLOWUP_POLICY_V1, FOLLOWUP_POLICY_V2])
def test_v3_facts_are_rejected_on_an_earlier_policy_version(policy_version: str) -> None:
    """A12 (domain half).  Validated in *both* directions: a legacy row that
    somehow acquired a ``measurement_as_of`` would break the canonical-hash
    block invariant and violate migration 0113's biconditional check."""

    with pytest.raises(CounterfactualFollowUpError) as error:
        build_crypto_price_movement_observation(
            **_crypto_kwargs(
                policy_version=policy_version,
                baseline_kind=FollowUpBaselineKind.DECISION_TIME,
                measurement_as_of=V3_MEASUREMENT_AS_OF,
            )
        )
    assert CRYPTO_FOLLOWUP_V3_FACT_ON_NON_V3_POLICY in str(error.value)


def test_v3_rejects_a_non_crypto_decision() -> None:
    """A13 (domain half, crypto builder).  The builder does not rely on its
    only caller behaving."""

    with pytest.raises(CounterfactualFollowUpError) as error:
        build_crypto_price_movement_observation(
            **_v3_crypto_kwargs(decision=_kalshi_decision(evaluated_at=V3_ANCHOR))
        )
    assert CRYPTO_FOLLOWUP_V3_VENUE_UNSUPPORTED in str(error.value)


def test_v3_rejects_a_window_that_precedes_its_anchor() -> None:
    with pytest.raises(CounterfactualFollowUpError) as error:
        build_crypto_price_movement_observation(
            **_v3_crypto_kwargs(
                baseline_time=V3_ANCHOR - timedelta(hours=1),
                target_time=V3_ANCHOR + timedelta(days=1) - timedelta(hours=1),
            )
        )
    assert CRYPTO_FOLLOWUP_V3_WINDOW_PRECEDES_ANCHOR in str(error.value)


def test_v3_rejects_a_window_span_that_is_not_the_declared_horizon() -> None:
    with pytest.raises(CounterfactualFollowUpError) as error:
        build_crypto_price_movement_observation(
            **_v3_crypto_kwargs(target_time=V3_WINDOW_END + timedelta(days=1))
        )
    assert CRYPTO_FOLLOWUP_V3_WINDOW_SPAN_INVALID in str(error.value)


def test_v3_rejects_a_measurement_instant_before_the_window_has_closed() -> None:
    with pytest.raises(CounterfactualFollowUpError) as error:
        build_crypto_price_movement_observation(
            **_v3_crypto_kwargs(measurement_as_of=V3_WINDOW_END - timedelta(seconds=1))
        )
    assert CRYPTO_FOLLOWUP_V3_MEASUREMENT_AS_OF_PRECEDES_WINDOW in str(error.value)


def test_v3_rejects_an_ineligible_decision_input_evidence_class() -> None:
    with pytest.raises(CounterfactualFollowUpError) as error:
        build_crypto_price_movement_observation(
            **_v3_crypto_kwargs(decision_input_evidence_class=EvidenceClass.FIXTURE)
        )
    assert CRYPTO_FOLLOWUP_DECISION_INPUT_EVIDENCE_INELIGIBLE in str(error.value)


def test_kalshi_and_securities_builders_reject_v3_outright() -> None:
    """A13 (domain half, the two non-CRYPTO builders).  FOLLOWUP_POLICY_V3 is
    a daily-candle forward-window contract; neither venue has a candle
    window for it to measure over."""

    with pytest.raises(CounterfactualFollowUpError) as error:
        build_kalshi_market_status_observation(**_kalshi_kwargs(policy_version=FOLLOWUP_POLICY_V3))
    assert FOLLOWUP_V3_POLICY_VERSION_UNSUPPORTED_FOR_VENUE in str(error.value)


# -- A10/A14: identity and hash ---------------------------------------------


def test_v3_measurement_as_of_is_hashed_and_observed_at_is_still_not() -> None:
    """A10.  ``measurement_as_of`` is a declared fact that changes what a row
    *means*, so it is hashed; ``observed_at`` is merely when the process ran,
    so it stays excluded exactly as it always has been.  Two rows that
    measured the identical window under the identical availability bound are
    the same fact even when they were computed hours apart."""

    early = build_crypto_price_movement_observation(
        **_v3_crypto_kwargs(observed_at=V3_MEASUREMENT_AS_OF)
    )
    late = build_crypto_price_movement_observation(
        **_v3_crypto_kwargs(observed_at=V3_MEASUREMENT_AS_OF + timedelta(days=9))
    )
    assert early.observed_at != late.observed_at
    assert early.canonical_hash == late.canonical_hash
    assert early.observation_id == late.observation_id

    later_bound = build_crypto_price_movement_observation(
        **_v3_crypto_kwargs(measurement_as_of=V3_MEASUREMENT_AS_OF + timedelta(days=1))
    )
    # Same identity (a replay of the same measurement), genuinely different
    # fact -- which is precisely why an identity-conflicting replay must be
    # rejected by the repository rather than silently overwriting.
    assert later_bound.observation_id == early.observation_id
    assert later_bound.canonical_hash != early.canonical_hash


def test_v3_identity_is_distinct_from_every_earlier_policy_version() -> None:
    ids = {
        version: followup_observation_id(
            decision_id="d1",
            policy_version=version,
            horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
            baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        )
        for version in (FOLLOWUP_POLICY_V1, FOLLOWUP_POLICY_V2, FOLLOWUP_POLICY_V3)
    }
    assert len(set(ids.values())) == 3


# Captured from the pre-FOLLOWUP_POLICY_V3 code (commit ``ab35718``) by
# running the identical construction below against that checkout, then pinned
# here as literals.  This is the offline half of A14: it proves the V3 hash
# block is *structurally* omitted for every earlier version -- not merely
# "unchanged because nobody passed a new value" -- so no already-retained
# V1/V2 row of any venue can have its hash perturbed by this milestone.
_LEGACY_V1_ID = "decision_followup_observation_4f2d07f811c713038820b340"
_LEGACY_V1_HASH = "1fd481626c7e9b7e865388a5b2e4f324006901dfee09372106670e8e7f904285"
_LEGACY_V2_DECISION_TIME_ID = "decision_followup_observation_7be4016958a84a54321fd743"
_LEGACY_V2_DECISION_TIME_HASH = "25401cbddaf33310324715bdd3360b83667d810e440d96ff5f844ee7f316c8b7"
_LEGACY_V2_ORIGIN_ID = "decision_followup_observation_d49235bbccede6f1377aebc5"
_LEGACY_V2_ORIGIN_HASH = "11836b2e84a95cff7d983ad4994719dc366f75a98b9057fa14891b013dfbc6e6"

_LEGACY_DAY = datetime(2026, 7, 3, tzinfo=UTC)
_LEGACY_HASH_PAYLOAD: dict[str, object] = {
    "decision_id": "dec-legacy",
    "decision_canonical_hash": "hash-legacy",
    "venue": SharedVenue.CRYPTO,
    "disposition": DecisionDisposition.ADVANCED,
    "horizon_kind": FollowUpHorizonKind.SHORT_HORIZON,
    "horizon_days": 1,
    "decision_evaluated_at": _LEGACY_DAY,
    "target_time": datetime(2026, 7, 4, tzinfo=UTC),
    "observation_type": FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT,
    "subject_instrument_id": "BTC",
    "subject_product_or_market_id": "BTC-USD",
    "provenance": EvidenceClass.HISTORICAL_PUBLIC,
    "source_lineage": ("batch-a", "batch-b"),
    "data_limitations": (CRYPTO_DAILY_CLOSE_LIMITATION,),
    "reference_price_at_baseline": Decimal("95"),
    "reference_price_at_target": Decimal("100"),
    "subsequent_return": Decimal("0.05263158"),
    "market_status_at_target": None,
    "yes_bid_at_target": None,
    "yes_ask_at_target": None,
    "settlement_result": None,
    "settlement_source": None,
}


def test_every_pre_v3_identity_and_hash_is_reproduced_byte_for_byte() -> None:
    """A14.  Recomputing a representative V1, V2/``DECISION_TIME`` and
    V2/``ORIGIN_EVIDENCE`` row under today's code reproduces the exact digest
    the pre-V3 code produced."""

    v1_id = followup_observation_id(
        decision_id="dec-legacy",
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    assert v1_id == _LEGACY_V1_ID
    assert (
        followup_observation_canonical_hash(
            observation_id=v1_id,
            policy_version=FOLLOWUP_POLICY_V1,
            baseline_kind=FollowUpBaselineKind.DECISION_TIME,
            baseline_time=_LEGACY_DAY,
            **_LEGACY_HASH_PAYLOAD,  # type: ignore[arg-type]
        )
        == _LEGACY_V1_HASH
    )

    v2_decision_time_id = followup_observation_id(
        decision_id="dec-legacy",
        policy_version=FOLLOWUP_POLICY_V2,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    assert v2_decision_time_id == _LEGACY_V2_DECISION_TIME_ID
    assert (
        followup_observation_canonical_hash(
            observation_id=v2_decision_time_id,
            policy_version=FOLLOWUP_POLICY_V2,
            baseline_kind=FollowUpBaselineKind.DECISION_TIME,
            baseline_time=_LEGACY_DAY,
            baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            evidence_eligibility_policy_version=CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1,
            **_LEGACY_HASH_PAYLOAD,  # type: ignore[arg-type]
        )
        == _LEGACY_V2_DECISION_TIME_HASH
    )

    v2_origin_id = followup_observation_id(
        decision_id="dec-legacy",
        policy_version=FOLLOWUP_POLICY_V2,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
    )
    assert v2_origin_id == _LEGACY_V2_ORIGIN_ID
    assert (
        followup_observation_canonical_hash(
            observation_id=v2_origin_id,
            policy_version=FOLLOWUP_POLICY_V2,
            baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
            baseline_time=_LEGACY_DAY,
            baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            evidence_eligibility_policy_version=CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1,
            **_LEGACY_HASH_PAYLOAD,  # type: ignore[arg-type]
        )
        == _LEGACY_V2_ORIGIN_HASH
    )


def test_passing_explicit_none_v3_facts_is_identical_to_omitting_them() -> None:
    """The hash block must be gated on ``measurement_as_of`` alone, so an
    explicit ``None`` and an omission are the same call -- the exact failure
    mode a prior milestone hit by appending an optional element
    unconditionally (see ``followup_observation_canonical_hash``)."""

    omitted = followup_observation_canonical_hash(
        observation_id=_LEGACY_V1_ID,
        policy_version=FOLLOWUP_POLICY_V1,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        baseline_time=_LEGACY_DAY,
        **_LEGACY_HASH_PAYLOAD,  # type: ignore[arg-type]
    )
    explicit = followup_observation_canonical_hash(
        observation_id=_LEGACY_V1_ID,
        policy_version=FOLLOWUP_POLICY_V1,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        baseline_time=_LEGACY_DAY,
        measurement_as_of=None,
        policy_anchor_at=None,
        decision_input_reference_price=None,
        decision_input_reference_bucket_start=None,
        decision_input_source_observation_id=None,
        decision_input_evidence_class=None,
        **_LEGACY_HASH_PAYLOAD,  # type: ignore[arg-type]
    )
    assert omitted == explicit == _LEGACY_V1_HASH


# -- A4/A5/A6/A8/A9/A19/A20: the application measurement path ---------------


def _v3_lookup(bucket_start: datetime, close: str, observation_id: str) -> CryptoPriceLookup:
    return CryptoPriceLookup(
        close=Decimal(close),
        source_observation_id=observation_id,
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        bucket_start=bucket_start,
    )


def _v3_sync_fixture(
    *,
    closes: dict[tuple[str, datetime], CryptoPriceLookup] | None = None,
    completed_at: dict[tuple[str, datetime], datetime] | None = None,
) -> tuple[_FakeDecisionSource, _FakeSubjectSource, _FakeCryptoPriceSource, _FakeRepository]:
    decision = _decision(decision_id="d-v3", evaluated_at=V3_ANCHOR)
    decisions = _FakeDecisionSource((decision,))
    subjects = _FakeSubjectSource(
        {"proj-1": _projection(generated_at=V3_ANCHOR, expected_holding_days=1)}
    )
    prices = _FakeCryptoPriceSource(
        {
            ("BTC-USD", V3_DECISION_INPUT_BUCKET): _v3_lookup(
                V3_DECISION_INPUT_BUCKET, "80", "batch-decision-input"
            ),
            ("BTC-USD", V3_START_BUCKET): _v3_lookup(V3_START_BUCKET, "90", "batch-window-start"),
            ("BTC-USD", V3_END_BUCKET): _v3_lookup(V3_END_BUCKET, "99", "batch-window-end"),
            # Decoy: the bucket FOLLOWUP_POLICY_V2's own target derivation
            # would have reached.  A regression back to it would still create
            # an observation -- only the stamped price catches it.
            ("BTC-USD", V3_WINDOW_END): _v3_lookup(V3_WINDOW_END, "500", "batch-decoy"),
        }
        if closes is None
        else closes,
        completed_at,
    )
    return decisions, subjects, prices, _FakeRepository()


def _v3_sync(
    decisions: _FakeDecisionSource,
    subjects: _FakeSubjectSource,
    prices: _FakeCryptoPriceSource,
    repository: _FakeRepository,
    *,
    measurement_as_of: datetime = V3_MEASUREMENT_AS_OF,
):  # type: ignore[no-untyped-def]
    return SyncFollowUpObservations(
        decisions, subjects, prices, _FakeKalshiSource(), repository
    ).execute(
        as_of=V3_MEASUREMENT_AS_OF,
        policy_version=FOLLOWUP_POLICY_V3,
        venues=(SharedVenue.CRYPTO,),
        measurement_as_of=measurement_as_of,
    )


def test_v3_sync_measures_the_declared_window_and_never_the_decision_input_price() -> None:
    """A4 + A15, through the real orchestration rather than the builder: both
    endpoints come from the exact derived buckets, and the decoy bucket that
    FOLLOWUP_POLICY_V2's target derivation would have reached (500) never
    appears anywhere in the retained row."""

    decisions, subjects, prices, repository = _v3_sync_fixture()
    result = _v3_sync(decisions, subjects, prices, repository)

    # 2 horizon kinds x 2 baseline kinds.  ``expected_holding_days == 1``
    # here, so STRATEGY_HORIZON and SHORT_HORIZON share one window, and the
    # fixture's ``generated_at == evaluated_at`` makes both baseline kinds
    # share one anchor -- four genuinely distinct identities measuring the
    # same window, which is exactly what makes the price assertions below a
    # clean test of the window contract rather than of the anchor plumbing.
    assert result.created_count == 4
    observations = list(repository._rows.values())
    assert {item.baseline_kind for item in observations} == {
        FollowUpBaselineKind.ORIGIN_EVIDENCE,
        FollowUpBaselineKind.DECISION_TIME,
    }
    for observation in observations:
        assert observation.policy_version == FOLLOWUP_POLICY_V3
        assert observation.baseline_time == V3_WINDOW_START
        assert observation.target_time == V3_WINDOW_END
        assert observation.reference_price_at_baseline == Decimal("90")
        assert observation.reference_price_at_target == Decimal("99")
        assert observation.subsequent_return == Decimal("0.1")
        assert observation.decision_input_reference_price == Decimal("80")
        assert observation.measurement_as_of == V3_MEASUREMENT_AS_OF
        assert observation.policy_anchor_at == V3_ANCHOR
    # Every measurement lookup was bounded by the declared instant.
    assert prices.measurement_as_of_calls
    assert set(prices.measurement_as_of_calls) == {V3_MEASUREMENT_AS_OF}


def test_v3_sync_fails_closed_when_an_exact_endpoint_bucket_is_absent() -> None:
    """A4 + A20.  A missing endpoint is never substituted with a neighbouring
    bucket, and it stays *transiently* blocked -- never re-classified
    permanent, because V3's availability gate advances with
    ``measurement_as_of``."""

    decisions, subjects, prices, repository = _v3_sync_fixture(
        closes={
            ("BTC-USD", V3_DECISION_INPUT_BUCKET): _v3_lookup(
                V3_DECISION_INPUT_BUCKET, "80", "batch-decision-input"
            ),
            ("BTC-USD", V3_START_BUCKET): _v3_lookup(V3_START_BUCKET, "90", "batch-window-start"),
            # window-end bucket deliberately absent; a neighbour is present
            # and must never be substituted for it.
            ("BTC-USD", V3_WINDOW_END): _v3_lookup(V3_WINDOW_END, "500", "batch-decoy"),
        }
    )
    result = _v3_sync(decisions, subjects, prices, repository)

    assert result.created_count == 0
    assert result.blocked_count == 4
    assert result.permanently_unavailable_count == 0
    assert all(
        entry.reasons == (CRYPTO_V3_WINDOW_END_MEASUREMENT_UNAVAILABLE,) for entry in result.entries
    )


def test_v3_sync_classifies_a_missing_decision_input_reference_as_permanent() -> None:
    """A19.  The one provably permanent CRYPTO condition survives V3
    unchanged -- its boundary is a frozen, anchor-derived instant, so once no
    retained batch satisfies it none ever will.  Classified at zero
    attempt/provider cost, exactly as Counterfactual Follow-Up Starvation Fix
    v1 requires, and under its own V3-specific reason string."""

    decisions, subjects, prices, repository = _v3_sync_fixture(
        closes={
            ("BTC-USD", V3_START_BUCKET): _v3_lookup(V3_START_BUCKET, "90", "batch-window-start"),
            ("BTC-USD", V3_END_BUCKET): _v3_lookup(V3_END_BUCKET, "99", "batch-window-end"),
        }
    )
    result = _v3_sync(decisions, subjects, prices, repository)

    assert result.created_count == 0
    assert result.genuine_attempts == 0  # zero budget spent
    assert result.provider_request_count == 0
    assert result.permanently_unavailable_count == 4
    assert all(
        entry.reasons == (CRYPTO_DECISION_INPUT_REFERENCE_PERMANENTLY_UNAVAILABLE,)
        for entry in result.entries
    )


def test_v3_replay_at_the_same_measurement_instant_is_unchanged_by_a_newer_batch() -> None:
    """A8 -- the property the whole milestone exists for.  Measure, then let a
    genuinely newer batch land that covers the identical window-end bucket
    with a different close, then replay at the *original*
    ``measurement_as_of``: the answer is byte-for-byte the same, because the
    newer batch's ``completed_at`` puts it outside the declared bound."""

    closes = {
        ("BTC-USD", V3_DECISION_INPUT_BUCKET): _v3_lookup(
            V3_DECISION_INPUT_BUCKET, "80", "batch-decision-input"
        ),
        ("BTC-USD", V3_START_BUCKET): _v3_lookup(V3_START_BUCKET, "90", "batch-window-start"),
        ("BTC-USD", V3_END_BUCKET): _v3_lookup(V3_END_BUCKET, "99", "batch-window-end"),
    }
    completed_at = {
        ("BTC-USD", V3_DECISION_INPUT_BUCKET): V3_DECISION_INPUT_BUCKET + timedelta(hours=1),
        ("BTC-USD", V3_START_BUCKET): V3_START_BUCKET + timedelta(hours=1),
        ("BTC-USD", V3_END_BUCKET): V3_END_BUCKET + timedelta(hours=1),
    }
    decisions, subjects, prices, repository = _v3_sync_fixture(
        closes=closes, completed_at=completed_at
    )
    first = _v3_sync(decisions, subjects, prices, repository)
    assert first.created_count == 4
    original = {key: item.canonical_hash for key, item in repository._rows.items()}

    # A genuinely newer, genuinely different batch covering the identical
    # bucket lands afterwards.
    closes[("BTC-USD", V3_END_BUCKET)] = _v3_lookup(V3_END_BUCKET, "777", "batch-recaptured")
    completed_at[("BTC-USD", V3_END_BUCKET)] = V3_MEASUREMENT_AS_OF + timedelta(days=3)

    replay = _v3_sync(decisions, subjects, prices, repository)
    # Idempotent replay of an identical fact: retained, unchanged, no
    # identity conflict.
    assert replay.created_count == 0
    assert {key: item.canonical_hash for key, item in repository._rows.items()} == original


def test_v3_measurement_becomes_available_only_once_its_batch_has_completed() -> None:
    """A9.  The complement of A8: a later declared bound may legitimately
    succeed where an earlier one could not.  The earlier bound stays
    unavailable forever, which is exactly what makes the earlier answer
    honest rather than merely stale."""

    closes = {
        ("BTC-USD", V3_DECISION_INPUT_BUCKET): _v3_lookup(
            V3_DECISION_INPUT_BUCKET, "80", "batch-decision-input"
        ),
        ("BTC-USD", V3_START_BUCKET): _v3_lookup(V3_START_BUCKET, "90", "batch-window-start"),
        ("BTC-USD", V3_END_BUCKET): _v3_lookup(V3_END_BUCKET, "99", "batch-backfilled-late"),
    }
    completed_at = {
        ("BTC-USD", V3_DECISION_INPUT_BUCKET): V3_DECISION_INPUT_BUCKET + timedelta(hours=1),
        ("BTC-USD", V3_START_BUCKET): V3_START_BUCKET + timedelta(hours=1),
        # Arrives after the first declared measurement instant.
        ("BTC-USD", V3_END_BUCKET): V3_MEASUREMENT_AS_OF + timedelta(days=2),
    }
    decisions, subjects, prices, repository = _v3_sync_fixture(
        closes=closes, completed_at=completed_at
    )

    early = _v3_sync(decisions, subjects, prices, repository)
    assert early.created_count == 0
    assert early.blocked_count == 4
    assert all(
        entry.reasons == (CRYPTO_V3_WINDOW_END_MEASUREMENT_UNAVAILABLE,) for entry in early.entries
    )

    later = _v3_sync(
        decisions,
        subjects,
        prices,
        repository,
        measurement_as_of=V3_MEASUREMENT_AS_OF + timedelta(days=3),
    )
    assert later.created_count == 4


def test_v3_observation_passes_its_own_read_time_integrity_check() -> None:
    """A genuine, untampered V3 row must verify against its own
    ``canonical_hash``.  Pinned as its own test because the first draft of
    this milestone did not feed the six V3 facts into
    ``observation_canonical_hash_matches``, which made every genuine V3 row
    fail this check and be permanently, wrongly assessed
    ``INELIGIBLE_EVIDENCE``/``OBSERVATION_HASH_INVALID`` by the recurring
    cycle's own automatic evidence-quality sweep -- a wrong verdict on good
    evidence, not a crash, and therefore exactly the kind of defect only a
    direct assertion catches."""

    observation = build_crypto_price_movement_observation(**_v3_crypto_kwargs())
    assert observation_canonical_hash_matches(observation)
    # And a tampered one does not -- the check is genuinely doing work.
    assert not observation_canonical_hash_matches(
        replace(observation, measurement_as_of=observation.measurement_as_of + timedelta(days=1))
    )
    assert not observation_canonical_hash_matches(
        replace(observation, decision_input_reference_price=Decimal("81"))
    )


def test_v3_observation_is_assessed_eligible_by_the_evidence_quality_policy() -> None:
    """The end-to-end consequence of the check above: a clean V3 row reaches
    ``ELIGIBLE``, so it can genuinely participate in downstream
    counterfactual outcome attribution."""

    observation = build_crypto_price_movement_observation(**_v3_crypto_kwargs())
    assessment = assess_followup_evidence_quality(
        observation=observation,
        baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=V3_MEASUREMENT_AS_OF,
    )
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    assert assessment.evidence_defect_type is FollowUpEvidenceDefectType.NONE
    assert is_evidence_eligible_for_downstream_attribution(assessment)


def test_v3_rejects_a_window_start_that_is_not_the_one_the_anchor_implies() -> None:
    """``baseline_time >= policy_anchor_at`` alone would still admit an
    arbitrarily *late* window stamped under the V3 name -- a different, and
    equally undetectable-after-the-fact, misstatement of what an immutable
    row measured.  The builder restates the derivation itself rather than
    trusting whichever caller performed it."""

    with pytest.raises(CounterfactualFollowUpError) as error:
        build_crypto_price_movement_observation(
            **_v3_crypto_kwargs(
                baseline_time=V3_WINDOW_START + timedelta(days=1),
                target_time=V3_WINDOW_END + timedelta(days=1),
                measurement_as_of=V3_MEASUREMENT_AS_OF + timedelta(days=1),
            )
        )
    assert CRYPTO_FOLLOWUP_V3_WINDOW_START_NOT_DERIVED in str(error.value)
