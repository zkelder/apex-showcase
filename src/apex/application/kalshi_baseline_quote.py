"""Application orchestration for Kalshi Decision-Linked Baseline Quote
Evidence v1, plus Kalshi Baseline Quote Capture & Economic Maturation v1's
additive ``MARKET_QUOTE_OBSERVATION`` fallback.

Reads currently-retained, already-immutable evidence through a narrow,
read-only Protocol seam, delegates all normalization logic to the pure
``domain.kalshi_baseline_quote`` builders, and persists each resulting
baseline quote idempotently. Performs no provider I/O of its own -- every
read the source Protocol exposes is already-retained, local evidence; this
service issues no HTTP request, creates no new investment judgment, and
mutates no historical row.

Self-healing, bounded sweep, mirroring ``application.counterfactual_followup
.SyncFollowUpObservations``/``application.counterfactual_outcome.
RefreshCounterfactualOutcomeAggregates``'s own idiom: every call re-scans for
KALSHI decisions still missing a baseline quote (oldest ``decision_id``
first, bounded by ``limit``) rather than tracking "this cycle's new
decisions" -- so a decision whose capture was skipped one cycle (budget
exhausted, a transient failure, or simply because it existed before this
capability shipped) is naturally retried by a later cycle without any
separate backfill mechanism. Because the source evaluation rows this reads
are themselves immutable, a capture attempted many cycles after decision
creation reproduces the byte-identical result a same-cycle capture would
have -- there is no staleness race to lose.

**Dispatch, per decision (Kalshi Baseline Quote Capture & Economic
Maturation v1)**: for each pending decision whose referenced evaluation is
found, exactly one of three things happens, never more than one:

1. The evaluation is a genuine ``CANDIDATE`` match -- the existing
   ``KalshiBaselineQuoteEvidence`` (matched-trade economics, V1) path
   fires, completely unmodified from how it always worked.
2. The evaluation is not a ``CANDIDATE``, but its already-retained
   orderbook snapshot(s) are genuinely two-sided -- the new
   ``KalshiMarketQuoteBaselineEvidence`` (plain market quote, V2) path
   fires instead, sourcing entirely from the batched
   ``orderbook_top_of_book`` read below. Zero new provider requests either
   way: every field either path persists was already fetched and retained
   by an earlier ``KALSHI_MARKET_REFRESH`` cycle.
3. Neither applies (no candidate match, and the orderbook was one-sided,
   missing entirely, or otherwise unusable) -- honestly counted, never
   retried differently later (the referenced evidence is immutable, so this
   decision will structurally never produce a row under either policy).

**Per-decision fault isolation (Kalshi Baseline Temporal Semantics & Sweep
Isolation v1)**: attempting path 1 or 2 above can still fail -- a malformed
or temporally-inconsistent referenced evaluation/snapshot raises
``KalshiBaselineQuoteError``/``ValueValidationError`` out of the relevant
``domain.kalshi_baseline_quote`` builder. Production once let that
exception escape ``execute`` entirely: the very first rejection aborted the
whole sweep, discarding every already-processed capture from that call and
silently starving every decision still unprocessed (confirmed: a coarse
day-boundary ``decision.evaluated_at`` compared against a real, later,
same-flow evidence-capture timestamp -- see ``domain.kalshi_baseline_quote
.KalshiMarketQuoteBaselineEvidence``'s own corrected comment for the full
temporal-authority finding). ``execute`` now catches exactly those two
named, already-established domain exception types around each individual
build attempt, counts the rejection by decision kind (``matched_trade_
validation_rejected_count``/``market_quote_validation_rejected_count``) and
by its exact reason (``rejection_reason_counts``), and continues to the
next decision. Deliberately narrow: nothing else is caught here -- a
genuine persistence failure from ``_save``'s own repository write (never
inside one of these ``try`` blocks), or any exception type this module does
not itself define as an expected rejection, still propagates immediately
and fails this task for real, exactly as it always did."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.counterfactual_ledger import CounterfactualDecision
from apex.domain.kalshi_baseline_quote import (
    BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
    THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE,
    KalshiBaselineQuoteError,
    KalshiBaselineQuoteEvidence,
    KalshiMarketQuoteBaselineEvidence,
    KalshiOrderbookTopOfBook,
    build_kalshi_baseline_quote_from_binary_complement,
    build_kalshi_baseline_quote_from_threshold_bundle,
    build_kalshi_market_quote_baseline_from_binary_complement,
    build_kalshi_market_quote_baseline_from_threshold_bundle,
)
from apex.domain.public_complement import (
    BinaryComplementEvaluation,
    ComplementFillSlice,
    ComplementState,
)
from apex.domain.public_threshold import (
    ThresholdBundleEvaluation,
    ThresholdEvaluationState,
    ThresholdMatchedSlice,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection
from apex.domain.values import ValueValidationError

DEFAULT_KALSHI_BASELINE_QUOTE_SWEEP_LIMIT = 500

_PersistableKalshiBaseline = KalshiBaselineQuoteEvidence | KalshiMarketQuoteBaselineEvidence


class KalshiBaselineQuoteEvidenceSource(Protocol):
    def pending_kalshi_decisions(self, *, limit: int) -> tuple[CounterfactualDecision, ...]:
        """KALSHI-venue decisions with no retained baseline quote yet,
        deterministically ordered, bounded to ``limit``."""
        ...

    def projections_by_id(
        self, projection_ids: Collection[str]
    ) -> Mapping[str, SharedOpportunityProjection]: ...

    def binary_complement_evidence(
        self, evaluation_ids: tuple[str, ...]
    ) -> Mapping[str, tuple[BinaryComplementEvaluation, tuple[ComplementFillSlice, ...]]]: ...

    def threshold_bundle_evidence(
        self, evaluation_ids: tuple[str, ...]
    ) -> Mapping[
        str, tuple[ThresholdBundleEvaluation, tuple[ThresholdMatchedSlice, ...], str, str]
    ]: ...

    def orderbook_top_of_book(
        self, snapshot_ids: Collection[str]
    ) -> Mapping[str, KalshiOrderbookTopOfBook]:
        """Batched, N+1-safe read of the top-of-book bid on each side of
        every named, already-retained, immutable ``market_orderbook_
        snapshot`` row -- never a live/current read, never a different
        snapshot than the one a decision's own fixed evaluation already
        names. A snapshot id with no genuine two-sided book present is
        still returned (with ``yes_bid``/``no_bid`` set to ``None`` on
        whichever side is missing), never silently omitted -- omission and
        "genuinely one-sided" must stay distinguishable to the caller."""
        ...


class KalshiBaselineQuoteRepository(Protocol):
    def save_quote(self, *, quote: _PersistableKalshiBaseline) -> bool: ...


class KalshiBaselineQuoteSyncError(ValueError):
    """Raised when a decision claimed by ``pending_kalshi_decisions`` cannot
    be resolved to its already-retained subject projection -- a genuine
    referential-integrity violation, since the decision itself could only
    have been built by resolving that same projection."""


@dataclass(frozen=True, slots=True)
class SyncKalshiBaselineQuoteResult:
    run_id: str
    generated_at: datetime
    considered_count: int
    # Totals across BOTH kinds -- the meaningful top-line "how many baseline
    # quotes did this cycle retain" answer every existing caller/warning
    # string already reads unchanged.
    captured_count: int
    idempotent_replay_count: int
    # Kalshi Baseline Quote Capture & Economic Maturation v1: granular
    # breakdown of the totals above by kind, additive.
    matched_trade_captured_count: int
    market_quote_captured_count: int
    unsupported_source_record_type_count: int
    # Unchanged meaning from before this milestone: the referenced
    # evaluation was found but was not a genuine CANDIDATE match. No longer
    # a dead end by itself -- see ``insufficient_market_quote_evidence_count``
    # for whether the V2 fallback then also failed or succeeded for the
    # same decisions.
    no_candidate_evaluation_count: int
    # Of the ``no_candidate_evaluation_count`` decisions, how many also could
    # not produce a V2 market-quote baseline for ANY reason (one-sided
    # orderbook, no snapshot at all, or a rejected build attempt) -- the
    # honest, permanent "no usable baseline evidence of either kind exists
    # for this decision" total. Always equals ``missing_snapshot_count +
    # one_sided_orderbook_count + market_quote_validation_rejected_count``;
    # kept as its own field (rather than removed) so no existing reader of
    # this already-established total needs to change.
    insufficient_market_quote_evidence_count: int
    # Kalshi Baseline Temporal Semantics & Sweep Isolation v1: the
    # ``insufficient_market_quote_evidence_count`` total broken down by
    # reason family, so a future production regression is visible by cause
    # rather than only by an undifferentiated count -- see this module's
    # own docstring and ``domain.kalshi_baseline_quote``'s corrected
    # temporal-invariant comment for the full rationale.
    #
    # No ``market_orderbook_snapshot`` row at all was found for the
    # evaluation's own referenced snapshot id (or the evaluation named no
    # snapshot id in the first place) -- permanent for this decision; the
    # referenced evaluation is immutable, so a later cycle cannot discover a
    # snapshot that was never captured.
    missing_snapshot_count: int
    # A snapshot was found for the referenced id, but its own orderbook was
    # genuinely one-sided (or otherwise not ``is_two_sided``) -- also
    # permanent, for the identical immutability reason.
    one_sided_orderbook_count: int
    # A per-decision V2 build was attempted (a two-sided snapshot was
    # genuinely available) but ``build_kalshi_market_quote_baseline_from_
    # binary_complement``/``..._from_threshold_bundle`` rejected it --
    # ``KalshiBaselineQuoteError``/``ValueValidationError`` raised during
    # construction, caught and counted here rather than aborting the
    # sweep (Kalshi Baseline Temporal Semantics & Sweep Isolation v1's
    # per-decision fault isolation). Bounded, expected, and permanent for
    # this decision -- the referenced evidence is immutable, so a replay
    # reproduces the identical rejection, never a different one.
    market_quote_validation_rejected_count: int
    # A V1 (matched-trade) build was attempted for a genuine CANDIDATE
    # evaluation but was rejected during construction -- structurally rare
    # (the sweep only ever attempts this when ``ComplementState.CANDIDATE``/
    # ``ThresholdEvaluationState.CANDIDATE`` already holds), but isolated
    # for the identical reason: one malformed decision must never abort
    # unrelated valid ones, no matter which of the two kinds it was
    # attempting.
    matched_trade_validation_rejected_count: int
    # The exact reason code from every caught validation rejection above
    # (both kinds), paired with how many decisions hit it this sweep --
    # sorted by reason code for deterministic comparison in tests and
    # operator warnings. A single decision contributes exactly one entry
    # (whichever build it attempted last); reads as a bounded diagnostic,
    # never iterated for control flow.
    rejection_reason_counts: tuple[tuple[str, int], ...]
    source_evaluation_not_found_count: int
    quote_ids: tuple[str, ...]


class SyncKalshiBaselineQuoteEvidence:
    """Bounded, idempotent, crash/replay-safe capture. No provider request,
    no PAPER/LIVE/execution authority; every produced row carries
    ``authority = "NONE"``."""

    def __init__(
        self, source: KalshiBaselineQuoteEvidenceSource, repository: KalshiBaselineQuoteRepository
    ) -> None:
        self._source = source
        self._repository = repository

    def execute(
        self,
        *,
        run_id: str,
        generated_at: datetime,
        limit: int = DEFAULT_KALSHI_BASELINE_QUOTE_SWEEP_LIMIT,
    ) -> SyncKalshiBaselineQuoteResult:
        decisions = self._source.pending_kalshi_decisions(limit=limit)

        binary_ids = tuple(
            decision.subject_source_record_id
            for decision in decisions
            if decision.subject_source_record_type == BINARY_COMPLEMENT_SOURCE_RECORD_TYPE
        )
        threshold_ids = tuple(
            decision.subject_source_record_id
            for decision in decisions
            if decision.subject_source_record_type == THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE
        )
        binary_evidence = self._source.binary_complement_evidence(binary_ids)
        threshold_evidence = self._source.threshold_bundle_evidence(threshold_ids)
        projection_ids = {decision.subject_projection_id for decision in decisions}
        projections = self._source.projections_by_id(projection_ids)

        # Kalshi Baseline Quote Capture & Economic Maturation v1: batch every
        # orderbook snapshot id any decision's evaluation references, up
        # front, regardless of whether it will turn out CANDIDATE (and so
        # never need it) -- one bounded round trip, never a per-decision
        # probe, mirroring every other read this class already performs.
        snapshot_ids: set[str] = set()
        for complement_evaluation, _fill_slices in binary_evidence.values():
            snapshot_ids.add(complement_evaluation.orderbook_snapshot_id)
        for bundle_evaluation, _slices, _broader, _narrower in threshold_evidence.values():
            if bundle_evaluation.broader_orderbook_snapshot_id:
                snapshot_ids.add(bundle_evaluation.broader_orderbook_snapshot_id)
            if bundle_evaluation.narrower_orderbook_snapshot_id:
                snapshot_ids.add(bundle_evaluation.narrower_orderbook_snapshot_id)
        top_of_book = self._source.orderbook_top_of_book(snapshot_ids) if snapshot_ids else {}

        quote_ids: list[str] = []
        matched_trade_captured = 0
        market_quote_captured = 0
        replayed = 0
        unsupported_type = 0
        no_candidate = 0
        missing_snapshot = 0
        one_sided_orderbook = 0
        market_quote_rejected = 0
        matched_trade_rejected = 0
        evaluation_not_found = 0
        rejection_reasons: dict[str, int] = {}

        def _save(quote: _PersistableKalshiBaseline) -> None:
            nonlocal replayed, matched_trade_captured, market_quote_captured
            was_replay = self._repository.save_quote(quote=quote)
            quote_ids.append(quote.quote_id)
            if was_replay:
                replayed += 1
            elif isinstance(quote, KalshiBaselineQuoteEvidence):
                matched_trade_captured += 1
            else:
                market_quote_captured += 1

        def _record_rejection(error: Exception) -> None:
            reason = str(error)
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        # Kalshi Baseline Temporal Semantics & Sweep Isolation v1: every
        # ``build_...`` call below is pure, in-memory construction over
        # evidence already fetched (batched) above -- no I/O, no provider
        # request. A ``KalshiBaselineQuoteError``/``ValueValidationError``
        # raised from one of them is an expected, bounded rejection of THAT
        # ONE decision (a malformed or temporally-inconsistent referenced
        # evaluation/snapshot), never evidence of a broader infrastructure
        # problem -- so it is caught and counted right here, and the loop
        # moves on to the next decision, rather than the previous
        # behavior of letting it escape and silently abort every decision
        # still unprocessed this sweep (production-confirmed: 184 of 500
        # genuinely eligible decisions never got a chance, and 8 more were
        # never even reached). Deliberately narrow -- only these two named,
        # already-established domain exception types are caught; anything
        # else (a ``TypeError`` from a real bug, an unexpected exception
        # from ``self._repository.save_quote`` inside ``_save`` -- which is
        # NEVER inside one of these ``try`` blocks -- a real persistence
        # conflict, a dropped connection) still propagates immediately and
        # fails this task genuinely, exactly as it always did. This is
        # deliberately not a blanket ``except Exception``: only a rejection
        # this module itself defines as an expected, bounded business
        # outcome is ever downgraded to a count.
        for decision in decisions:
            projection = projections.get(decision.subject_projection_id)
            if projection is None:
                raise KalshiBaselineQuoteSyncError("KALSHI_BASELINE_QUOTE_PROJECTION_MISSING")

            if decision.subject_source_record_type == BINARY_COMPLEMENT_SOURCE_RECORD_TYPE:
                complement_found = binary_evidence.get(decision.subject_source_record_id)
                if complement_found is None:
                    evaluation_not_found += 1
                    continue
                complement_evaluation, fill_slices = complement_found
                if (
                    complement_evaluation.state is ComplementState.CANDIDATE
                    and complement_evaluation.matched_quantity > 0
                    and fill_slices
                ):
                    try:
                        matched_trade_quote = build_kalshi_baseline_quote_from_binary_complement(
                            decision=decision,
                            projection=projection,
                            evaluation=complement_evaluation,
                            fill_slices=fill_slices,
                            run_id=run_id,
                            captured_at=generated_at,
                        )
                    except (KalshiBaselineQuoteError, ValueValidationError) as error:
                        matched_trade_rejected += 1
                        _record_rejection(error)
                        continue
                    _save(matched_trade_quote)
                    continue
                # Honest, permanent "no matched-trade evidence" fact
                # (Phase 2): a NO_CANDIDATE/INELIGIBLE/INVALID evaluation has
                # no acquirable price. Never retried differently later --
                # the referenced evaluation is immutable, so this decision
                # will structurally never produce a matched-trade row. Fall
                # through to the plain market-quote baseline instead.
                no_candidate += 1
                snapshot = top_of_book.get(complement_evaluation.orderbook_snapshot_id)
                if snapshot is None:
                    missing_snapshot += 1
                    continue
                if not snapshot.is_two_sided:
                    one_sided_orderbook += 1
                    continue
                try:
                    market_quote = build_kalshi_market_quote_baseline_from_binary_complement(
                        decision=decision,
                        projection=projection,
                        evaluation=complement_evaluation,
                        snapshot=snapshot,
                        run_id=run_id,
                        captured_at=generated_at,
                    )
                except (KalshiBaselineQuoteError, ValueValidationError) as error:
                    market_quote_rejected += 1
                    _record_rejection(error)
                    continue
                _save(market_quote)
            elif decision.subject_source_record_type == THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE:
                bundle_found = threshold_evidence.get(decision.subject_source_record_id)
                if bundle_found is None:
                    evaluation_not_found += 1
                    continue
                bundle_evaluation, matched_slices, broader_ticker, narrower_ticker = bundle_found
                if (
                    bundle_evaluation.state is ThresholdEvaluationState.CANDIDATE
                    and bundle_evaluation.matched_quantity > 0
                    and matched_slices
                    and bundle_evaluation.broader_observed_at is not None
                    and bundle_evaluation.narrower_observed_at is not None
                ):
                    try:
                        matched_trade_quote = build_kalshi_baseline_quote_from_threshold_bundle(
                            decision=decision,
                            projection=projection,
                            evaluation=bundle_evaluation,
                            matched_slices=matched_slices,
                            broader_market_ticker=broader_ticker,
                            narrower_market_ticker=narrower_ticker,
                            run_id=run_id,
                            captured_at=generated_at,
                        )
                    except (KalshiBaselineQuoteError, ValueValidationError) as error:
                        matched_trade_rejected += 1
                        _record_rejection(error)
                        continue
                    _save(matched_trade_quote)
                    continue
                no_candidate += 1
                broader_id = bundle_evaluation.broader_orderbook_snapshot_id
                narrower_id = bundle_evaluation.narrower_orderbook_snapshot_id
                broader_snapshot = top_of_book.get(broader_id) if broader_id else None
                narrower_snapshot = top_of_book.get(narrower_id) if narrower_id else None
                if broader_snapshot is None or narrower_snapshot is None:
                    missing_snapshot += 1
                    continue
                if not broader_snapshot.is_two_sided or not narrower_snapshot.is_two_sided:
                    one_sided_orderbook += 1
                    continue
                try:
                    market_quote = build_kalshi_market_quote_baseline_from_threshold_bundle(
                        decision=decision,
                        projection=projection,
                        evaluation=bundle_evaluation,
                        broader_snapshot=broader_snapshot,
                        narrower_snapshot=narrower_snapshot,
                        run_id=run_id,
                        captured_at=generated_at,
                    )
                except (KalshiBaselineQuoteError, ValueValidationError) as error:
                    market_quote_rejected += 1
                    _record_rejection(error)
                    continue
                _save(market_quote)
            else:
                # ExhaustiveEventSetEvaluation (or any future evaluator
                # without a persisted, immutable per-decision row) -- see
                # domain.kalshi_baseline_quote's module docstring for
                # exactly why this stays unsupported rather than forced.
                unsupported_type += 1
                continue

        return SyncKalshiBaselineQuoteResult(
            run_id=run_id,
            generated_at=generated_at,
            considered_count=len(decisions),
            captured_count=matched_trade_captured + market_quote_captured,
            idempotent_replay_count=replayed,
            matched_trade_captured_count=matched_trade_captured,
            market_quote_captured_count=market_quote_captured,
            unsupported_source_record_type_count=unsupported_type,
            no_candidate_evaluation_count=no_candidate,
            insufficient_market_quote_evidence_count=(
                missing_snapshot + one_sided_orderbook + market_quote_rejected
            ),
            missing_snapshot_count=missing_snapshot,
            one_sided_orderbook_count=one_sided_orderbook,
            market_quote_validation_rejected_count=market_quote_rejected,
            matched_trade_validation_rejected_count=matched_trade_rejected,
            rejection_reason_counts=tuple(sorted(rejection_reasons.items())),
            source_evaluation_not_found_count=evaluation_not_found,
            quote_ids=tuple(quote_ids),
        )
