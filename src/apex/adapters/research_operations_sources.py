"""Read-only retained-state sources for the governed research agenda."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_outcome_postgres import PostgresCounterfactualOutcomeRepository
from apex.adapters.cross_venue_allocation_postgres import PostgresCrossVenueAllocationRepository
from apex.adapters.performance_attribution_postgres import PostgresPerformanceAttributionRepository
from apex.adapters.securities_structure_outcome_postgres import (
    PostgresSecuritiesStructureOutcomeRepository,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.shared_opportunity_sources import RetainedSharedOpportunitySource
from apex.application.research_operations import ResearchProposalGenerationContext
from apex.domain.counterfactual_outcome import (
    V3_DECISION_TIME_WINDOW_LAGGED_BY_DAILY_CEILING,
    CounterfactualOutcomeAggregate,
    counterfactual_outcome_sourcing_exclusion,
)
from apex.domain.research_operations import (
    ResearchAgendaSourceFact,
    ResearchVenue,
)
from apex.domain.securities_structure_outcome import (
    CURRENT_SECURITIES_STRUCTURE_EVIDENCE_QUALITY_POLICY_VERSION,
)
from apex.domain.shared_opportunity import EligibilityState, SharedVenue
from apex.domain.values import content_digest

# The one place the counterfactual-outcome research fact id is spelled.
# ``facts()`` builds ids with it and ``generation_context()`` excludes
# ids with it, so the two can never drift apart.
_COUNTERFACTUAL_OUTCOME_FACT_ID_PREFIX = "counterfactual-outcome-"


@dataclass(frozen=True, slots=True)
class QuarantinedCounterfactualOutcomeCohort:
    """One already-``QUALIFIED`` counterfactual-outcome cohort that
    ``RetainedResearchAgendaSource.facts()`` no longer emits a research
    agenda fact for, together with the reviewed defect that withheld it.

    Carries no verdict about the aggregate itself: the row is still
    computed, still retained, still queryable, and everything it already
    produced stays exactly as retained."""

    cohort_id: str
    venue: SharedVenue
    strategy_or_method_id: str
    followup_policy_version: str
    defect_code: str
    corrected_by_policy_version: str
    summary: str
    # Counterfactual Outcome Evidence Specificity v3: the two further
    # dimensions the sourcing decision now consults (see ``domain.
    # counterfactual_outcome.counterfactual_outcome_sourcing_exclusion``).
    outcome_policy_version: str = ""
    baseline_kind: str = ""
    # ``True`` when the cohort is withheld by V3's deliberate ORIGIN_EVIDENCE-
    # only sourcing selection rather than by a reviewed defect -- the
    # Operations page must never read a non-defective sibling as a defect.
    sourcing_selection: bool = False


class RetainedResearchAgendaSource:
    """Translate retained blockers into exact fact records; never invent prose or evidence."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._shared = PostgresSharedOpportunityRepository(engine)
        self._allocation = PostgresCrossVenueAllocationRepository(engine)
        self._performance = PostgresPerformanceAttributionRepository(engine)
        self._methods = RetainedSharedOpportunitySource(engine)
        self._counterfactual_outcome = PostgresCounterfactualOutcomeRepository(engine)
        self._securities_structure_outcome = PostgresSecuritiesStructureOutcomeRepository(engine)

    def generation_context(
        self, *, agenda_items: tuple[object, ...]
    ) -> ResearchProposalGenerationContext:
        """Describe only retained data and existing experiment identities."""

        from apex.adapters.research_integrity_postgres import (
            PostgresResearchIntegrityRepository,
        )

        source_fact_ids = tuple(
            str(value.source_fact_id) for value in agenda_items if hasattr(value, "source_fact_id")
        )
        experiments = tuple(
            value.experiment_id
            for value in PostgresResearchIntegrityRepository(self._engine).families()
        )
        return ResearchProposalGenerationContext(
            retained_data_ids=source_fact_ids,
            executable_experiment_ids=experiments,
            provider_capability_ids=(),
            remaining_experiment_budget=3,
            evidence_class_compatible=True,
            # Counterfactual V2 Research Evidence Quarantine v1. Withholding
            # the fact in ``facts()`` only stops a *new* agenda item being
            # created; ``GenerateResearchProposals`` draws its candidates
            # from the retained agenda item table, so the items created
            # before the defect was known -- 26 in production, 13 of them
            # still without a proposal -- would otherwise have stayed
            # permanently eligible. Built from the identical
            # ``counterfactual-outcome-<cohort_id>`` fact-id formula
            # ``facts()`` itself uses, so an excluded item and a withheld
            # fact can never be about different cohorts.
            excluded_source_fact_ids=tuple(
                f"{_COUNTERFACTUAL_OUTCOME_FACT_ID_PREFIX}{value.cohort_id}"
                for value in self.quarantined_counterfactual_outcome_cohorts()
            ),
        )

    @staticmethod
    def _method_venue(value: object) -> ResearchVenue:
        return ResearchVenue.KALSHI if str(value) == "KALSHI" else ResearchVenue.CRYPTO

    @staticmethod
    def _health_venue(strategy_or_method_id: str) -> ResearchVenue | None:
        normalized = strategy_or_method_id.upper()
        if "KALSHI" in normalized:
            return ResearchVenue.KALSHI
        if "CRYPTO" in normalized or "COINBASE" in normalized:
            return ResearchVenue.CRYPTO
        return None

    @staticmethod
    def _count(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @staticmethod
    def _strings(value: object) -> tuple[str, ...]:
        if isinstance(value, (tuple, list)):
            return tuple(str(item) for item in value)
        return ()

    def quarantined_counterfactual_outcome_cohorts(
        self,
    ) -> tuple[QuarantinedCounterfactualOutcomeCohort, ...]:
        """Counterfactual V2 Research Evidence Quarantine v1: the exact
        cohorts ``facts()`` withheld, and why -- the disclosure counterpart
        of that filter, so a withheld cohort is explainable rather than
        merely absent.

        Read-only and derived from the identical
        ``earliest_qualified_per_cohort()`` call ``facts()`` itself makes,
        so the two can never disagree about which cohorts are affected.
        Deterministically ordered by ``cohort_id``."""

        return tuple(
            sorted(
                (
                    QuarantinedCounterfactualOutcomeCohort(
                        cohort_id=aggregate.cohort_id,
                        venue=aggregate.venue,
                        strategy_or_method_id=aggregate.strategy_or_method_id,
                        followup_policy_version=aggregate.followup_policy_version,
                        defect_code=exclusion.reason_code,
                        corrected_by_policy_version=exclusion.corrected_by,
                        summary=exclusion.summary,
                        outcome_policy_version=aggregate.outcome_policy_version,
                        baseline_kind=aggregate.baseline_kind.value,
                        sourcing_selection=(
                            exclusion.reason_code == V3_DECISION_TIME_WINDOW_LAGGED_BY_DAILY_CEILING
                        ),
                    )
                    for aggregate in self._counterfactual_outcome.earliest_qualified_per_cohort()
                    if (
                        exclusion := counterfactual_outcome_sourcing_exclusion(
                            venue=aggregate.venue,
                            outcome_policy_version=aggregate.outcome_policy_version,
                            followup_policy_version=aggregate.followup_policy_version,
                            baseline_kind=aggregate.baseline_kind,
                        )
                    )
                    is not None
                ),
                key=lambda value: value.cohort_id,
            )
        )

    @staticmethod
    def _counterfactual_outcome_venue(aggregate: CounterfactualOutcomeAggregate) -> ResearchVenue:
        return (
            ResearchVenue.KALSHI if aggregate.venue is SharedVenue.KALSHI else ResearchVenue.CRYPTO
        )

    @staticmethod
    def _observed_at(value: object, fallback: datetime) -> datetime:
        if isinstance(value, datetime):
            return value.astimezone(UTC)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                pass
        return fallback

    def facts(self, *, as_of: datetime) -> tuple[ResearchAgendaSourceFact, ...]:
        facts: list[ResearchAgendaSourceFact] = []
        for method in self._methods.strategy_library():
            method_id = str(method.get("method_id"))
            record_id = str(method.get("current_record_id") or method_id)
            record_hash = content_digest(method)
            result_count = self._count(method.get("result_count"))
            eligible_count = self._count(method.get("eligible_result_count"))
            limitations = self._strings(method.get("limitations"))
            if method_id == "CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1" and eligible_count == 0:
                fact_type = "INSUFFICIENT_CALIBRATION"
                summary = (
                    "Current relative-strength calibration has no retained source-eligible result."
                )
            elif method_id == "CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1" and eligible_count == 0:
                fact_type = "NEGATIVE_EXPECTED_ECONOMICS"
                summary = "Current reversal-regime evidence has no positive source-eligible result."
            elif method_id == "KALSHI_RELATED_MARKET_CONSISTENCY_V1" and eligible_count == 0:
                fact_type = "NON_POSITIVE_NET_GAP"
                summary = (
                    "Current related-market packages have no positive conservative retained gap."
                )
            elif method_id == "KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1" and eligible_count == 0:
                fact_type = "EXHAUSTIVENESS_UNPROVEN"
                summary = (
                    "Current event-set evidence has no source-eligible proven exhaustive package."
                )
            elif limitations and result_count == 0:
                fact_type = "DATA_QUALITY_LIMITATION"
                summary = f"{method_id} is blocked by retained data or capability limitations."
            else:
                continue
            facts.append(
                ResearchAgendaSourceFact.create(
                    fact_id=f"{fact_type.lower()}-{record_id}",
                    fact_type=fact_type,
                    venue=self._method_venue(method.get("venue")),
                    market_scope=(method_id,),
                    state="BLOCKED",
                    summary=summary,
                    evidence_ids=(record_id,),
                    evidence_hashes=(record_hash,),
                    observed_at=self._observed_at(method.get("evaluated_at"), as_of),
                )
            )
        sets = self._shared.opportunity_sets()
        if sets:
            current = sets[-1]
            decisions = self._shared.eligibility_decisions(current.opportunity_set_id)
            if not any(value.state is EligibilityState.ELIGIBLE for value in decisions):
                facts.append(
                    ResearchAgendaSourceFact.create(
                        fact_id=f"zero-shared-eligible-{current.opportunity_set_id}",
                        fact_type="ZERO_SHARED_ELIGIBLE",
                        venue=ResearchVenue.CROSS_VENUE,
                        market_scope=tuple(
                            sorted(
                                {
                                    value.venue.value
                                    for identity in current.projection_ids
                                    if (value := self._shared.projection(identity)) is not None
                                }
                            )
                        )
                        or (SharedVenue.CRYPTO.value, SharedVenue.KALSHI.value),
                        state="BLOCKED",
                        summary=(
                            "The exact current Shared Opportunity set contains zero eligible "
                            "records."
                        ),
                        evidence_ids=(current.opportunity_set_id,),
                        evidence_hashes=(current.canonical_hash,),
                        observed_at=current.created_at,
                    )
                )
        allocation_decisions = self._allocation.decisions()
        if allocation_decisions:
            allocation = allocation_decisions[-1]
            if not allocation.paper_trial_proposal_ids:
                facts.append(
                    ResearchAgendaSourceFact.create(
                        fact_id=f"zero-paper-ready-{allocation.decision_id}",
                        fact_type="ZERO_PAPER_READY",
                        venue=ResearchVenue.CROSS_VENUE,
                        market_scope=("CURRENT_CROSS_VENUE_ALLOCATION",),
                        state="BLOCKED",
                        summary=(
                            "The current authority-free allocation retained zero PAPER-ready "
                            "trial proposals."
                        ),
                        evidence_ids=(allocation.decision_id,),
                        evidence_hashes=(allocation.canonical_hash,),
                        observed_at=allocation.decided_at,
                    )
                )
        for health in self._performance.health_assessments():
            venue = self._health_venue(health.strategy_or_method_id)
            if health.failed_thresholds and venue is not None:
                facts.append(
                    ResearchAgendaSourceFact.create(
                        fact_id=f"failed-health-{health.health_id}",
                        fact_type="FAILED_STRATEGY_HEALTH",
                        venue=venue,
                        market_scope=(health.strategy_or_method_id,),
                        state=health.state.value,
                        summary=(
                            "Retained strategy-health evidence contains failed descriptive "
                            "thresholds."
                        ),
                        evidence_ids=(health.health_id,),
                        evidence_hashes=(health.canonical_hash,),
                        observed_at=health.window_end,
                    )
                )
        # Counterfactual Outcome Feedback v1, Phase 6. One fact per cohort
        # that has *ever* reached ``QUALIFIED`` -- anchored to the single
        # earliest such row for that cohort (``earliest_qualified_per_
        # cohort``), never the latest. This is deliberate, not an
        # oversight: a fact anchored to the freshest daily aggregate would
        # give this fact a fresh ``fact_id``/``observed_at`` every cycle the
        # underlying evidence merely continues to accumulate, which would
        # either spam a new agenda item daily or -- if the id were made
        # cohort-stable while ``observed_at`` still drifted -- raise
        # ``RESEARCH_OPERATIONS_IDENTITY_CONFLICT`` the first time the
        # aggregate's own counts genuinely changed underneath a reused id.
        # Anchoring to the fixed earliest-qualified row makes both the id
        # and the hash permanently stable once a cohort first qualifies:
        # exactly one agenda item per cohort, ever, matching "no agenda
        # spam" -- a human can always inspect the freshest aggregate
        # separately (``aggregates_for_cohort``) if they want current
        # numbers.  ``INSUFFICIENT_SAMPLE``/``VENUE_ECONOMIC_REDUCTION_
        # UNAVAILABLE`` cohorts never reach here at all
        # (``earliest_qualified_per_cohort`` only ever returns ``QUALIFIED``
        # rows), which is what keeps an underpowered cell from ever
        # manufacturing a faux statistical conclusion in the agenda.
        for aggregate in self._counterfactual_outcome.earliest_qualified_per_cohort():
            # Counterfactual V2 Research Evidence Quarantine v1: a
            # quarantined cohort emits no fact, so no *new* agenda item is
            # ever created from one.
            #
            # Deliberately not the only enforcement point, and assuming it
            # was would have left the live gap wide open: ``Generate
            # ResearchProposals`` draws its candidates from the *retained
            # agenda item table*, not from this cycle's facts, so the 26
            # items created before the defect was known (13 still without a
            # proposal) are unaffected by anything done here. See
            # ``generation_context`` above, and the executor.
            #
            # Withheld here means: this cohort originates no *new* research
            # work from now on. It does not mean the aggregate is invalid,
            # hidden, or gone -- the row is still computed every cycle,
            # still returned by ``aggregates_for_cohort``/``census``, and
            # still carries its own unchanged explanation and hash; and any
            # agenda item, proposal, or feasibility row it already produced
            # stays exactly as retained (see ``domain.counterfactual_
            # followup.DEFECTIVE_FOLLOWUP_POLICY_VERSIONS``).
            #
            # Counterfactual Outcome Evidence Specificity v3: the single
            # domain choke point now also consults the outcome-policy
            # quarantine (CRYPTO V1's raw-count qualification) and V3's own
            # ORIGIN_EVIDENCE-only sourcing selection -- one call, three
            # gates, identical at every enforcement point.
            if (
                counterfactual_outcome_sourcing_exclusion(
                    venue=aggregate.venue,
                    outcome_policy_version=aggregate.outcome_policy_version,
                    followup_policy_version=aggregate.followup_policy_version,
                    baseline_kind=aggregate.baseline_kind,
                )
                is not None
            ):
                continue
            facts.append(
                ResearchAgendaSourceFact.create(
                    fact_id=(f"{_COUNTERFACTUAL_OUTCOME_FACT_ID_PREFIX}{aggregate.cohort_id}"),
                    fact_type="COUNTERFACTUAL_OUTCOME_PATTERN",
                    venue=self._counterfactual_outcome_venue(aggregate),
                    market_scope=(aggregate.strategy_or_method_id,),
                    state=aggregate.qualification_state.value,
                    summary=aggregate.explanation,
                    evidence_ids=(aggregate.aggregate_id,),
                    evidence_hashes=(aggregate.canonical_hash,),
                    observed_at=aggregate.generated_at,
                )
            )
        # Securities Structure Forward Performance v1, mirroring Phase 6
        # immediately above exactly (same "anchor to the single earliest
        # QUALIFIED row per cohort, never the latest" reasoning -- see that
        # block's own comment): one fact per structure-type cohort of
        # *pursued* (``ADVANCED``) SECURITIES decisions that has ever
        # reached ``QUALIFIED``. Always venue ``SECURITIES`` directly (no
        # venue-mapping helper is needed the way ``_counterfactual_outcome_
        # venue`` maps CRYPTO/KALSHI: this repository only ever retains
        # SECURITIES-derived aggregates in the first place).
        securities_structure_aggregates = (
            self._securities_structure_outcome.earliest_qualified_per_cohort()
        )
        for structure_aggregate in securities_structure_aggregates:
            # Securities Follow-Up Temporal Integrity v1: only cohorts built
            # over the canonical SECURITIES evidence-quality verdict (V2,
            # which rejects terminal prices from a later session) originate
            # new research work.  A V1 cohort is withheld from new sourcing
            # only; its retained aggregates and any agenda item it already
            # produced stay exactly as retained (the six existing items are
            # anchored to 2026-08-26..09-03 aggregates, which predate every
            # late terminal observation -- see docs/SECURITIES_FOLLOWUP_
            # TEMPORAL_INTEGRITY_V1.md).
            if (
                structure_aggregate.evidence_quality_policy_version
                != CURRENT_SECURITIES_STRUCTURE_EVIDENCE_QUALITY_POLICY_VERSION
            ):
                continue
            facts.append(
                ResearchAgendaSourceFact.create(
                    fact_id=f"securities-structure-outcome-{structure_aggregate.cohort_id}",
                    fact_type="SECURITIES_STRUCTURE_OUTCOME_PATTERN",
                    venue=ResearchVenue.SECURITIES,
                    market_scope=(
                        structure_aggregate.strategy_or_method_id,
                        structure_aggregate.structure_type,
                    ),
                    state=structure_aggregate.qualification_state.value,
                    summary=structure_aggregate.explanation,
                    evidence_ids=(structure_aggregate.aggregate_id,),
                    evidence_hashes=(structure_aggregate.canonical_hash,),
                    observed_at=structure_aggregate.generated_at,
                )
            )
        return tuple(sorted(facts, key=lambda value: value.fact_id))
