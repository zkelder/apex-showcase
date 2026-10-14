"""Read-only evidence source for Counterfactual Outcome Feedback v1.

Composes the already-retained counterfactual ledger, classification,
follow-up, and shared-opportunity-projection repositories into the one
``application.counterfactual_outcome.CounterfactualOutcomeEvidenceSource``
Protocol the aggregation service depends on -- mirrors the established
``*_sources.py`` composition idiom (``research_operations_sources.py``,
``shared_opportunity_sources.py``, ``total_market_context_sources.py``).
No provider client, credential, or network capability; every read here is
local PostgreSQL evidence."""

from __future__ import annotations

from collections.abc import Collection, Mapping

from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
)
from apex.adapters.kalshi_baseline_quote_postgres import PostgresKalshiBaselineQuoteRepository
from apex.domain.counterfactual_classification import CounterfactualDecisionClassification
from apex.domain.counterfactual_followup import (
    DecisionFollowUpObservation,
    FollowUpEvidenceQualityAssessment,
)
from apex.domain.counterfactual_ledger import CounterfactualDecision
from apex.domain.kalshi_baseline_quote import KalshiBaselineQuoteEvidence
from apex.domain.shared_opportunity import SharedOpportunityProjection


class RetainedCounterfactualOutcomeSource:
    """Implements ``application.counterfactual_outcome.
    CounterfactualOutcomeEvidenceSource``. Every method returns the full
    currently-retained set with no filtering of its own -- version scoping
    (which classifier/evidence-quality-policy version to honor) is
    deliberately left to the one place that already owns it,
    ``domain.counterfactual_outcome.build_counterfactual_outcome_
    aggregates``, so the filtering rule is never duplicated between an
    adapter query and the domain policy."""

    def __init__(self, engine: Engine) -> None:
        self._followup = PostgresCounterfactualFollowUpRepository(engine)
        self._kalshi_baseline_quote = PostgresKalshiBaselineQuoteRepository(engine)

    def all_decisions(self) -> tuple[CounterfactualDecision, ...]:
        return self._followup.ledger.all_decisions()

    def all_classifications(self) -> tuple[CounterfactualDecisionClassification, ...]:
        return self._followup.ledger.classifications()

    def all_observations(self) -> tuple[DecisionFollowUpObservation, ...]:
        return self._followup.observations()

    def all_assessments(self) -> tuple[FollowUpEvidenceQualityAssessment, ...]:
        return self._followup.assessments()

    def all_kalshi_baseline_quotes(self) -> tuple[KalshiBaselineQuoteEvidence, ...]:
        # Kalshi Baseline Quote Capture & Economic Maturation v1: the table
        # this reads now also retains ``MARKET_QUOTE_OBSERVATION`` rows
        # (``KalshiMarketQuoteBaselineEvidence``), which never feed
        # ``reduce_kalshi_binary_complement_outcome``/
        # ``COUNTERFACTUAL_OUTCOME_POLICY_V2`` -- no capital was ever
        # modeled as deployed against a plain quote, so there is no return
        # to reduce. ``matched_trade_quotes()`` filters to the
        # ``MATCHED_TRADE_EXECUTION`` subset at the SQL layer, keeping that
        # exclusion structural rather than a convention this call site would
        # otherwise have to remember on every future read.
        return self._kalshi_baseline_quote.matched_trade_quotes()

    def projections_by_id(
        self, projection_ids: Collection[str]
    ) -> Mapping[str, SharedOpportunityProjection]:
        return self._followup.ledger.shared_opportunity.projections_by_id(projection_ids)
