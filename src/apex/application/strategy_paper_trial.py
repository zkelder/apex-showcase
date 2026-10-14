"""Orchestrates one strategy-driven CRYPTO PAPER trial end to end, reusing
Apex's existing Coinbase-Compatible Crypto Spot PAPER Adapter application
services (``application.crypto_paper``) unmodified. See
``domain.strategy_paper_trial`` for why this is safe -- every economic
decision remains that existing, already-reviewed machinery; this module is
pure orchestration plus one genuine, credential-free market-data capture.

``ExecuteStrategyPaperTrial.place_order`` mirrors
``adapters.operating_cycle_tasks._execute_crypto_paper_trial``'s
authorize -> construct -> preview -> submit sequence, narrowed to one
strategy-sourced candidate at a time (no cross-venue-allocation proposal
list) and driven by a caller-supplied repository -- in this milestone,
always the existing ``PostgresCryptoPaperRepository`` pointed at an
isolated, disposable database, never production (see docs/
WORKTREE_VERIFICATION.md and this milestone's own operating instructions).
Fill, expiry/cancellation, and reconciliation reuse the existing
``ApplyCryptoPaperMarketEvidence`` / ``ExpireCryptoPaperOrder`` /
``ReconcileCryptoPaperAccount`` services directly -- they are already
fully venue-generic (keyed by ``account_id``/``order_id`` only) and need no
strategy-specific wrapper.

``place_order``/``apply_evidence`` accept a ``RetainedPaperPriceEvidence``
however the caller obtained it -- they do not care how. The caller SHOULD
obtain it via ``obtain_decision_price_evidence`` below, which prefers the
canonical Live Market Observation substrate (``application.
live_market_observation.ObtainContemporaneousMarketObservation``) so a
real decision retains the exact durable ``observation_id`` that supported
it, rather than via ``capture_genuine_price_evidence`` directly -- that
function remains only as the bounded fallback ``obtain_decision_price_
evidence`` itself reaches for on STALE/ABSENT, or for a test seeding an
isolated substrate observation from a real provider call.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from apex.application.crypto_paper import (
    ApplyCryptoPaperMarketEvidence,
    AuthorizeCryptoPaperOpportunity,
    ConstructCryptoPaperOrderIntent,
    CryptoPaperRepository,
    CryptoPaperResult,
    ExpireCryptoPaperOrder,
    PreviewCryptoPaperOrder,
    ReconcileCryptoPaperAccount,
    RegisterCryptoPaperProviderContract,
    SubmitCryptoPaperOrder,
)
from apex.application.live_market_observation import (
    LiveQuoteSource,
    ObtainContemporaneousMarketObservation,
)
from apex.domain.crypto_paper import (
    MAX_ORDER_NOTIONAL,
    CryptoPaperAuthorization,
    CryptoPaperCancellation,
    CryptoPaperFillApplication,
    CryptoPaperProviderContract,
    CryptoPaperReconciliation,
    PaperEligibilityState,
    PaperOrderType,
    PaperPricingSource,
    PaperSide,
    RetainedPaperPriceEvidence,
    evaluate_paper_eligibility,
)
from apex.domain.crypto_universe import CryptoUniverseSnapshot
from apex.domain.live_market_observation import (
    ContemporaneousObservationDisposition,
    LiveObservationType,
)
from apex.domain.shared_opportunity import (
    EligibilityState,
    SharedOpportunityProjection,
    SharedVenue,
    evaluate_eligibility,
)
from apex.domain.strategy_definition import StrategyDefinition
from apex.domain.strategy_paper_trial import (
    PAPER_TRIAL_PRICE_SCALE,
    STRATEGY_TRIAL_NOTIONAL,
    StrategyPaperOrderPlacement,
    StrategyPaperTrialError,
    price_evidence_from_live_observation,
)

# Same conservative cost assumptions the recurring cycle's own
# _execute_crypto_paper_trial uses (adapters/operating_cycle_tasks.py) --
# reused verbatim, never separately tuned for this new caller.
STRATEGY_TRIAL_FEE_BPS = 60
STRATEGY_TRIAL_SPREAD_BPS = 10
STRATEGY_TRIAL_SLIPPAGE_BPS = 10
STRATEGY_TRIAL_ORDER_VALID_FOR = timedelta(hours=4)
STRATEGY_TRIAL_AUTHORIZATION_VALID_FOR = timedelta(hours=1)


def capture_genuine_price_evidence(
    source: LiveQuoteSource,
    *,
    product_id: str,
    requested_at: datetime,
    maximum_depth: int = 5,
) -> RetainedPaperPriceEvidence:
    """One real, credential-free public Coinbase top-of-book capture --
    never a fixture. Mirrors adapters/operating_cycle_tasks.py's
    ``_crypto_price_evidence`` shape, sourced from a fresh capture instead
    of a pre-existing pulse checkpoint.

    ``source`` is injected (the same ``LiveQuoteSource`` Protocol
    ``application.live_market_observation`` already depends on) rather than
    constructed here -- the application layer must not import
    ``apex.adapters`` directly (see ``tests/security/
    test_architecture_boundaries.py``); the caller (a CLI/composition root,
    or a test) is responsible for constructing the concrete
    ``CoinbasePublicUniverseSource``.

    This is the BOUNDED FALLBACK capture, never the preferred entry point --
    see ``obtain_decision_price_evidence`` below, which consults the
    canonical Live Market Observation substrate first and only reaches this
    function when that substrate reports STALE/ABSENT. Calling this
    directly is legitimate for bootstrapping (an instrument no capture
    round has ever observed yet) or for a test seeding its own isolated
    observation from a real provider call -- never as a silent way to
    re-obtain "fresh-looking" evidence in place of a STALE verdict for an
    observation that already exists."""

    book = source.capture_product_book(
        product_id=product_id, maximum_depth=maximum_depth, requested_at=requested_at
    )
    # The real HTTP round trip means the provider's own captured_at can land
    # strictly after requested_at (the moment we initiated the call) --
    # ingested_at must be at or after observed_at, so re-stamp it using the
    # later of the two rather than the pre-call timestamp.
    ingested_at = max(requested_at, book.captured_at)
    # book.midpoint is (best_bid + best_ask) / 2, computed without regard to
    # domain.values's own MAX_SCALE=8 authoritative-decimal contract that
    # every downstream crypto_paper.py computation (spread/slippage/fee, all
    # derived by multiplying this price) must respect -- the production
    # pulse-capture pipeline's own persistence layer already enforces this
    # implicitly; a fresh capture must do so explicitly.
    price = book.midpoint.quantize(PAPER_TRIAL_PRICE_SCALE)
    return RetainedPaperPriceEvidence.create(
        evidence_id=f"strategy-paper-trial-{product_id}-{requested_at.isoformat()}-price-evidence",
        product_id=product_id,
        observed_at=book.captured_at,
        ingested_at=ingested_at,
        price=price,
        available_base_quantity=book.visible_ask_depth,
        source=PaperPricingSource.GENUINE_TOP_OF_BOOK,
        evidence_class="GENUINE_PUBLIC",
    )


def obtain_decision_price_evidence(
    *,
    contemporaneous: ObtainContemporaneousMarketObservation,
    venue: SharedVenue,
    product_id: str,
    as_of: datetime,
    max_staleness_seconds: int,
    fallback_source: LiveQuoteSource | None = None,
) -> RetainedPaperPriceEvidence:
    """The preferred entry point for strategy-driven PAPER decision-time
    price evidence: consult the canonical Live Market Observation substrate
    (``ObtainContemporaneousMarketObservation``) first, so a real PAPER
    decision retains the exact durable ``observation_id`` that supported it
    instead of opening a second, ungoverned quote-evidence path (see module
    docstring's governing requirement).

    Fails closed on STALE/ABSENT by default -- the substrate's own
    fail-closed doctrine (``evaluate_contemporaneous_observation``). A
    caller may supply ``fallback_source`` for the one legitimate bounded
    purpose this milestone identified: bootstrapping evidence for an
    instrument no capture round has ever observed yet, or a test seeding
    its own isolated observation from a real provider call without
    requiring the production live-observer worker to be installed/running.
    The fallback is never a silent way to dodge a STALE verdict for an
    observation that DOES exist -- it performs a distinct, freshly-labeled
    direct capture (``capture_genuine_price_evidence``) whose
    ``evidence_id`` carries no ``live-observation-`` prefix, so which path
    produced any given piece of evidence stays durably legible from the id
    alone.
    """

    result = contemporaneous.execute(
        venue=venue,
        instrument_id=product_id,
        observation_type=LiveObservationType.QUOTE,
        as_of=as_of,
        max_staleness_seconds=max_staleness_seconds,
    )
    if result.disposition is ContemporaneousObservationDisposition.SATISFIED:
        assert result.observation is not None
        return price_evidence_from_live_observation(result.observation, product_id=product_id)
    if fallback_source is None:
        raise StrategyPaperTrialError(
            f"PAPER_DECISION_EVIDENCE_{result.disposition.value}: no durable live "
            f"observation retained for {product_id} and no fallback_source supplied"
        )
    return capture_genuine_price_evidence(
        fallback_source, product_id=product_id, requested_at=as_of
    )


class ExecuteStrategyPaperTrial:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def place_order(
        self,
        *,
        definition: StrategyDefinition,
        projection: SharedOpportunityProjection,
        contract: CryptoPaperProviderContract,
        universe: CryptoUniverseSnapshot,
        account_id: str,
        price_evidence: RetainedPaperPriceEvidence,
        operating_cycle_enabled: bool,
        executed_at: datetime,
        actor: str,
        idempotency_seed: str,
    ) -> StrategyPaperOrderPlacement:
        if projection.strategy_or_method_id != definition.strategy_id:
            raise StrategyPaperTrialError("projection does not belong to this strategy definition")

        shared_decision = evaluate_eligibility(projection, evaluated_at=executed_at)
        account = self._repository.account(account_id)
        state = self._repository.state(account_id)
        account_state = state.account_state if state is not None else None

        eligibility = evaluate_paper_eligibility(
            decision_id=f"{idempotency_seed}-eligibility",
            opportunity_set_id=idempotency_seed,
            projection=projection,
            shared_decision=shared_decision,
            universe=universe,
            contract=contract,
            account=account,
            account_state=account_state,
            operating_cycle_enabled=operating_cycle_enabled,
            evaluated_at=executed_at,
        )
        if (
            shared_decision.state is not EligibilityState.ELIGIBLE
            or eligibility.state is not PaperEligibilityState.ELIGIBLE
        ):
            return StrategyPaperOrderPlacement(
                definition_id=definition.strategy_id,
                projection_id=projection.projection_id,
                eligibility=eligibility,
                intent=None,
                preview=None,
                order=None,
            )

        RegisterCryptoPaperProviderContract(self._repository).execute(
            contract=contract, idempotency_key=f"{contract.contract_id}-key"
        )

        authorization_id = f"{idempotency_seed}-authorization"
        authorization = CryptoPaperAuthorization.create(
            authorization_id=authorization_id,
            account_id=account_id,
            eligibility=eligibility,
            projection=projection,
            maximum_quote_notional=min(STRATEGY_TRIAL_NOTIONAL, MAX_ORDER_NOTIONAL),
            maximum_base_size=None,
            allowed_side=PaperSide.BUY,
            allowed_order_type=PaperOrderType.MARKET,
            expires_at=executed_at + STRATEGY_TRIAL_AUTHORIZATION_VALID_FOR,
            actor=actor,
            reason=f"strategy-paper-trial:{definition.strategy_id}",
            idempotency_key=f"{authorization_id}-key",
            created_at=executed_at,
        )
        AuthorizeCryptoPaperOpportunity(self._repository).execute(
            account_id=account_id, authorization=authorization
        )

        intent_id = f"{idempotency_seed}-intent"
        intent_result = ConstructCryptoPaperOrderIntent(self._repository).execute(
            account_id=account_id,
            intent_id=intent_id,
            authorization_id=authorization_id,
            contract_id=contract.contract_id,
            side=PaperSide.BUY,
            order_type=PaperOrderType.MARKET,
            quote_size=min(STRATEGY_TRIAL_NOTIONAL, MAX_ORDER_NOTIONAL),
            base_size=None,
            limit_price=None,
            created_at=executed_at,
            idempotency_key=f"{intent_id}-key",
        )

        preview_id = f"{idempotency_seed}-preview"
        preview_result = PreviewCryptoPaperOrder(self._repository).execute(
            account_id=account_id,
            preview_id=preview_id,
            intent_id=intent_result.value.intent_id,
            evidence=price_evidence,
            fee_bps=STRATEGY_TRIAL_FEE_BPS,
            spread_bps=STRATEGY_TRIAL_SPREAD_BPS,
            slippage_bps=STRATEGY_TRIAL_SLIPPAGE_BPS,
            created_at=executed_at,
            idempotency_key=f"{preview_id}-key",
        )
        if preview_result.value.rejection_reasons:
            return StrategyPaperOrderPlacement(
                definition_id=definition.strategy_id,
                projection_id=projection.projection_id,
                eligibility=eligibility,
                intent=intent_result.value,
                preview=preview_result.value,
                order=None,
            )

        order_id = f"{idempotency_seed}-order"
        submission = SubmitCryptoPaperOrder(self._repository).execute(
            account_id=account_id,
            order_id=order_id,
            reservation_id=f"{order_id}-reservation",
            preview_id=preview_result.value.preview_id,
            submitted_at=executed_at,
            valid_until=executed_at + STRATEGY_TRIAL_ORDER_VALID_FOR,
            actor=actor,
            idempotency_key=f"{order_id}-key",
        )
        return StrategyPaperOrderPlacement(
            definition_id=definition.strategy_id,
            projection_id=projection.projection_id,
            eligibility=eligibility,
            intent=intent_result.value,
            preview=preview_result.value,
            order=submission.value.order,
        )

    def close_position(
        self,
        *,
        definition: StrategyDefinition,
        projection: SharedOpportunityProjection,
        contract: CryptoPaperProviderContract,
        universe: CryptoUniverseSnapshot,
        account_id: str,
        price_evidence: RetainedPaperPriceEvidence,
        base_quantity: Decimal,
        operating_cycle_enabled: bool,
        executed_at: datetime,
        actor: str,
        idempotency_seed: str,
    ) -> StrategyPaperOrderPlacement:
        """Repeatable Strategy PAPER Evidence v1: the SELL-side sibling of
        ``place_order``, closing an existing PAPER position through the
        SAME generic authorize -> construct -> preview -> submit sequence,
        never a shortcut around it (see ``domain.strategy_paper_trial_
        lifecycle`` module docstring, Part 5D of the milestone this method
        implements). Deliberately a SEPARATE method rather than a
        refactored, side-parameterized ``place_order`` -- ``place_order``
        drives the one production PAPER position already open in this
        codebase, and this milestone's own safety requirements (Part 1:
        "without weakening ... determinism, replay correctness") make
        leaving that exact, already-proven code path untouched more
        important than deduplicating a genuinely small amount of shared
        shape. The one real difference from ``place_order``: ``side=SELL``,
        sized by ``base_quantity`` (the caller's own currently-held position
        quantity -- this method never reads it itself, so it never silently
        oversizes a close against evidence the caller didn't actually
        retain) rather than a fixed quote notional, and ``allowed_side``/
        ``maximum_base_size`` on the authorization are set accordingly.
        ``domain.crypto_paper.construct_order_intent`` itself is what
        actually refuses an oversized SELL against real account holdings
        (``CRYPTO_PAPER_ASSET_BALANCE_INSUFFICIENT``) -- this method adds no
        parallel check.

        The authorization's own ``maximum_quote_notional`` is sized from
        the position's CURRENT value (``base_quantity * price_evidence.
        price``, with a 10% buffer for the real fill price moving against
        the decision-time estimate) -- never the trial's original, much
        smaller ``STRATEGY_TRIAL_NOTIONAL`` entry size. A trial that
        genuinely appreciated well past ``domain.crypto_paper.
        MAX_ORDER_NOTIONAL`` by the time it closes is a known, disclosed,
        pre-existing limitation of that unmodified system-wide per-order
        cap, not something this method silently works around -- the
        eventual preview-stage rejection in that case is the existing
        safety behavior working as intended.
        """

        if projection.strategy_or_method_id != definition.strategy_id:
            raise StrategyPaperTrialError("projection does not belong to this strategy definition")

        shared_decision = evaluate_eligibility(projection, evaluated_at=executed_at)
        account = self._repository.account(account_id)
        state = self._repository.state(account_id)
        account_state = state.account_state if state is not None else None

        eligibility = evaluate_paper_eligibility(
            decision_id=f"{idempotency_seed}-eligibility",
            opportunity_set_id=idempotency_seed,
            projection=projection,
            shared_decision=shared_decision,
            universe=universe,
            contract=contract,
            account=account,
            account_state=account_state,
            operating_cycle_enabled=operating_cycle_enabled,
            evaluated_at=executed_at,
        )
        if (
            shared_decision.state is not EligibilityState.ELIGIBLE
            or eligibility.state is not PaperEligibilityState.ELIGIBLE
        ):
            return StrategyPaperOrderPlacement(
                definition_id=definition.strategy_id,
                projection_id=projection.projection_id,
                eligibility=eligibility,
                intent=None,
                preview=None,
                order=None,
            )

        RegisterCryptoPaperProviderContract(self._repository).execute(
            contract=contract, idempotency_key=f"{contract.contract_id}-key"
        )

        # base_quantity carries up to 8 decimal places (BTC's own
        # base_increment) and price_evidence.price carries 2
        # (PAPER_TRIAL_PRICE_SCALE) -- their product, further scaled by the
        # 1.1 buffer, can exceed domain.values.MAX_SCALE (8) before
        # rounding. Quantize to cents (matching every other price this
        # module produces) with ROUND_CEILING so the cap is never rounded
        # DOWN below the position's own real value.
        current_position_value = base_quantity * price_evidence.price
        maximum_quote_notional = min(
            (current_position_value * Decimal("1.1")).quantize(
                PAPER_TRIAL_PRICE_SCALE, rounding=ROUND_CEILING
            ),
            MAX_ORDER_NOTIONAL,
        )
        # PAPER Trial Closure Readiness v1: RESUMABLE. Each step below is its
        # own short transaction, so a crash or rejection can leave this
        # attempt part-way done. Every step first looks for its own
        # already-retained artifact (exact id) and reuses it rather than
        # recreating it: an existing authorization is never re-authorized,
        # an existing order is never resubmitted, and a dead preview
        # (rejected or expired) ends this attempt without an order -- the
        # caller (``domain.strategy_paper_trial_lifecycle.
        # classify_close_attempts``) decides when a NEW attempt may begin.
        authorization_id = f"{idempotency_seed}-authorization"
        intent_id = f"{idempotency_seed}-intent"
        preview_id = f"{idempotency_seed}-preview"
        order_id = f"{idempotency_seed}-order"
        retained_authorization = _by_id(
            () if state is None else state.authorizations, "authorization_id", authorization_id
        )
        retained_order = _by_id(() if state is None else state.orders, "order_id", order_id)
        if retained_order is not None:
            return StrategyPaperOrderPlacement(
                definition_id=definition.strategy_id,
                projection_id=projection.projection_id,
                eligibility=eligibility,
                intent=_by_id(state.intents if state else (), "intent_id", intent_id),
                preview=_by_id(state.previews if state else (), "preview_id", preview_id),
                order=retained_order,
            )
        if retained_authorization is None:
            authorization = CryptoPaperAuthorization.create(
                authorization_id=authorization_id,
                account_id=account_id,
                eligibility=eligibility,
                projection=projection,
                maximum_quote_notional=maximum_quote_notional,
                maximum_base_size=base_quantity,
                allowed_side=PaperSide.SELL,
                allowed_order_type=PaperOrderType.MARKET,
                expires_at=executed_at + STRATEGY_TRIAL_AUTHORIZATION_VALID_FOR,
                actor=actor,
                reason=f"strategy-paper-trial-close:{definition.strategy_id}",
                idempotency_key=f"{authorization_id}-key",
                created_at=executed_at,
            )
            AuthorizeCryptoPaperOpportunity(self._repository).execute(
                account_id=account_id, authorization=authorization
            )

        intent = _by_id(state.intents if state else (), "intent_id", intent_id)
        if intent is None:
            intent = (
                ConstructCryptoPaperOrderIntent(self._repository)
                .execute(
                    account_id=account_id,
                    intent_id=intent_id,
                    authorization_id=authorization_id,
                    contract_id=contract.contract_id,
                    side=PaperSide.SELL,
                    order_type=PaperOrderType.MARKET,
                    quote_size=None,
                    base_size=base_quantity,
                    limit_price=None,
                    created_at=executed_at,
                    idempotency_key=f"{intent_id}-key",
                )
                .value
            )

        preview = _by_id(state.previews if state else (), "preview_id", preview_id)
        if preview is None:
            preview = (
                PreviewCryptoPaperOrder(self._repository)
                .execute(
                    account_id=account_id,
                    preview_id=preview_id,
                    intent_id=intent.intent_id,
                    evidence=price_evidence,
                    fee_bps=STRATEGY_TRIAL_FEE_BPS,
                    spread_bps=STRATEGY_TRIAL_SPREAD_BPS,
                    slippage_bps=STRATEGY_TRIAL_SLIPPAGE_BPS,
                    created_at=executed_at,
                    idempotency_key=f"{preview_id}-key",
                )
                .value
            )
        if preview.rejection_reasons or executed_at >= preview.expires_at:
            return StrategyPaperOrderPlacement(
                definition_id=definition.strategy_id,
                projection_id=projection.projection_id,
                eligibility=eligibility,
                intent=intent,
                preview=preview,
                order=None,
            )

        submission = SubmitCryptoPaperOrder(self._repository).execute(
            account_id=account_id,
            order_id=order_id,
            reservation_id=f"{order_id}-reservation",
            preview_id=preview.preview_id,
            submitted_at=executed_at,
            valid_until=executed_at + STRATEGY_TRIAL_ORDER_VALID_FOR,
            actor=actor,
            idempotency_key=f"{order_id}-key",
        )
        return StrategyPaperOrderPlacement(
            definition_id=definition.strategy_id,
            projection_id=projection.projection_id,
            eligibility=eligibility,
            intent=intent,
            preview=preview,
            order=submission.value.order,
        )

    def apply_evidence(
        self,
        *,
        account_id: str,
        order_id: str,
        fill_id: str,
        evidence: RetainedPaperPriceEvidence,
        actor: str,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperFillApplication | None]:
        return ApplyCryptoPaperMarketEvidence(self._repository).execute(
            account_id=account_id,
            order_id=order_id,
            fill_id=fill_id,
            evidence=evidence,
            fee_bps=STRATEGY_TRIAL_FEE_BPS,
            slippage_bps=STRATEGY_TRIAL_SLIPPAGE_BPS,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    def expire_order(
        self,
        *,
        account_id: str,
        order_id: str,
        expired_at: datetime,
        actor: str,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperCancellation]:
        return ExpireCryptoPaperOrder(self._repository).execute(
            account_id=account_id,
            order_id=order_id,
            expired_at=expired_at,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    def reconcile(
        self,
        *,
        account_id: str,
        reconciliation_id: str,
        evaluated_at: datetime,
        order_id: str | None,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperReconciliation]:
        return ReconcileCryptoPaperAccount(self._repository).execute(
            account_id=account_id,
            reconciliation_id=reconciliation_id,
            evaluated_at=evaluated_at,
            order_id=order_id,
            idempotency_key=idempotency_key,
        )


def _by_id[T](values: Sequence[T], attribute: str, identity: str) -> T | None:
    """The retained artifact whose ``attribute`` equals ``identity`` exactly."""

    return next((item for item in values if getattr(item, attribute) == identity), None)
