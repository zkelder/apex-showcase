"""Cross-Market Research v3: Fundamental and Market Evidence Integration
with Forward Options Seam.

Controlled hypothesis research testing whether point-in-time corporate
fundamental/filing evidence adds reproducible research value beyond
price/market context alone. This is *not* strategy generation and *not*
execution -- every retained record below is ``authority = "NONE"``.

This module is the third generation of Apex's bounded, governed research-
agenda layer, and is deliberately additive, not a rewrite:

* ``domain.cross_market_research`` (v1, price-only) supplies every genuinely
  generic primitive this module reuses verbatim: ``ConditionRule``,
  ``ProposalSource``, ``DiscoveryStatus``, ``MarketId``,
  ``CrossMarketRegimeDimension``/``CrossMarketRegimeSnapshot``,
  ``StabilityReport``/``derive_stability_state``, the append-only knowledge
  base (``ResearchKnowledgeEntry``, ``ResearchContradiction``,
  ``FollowUpProposal``, ``AgendaQualityReport``), the resumable
  ``AgendaRun`` cursor, ``HypothesisExecutionRecord``, and the generic
  ``DatedValue``/series-math toolkit. Two of its ``HypothesisTemplateId``
  members (``CONDITIONAL_RELATIONSHIP_V1``, ``INTERACTION_V1``) are reused
  unmodified because their meaning was never price-feature-specific; three
  new members (``CROSS_SECTIONAL_V1``, ``CORPORATE_CHANGE_V1``,
  ``FILING_EVENT_V1``) were added there, additively, for the two shapes v1
  never needed.
* ``domain.research_methodology`` supplies the *entire* governed trial
  lifecycle this module drives every hypothesis through unmodified:
  ``ResearchGovernancePolicy``, ``ResearchTrial``, ``ResearchTrialFamily``
  and multiple-testing correction, ``RobustnessCheckRun``/summary,
  ``ResearchConclusion``/``derive_conclusion_state``, and
  ``PromotionEvaluation``. Two ``RobustnessCheckType`` members
  (``WINSORIZED_SENSITIVITY``, ``MEDIAN_VS_MEAN_COMPARISON``) were added
  there, additively, for corporate research's own section-19 requirement.
* ``domain.research_calibration``/``research_calibration_v2`` supply
  ``EvidenceGrade``/``derive_evidence_grade``, ``NullControlFamily``/
  ``NullControlNature``/``NullControlDesignFamily``/``NullGenerationMethod``,
  ``ConditioningShape``, and ``MatchedCalibrationResult`` unmodified. Three
  ``ResearchDesign`` members (``CROSS_SECTIONAL``, ``CHANGE_BASED``,
  ``FILING_EVENT``) were added there, additively, for the two shapes v2's
  taxonomy never needed (``CONDITIONAL``/``INTERACTION`` are reused as-is).
* ``domain.corporate_fundamentals`` supplies every point-in-time fact,
  the six-year bounded dataset, and the bounded ``CorporateReplayFeatureFrame``
  projection this module reads -- it computes no new fundamental fact and
  applies no new anti-lookahead rule of its own.
* ``domain.research_cohort`` supplies the genuine, performance-blind
  sector/role declarations this module reuses for sector-relative outcomes
  and leave-one-sector-out robustness.

What is genuinely new here (and has no faithful representation in any of
the above): a corporate-evidence-only feature vocabulary bound to the
retained feature projection (section 3); a small outcome vocabulary and a
bounded 5/20/60-session horizon set (section 6); an agenda model whose
every genuine hypothesis is *structurally* required to carry a corporate
feature (``GeneratedCorporateHypothesis.feature_id`` is not optional --
there is no way to construct one without it, so section 2's "every genuine
hypothesis must require a corporate feature" is enforced by the type, not
by convention); five corporate-specific negative-control kinds (section 9);
a cross-sectional/shared-market-date panel-dependence diagnostic (section
15, genuinely new statistics -- pooling many issuers on the same date is
not the same non-independence problem the existing time-rolling-window
``OverlapDiagnostics`` measures, so this module computes its own cluster-
by-date effective sample and reports it *through* that same reused
dataclass shape, honestly relabelled); an incremental-contribution record
(section 8); a bounded corporate-contribution-accounting seam (section 22);
and a purely descriptive, current-only options-forward seam (section 23).

Non-goals (deliberate, matching this milestone's scope restrictions): no
free-form hypothesis text, no parameter search, no composite fundamental-
quality score, no Strategy/Opportunity/PAPER/LIVE authority, no filing-text
LLM analysis, no sentiment, no new data provider.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum

from apex.domain.cross_market_research import ConditionRule, DiscoveryStatus, ProposalSource
from apex.domain.research_methodology import BenchmarkKind, OverlapDiagnostics, OverlapHandlingMode
from apex.domain.values import content_digest, deterministic_id

CORPORATE_MARKET_RESEARCH_V3 = "CROSS_MARKET_RESEARCH_V3_CORPORATE_EVIDENCE_INTEGRATION"
CORPORATE_MARKET_RESEARCH_WARNING = (
    "CORPORATE + MARKET CONTROLLED HYPOTHESIS RESEARCH EVIDENCE ONLY -- NOT STRATEGY "
    "GENERATION, NOT EXECUTION; NO STRATEGY, OPPORTUNITY, PAPER, OR LIVE AUTHORITY"
)
NEW_INFORMATION_REQUIREMENT_NOTE = (
    "Every genuine (non-negative-control) hypothesis in this agenda structurally requires a "
    "corporate fundamental or filing-event feature: GeneratedCorporateHypothesis.feature_id is "
    "a required CorporateFeatureId, never optional and never a pure price/BTC/volatility-only "
    "relationship -- this is enforced by the type, not by convention."
)
COMPOSITE_SCORE_PROHIBITION_NOTE = (
    "This module never builds a composite fundamental-quality score. Each CorporateFeatureId "
    "is evaluated as its own independent hypothesis; incremental-contribution and redundancy "
    "evidence (section 22) are retained separately, never averaged into one number."
)
SURVIVORSHIP_LIMITED = "SURVIVORSHIP_LIMITED"
POINT_IN_TIME_UNIVERSE_INCOMPLETE = "POINT_IN_TIME_UNIVERSE_INCOMPLETE"
EARNINGS_PROXY_LIMITATION = (
    "SEC Item 2.02 filing/acceptance evidence is a filing/earnings-proxy event only; it is "
    "never labeled an exact earnings-announcement timestamp unless separately evidenced."
)
APPROXIMATE_US_EQUITY_CLOSE_UTC_HOUR = 21
MARKET_CLOSE_APPROXIMATION_LIMITATION = (
    "APPROXIMATE_FIXED_UTC_MARKET_CLOSE_NOT_DST_AWARE: filing-event next-session shifting uses "
    "a fixed 21:00 UTC threshold (covers 4pm US Eastern under both EST and EDT conservatively) "
    "rather than a genuine per-session exchange-calendar close time."
)
OPTIONS_SEAM_STRUCTURALLY_READY = "OPTIONS_SEAM_STRUCTURALLY_READY"
OPTIONS_SEAM_FACTUALLY_UNAVAILABLE = "OPTIONS_SEAM_FACTUALLY_UNAVAILABLE_ZERO_FORWARD_SESSIONS"
OPTIONS_SEAM_CURRENT_DESCRIPTIVE_ONLY = (
    "Forward options evidence, when present, is surfaced as current descriptive context only; "
    "it is never attached to a historical frame and never enters training/validation/holdout."
)


class CorporateMarketResearchError(ValueError):
    """A v3 controlled-research invariant was violated."""


def _aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CorporateMarketResearchError(code)
    return value


def _text(value: str, code: str, *, maximum: int = 500) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise CorporateMarketResearchError(code)
    return normalized


def _bounded_decimal(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return value.quantize(Decimal("0.00000001"))


def _decimal_sqrt(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        if value < 0:
            raise CorporateMarketResearchError("CORPORATE_MARKET_SQRT_NEGATIVE_INPUT")
        return +value.sqrt()


# ==========================================================================
# Section 3: corporate feature vocabulary (bound to the retained
# point-in-time projection -- ``domain.corporate_fundamentals.
# CorporateReplayFeatureFrame`` supplies ten of these twelve fields
# verbatim; ``AMENDMENT_INDICATOR``/``FILING_LAG_FROM_PERIOD_END_DAYS`` are
# derived directly from already-visible ``CorporateFilingEvent`` evidence
# by ``application.corporate_market_research`` without changing that
# already-complete module).
# ==========================================================================


class CorporateFeatureId(StrEnum):
    """The entire corporate feature library for this agenda. A genuine
    hypothesis's ``feature_id`` must be one of the first twelve; nothing
    may construct a free-form or composite feature. The last two are
    synthetic negative-control-only baselines, mirroring
    ``cross_market_research.NEGATIVE_CONTROL_FEATURE_IDS``'s own pattern."""

    REVENUE_GROWTH_YOY = "REVENUE_GROWTH_YOY"
    NET_INCOME_GROWTH_YOY = "NET_INCOME_GROWTH_YOY"
    OPERATING_CASH_FLOW_GROWTH_YOY = "OPERATING_CASH_FLOW_GROWTH_YOY"
    OPERATING_MARGIN = "OPERATING_MARGIN"
    OPERATING_MARGIN_CHANGE_YOY = "OPERATING_MARGIN_CHANGE_YOY"
    LEVERAGE_LIABILITIES_TO_ASSETS = "LEVERAGE_LIABILITIES_TO_ASSETS"
    CASH_TO_ASSETS = "CASH_TO_ASSETS"
    SHARE_COUNT_CHANGE_YOY = "SHARE_COUNT_CHANGE_YOY"
    DAYS_SINCE_10Q_OR_10K = "DAYS_SINCE_10Q_OR_10K"
    RECENT_8K_INDICATOR = "RECENT_8K_INDICATOR"
    AMENDMENT_INDICATOR = "AMENDMENT_INDICATOR"
    FILING_LAG_FROM_PERIOD_END_DAYS = "FILING_LAG_FROM_PERIOD_END_DAYS"
    RANDOM_NOISE_BASELINE = "RANDOM_NOISE_BASELINE"
    ALWAYS_PRESENT_BASELINE = "ALWAYS_PRESENT_BASELINE"


GENUINE_CORPORATE_FEATURE_IDS = frozenset(
    {
        CorporateFeatureId.REVENUE_GROWTH_YOY,
        CorporateFeatureId.NET_INCOME_GROWTH_YOY,
        CorporateFeatureId.OPERATING_CASH_FLOW_GROWTH_YOY,
        CorporateFeatureId.OPERATING_MARGIN,
        CorporateFeatureId.OPERATING_MARGIN_CHANGE_YOY,
        CorporateFeatureId.LEVERAGE_LIABILITIES_TO_ASSETS,
        CorporateFeatureId.CASH_TO_ASSETS,
        CorporateFeatureId.SHARE_COUNT_CHANGE_YOY,
        CorporateFeatureId.DAYS_SINCE_10Q_OR_10K,
        CorporateFeatureId.RECENT_8K_INDICATOR,
        CorporateFeatureId.AMENDMENT_INDICATOR,
        CorporateFeatureId.FILING_LAG_FROM_PERIOD_END_DAYS,
    }
)
# Growth/level features whose "change" reading is a genuine YoY comparison
# already computed by ``compute_derived_features`` -- the CHANGE_BASED_V1
# template may only bind to one of these (never to a pure level like
# ``OPERATING_MARGIN`` -- that belongs to CROSS_SECTIONAL_V1 instead).
CHANGE_BASED_ELIGIBLE_FEATURE_IDS = frozenset(
    {
        CorporateFeatureId.OPERATING_MARGIN_CHANGE_YOY,
        CorporateFeatureId.SHARE_COUNT_CHANGE_YOY,
        CorporateFeatureId.OPERATING_CASH_FLOW_GROWTH_YOY,
        CorporateFeatureId.REVENUE_GROWTH_YOY,
        CorporateFeatureId.NET_INCOME_GROWTH_YOY,
    }
)


# ==========================================================================
# Section 6: outcome vocabulary and bounded horizon set
# ==========================================================================


class CorporateOutcomeId(StrEnum):
    SUBSEQUENT_RETURN = "SUBSEQUENT_RETURN"
    SUBSEQUENT_REALIZED_VOLATILITY = "SUBSEQUENT_REALIZED_VOLATILITY"
    DRAWDOWN = "DRAWDOWN"
    BENCHMARK_RELATIVE_RETURN = "BENCHMARK_RELATIVE_RETURN"
    SECTOR_RELATIVE_RETURN = "SECTOR_RELATIVE_RETURN"


ALLOWED_HORIZONS_SESSIONS: tuple[int, ...] = (5, 20, 60)


# ==========================================================================
# Section 5: research designs / hypothesis templates and families
# ==========================================================================


class CorporateHypothesisFamily(StrEnum):
    """The multiple-testing partition (section 20): one ``ResearchTrialFamily``
    is built per member below, bundling every genuine hypothesis sharing
    that family so correction happens within each family separately --
    never pooled across unrelated designs."""

    CROSS_SECTIONAL_LEVEL = "CROSS_SECTIONAL_LEVEL"
    CROSS_SECTIONAL_CHANGE = "CROSS_SECTIONAL_CHANGE"
    FILING_EVENT = "FILING_EVENT"
    CONDITIONAL_MARKET_REGIME = "CONDITIONAL_MARKET_REGIME"
    INTERACTION_FUNDAMENTAL_MARKET = "INTERACTION_FUNDAMENTAL_MARKET"
    NEGATIVE_CONTROL = "NEGATIVE_CONTROL"


# Section 9's minimum required negative-control categories. Extends
# (never redefines) ``cross_market_research.NEGATIVE_CONTROL_KINDS`` and
# ``research_calibration.NULL_CONTROL_KINDS`` with corporate-specific kinds,
# exactly mirroring how each of those two already extended its predecessor.
CORPORATE_NEGATIVE_CONTROL_KINDS = (
    "TEMPORALLY_DISPLACED_CORPORATE_FACT",
    "MISMATCHED_ISSUER_FUNDAMENTALS",
    "RANDOMIZED_ISSUER_ASSIGNMENT",
    "UNRELATED_CORPORATE_CONCEPT_OUTCOME_PAIRING",
    "RANDOM_FILING_EVENT_DATE_MATCHED_CALENDAR",
)


# ==========================================================================
# Section 1: research agenda (the search-space budget)
# ==========================================================================


@dataclass(frozen=True, slots=True)
class CorporateMarketResearchAgenda:
    """The declared, bounded, immutable search-space budget (section 1).
    Registering an agenda is the only way its hypotheses may later be
    proposed; every bound below is a hard ceiling re-checked at hypothesis-
    build time. The search budget is never widened after this is
    registered -- ``CorporateMarketResearchAgenda`` has no update method."""

    agenda_id: str
    version: str
    objective: str
    corporate_dataset_id: str
    equity_snapshot_id: str
    replay_run_id: str
    cross_market_dataset_id: str
    cohort_policy_id: str
    allowed_feature_ids: tuple[CorporateFeatureId, ...]
    allowed_outcome_ids: tuple[CorporateOutcomeId, ...]
    allowed_horizons_sessions: tuple[int, ...]
    allowed_templates: tuple[str, ...]
    minimum_genuine_hypothesis_count: int
    maximum_genuine_hypothesis_count: int
    maximum_hypothesis_count: int
    maximum_configurations_per_hypothesis: int
    maximum_total_trials: int
    train_validation_holdout_policy: str
    multiple_testing_family_policy: str
    robustness_requirements: tuple[str, ...]
    negative_control_kinds: tuple[str, ...]
    minimum_matched_null_count: int
    lookback_years: int
    registered_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        version: str,
        objective: str,
        corporate_dataset_id: str,
        equity_snapshot_id: str,
        replay_run_id: str,
        cross_market_dataset_id: str,
        cohort_policy_id: str,
        allowed_feature_ids: tuple[CorporateFeatureId | str, ...],
        allowed_outcome_ids: tuple[CorporateOutcomeId | str, ...],
        allowed_horizons_sessions: tuple[int, ...],
        allowed_templates: tuple[str, ...],
        maximum_genuine_hypothesis_count: int,
        maximum_hypothesis_count: int,
        maximum_configurations_per_hypothesis: int,
        maximum_total_trials: int,
        train_validation_holdout_policy: str,
        multiple_testing_family_policy: str,
        robustness_requirements: tuple[str, ...],
        lookback_years: int,
        registered_at: datetime,
        negative_control_kinds: tuple[str, ...] = CORPORATE_NEGATIVE_CONTROL_KINDS,
        minimum_matched_null_count: int = 12,
    ) -> CorporateMarketResearchAgenda:
        if not (12 <= maximum_genuine_hypothesis_count <= 24):
            raise CorporateMarketResearchError("AGENDA_GENUINE_HYPOTHESIS_COUNT_MUST_BE_12_TO_24")
        if not (maximum_genuine_hypothesis_count <= maximum_hypothesis_count <= 60):
            raise CorporateMarketResearchError("AGENDA_TOTAL_HYPOTHESIS_COUNT_OUT_OF_BOUNDS")
        if not (1 <= maximum_configurations_per_hypothesis <= 6):
            raise CorporateMarketResearchError("AGENDA_CONFIGURATIONS_PER_HYPOTHESIS_OUT_OF_BOUNDS")
        if maximum_total_trials < maximum_hypothesis_count:
            raise CorporateMarketResearchError("AGENDA_TRIAL_BUDGET_TOO_SMALL_FOR_ITS_OWN_BOUNDS")
        if maximum_total_trials > 1_000:
            raise CorporateMarketResearchError("AGENDA_TRIAL_BUDGET_UNBOUNDED")
        features = tuple(CorporateFeatureId(item) for item in allowed_feature_ids)
        if not GENUINE_CORPORATE_FEATURE_IDS.issubset(set(features)):
            raise CorporateMarketResearchError("AGENDA_MUST_ALLOW_EVERY_GENUINE_CORPORATE_FEATURE")
        outcomes = tuple(CorporateOutcomeId(item) for item in allowed_outcome_ids)
        if not outcomes:
            raise CorporateMarketResearchError("AGENDA_REQUIRES_AT_LEAST_ONE_OUTCOME")
        horizons = tuple(sorted(set(allowed_horizons_sessions)))
        if not horizons or set(horizons) - set(ALLOWED_HORIZONS_SESSIONS):
            raise CorporateMarketResearchError("AGENDA_HORIZON_SET_MUST_BE_PREDECLARED_AND_BOUNDED")
        templates = tuple(sorted(set(allowed_templates)))
        if not templates:
            raise CorporateMarketResearchError("AGENDA_REQUIRES_AT_LEAST_ONE_TEMPLATE")
        if (
            not corporate_dataset_id
            or not equity_snapshot_id
            or not replay_run_id
            or not cross_market_dataset_id
        ):
            raise CorporateMarketResearchError("AGENDA_DATASET_LINEAGE_REQUIRED")
        if not cohort_policy_id:
            raise CorporateMarketResearchError("AGENDA_COHORT_POLICY_REQUIRED")
        if not (1 <= lookback_years <= 20):
            raise CorporateMarketResearchError("AGENDA_LOOKBACK_YEARS_INVALID")
        controls = tuple(sorted(set(negative_control_kinds)))
        if not set(CORPORATE_NEGATIVE_CONTROL_KINDS).issubset(set(controls)):
            raise CorporateMarketResearchError(
                "AGENDA_MUST_DECLARE_EVERY_REQUIRED_NEGATIVE_CONTROL_KIND"
            )
        if minimum_matched_null_count < 1:
            raise CorporateMarketResearchError("AGENDA_MINIMUM_MATCHED_NULL_COUNT_INVALID")
        requirements = tuple(sorted(set(robustness_requirements)))
        registered = _aware(registered_at, "AGENDA_REGISTERED_AT_INVALID")
        payload = (
            CORPORATE_MARKET_RESEARCH_V3,
            _text(version, "AGENDA_VERSION_INVALID", maximum=80),
            _text(objective, "AGENDA_OBJECTIVE_INVALID", maximum=2_000),
            corporate_dataset_id,
            equity_snapshot_id,
            replay_run_id,
            cross_market_dataset_id,
            cohort_policy_id,
            features,
            outcomes,
            horizons,
            templates,
            maximum_genuine_hypothesis_count,
            maximum_hypothesis_count,
            maximum_configurations_per_hypothesis,
            maximum_total_trials,
            _text(
                train_validation_holdout_policy, "AGENDA_PERIOD_POLICY_TEXT_INVALID", maximum=400
            ),
            _text(
                multiple_testing_family_policy,
                "AGENDA_MULTIPLE_TESTING_POLICY_TEXT_INVALID",
                maximum=400,
            ),
            requirements,
            controls,
            minimum_matched_null_count,
            lookback_years,
        )
        return cls(
            agenda_id=deterministic_id("corporate_market_research_agenda", payload),
            version=payload[1],
            objective=payload[2],
            corporate_dataset_id=corporate_dataset_id,
            equity_snapshot_id=equity_snapshot_id,
            replay_run_id=replay_run_id,
            cross_market_dataset_id=cross_market_dataset_id,
            cohort_policy_id=cohort_policy_id,
            allowed_feature_ids=features,
            allowed_outcome_ids=outcomes,
            allowed_horizons_sessions=horizons,
            allowed_templates=templates,
            minimum_genuine_hypothesis_count=12,
            maximum_genuine_hypothesis_count=maximum_genuine_hypothesis_count,
            maximum_hypothesis_count=maximum_hypothesis_count,
            maximum_configurations_per_hypothesis=maximum_configurations_per_hypothesis,
            maximum_total_trials=maximum_total_trials,
            train_validation_holdout_policy=payload[16],
            multiple_testing_family_policy=payload[17],
            robustness_requirements=requirements,
            negative_control_kinds=controls,
            minimum_matched_null_count=minimum_matched_null_count,
            lookback_years=lookback_years,
            registered_at=registered,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 7 (panel row types): the bounded per-(security, as_of) evidence
# every hypothesis reads. Built once by ``application.corporate_market_
# research.BuildCorporatePanel`` and shared by every hypothesis (section
# 30: "do not rebuild identical feature frames per hypothesis") -- these
# are pure, unvalidated data holders, never persisted on their own,
# exactly mirroring ``cross_market_research.CrossMarketObservation``.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class CorporateFeatureObservation:
    """One security's genuine, point-in-time-safe feature panel as of one
    session date. ``None`` means the feature was not yet computable at
    this ``as_of`` (insufficient prior-period evidence) -- never a
    fabricated value. ``snapshot_quality``/``missing_fields`` are carried
    through unmodified from ``CorporateReplayFeatureFrame``."""

    stable_security_id: str
    session_date: str
    as_of: datetime
    values: tuple[tuple[CorporateFeatureId, Decimal | None], ...]
    snapshot_quality: str
    missing_fields: tuple[str, ...]
    fact_lineage: tuple[str, ...]

    def feature(self, feature_id: CorporateFeatureId) -> Decimal | None:
        for key, value in self.values:
            if key is feature_id:
                return value
        return None


@dataclass(frozen=True, slots=True)
class CorporateOutcomeObservation:
    """One security's genuine forward-looking outcome set, measured from
    ``session_date`` to ``session_date + horizon_sessions`` sessions
    later. Never point-in-time-gated at ``session_date`` (an outcome is
    defined to look forward), mirroring
    ``cross_market_research.CrossMarketOutcomeSet``."""

    stable_security_id: str
    session_date: str
    horizon_sessions: int
    values: tuple[tuple[CorporateOutcomeId, Decimal | None], ...]
    complete: bool

    def outcome(self, outcome_id: CorporateOutcomeId) -> Decimal | None:
        for key, value in self.values:
            if key is outcome_id:
                return value
        return None


@dataclass(frozen=True, slots=True)
class CorporateFilingEventObservation:
    """One genuine filing-availability event usable by ``FILING_EVENT_V1``
    hypotheses (section 12). ``first_outcome_session_date`` is already
    shifted to the next applicable market session when
    ``event_available_at`` fell after the approximate close threshold or
    on a non-trading date -- callers never need to re-derive this."""

    stable_security_id: str
    filing_event_id: str
    form: str
    is_earnings_proxy: bool
    is_amendment: bool
    event_available_at: datetime
    event_session_date: str
    first_outcome_session_date: str
    shifted_to_next_session: bool


# ==========================================================================
# Section 4: sector-relative / sector-robustness support (reuses
# ``domain.research_cohort.CohortSector`` groupings -- no new sector
# taxonomy is invented here).
# ==========================================================================


class SectorRelativeState(StrEnum):
    OUTPERFORMED_SECTOR = "OUTPERFORMED_SECTOR"
    UNDERPERFORMED_SECTOR = "UNDERPERFORMED_SECTOR"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


def sector_relative_return(
    *, security_return: Decimal | None, sector_basket_return: Decimal | None
) -> Decimal | None:
    """The pure sector-relative-return primitive (section 6's outcome
    vocabulary): a security's own subsequent return minus its declared
    ``CohortSector`` basket's equal-weight subsequent return over the
    identical window. ``None`` when either side is unavailable -- never a
    zero-filled default."""

    if security_return is None or sector_basket_return is None:
        return None
    return _bounded_decimal(security_return - sector_basket_return)


# ==========================================================================
# Section 5 (continued): the hypothesis itself. ``feature_id`` is a
# required, non-optional ``CorporateFeatureId`` -- section 2's "every
# genuine hypothesis requires a corporate feature" is therefore enforced
# structurally: there is no code path that constructs a
# ``GeneratedCorporateHypothesis`` without one.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class GeneratedCorporateHypothesis:
    hypothesis_id: str
    agenda_id: str
    template_id: str
    template_version: str
    family: CorporateHypothesisFamily
    feature_id: CorporateFeatureId
    condition_rule: ConditionRule
    condition_label: str | None
    secondary_context_dimension: str | None
    secondary_condition_label: str | None
    outcome_id: CorporateOutcomeId
    horizon_sessions: int
    benchmark_kind: BenchmarkKind
    rationale: str
    proposal_source: ProposalSource
    is_negative_control: bool
    negative_control_kind: str | None
    control_parameters: tuple[tuple[str, str], ...]
    discovery_status: DiscoveryStatus
    ordinal: int
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        agenda: CorporateMarketResearchAgenda,
        template_id: str,
        template_version: str,
        family: CorporateHypothesisFamily | str,
        feature_id: CorporateFeatureId | str,
        condition_rule: ConditionRule | str,
        condition_label: str | None,
        secondary_context_dimension: str | None,
        secondary_condition_label: str | None,
        outcome_id: CorporateOutcomeId | str,
        horizon_sessions: int,
        benchmark_kind: BenchmarkKind | str,
        rationale: str,
        proposal_source: ProposalSource | str,
        is_negative_control: bool,
        negative_control_kind: str | None,
        discovery_status: DiscoveryStatus | str,
        ordinal: int,
        created_at: datetime,
        control_parameters: tuple[tuple[str, str], ...] = (),
    ) -> GeneratedCorporateHypothesis:
        if template_id not in agenda.allowed_templates:
            raise CorporateMarketResearchError("HYPOTHESIS_TEMPLATE_NOT_APPROVED_BY_AGENDA")
        feature = CorporateFeatureId(feature_id)
        if feature not in agenda.allowed_feature_ids:
            raise CorporateMarketResearchError("HYPOTHESIS_FEATURE_NOT_APPROVED_BY_AGENDA")
        if not is_negative_control and feature not in GENUINE_CORPORATE_FEATURE_IDS:
            raise CorporateMarketResearchError(
                "GENUINE_HYPOTHESIS_REQUIRES_A_REAL_CORPORATE_FEATURE_NOT_A_CONTROL_BASELINE"
            )
        if (
            template_id == "CORPORATE_CHANGE_V1"
            and feature not in CHANGE_BASED_ELIGIBLE_FEATURE_IDS
        ):
            raise CorporateMarketResearchError("CHANGE_BASED_TEMPLATE_REQUIRES_A_CHANGE_FEATURE")
        rule = ConditionRule(condition_rule)
        family_value = CorporateHypothesisFamily(family)
        if (
            template_id in ("CONDITIONAL_RELATIONSHIP_V1", "INTERACTION_V1")
            and secondary_context_dimension is None
        ):
            raise CorporateMarketResearchError(
                "CONDITIONAL_OR_INTERACTION_TEMPLATE_REQUIRES_SECONDARY_CONTEXT"
            )
        outcome = CorporateOutcomeId(outcome_id)
        if outcome not in agenda.allowed_outcome_ids:
            raise CorporateMarketResearchError("HYPOTHESIS_OUTCOME_NOT_APPROVED_BY_AGENDA")
        if horizon_sessions not in agenda.allowed_horizons_sessions:
            raise CorporateMarketResearchError("HYPOTHESIS_HORIZON_NOT_APPROVED_BY_AGENDA")
        benchmark = BenchmarkKind(benchmark_kind)
        source = ProposalSource(proposal_source)
        if source is not ProposalSource.DETERMINISTIC_RULE:
            raise CorporateMarketResearchError(
                "ASSISTED_MODEL_PROPOSALS_ARE_DISABLED_THIS_MILESTONE"
            )
        status = DiscoveryStatus(discovery_status)
        if is_negative_control != (status is DiscoveryStatus.NEGATIVE_CONTROL):
            raise CorporateMarketResearchError("NEGATIVE_CONTROL_FLAG_MUST_MATCH_DISCOVERY_STATUS")
        if is_negative_control:
            if negative_control_kind not in agenda.negative_control_kinds:
                raise CorporateMarketResearchError("NEGATIVE_CONTROL_KIND_NOT_APPROVED_BY_AGENDA")
            if family_value is not CorporateHypothesisFamily.NEGATIVE_CONTROL:
                raise CorporateMarketResearchError("NEGATIVE_CONTROL_MUST_USE_CONTROL_FAMILY")
        elif family_value is CorporateHypothesisFamily.NEGATIVE_CONTROL:
            raise CorporateMarketResearchError("GENUINE_HYPOTHESIS_MUST_NOT_USE_CONTROL_FAMILY")
        if ordinal < 0:
            raise CorporateMarketResearchError("HYPOTHESIS_ORDINAL_INVALID")
        created = _aware(created_at, "HYPOTHESIS_CREATED_AT_INVALID")
        params = tuple(sorted(control_parameters))
        payload = (
            CORPORATE_MARKET_RESEARCH_V3,
            agenda.agenda_id,
            template_id,
            _text(template_version, "TEMPLATE_VERSION_INVALID", maximum=40),
            family_value,
            feature,
            rule,
            condition_label,
            secondary_context_dimension,
            secondary_condition_label,
            outcome,
            horizon_sessions,
            benchmark,
            _text(rationale, "HYPOTHESIS_RATIONALE_INVALID", maximum=1_000),
            source,
            is_negative_control,
            negative_control_kind,
            params,
            status,
            ordinal,
        )
        return cls(
            hypothesis_id=deterministic_id("corporate_market_hypothesis", payload),
            agenda_id=agenda.agenda_id,
            template_id=template_id,
            template_version=payload[3],
            family=family_value,
            feature_id=feature,
            condition_rule=rule,
            condition_label=condition_label,
            secondary_context_dimension=secondary_context_dimension,
            secondary_condition_label=secondary_condition_label,
            outcome_id=outcome,
            horizon_sessions=horizon_sessions,
            benchmark_kind=benchmark,
            rationale=payload[13],
            proposal_source=source,
            is_negative_control=is_negative_control,
            negative_control_kind=negative_control_kind,
            control_parameters=params,
            discovery_status=status,
            ordinal=ordinal,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 15: panel-dependence diagnostics. Genuinely new statistics --
# reuses ``research_methodology.OverlapDiagnostics``'s *shape* (raw vs.
# effective sample, handling mode, inference availability) because that
# shape is generic to "how much does non-independence shrink my usable
# sample", but computes it via cluster-by-shared-market-date, never via
# the time-rolling-window overlap the reused type's own
# ``compute_overlap_diagnostics`` measures. This is why every ``note``
# this function produces says "PANEL_" up front -- never silently
# presented as the time-overlap diagnostic it is not.
# ==========================================================================

MINIMUM_EFFECTIVE_PANEL_CLUSTERS = 8


@dataclass(frozen=True, slots=True)
class CorporatePanelRow:
    """One (security, as_of) sample value entering a trial -- the
    corporate-research analogue of a single ``HistoricalResearchEvaluation``
    outcome, kept intentionally minimal and never persisted on its own
    (rebuildable at any time from the retained raw filings/facts/bars)."""

    stable_security_id: str
    session_date: str
    value: Decimal


def compute_panel_dependence_diagnostics(
    rows: Sequence[CorporatePanelRow],
) -> OverlapDiagnostics:
    """Clusters ``rows`` by ``session_date`` (every security sharing one
    market date is one cluster, not an independent sample -- milestone
    section 15) and reports a conservative effective sample equal to the
    number of distinct dates, exactly the same "one cluster ~= one
    independent unit" convention ``research_methodology.
    compute_overlap_diagnostics`` already uses for time-rolling windows
    (there: raw / horizon; here: raw / mean cluster size, which reduces to
    the distinct-date count). Descriptive statistics always remain
    available on the caller's raw sample; only *inference* is suppressed
    when the distinct-date count is thin."""

    raw = len(rows)
    if raw == 0:
        return OverlapDiagnostics(
            overlapping_pair_count=0,
            raw_sample_count=0,
            effective_sample_count=Decimal(0),
            handling_mode=OverlapHandlingMode.EXCLUDED_FROM_INFERENCE,
            inference_available=False,
            note="PANEL_EMPTY_SAMPLE",
        )
    by_date: dict[str, int] = {}
    for row in rows:
        by_date[row.session_date] = by_date.get(row.session_date, 0) + 1
    distinct_dates = len(by_date)
    clustered_dates = sum(1 for count in by_date.values() if count > 1)
    if distinct_dates >= MINIMUM_EFFECTIVE_PANEL_CLUSTERS:
        return OverlapDiagnostics(
            overlapping_pair_count=clustered_dates,
            raw_sample_count=raw,
            effective_sample_count=Decimal(distinct_dates),
            handling_mode=OverlapHandlingMode.RETAINED_DESCRIPTIVE,
            inference_available=True,
            note=(
                f"PANEL_SHARED_MARKET_DATE_DEPENDENCE: {raw} (security, date) rows cluster into "
                f"{distinct_dates} distinct market dates ({clustered_dates} dates shared by more "
                "than one security); effective sample uses the distinct-date count, not the raw "
                "row count."
            ),
        )
    return OverlapDiagnostics(
        overlapping_pair_count=clustered_dates,
        raw_sample_count=raw,
        effective_sample_count=Decimal(distinct_dates),
        handling_mode=OverlapHandlingMode.EXCLUDED_FROM_INFERENCE,
        inference_available=False,
        note=(
            f"PANEL_SHARED_MARKET_DATE_DEPENDENCE_INSUFFICIENT_DISTINCT_DATES: only "
            f"{distinct_dates} distinct market dates back {raw} rows (minimum "
            f"{MINIMUM_EFFECTIVE_PANEL_CLUSTERS}) -- descriptive statistics retained, inference "
            "suppressed."
        ),
    )


def clustered_bootstrap_means(
    rows: Sequence[CorporatePanelRow], *, seed: int, resample_count: int = 500
) -> tuple[Decimal, ...]:
    """A block/cluster bootstrap by ``session_date`` (resample dates with
    replacement, keep every row for each resampled date) -- the panel
    analogue of ``research_methodology.seeded_bootstrap_confidence_interval``'s
    plain row-level resampling, which would understate variance for
    clustered panel data. Deterministic and seeded, matching this
    codebase's stdlib-only convention."""

    dates: dict[str, list[Decimal]] = {}
    for row in rows:
        dates.setdefault(row.session_date, []).append(row.value)
    ordered_dates = tuple(sorted(dates))
    if len(ordered_dates) < 2:
        return ()
    generator = random.Random(seed)
    means: list[Decimal] = []
    with localcontext() as context:
        context.prec = 28
        for _ in range(resample_count):
            resample: list[Decimal] = []
            for _pick in range(len(ordered_dates)):
                chosen_date = ordered_dates[generator.randrange(len(ordered_dates))]
                resample.extend(dates[chosen_date])
            if resample:
                means.append(sum(resample, Decimal(0)) / Decimal(len(resample)))
    return tuple(means)


# ==========================================================================
# Section 8: incremental-value evidence. Retains three benchmark-style
# comparisons side by side and never collapses them into one score.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class IncrementalContributionResult:
    hypothesis_id: str
    feature_id: CorporateFeatureId
    context_dimension: str
    market_only_effect: Decimal | None
    corporate_only_effect: Decimal | None
    combined_effect: Decimal | None
    market_only_sample: int
    corporate_only_sample: int
    combined_sample: int
    incremental_delta_present: bool
    note: str
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        hypothesis_id: str,
        feature_id: CorporateFeatureId,
        context_dimension: str,
        market_only_effect: Decimal | None,
        corporate_only_effect: Decimal | None,
        combined_effect: Decimal | None,
        market_only_sample: int,
        corporate_only_sample: int,
        combined_sample: int,
    ) -> IncrementalContributionResult:
        if not hypothesis_id:
            raise CorporateMarketResearchError("INCREMENTAL_CONTRIBUTION_HYPOTHESIS_ID_REQUIRED")
        present = (
            combined_effect is not None
            and market_only_effect is not None
            and abs(combined_effect) > abs(market_only_effect)
        )
        note = (
            "COMBINED_EFFECT_EXCEEDS_MARKET_ONLY_BASELINE_IN_MAGNITUDE"
            if present
            else "COMBINED_EFFECT_DOES_NOT_EXCEED_MARKET_ONLY_BASELINE_OR_UNAVAILABLE"
        )
        payload = (
            CORPORATE_MARKET_RESEARCH_V3,
            hypothesis_id,
            feature_id,
            context_dimension,
            market_only_effect,
            corporate_only_effect,
            combined_effect,
            market_only_sample,
            corporate_only_sample,
            combined_sample,
        )
        return cls(
            hypothesis_id=hypothesis_id,
            feature_id=feature_id,
            context_dimension=context_dimension,
            market_only_effect=market_only_effect,
            corporate_only_effect=corporate_only_effect,
            combined_effect=combined_effect,
            market_only_sample=market_only_sample,
            corporate_only_sample=corporate_only_sample,
            combined_sample=combined_sample,
            incremental_delta_present=present,
            note=note,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 22: corporate contribution accounting seam (bounded -- not the
# full long-term Signal Contribution Accounting system).
# ==========================================================================


@dataclass(frozen=True, slots=True)
class CorporateContributionEvidence:
    """Retains just enough to later answer standalone value, incremental
    value, redundancy, coverage, and missingness burden for one genuine
    hypothesis's feature -- section 22's seam, not its complete system."""

    hypothesis_id: str
    feature_id: CorporateFeatureId
    standalone_effect_size: Decimal | None
    incremental_result_id: str | None
    redundant_with: tuple[tuple[CorporateFeatureId, Decimal], ...]
    coverage_fraction: Decimal | None
    missingness_burden_count: int
    notes: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        hypothesis_id: str,
        feature_id: CorporateFeatureId,
        standalone_effect_size: Decimal | None,
        incremental_result_id: str | None,
        redundant_with: tuple[tuple[CorporateFeatureId, Decimal], ...],
        coverage_fraction: Decimal | None,
        missingness_burden_count: int,
        notes: tuple[str, ...],
    ) -> CorporateContributionEvidence:
        if not hypothesis_id:
            raise CorporateMarketResearchError("CONTRIBUTION_EVIDENCE_HYPOTHESIS_ID_REQUIRED")
        if missingness_burden_count < 0:
            raise CorporateMarketResearchError("CONTRIBUTION_EVIDENCE_MISSINGNESS_INVALID")
        if coverage_fraction is not None and not (Decimal(0) <= coverage_fraction <= Decimal(1)):
            raise CorporateMarketResearchError("CONTRIBUTION_EVIDENCE_COVERAGE_OUT_OF_BOUNDS")
        redundancy = tuple(sorted(redundant_with, key=lambda item: item[0].value))
        payload = (
            CORPORATE_MARKET_RESEARCH_V3,
            hypothesis_id,
            feature_id,
            standalone_effect_size,
            incremental_result_id,
            redundancy,
            coverage_fraction,
            missingness_burden_count,
            tuple(sorted(set(notes))),
        )
        return cls(
            hypothesis_id=hypothesis_id,
            feature_id=feature_id,
            standalone_effect_size=standalone_effect_size,
            incremental_result_id=incremental_result_id,
            redundant_with=redundancy,
            coverage_fraction=coverage_fraction,
            missingness_burden_count=missingness_burden_count,
            notes=tuple(sorted(set(notes))),
            canonical_hash=content_digest(payload),
        )


def pearson_correlation(pairs: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    """Plain cross-sectional Pearson correlation over paired
    (feature_a, feature_b) values from the identical (security, as_of)
    rows -- used only for the bounded section-22 redundancy check, never
    for a composite score. Distinct from
    ``cross_market_research.series_correlation``, which correlates two
    *time series*; this correlates two *columns of one panel*."""

    values = list(pairs)
    if len(values) < 3:
        return None
    with localcontext() as context:
        context.prec = 28
        mean_a = sum((a for a, _b in values), Decimal(0)) / Decimal(len(values))
        mean_b = sum((b for _a, b in values), Decimal(0)) / Decimal(len(values))
        covariance = sum(((a - mean_a) * (b - mean_b) for a, b in values), Decimal(0))
        variance_a = sum(((a - mean_a) ** 2 for a, _b in values), Decimal(0))
        variance_b = sum(((b - mean_b) ** 2 for _a, b in values), Decimal(0))
        if variance_a <= 0 or variance_b <= 0:
            return None
        correlation = covariance / (_decimal_sqrt(variance_a) * _decimal_sqrt(variance_b))
    return _bounded_decimal(max(Decimal(-1), min(Decimal(1), correlation)))


# ==========================================================================
# Section 23/24: forward options seam (structural only -- see module
# docstring; never attaches a current value to a historical frame).
# ==========================================================================


@dataclass(frozen=True, slots=True)
class OptionsForwardSeamSnapshot:
    """A purely descriptive, current-only snapshot of whatever forward
    options evidence Apex has genuinely retained (section 23). Never
    included in any hypothesis's train/validation/holdout sample --
    nothing in this module or ``application.corporate_market_research``
    ever reads this type when building a ``CorporatePanelRow``."""

    availability: str
    as_of: datetime | None
    underlying_symbols: tuple[str, ...]
    atm_iv_by_underlying: tuple[tuple[str, Decimal], ...]
    skew_state_by_underlying: tuple[tuple[str, str], ...]
    term_structure_shape_by_underlying: tuple[tuple[str, str], ...]
    expected_move_percent_by_underlying: tuple[tuple[str, Decimal], ...]
    iv_to_realized_volatility_by_underlying: tuple[tuple[str, Decimal], ...]
    limitations: tuple[str, ...]

    @classmethod
    def unavailable(cls, *, reason: str) -> OptionsForwardSeamSnapshot:
        return cls(
            availability=OPTIONS_SEAM_FACTUALLY_UNAVAILABLE,
            as_of=None,
            underlying_symbols=(),
            atm_iv_by_underlying=(),
            skew_state_by_underlying=(),
            term_structure_shape_by_underlying=(),
            expected_move_percent_by_underlying=(),
            iv_to_realized_volatility_by_underlying=(),
            limitations=(OPTIONS_SEAM_CURRENT_DESCRIPTIVE_ONLY, reason),
        )


def corporate_market_research_warnings() -> tuple[str, ...]:
    return (
        CORPORATE_MARKET_RESEARCH_WARNING,
        NEW_INFORMATION_REQUIREMENT_NOTE,
        COMPOSITE_SCORE_PROHIBITION_NOTE,
        SURVIVORSHIP_LIMITED,
        POINT_IN_TIME_UNIVERSE_INCOMPLETE,
        EARNINGS_PROXY_LIMITATION,
        MARKET_CLOSE_APPROXIMATION_LIMITATION,
        OPTIONS_SEAM_CURRENT_DESCRIPTIVE_ONLY,
        "PANEL_OBSERVATIONS_SHARE_MARKET_DATES_NOT_INDEPENDENT_SAMPLES",
    )
