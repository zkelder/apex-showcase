from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_definition import (
    MINIMUM_PROMOTION_SAMPLE_SIZE,
    StrategyDefinition,
    StrategyDefinitionError,
    StrategyEvidenceRequirements,
    StrategyHorizonKind,
    StrategyLifecycleState,
    StrategyProvenance,
    StrategyRuleKind,
    StrategyRuleParameters,
    StrategySourceKind,
    next_version,
)

NOW = datetime(2026, 8, 25, tzinfo=UTC)

_EXTERNAL_PROVENANCE = StrategyProvenance(
    reference_kind="PUBLISHED_CONVENTION",
    reference_description="Textbook dual moving-average crossover ('golden/death cross').",
    origin_note="Widely published technical-analysis convention; no external performance claim.",
)
_INTERNAL_PROVENANCE = StrategyProvenance(
    reference_kind="INTERNAL_RESEARCH_METHOD",
    reference_description="CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",
    origin_note="Identifies Apex's own existing internal research method for comparison purposes.",
)
_EVIDENCE = StrategyEvidenceRequirements(
    minimum_history_days=200,
    requires_contemporaneous_observation=True,
    maximum_observation_staleness_seconds=1800,
)


def _buy_and_hold(**overrides: object) -> StrategyDefinition:
    kwargs: dict[str, object] = dict(
        source_kind=StrategySourceKind.EXTERNAL,
        display_name="Buy and Hold BTC-USD",
        venue=SharedVenue.CRYPTO,
        instrument_ids=("BTC-USD",),
        horizon_kind=StrategyHorizonKind.DAILY_SIGNAL_INDEFINITE_HOLD,
        rule_kind=StrategyRuleKind.BUY_AND_HOLD,
        rule_parameters=StrategyRuleParameters(),
        evidence_requirements=_EVIDENCE,
        provenance=_EXTERNAL_PROVENANCE,
        created_at=NOW,
    )
    kwargs.update(overrides)
    return StrategyDefinition.create(**kwargs)  # type: ignore[arg-type]


def test_buy_and_hold_creates_research_only_version_one() -> None:
    definition = _buy_and_hold()
    assert definition.version == 1
    assert definition.parent_strategy_id is None
    assert definition.lifecycle_state is StrategyLifecycleState.RESEARCH_ONLY
    assert definition.strategy_id.startswith("strategy_definition_")
    assert definition.created_at == NOW


def test_strategy_id_is_stable_identity_but_canonical_hash_is_full_content() -> None:
    a = _buy_and_hold(display_name="Buy and Hold BTC-USD")
    b = _buy_and_hold(display_name="A Different Display Name")
    assert a.strategy_id == b.strategy_id  # same source/venue/instruments/rule
    assert a.canonical_hash != b.canonical_hash  # different full record


def test_creation_is_deterministic() -> None:
    a = _buy_and_hold()
    b = _buy_and_hold()
    assert a.strategy_id == b.strategy_id
    assert a.canonical_hash == b.canonical_hash


def test_only_crypto_venue_supported() -> None:
    with pytest.raises(StrategyDefinitionError, match="CRYPTO"):
        _buy_and_hold(venue=SharedVenue.KALSHI)


def test_created_at_must_be_timezone_aware() -> None:
    with pytest.raises(StrategyDefinitionError, match="timezone-aware"):
        _buy_and_hold(created_at=datetime(2026, 8, 25))


def test_instrument_ids_must_be_nonempty_and_unique() -> None:
    with pytest.raises(StrategyDefinitionError, match="non-empty"):
        _buy_and_hold(instrument_ids=())
    with pytest.raises(StrategyDefinitionError, match="duplicates"):
        _buy_and_hold(instrument_ids=("BTC-USD", "BTC-USD"))


def test_external_provenance_requires_published_convention_kind() -> None:
    with pytest.raises(StrategyDefinitionError, match="reference_kind"):
        _buy_and_hold(provenance=_INTERNAL_PROVENANCE)


def test_internal_provenance_requires_internal_research_method_kind() -> None:
    with pytest.raises(StrategyDefinitionError, match="reference_kind"):
        _buy_and_hold(
            source_kind=StrategySourceKind.INTERNAL,
            rule_kind=StrategyRuleKind.INTERNAL_METHOD_REFERENCE,
            rule_parameters=StrategyRuleParameters(referenced_internal_method_id="X"),
            provenance=_EXTERNAL_PROVENANCE,
        )


def test_provenance_reference_description_required() -> None:
    with pytest.raises(StrategyDefinitionError, match="reference_description"):
        _buy_and_hold(
            provenance=StrategyProvenance(
                reference_kind="PUBLISHED_CONVENTION",
                reference_description="   ",
                origin_note="note",
            )
        )


def test_version_and_parent_must_agree() -> None:
    with pytest.raises(StrategyDefinitionError, match="parent_strategy_id"):
        _buy_and_hold(version=2, parent_strategy_id=None)
    with pytest.raises(StrategyDefinitionError, match="parent_strategy_id"):
        _buy_and_hold(version=1, parent_strategy_id="strategy_definition_deadbeef")


def test_research_only_lifecycle_state_requires_version_one() -> None:
    with pytest.raises(StrategyDefinitionError, match="RESEARCH_ONLY"):
        _buy_and_hold(
            version=2,
            parent_strategy_id="strategy_definition_deadbeef",
            lifecycle_state=StrategyLifecycleState.RESEARCH_ONLY,
        )


class TestStrategyLifecycleGuard:
    """Recurring Strategy PAPER Orchestration v1, Phase H: version 1 is
    RESEARCH_ONLY if and only if it's version 1 -- a caller can no longer
    fabricate a version-1 definition already in PAPER_ELIGIBLE/ACTIVE/
    RETIRED, bypassing every evidence gate."""

    def test_version_one_cannot_be_paper_eligible_directly(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="RESEARCH_ONLY"):
            _buy_and_hold(version=1, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE)

    def test_version_one_cannot_be_active_directly(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="RESEARCH_ONLY"):
            _buy_and_hold(version=1, lifecycle_state=StrategyLifecycleState.ACTIVE)

    def test_version_one_cannot_be_retired_directly(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="RESEARCH_ONLY"):
            _buy_and_hold(version=1, lifecycle_state=StrategyLifecycleState.RETIRED)


class TestMovingAverageTrendRule:
    def _definition(self, **params: object) -> StrategyDefinition:
        return _buy_and_hold(
            rule_kind=StrategyRuleKind.MOVING_AVERAGE_TREND,
            rule_parameters=StrategyRuleParameters(
                fast_window_days=50, slow_window_days=200, **params
            ),
        )

    def test_valid_dual_window_accepted(self) -> None:
        definition = self._definition()
        assert definition.rule_parameters.fast_window_days == 50
        assert definition.rule_parameters.slow_window_days == 200

    def test_fast_must_be_strictly_less_than_slow(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="fast_window_days"):
            _buy_and_hold(
                rule_kind=StrategyRuleKind.MOVING_AVERAGE_TREND,
                rule_parameters=StrategyRuleParameters(fast_window_days=200, slow_window_days=50),
            )

    def test_windows_below_minimum_rejected(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="at least 2 days"):
            _buy_and_hold(
                rule_kind=StrategyRuleKind.MOVING_AVERAGE_TREND,
                rule_parameters=StrategyRuleParameters(fast_window_days=1, slow_window_days=5),
            )

    def test_missing_windows_rejected(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="fast_window_days"):
            _buy_and_hold(
                rule_kind=StrategyRuleKind.MOVING_AVERAGE_TREND,
                rule_parameters=StrategyRuleParameters(),
            )

    def test_unrelated_parameters_rejected(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="unexpected rule parameters"):
            _buy_and_hold(
                rule_kind=StrategyRuleKind.MOVING_AVERAGE_TREND,
                rule_parameters=StrategyRuleParameters(
                    fast_window_days=50,
                    slow_window_days=200,
                    momentum_lookback_days=90,
                ),
            )


class TestTimeSeriesMomentumRule:
    def test_valid_momentum_accepted(self) -> None:
        definition = _buy_and_hold(
            rule_kind=StrategyRuleKind.TIME_SERIES_MOMENTUM,
            rule_parameters=StrategyRuleParameters(
                momentum_lookback_days=90,
                momentum_entry_threshold=Decimal("0"),
                maximum_holding_days=30,
            ),
        )
        assert definition.rule_parameters.momentum_lookback_days == 90

    def test_missing_threshold_rejected(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="momentum_entry_threshold"):
            _buy_and_hold(
                rule_kind=StrategyRuleKind.TIME_SERIES_MOMENTUM,
                rule_parameters=StrategyRuleParameters(momentum_lookback_days=90),
            )


class TestMeanReversionRule:
    def test_valid_reversion_accepted(self) -> None:
        definition = _buy_and_hold(
            rule_kind=StrategyRuleKind.MEAN_REVERSION,
            rule_parameters=StrategyRuleParameters(
                reversion_lookback_days=20,
                reversion_entry_threshold=Decimal("-0.05"),
            ),
        )
        assert definition.rule_parameters.reversion_entry_threshold == Decimal("-0.05")

    def test_nonnegative_threshold_rejected(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="negative"):
            _buy_and_hold(
                rule_kind=StrategyRuleKind.MEAN_REVERSION,
                rule_parameters=StrategyRuleParameters(
                    reversion_lookback_days=20,
                    reversion_entry_threshold=Decimal("0.05"),
                ),
            )


class TestInternalMethodReference:
    def test_valid_internal_reference_accepted(self) -> None:
        definition = _buy_and_hold(
            source_kind=StrategySourceKind.INTERNAL,
            rule_kind=StrategyRuleKind.INTERNAL_METHOD_REFERENCE,
            rule_parameters=StrategyRuleParameters(
                referenced_internal_method_id="CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1"
            ),
            provenance=_INTERNAL_PROVENANCE,
        )
        assert definition.source_kind is StrategySourceKind.INTERNAL

    def test_requires_internal_source_kind(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="INTERNAL"):
            _buy_and_hold(
                rule_kind=StrategyRuleKind.INTERNAL_METHOD_REFERENCE,
                rule_parameters=StrategyRuleParameters(referenced_internal_method_id="X"),
                provenance=_EXTERNAL_PROVENANCE,
            )

    def test_requires_referenced_method_id(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="referenced_internal_method_id"):
            _buy_and_hold(
                source_kind=StrategySourceKind.INTERNAL,
                rule_kind=StrategyRuleKind.INTERNAL_METHOD_REFERENCE,
                rule_parameters=StrategyRuleParameters(),
                provenance=_INTERNAL_PROVENANCE,
            )

    def test_external_rule_cannot_set_referenced_method_id(self) -> None:
        with pytest.raises(StrategyDefinitionError, match="referenced_internal_method_id"):
            _buy_and_hold(
                rule_kind=StrategyRuleKind.BUY_AND_HOLD,
                rule_parameters=StrategyRuleParameters(referenced_internal_method_id="X"),
            )


class TestNextVersion:
    def test_preserves_strategy_id_and_bumps_version(self) -> None:
        v1 = _buy_and_hold()
        v2 = next_version(
            v1,
            lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE,
            created_at=NOW,
        )
        assert v2.strategy_id == v1.strategy_id
        assert v2.version == 2
        assert v2.parent_strategy_id == v1.strategy_id
        assert v2.lifecycle_state is StrategyLifecycleState.PAPER_ELIGIBLE
        # the parent is untouched -- no in-place mutation
        assert v1.version == 1
        assert v1.lifecycle_state is StrategyLifecycleState.RESEARCH_ONLY

    def test_cannot_version_back_to_research_only(self) -> None:
        v1 = _buy_and_hold()
        with pytest.raises(StrategyDefinitionError, match="RESEARCH_ONLY"):
            next_version(v1, lifecycle_state=StrategyLifecycleState.RESEARCH_ONLY, created_at=NOW)

    def test_cannot_version_directly_to_active(self) -> None:
        # Strategy Lifecycle Guard (Phase H): next_version is a bare,
        # ungated version-bump primitive -- ACTIVE must go through the one
        # explicit, evidence-gated apex.domain.strategy_evaluation.
        # promote_to_active instead.
        v1 = _buy_and_hold()
        v2 = next_version(v1, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW)
        with pytest.raises(StrategyDefinitionError, match="promote_to_active"):
            next_version(v2, lifecycle_state=StrategyLifecycleState.ACTIVE, created_at=NOW)

    def test_chained_versions_keep_same_identity(self) -> None:
        v1 = _buy_and_hold()
        v2 = next_version(v1, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW)
        v3 = next_version(v2, lifecycle_state=StrategyLifecycleState.RETIRED, created_at=NOW)
        assert v3.strategy_id == v1.strategy_id
        assert v3.version == 3
        assert v3.parent_strategy_id == v2.strategy_id


def test_minimum_promotion_sample_size_matches_shared_statistical_convention() -> None:
    # Cross-check against the same minimum-sample convention used elsewhere
    # in Apex (see module docstring) rather than an arbitrary new number.
    from apex.domain.shared_opportunity import STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES

    assert MINIMUM_PROMOTION_SAMPLE_SIZE == STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES
