"""Migration Compatibility & Deployment Guard v1: the one adapter that reads
``alembic_version`` and enforces :mod:`apex.domain.schema_compatibility`'s
exact-revision contract before an unattended worker begins task dispatch.

Both ``apex-operating-cycle-worker`` and ``apex-live-observer-worker`` call
:func:`require_compatible_schema` as the literal first action inside their
CLI entrypoints (``apex.cli._run_supervised_operating_cycle_worker`` /
``_run_supervised_live_observer_worker``), before either constructs a
repository, claims a runner lease, or reads a plan. They share one exact
contract rather than each declaring its own minimum revision: both ship
from the identical checkout with no independent versioning, so a per-worker
compatibility matrix would only add maintenance surface (a table nobody
updates when a new task type gains a new table dependency) without ever
being exercised -- see ``apex.domain.schema_compatibility``'s module
docstring for the full reasoning and the real incident this closes.

Read-only: this module never issues DDL, never calls ``alembic upgrade``,
and never imports ``alembic`` at all -- ``alembic_version`` is read with one
plain ``SELECT``, exactly like
``scripts/dev.py::_schema_preflight_disposable_demo_database`` already
does for the disposable-demo-database lifecycle. A genuine connection
failure (wrong host/port, Postgres down) is distinguished from an
indeterminate-but-reachable schema state: the former raises
``apex.adapters.postgres.PostgresUnavailableError`` (the existing,
credential-free connectivity error every other command already raises),
the latter raises :class:`~apex.domain.schema_compatibility.SchemaCompatibilityError`
with :attr:`~apex.domain.schema_compatibility.SchemaCompatibilityStatus.SCHEMA_REVISION_UNKNOWN`.
Neither swallows any other exception -- a genuine query defect inside a
task that runs *after* this guard passes surfaces exactly as it always has.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from apex.adapters.postgres import PostgresUnavailableError
from apex.domain.schema_compatibility import (
    REQUIRED_SCHEMA_REVISION,
    SchemaCompatibilityError,
    SchemaCompatibilityReport,
    evaluate_schema_compatibility,
)

_ALEMBIC_VERSION_TABLE = "alembic_version"


def fetch_observed_schema_revisions(engine: Engine) -> tuple[str, ...]:
    """The raw ``version_num`` row(s) currently in ``alembic_version``, or
    an empty tuple when that table does not exist yet (a fresh or never-
    migrated database). Exactly one row is the normal case -- Apex's
    migration chain is enforced single-head (see
    ``scripts/dev.py::_disposable_demo_migration_heads``) -- so more than
    one row only ever reflects a genuinely corrupt or mid-migration state.

    Raises :class:`~apex.adapters.postgres.PostgresUnavailableError` if the
    database itself cannot be reached at all; that failure is never
    reinterpreted as a schema-compatibility question."""

    try:
        with engine.connect() as connection:
            table_names = frozenset(sa.inspect(connection).get_table_names())
            if _ALEMBIC_VERSION_TABLE not in table_names:
                return ()
            rows = connection.scalars(sa.text(f"SELECT version_num FROM {_ALEMBIC_VERSION_TABLE}"))
            return tuple(str(value) for value in rows)
    except DBAPIError as error:
        raise PostgresUnavailableError(
            "local PostgreSQL is unavailable for the schema-compatibility check"
        ) from error


def require_compatible_schema(engine: Engine, *, worker_name: str) -> SchemaCompatibilityReport:
    """The one shared startup guard. Call this before any repository read,
    runner-lease claim, or task dispatch -- it performs no schema mutation
    and its only DDL-adjacent read is the single ``SELECT`` in
    :func:`fetch_observed_schema_revisions`.

    Raises :class:`~apex.domain.schema_compatibility.SchemaCompatibilityError`
    (report attached as ``.report``) when the observed schema does not
    exactly match :data:`~apex.domain.schema_compatibility.REQUIRED_SCHEMA_REVISION`;
    returns the ``COMPATIBLE`` report otherwise. Raises
    :class:`~apex.adapters.postgres.PostgresUnavailableError` instead when
    the database cannot be reached at all."""

    observed = fetch_observed_schema_revisions(engine)
    report = evaluate_schema_compatibility(
        required_revision=REQUIRED_SCHEMA_REVISION,
        observed_revisions=observed,
        worker_name=worker_name,
    )
    if not report.compatible:
        raise SchemaCompatibilityError(report)
    return report
