"""Migration Compatibility & Deployment Guard v1: offline tests for
``apex.domain.schema_compatibility`` -- no Postgres required, so these run
as part of ``verify-core``/``test-offline``. See
``tests/persistence/test_postgres_schema_compatibility.py`` for the real-
database regression proving the actual 0110 incident (commit ``cce8f00``
live against a database still on migration ``0109``) cannot recur, and for
proof that neither unattended worker dispatches task work when
incompatible."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

from apex.domain.schema_compatibility import (
    REQUIRED_SCHEMA_REVISION,
    SchemaCompatibilityError,
    SchemaCompatibilityReport,
    SchemaCompatibilityStatus,
    evaluate_schema_compatibility,
)

ROOT = Path(__file__).resolve().parents[1]

_INCOMPATIBLE_REPORT = SchemaCompatibilityReport(
    status=SchemaCompatibilityStatus.SCHEMA_REVISION_BEHIND_APPLICATION,
    worker_name="fixture-worker",
    required_revision="0110_strategy_evaluation_history",
    observed_revision="0109_external_strategy_research",
    observed_revision_count=1,
    detail=(
        "SCHEMA_REVISION_BEHIND_APPLICATION worker=fixture-worker "
        "required=0110_strategy_evaluation_history "
        "observed=0109_external_strategy_research remediation='migrate'"
    ),
)


def test_compatible_when_observed_matches_required_exactly() -> None:
    report = evaluate_schema_compatibility(
        required_revision="0110_strategy_evaluation_history",
        observed_revisions=("0110_strategy_evaluation_history",),
        worker_name="operating-cycle-worker",
    )
    assert report.status is SchemaCompatibilityStatus.COMPATIBLE
    assert report.compatible
    assert report.observed_revision == "0110_strategy_evaluation_history"
    assert report.observed_revision_count == 1


def test_regression_reproduces_the_real_0110_incident_shape() -> None:
    """The literal incident: ``cce8f00`` (declares ``0110``) live against a
    database still at ``0109``. Must classify as BEHIND, never raise a
    database-shaped exception, and the resulting error message must name
    both revisions and the worker -- exactly what an operator needs from
    the journal, with no deep SQL traceback anywhere in this
    classification path."""

    report = evaluate_schema_compatibility(
        required_revision="0110_strategy_evaluation_history",
        observed_revisions=("0109_external_strategy_research",),
        worker_name="operating-cycle-worker",
    )
    assert report.status is SchemaCompatibilityStatus.SCHEMA_REVISION_BEHIND_APPLICATION
    assert not report.compatible
    message = str(SchemaCompatibilityError(report))
    assert "SCHEMA_REVISION_BEHIND_APPLICATION" in message
    assert "0110_strategy_evaluation_history" in message
    assert "0109_external_strategy_research" in message
    assert "operating-cycle-worker" in message
    assert "migrate" in message.lower()


def test_ahead_when_observed_revision_is_numerically_newer() -> None:
    report = evaluate_schema_compatibility(
        required_revision="0110_strategy_evaluation_history",
        observed_revisions=("0111_future_migration",),
        worker_name="live-observer-worker",
    )
    assert report.status is SchemaCompatibilityStatus.SCHEMA_REVISION_AHEAD_OF_APPLICATION
    message = str(SchemaCompatibilityError(report)).lower()
    assert "0111_future_migration" in message
    assert "deploy application code" in message


def test_unknown_when_no_revision_is_retained() -> None:
    report = evaluate_schema_compatibility(
        required_revision=REQUIRED_SCHEMA_REVISION,
        observed_revisions=(),
        worker_name="operating-cycle-worker",
    )
    assert report.status is SchemaCompatibilityStatus.SCHEMA_REVISION_UNKNOWN
    assert report.observed_revision is None
    assert report.observed_revision_count == 0


def test_unknown_when_multiple_revisions_are_simultaneously_retained() -> None:
    report = evaluate_schema_compatibility(
        required_revision=REQUIRED_SCHEMA_REVISION,
        observed_revisions=(
            "0109_external_strategy_research",
            "0110_strategy_evaluation_history",
        ),
        worker_name="operating-cycle-worker",
    )
    assert report.status is SchemaCompatibilityStatus.SCHEMA_REVISION_UNKNOWN
    assert report.observed_revision_count == 2


def test_unknown_when_observed_revision_does_not_follow_naming_convention() -> None:
    report = evaluate_schema_compatibility(
        required_revision=REQUIRED_SCHEMA_REVISION,
        observed_revisions=("not-a-real-revision",),
        worker_name="operating-cycle-worker",
    )
    assert report.status is SchemaCompatibilityStatus.SCHEMA_REVISION_UNKNOWN


def test_never_leaks_connection_details_in_any_report_detail() -> None:
    for observed in ((), ("0109_external_strategy_research",), ("0111_future_migration",)):
        report = evaluate_schema_compatibility(
            required_revision=REQUIRED_SCHEMA_REVISION,
            observed_revisions=observed,
            worker_name="operating-cycle-worker",
        )
        lowered = report.detail.lower()
        for leaked in ("password", "127.0.0.1", "postgresql://", "apex_local_only", "@"):
            assert leaked not in lowered


def test_required_schema_revision_matches_the_true_alembic_head() -> None:
    """The cheap mechanical guard for a migration-dependent commit that
    forgets to update the declared schema requirement: this fails here,
    offline, before any deploy -- rather than as a live crash loop like
    the real 0110 incident. No Postgres needed -- ``ScriptDirectory`` reads
    ``migrations/versions/`` from disk only, exactly like
    ``scripts/dev.py::_disposable_demo_migration_heads`` already does."""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads()
    assert len(heads) == 1, (
        f"Apex's migration chain is expected to stay single-head; found {len(heads)}: {heads!r}"
    )
    assert heads[0] == REQUIRED_SCHEMA_REVISION, (
        "apex.domain.schema_compatibility.REQUIRED_SCHEMA_REVISION "
        f"({REQUIRED_SCHEMA_REVISION!r}) no longer matches the true Alembic head "
        f"({heads[0]!r}) -- bump it in the same commit that adds a new migration."
    )


def test_recent_migration_chain_matches_numeric_prefix_order() -> None:
    """Validates the one assumption ``evaluate_schema_compatibility`` relies
    on to classify BEHIND vs. AHEAD without parsing ``down_revision`` links
    or importing ``alembic`` into the domain layer: that a revision id's
    zero-padded numeric prefix already reflects true chain order. Bounded
    to the recent chain (``0105``-``0110``, matching this milestone's own
    expand/contract review) rather than the entire history -- see
    ``docs/MIGRATION_COMPATIBILITY_GUARD_V1.md``."""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script_directory = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    recent = (
        "0105_securities_structure_perf",
        "0106_live_market_observation",
        "0107_kalshi_resolution_fact",
        "0108_kalshi_baseline_v2",
        "0109_external_strategy_research",
        "0110_strategy_evaluation_history",
    )
    for later, earlier in zip(recent[1:], recent[:-1], strict=True):
        revision = script_directory.get_revision(later)
        assert revision is not None
        assert revision.down_revision == earlier, (
            f"{later}'s down_revision is {revision.down_revision!r}, not {earlier!r} -- "
            "the numeric-prefix-order assumption in apex.domain.schema_compatibility "
            "no longer holds for this pair"
        )


# --- CLI wiring: offline proof of ordering, no Postgres required ---------
#
# These mirror tests/test_operating_cycle.py's existing
# test_operating_cycle_worker_rejects_unknown_plan_before_touching_executor,
# which proves the same kind of ordering property (a check runs, and
# raises, before the next statement touches its dependencies) for the
# pre-existing plan-existence check one statement after this guard. They
# live here rather than in tests/test_operating_cycle.py or
# tests/test_live_market_observation.py because they are about this
# guard's wiring into apex.cli, not about operating-cycle or live-market-
# observation domain behavior. See
# tests/persistence/test_postgres_schema_compatibility.py for the same
# proof against a real incompatible database.


def test_operating_cycle_worker_rejects_incompatible_schema_before_touching_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apex.cli import _run_supervised_operating_cycle_worker

    def _raise_incompatible(engine: object, *, worker_name: str) -> NoReturn:
        raise SchemaCompatibilityError(_INCOMPATIBLE_REPORT)

    monkeypatch.setattr(
        "apex.adapters.schema_compatibility_postgres.require_compatible_schema",
        _raise_incompatible,
    )

    class _ExplodingRepository:
        def plan(self, plan_id: str) -> object | None:
            raise AssertionError("repository.plan() must not run when schema is incompatible")

    with pytest.raises(SchemaCompatibilityError):
        _run_supervised_operating_cycle_worker(
            engine=None,
            repository=_ExplodingRepository(),
            source=None,
            plan_id="plan-fixture",
            runner_id="runner-fixture",
            run_instance_id="runner-fixture-instance-1",
            once=True,
            poll_seconds=150,
            max_cycles=1,
            authority={},
        )


def test_operating_cycle_worker_proceeds_past_a_compatible_schema_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror image of the test above: when the guard reports
    COMPATIBLE, execution reaches the next existing check
    (``repository.plan()``) exactly as it did before this milestone -- the
    guard must never block a genuinely matching schema."""

    from apex.cli import _run_supervised_operating_cycle_worker

    compatible_report = SchemaCompatibilityReport(
        status=SchemaCompatibilityStatus.COMPATIBLE,
        worker_name="operating-cycle-worker",
        required_revision=REQUIRED_SCHEMA_REVISION,
        observed_revision=REQUIRED_SCHEMA_REVISION,
        observed_revision_count=1,
        detail="COMPATIBLE",
    )
    monkeypatch.setattr(
        "apex.adapters.schema_compatibility_postgres.require_compatible_schema",
        lambda engine, *, worker_name: compatible_report,
    )

    class _NoPlanRepository:
        def plan(self, plan_id: str) -> object | None:
            return None

    with pytest.raises(ValueError, match="OPERATING_PLAN_NOT_FOUND"):
        _run_supervised_operating_cycle_worker(
            engine=None,
            repository=_NoPlanRepository(),
            source=None,
            plan_id="plan-fixture",
            runner_id="runner-fixture",
            run_instance_id="runner-fixture-instance-1",
            once=True,
            poll_seconds=150,
            max_cycles=1,
            authority={},
        )


def test_live_observer_worker_rejects_incompatible_schema_before_building_repositories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same ordering proof as the operating-cycle-worker test above, for
    ``apex.cli._run_supervised_live_observer_worker``. That function builds
    its own repositories directly from ``engine`` (no injectable
    repository parameter), so this asserts the raised exception type is
    exactly ``SchemaCompatibilityError`` -- not, say, an ``AttributeError``
    from later code touching the sentinel ``engine=None`` -- which is only
    possible if the guard genuinely runs before anything else."""

    from apex.cli import _run_supervised_live_observer_worker

    def _raise_incompatible(engine: object, *, worker_name: str) -> NoReturn:
        raise SchemaCompatibilityError(_INCOMPATIBLE_REPORT)

    monkeypatch.setattr(
        "apex.adapters.schema_compatibility_postgres.require_compatible_schema",
        _raise_incompatible,
    )

    with pytest.raises(SchemaCompatibilityError):
        _run_supervised_live_observer_worker(
            engine=None,
            plan_id="plan-fixture",
            runner_id="runner-fixture",
            run_instance_id="runner-fixture-instance-1",
            once=True,
            poll_seconds=150,
            max_cycles=1,
            authority={},
        )
