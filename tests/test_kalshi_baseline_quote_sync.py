"""Offline (no Postgres) proofs for ``SyncKalshiBaselineQuoteEvidence``'s
per-decision fault isolation: Kalshi Baseline Temporal Semantics & Sweep
Isolation v1.

Mirrors ``tests/test_counterfactual_followup.py``'s own established
in-memory-fake pattern (``_FakeDecisionSource``/``_FakeRepository``, etc.)
for the identical reason: the isolation/counting logic this module adds is
pure control flow over already-fetched, already-immutable evidence -- no
database required to prove it. Real end-to-end proof (real Postgres, real
evaluator/scan pipeline output) lives in
``tests/persistence/test_postgres_kalshi_baseline_quote.py``; these two
files intentionally exercise the identical contract from two different
angles (in-memory/Postgres parity), never a competing one.

Reuses ``tests/test_kalshi_baseline_quote.py``'s own vetted fixture
builders (``_binary_complement_evaluation_and_slices``/``_projection``/
``_decision_for``) for realistic, already-scanned ``BinaryComplement
Evaluation``/``ComplementFillSlice``/``SharedOpportunityProjection``/
``CounterfactualDecision`` objects rather than hand-rolling them a second
time -- an established cross-test-file idiom in this suite (see
``tests.test_counterfactual_followup`` imported the same way elsewhere).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping

import pytest

from apex.application.kalshi_baseline_quote import (
    KalshiBaselineQuoteSyncError,
    SyncKalshiBaselineQuoteEvidence,
)
from apex.domain.counterfactual_ledger import CounterfactualDecision
from apex.domain.kalshi_baseline_quote import (
    BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
    KalshiBaselineQuoteEvidence,
    KalshiMarketQuoteBaselineEvidence,
    KalshiOrderbookTopOfBook,
)
from apex.domain.public_complement import BinaryComplementEvaluation, ComplementFillSlice
from apex.domain.public_threshold import ThresholdBundleEvaluation, ThresholdMatchedSlice
from apex.domain.shared_opportunity import SharedOpportunityProjection
from tests.test_kalshi_baseline_quote import (
    NOW,
    _binary_complement_evaluation_and_slices,
    _decision_for,
    _projection,
)

_PersistableKalshiBaseline = KalshiBaselineQuoteEvidence | KalshiMarketQuoteBaselineEvidence


class _FakeSource:
    """Minimal, in-memory ``KalshiBaselineQuoteEvidenceSource`` -- every
    method is a bounded, already-batched dict lookup, exactly mirroring the
    real Postgres adapter's own contract (never a live/latest query)."""

    def __init__(
        self,
        *,
        decisions: tuple[CounterfactualDecision, ...],
        projections: Mapping[str, SharedOpportunityProjection],
        binary_evidence: Mapping[
            str, tuple[BinaryComplementEvaluation, tuple[ComplementFillSlice, ...]]
        ],
    ) -> None:
        self._decisions = decisions
        self._projections = projections
        self._binary_evidence = binary_evidence

    def pending_kalshi_decisions(self, *, limit: int) -> tuple[CounterfactualDecision, ...]:
        return self._decisions[:limit]

    def projections_by_id(
        self, projection_ids: Collection[str]
    ) -> Mapping[str, SharedOpportunityProjection]:
        return {pid: self._projections[pid] for pid in projection_ids if pid in self._projections}

    def binary_complement_evidence(
        self, evaluation_ids: tuple[str, ...]
    ) -> Mapping[str, tuple[BinaryComplementEvaluation, tuple[ComplementFillSlice, ...]]]:
        return {
            eid: self._binary_evidence[eid]
            for eid in evaluation_ids
            if eid in self._binary_evidence
        }

    def threshold_bundle_evidence(
        self, evaluation_ids: tuple[str, ...]
    ) -> Mapping[
        str, tuple[ThresholdBundleEvaluation, tuple[ThresholdMatchedSlice, ...], str, str]
    ]:
        return {}

    def orderbook_top_of_book(
        self, snapshot_ids: Collection[str]
    ) -> Mapping[str, KalshiOrderbookTopOfBook]:
        return {}


class _FakeRepository:
    """Mirrors ``PostgresKalshiBaselineQuoteRepository.save_quote``'s own
    idempotent-replay contract (mirrors ``tests.test_counterfactual_followup
    ._FakeRepository`` exactly): identical id+hash replays ``True`` and
    writes nothing; a conflicting payload under the same id raises."""

    def __init__(self) -> None:
        self.rows: dict[str, _PersistableKalshiBaseline] = {}

    def save_quote(self, *, quote: _PersistableKalshiBaseline) -> bool:
        existing = self.rows.get(quote.quote_id)
        if existing is not None:
            if existing.canonical_hash != quote.canonical_hash:
                raise KalshiBaselineQuoteSyncError("FAKE_REPOSITORY_CONFLICT")
            return True
        self.rows[quote.quote_id] = quote
        return False


class _InfraFailingRepository:
    """Simulates a genuine, unexpected persistence-layer fault (a dropped
    connection, a real constraint violation type this module never defines
    as an expected rejection) -- deliberately NOT one of the two domain
    exception types ``SyncKalshiBaselineQuoteEvidence.execute`` catches, so
    it must propagate rather than being counted as "one more rejection"."""

    def save_quote(self, *, quote: _PersistableKalshiBaseline) -> bool:
        raise RuntimeError("SIMULATED_INFRA_FAILURE")


def _valid_decision_projection_evaluation():
    evaluation, slices = _binary_complement_evaluation_and_slices()
    projection = _projection(
        source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        source_record_id=evaluation.evaluation_id,
        source_canonical_hash=str(evaluation.canonical_input_hash),
        instrument_id=evaluation.provider_market_ticker,
        product_or_market_id=evaluation.provider_market_ticker,
    )
    decision = _decision_for(projection)
    return decision, projection, evaluation, slices


def _source_with_one_bad_and_one_good_decision() -> tuple[_FakeSource, str, str]:
    """One genuinely valid CANDIDATE decision, and one decision whose own
    recorded projection linkage is internally inconsistent (a
    ``KALSHI_BASELINE_QUOTE_DECISION_PROJECTION_SOURCE_MISMATCH`` --
    exactly the class of rejection ``_validate_decision_projection_linkage``
    already guards, restated here as a deliberately malformed input rather
    than a temporal one, to prove isolation is not specific to the temporal
    fix). The bad decision is placed FIRST, mirroring the exact production
    shape (Kalshi Baseline Temporal Semantics & Sweep Isolation v1's own
    root-cause finding: the first rejection used to abort every decision
    still unprocessed)."""

    good_decision, good_projection, evaluation, slices = _valid_decision_projection_evaluation()

    bad_projection = dataclasses.replace(
        good_projection,
        projection_id="kbq-sync-bad-projection",
        source_record_id="kbq-sync-unrelated-record-id",
    )
    bad_decision = dataclasses.replace(
        good_decision,
        decision_id="kbq-sync-bad-decision",
        canonical_hash="kbq-sync-bad-decision-hash",
        subject_projection_id=bad_projection.projection_id,
    )

    source = _FakeSource(
        decisions=(bad_decision, good_decision),
        projections={
            good_projection.projection_id: good_projection,
            bad_projection.projection_id: bad_projection,
        },
        binary_evidence={evaluation.evaluation_id: (evaluation, slices)},
    )
    return source, bad_decision.decision_id, good_decision.decision_id


def test_one_invalid_decision_does_not_block_a_later_valid_one() -> None:
    source, bad_id, good_id = _source_with_one_bad_and_one_good_decision()
    repository = _FakeRepository()

    result = SyncKalshiBaselineQuoteEvidence(source, repository).execute(
        run_id="sync-isolation-run", generated_at=NOW
    )

    assert result.considered_count == 2
    assert result.matched_trade_captured_count == 1
    assert result.matched_trade_validation_rejected_count == 1
    assert result.captured_count == 1
    assert len(result.rejection_reason_counts) == 1
    reason, count = result.rejection_reason_counts[0]
    assert "DECISION_PROJECTION_SOURCE_MISMATCH" in reason
    assert count == 1
    # The valid decision genuinely persisted -- not merely counted.
    assert len(repository.rows) == 1
    persisted = next(iter(repository.rows.values()))
    assert persisted.decision_id == good_id
    assert bad_id not in {row.decision_id for row in repository.rows.values()}


def test_multiple_validation_failures_stay_bounded_and_do_not_abort_the_sweep() -> None:
    """Two independently malformed decisions, in front of and behind one
    genuinely valid decision, all in the same sweep -- the loop must reach
    every decision regardless of position."""

    good_decision, good_projection, evaluation, slices = _valid_decision_projection_evaluation()

    def _bad(identity: str) -> CounterfactualDecision:
        bad_projection = dataclasses.replace(
            good_projection,
            projection_id=f"kbq-sync-bad-projection-{identity}",
            source_record_id=f"kbq-sync-unrelated-record-id-{identity}",
        )
        return dataclasses.replace(
            good_decision,
            decision_id=f"kbq-sync-bad-decision-{identity}",
            canonical_hash=f"kbq-sync-bad-decision-hash-{identity}",
            subject_projection_id=bad_projection.projection_id,
        ), bad_projection

    bad_one, bad_one_projection = _bad("one")
    bad_two, bad_two_projection = _bad("two")

    source = _FakeSource(
        decisions=(bad_one, good_decision, bad_two),
        projections={
            good_projection.projection_id: good_projection,
            bad_one_projection.projection_id: bad_one_projection,
            bad_two_projection.projection_id: bad_two_projection,
        },
        binary_evidence={evaluation.evaluation_id: (evaluation, slices)},
    )
    repository = _FakeRepository()

    result = SyncKalshiBaselineQuoteEvidence(source, repository).execute(
        run_id="sync-isolation-multi-run", generated_at=NOW
    )

    assert result.considered_count == 3
    assert result.matched_trade_captured_count == 1
    assert result.matched_trade_validation_rejected_count == 2
    # Both rejections share the identical reason code (the same mismatch
    # class), so they collapse into one bounded entry with count 2 --
    # proving the breakdown is genuinely by reason, not by raw occurrence.
    assert result.rejection_reason_counts == (
        ("KALSHI_BASELINE_QUOTE_DECISION_PROJECTION_SOURCE_MISMATCH", 2),
    )
    assert len(repository.rows) == 1


def test_unexpected_repository_failure_propagates_as_a_genuine_task_failure() -> None:
    """Section 6's hard requirement, restated as a test: a real
    infrastructure fault during persistence is never silently downgraded to
    "one more rejected decision" -- it must surface as a genuine failure."""

    decision, projection, evaluation, slices = _valid_decision_projection_evaluation()
    source = _FakeSource(
        decisions=(decision,),
        projections={projection.projection_id: projection},
        binary_evidence={evaluation.evaluation_id: (evaluation, slices)},
    )

    with pytest.raises(RuntimeError, match="SIMULATED_INFRA_FAILURE"):
        SyncKalshiBaselineQuoteEvidence(source, _InfraFailingRepository()).execute(
            run_id="sync-infra-failure-run", generated_at=NOW
        )


def test_referential_integrity_failure_still_propagates_not_counted() -> None:
    """A decision whose ``subject_projection_id`` resolves to no retained
    projection at all is a genuine referential-integrity violation (the
    decision could only ever have been built by resolving that exact
    projection) -- distinct from an expected, bounded validation rejection,
    and must keep failing the task outright, never silently counted."""

    decision, _projection_obj, evaluation, slices = _valid_decision_projection_evaluation()
    orphaned = dataclasses.replace(decision, subject_projection_id="does-not-exist")
    source = _FakeSource(
        decisions=(orphaned,),
        projections={},
        binary_evidence={evaluation.evaluation_id: (evaluation, slices)},
    )

    with pytest.raises(
        KalshiBaselineQuoteSyncError, match="KALSHI_BASELINE_QUOTE_PROJECTION_MISSING"
    ):
        SyncKalshiBaselineQuoteEvidence(source, _FakeRepository()).execute(
            run_id="sync-orphan-run", generated_at=NOW
        )


def test_replaying_the_identical_sweep_is_idempotent_and_captures_no_duplicate() -> None:
    decision, projection, evaluation, slices = _valid_decision_projection_evaluation()
    source = _FakeSource(
        decisions=(decision,),
        projections={projection.projection_id: projection},
        binary_evidence={evaluation.evaluation_id: (evaluation, slices)},
    )
    repository = _FakeRepository()
    sync = SyncKalshiBaselineQuoteEvidence(source, repository)

    first = sync.execute(run_id="sync-replay-run-1", generated_at=NOW)
    assert first.matched_trade_captured_count == 1
    assert first.idempotent_replay_count == 0

    second = sync.execute(run_id="sync-replay-run-2", generated_at=NOW)
    assert second.matched_trade_captured_count == 0
    assert second.idempotent_replay_count == 1
    assert len(repository.rows) == 1
