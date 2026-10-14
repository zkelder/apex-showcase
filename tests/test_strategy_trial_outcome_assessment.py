"""Strategy PAPER Trial Outcome Assessment v1: domain and application tests.

Requirement numbers refer to the milestone brief's required tests (see
``docs/STRATEGY_TRIAL_OUTCOME_ASSESSMENT_V1.md``). Economics use the real
generation-1 production trial's exact numbers where the brief asks for
production-shaped fixtures, and every expected Decimal is recomputed here
from ``domain.crypto_paper``'s documented rounding rules, never echoed.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from apex.application.strategy_paper_trial_lifecycle import (
    InMemoryStrategyPaperTrialClosureRepository,
    RecordStrategyPaperTrialClosed,
    RecordStrategyPaperTrialOpened,
)
from apex.application.strategy_trial_outcome_assessment import (
    AssessClosedStrategyPaperTrials,
    InMemoryStrategyTrialOutcomeAssessmentRepository,
    StrategyTrialOutcomeAssessmentResearchError,
    strategy_trial_outcome_report,
)
from apex.application.strategy_version_provenance import (
    InMemoryStrategyVersionProvenanceRepository,
    RecordProvenancedTrialOpened,
)
from apex.domain.crypto_paper import CryptoPaperFill, PaperSide
from apex.domain.performance_attribution import PaperEvidenceClass
from apex.domain.strategy_definition import StrategyDefinition
from apex.domain.strategy_paper_trial import closed_position_attribution_source_hash
from apex.domain.strategy_paper_trial_evidence_coverage import TrialEvidenceCoverage
from apex.domain.strategy_paper_trial_lifecycle import (
    StrategyPaperTrialClosureRecord,
    StrategyPaperTrialRecord,
    StrategyPaperTrialTerminalReason,
    build_strategy_paper_trial_closure_record,
    build_strategy_paper_trial_record,
    build_unfilled_expiry_closure_record,
)
from apex.domain.strategy_trial_outcome_assessment import (
    STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1,
    STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE,
    ClosedTrialFacts,
    ExpectedReturnBasis,
    StrategyTrialOutcomeAssessmentError,
    StrategyTrialOutcomeStatus,
    TrialEconomicsState,
    build_strategy_trial_outcome_assessment,
    build_trial_attribution_lineage,
    comparable_population_key,
    derive_trial_economics,
    is_close_attempt_order_id,
    select_sample_population,
    verify_strategy_trial_outcome_assessment,
)
from apex.domain.strategy_version_provenance import TrialProvenanceClass
from tests.test_strategy_version_provenance import NOW, _buy_and_hold, _Cycle, _edited

ACCOUNT = "genuine-strategy-crypto-paper-account"
CENT = Decimal("0.01")
SATOSHI = Decimal("0.00000001")

# ---- The real generation-1 production trial (docs/PAPER_TRIAL_CLOSURE_
# READINESS_V1.md / test_postgres_strategy_paper_trial_closure_readiness.py).
PRODUCTION_BASE = Decimal("0.00012687")
PRODUCTION_FILL_PRICE = Decimal("78293.22")
PRODUCTION_AVERAGE_COST = Decimal("78663.19854969")
PRODUCTION_OPENED_AT = datetime(2026, 8, 30, 0, 0, 30, 374926, tzinfo=UTC)
PRODUCTION_FILLED_AT = datetime(2026, 8, 30, 0, 4, 30, 814717, tzinfo=UTC)
CLOSE_FILL_MID = Decimal("110100.00")
CLOSE_FILL_AT = datetime(2026, 9, 30, 0, 5, 5, tzinfo=UTC)
CLOSE_TICK_AT = datetime(2026, 9, 30, 0, 5, 31, tzinfo=UTC)


def _floor(value: Decimal, increment: Decimal) -> Decimal:
    return value.quantize(increment, rounding=ROUND_FLOOR)


def _ceil(value: Decimal, increment: Decimal) -> Decimal:
    return value.quantize(increment, rounding=ROUND_CEILING)


def _production_economics() -> dict[str, Decimal]:
    """``domain.crypto_paper``'s documented BUY and SELL arithmetic,
    recomputed independently."""

    open_quote = _floor(PRODUCTION_BASE * PRODUCTION_FILL_PRICE, CENT)
    open_fee = _floor(open_quote * Decimal("0.006"), CENT)
    assert open_quote + open_fee == Decimal("9.98")  # retained cumulative_cost
    assert _floor((open_quote + open_fee) / PRODUCTION_BASE, SATOSHI) == PRODUCTION_AVERAGE_COST
    close_price = _floor(CLOSE_FILL_MID * (Decimal(1) - Decimal("0.001")), CENT)
    close_quote = _floor(PRODUCTION_BASE * close_price, CENT)
    close_fee = _floor(close_quote * Decimal("0.006"), CENT)
    cost_basis = _ceil(PRODUCTION_AVERAGE_COST * PRODUCTION_BASE, SATOSHI)
    realized = (close_quote - close_fee) - cost_basis
    return {
        "open_quote": open_quote,
        "open_fee": open_fee,
        "open_cost": open_quote + open_fee,
        "close_price": close_price,
        "close_quote": close_quote,
        "close_fee": close_fee,
        "close_net": close_quote - close_fee,
        "cost_basis": cost_basis,
        "realized": realized,
        "cash_flow": (close_quote - close_fee) - (open_quote + open_fee),
    }


def _coverage(trial: StrategyPaperTrialRecord, closed_at: datetime) -> TrialEvidenceCoverage:
    return TrialEvidenceCoverage(
        instrument_id=trial.instrument_id,
        window_start=trial.opened_at,
        window_end=closed_at,
        window_duration_seconds=int((closed_at - trial.opened_at).total_seconds()),
        nominal_days=31,
        covered_days=20,
        qualifying_observation_count=100,
        largest_gap_seconds=3600,
    )


def _fill(
    order_id: str,
    side: PaperSide,
    *,
    base: Decimal,
    quote: Decimal,
    fee: Decimal,
    at: datetime,
    product: str = "BTC-USD",
) -> CryptoPaperFill:
    return CryptoPaperFill(
        fill_id=f"{order_id}-fill",
        order_id=order_id,
        product_id=product,
        side=side,
        price=_floor(quote / base, CENT),
        base_quantity=base,
        quote_quantity=quote,
        fee=fee,
        slippage=Decimal(0),
        evidence_id=f"{order_id}-evidence",
        event_time=at,
        ingestion_time=at,
        canonical_hash="f" * 64,
    )


@dataclasses.dataclass
class _Trial:
    trial: StrategyPaperTrialRecord
    closure: StrategyPaperTrialClosureRecord | None
    fills: list[CryptoPaperFill]


class _World:
    """In-memory retained state: trials, closures, provenance, evaluations,
    fills, orders, and final attribution rows."""

    def __init__(self) -> None:
        self.cycle = _Cycle()
        self.trials = self.cycle.trials
        self.closures = InMemoryStrategyPaperTrialClosureRepository()
        self.provenance: InMemoryStrategyVersionProvenanceRepository = self.cycle.provenance
        self.assessments = InMemoryStrategyTrialOutcomeAssessmentRepository()
        self.fills: list[CryptoPaperFill] = []
        self.orders: dict[str, SimpleNamespace] = {}
        self.attributions: dict[tuple[str, str], SimpleNamespace] = {}
        self.promoted: dict[str, tuple[StrategyDefinition, str]] = {}

    # ---- retained-state doubles
    def state(self, account_id: str) -> Any:
        return SimpleNamespace(fills=tuple(self.fills), orders=tuple(self.orders.values()))

    def attribution_for_source(
        self, source_id: str, source_hash: str, evidence_class: PaperEvidenceClass
    ) -> Any:
        assert evidence_class is PaperEvidenceClass.GENUINE_PAPER
        return self.attributions.get((source_id, source_hash))

    def service(self) -> AssessClosedStrategyPaperTrials:
        return AssessClosedStrategyPaperTrials(
            assessments=self.assessments,
            trials=self.trials,
            closures=self.closures,
            provenance=self.provenance,
            evaluations=self.cycle.evaluations,
            paper_states=self,
            attributions=self,
        )

    # ---- builders
    def _order(self, order_id: str, strategy_id: str) -> str:
        order_hash = f"{abs(hash(order_id)):064d}"[-64:]
        self.orders[order_id] = SimpleNamespace(order_id=order_id, canonical_hash=order_hash)
        return order_hash

    def _attribute(self, order_id: str, source_hash: str, strategy_id: str) -> None:
        self.attributions[(order_id, source_hash)] = SimpleNamespace(
            attribution_id=f"attribution-{order_id}",
            canonical_hash=f"{abs(hash(('a', order_id))):064d}"[-64:],
            source_id=order_id,
            source_hash=source_hash,
            strategy_or_method_id=strategy_id,
            evidence_class=PaperEvidenceClass.GENUINE_PAPER,
            position_state="CLOSED",
        )

    def admitted(
        self, definition: StrategyDefinition | None = None
    ) -> tuple[StrategyDefinition, str]:
        definition = definition or _buy_and_hold()
        if definition.canonical_hash not in self.promoted:
            # One evaluation day per artifact: evaluation identity excludes the
            # definition hash (provenance gap G2, disclosed), so two artifacts
            # evaluated on the same day would share an evaluation id.
            _recorded, promoted, admission = self.cycle.admit(
                definition, as_of=NOW + timedelta(days=len(self.promoted))
            )
            self.promoted[definition.canonical_hash] = (
                promoted,
                admission.transition.transition_id,
            )
        return self.promoted[definition.canonical_hash]

    def open(
        self,
        *,
        generation: int,
        opened_at: datetime,
        legacy: bool = False,
        definition: StrategyDefinition | None = None,
    ) -> StrategyPaperTrialRecord:
        promoted, transition_id = self.admitted(definition)
        trial = build_strategy_paper_trial_record(
            strategy_id=promoted.strategy_id,
            strategy_version=promoted.version,
            instrument_id="BTC-USD",
            account_id=ACCOUNT,
            generation=generation,
            opened_at=opened_at,
            opening_authorization_id=f"{promoted.strategy_id}-g{generation}-auth",
            opening_order_id=f"{promoted.strategy_id}-{promoted.canonical_hash[:6]}-g{generation}",
            opening_realized_pnl_baseline=Decimal(0),
            legacy_adopted=legacy,
        )
        if legacy:
            RecordStrategyPaperTrialOpened(self.trials).execute(record=trial)
        else:
            RecordProvenancedTrialOpened(self.provenance).execute(
                trial=trial, transition_id=transition_id
            )
        return trial

    def close(
        self,
        trial: StrategyPaperTrialRecord,
        *,
        buy: tuple[Decimal, Decimal, Decimal],
        sell: tuple[Decimal, Decimal, Decimal] | None,
        closed_at: datetime,
        realized: Decimal | None = None,
        attribute: bool = True,
        close_attempt: int = 1,
    ) -> StrategyPaperTrialClosureRecord:
        opening_hash = self._order(trial.opening_order_id, trial.strategy_id)
        base, quote, fee = buy
        self.fills.append(
            _fill(
                trial.opening_order_id,
                PaperSide.BUY,
                base=base,
                quote=quote,
                fee=fee,
                at=trial.opened_at + timedelta(minutes=4),
            )
        )
        if sell is None:
            closure = build_unfilled_expiry_closure_record(
                trial=trial, closed_at=closed_at, coverage=_coverage(trial, closed_at)
            )
            self.fills.pop()  # an unfilled opening order has no fill
            if attribute:
                self._attribute(trial.opening_order_id, opening_hash, trial.strategy_id)
        else:
            seed = (
                f"{trial.trial_id}-close"
                if close_attempt == 1
                else (f"{trial.trial_id}-close-{close_attempt}")
            )
            close_order = f"{seed}-order"
            closing_hash = self._order(close_order, trial.strategy_id)
            sbase, squote, sfee = sell
            self.fills.append(
                _fill(
                    close_order,
                    PaperSide.SELL,
                    base=sbase,
                    quote=squote,
                    fee=sfee,
                    at=closed_at - timedelta(seconds=26),
                )
            )
            if realized is None:
                average = _floor((quote + fee) / base, SATOSHI)
                realized = (squote - sfee) - _ceil(average * sbase, SATOSHI)
            closure = build_strategy_paper_trial_closure_record(
                trial=trial,
                closed_at=closed_at,
                closing_authorization_id=f"{seed}-authorization",
                closing_order_id=close_order,
                realized_paper_pnl=realized,
                coverage=_coverage(trial, closed_at),
            )
            if attribute:
                self._attribute(
                    trial.opening_order_id,
                    closed_position_attribution_source_hash(opening_hash),
                    trial.strategy_id,
                )
                self._attribute(
                    close_order,
                    closed_position_attribution_source_hash(closing_hash),
                    trial.strategy_id,
                )
        RecordStrategyPaperTrialClosed(trials=self.trials, closures=self.closures).execute(
            record=closure
        )
        return closure

    def production_close(
        self, trial: StrategyPaperTrialRecord, *, closed_at: datetime = CLOSE_TICK_AT
    ) -> StrategyPaperTrialClosureRecord:
        economics = _production_economics()
        return self.close(
            trial,
            buy=(PRODUCTION_BASE, economics["open_quote"], economics["open_fee"]),
            sell=(PRODUCTION_BASE, economics["close_quote"], economics["close_fee"]),
            closed_at=closed_at,
            realized=economics["realized"],
        )


def _only(world: _World):  # type: ignore[no-untyped-def]
    (assessment,) = world.assessments.assessments()
    return assessment


# ============================================================ economics


class TestEconomics:
    def test_close_attempt_ids_are_matched_exactly(self) -> None:
        assert is_close_attempt_order_id(trial_id="t", order_id="t-close-order")
        assert is_close_attempt_order_id(trial_id="t", order_id="t-close-2-order")
        assert is_close_attempt_order_id(trial_id="t", order_id="t-close-17-order")
        assert not is_close_attempt_order_id(trial_id="t", order_id="t-close-1-order")
        assert not is_close_attempt_order_id(trial_id="t", order_id="t-close-02-order")
        assert not is_close_attempt_order_id(trial_id="t", order_id="t2-close-order")
        assert not is_close_attempt_order_id(trial_id="t", order_id="t-close-order-x")
        assert not is_close_attempt_order_id(trial_id="t.x", order_id="tAx-close-order")

    def test_production_shaped_economics_are_exact_decimals(self) -> None:
        """Requirement 4: opening, closing, cash-flow and ledger P&L, residual,
        and realized return all re-derived independently."""

        world = _World()
        trial = world.open(generation=1, opened_at=PRODUCTION_OPENED_AT, legacy=True)
        closure = world.production_close(trial)
        expected = _production_economics()
        economics = derive_trial_economics(trial=trial, closure=closure, fills=world.fills)
        assert economics.state is TrialEconomicsState.RECONCILED
        assert economics.opening_base_quantity == PRODUCTION_BASE
        assert economics.opening_quote_quantity == Decimal("9.93")
        assert economics.opening_fees == Decimal("0.05")
        assert economics.opening_cost == Decimal("9.98")
        assert economics.closing_base_quantity == PRODUCTION_BASE
        assert economics.closing_quote_quantity == expected["close_quote"] == Decimal("13.95")
        assert economics.closing_fees == expected["close_fee"] == Decimal("0.08")
        assert economics.closing_net_proceeds == Decimal("13.87")
        assert economics.cash_flow_pnl == Decimal("3.89")
        assert economics.ledger_realized_pnl == expected["realized"]
        assert economics.rounding_residual == expected["cash_flow"] - expected["realized"]
        assert abs(economics.rounding_residual) <= economics.reconciliation_tolerance
        assert economics.realized_return == (expected["realized"] / Decimal("9.98")).quantize(
            SATOSHI
        )
        assert economics.realized_return == Decimal("0.38977956")

    def test_residual_beyond_tolerance_is_unreconciled_never_absorbed(self) -> None:
        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        closure = world.close(
            trial,
            buy=(Decimal("0.0001"), Decimal("5.00"), Decimal("0.03")),
            sell=(Decimal("0.0001"), Decimal("6.00"), Decimal("0.03")),
            closed_at=NOW + timedelta(days=30),
            realized=Decimal("0.90"),  # cash flow is 0.94: another fill mixed in
        )
        economics = derive_trial_economics(trial=trial, closure=closure, fills=world.fills)
        assert economics.state is TrialEconomicsState.UNRECONCILED
        assert economics.reasons == ("ROUNDING_RESIDUAL_EXCEEDS_TOLERANCE",)
        assert economics.rounding_residual == Decimal("0.04")
        assert economics.realized_return is None

    def test_partial_close_that_leaves_base_is_unreconciled(self) -> None:
        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        closure = world.close(
            trial,
            buy=(Decimal("0.0002"), Decimal("10.00"), Decimal("0.06")),
            sell=(Decimal("0.0001"), Decimal("6.00"), Decimal("0.03")),
            closed_at=NOW + timedelta(days=30),
        )
        economics = derive_trial_economics(trial=trial, closure=closure, fills=world.fills)
        assert economics.state is TrialEconomicsState.UNRECONCILED
        assert "POSITION_NOT_FLATTENED_BY_TRIAL_FILLS" in economics.reasons

    def test_other_trials_fills_on_the_same_product_are_never_used(self) -> None:
        world = _World()
        first = world.open(generation=1, opened_at=NOW)
        world.production_close(first, closed_at=NOW + timedelta(days=30))
        second = world.open(generation=2, opened_at=NOW + timedelta(days=32))
        closure = world.close(
            second,
            buy=(Decimal("0.0001"), Decimal("5.00"), Decimal("0.03")),
            sell=(Decimal("0.0001"), Decimal("4.00"), Decimal("0.02")),
            closed_at=NOW + timedelta(days=62),
        )
        economics = derive_trial_economics(trial=second, closure=closure, fills=world.fills)
        assert economics.opening_fill_count == 1 and economics.closing_fill_count == 1
        assert economics.opening_cost == Decimal("5.03")
        assert economics.cash_flow_pnl == Decimal("-1.05")
        assert economics.state is TrialEconomicsState.RECONCILED
        assert economics.realized_return is not None and economics.realized_return < 0

    def test_unfilled_expiry_has_no_economic_outcome_never_zero(self) -> None:
        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        closure = world.close(
            trial,
            buy=(Decimal("1"), Decimal("1"), Decimal("0")),
            sell=None,
            closed_at=NOW + timedelta(hours=5),
        )
        economics = derive_trial_economics(trial=trial, closure=closure, fills=world.fills)
        assert economics.state is TrialEconomicsState.NO_EXPOSURE
        assert economics.cash_flow_pnl is None
        assert economics.ledger_realized_pnl is None
        assert economics.realized_return is None

    def test_decimal_scale_never_exceeds_canonical_bound(self) -> None:
        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        closure = world.close(
            trial,
            buy=(Decimal("123.45678901"), Decimal("1000.00"), Decimal("6.00")),
            sell=(Decimal("123.45678901"), Decimal("1100.00"), Decimal("6.60")),
            closed_at=NOW + timedelta(days=30),
        )
        economics = derive_trial_economics(trial=trial, closure=closure, fills=world.fills)
        assert economics.reconciliation_tolerance.as_tuple().exponent == -8
        assert economics.state is TrialEconomicsState.RECONCILED


# ============================================================ assessment


class TestLegacyGenerationOne:
    def test_closed_legacy_trial_gets_exactly_one_predates_assessment(self) -> None:
        """Requirements 1 and 2."""

        world = _World()
        trial = world.open(generation=1, opened_at=PRODUCTION_OPENED_AT, legacy=True)
        world.production_close(trial)
        result = world.service().execute(assessed_at=CLOSE_TICK_AT + timedelta(minutes=5))
        assert len(result.written) == 1
        assessment = _only(world)
        assert assessment.status is StrategyTrialOutcomeStatus.PREDATES_STRATEGY_PROVENANCE
        assert assessment.provenance_class is TrialProvenanceClass.PREDATES_STRATEGY_PROVENANCE
        assert assessment.trial_legacy_adopted is True
        # Requirement 2: never fabricated into a bound strategy version.
        for field in (
            "version_registry",
            "definition_hash",
            "provenance_hash",
            "transition_id",
            "transition_hash",
            "evaluation_id",
            "evaluation_hash",
            "evaluated_version",
            "evaluation_net_return",
            "population_id",
            "population_members_hash",
        ):
            assert getattr(assessment, field) is None, field
        assert assessment.population_size == 0
        assert assessment.expected_return_basis is (
            ExpectedReturnBasis.NO_JUSTIFYING_EVALUATION_RETAINED
        )
        assert "NO_STRATEGY_VERSION_BINDING" in assessment.reason_codes
        # Economics are still assessed in full.
        assert assessment.economics_state is TrialEconomicsState.RECONCILED
        assert assessment.realized_return == Decimal("0.38977956")
        assert assessment.authority == "NONE"
        # Replay: a second run writes nothing.
        again = world.service().execute(assessed_at=CLOSE_TICK_AT + timedelta(minutes=10))
        assert again.written == () and again.replayed == ()
        assert len(world.assessments.assessments()) == 1

    def test_legacy_facts_can_never_carry_a_lineage(self) -> None:
        world = _World()
        trial = world.open(generation=1, opened_at=PRODUCTION_OPENED_AT, legacy=True)
        closure = world.production_close(trial)
        economics = derive_trial_economics(trial=trial, closure=closure, fills=world.fills)
        bound = world.open(generation=2, opened_at=CLOSE_TICK_AT + timedelta(days=1))
        bound_closure = world.production_close(bound, closed_at=CLOSE_TICK_AT + timedelta(days=31))
        bound_facts = _facts(world, bound, bound_closure)
        with pytest.raises(StrategyTrialOutcomeAssessmentError, match="LINEAGE_SHAPE"):
            ClosedTrialFacts(trial, closure, TrialProvenanceClass.BOUND, None, economics)
        with pytest.raises(StrategyTrialOutcomeAssessmentError, match="LEGACY_CLASSIFICATION"):
            ClosedTrialFacts(
                trial, closure, TrialProvenanceClass.BOUND, bound_facts.lineage, economics
            )

    def test_legacy_trial_never_enters_a_population(self) -> None:
        world = _World()
        legacy = world.open(generation=1, opened_at=PRODUCTION_OPENED_AT, legacy=True)
        world.production_close(legacy)
        bound = world.open(generation=2, opened_at=CLOSE_TICK_AT + timedelta(days=1))
        world.production_close(bound, closed_at=CLOSE_TICK_AT + timedelta(days=31))
        world.service().execute(assessed_at=CLOSE_TICK_AT + timedelta(days=31, minutes=1))
        by_trial = {item.trial_id: item for item in world.assessments.assessments()}
        assert by_trial[bound.trial_id].population_size == 1  # itself only
        assert by_trial[legacy.trial_id].population_size == 0


def _facts(
    world: _World, trial: StrategyPaperTrialRecord, closure: StrategyPaperTrialClosureRecord
) -> ClosedTrialFacts:
    gathered = world.service()._gather((closure,))
    return gathered.facts[trial.trial_id]


class TestBoundTrial:
    def test_bound_trial_persists_exact_lineage(self) -> None:
        """Requirement 3."""

        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        closure = world.production_close(trial, closed_at=NOW + timedelta(days=30))
        world.service().execute(assessed_at=NOW + timedelta(days=30, minutes=1))
        assessment = _only(world)
        provenance = world.provenance.trial_provenance(trial_id=trial.trial_id)
        assert provenance is not None
        transition = world.provenance.transition(transition_id=provenance.transition_id)
        assert transition is not None
        evaluation = world.cycle.evaluations.get(evaluation_id=transition.evaluation_id)
        assert evaluation is not None
        assert assessment.provenance_class is TrialProvenanceClass.BOUND
        assert assessment.version_registry is provenance.version_ref.registry
        assert assessment.definition_hash == provenance.version_ref.definition_hash
        assert assessment.provenance_hash == provenance.canonical_hash
        assert assessment.transition_id == transition.transition_id
        assert assessment.transition_hash == transition.canonical_hash
        assert assessment.transition_kind == "CYCLE_GATE_ADMISSION"
        assert assessment.evaluation_id == evaluation.evaluation_id
        assert assessment.evaluation_hash == evaluation.canonical_hash
        assert assessment.evaluated_version == 1 and assessment.strategy_version == 2
        assert assessment.evaluation_net_return == evaluation.candidate.net_return
        assert assessment.evaluation_window_end == evaluation.candidate.window_end
        assert assessment.expected_return_basis is (
            ExpectedReturnBasis.EVALUATION_WINDOW_NET_RETURN_NOT_HORIZON_MATCHED
        )
        assert "EXPECTED_RETURN_NOT_HORIZON_MATCHED_NO_DELTA_COMPUTED" in assessment.reason_codes
        assert assessment.closure_id == closure.closure_id
        assert assessment.closure_hash == closure.canonical_hash
        assert assessment.trial_hash == trial.canonical_hash
        assert assessment.opening_attribution_id == f"attribution-{trial.opening_order_id}"
        assert assessment.closing_attribution_id == f"attribution-{closure.closing_order_id}"

    def test_one_bound_trial_is_insufficient_sample(self) -> None:
        """Requirement 5."""

        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        world.production_close(trial, closed_at=NOW + timedelta(days=30))
        world.service().execute(assessed_at=NOW + timedelta(days=31))
        assessment = _only(world)
        assert assessment.status is StrategyTrialOutcomeStatus.INSUFFICIENT_SAMPLE
        assert assessment.population_size == 1
        assert assessment.minimum_sample_size == STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE == 30
        assert assessment.population_mean_realized_return is None
        assert assessment.population_positive_count is None
        assert assessment.population_total_realized_pnl is None
        assert "POPULATION_BELOW_MINIMUM_SAMPLE" in assessment.reason_codes
        assert "NO_STRATEGY_LEVEL_VERDICT_V1" in assessment.reason_codes


def _run_generations(world: _World, count: int, *, start: datetime = NOW) -> list[datetime]:
    closes = []
    for generation in range(1, count + 1):
        opened = start + timedelta(days=32 * (generation - 1))
        trial = world.open(generation=generation, opened_at=opened)
        # Alternate winners and losers so population statistics are non-trivial.
        sell_quote = Decimal("6.00") if generation % 3 else Decimal("4.00")
        closed = opened + timedelta(days=30)
        world.close(
            trial,
            buy=(Decimal("0.0001"), Decimal("5.00"), Decimal("0.03")),
            sell=(Decimal("0.0001"), sell_quote, Decimal("0.03")),
            closed_at=closed,
        )
        closes.append(closed)
    return closes


class TestSamplePopulation:
    def test_population_reaching_the_minimum_is_assessed_deterministically(self) -> None:
        """Requirement 6: the 30th comparable generation is the first ASSESSED;
        every earlier one stays INSUFFICIENT_SAMPLE with statistics withheld."""

        world = _World()
        _run_generations(world, STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE)
        result = world.service().execute(assessed_at=NOW + timedelta(days=2000))
        assert len(result.written) == STRATEGY_TRIAL_OUTCOME_MAX
        assert result.backlog_remaining == 30 - STRATEGY_TRIAL_OUTCOME_MAX
        world.service().execute(assessed_at=NOW + timedelta(days=2001))
        assessments = world.assessments.assessments()
        assert [item.population_size for item in assessments] == list(range(1, 31))
        assert all(
            item.status is StrategyTrialOutcomeStatus.INSUFFICIENT_SAMPLE
            and item.population_mean_realized_return is None
            for item in assessments[:-1]
        )
        last = assessments[-1]
        assert last.status is StrategyTrialOutcomeStatus.ASSESSED
        returns = [item.realized_return for item in assessments]
        assert all(value is not None for value in returns)
        expected_mean = (sum(cast(list[Decimal], returns), Decimal(0)) / 30).quantize(SATOSHI)
        assert last.population_mean_realized_return == expected_mean
        assert last.population_positive_count == sum(
            1 for value in cast(list[Decimal], returns) if value > 0
        )
        assert last.population_total_realized_pnl == sum(
            (cast(Decimal, item.ledger_realized_pnl) for item in assessments), Decimal(0)
        )
        # Deterministic: rebuilding from fresh state in any candidate order
        # reproduces byte-identical assessments.
        facts = world.service()._gather(tuple(world.closures.all_closures())).facts
        subject = facts[last.trial_id]
        forward = list(facts.values())
        for candidates in (forward, list(reversed(forward))):
            rebuilt = build_strategy_trial_outcome_assessment(
                subject=subject,
                candidates=candidates,
                attribution=build_trial_attribution_lineage(
                    trial=subject.trial,
                    opening=cast(
                        Any,
                        world.attributions[
                            next(
                                key
                                for key in world.attributions
                                if key[0] == subject.trial.opening_order_id
                            )
                        ],
                    ),
                    closing=cast(
                        Any,
                        world.attributions[
                            next(
                                key
                                for key in world.attributions
                                if key[0] == subject.closure.closing_order_id
                            )
                        ],
                    ),
                    expected_opening_source=next(
                        key
                        for key in world.attributions
                        if key[0] == subject.trial.opening_order_id
                    ),
                    expected_closing_source=next(
                        key
                        for key in world.attributions
                        if key[0] == subject.closure.closing_order_id
                    ),
                ),
            )
            assert rebuilt == last

    def test_later_generations_never_leak_into_an_earlier_population(self) -> None:
        world = _World()
        _run_generations(world, 3)
        facts = world.service()._gather(tuple(world.closures.all_closures())).facts
        first = min(facts.values(), key=lambda item: item.closure.closed_at)
        population = select_sample_population(subject=first, candidates=tuple(facts.values()))
        assert population is not None
        assert population.size == 1
        assert population.members[0][0] == first.trial.trial_id

    def test_version_change_starts_a_new_population_and_rewrites_nothing(self) -> None:
        """Requirement 17."""

        world = _World()
        original = _buy_and_hold()
        first = world.open(generation=1, opened_at=NOW, definition=original)
        world.production_close(first, closed_at=NOW + timedelta(days=30))
        world.service().execute(assessed_at=NOW + timedelta(days=30, minutes=1))
        (before,) = world.assessments.assessments()

        edited = _edited(original, display_name="Buy and hold (edited)")
        assert edited.strategy_id == original.strategy_id
        assert edited.canonical_hash != original.canonical_hash
        second = world.open(generation=2, opened_at=NOW + timedelta(days=32), definition=edited)
        world.production_close(second, closed_at=NOW + timedelta(days=62))
        world.service().execute(assessed_at=NOW + timedelta(days=62, minutes=1))
        after = {item.trial_id: item for item in world.assessments.assessments()}
        assert after[first.trial_id] == before  # byte-identical, never rewritten
        assert after[second.trial_id].definition_hash != before.definition_hash
        assert after[second.trial_id].population_id != before.population_id
        assert after[second.trial_id].population_size == 1


class TestDiscoveryAndIdempotency:
    def test_open_trial_is_never_assessed(self) -> None:
        """Requirement 10."""

        world = _World()
        world.open(generation=1, opened_at=NOW)
        result = world.service().execute(assessed_at=NOW + timedelta(days=40))
        assert result.written == () and world.assessments.assessments() == ()

    def test_backlog_from_before_deployment_is_discovered_and_drains(self) -> None:
        """Requirement 9: trials closed before the assessment capability
        existed are found, oldest first, bounded per dispatch."""

        world = _World()
        closes = _run_generations(world, 3)
        result = world.service().execute(assessed_at=closes[-1] + timedelta(days=10), limit=2)
        assert len(result.written) == 2 and result.backlog_remaining == 1
        result = world.service().execute(assessed_at=closes[-1] + timedelta(days=11), limit=2)
        assert len(result.written) == 1 and result.backlog_remaining == 0
        assert [item.closed_at for item in world.assessments.assessments()] == closes

    def test_replay_is_a_no_op_and_changed_content_conflicts(self) -> None:
        """Requirements 7, 8 and 12."""

        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        world.production_close(trial, closed_at=NOW + timedelta(days=30))
        world.service().execute(assessed_at=NOW + timedelta(days=31))
        assessment = _only(world)
        assert (
            world.assessments.register(assessment=assessment, assessed_at=NOW + timedelta(days=99))
            is True
        )
        assert len(world.assessments.assessments()) == 1
        changed = dataclasses.replace(assessment, realized_return=Decimal("0.5"))
        changed = dataclasses.replace(changed, canonical_hash=_rehash(changed))
        with pytest.raises(StrategyTrialOutcomeAssessmentResearchError, match="IDENTITY_CONFLICT"):
            world.assessments.register(assessment=changed, assessed_at=NOW)

    def test_tampered_hash_fails_verification(self) -> None:
        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        world.production_close(trial, closed_at=NOW + timedelta(days=30))
        world.service().execute(assessed_at=NOW + timedelta(days=31))
        tampered = dataclasses.replace(_only(world), realized_return=Decimal("9"))
        with pytest.raises(StrategyTrialOutcomeAssessmentError, match="HASH_MISMATCH"):
            verify_strategy_trial_outcome_assessment(tampered)

    def test_missing_final_attribution_defers_without_writing(self) -> None:
        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        world.production_close(trial, closed_at=NOW + timedelta(days=30))
        world.attributions.clear()
        result = world.service().execute(assessed_at=NOW + timedelta(days=31))
        assert result.written == ()
        assert result.deferred == (f"{trial.trial_id}:AWAITING_CLOSED_ATTRIBUTION",)

    def test_open_era_attribution_is_never_accepted_as_final(self) -> None:
        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        world.production_close(trial, closed_at=NOW + timedelta(days=30))
        opening_hash = world.orders[trial.opening_order_id].canonical_hash
        # Only the OPEN-era BUY row (raw order hash) exists; the superseding
        # POSITION_CLOSED row was never materialized.
        world.attributions = {
            key: value
            for key, value in world.attributions.items()
            if key[0] != trial.opening_order_id
        }
        world._attribute(trial.opening_order_id, opening_hash, trial.strategy_id)
        result = world.service().execute(assessed_at=NOW + timedelta(days=31))
        assert result.deferred == (f"{trial.trial_id}:AWAITING_CLOSED_ATTRIBUTION",)

    def test_one_broken_trial_never_blocks_another(self) -> None:
        world = _World()
        closes = _run_generations(world, 2)
        first, second = world.closures.all_closures()
        # Corrupt only the first trial's economics: its opening fill vanishes.
        first_trial = world.trials.get(trial_id=first.trial_id)
        assert first_trial is not None
        world.fills = [
            item for item in world.fills if item.order_id != first_trial.opening_order_id
        ]
        world.orders.pop(first_trial.opening_order_id)
        result = world.service().execute(assessed_at=closes[-1] + timedelta(days=1))
        assert result.failed == (
            f"{first.trial_id}:STRATEGY_TRIAL_OUTCOME_OPENING_ORDER_NOT_FOUND",
        )
        (written,) = world.assessments.assessments()
        assert written.trial_id == second.trial_id
        # The broken trial is simply excluded from the population too.
        assert written.population_size == 1

    def test_expired_sell_attempt_without_closure_is_not_a_completed_trial(self) -> None:
        """Requirement 11: a dead close attempt leaves the trial ACTIVE, so it
        is never assessed; and its SELL fills (if any) are not economics of
        any completed trial until a closure exists."""

        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        world.fills.append(
            _fill(
                f"{trial.trial_id}-close-order",
                PaperSide.SELL,
                base=Decimal("0.00005"),
                quote=Decimal("3.00"),
                fee=Decimal("0.01"),
                at=NOW + timedelta(days=30),
            )
        )
        result = world.service().execute(assessed_at=NOW + timedelta(days=31))
        assert result.written == () and world.assessments.assessments() == ()

    def test_unfilled_expiry_closure_is_no_economic_outcome(self) -> None:
        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        world.close(
            trial,
            buy=(Decimal("1"), Decimal("1"), Decimal("0")),
            sell=None,
            closed_at=NOW + timedelta(hours=5),
        )
        world.service().execute(assessed_at=NOW + timedelta(hours=6))
        assessment = _only(world)
        assert assessment.status is StrategyTrialOutcomeStatus.NO_ECONOMIC_OUTCOME
        assert assessment.terminal_reason is (
            StrategyPaperTrialTerminalReason.OPENING_ORDER_EXPIRED_UNFILLED
        )
        assert assessment.ledger_realized_pnl is None and assessment.realized_return is None
        assert assessment.closing_attribution_id is None
        assert assessment.population_size == 0  # not a sample member, not even itself

    def test_legacy_and_bound_generations_coexist(self) -> None:
        """Requirement 16."""

        world = _World()
        legacy = world.open(generation=1, opened_at=PRODUCTION_OPENED_AT, legacy=True)
        world.production_close(legacy)
        bound = world.open(generation=2, opened_at=CLOSE_TICK_AT + timedelta(days=1))
        world.production_close(bound, closed_at=CLOSE_TICK_AT + timedelta(days=31))
        world.service().execute(assessed_at=CLOSE_TICK_AT + timedelta(days=32))
        statuses = {item.generation: item.status for item in world.assessments.assessments()}
        assert statuses == {
            1: StrategyTrialOutcomeStatus.PREDATES_STRATEGY_PROVENANCE,
            2: StrategyTrialOutcomeStatus.INSUFFICIENT_SAMPLE,
        }
        report = strategy_trial_outcome_report(
            assessments=world.assessments.assessments(),
            closures=world.closures.all_closures(),
            active_trials=(),
        )
        assert report["provenance_bound_sample_count"] == 1
        assert report["status_counts"] == {
            "INSUFFICIENT_SAMPLE": 1,
            "PREDATES_STRATEGY_PROVENANCE": 1,
        }
        assert report["closed_trials_awaiting_assessment"] == ()
        assert report["authority"] == "NONE" and report["live_authority"] == "NONE"


STRATEGY_TRIAL_OUTCOME_MAX = 25


def _rehash(item: Any) -> str:
    from apex.domain.strategy_trial_outcome_assessment import _content_hash

    return _content_hash(item)


# ============================================================ boundaries


SOURCE = Path(__file__).resolve().parents[1] / "src" / "apex"
_FORBIDDEN_IMPORTS = (
    "apex.application.strategy_paper_trial",
    "apex.application.crypto_paper",
    "apex.adapters.coinbase",
    "apex.adapters.kalshi",
    "apex.adapters.alpaca",
    "urllib",
    "http",
    "requests",
    "socket",
)


@pytest.mark.parametrize(
    "path",
    (
        "domain/strategy_trial_outcome_assessment.py",
        "application/strategy_trial_outcome_assessment.py",
        "adapters/strategy_trial_outcome_assessment_postgres.py",
    ),
)
def test_modules_have_no_provider_or_execution_capability(path: str) -> None:
    """Requirement 14: nothing here can reach a provider or place, change,
    or authorize a PAPER order."""

    tree = ast.parse((SOURCE / path).read_text())
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])  # type: ignore[list-item]
    }
    for name in imported:
        assert name is not None
        assert not any(
            name == forbidden
            or name.startswith(f"{forbidden}.")
            or name.startswith(f"{forbidden}_")
            for forbidden in _FORBIDDEN_IMPORTS
        ), (path, name)


def test_assessment_policy_is_versioned_and_authority_free() -> None:
    """Requirement 15."""

    assert STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1.endswith("_V1")
    world = _World()
    trial = world.open(generation=1, opened_at=NOW)
    world.production_close(trial, closed_at=NOW + timedelta(days=30))
    world.service().execute(assessed_at=NOW + timedelta(days=31))
    assessment = _only(world)
    assert assessment.authority == "NONE"
    # No field of the record can carry, grant, or name LIVE/PAPER authority.
    fields = type(assessment).__dataclass_fields__
    assert [name for name in fields if "authority" in name] == ["authority"]
    assert not any("live" in name or "paper_eligib" in name for name in fields)


# ============================================================ Fable review fixes


class TestReviewFixes:
    def test_uncanonicalizable_value_fails_only_its_own_trial(self) -> None:
        """Fable #1: a Decimal beyond canonical precision is that trial's
        failure; the next trial is still assessed in the same dispatch."""

        world = _World()
        huge = world.open(generation=1, opened_at=NOW)
        world.close(
            huge,
            buy=(Decimal("0.00000001"), Decimal("12345678901.12345678"), Decimal("0")),
            sell=(Decimal("0.00000001"), Decimal("12345678902.12345678"), Decimal("0")),
            closed_at=NOW + timedelta(days=30),
            realized=Decimal("1"),
        )
        normal = world.open(generation=2, opened_at=NOW + timedelta(days=32))
        world.production_close(normal, closed_at=NOW + timedelta(days=62))
        result = world.service().execute(assessed_at=NOW + timedelta(days=63))
        assert len(result.failed) == 1 and result.failed[0].startswith(huge.trial_id)
        assert "ValueValidationError" in result.failed[0]
        (written,) = world.assessments.assessments()
        assert written.trial_id == normal.trial_id

    def test_persistence_rejection_fails_only_its_own_trial(self) -> None:
        world = _World()
        closes = _run_generations(world, 2)
        first = world.closures.all_closures()[0]
        original = world.assessments.register

        def reject_first(*, assessment: Any, assessed_at: datetime) -> bool:
            if assessment.trial_id == first.trial_id:
                raise StrategyTrialOutcomeAssessmentResearchError(
                    "STRATEGY_TRIAL_OUTCOME_ASSESSMENT_PERSISTENCE_CONFLICT"
                )
            return original(assessment=assessment, assessed_at=assessed_at)

        world.assessments.register = reject_first  # type: ignore[method-assign]
        result = world.service().execute(assessed_at=closes[-1] + timedelta(days=1))
        assert result.failed == (
            f"{first.trial_id}:STRATEGY_TRIAL_OUTCOME_ASSESSMENT_PERSISTENCE_CONFLICT",
        )
        assert len(result.written) == 1

    def test_non_sample_row_never_carries_peer_statistics(self) -> None:
        """Fable #2: with a qualified population of peers, an unreconciled
        trial records the population size but no peer statistics."""

        world = _World()
        closes = _run_generations(world, STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE)
        late = world.open(generation=31, opened_at=closes[-1] + timedelta(days=2))
        world.close(
            late,
            buy=(Decimal("0.0001"), Decimal("5.00"), Decimal("0.03")),
            sell=(Decimal("0.0001"), Decimal("6.00"), Decimal("0.03")),
            closed_at=closes[-1] + timedelta(days=32),
            realized=Decimal("0.50"),  # disagrees with the fills
        )
        for day in range(3):
            world.service().execute(assessed_at=closes[-1] + timedelta(days=40 + day))
        by_generation = {item.generation: item for item in world.assessments.assessments()}
        assert by_generation[30].status is StrategyTrialOutcomeStatus.ASSESSED
        unreconciled = by_generation[31]
        assert unreconciled.status is StrategyTrialOutcomeStatus.ECONOMICS_UNRECONCILED
        assert unreconciled.population_size == STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE
        assert unreconciled.population_mean_realized_return is None
        assert unreconciled.population_positive_count is None
        assert unreconciled.population_total_realized_pnl is None

    def test_population_is_scoped_to_one_paper_account(self) -> None:
        """Fable #4."""

        world = _World()
        trial = world.open(generation=1, opened_at=NOW)
        closure = world.production_close(trial, closed_at=NOW + timedelta(days=30))
        facts = _facts(world, trial, closure)
        other = dataclasses.replace(
            facts, trial=dataclasses.replace(facts.trial, account_id="another-account")
        )
        assert comparable_population_key(facts) != comparable_population_key(other)
