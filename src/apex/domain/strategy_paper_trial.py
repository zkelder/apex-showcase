"""Pure glue turning a genuine StrategyDefinition + StrategyEvaluationResult
into the exact input shapes Apex's existing Coinbase-Compatible Crypto Spot
PAPER Adapter (``domain.crypto_paper``) already consumes.

This module adds ZERO new PAPER execution logic. Every economically
meaningful decision (fee/spread/slippage modeling, order state machine,
balanced ledger postings, eligibility gates) is the existing,
already-reviewed ``domain.crypto_paper`` machinery, called unmodified.
What is new here is narrowly the honest translation step: a strategy's own
real signal/evaluation evidence -> a ``SharedOpportunityProjection`` (the
one typed shape every existing PAPER/eligibility function already expects)
-- and, symmetrically, a completed trial -> an ``AttributionSourceSnapshot``
(the shape ``performance_attribution.materialize_attribution`` already
expects), so the existing performance/health machinery can consume a new
strategy's evidence without any changes to it either.

``build_strategy_opportunity_projection`` fails closed (raises) whenever
the supplied evaluation lacks a real, computed ``net_return`` or has zero
sample size -- it can never construct a projection from absent or invented
evidence. It does not require ``MINIMUM_PROMOTION_SAMPLE_SIZE``: producing
one genuine Opportunity from a RESEARCH_ONLY strategy is legitimate (every
existing internal Apex method does exactly this long before any formal
promotion); ``strategy_evaluation.promote_to_paper_eligible`` remains the
separate, stricter gate for the strategy's own lifecycle state.

The product/contract reference data here (``reference_crypto_universe_
snapshot`` / ``reference_crypto_provider_contract``) is deliberately
labeled ``EvidenceClass.FIXTURE`` -- it supplies well-known, stable
Coinbase spot-market metadata (tick sizes, trading status) through the
existing, unmodified ``PublicCryptoProduct.create`` / ``build_provider_
contract`` constructors, rather than re-deriving it from a fresh discovery
capture. Genuine product/universe discovery is ``CRYPTO_UNIVERSE_REFRESH``'s
own, already-exercised concern -- reproducing it is out of this milestone's
narrow scope. The actual DECISION-TIME PRICE evidence used for preview and
fill is never a fixture. The preferred source of that evidence is the
canonical Live Market Observation substrate (``domain.live_market_
observation``, ``application.live_market_observation.
ObtainContemporaneousMarketObservation``) via ``application.strategy_paper_
trial.obtain_decision_price_evidence`` -- see ``price_evidence_from_live_
observation`` below, which retains the substrate's durable
``observation_id`` (embedded in ``evidence_id``, since ``RetainedPaperPrice
Evidence`` has no dedicated lineage field of its own) rather than opening a
second, ungoverned quote-evidence path. ``application.strategy_paper_trial.
capture_genuine_price_evidence`` remains as the bounded fallback that path
uses when the substrate reports STALE/ABSENT -- a real, credential-free
public capture in its own right, never a fixture, but one that carries no
durable observation lineage and must never be reached for silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from apex.domain.crypto_paper import (
    CryptoPaperCancellation,
    CryptoPaperEligibility,
    CryptoPaperFillApplication,
    CryptoPaperOrder,
    CryptoPaperOrderIntent,
    CryptoPaperPreview,
    CryptoPaperProviderContract,
    CryptoPaperReconciliation,
    PaperPricingSource,
    PaperSide,
    RetainedPaperPriceEvidence,
    build_provider_contract,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.crypto_universe import (
    CryptoDailyObservation,
    CryptoUniverseSnapshot,
    PublicCryptoProduct,
)
from apex.domain.live_market_observation import LiveMarketObservation
from apex.domain.performance_attribution import (
    AttributionSourceSnapshot,
    PaperEvidenceClass,
    PaperVenue,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedOpportunityProjection
from apex.domain.strategy_definition import StrategyDefinition, StrategySourceKind
from apex.domain.strategy_evaluation import StrategyEvaluationResult
from apex.domain.values import content_digest, deterministic_id

STRATEGY_PAPER_TRIAL_WARNING = "STRATEGY PAPER TRIAL -- SIMULATED CAPITAL ONLY, NO LIVE AUTHORITY"

# Conservative, fixed research-scale notional -- never scaled by confidence
# or "expected edge." Matches crypto_paper.py's own MAX_ORDER_NOTIONAL cap
# (an authorization for more than this is clamped there regardless), and
# treats maximum_loss the same way Apex's own crypto research doctrine
# already does elsewhere ("spot downside assumes price can fall to zero" --
# see AGENTS.md's Genuine Opportunity Evidence Completion v1): the entire
# notional is the worst case, not a smaller modeled figure.
STRATEGY_TRIAL_NOTIONAL = Decimal("10")

# A trial's SharedOpportunityProjection stays valid for this long -- long
# enough to survive one crypto_paper.py preview/submit/fill round trip
# (whose own order validity window, CRYPTO_ORDER_VALID_FOR, is 4 hours),
# short enough to fail closed quickly if evidence goes stale.
STRATEGY_TRIAL_VALIDITY_SECONDS = 6 * 60 * 60
STRATEGY_TRIAL_FRESHNESS_LIMIT_SECONDS = 30 * 60

# USD cents -- matches the reference contract's own price_increment
# (reference_crypto_provider_contract, below). Rounding to 8 places
# (domain.values's own MAX_SCALE) would be wrong here: Decimal
# multiplication scale is additive (spread = price * spread_rate), so an
# 8-place price times a 4-place rate already overflows MAX_SCALE downstream
# in domain.crypto_paper.build_paper_preview -- cent precision is what real
# Coinbase USD-quoted prices actually carry, and it is what keeps every
# derived fee/spread/slippage computation safely under MAX_SCALE. Shared by
# every decision-time price source this module produces (both the direct
# fallback capture in application.strategy_paper_trial and the
# live-observation-derived path below) so the two never drift apart.
PAPER_TRIAL_PRICE_SCALE = Decimal("0.01")


class StrategyPaperTrialError(ValueError):
    """A strategy-driven PAPER trial input violated an evidence invariant."""


def reference_crypto_universe_snapshot(
    *, instrument_id: str, created_at: datetime
) -> CryptoUniverseSnapshot:
    """A minimal, single-instrument universe snapshot scoped to one trial --
    not a full CRYPTO_UNIVERSE_REFRESH pass (see module docstring)."""

    payload = (instrument_id, created_at)
    snapshot_id = deterministic_id("strategy_paper_trial_universe", payload)
    return CryptoUniverseSnapshot(
        snapshot_id=snapshot_id,
        source_capture_id=snapshot_id,
        policy_id="STRATEGY_PAPER_TRIAL_REFERENCE_UNIVERSE_V1",
        policy_version=1,
        considered_product_ids=(instrument_id,),
        decisions=(),
        eligible_product_ids=(instrument_id,),
        excluded_product_ids=(),
        evidence_class=EvidenceClass.FIXTURE,
        evidence_limitations=(
            "single-instrument reference snapshot for one strategy trial, "
            "not a genuine CRYPTO_UNIVERSE_REFRESH pass",
        ),
        manifest_hash=content_digest(payload),
        created_at=created_at,
        canonical_hash=content_digest(payload),
    )


def reference_crypto_provider_contract(
    *, instrument_id: str, base_asset: str, created_at: datetime
) -> CryptoPaperProviderContract:
    """Real, stable, publicly-documented Coinbase BTC-USD-style spot tick
    sizes, through the existing unmodified ``PublicCryptoProduct.create`` /
    ``build_provider_contract`` constructors. See module docstring for why
    this is labeled FIXTURE rather than a fresh discovery capture."""

    product = PublicCryptoProduct.create(
        product_id=instrument_id,
        provider="COINBASE",
        base_asset=base_asset,
        quote_asset="USD",
        market_type="SPOT",
        trading_status="ACTIVE",
        product_status="ONLINE",
        price_increment="0.01",
        quantity_increment="0.00000001",
        minimum_quantity="0.00000001",
        minimum_notional="1",
        public_volume_24h=None,
        captured_at=created_at,
        source_contract_version="STRATEGY_PAPER_TRIAL_REFERENCE_V1",
        raw_evidence_hash="0" * 64,
        parser_version="STRATEGY_PAPER_TRIAL_REFERENCE_V1",
        evidence_class=EvidenceClass.FIXTURE,
    )
    return build_provider_contract(
        contract_id=deterministic_id("strategy_paper_trial_contract", (instrument_id, created_at)),
        product=product,
        source_capture_id=product.product_id,
    )


def price_evidence_from_live_observation(
    observation: LiveMarketObservation, *, product_id: str
) -> RetainedPaperPriceEvidence:
    """Convert one durable, canonical ``LiveMarketObservation`` (Live Market
    Observation Foundation v1, ``domain.live_market_observation``) into the
    ``RetainedPaperPriceEvidence`` shape ``domain.crypto_paper`` already
    consumes. This is the preferred source of decision-time price evidence
    whenever the substrate holds a SATISFIED contemporaneous observation
    (see ``application.strategy_paper_trial.obtain_decision_price_
    evidence``) -- so a strategy-driven PAPER decision retains the exact
    durable ``observation_id`` that supported it, rather than a fresh,
    unanchored provider capture. ``RetainedPaperPriceEvidence`` has no
    dedicated lineage field of its own (an existing, unmodified production
    type; see module docstring), so the id is embedded in ``evidence_id``
    instead -- exactly the "opaque evidence-anchor id" doctrine ``domain.
    live_market_observation``'s own module docstring already commits to (a
    consumer references a retained observation only by its id string, never
    by importing that module's types into the decision path this evidence
    eventually feeds)."""

    if observation.instrument_id != product_id:
        raise StrategyPaperTrialError(
            "PAPER_DECISION_EVIDENCE_INSTRUMENT_MISMATCH: observation "
            f"{observation.observation_id} is for {observation.instrument_id}, not {product_id}"
        )
    if observation.bid_price is None or observation.ask_price is None:
        raise StrategyPaperTrialError(
            "PAPER_DECISION_EVIDENCE_MISSING_TOP_OF_BOOK: live observation "
            f"{observation.observation_id} has no retained bid/ask"
        )
    price = ((observation.bid_price + observation.ask_price) / 2).quantize(PAPER_TRIAL_PRICE_SCALE)
    return RetainedPaperPriceEvidence.create(
        evidence_id=f"live-observation-{observation.observation_id}-price-evidence",
        product_id=product_id,
        observed_at=observation.captured_at,
        ingested_at=observation.captured_at,
        price=price,
        available_base_quantity=observation.book_depth_ask,
        source=PaperPricingSource.GENUINE_TOP_OF_BOOK,
        evidence_class="GENUINE_PUBLIC",
    )


def build_strategy_opportunity_projection(
    *,
    definition: StrategyDefinition,
    evaluation: StrategyEvaluationResult,
    reference_price: Decimal,
    generated_at: datetime,
    opportunity_ordinal: str,
) -> SharedOpportunityProjection:
    """Honestly translate one strategy's own real evaluation evidence into
    the shape every existing eligibility/PAPER function already expects.
    Fails closed -- never fabricates a projection from absent evidence."""

    if evaluation.strategy_id != definition.strategy_id:
        raise StrategyPaperTrialError("evaluation does not belong to this strategy definition")
    if evaluation.net_return is None:
        raise StrategyPaperTrialError(
            "cannot build an Opportunity projection without a real net_return"
        )
    if evaluation.sample_size <= 0:
        raise StrategyPaperTrialError(
            "cannot build an Opportunity projection from a zero-sample evaluation"
        )
    if reference_price <= 0:
        raise StrategyPaperTrialError("reference_price must be positive")

    instrument_id = definition.instrument_ids[0]
    projection_id = deterministic_id(
        "strategy_paper_trial_projection",
        (definition.strategy_id, definition.version, opportunity_ordinal, generated_at),
    )
    valid_until = generated_at + timedelta(seconds=STRATEGY_TRIAL_VALIDITY_SECONDS)
    gross_return = (
        evaluation.gross_return if evaluation.gross_return is not None else evaluation.net_return
    )
    return SharedOpportunityProjection.create(
        projection_id=projection_id,
        venue=definition.venue,
        source_record_type="StrategyDefinition",
        source_record_id=definition.strategy_id,
        source_canonical_hash=definition.canonical_hash,
        strategy_or_method_id=definition.strategy_id,
        instrument_id=instrument_id,
        product_or_market_id=instrument_id,
        direction="HYPOTHETICAL_LONG_SPOT_RESEARCH",
        generated_at=generated_at,
        valid_until=valid_until,
        evidence_class="GENUINE_PUBLIC",
        freshness_seconds=0,
        freshness_limit_seconds=STRATEGY_TRIAL_FRESHNESS_LIMIT_SECONDS,
        expected_gross_return=gross_return,
        expected_net_return=evaluation.net_return,
        comparable_value=evaluation.win_rate,
        comparable_value_identity="WIN_RATE" if evaluation.win_rate is not None else None,
        uncertainty=None,
        maximum_loss=STRATEGY_TRIAL_NOTIONAL,
        modeled_fees=evaluation.total_fees,
        modeled_slippage=evaluation.total_slippage,
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital=STRATEGY_TRIAL_NOTIONAL,
        maximum_sensible_research_allocation=STRATEGY_TRIAL_NOTIONAL,
        source_maximum_allocation_fraction=Decimal("1"),
        expected_holding_days=None,
        estimated_lockup_days=None,
        supporting_evidence_ids=(evaluation.canonical_hash,),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(
            f"reference_price {reference_price} supplied at construction time, "
            "not independently re-validated by this projection",
        ),
        source_details=(
            ("evaluation_kind", evaluation.evaluation_kind.value),
            ("source_kind", definition.source_kind.value),
            ("sample_size", str(evaluation.sample_size)),
        ),
        statistical_sample_size=evaluation.sample_size,
    )


@dataclass(frozen=True, slots=True)
class StrategyPaperOrderPlacement:
    """Everything produced by placing (not yet filling) one strategy-driven
    PAPER order -- the authorize/construct/preview/submit stage."""

    definition_id: str
    projection_id: str
    eligibility: CryptoPaperEligibility
    intent: CryptoPaperOrderIntent | None
    preview: CryptoPaperPreview | None
    order: CryptoPaperOrder | None


@dataclass(frozen=True, slots=True)
class StrategyPaperTrialOutcome:
    """The complete lifecycle result of one strategy-driven PAPER trial,
    from placement through fill/expiry/cancellation and reconciliation --
    the record Phase G's closed loop converts into attribution evidence."""

    placement: StrategyPaperOrderPlacement
    fill: CryptoPaperFillApplication | None
    closure: CryptoPaperCancellation | None
    reconciliation: CryptoPaperReconciliation | None


def closed_position_attribution_source_hash(order_canonical_hash: str) -> str:
    """The ``source_hash`` of the one superseding attribution row an order
    receives once its trial has a ``TRIAL_HORIZON_EXIT`` closure (PAPER Trial
    Closure Readiness v1, F3). Shared so a reader can locate that exact row
    by ``(source_id, source_hash)`` instead of re-deriving the formula."""

    return content_digest((order_canonical_hash, "POSITION_CLOSED"))


def build_attribution_source_from_trial(
    *,
    definition: StrategyDefinition,
    evaluation: StrategyEvaluationResult,
    outcome: StrategyPaperTrialOutcome,
    materialized_at: datetime,
    context_at_consideration_id: str | None = None,
    consideration_context: str | None = None,
    position_closed: bool = False,
    closed_at: datetime | None = None,
) -> AttributionSourceSnapshot:
    """Map one completed strategy-driven PAPER trial into the existing,
    unmodified ``AttributionSourceSnapshot`` shape -- closing the loop
    through Apex's existing performance/health machinery
    (``performance_attribution.materialize_attribution`` /
    ``assess_strategy_health``) without adding a parallel one.

    ``context_at_consideration_id``/``consideration_context`` close the
    Total Market Context integration hook (Recurring Strategy PAPER
    Orchestration v1, Phase D): a caller with a real, retained
    ``TotalMarketContextSnapshot`` associated with this trial's own
    consideration point passes its exact ``snapshot_id``/
    ``overall_state.value`` here -- mirroring the identical, already-shipped
    pattern ``adapters.performance_attribution_sources.
    RetainedPerformanceAttributionSource._crypto_snapshot`` already uses for
    the older Cross-Venue-Allocation PAPER path
    (``context_at_consideration_id=decision.context_snapshot_id,
    consideration_context=decision.context_state.value``). Both default to
    ``None`` -- never fabricated -- so a caller with no eligible context
    (Phase D's own documented abstain path) is unchanged from before this
    parameter existed, and ``performance_attribution.assess_strategy_health``
    correctly reports ``DATA_QUALITY_BLOCKED`` rather than inventing
    evidence (see this module's own persistence test for why that is the
    honest prior default, not a bug).

    ``position_closed`` (Repeatable Strategy PAPER Evidence v1): ``outcome.
    closure`` alone only ever gets set for an order that expired/canceled
    without fully executing (a ``CryptoPaperCancellation`` -- see the
    caller, ``adapters.operating_cycle_tasks.
    _materialize_strategy_paper_performance``) -- it was never a signal
    for "this order's OWN fill fully closed the position," since no SELL
    fill had ever been exercised before this milestone. A caller that has
    independently confirmed the underlying ``domain.crypto_paper.
    CryptoPaperPosition.state == "CLOSED"`` (e.g. a bounded trial's own
    closing SELL) passes ``position_closed=True`` here rather than this
    function re-deriving it or fabricating a ``CryptoPaperCancellation``
    that would misrepresent a genuine fill as a cancellation. Defaults to
    ``False`` -- a caller that never passes it sees byte-identical
    behavior to before this parameter existed.

    PAPER Trial Closure Readiness v1:

    * Superseding, never rewriting (F3). Attribution rows are immutable and
      reused by ``(source_id, source_hash)``; a BUY order's own hash never
      changes after its fill, so its OPEN-era row was reused forever, even
      after the trial closed. When ``position_closed`` is set, the source
      hash folds in ``"POSITION_CLOSED"`` -- a distinct source hash, so one
      new, superseding row is materialized for the closed economic outcome
      while the historical OPEN row stays retained and auditable. Unchanged
      for every not-closed order (no churn). ``closed_at`` (the economic
      close time) becomes ``completed_at`` when supplied.
    * An expired/canceled SELL is never "CLOSED" (F4). ``outcome.closure``
      (a ``CryptoPaperCancellation``) on a SELL means the close attempt
      died, and the position it was meant to close is still open -- so only
      a BUY cancellation (an opening order that ended) is terminal. A SELL
      never reports ``open_exposure`` of its own: it reduces exposure, and
      the exposure itself is attributed to the BUY that created it."""

    order = outcome.placement.order
    preview = outcome.placement.preview
    if order is None or preview is None:
        raise StrategyPaperTrialError("cannot attribute a trial that was never placed")

    fill = outcome.fill
    filled_quantity = (
        fill.order.filled_base_quantity if fill is not None else order.filled_base_quantity
    )
    filled_notional = (
        fill.order.filled_quote_quantity if fill is not None else order.filled_quote_quantity
    )
    realized_fees = fill.order.cumulative_fees if fill is not None else order.cumulative_fees
    terminal = position_closed or (outcome.closure is not None and order.side is PaperSide.BUY)
    position_state = "CLOSED" if terminal else order.state.value
    reconciliation_status = (
        outcome.reconciliation.status.value if outcome.reconciliation is not None else None
    )

    return AttributionSourceSnapshot(
        source_id=order.order_id,
        source_hash=(
            closed_position_attribution_source_hash(order.canonical_hash)
            if position_closed
            else order.canonical_hash
        ),
        venue=PaperVenue.CRYPTO_INTERNAL_PAPER,
        evidence_class=PaperEvidenceClass.GENUINE_PAPER,
        strategy_or_method_id=definition.strategy_id,
        strategy_or_method_version=str(definition.version),
        instrument_id=definition.instrument_ids[0],
        side=order.side.value,
        source_opportunity_id=outcome.placement.projection_id,
        shared_projection_id=outcome.placement.projection_id,
        allocation_decision_id=None,
        paper_trial_proposal_id=None,
        authorization_id=order.authorization_id,
        order_id=order.order_id,
        reservation_id=None,
        reservation_original_amount=None,
        reservation_current_amount=None,
        reservation_consumed_amount=None,
        fill_ids=() if fill is None else (fill.fill.fill_id,),
        position_id=None,
        settlement_id=None,
        resolution_fact_id=None,
        context_at_consideration_id=context_at_consideration_id,
        context_at_execution_id=None,
        consideration_context=consideration_context,
        execution_context=None,
        intended_quantity=preview.estimated_base_quantity,
        filled_quantity=filled_quantity,
        intended_notional=preview.estimated_total_cost,
        filled_notional=filled_notional,
        expected_gross_return=evaluation.gross_return,
        realized_gross_return=None,
        expected_net_return=evaluation.net_return,
        realized_net_return=None,
        estimated_fees=preview.modeled_fee,
        realized_fees=realized_fees,
        estimated_slippage=preview.modeled_slippage,
        realized_slippage=None,
        modeled_maximum_loss=STRATEGY_TRIAL_NOTIONAL,
        realized_loss=None,
        open_exposure=None if terminal or order.side is PaperSide.SELL else filled_notional,
        unrealized_paper_pnl=None,
        expected_lockup_seconds=None,
        actual_lockup_seconds=None,
        considered_at=order.submitted_at,
        first_fill_at=fill.fill.event_time if fill is not None else None,
        completed_at=None if not terminal else (closed_at or order.submitted_at),
        expires_at=order.valid_until,
        position_state=position_state,
        reconciliation_status=reconciliation_status,
        reconciliation_issues=(
            () if outcome.reconciliation is None else outcome.reconciliation.discrepancies
        ),
        opportunity_expiration_behavior=None,
        limitations=(
            "source_kind="
            + definition.source_kind.value
            + (
                ""
                if definition.source_kind is StrategySourceKind.EXTERNAL
                else " (internal method identity wrapper)"
            ),
        ),
    )


def recurring_strategy_paper_trial_seed(
    *, strategy_id: str, strategy_version: int, instrument_id: str
) -> str:
    """The deterministic, cycle-independent base identity for one recurring
    strategy-driven PAPER trial (Recurring Strategy PAPER Orchestration v1,
    Phase B/F). A pure function of the strategy's own stable identity plus
    the traded instrument -- never wall-clock time, never a cycle_id -- so a
    caller re-derives the identical seed on every cycle, letting an existing
    ``CryptoPaperAuthorization`` lookup on this exact id recognize "already
    open"/"already considered" without any new registration or repository:
    ``strategy_id``/``version`` never change once promoted (see
    ``strategy_definition.next_version``'s parent-lineage contract), so the
    same inputs always produce the same seed.

    Deliberately scoped to ``(strategy_id, version, instrument_id)`` only: at
    most one lifetime trial per promoted strategy version per instrument.
    Continuous re-signaling (a fresh trial after an earlier one closes, or
    multiple concurrent trials per strategy) would need a richer per-signal
    identity -- e.g. a signal-generation window bucket -- plus corresponding
    capital-reuse accounting; deliberately not built here (Phase E: "do not
    invent a portfolio optimizer"). This milestone's required demonstration
    is one genuine open -> continue -> close lifecycle, not an open-ended
    re-signaling engine.
    """

    return deterministic_id(
        "strategy_paper_trial_recurring_candidate",
        (strategy_id, strategy_version, instrument_id),
    )


def daily_candles_from_close_observations(
    observations: tuple[CryptoDailyObservation, ...],
    *,
    source_observation_id: str,
    evidence_class: EvidenceClass,
    ingested_at: datetime,
) -> tuple[CryptoDailyCandle, ...]:
    """Derive genuine, close-only daily bars into the OHLC-shaped
    ``CryptoDailyCandle`` input ``domain.strategy_benchmark.
    simulate_strategy_backtest`` already expects.

    Apex's recurring CRYPTO_MARKET_PULSE/CRYPTO_UNIVERSE_REFRESH pipeline
    retains genuine daily *close* prices only (``CryptoDailyObservation`` --
    no open/high/low; see its own module), whereas full-OHLC
    ``CryptoDailyCandle`` evidence today lives exclusively in the separate,
    much heavier crypto-candidate-research/campaign subsystem (hypothesis ->
    capture -> experiment -> dossier) this orchestration-only milestone
    deliberately does not reach into (Phase A archaeology). Rather than
    fabricate an unobserved open/high/low, this sets
    ``open = high = low = close`` for each real retained close -- the
    standard, conservative "close-to-close" backtesting convention. Signal
    computation is unaffected (``compute_daily_signal`` only ever reads
    strictly-prior closes); execution is priced at the SAME day's own close
    rather than an unobserved same-day open, which only ever ADDS one extra
    day of honest implementation lag versus the intraday-open execution
    ``simulate_strategy_backtest`` was written against -- it never turns an
    unseen price into a favorable one. Every value used here is a real,
    retained, genuinely-captured Coinbase daily close; nothing here is a
    fixture, and ``evidence_class`` is passed through from the source
    observation unchanged, never upgraded or downgraded.
    """

    return tuple(
        CryptoDailyCandle.create(
            capture_id=source_observation_id,
            attempt_id=deterministic_id(
                "strategy_paper_trial_close_derived_candle_attempt",
                (source_observation_id, item.bucket_start),
            ),
            evidence_class=evidence_class,
            bucket_start=item.bucket_start,
            open=item.close,
            high=item.close,
            low=item.close,
            close=item.close,
            volume=item.volume,
            ingested_at=ingested_at,
            raw_response_hash=content_digest(
                (source_observation_id, item.bucket_start, item.close)
            ),
        )
        for item in sorted(observations, key=lambda entry: entry.bucket_start)
    )
