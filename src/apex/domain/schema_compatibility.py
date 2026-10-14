"""Migration Compatibility & Deployment Guard v1: pure classification rules
for whether the running application's declared schema requirement matches
the database schema revision an adapter has already observed.

This module owns zero I/O. It never opens a connection, never imports
``alembic``/``sqlalchemy``, and never reads ``migrations/versions`` itself --
an adapter (``apex.adapters.schema_compatibility_postgres``) reads the
database's ``alembic_version`` table and hands the raw observed revision
strings in here for classification. This keeps the actual compatibility
*rule* testable without Postgres, matching every other domain module in
this repository.

Real incident this exists to prevent: commit ``cce8f00`` (migration
``0110_strategy_evaluation_history``) was live application code while
production Postgres was still on migration ``0109_external_strategy_research``.
``apex-operating-cycle-worker`` then crashed every five minutes for roughly
four hours with a deep ``psycopg.errors.UndefinedTable`` on
``internal_strategy_evaluation`` -- a table only ``0110`` creates -- because
nothing checked schema compatibility before task dispatch. See
``docs/MIGRATION_COMPATIBILITY_GUARD_V1.md`` for the full reconstruction.

Compatibility contract: **exact schema revision equality**. The running
application declares :data:`REQUIRED_SCHEMA_REVISION`; it is compatible with
a database only when that database's single ``alembic_version`` row matches
exactly. This mirrors the one schema-preflight check that already existed
before this module (``scripts/dev.py::_schema_preflight_disposable_demo_database``,
which has required exact single-head equality for the disposable-demo
database since before this milestone) rather than introducing a new,
looser "minimum compatible revision" model -- Apex's migrations are a
strictly linear chain (one head, enforced by
``scripts/dev.py::_disposable_demo_migration_heads``), so there is exactly
one revision the running code was ever tested against, and a bounded or
minimum-revision contract would require maintaining a compatibility matrix
this repository has no other machinery for and does not need: every worker
ships from the identical single checkout, so there is never a moment where
one worker genuinely needs a looser contract than another (see
``apex.adapters.schema_compatibility_postgres`` module docstring for how
both unattended workers share this one contract).

Ordering between two revision strings (needed only to report *which*
direction a mismatch runs -- "behind" vs. "ahead") reuses the one existing,
load-bearing Apex migration convention: every revision id in
``migrations/versions/`` is named ``NNNN_description`` with a strictly
increasing, gapless-enough zero-padded integer prefix that already defines
the down-revision chain order (verified for the current chain by
``tests/test_schema_compatibility.py::test_recent_migration_chain_matches_numeric_prefix_order``).
Reusing that convention here avoids parsing ``down_revision`` links or
importing ``alembic`` into this pure module at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

#: The exact schema revision this application version requires. Bump this
#: alongside every new migration that lands under ``migrations/versions/``
#: -- ``tests/test_schema_compatibility.py`` mechanically asserts this
#: constant equals the true Alembic head, so a migration-only commit that
#: forgets to bump it fails fast and offline, before any deploy.
REQUIRED_SCHEMA_REVISION: Final[str] = "0118_evidence_gap_recovery"

_REVISION_PREFIX = re.compile(r"\A(\d+)_")


class SchemaCompatibilityStatus(Enum):
    """One typed, bounded classification of a worker's schema compatibility
    check -- never a raw database exception."""

    COMPATIBLE = "COMPATIBLE"
    SCHEMA_REVISION_BEHIND_APPLICATION = "SCHEMA_REVISION_BEHIND_APPLICATION"
    SCHEMA_REVISION_AHEAD_OF_APPLICATION = "SCHEMA_REVISION_AHEAD_OF_APPLICATION"
    SCHEMA_REVISION_UNKNOWN = "SCHEMA_REVISION_UNKNOWN"


@dataclass(frozen=True, slots=True)
class SchemaCompatibilityReport:
    """The full, operator-legible result of one compatibility evaluation.

    Never carries a connection string, host, user, or password -- only
    revision identifiers, the worker name, and a plain-text explanation --
    so it is always safe to log verbatim, matching
    ``apex.adapters.postgres.PostgresUnavailableError``'s existing
    "no leaked connection details" convention.
    """

    status: SchemaCompatibilityStatus
    worker_name: str
    required_revision: str
    observed_revision: str | None
    observed_revision_count: int
    detail: str

    @property
    def compatible(self) -> bool:
        return self.status is SchemaCompatibilityStatus.COMPATIBLE


class SchemaCompatibilityError(RuntimeError):
    """Raised by the adapter layer when a worker's schema is incompatible.

    Carries the full :class:`SchemaCompatibilityReport` as ``.report`` so a
    caller can branch on ``.status`` instead of parsing the message, while
    ``str(error)`` alone (what an uncaught exception prints to the journal)
    is already the complete, actionable, credential-free explanation."""

    def __init__(self, report: SchemaCompatibilityReport) -> None:
        self.report = report
        super().__init__(report.detail)


def _revision_sequence(revision: str) -> int | None:
    """The leading zero-padded integer Apex's migration-naming convention
    assigns every revision id, or ``None`` if ``revision`` does not follow
    that convention at all (treated as unknown/unparseable, never guessed
    at)."""

    match = _REVISION_PREFIX.match(revision)
    return int(match.group(1)) if match else None


def _remediation(status: SchemaCompatibilityStatus, *, required: str, observed: str | None) -> str:
    if status is SchemaCompatibilityStatus.SCHEMA_REVISION_BEHIND_APPLICATION:
        return (
            "apply the missing migration against this worker's target database "
            "('python scripts/dev.py migrate', or 'alembic upgrade head'), then "
            "restart the worker; do not restart repeatedly without migrating -- "
            "the schema will not become compatible on its own"
        )
    if status is SchemaCompatibilityStatus.SCHEMA_REVISION_AHEAD_OF_APPLICATION:
        return (
            f"deploy application code whose declared REQUIRED_SCHEMA_REVISION is "
            f"'{observed}' or later (this checkout still declares '{required}'), "
            "then restart the worker"
        )
    return (
        "inspect migration state directly ('alembic current', 'alembic history') "
        "before restarting the worker -- do not assume either direction; a "
        "partially applied or failed migration must be repaired, not guessed at"
    )


def evaluate_schema_compatibility(
    *,
    required_revision: str,
    observed_revisions: tuple[str, ...],
    worker_name: str,
) -> SchemaCompatibilityReport:
    """Pure classification: given the application's required revision and
    the raw ``version_num`` row(s) an adapter already read from
    ``alembic_version``, decide compatibility. Never raises -- see
    :class:`SchemaCompatibilityError` for the raising wrapper an adapter
    applies to this result.

    ``observed_revisions`` is empty when ``alembic_version`` has no row (a
    fresh/never-migrated database) or the table itself does not exist; it
    has more than one entry only for a genuinely corrupt or mid-migration
    database, since Apex's migration chain is enforced single-head. Both
    shapes classify as :attr:`SchemaCompatibilityStatus.SCHEMA_REVISION_UNKNOWN`
    -- this function never guesses which direction an indeterminate schema
    state might lean."""

    if len(observed_revisions) != 1:
        observed_count = len(observed_revisions)
        state = (
            "absent (never migrated, or the wrong database)"
            if observed_count == 0
            else (
                f"multiple simultaneous revisions ({', '.join(sorted(observed_revisions))}) -- "
                "a partially applied or corrupt migration state"
            )
        )
        remediation = _remediation(
            SchemaCompatibilityStatus.SCHEMA_REVISION_UNKNOWN,
            required=required_revision,
            observed=None,
        )
        return SchemaCompatibilityReport(
            status=SchemaCompatibilityStatus.SCHEMA_REVISION_UNKNOWN,
            worker_name=worker_name,
            required_revision=required_revision,
            observed_revision=None,
            observed_revision_count=observed_count,
            detail=(
                f"SCHEMA_REVISION_UNKNOWN worker={worker_name} "
                f"required={required_revision} observed={state} "
                f"remediation={remediation!r}"
            ),
        )

    (observed,) = observed_revisions
    if observed == required_revision:
        return SchemaCompatibilityReport(
            status=SchemaCompatibilityStatus.COMPATIBLE,
            worker_name=worker_name,
            required_revision=required_revision,
            observed_revision=observed,
            observed_revision_count=1,
            detail=f"COMPATIBLE worker={worker_name} revision={required_revision}",
        )

    required_sequence = _revision_sequence(required_revision)
    observed_sequence = _revision_sequence(observed)
    if required_sequence is None or observed_sequence is None:
        status = SchemaCompatibilityStatus.SCHEMA_REVISION_UNKNOWN
    elif observed_sequence < required_sequence:
        status = SchemaCompatibilityStatus.SCHEMA_REVISION_BEHIND_APPLICATION
    else:
        status = SchemaCompatibilityStatus.SCHEMA_REVISION_AHEAD_OF_APPLICATION

    return SchemaCompatibilityReport(
        status=status,
        worker_name=worker_name,
        required_revision=required_revision,
        observed_revision=observed,
        observed_revision_count=1,
        detail=(
            f"{status.value} worker={worker_name} required={required_revision} "
            f"observed={observed} "
            f"remediation={_remediation(status, required=required_revision, observed=observed)!r}"
        ),
    )
