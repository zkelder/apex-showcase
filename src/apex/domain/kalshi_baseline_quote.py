"""Kalshi Decision-Linked Baseline Quote Evidence v1.

This module owns no provider client, credential, network, execution, or
capital-mutation capability, and it introduces no new investment judgment of
its own.  It closes one real, previously-open evidence gap: Apex retains a
``CounterfactualDecision`` for every Kalshi opportunity Apex ever considered,
and (separately) retains later follow-up/settlement evidence
(``domain.counterfactual_followup``), but until this milestone it retained
**no durable decision-time ("baseline") quote** -- so
``domain.counterfactual_outcome`` could not (and honestly did not) compute an
economic return for any Kalshi cohort; every one was, correctly, classified
``VENUE_ECONOMIC_REDUCTION_UNAVAILABLE`` for lack of an entry price, exactly
as that module's own docstring already documented as its prerequisite for a
future milestone.

Evidence path this module attaches to (traced, not assumed):

    provider capture (adapters.kalshi_public)
    -> normalized market/orderbook evidence (domain.public_market,
       domain.public_complement's ``PublicOrderbookSnapshot``/
       ``PublicOrderbookLevel``)
    -> Kalshi opportunity evaluator (domain.public_complement's
       ``BinaryComplementEvaluation``/``ComplementFillSlice``, or
       domain.public_threshold's ``ThresholdBundleEvaluation``/
       ``ThresholdMatchedSlice``)
    -> SharedOpportunityProjection (adapters.shared_opportunity_sources)
    -> eligibility / CounterfactualDecision (domain.counterfactual_ledger)
    -> (this module) KalshiBaselineQuoteEvidence
    -> DecisionFollowUpObservation settlement (domain.counterfactual_followup,
       already retains ``settlement_result`` for a Kalshi follow-up)
    -> economic-reduction (this module's ``reduce_kalshi_binary_complement_
       outcome``) feeds an additive ``COUNTERFACTUAL_OUTCOME_POLICY_V2``
       (domain.counterfactual_outcome) -- V1 is untouched.

Design choices, so a later milestone can extend this without redesigning it:

* **Least invasive attachment point.**  A new, append-only table keyed by
  ``decision_id`` (FK to ``counterfactual_decision.decision_id``, the exact
  hook that module's own docstring already reserved -- see
  ``counterfactual_ledger.FUTURE_OUTCOME_HOOK``), rather than widening
  ``SharedOpportunityProjection`` or ``CounterfactualDecision`` themselves.
  Both of those are immutable, already-shipped, cross-venue-generic records;
  adding Kalshi-specific quote fields to either would either widen a
  venue-neutral contract for one venue's benefit or force an awkward
  ``None`` on every Crypto row forever.
* **Reuse evidence Apex already paid for.**  Every field on
  ``KalshiBaselineQuoteEvidence`` is copied verbatim from already-retained,
  already-immutable rows (``BinaryComplementEvaluation``/
  ``ComplementFillSlice`` or ``ThresholdBundleEvaluation``/
  ``ThresholdMatchedSlice``) that a ``CounterfactualDecision`` already
  references by exact id (``subject_source_record_id``) and hash
  (transitively, via ``SharedOpportunityProjection.source_canonical_hash``).
  Nothing here issues a new provider request, re-derives a price, or
  re-walks an orderbook; it only re-projects data Apex already captured and
  already committed to at decision time.  Because the source rows are
  immutable, capturing a baseline quote one cycle -- or one hundred cycles --
  after the decision was created reproduces the byte-identical result: there
  is no "staleness" risk to race against, only a bounded backlog to work
  through.
* **Baseline means "as of the exact referenced evaluation", never "as of
  whenever we got around to reading it."**  ``decision.subject_source_record_
  id``/``subject_source_record_type`` are fixed, immutable pointers set at
  decision-creation time.  This module only ever reads the *one* evaluation
  row those pointers name -- never "the current/latest" evaluation for that
  market -- so a capture that happens to run long after the decision was
  made can never silently substitute a later quote for the baseline one.
  ``captured_at``/``run_id`` are retained purely as *when Apex derived this
  row* provenance and participate in neither ``quote_id`` nor
  ``canonical_hash`` (mirrors ``CounterfactualOutcomeAggregate`` excluding
  ``generated_at`` and ``CounterfactualDecisionClassification`` excluding
  ``classified_at`` -- a genuine crash-resume replay under a different
  wall-clock instant must never manufacture a spurious identity conflict).
* **Full leg manifest, never a collapsed scalar.**  A binary-complement
  quote's legs are its already-matched ``ComplementFillSlice`` rows,
  ordinal-preserved.  A threshold-ladder quote's legs are its already-matched
  ``ThresholdMatchedSlice`` rows, each expanded into its two constituent legs
  (``THRESHOLD_BROADER_YES``/``THRESHOLD_NARROWER_NO``) since a slice pairs
  one fill on each of two distinct markets.  ``KalshiBaselineLegRole``
  disambiguates a leg's role explicitly rather than leaving it to be inferred
  from position.
* **Method isolation, honestly scoped.**  Capture (Phases 1-4) supports the
  two Kalshi evaluators with a persisted, immutable per-decision evaluation
  row to source from: ``BinaryComplementEvaluation`` and
  ``ThresholdBundleEvaluation``. ``ExhaustiveEventSetEvaluation`` is
  deliberately excluded from capture: it has no persisted table at all (see
  ``adapters.shared_opportunity_sources``) -- its ``evaluation_id`` is a
  content hash recomputed fresh from current rows on every read, so
  "capturing" it would mean *re-running* an N-leg reduction against
  whatever orderbook rows are currently latest, not reading an immutable,
  already-fixed decision-time row. That is a new computation, not a
  re-projection of evidence Apex already paid for, so it stays unsupported
  rather than forced.  The pure economic-reduction function (Phase 5,
  ``reduce_kalshi_binary_complement_outcome``) is scoped narrower still, to
  ``BinaryComplementEvaluation`` alone: it is the one case where the exact
  existing follow-up settlement path (``domain.counterfactual_followup``,
  ``DecisionFollowUpObservation.settlement_result``) already resolves a
  single, unambiguous market's clean YES/NO result.  A threshold-ladder
  decision's ``product_or_market_id`` names *two* markets
  (``"{broader}+{narrower}"``); the existing follow-up source's multi-leg
  branch (``adapters.counterfactual_followup_sources.KalshiFollowUpPublicMarket
  Source._read_event``) only ever returns a comma-joined, unstructured
  ``settlement_result`` string for that case (``MULTI_LEG_PRICE_LIMITATION``)
  -- a diagnostic string, never a designed per-leg settlement contract.
  Building a real structured dual-leg settlement capture is legitimate,
  bounded future work; parsing that comma-joined string here would be a
  fragile hack over a value never designed to support economic computation,
  not an honest reduction.  Threshold-ladder baseline quotes are still
  captured (Phases 1-4) -- the evidence is retained prospectively -- but
  their economic reduction stays explicitly unavailable in v1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from apex.domain.counterfactual_followup import DecisionFollowUpObservation
from apex.domain.counterfactual_ledger import CounterfactualDecision
from apex.domain.public_complement import (
    FEE_MODEL_IDENTITY,
    ROUNDING_IDENTITY,
    BinaryComplementEvaluation,
    ComplementFillSlice,
    ComplementState,
)
from apex.domain.public_threshold import (
    THRESHOLD_EVALUATOR_VERSION,
    ThresholdBundleEvaluation,
    ThresholdEvaluationState,
    ThresholdMatchedSlice,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import (
    ContentHash,
    ValueValidationError,
    content_digest,
    deterministic_id,
    parse_decimal,
)

KALSHI_BASELINE_QUOTE_POLICY_V1 = "KALSHI_BASELINE_QUOTE_POLICY_V1"
KALSHI_BASELINE_QUOTE_WARNING = (
    "KALSHI BASELINE QUOTE EVIDENCE — DECISION-TIME EVIDENCE ONLY — "
    "NO EXECUTABILITY, FILL, OR TRADING AUTHORITY — NOT A LIVE QUOTE"
)

# Kalshi Baseline Quote Capture & Economic Maturation v1: additive sibling
# policy version for ``KalshiMarketQuoteBaselineEvidence`` (see that class's
# own docstring, far below, for the full rationale). V1 above is completely
# unmodified -- its own rows, identity formula, and validation are untouched
# by anything added under this second policy version.
KALSHI_BASELINE_QUOTE_POLICY_V2 = "KALSHI_BASELINE_QUOTE_POLICY_V2"


class KalshiQuoteKind(StrEnum):
    """Which of the two structurally distinct evidence shapes one retained
    ``kalshi_baseline_quote`` row carries. Never inferred from
    ``policy_version`` alone at a read site that does not already import
    this module -- persisted as its own first-class column so an operator
    query never has to parse the JSON ``payload`` just to count rows by
    kind."""

    MATCHED_TRADE_EXECUTION = "MATCHED_TRADE_EXECUTION"
    MARKET_QUOTE_OBSERVATION = "MARKET_QUOTE_OBSERVATION"


# The only two Kalshi evaluators with a persisted, immutable, per-decision
# evaluation row to source a baseline quote from without any new computation.
# ``ExhaustiveEventSetEvaluation`` is deliberately absent -- see module
# docstring.
BINARY_COMPLEMENT_SOURCE_RECORD_TYPE = "BinaryComplementEvaluation"
THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE = "ThresholdBundleEvaluation"
SUPPORTED_BASELINE_QUOTE_SOURCE_RECORD_TYPES = frozenset(
    {BINARY_COMPLEMENT_SOURCE_RECORD_TYPE, THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE}
)

# Of those two, the one whose economic reduction (Phase 5) is defensible in
# v1 -- see module docstring's "Method isolation" note.
_REDUCIBLE_SOURCE_RECORD_TYPES = frozenset({BINARY_COMPLEMENT_SOURCE_RECORD_TYPE})

_RETURN_QUANTUM = Decimal("0.00000001")
_ONE = Decimal("1")


class KalshiBaselineQuoteError(ValueError):
    """Raised when retained baseline-quote inputs are invalid, reference an
    unsupported evaluator, or conflict with already-retained evidence."""


class KalshiBaselineLegRole(StrEnum):
    """Which economic role one retained leg plays. Disambiguates explicitly
    rather than leaving a reader to infer it from list position."""

    SINGLE_MARKET_COMPLEMENT = "SINGLE_MARKET_COMPLEMENT"
    THRESHOLD_BROADER_YES = "THRESHOLD_BROADER_YES"
    THRESHOLD_NARROWER_NO = "THRESHOLD_NARROWER_NO"


class KalshiQuoteLegRole(StrEnum):
    """Which market in the pair one ``KalshiMarketQuoteLeg`` describes.
    Deliberately a separate enum from ``KalshiBaselineLegRole`` above rather
    than reused: that enum's members name which *side was bought*
    (``..._YES``/``..._NO`` suffixes) for a matched-trade leg -- meaningless
    for a plain market-quote leg, which always retains *both* sides of its
    one market. Reusing it here would silently blur the exact
    trade-execution/market-observation distinction this module exists to
    keep sharp."""

    SINGLE_MARKET = "SINGLE_MARKET"
    THRESHOLD_BROADER = "THRESHOLD_BROADER"
    THRESHOLD_NARROWER = "THRESHOLD_NARROWER"


class SettlementValueBasis(StrEnum):
    """Whether ``KalshiBaselineQuoteEvidence.modeled_settlement_value`` is an
    exact, deterministic value or a conservative lower bound.  A binary
    complement's settlement value is *exact* by construction (holding both
    the YES and NO side of one market guarantees exactly
    ``matched_quantity`` dollars at settlement, regardless of which side
    resolves).  A threshold ladder's is only a conservative *floor*: the
    genuinely-guaranteed minimum, not necessarily the realized value (see
    ``public_threshold``'s own settlement-floor discussion -- an outcome
    strictly between the two thresholds pays both legs, exceeding the
    floor)."""

    EXACT_COMPLEMENT_VALUE = "EXACT_COMPLEMENT_VALUE"
    CONSERVATIVE_FLOOR_VALUE = "CONSERVATIVE_FLOOR_VALUE"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_decimal(value: Decimal) -> Decimal:
    # PostgreSQL returns the declared Numeric(38,18) display scale (e.g.
    # ``Decimal("0.500000000000000000")`` for a genuine "0.50"); immutable
    # hashes and domain validation retain only the authoritative fixed
    # value. Mirrors ``domain.public_complement``'s own identically-named,
    # identically-reasoned helper exactly -- V1's legs never needed this
    # (their values always arrive pre-normalized, already having passed
    # through that sibling helper on ``ComplementFillSlice``/
    # ``ThresholdMatchedSlice`` first), but the market-quote-observation
    # path added by Kalshi Baseline Quote Capture & Economic Maturation v1
    # reads raw ``market_orderbook_level.bid_price`` directly, with no such
    # intermediate normalization -- so this helper needs the same fix its
    # sibling already carries, not a second one.
    return parse_decimal(format(value.normalize(), "f"), allow_negative=True)


@dataclass(frozen=True, slots=True)
class KalshiBaselineQuoteLeg:
    """One deterministic, already-matched fill this baseline quote retains
    verbatim from an immutable retained evaluation row.  Never a live quote,
    never re-walked, never re-priced."""

    ordinal: int
    leg_role: KalshiBaselineLegRole
    market_ticker: str
    purchased_side: str
    source_level_id: str
    quantity: Decimal
    acquisition_price: Decimal
    acquisition_cost: Decimal
    modeled_fee: Decimal
    rounding_reserve: Decimal

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueValidationError("baseline quote leg ordinal must not be negative")
        if not self.market_ticker or not self.source_level_id:
            raise ValueValidationError("baseline quote leg identities must not be empty")
        if self.purchased_side not in {"YES", "NO"}:
            raise ValueValidationError("baseline quote leg side must be YES or NO")
        for name in (
            "quantity",
            "acquisition_price",
            "acquisition_cost",
            "modeled_fee",
            "rounding_reserve",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))
        if self.quantity <= 0:
            raise ValueValidationError("baseline quote leg quantity must be positive")
        if not Decimal(0) <= self.acquisition_price <= _ONE:
            raise ValueValidationError("baseline quote leg acquisition price must be in [0, 1]")
        if self.acquisition_cost != self.quantity * self.acquisition_price:
            raise ValueValidationError("baseline quote leg acquisition cost must be exact")
        if self.modeled_fee < 0 or self.rounding_reserve < 0:
            raise ValueValidationError("baseline quote leg costs must not be negative")

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class KalshiBaselineQuoteEvidence:
    """One immutable, append-only, decision-linked record of the Kalshi
    quote evidence genuinely available as of the exact retained evaluation a
    ``CounterfactualDecision`` already references -- never later settlement
    or orderbook state, never a hindsight-selected price.  Authority is
    always ``NONE``; this is evidence, not an executable quote, credit,
    fill, or trading signal."""

    quote_id: str
    policy_version: str
    decision_id: str
    decision_canonical_hash: str
    venue: SharedVenue
    subject_projection_id: str
    subject_source_record_type: str
    subject_source_record_id: str
    subject_source_canonical_hash: str
    instrument_id: str
    product_or_market_id: str
    market_tickers: tuple[str, ...]
    decision_evaluated_at: datetime
    evaluation_observed_at: tuple[datetime, ...]
    captured_at: datetime
    evaluator_version: str
    fee_model_identity: str
    rounding_identity: str
    legs: tuple[KalshiBaselineQuoteLeg, ...]
    matched_quantity: Decimal
    gross_acquisition_cost: Decimal
    total_modeled_fees: Decimal
    total_modeled_acquisition_cost: Decimal
    conservative_net_lower_bound: Decimal
    modeled_settlement_value: Decimal
    settlement_value_basis: str
    source_evidence_class: str
    run_id: str
    canonical_hash: str
    authority: str = "NONE"

    def __post_init__(self) -> None:
        if self.venue is not SharedVenue.KALSHI:
            raise ValueValidationError("baseline quote evidence is Kalshi-only")
        for name in (
            "quote_id",
            "policy_version",
            "decision_id",
            "decision_canonical_hash",
            "subject_projection_id",
            "subject_source_record_id",
            "subject_source_canonical_hash",
            "instrument_id",
            "product_or_market_id",
            "evaluator_version",
            "fee_model_identity",
            "rounding_identity",
            "source_evidence_class",
            "run_id",
            "canonical_hash",
        ):
            if not getattr(self, name):
                raise ValueValidationError(f"baseline quote {name} must not be empty")
        if self.subject_source_record_type not in SUPPORTED_BASELINE_QUOTE_SOURCE_RECORD_TYPES:
            raise ValueValidationError("baseline quote source record type is not supported")
        if self.settlement_value_basis not in {item.value for item in SettlementValueBasis}:
            raise ValueValidationError("baseline quote settlement value basis is invalid")
        if self.authority != "NONE":
            raise ValueValidationError("baseline quote evidence authority must be NONE")
        object.__setattr__(
            self,
            "decision_evaluated_at",
            _aware(self.decision_evaluated_at, "decision_evaluated_at"),
        )
        object.__setattr__(self, "captured_at", _aware(self.captured_at, "captured_at"))
        if not self.market_tickers:
            raise ValueValidationError("baseline quote must retain at least one market ticker")
        if not self.evaluation_observed_at:
            raise ValueValidationError("baseline quote must retain at least one observation time")
        object.__setattr__(
            self,
            "evaluation_observed_at",
            tuple(_aware(value, "evaluation_observed_at") for value in self.evaluation_observed_at),
        )
        if not self.legs:
            raise ValueValidationError("baseline quote must retain at least one leg")
        ordinals = [leg.ordinal for leg in self.legs]
        if ordinals != sorted(ordinals) or len(set(ordinals)) != len(ordinals):
            raise ValueValidationError("baseline quote legs must have strictly increasing ordinals")
        for name in (
            "matched_quantity",
            "gross_acquisition_cost",
            "total_modeled_fees",
            "total_modeled_acquisition_cost",
            "conservative_net_lower_bound",
            "modeled_settlement_value",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))
        if self.matched_quantity <= 0:
            raise ValueValidationError("baseline quote matched quantity must be positive")
        if (
            self.gross_acquisition_cost < 0
            or self.total_modeled_fees < 0
            or self.total_modeled_acquisition_cost < 0
            or self.modeled_settlement_value < 0
        ):
            raise ValueValidationError("baseline quote economic fields must not be negative")
        if self.conservative_net_lower_bound <= 0:
            raise ValueValidationError(
                "baseline quote conservative net lower bound must be positive "
                "(only CANDIDATE-state evaluations are ever captured)"
            )
        if (
            self.total_modeled_acquisition_cost
            != self.gross_acquisition_cost + self.total_modeled_fees
        ):
            raise ValueValidationError(
                "baseline quote total modeled acquisition cost must equal gross cost plus fees"
            )


def kalshi_baseline_quote_id(*, policy_version: str, decision_id: str) -> str:
    """Pure function of ``(policy_version, decision_id)`` only -- never of
    ``captured_at``/``run_id``.  At most one baseline quote row can ever
    exist per decision under one policy version; replaying the identical
    capture (this cycle, or one hundred cycles later) always reproduces the
    identical id."""

    return deterministic_id("kalshi_baseline_quote", (policy_version, decision_id))


def kalshi_baseline_quote_canonical_hash(
    *,
    quote_id: str,
    policy_version: str,
    decision_id: str,
    decision_canonical_hash: str,
    venue: SharedVenue | str,
    subject_projection_id: str,
    subject_source_record_type: str,
    subject_source_record_id: str,
    subject_source_canonical_hash: str,
    instrument_id: str,
    product_or_market_id: str,
    market_tickers: tuple[str, ...],
    decision_evaluated_at: datetime,
    evaluation_observed_at: tuple[datetime, ...],
    evaluator_version: str,
    fee_model_identity: str,
    rounding_identity: str,
    legs: tuple[KalshiBaselineQuoteLeg, ...],
    matched_quantity: Decimal,
    gross_acquisition_cost: Decimal,
    total_modeled_fees: Decimal,
    total_modeled_acquisition_cost: Decimal,
    conservative_net_lower_bound: Decimal,
    modeled_settlement_value: Decimal,
    settlement_value_basis: str,
    source_evidence_class: str,
) -> str:
    """The ``KalshiBaselineQuoteEvidence.canonical_hash`` formula, exposed so
    a future migration or diagnostic can recompute it byte-for-byte.
    Deliberately excludes ``captured_at``/``run_id`` -- both are "when Apex
    derived this row" provenance, never part of what the row attests is
    true, mirroring ``CounterfactualOutcomeAggregate`` excluding
    ``generated_at`` and ``CounterfactualDecisionClassification`` excluding
    ``classified_at`` for the identical crash-resume-replay reason."""

    payload = (
        KALSHI_BASELINE_QUOTE_POLICY_V1,
        quote_id,
        policy_version,
        decision_id,
        decision_canonical_hash,
        venue,
        subject_projection_id,
        subject_source_record_type,
        subject_source_record_id,
        subject_source_canonical_hash,
        instrument_id,
        product_or_market_id,
        market_tickers,
        decision_evaluated_at,
        evaluation_observed_at,
        evaluator_version,
        fee_model_identity,
        rounding_identity,
        legs,
        matched_quantity,
        gross_acquisition_cost,
        total_modeled_fees,
        total_modeled_acquisition_cost,
        conservative_net_lower_bound,
        modeled_settlement_value,
        settlement_value_basis,
        source_evidence_class,
        "NONE",
    )
    return content_digest(payload)


def _validate_decision_projection_linkage(
    *,
    decision: CounterfactualDecision,
    projection: SharedOpportunityProjection,
    expected_source_record_type: str,
    expected_source_record_id: str,
) -> None:
    if decision.venue is not SharedVenue.KALSHI or projection.venue is not SharedVenue.KALSHI:
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_VENUE_MISMATCH")
    if decision.subject_projection_id != projection.projection_id:
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_PROJECTION_IDENTITY_MISMATCH")
    if (
        decision.subject_source_record_type != projection.source_record_type
        or decision.subject_source_record_id != projection.source_record_id
    ):
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_DECISION_PROJECTION_SOURCE_MISMATCH")
    if decision.subject_source_record_type != expected_source_record_type:
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_SOURCE_TYPE_MISMATCH")
    if decision.subject_source_record_id != expected_source_record_id:
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_EVALUATION_IDENTITY_MISMATCH")


def build_kalshi_baseline_quote_from_binary_complement(
    *,
    decision: CounterfactualDecision,
    projection: SharedOpportunityProjection,
    evaluation: BinaryComplementEvaluation,
    fill_slices: tuple[ComplementFillSlice, ...],
    run_id: str,
    captured_at: datetime,
) -> KalshiBaselineQuoteEvidence:
    """Pure; performs no I/O and mutates nothing.  Raises
    ``KalshiBaselineQuoteError`` if the evaluation was never a genuine,
    positively-matched ``CANDIDATE`` -- callers must only invoke this for an
    evaluation already known to have matched depth; a ``NO_CANDIDATE``/
    ``INELIGIBLE``/``INVALID`` evaluation carries no acquirable price and
    correctly never produces a baseline-quote row at all (Phase 2:
    availability discipline -- absence of a row *is* the honest "no
    sufficient baseline evidence" fact)."""

    _validate_decision_projection_linkage(
        decision=decision,
        projection=projection,
        expected_source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        expected_source_record_id=evaluation.evaluation_id,
    )
    if evaluation.state is not ComplementState.CANDIDATE or evaluation.matched_quantity <= 0:
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_EVALUATION_NOT_CANDIDATE")
    if not fill_slices:
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_NO_FILL_SLICES")
    if any(item.evaluation_id != evaluation.evaluation_id for item in fill_slices):
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_FILL_SLICE_EVALUATION_MISMATCH")

    legs = tuple(
        KalshiBaselineQuoteLeg(
            ordinal=fill_slice.ordinal,
            leg_role=KalshiBaselineLegRole.SINGLE_MARKET_COMPLEMENT,
            market_ticker=evaluation.provider_market_ticker,
            purchased_side=fill_slice.purchased_side,
            source_level_id=fill_slice.source_level_id,
            quantity=fill_slice.quantity,
            acquisition_price=fill_slice.ask_price,
            acquisition_cost=fill_slice.acquisition_cost,
            modeled_fee=fill_slice.modeled_trade_fee,
            rounding_reserve=fill_slice.rounding_reserve,
        )
        for fill_slice in sorted(fill_slices, key=lambda item: item.ordinal)
    )
    quote_id = kalshi_baseline_quote_id(
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V1, decision_id=decision.decision_id
    )
    market_tickers = (evaluation.provider_market_ticker,)
    evaluation_observed_at = (evaluation.evaluated_at,)
    total_modeled_fees = evaluation.modeled_taker_fees + evaluation.conservative_rounding_reserve
    canonical_hash = kalshi_baseline_quote_canonical_hash(
        quote_id=quote_id,
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V1,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.KALSHI,
        subject_projection_id=projection.projection_id,
        subject_source_record_type=decision.subject_source_record_type,
        subject_source_record_id=decision.subject_source_record_id,
        subject_source_canonical_hash=projection.source_canonical_hash,
        instrument_id=projection.instrument_id,
        product_or_market_id=projection.product_or_market_id,
        market_tickers=market_tickers,
        decision_evaluated_at=decision.evaluated_at,
        evaluation_observed_at=evaluation_observed_at,
        evaluator_version=evaluation.evaluator_version,
        fee_model_identity=FEE_MODEL_IDENTITY,
        rounding_identity=evaluation.rounding_identity,
        legs=legs,
        matched_quantity=evaluation.matched_quantity,
        gross_acquisition_cost=evaluation.gross_acquisition_cost,
        total_modeled_fees=total_modeled_fees,
        total_modeled_acquisition_cost=evaluation.total_modeled_acquisition_cost,
        conservative_net_lower_bound=evaluation.conservative_net_lower_bound,
        modeled_settlement_value=evaluation.gross_settlement_value,
        settlement_value_basis=SettlementValueBasis.EXACT_COMPLEMENT_VALUE.value,
        source_evidence_class=projection.evidence_class,
    )
    return KalshiBaselineQuoteEvidence(
        quote_id=quote_id,
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V1,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.KALSHI,
        subject_projection_id=projection.projection_id,
        subject_source_record_type=decision.subject_source_record_type,
        subject_source_record_id=decision.subject_source_record_id,
        subject_source_canonical_hash=projection.source_canonical_hash,
        instrument_id=projection.instrument_id,
        product_or_market_id=projection.product_or_market_id,
        market_tickers=market_tickers,
        decision_evaluated_at=decision.evaluated_at,
        evaluation_observed_at=evaluation_observed_at,
        evaluator_version=evaluation.evaluator_version,
        fee_model_identity=FEE_MODEL_IDENTITY,
        rounding_identity=evaluation.rounding_identity,
        legs=legs,
        matched_quantity=evaluation.matched_quantity,
        gross_acquisition_cost=evaluation.gross_acquisition_cost,
        total_modeled_fees=total_modeled_fees,
        total_modeled_acquisition_cost=evaluation.total_modeled_acquisition_cost,
        conservative_net_lower_bound=evaluation.conservative_net_lower_bound,
        modeled_settlement_value=evaluation.gross_settlement_value,
        settlement_value_basis=SettlementValueBasis.EXACT_COMPLEMENT_VALUE.value,
        source_evidence_class=projection.evidence_class,
        captured_at=captured_at,
        run_id=run_id,
        canonical_hash=canonical_hash,
        authority="NONE",
    )


def build_kalshi_baseline_quote_from_threshold_bundle(
    *,
    decision: CounterfactualDecision,
    projection: SharedOpportunityProjection,
    evaluation: ThresholdBundleEvaluation,
    matched_slices: tuple[ThresholdMatchedSlice, ...],
    broader_market_ticker: str,
    narrower_market_ticker: str,
    run_id: str,
    captured_at: datetime,
) -> KalshiBaselineQuoteEvidence:
    """Pure; performs no I/O and mutates nothing.  Mirrors
    ``build_kalshi_baseline_quote_from_binary_complement`` exactly, except
    each already-matched ``ThresholdMatchedSlice`` expands into its two
    constituent legs (one fill on the broader-YES market, one on the
    narrower-NO market) since a threshold-ladder slice pairs one fill on
    each of two distinct markets."""

    _validate_decision_projection_linkage(
        decision=decision,
        projection=projection,
        expected_source_record_type=THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE,
        expected_source_record_id=evaluation.evaluation_id,
    )
    if (
        evaluation.state is not ThresholdEvaluationState.CANDIDATE
        or evaluation.matched_quantity <= 0
    ):
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_EVALUATION_NOT_CANDIDATE")
    if not matched_slices:
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_NO_FILL_SLICES")
    if any(item.evaluation_id != evaluation.evaluation_id for item in matched_slices):
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_FILL_SLICE_EVALUATION_MISMATCH")
    if not broader_market_ticker or not narrower_market_ticker:
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_MARKET_TICKER_MISSING")
    if evaluation.broader_observed_at is None or evaluation.narrower_observed_at is None:
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_ORDERBOOK_OBSERVATION_MISSING")

    legs: list[KalshiBaselineQuoteLeg] = []
    for matched_slice in sorted(matched_slices, key=lambda item: item.ordinal):
        legs.append(
            KalshiBaselineQuoteLeg(
                ordinal=len(legs),
                leg_role=KalshiBaselineLegRole.THRESHOLD_BROADER_YES,
                market_ticker=broader_market_ticker,
                purchased_side="YES",
                source_level_id=matched_slice.broader_yes_level_id,
                quantity=matched_slice.quantity,
                acquisition_price=matched_slice.broader_yes_ask,
                acquisition_cost=matched_slice.broader_cost,
                modeled_fee=matched_slice.broader_fee,
                rounding_reserve=matched_slice.broader_rounding_reserve,
            )
        )
        legs.append(
            KalshiBaselineQuoteLeg(
                ordinal=len(legs),
                leg_role=KalshiBaselineLegRole.THRESHOLD_NARROWER_NO,
                market_ticker=narrower_market_ticker,
                purchased_side="NO",
                source_level_id=matched_slice.narrower_no_level_id,
                quantity=matched_slice.quantity,
                acquisition_price=matched_slice.narrower_no_ask,
                acquisition_cost=matched_slice.narrower_cost,
                modeled_fee=matched_slice.narrower_fee,
                rounding_reserve=matched_slice.narrower_rounding_reserve,
            )
        )

    quote_id = kalshi_baseline_quote_id(
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V1, decision_id=decision.decision_id
    )
    market_tickers = (broader_market_ticker, narrower_market_ticker)
    evaluation_observed_at = (evaluation.broader_observed_at, evaluation.narrower_observed_at)
    total_modeled_fees = evaluation.total_modeled_fee + evaluation.conservative_rounding_reserve
    legs_tuple = tuple(legs)
    canonical_hash = kalshi_baseline_quote_canonical_hash(
        quote_id=quote_id,
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V1,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.KALSHI,
        subject_projection_id=projection.projection_id,
        subject_source_record_type=decision.subject_source_record_type,
        subject_source_record_id=decision.subject_source_record_id,
        subject_source_canonical_hash=projection.source_canonical_hash,
        instrument_id=projection.instrument_id,
        product_or_market_id=projection.product_or_market_id,
        market_tickers=market_tickers,
        decision_evaluated_at=decision.evaluated_at,
        evaluation_observed_at=evaluation_observed_at,
        evaluator_version=THRESHOLD_EVALUATOR_VERSION,
        fee_model_identity=FEE_MODEL_IDENTITY,
        rounding_identity=ROUNDING_IDENTITY,
        legs=legs_tuple,
        matched_quantity=evaluation.matched_quantity,
        gross_acquisition_cost=evaluation.gross_acquisition_cost,
        total_modeled_fees=total_modeled_fees,
        total_modeled_acquisition_cost=evaluation.total_modeled_acquisition_cost,
        conservative_net_lower_bound=evaluation.conservative_net_lower_bound,
        modeled_settlement_value=evaluation.gross_lower_bound_value,
        settlement_value_basis=SettlementValueBasis.CONSERVATIVE_FLOOR_VALUE.value,
        source_evidence_class=projection.evidence_class,
    )
    return KalshiBaselineQuoteEvidence(
        quote_id=quote_id,
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V1,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.KALSHI,
        subject_projection_id=projection.projection_id,
        subject_source_record_type=decision.subject_source_record_type,
        subject_source_record_id=decision.subject_source_record_id,
        subject_source_canonical_hash=projection.source_canonical_hash,
        instrument_id=projection.instrument_id,
        product_or_market_id=projection.product_or_market_id,
        market_tickers=market_tickers,
        decision_evaluated_at=decision.evaluated_at,
        evaluation_observed_at=evaluation_observed_at,
        evaluator_version=THRESHOLD_EVALUATOR_VERSION,
        fee_model_identity=FEE_MODEL_IDENTITY,
        rounding_identity=ROUNDING_IDENTITY,
        legs=legs_tuple,
        matched_quantity=evaluation.matched_quantity,
        gross_acquisition_cost=evaluation.gross_acquisition_cost,
        total_modeled_fees=total_modeled_fees,
        total_modeled_acquisition_cost=evaluation.total_modeled_acquisition_cost,
        conservative_net_lower_bound=evaluation.conservative_net_lower_bound,
        modeled_settlement_value=evaluation.gross_lower_bound_value,
        settlement_value_basis=SettlementValueBasis.CONSERVATIVE_FLOOR_VALUE.value,
        source_evidence_class=projection.evidence_class,
        captured_at=captured_at,
        run_id=run_id,
        canonical_hash=canonical_hash,
        authority="NONE",
    )


def reduce_kalshi_binary_complement_outcome(
    *, baseline: KalshiBaselineQuoteEvidence, observation: DecisionFollowUpObservation
) -> Decimal | None:
    """The one narrow, pure Phase-5 economic-reduction function this
    milestone ships.  Scoped to ``BinaryComplementEvaluation``-sourced
    baselines only -- see the module docstring's "Method isolation" note for
    exactly why threshold-ladder decisions stay unavailable.

    Returns a net return *ratio* (mirrors ``DecisionFollowUpObservation.
    subsequent_return``'s own ratio semantics for CRYPTO, so the two venues'
    values remain comparable in the same statistical units), or ``None`` when
    a genuine economic return cannot yet be defensibly computed. ``None`` and
    a computed value (including an exact ``0`` or a negative value) are
    never conflated: ``None`` means *unavailable* (no baseline, market has
    not genuinely settled, or a non-clean-binary result such as ``VOID`` or a
    multi-leg comma-joined string); a returned ``Decimal`` -- even a negative
    one -- means a genuine, defensible economic outcome was computed,
    including a genuine loss.

    Deterministic and side-effect-free: given the same ``baseline`` and
    ``observation`` content, always returns the identical value.

    Economic basis: a binary complement holds *both* the YES and NO side of
    one exact market.  Because YES and NO settle as exact complements of one
    another ($1.00 to the winning side, $0.00 to the losing side, for the
    identical market), a complement position's total settlement payout is
    *not* contingent on which way the market resolves -- it is exactly
    ``matched_quantity`` dollars (``baseline.modeled_settlement_value``,
    already retained verbatim from the source evaluation's own
    ``gross_settlement_value = matched_quantity * 1``) whenever the market
    resolves as a clean binary YES or NO.  This is the same arithmetic
    identity ``domain.public_complement.evaluate_binary_complement`` itself
    relies on -- never a probabilistic assumption, and never reused for a
    market that resolved any other way (``VOID``, unresolved, or an
    unstructured multi-leg result all fail closed to ``None`` below)."""

    if baseline.decision_id != observation.decision_id:
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_REDUCTION_DECISION_MISMATCH")
    if baseline.subject_source_record_type not in _REDUCIBLE_SOURCE_RECORD_TYPES:
        return None
    if baseline.settlement_value_basis != SettlementValueBasis.EXACT_COMPLEMENT_VALUE.value:
        # Defense in depth: every ``BinaryComplementEvaluation``-sourced
        # baseline this module's own builder ever produces already carries
        # ``EXACT_COMPLEMENT_VALUE`` (never the threshold-ladder floor
        # basis) -- restated here so this function's own correctness never
        # silently depends on that invariant holding elsewhere. A
        # conservative *floor* is not a safe stand-in for an *exact*
        # guaranteed payout in a return computation.
        return None
    if baseline.total_modeled_acquisition_cost <= 0:
        return None
    if observation.settlement_result not in ("YES", "NO"):
        # Covers: not yet settled (``None``), ``VOID``/exotic resolutions,
        # and the multi-leg comma-joined string a threshold-ladder/
        # multi-market follow-up read would produce -- fails closed rather
        # than guessing, exactly as required (Phase 2/8).
        return None
    payout = baseline.modeled_settlement_value
    net_return = (
        payout - baseline.total_modeled_acquisition_cost
    ) / baseline.total_modeled_acquisition_cost
    return net_return.quantize(_RETURN_QUANTUM, rounding=ROUND_HALF_EVEN)


# =============================================================================
# Kalshi Baseline Quote Capture & Economic Maturation v1
# =============================================================================
#
# Root cause this section closes (bounded archaeology, not assumption):
# ``kalshi_baseline_quote`` stayed at 0 rows in production not because
# capture was ever unwired -- ``SyncKalshiBaselineQuoteEvidence`` has run
# inside ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` every 5 minutes since v1
# shipped, with 100% referential success resolving every decision's
# ``subject_source_record_id`` back to its exact retained evaluation row --
# but because *every* ``binary_complement_evaluation``/
# ``threshold_bundle_evaluation`` row ever produced, system-wide, across
# 368 + 707 rows, has landed in ``NO_CANDIDATE``/``INELIGIBLE`` state, never
# once ``CANDIDATE``. That is a genuine, structural fact about Kalshi's own
# market-making (a resting YES ask plus a resting NO ask essentially always
# sums to at least $1 -- that is what keeps the market maker solvent), not a
# code defect: ``build_kalshi_baseline_quote_from_binary_complement``/
# ``..._from_threshold_bundle`` above correctly, honestly decline to
# manufacture matched-trade economics for a trade that never had a matching
# depth. The classification is "fail-closed rule too broad relative to what
# a *baseline quote* is actually supposed to mean" -- V1 above conflates two
# genuinely different things under one name:
#
# 1. **Matched-trade execution economics** (what V1 actually models): the
#    acquisition cost/fees/guaranteed settlement value of a complement or
#    threshold-ladder position Apex could have *executed* -- meaningful only
#    when a genuine ``CANDIDATE`` match existed.
# 2. **The plain market quote observed at decision time** (what this
#    section adds): the YES/NO bid the provider's own resting orderbook
#    showed when Apex evaluated the market -- meaningful for *every*
#    evaluation, matched or not, and exactly the "analogue of CRYPTO's
#    ``reference_price_at_baseline``/``ORIGIN_EVIDENCE``"
#    ``domain.counterfactual_outcome``'s own module docstring names as the
#    explicit prerequisite this module was always supposed to close (see
#    that docstring's "Kalshi (Phase 8)" note -- written when no historical
#    Kalshi quote archive existed; ``adapters.postgres``'s
#    ``market_orderbook_snapshot``/``market_orderbook_level`` tables, added
#    since, now retain exactly that archive, confirmed by direct inspection:
#    100% of retained ``binary_complement_evaluation``/
#    ``threshold_bundle_evaluation`` rows resolve their own
#    ``orderbook_snapshot_id`` pointer(s) to a genuinely retained snapshot).
#
# ``KalshiMarketQuoteBaselineEvidence`` (``KALSHI_BASELINE_QUOTE_POLICY_V2``,
# ``quote_kind = MARKET_QUOTE_OBSERVATION``) is that second, additive
# concept. It never replaces or reinterprets V1 -- a decision's referenced
# evaluation is either ``CANDIDATE`` (V1 applies) or it is not (this section
# applies instead); the two are structurally mutually exclusive per decision
# (one immutable evaluation row, one fixed ``state``), which is also why
# both kinds safely share one ``decision_id``-unique table: no decision can
# ever earn both. Sourced entirely from evidence Apex already paid for and
# already retained (the exact ``market_orderbook_snapshot``/
# ``market_orderbook_level`` rows the already-immutable evaluation already
# points to) -- zero new provider requests, zero new investment judgment.
# Point-in-time safety is structural, not a fresh "as of T" query that could
# race: every leg's quote is read from the *one* snapshot id the *one* fixed
# evaluation the decision already references names, never "the current/
# latest" snapshot for that market -- the identical discipline V1's own
# module docstring already established, extended rather than re-invented.
#
# What this section deliberately does NOT do: it does not compute an
# economic "return" for a ``MARKET_QUOTE_OBSERVATION`` baseline. No capital
# was ever modeled as deployed against a plain quote, so there is no trade
# to reduce to a return -- inventing one would be exactly the "convert 'we
# now know the outcome' into 'the decision was good'" conflation this
# milestone was explicitly told never to make. These rows exist as honest,
# retained, point-in-time evidence (closing the "Apex cannot even show what
# the market said" gap) and to unblock operator observability; they
# intentionally do not feed ``reduce_kalshi_binary_complement_outcome`` or
# ``COUNTERFACTUAL_OUTCOME_POLICY_V2`` -- see
# ``adapters.counterfactual_outcome_sources`` for the read-side filter that
# keeps it that way.

# Precision for a derived probability estimate in [0, 1] -- identical
# quantum to ``_RETURN_QUANTUM`` above, named separately because the two
# quantities (a return ratio vs. a probability) are never the same kind of
# number even though this module happens to want the same precision for
# both.
_PROBABILITY_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class KalshiOrderbookTopOfBook:
    """Not itself retained evidence, never persisted, never hashed -- a
    read-only, ephemeral carrier for the top-of-book bid on each side of one
    already-retained, immutable ``market_orderbook_snapshot`` row, batched by
    ``application.kalshi_baseline_quote.KalshiBaselineQuoteEvidenceSource.
    orderbook_top_of_book`` for the builders below. ``yes_bid``/``no_bid``
    are ``None`` exactly when that side's own bid-level count on the
    snapshot is zero (a genuinely one-sided book) -- never a sentinel, never
    inferred, never defaulted."""

    orderbook_snapshot_id: str
    market_ticker: str
    observed_at: datetime
    data_quality_state: str
    yes_bid: Decimal | None
    no_bid: Decimal | None

    def __post_init__(self) -> None:
        if not self.orderbook_snapshot_id or not self.market_ticker:
            raise ValueValidationError("orderbook top-of-book identities must not be empty")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        if self.yes_bid is not None:
            object.__setattr__(self, "yes_bid", _canonical_decimal(self.yes_bid))
            if not Decimal(0) <= self.yes_bid <= _ONE:
                raise ValueValidationError("orderbook top-of-book yes bid must be in [0, 1]")
        if self.no_bid is not None:
            object.__setattr__(self, "no_bid", _canonical_decimal(self.no_bid))
            if not Decimal(0) <= self.no_bid <= _ONE:
                raise ValueValidationError("orderbook top-of-book no bid must be in [0, 1]")

    @property
    def is_two_sided(self) -> bool:
        return (
            self.yes_bid is not None
            and self.no_bid is not None
            and self.data_quality_state == "VALID"
        )


@dataclass(frozen=True, slots=True)
class KalshiMarketQuoteLeg:
    """One market's full, plain, two-sided quote as genuinely observed at
    decision time -- never a matched-trade fill, never a live/current quote.
    ``yes_ask``/``no_ask`` are always the exact reciprocal of the *other*
    side's own bid (``PublicOrderbookLevel``'s own documented convention:
    "its reciprocal ask is derived, never supplied") -- recomputed and
    checked here rather than trusted from any caller, exactly like
    ``KalshiBaselineQuoteLeg.acquisition_cost`` above recomputes and checks
    its own caller-supplied value."""

    ordinal: int
    leg_role: KalshiQuoteLegRole
    market_ticker: str
    orderbook_snapshot_id: str
    quote_observed_at: datetime
    yes_bid: Decimal
    no_bid: Decimal
    yes_ask: Decimal
    no_ask: Decimal
    implied_yes_probability: Decimal

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueValidationError("market quote leg ordinal must not be negative")
        if not self.market_ticker or not self.orderbook_snapshot_id:
            raise ValueValidationError("market quote leg identities must not be empty")
        object.__setattr__(
            self, "quote_observed_at", _aware(self.quote_observed_at, "quote_observed_at")
        )
        for name in ("yes_bid", "no_bid", "yes_ask", "no_ask", "implied_yes_probability"):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))
        for name in ("yes_bid", "no_bid", "yes_ask", "no_ask", "implied_yes_probability"):
            value = getattr(self, name)
            if not Decimal(0) <= value <= _ONE:
                raise ValueValidationError(f"market quote leg {name} must be in [0, 1]")
        if self.yes_ask != _ONE - self.no_bid:
            raise ValueValidationError(
                "market quote leg yes_ask must be the exact reciprocal of no_bid"
            )
        if self.no_ask != _ONE - self.yes_bid:
            raise ValueValidationError(
                "market quote leg no_ask must be the exact reciprocal of yes_bid"
            )
        expected_probability = ((self.yes_bid + self.yes_ask) / 2).quantize(
            _PROBABILITY_QUANTUM, rounding=ROUND_HALF_EVEN
        )
        if self.implied_yes_probability != expected_probability:
            raise ValueValidationError(
                "market quote leg implied_yes_probability must be the exact "
                "midpoint of yes_bid and yes_ask"
            )

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class KalshiMarketQuoteBaselineEvidence:
    """One immutable, append-only, decision-linked record of the plain
    Kalshi market quote genuinely observable as of the exact retained
    evaluation a ``CounterfactualDecision`` already references -- never a
    matched-trade fill (see ``KalshiBaselineQuoteEvidence`` above for that),
    never later settlement or a live/current quote. Authority is always
    ``NONE``. Captured only when ``KalshiBaselineQuoteEvidence`` (V1) does
    not apply (the referenced evaluation was not a genuine ``CANDIDATE``
    match) -- see this section's own module-level comment block for the
    full rationale."""

    quote_id: str
    policy_version: str
    decision_id: str
    decision_canonical_hash: str
    venue: SharedVenue
    subject_projection_id: str
    subject_source_record_type: str
    subject_source_record_id: str
    subject_source_canonical_hash: str
    instrument_id: str
    product_or_market_id: str
    decision_evaluated_at: datetime
    legs: tuple[KalshiMarketQuoteLeg, ...]
    captured_at: datetime
    evaluator_version: str
    source_evidence_class: str
    run_id: str
    canonical_hash: str
    quote_kind: str = KalshiQuoteKind.MARKET_QUOTE_OBSERVATION.value
    authority: str = "NONE"

    def __post_init__(self) -> None:
        if self.venue is not SharedVenue.KALSHI:
            raise ValueValidationError("market quote baseline evidence is Kalshi-only")
        for name in (
            "quote_id",
            "policy_version",
            "decision_id",
            "decision_canonical_hash",
            "subject_projection_id",
            "subject_source_record_id",
            "subject_source_canonical_hash",
            "instrument_id",
            "product_or_market_id",
            "evaluator_version",
            "source_evidence_class",
            "run_id",
            "canonical_hash",
        ):
            if not getattr(self, name):
                raise ValueValidationError(f"market quote baseline {name} must not be empty")
        if self.subject_source_record_type not in SUPPORTED_BASELINE_QUOTE_SOURCE_RECORD_TYPES:
            raise ValueValidationError("market quote baseline source record type is not supported")
        if self.quote_kind != KalshiQuoteKind.MARKET_QUOTE_OBSERVATION.value:
            raise ValueValidationError("market quote baseline quote_kind is invalid")
        if self.authority != "NONE":
            raise ValueValidationError("market quote baseline evidence authority must be NONE")
        object.__setattr__(
            self,
            "decision_evaluated_at",
            _aware(self.decision_evaluated_at, "decision_evaluated_at"),
        )
        object.__setattr__(self, "captured_at", _aware(self.captured_at, "captured_at"))
        if not self.legs:
            raise ValueValidationError("market quote baseline must retain at least one leg")
        ordinals = [leg.ordinal for leg in self.legs]
        if ordinals != sorted(ordinals) or len(set(ordinals)) != len(ordinals):
            raise ValueValidationError(
                "market quote baseline legs must have strictly increasing ordinals"
            )
        # Temporal correctness (hard requirement) -- Kalshi Baseline Temporal
        # Semantics & Sweep Isolation v1: an earlier revision of this check
        # compared ``leg.quote_observed_at`` against ``self.decision_
        # evaluated_at``. That comparison was wrong, not merely strict,
        # because ``decision_evaluated_at`` is never the real instant a
        # decision was made -- it is copied verbatim from
        # ``EligibilityDecision``/``MandateEvaluation.evaluated_at``, which
        # ``application.shared_opportunity.build_opportunity_set`` sets to
        # one ``as_of`` shared by *every* decision in that rebuild, and
        # ``apex.cli``'s ``operating-cycle-worker`` computes that ``as_of``
        # as ``datetime.now(UTC).replace(hour=0, minute=0, second=0,
        # microsecond=0)`` -- the current UTC day's exact midnight, frozen
        # for the whole day so repeated polls/restarts converge on one
        # idempotent daily cycle rather than duplicating tasks. It is a
        # scheduler/rebuild bucket boundary, not a capture timestamp. Real
        # evidence -- the orderbook response this leg's quote is drawn from
        # -- is timestamped with the genuine wall clock at the moment the
        # provider response was received (``evaluate_binary_complement``'s
        # own ``evaluated_at=orderbook_response.received_at``), which is
        # always at or after the day's midnight and routinely a few minutes
        # after it once real capture work has run. Comparing a real capture
        # instant against a fixed midnight floor therefore rejected the
        # ordinary case, not an edge case: any evaluation captured after
        # 00:00:00 UTC on the same day its decision was rebuilt -- nearly
        # every one -- tripped this check.
        #
        # The genuine proof that a leg's quote belongs to this decision was
        # never a timestamp at all -- it is the immutable identity chain
        # already enforced before this object is ever constructed:
        # ``_validate_decision_projection_linkage`` ties
        # ``decision.subject_source_record_id`` to the exact evaluation
        # (``evaluation.evaluation_id``) that produced it, and
        # ``build_kalshi_market_quote_baseline_from_binary_complement``/
        # ``..._from_threshold_bundle`` each additionally require
        # ``snapshot.orderbook_snapshot_id == evaluation.orderbook_
        # snapshot_id`` (``KALSHI_MARKET_QUOTE_BASELINE_SNAPSHOT_IDENTITY_
        # MISMATCH`` otherwise). Both ids are deterministic/content-addressed
        # and immutable, so that chain proves this exact leg is the exact
        # evidence that produced this exact decision far more precisely than
        # any wall-clock range could -- a coincidentally-timed but unrelated
        # snapshot could never share that evaluation's own id, no matter
        # what timestamp it carries. Nothing here weakens that chain; this
        # module's identity checks are unchanged.
        #
        # What a wall-clock check still legitimately catches, independent of
        # that identity chain: a leg claiming to have been observed *after*
        # the genuinely real "now" at which Apex is deriving this very row
        # (``captured_at`` -- always the sweep's real ``executed_at`` wall
        # clock, per ``operating_cycle_tasks.py``'s
        # ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` branch and
        # ``SyncKalshiBaselineQuoteEvidence.execute``'s own ``generated_at``
        # parameter -- never the coarse midnight floor). Causality makes
        # this bound unconditionally true for any genuine row (evidence must
        # already have been captured before Apex can read and re-project
        # it), so it can never misfire on legitimate same-day evidence the
        # way the old comparison did, while still failing loudly on
        # corrupted or clock-skewed input claiming an impossible future
        # observation relative to its own capture.
        for leg in self.legs:
            if leg.quote_observed_at > self.captured_at:
                raise ValueValidationError(
                    "market quote baseline leg cannot be observed after its own capture"
                )


def kalshi_market_quote_baseline_canonical_hash(
    *,
    quote_id: str,
    policy_version: str,
    decision_id: str,
    decision_canonical_hash: str,
    venue: SharedVenue | str,
    subject_projection_id: str,
    subject_source_record_type: str,
    subject_source_record_id: str,
    subject_source_canonical_hash: str,
    instrument_id: str,
    product_or_market_id: str,
    decision_evaluated_at: datetime,
    legs: tuple[KalshiMarketQuoteLeg, ...],
    evaluator_version: str,
    source_evidence_class: str,
) -> str:
    """The ``KalshiMarketQuoteBaselineEvidence.canonical_hash`` formula.
    Deliberately excludes ``captured_at``/``run_id`` for the identical
    crash-resume-replay reason ``kalshi_baseline_quote_canonical_hash``
    above does -- a genuine replay under a different wall-clock instant or
    ``run_id`` must never manufacture a spurious identity conflict."""

    payload = (
        KALSHI_BASELINE_QUOTE_POLICY_V2,
        quote_id,
        policy_version,
        decision_id,
        decision_canonical_hash,
        venue,
        subject_projection_id,
        subject_source_record_type,
        subject_source_record_id,
        subject_source_canonical_hash,
        instrument_id,
        product_or_market_id,
        decision_evaluated_at,
        legs,
        evaluator_version,
        source_evidence_class,
        KalshiQuoteKind.MARKET_QUOTE_OBSERVATION.value,
        "NONE",
    )
    return content_digest(payload)


def _market_quote_leg(
    *, ordinal: int, leg_role: KalshiQuoteLegRole, snapshot: KalshiOrderbookTopOfBook
) -> KalshiMarketQuoteLeg:
    if snapshot.yes_bid is None or snapshot.no_bid is None:
        raise KalshiBaselineQuoteError("KALSHI_MARKET_QUOTE_BASELINE_ONE_SIDED_ORDERBOOK")
    yes_bid = snapshot.yes_bid
    no_bid = snapshot.no_bid
    yes_ask = _ONE - no_bid
    no_ask = _ONE - yes_bid
    implied = ((yes_bid + yes_ask) / 2).quantize(_PROBABILITY_QUANTUM, rounding=ROUND_HALF_EVEN)
    return KalshiMarketQuoteLeg(
        ordinal=ordinal,
        leg_role=leg_role,
        market_ticker=snapshot.market_ticker,
        orderbook_snapshot_id=snapshot.orderbook_snapshot_id,
        quote_observed_at=snapshot.observed_at,
        yes_bid=yes_bid,
        no_bid=no_bid,
        yes_ask=yes_ask,
        no_ask=no_ask,
        implied_yes_probability=implied,
    )


def build_kalshi_market_quote_baseline_from_binary_complement(
    *,
    decision: CounterfactualDecision,
    projection: SharedOpportunityProjection,
    evaluation: BinaryComplementEvaluation,
    snapshot: KalshiOrderbookTopOfBook,
    run_id: str,
    captured_at: datetime,
) -> KalshiMarketQuoteBaselineEvidence:
    """Pure; performs no I/O and mutates nothing. Raises
    ``KalshiBaselineQuoteError`` if ``evaluation`` is a genuine ``CANDIDATE``
    match (that case belongs to
    ``build_kalshi_baseline_quote_from_binary_complement`` above, never
    here -- the two paths are mutually exclusive by construction, restated
    here defensively) or if ``snapshot`` is not genuinely two-sided (fail
    closed: a one-sided book is never silently treated as a valid baseline;
    callers are expected to have already checked ``snapshot.is_two_sided``
    and counted the insufficient-evidence case themselves, but this function
    never trusts that discipline blindly)."""

    _validate_decision_projection_linkage(
        decision=decision,
        projection=projection,
        expected_source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        expected_source_record_id=evaluation.evaluation_id,
    )
    if evaluation.state is ComplementState.CANDIDATE:
        raise KalshiBaselineQuoteError("KALSHI_MARKET_QUOTE_BASELINE_EVALUATION_WAS_CANDIDATE")
    if snapshot.orderbook_snapshot_id != evaluation.orderbook_snapshot_id:
        raise KalshiBaselineQuoteError("KALSHI_MARKET_QUOTE_BASELINE_SNAPSHOT_IDENTITY_MISMATCH")
    if not snapshot.is_two_sided:
        raise KalshiBaselineQuoteError("KALSHI_MARKET_QUOTE_BASELINE_ONE_SIDED_ORDERBOOK")

    legs = (
        _market_quote_leg(ordinal=0, leg_role=KalshiQuoteLegRole.SINGLE_MARKET, snapshot=snapshot),
    )
    quote_id = kalshi_baseline_quote_id(
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V2, decision_id=decision.decision_id
    )
    canonical_hash = kalshi_market_quote_baseline_canonical_hash(
        quote_id=quote_id,
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V2,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.KALSHI,
        subject_projection_id=projection.projection_id,
        subject_source_record_type=decision.subject_source_record_type,
        subject_source_record_id=decision.subject_source_record_id,
        subject_source_canonical_hash=projection.source_canonical_hash,
        instrument_id=projection.instrument_id,
        product_or_market_id=projection.product_or_market_id,
        decision_evaluated_at=decision.evaluated_at,
        legs=legs,
        evaluator_version=evaluation.evaluator_version,
        source_evidence_class=projection.evidence_class,
    )
    return KalshiMarketQuoteBaselineEvidence(
        quote_id=quote_id,
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V2,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.KALSHI,
        subject_projection_id=projection.projection_id,
        subject_source_record_type=decision.subject_source_record_type,
        subject_source_record_id=decision.subject_source_record_id,
        subject_source_canonical_hash=projection.source_canonical_hash,
        instrument_id=projection.instrument_id,
        product_or_market_id=projection.product_or_market_id,
        decision_evaluated_at=decision.evaluated_at,
        legs=legs,
        captured_at=captured_at,
        evaluator_version=evaluation.evaluator_version,
        source_evidence_class=projection.evidence_class,
        run_id=run_id,
        canonical_hash=canonical_hash,
    )


def build_kalshi_market_quote_baseline_from_threshold_bundle(
    *,
    decision: CounterfactualDecision,
    projection: SharedOpportunityProjection,
    evaluation: ThresholdBundleEvaluation,
    broader_snapshot: KalshiOrderbookTopOfBook,
    narrower_snapshot: KalshiOrderbookTopOfBook,
    run_id: str,
    captured_at: datetime,
) -> KalshiMarketQuoteBaselineEvidence:
    """Mirrors ``build_kalshi_market_quote_baseline_from_binary_complement``
    exactly, except it retains the full two-sided quote of *both* markets in
    the threshold pair -- not just the broader-YES/narrower-NO sides a
    matched trade would have bought. Both legs must independently be
    genuinely two-sided; a threshold pair with only one leg's book available
    is not a usable baseline (fails closed, never a partial one)."""

    _validate_decision_projection_linkage(
        decision=decision,
        projection=projection,
        expected_source_record_type=THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE,
        expected_source_record_id=evaluation.evaluation_id,
    )
    if evaluation.state is ThresholdEvaluationState.CANDIDATE:
        raise KalshiBaselineQuoteError("KALSHI_MARKET_QUOTE_BASELINE_EVALUATION_WAS_CANDIDATE")
    if (
        broader_snapshot.orderbook_snapshot_id != evaluation.broader_orderbook_snapshot_id
        or narrower_snapshot.orderbook_snapshot_id != evaluation.narrower_orderbook_snapshot_id
    ):
        raise KalshiBaselineQuoteError("KALSHI_MARKET_QUOTE_BASELINE_SNAPSHOT_IDENTITY_MISMATCH")
    if not broader_snapshot.is_two_sided or not narrower_snapshot.is_two_sided:
        raise KalshiBaselineQuoteError("KALSHI_MARKET_QUOTE_BASELINE_ONE_SIDED_ORDERBOOK")

    legs = (
        _market_quote_leg(
            ordinal=0, leg_role=KalshiQuoteLegRole.THRESHOLD_BROADER, snapshot=broader_snapshot
        ),
        _market_quote_leg(
            ordinal=1, leg_role=KalshiQuoteLegRole.THRESHOLD_NARROWER, snapshot=narrower_snapshot
        ),
    )
    quote_id = kalshi_baseline_quote_id(
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V2, decision_id=decision.decision_id
    )
    canonical_hash = kalshi_market_quote_baseline_canonical_hash(
        quote_id=quote_id,
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V2,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.KALSHI,
        subject_projection_id=projection.projection_id,
        subject_source_record_type=decision.subject_source_record_type,
        subject_source_record_id=decision.subject_source_record_id,
        subject_source_canonical_hash=projection.source_canonical_hash,
        instrument_id=projection.instrument_id,
        product_or_market_id=projection.product_or_market_id,
        decision_evaluated_at=decision.evaluated_at,
        legs=legs,
        evaluator_version=THRESHOLD_EVALUATOR_VERSION,
        source_evidence_class=projection.evidence_class,
    )
    return KalshiMarketQuoteBaselineEvidence(
        quote_id=quote_id,
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V2,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.KALSHI,
        subject_projection_id=projection.projection_id,
        subject_source_record_type=decision.subject_source_record_type,
        subject_source_record_id=decision.subject_source_record_id,
        subject_source_canonical_hash=projection.source_canonical_hash,
        instrument_id=projection.instrument_id,
        product_or_market_id=projection.product_or_market_id,
        decision_evaluated_at=decision.evaluated_at,
        legs=legs,
        captured_at=captured_at,
        evaluator_version=THRESHOLD_EVALUATOR_VERSION,
        source_evidence_class=projection.evidence_class,
        run_id=run_id,
        canonical_hash=canonical_hash,
    )
