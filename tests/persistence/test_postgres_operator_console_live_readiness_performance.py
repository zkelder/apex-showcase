"""PostgreSQL persistence proofs for Operator Console Live Readiness
Performance v1.

``GET /api/v1/operator-console/live-readiness`` and
``GET /api/v1/operator-console/live-preflight`` (``operator_api.py``) both
call ``application.live_preflight.live_preflight_workspace()`` against the
real, unwrapped ``live_preflight_repository``. That function's own
``active_intents`` computation calls ``repository.invalidations(
item.intent_id)`` once per retained ``LiveIntentProposal``, and
``PostgresLivePreflightRepository.invalidations()`` is a fully unfiltered
``SELECT * FROM live_intent_invalidation`` (no SQL ``WHERE`` clause) that
re-fetches and re-deserializes every retained invalidation on every call,
then filters in Python -- an intents() x invalidations() N+1, worse in that
one respect than the Operator Console Follow-Up/Home Performance v1 defects
(those at least filtered in SQL). Measured directly (cProfile + call-count
instrumentation against a seeded corpus, never the retained ``apex``
database): 0.02s at today's real 0-intent/0-invalidation production shape,
1.43s at 200 intents/20 invalidations, 6.90s at 500 intents/50 invalidations
(already past this milestone's 5s hard ceiling) -- confirming the
O(intents x invalidations) shape.

The fix adds one small, request-scoped wrapper in ``operator_api.py``
(``_MemoizedLivePreflightInvalidations``) plus one additive, batched read
(``PostgresLivePreflightRepository.invalidations_by_intent_id()`` -- the
identical underlying query ``invalidations()`` itself issues, just once).
Both are purely additive: ``live_preflight_workspace``,
``build_live_preflight_operating_tasks``, ``invalidations()``, and every
mutation service in ``application.live_preflight`` receive zero line
changes.

These tests exercise the real, unmodified ``live_preflight_workspace()``
throughout -- only the repository object fed into it differs -- and prove:

  A. the wrapped construction never calls the real, underlying
     invalidation-table read more than once per instance, regardless of how
     many intents/invalidations exist (a deterministic call-COUNT bound,
     never a flaky wall-clock one), and the old per-intent
     ``invalidations()`` method is never called at all through the wrapper;
  B. wrapped and unwrapped dependency construction produce byte-identical
     ``live_preflight_workspace()`` output -- including the one
     safety-critical field genuinely derived from invalidation state, the
     ``LIVE_INTENT_ELIGIBILITY_REVIEW`` operating-cycle task's disposition
     and evidence_ids -- across multiple intents, multiple invalidations
     (including more than one invalidation for the same intent), historical
     vs. near-``as_of`` invalidations, zero invalidations, mixed
     CRYPTO/KALSHI venues, and mixed active/expired/invalidated intent
     states;
  C. execution-authority/kill-switch-adjacent constant fields (``authority``,
     ``live_authority``, ``provider_write_count``) remain exactly
     ``"NONE"``/``0`` regardless of invalidation state;
  D. stale-preflight detection (an unrelated operating-cycle task fed from
     the same workspace) is unaffected by the invalidation wrapper;
  E. nothing this milestone added ever mutates retained evidence.

A genuine multi-hundred-intent reproduction is not something a persistence
test should reseed (far too slow, and the real retained ``apex`` database
has zero intents today regardless -- no Shared Opportunity has ever reached
genuine LIVE eligibility) -- that scale evidence lives in the Operator
Console Live Readiness Performance v1 investigation itself (a bounded
seeded-corpus reproduction script, documented in that milestone's final
report).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.live_preflight_acceptance import ACCEPTANCE_AT, _run_internal
from apex.adapters.live_preflight_postgres import (
    LIVE_PREFLIGHT_TABLES,
    PostgresLivePreflightRepository,
)
from apex.application.live_preflight import (
    InvalidateLiveIntent,
    LivePreflightRepository,
    live_preflight_workspace,
)
from apex.domain.live_preflight import LiveIntentProposal, LiveOrderType
from apex.domain.live_readiness import Provider
from apex.operator_api import _MemoizedLivePreflightInvalidations

pytestmark = pytest.mark.postgres

# Inside the acceptance fixture's own 1-hour intent expiry window (see
# live_preflight_acceptance._repository_with_intent: expires_at =
# ACCEPTANCE_AT + timedelta(hours=1)) -- so a seeded intent is ACTIVE at
# AS_OF unless explicitly invalidated, isolating invalidation-driven
# exclusion from expiry-driven exclusion in the tests below that are not
# specifically about expiry.
AS_OF = ACCEPTANCE_AT + timedelta(minutes=50)


def _seed_intent(
    repository: PostgresLivePreflightRepository, *, venue: str = "CRYPTO", suffix: str
) -> LiveIntentProposal:
    """Builds one real, immutable ``LiveIntentProposal`` via the real
    ``ConstructLiveIntent`` application service (against a throwaway
    in-memory repository -- mirroring
    ``tests/persistence/test_postgres_live_preflight.py``'s own
    ``_persist_complete_fixture`` pattern exactly), then persists it into
    the real, retained Postgres repository this test uses."""

    _, intent, _ = _run_internal(venue=venue, suffix=suffix)
    return repository.save_intent(intent)


def _seed_intent_with_preflight(
    repository: PostgresLivePreflightRepository, *, venue: str = "CRYPTO", suffix: str
) -> LiveIntentProposal:
    """As ``_seed_intent``, but also persists the intent's real eligibility
    decision and internal preflight (needed only by the stale-preflight
    test below; the other tests only need the intent itself, matching
    ``live_preflight_workspace``'s own minimal dependency on each field)."""

    source, intent, preflight = _run_internal(venue=venue, suffix=suffix)
    eligibility = source.eligibility(preflight.eligibility_decision_id)
    assert eligibility is not None
    repository.save_intent(intent)
    repository.save_eligibility(eligibility)
    repository.save_preflight(preflight)
    return intent


def _invalidate(
    repository: PostgresLivePreflightRepository,
    intent: LiveIntentProposal,
    *,
    invalidated_at: datetime,
    reason: str = "TEST_INVALIDATION",
) -> None:
    InvalidateLiveIntent(repository).execute(
        intent_id=intent.intent_id,
        invalidated_at=invalidated_at,
        reason=reason,
        confirmation=f"INVALIDATE_LIVE_INTENT:{intent.intent_id}",
    )


def _review_task(workspace: dict[str, object]):
    tasks = workspace["operating_cycle_tasks"]
    return next(t for t in tasks if t.task_type == "LIVE_INTENT_ELIGIBILITY_REVIEW")  # type: ignore[union-attr]


def _stale_task(workspace: dict[str, object]):
    tasks = workspace["operating_cycle_tasks"]
    return next(t for t in tasks if t.task_type == "STALE_PREFLIGHT_DETECTION")  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# B: wrapped and unwrapped construction are byte-identical.
# --------------------------------------------------------------------------


def test_wrapped_matches_unwrapped_with_multiple_intents_and_invalidations(
    postgres_engine: Engine,
) -> None:
    """Seeds a genuinely mixed backlog: several CRYPTO intents and a KALSHI
    intent, one intent invalidated twice (a real "multiple invalidations
    for one intent" edge case), one intent with an invalidation unrelated
    to any other intent, one left with zero invalidations, and asserts the
    wrapped-dependency ``live_preflight_workspace`` reports the *exact*
    same output (full dict equality) as the real, unwrapped repository.
    This is the master proof that the performance fix changed nothing
    observable."""

    engine = postgres_engine
    real_repository = PostgresLivePreflightRepository(engine)

    never_invalidated = _seed_intent(real_repository, suffix="never-invalidated")
    invalidated_once = _seed_intent(real_repository, suffix="invalidated-once")
    invalidated_twice = _seed_intent(real_repository, suffix="invalidated-twice")
    kalshi_active = _seed_intent(real_repository, venue="KALSHI", suffix="kalshi-active")
    kalshi_invalidated = _seed_intent(real_repository, venue="KALSHI", suffix="kalshi-invalidated")

    _invalidate(
        real_repository, invalidated_once, invalidated_at=ACCEPTANCE_AT + timedelta(minutes=5)
    )
    _invalidate(
        real_repository, invalidated_twice, invalidated_at=ACCEPTANCE_AT + timedelta(minutes=6)
    )
    _invalidate(
        real_repository,
        invalidated_twice,
        invalidated_at=AS_OF - timedelta(minutes=30),
        reason="SECOND_TEST_INVALIDATION",
    )
    _invalidate(
        real_repository, kalshi_invalidated, invalidated_at=ACCEPTANCE_AT + timedelta(minutes=5)
    )

    unwrapped: LivePreflightRepository = PostgresLivePreflightRepository(engine)
    wrapped: LivePreflightRepository = _MemoizedLivePreflightInvalidations(real_repository)

    unwrapped_workspace = live_preflight_workspace(unwrapped, as_of=AS_OF)
    wrapped_workspace = live_preflight_workspace(wrapped, as_of=AS_OF)

    assert wrapped_workspace == unwrapped_workspace
    assert len(wrapped_workspace["intents"]) == 5  # type: ignore[arg-type]

    review = _review_task(wrapped_workspace)
    assert review.disposition == "REVIEW_REQUIRED"
    assert set(review.evidence_ids) == {never_invalidated.intent_id, kalshi_active.intent_id}
    assert invalidated_once.intent_id not in review.evidence_ids
    assert invalidated_twice.intent_id not in review.evidence_ids
    assert kalshi_invalidated.intent_id not in review.evidence_ids

    # Multiple invalidations for one intent are retained (both rows exist)
    # but the review task's boolean exclusion is unaffected by the count.
    assert len(real_repository.invalidations(invalidated_twice.intent_id)) == 2


def test_historical_and_current_invalidation_both_exclude_the_intent(
    postgres_engine: Engine,
) -> None:
    """An invalidation timestamped long before ``as_of`` ("historical") and
    one timestamped only moments before ``as_of`` ("current") must both
    exclude their intent identically -- ``active_intents`` is a pure
    existence check with no ``as_of``-relative filtering in the real,
    unmodified ``live_preflight_workspace``, and the wrapper must not
    introduce any."""

    engine = postgres_engine
    real_repository = PostgresLivePreflightRepository(engine)
    historical = _seed_intent(real_repository, suffix="historical-invalidation")
    current = _seed_intent(real_repository, suffix="current-invalidation")

    _invalidate(real_repository, historical, invalidated_at=ACCEPTANCE_AT + timedelta(minutes=5))
    _invalidate(real_repository, current, invalidated_at=AS_OF - timedelta(seconds=1))

    wrapped = _MemoizedLivePreflightInvalidations(real_repository)
    workspace = live_preflight_workspace(wrapped, as_of=AS_OF)
    review = _review_task(workspace)

    assert historical.intent_id not in review.evidence_ids
    assert current.intent_id not in review.evidence_ids
    assert review.disposition == "ZERO_INTENT"

    unwrapped_workspace = live_preflight_workspace(
        PostgresLivePreflightRepository(engine), as_of=AS_OF
    )
    assert workspace == unwrapped_workspace


def _make_intent_with_expiry(*, suffix: str, expires_at: datetime) -> LiveIntentProposal:
    """Directly constructs a real, fully self-consistent (hash included)
    ``LiveIntentProposal`` with an explicit, short ``expires_at`` --
    ``_run_internal``/``_repository_with_intent`` always use the fixture's
    fixed 1-hour expiry, so this is the only way to get an intent that is
    genuinely EXPIRED at a read time when a sibling intent (built the
    normal way) is not. Zero cross-referential lineage is checked by
    ``LiveIntentProposal.create()`` itself (only by ``ConstructLiveIntent``,
    exercised elsewhere) -- this test is about ``status_at()``/invalidation
    interaction, not construction-time lineage validation, so dummy but
    well-formed 64-hex-character lineage hashes are sufficient."""

    hash64 = "b" * 64
    return LiveIntentProposal.create(
        intent_id=f"fixture-live-intent-{suffix}",
        source_opportunity_id=f"fixture-opportunity-{suffix}",
        source_opportunity_hash=hash64,
        shared_projection_id=f"fixture-projection-{suffix}",
        shared_projection_hash=hash64,
        allocation_decision_id=f"fixture-allocation-{suffix}",
        allocation_decision_hash=hash64,
        allocation_line_id=f"fixture-line-{suffix}",
        allocation_amount="10.00",
        paper_trial_proposal_id=f"fixture-trial-{suffix}",
        paper_trial_proposal_hash=hash64,
        provider=Provider.COINBASE,
        account_scope_id="fixture-crypto-live-account",
        product_or_market_id="BTC-USD",
        side="BUY",
        order_type=LiveOrderType.LIMIT,
        requested_quantity="0.01",
        requested_notional=None,
        limit_price="10.00",
        maximum_risk="2",
        strategy_or_method_version="TEST:V1",
        opportunity_observed_at=ACCEPTANCE_AT,
        allocation_decided_at=ACCEPTANCE_AT,
        capability_observed_at=ACCEPTANCE_AT,
        created_at=ACCEPTANCE_AT,
        expires_at=expires_at,
    )


def test_expired_and_invalidated_intents_are_excluded_independently(
    postgres_engine: Engine,
) -> None:
    """Four intents, one per combination of {invalidated, not invalidated}
    x {expired as of the read, not expired}: only the intent that is
    neither invalidated nor expired may appear as active. Proves the two
    independent exclusion paths (the wrapper's invalidation filter, and
    ``LiveIntentProposal.status_at()``'s own expiry check inside
    ``build_live_preflight_operating_tasks``) do not interfere with each
    other through the wrapper."""

    engine = postgres_engine
    real_repository = PostgresLivePreflightRepository(engine)

    # live_active/live_invalidated use the fixture's normal 1-hour expiry;
    # expired_only/expired_and_invalidated get a deliberately short one, so
    # a single read_at can distinguish "not expired" from "expired".
    short_expiry = ACCEPTANCE_AT + timedelta(minutes=5)
    live_active = _seed_intent(real_repository, suffix="expiry-live-active")
    live_invalidated = _seed_intent(real_repository, suffix="expiry-live-invalidated")
    expired_only = real_repository.save_intent(
        _make_intent_with_expiry(suffix="expiry-expired-only", expires_at=short_expiry)
    )
    expired_and_invalidated = real_repository.save_intent(
        _make_intent_with_expiry(suffix="expiry-expired-invalidated", expires_at=short_expiry)
    )

    _invalidate(
        real_repository, live_invalidated, invalidated_at=ACCEPTANCE_AT + timedelta(minutes=2)
    )
    _invalidate(
        real_repository,
        expired_and_invalidated,
        invalidated_at=ACCEPTANCE_AT + timedelta(minutes=2),
    )

    # Strictly between the short 5-minute expiry and the fixture's normal
    # 1-hour expiry: expired_only/expired_and_invalidated are EXPIRED here;
    # live_active/live_invalidated are not.
    read_at = ACCEPTANCE_AT + timedelta(minutes=10)

    wrapped_workspace = live_preflight_workspace(
        _MemoizedLivePreflightInvalidations(real_repository), as_of=read_at
    )
    unwrapped_workspace = live_preflight_workspace(
        PostgresLivePreflightRepository(engine), as_of=read_at
    )
    assert wrapped_workspace == unwrapped_workspace

    review = _review_task(wrapped_workspace)
    assert set(review.evidence_ids) == {live_active.intent_id}
    for excluded in (live_invalidated, expired_only, expired_and_invalidated):
        assert excluded.intent_id not in review.evidence_ids


# --------------------------------------------------------------------------
# A: deterministic, non-flaky call-count bound (never a wall-clock assertion).
# --------------------------------------------------------------------------


def test_memoized_invalidations_fetches_the_real_repository_at_most_once(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With several intents, some invalidated, the real per-intent
    ``invalidations()`` (before this fix: called once per intent, each an
    unfiltered full-table scan) must be called zero times through the
    wrapper, and the new batched ``invalidations_by_intent_id()`` must be
    called at most once regardless of intent count."""

    engine = postgres_engine
    real_repository = PostgresLivePreflightRepository(engine)
    intent_count = 12
    intents = [
        _seed_intent(real_repository, suffix=f"callcount{index}") for index in range(intent_count)
    ]
    for intent in intents[::3]:
        _invalidate(real_repository, intent, invalidated_at=AS_OF - timedelta(minutes=1))

    per_intent_calls = 0
    real_invalidations = PostgresLivePreflightRepository.invalidations

    def counting_invalidations(self: PostgresLivePreflightRepository, intent_id: str):
        nonlocal per_intent_calls
        per_intent_calls += 1
        return real_invalidations(self, intent_id)

    batched_calls = 0
    real_batched = PostgresLivePreflightRepository.invalidations_by_intent_id

    def counting_batched(self: PostgresLivePreflightRepository):
        nonlocal batched_calls
        batched_calls += 1
        return real_batched(self)

    monkeypatch.setattr(PostgresLivePreflightRepository, "invalidations", counting_invalidations)
    monkeypatch.setattr(
        PostgresLivePreflightRepository, "invalidations_by_intent_id", counting_batched
    )

    wrapped = _MemoizedLivePreflightInvalidations(real_repository)
    workspace = live_preflight_workspace(wrapped, as_of=AS_OF)

    assert len(workspace["intents"]) == intent_count  # type: ignore[arg-type]
    assert per_intent_calls == 0, (
        f"expected the wrapper to never call the real per-intent invalidations(); "
        f"observed {per_intent_calls} calls"
    )
    assert batched_calls <= 1, (
        f"expected the batched read at most once regardless of {intent_count} intents; "
        f"observed {batched_calls} calls"
    )


# --------------------------------------------------------------------------
# C: execution-authority/kill-switch-adjacent constants are unaffected.
# --------------------------------------------------------------------------


def test_authority_and_kill_switch_adjacent_fields_stay_none_regardless_of_state(
    postgres_engine: Engine,
) -> None:
    engine = postgres_engine
    real_repository = PostgresLivePreflightRepository(engine)
    active = _seed_intent(real_repository, suffix="authority-active")
    invalidated = _seed_intent(real_repository, suffix="authority-invalidated")
    _invalidate(real_repository, invalidated, invalidated_at=AS_OF - timedelta(minutes=1))

    for repository in (
        PostgresLivePreflightRepository(engine),
        _MemoizedLivePreflightInvalidations(real_repository),
    ):
        workspace = live_preflight_workspace(repository, as_of=AS_OF)
        assert workspace["authority"] == "NONE"
        assert workspace["live_authority"] == "NONE"
        assert workspace["provider_write_count"] == 0
        assert workspace["provider_request_count"] == 0
        assert "SUBMIT" in workspace["prohibited_actions"]  # type: ignore[operator]
        assert "LIVE_PILOT_ARMED" in workspace["prohibited_actions"]  # type: ignore[operator]
    assert active.intent_id  # keeps the seeded fixture referenced/used


# --------------------------------------------------------------------------
# D: an unrelated operating-cycle task (stale-preflight detection) is
# unaffected by the invalidation wrapper.
# --------------------------------------------------------------------------


def test_stale_preflight_detection_unaffected_by_invalidation_wrapper(
    postgres_engine: Engine,
) -> None:
    engine = postgres_engine
    real_repository = PostgresLivePreflightRepository(engine)

    # This intent's preflight uses the fixture default evaluated_at
    # (ACCEPTANCE_AT + 2 minutes); reading 30 minutes after ACCEPTANCE_AT
    # makes it >900s old, i.e. genuinely STALE.
    _seed_intent_with_preflight(real_repository, suffix="stale-preflight")
    # An unrelated invalidated intent must not affect the stale-preflight
    # task at all -- it reads from `preflights`, not `active_intents`.
    invalidated_intent = _seed_intent(real_repository, suffix="stale-unrelated-invalidated")
    _invalidate(
        real_repository, invalidated_intent, invalidated_at=ACCEPTANCE_AT + timedelta(minutes=5)
    )

    read_at = ACCEPTANCE_AT + timedelta(minutes=30)
    wrapped_workspace = live_preflight_workspace(
        _MemoizedLivePreflightInvalidations(real_repository), as_of=read_at
    )
    unwrapped_workspace = live_preflight_workspace(
        PostgresLivePreflightRepository(engine), as_of=read_at
    )
    assert wrapped_workspace == unwrapped_workspace

    stale = _stale_task(wrapped_workspace)
    assert stale.disposition == "STALE"
    assert any(
        pid.startswith("fixture-internal-preflight-stale-preflight") for pid in stale.evidence_ids
    )


# --------------------------------------------------------------------------
# Correctness at zero/no-invalidation state.
# --------------------------------------------------------------------------


def test_wrapped_workspace_handles_zero_intents(postgres_engine: Engine) -> None:
    """Matches today's genuine retained-``apex``-database shape exactly:
    zero intents, zero invalidations."""

    engine = postgres_engine
    wrapped = _MemoizedLivePreflightInvalidations(PostgresLivePreflightRepository(engine))
    workspace = live_preflight_workspace(wrapped, as_of=AS_OF)
    assert workspace["intents"] == ()
    review = _review_task(workspace)
    assert review.disposition == "ZERO_INTENT"
    assert review.evidence_ids == ()


def test_wrapped_workspace_handles_intents_with_no_invalidations_at_all(
    postgres_engine: Engine,
) -> None:
    """Zero rows in ``live_intent_invalidation`` but several intents --
    the batched read must return an empty mapping cleanly, not error, and
    every intent must be treated as active."""

    engine = postgres_engine
    real_repository = PostgresLivePreflightRepository(engine)
    intents = [_seed_intent(real_repository, suffix=f"noinval{index}") for index in range(5)]

    wrapped = _MemoizedLivePreflightInvalidations(real_repository)
    workspace = live_preflight_workspace(wrapped, as_of=AS_OF)
    review = _review_task(workspace)
    assert review.disposition == "REVIEW_REQUIRED"
    assert set(review.evidence_ids) == {item.intent_id for item in intents}
    assert real_repository.invalidations_by_intent_id() == {}


# --------------------------------------------------------------------------
# E: nothing this milestone's new read path can mutate retained evidence.
# --------------------------------------------------------------------------


def test_no_write_reachable_through_the_new_read_model_wrapper(postgres_engine: Engine) -> None:
    engine = postgres_engine
    real_repository = PostgresLivePreflightRepository(engine)
    active = _seed_intent(real_repository, suffix="writecheck-active")
    invalidated = _seed_intent(real_repository, suffix="writecheck-invalidated")
    _invalidate(real_repository, invalidated, invalidated_at=AS_OF - timedelta(minutes=1))

    with engine.connect() as connection:
        before = {
            table: connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()
            for table in LIVE_PREFLIGHT_TABLES
        }

    wrapped = _MemoizedLivePreflightInvalidations(real_repository)
    workspace = live_preflight_workspace(wrapped, as_of=AS_OF)
    # Exercise the memoized path twice more (as both routes would within
    # one request cycle, and as repeated GETs would across requests) to
    # prove repeated reads stay writeless too.
    live_preflight_workspace(wrapped, as_of=AS_OF)
    live_preflight_workspace(_MemoizedLivePreflightInvalidations(real_repository), as_of=AS_OF)

    assert active.intent_id in {item.intent_id for item in workspace["intents"]}  # type: ignore[union-attr]

    with engine.connect() as connection:
        after = {
            table: connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()
            for table in LIVE_PREFLIGHT_TABLES
        }
    assert after == before
