"""CryptoResearchConflict Root-Cause & Autonomous Recovery v1.

Fixes a real, reproduced production defect. On 2026-08-16 the unattended
``CRYPTO_MARKET_PULSE`` task genuinely FAILED with retained ``error_code``
``CryptoResearchConflict`` -- the first time the retained stateful
multi-asset pullback-rotation signal (``domain.crypto_stateful_research.
current_stateful_signal``, driven through
``application.crypto_stateful_research.RunCurrentCryptoStatefulMethod``)
ever selected an asset to enter (``CryptoStatefulCurrentSignal.state ==
LONG``, asset XLM) instead of staying in ``CASH``. Every earlier day since
this method was wired into the recurring cycle had stayed ``CASH``
(confirmed against all six retained ``crypto_stateful_current_signal``
rows), so the defect was latent and had never before been exercised on
the one code path that actually persists an opportunity.

Root cause: ``crypto_research_opportunity.discovery_run_id`` carried a
single ``FOREIGN KEY ... REFERENCES crypto_discovery_run(run_id)`` added
in migration 0055, back when every opportunity genuinely came from a
cross-sectional ``CryptoDiscoveryRun``. That constraint assumed every
opportunity's lineage pointer always names a ``crypto_discovery_run`` row.
But the stateful pullback-rotation method (added later) never produces a
``CryptoDiscoveryRun`` at all -- it produces a ``CryptoStatefulCurrentSignal``
instead, in its own, entirely separate, already-FK-safe identity space --
and ``current_stateful_signal`` correctly, deliberately reuses
``discovery_run_id`` to carry that signal's id. This reuse is not a bug:
``adapters.shared_opportunity_sources`` already reads
``item.discovery_run_id == stateful_signal.signal_id`` at two call sites,
and ``RunCurrentCryptoStatefulMethod``'s own idempotent-replay branch
already reads it back the same way. So the field's real, intended identity
contract has always been "the id of whichever run or signal produced this
opportunity" -- a union of two legitimate, independently-FK-safe producer
spaces -- not "always a crypto_discovery_run row". The 0055 constraint was
simply narrower than that already-established intent, so every attempt to
persist a genuine stateful-sourced opportunity was guaranteed to raise a
foreign-key ``IntegrityError``. ``PostgresCryptoUniverseRepository._insert``
(an inherited, generic persistence-conflict mapper shared by every crypto
evidence table) wraps *any* ``IntegrityError`` as
``CryptoResearchConflict("CRYPTO_OPPORTUNITY_IDENTITY_CONFLICT")`` --
indistinguishable, from the caller's side, from a genuine same-identity
content conflict -- and that is exactly what
``application.operating_cycle.ExecuteOperatingCycle._dispatch_task``
recorded as the retained task-result ``error_code`` (it maps any
uncaught ``RuntimeError`` subclass to ``type(error).__name__``).

A full audit of every ``CryptoResearchOpportunity`` construction site
(``domain.crypto_universe.build_opportunities``,
``build_relative_strength_opportunities``,
``build_reversal_regime_opportunities``, and
``domain.crypto_stateful_research.current_stateful_signal``) and of every
other foreign key referencing ``crypto_discovery_run`` confirms this is
the only column and the only producer with this defect shape: the three
``crypto_universe`` builders always pass a genuine ``CryptoDiscoveryRun.
run_id`` and are unaffected. Genuinely retained CRYPTO evidence otherwise
shows no other row that would satisfy this failure's predicate -- the
2026-08-16 failure is the first and only ``CryptoResearchConflict`` ever
recorded against ``CRYPTO_MARKET_PULSE`` in retained history, not part of
a broader pattern.

This migration is the entire fix; it requires no application or domain
code change, because ``current_stateful_signal`` was already constructing
the opportunity correctly for the field's real, already-intended contract
-- only the database's enforcement of that contract was too narrow. It
replaces the single-table foreign key with a ``BEFORE INSERT`` trigger
that enforces the field's actual, always-intended invariant precisely:
every ``discovery_run_id`` must still durably resolve to a real, existing
producer row -- now checked against *both* legitimate producer tables
(``crypto_discovery_run`` OR ``crypto_stateful_current_signal``) instead
of exactly one. A row whose ``discovery_run_id`` resolves to neither still
fails closed with the identical ``CryptoResearchConflict`` as before --
this widens which genuine identities are accepted, it does not weaken what
counts as genuine, and it does not touch the existing
``apex_immutable_crypto_research_opportunity_trigger`` (that trigger only
fires on UPDATE/DELETE; this one only fires on INSERT). No historical row
is touched, rewritten, or backfilled: all already-retained (non-stateful)
opportunities keep satisfying the new check exactly as they satisfied the
old one, since every one of them already names a real
``crypto_discovery_run`` row.

Referential-integrity follow-up (same milestone, pre-merge audit): dropping
the single-table FK also silently drops a guarantee it provided for free
that the row-level ``apex_immutable_*_trigger`` (``BEFORE UPDATE OR
DELETE``, ``FOR EACH ROW``) never covered -- Postgres TRUNCATE bypasses
row-level triggers entirely, but a real FK constraint independently refuses
to TRUNCATE a referenced parent table on its own. Proven empirically: on
revision 0099, ``TRUNCATE crypto_discovery_run`` alone (naming only that
table, no ``CASCADE``) raises Postgres's own ``FeatureNotSupported: cannot
truncate a table referenced in a foreign key constraint``; on this
revision, with the FK dropped and before this paragraph's fix, the
identical statement silently succeeds and strands any
``crypto_research_opportunity`` row that referenced it -- a genuine
regression in durable lineage integrity, specific to ``crypto_discovery_run``
(the producer that used to have an incoming FK; ``crypto_stateful_current_signal``
never had one, so it was never protected against TRUNCATE and is not a
regression, but is closed here too for a consistent closed producer set).
This migration therefore also adds a narrow, statement-level ``BEFORE
TRUNCATE`` guard on both producer tables. It intentionally does not reuse
``apex_reject_mutation`` (the 150+-table UPDATE/DELETE boundary) as-is,
because ``tests/persistence/conftest.py``'s ``clean_database`` fixture
legitimately issues one combined ``TRUNCATE TABLE <every table> RESTART
IDENTITY CASCADE`` between every test, including both producer tables, and
must keep working. The new guard therefore fails closed by default and
only permits TRUNCATE inside a transaction that has explicitly opted in via
``SET LOCAL apex.allow_producer_truncate = 'true'`` -- exactly what that
fixture now does immediately before its own TRUNCATE -- so production and
any other untagged caller remain unconditionally blocked, matching what the
dropped FK already guaranteed. This is deliberately scoped to only the two
lineage producer tables, not generalized to the wider immutable-table
boundary.

Test-bypass hardening (2026-08-16, final pre-merge security review): the GUC
opt-in above is necessary but was not, by itself, a genuine access-control
boundary. Proven empirically against the disposable ``apex_test`` database,
connecting as the exact same ``apex`` role/password the production connection
uses (this repo has no separate test-only role -- database name is the only
thing that ever distinguishes the two): Postgres accepts
``SET LOCAL apex.allow_producer_truncate = 'true'`` from that role with no
privilege beyond ordinary ``CONNECT``, because a namespaced, dotted setting
like ``apex.allow_producer_truncate`` is treated as an unvalidated placeholder
GUC that any role may set. Since production and test authenticate identically,
nothing about the role itself stops a caller holding ordinary production
credentials from opting in immediately before a TRUNCATE and defeating the
guard against the live ``apex`` database. The guard function above now also
requires ``current_database()`` to end with the literal suffix ``_test`` --
the one naming invariant ``PostgresSettings.__post_init__`` already enforces
for ``test_database`` (and that ``APEX_POSTGRES_TEST_DB`` may rename but must
keep) -- so opting in to the GUC only ever succeeds against a database that is
already, independently, established as disposable; a database named exactly
``apex`` can never satisfy it regardless of the GUC. ``tests/persistence/
conftest.py`` needed no change: its ``postgres_engine``/``clean_database``
fixtures already only ever connect to ``postgres_settings.database``, which
``for_test()`` always sets to ``test_database`` -- guaranteed by construction
to end with ``_test`` -- so the existing whole-database TRUNCATE cleanup
continues to work unmodified. Empirically reconfirmed post-fix against both a
throwaway, non-``_test``-suffixed scratch database (untagged, tagged, and
tagged-``CASCADE`` TRUNCATE all still blocked) and the real ``apex_test``
database (untagged blocked, tagged succeeds, exactly as ``clean_database``
needs). Scoped identically to the paragraph above: only this one TRUNCATE
guard function changed. ``apex_reject_mutation`` (the separate 150+-table
UPDATE/DELETE boundary) was not touched and was out of scope for this review;
it has no TRUNCATE guard of its own at all today, so any immutable table it
protects that lacks its own incoming foreign key is presumably not protected
against an untagged TRUNCATE either -- noted here for a future audit, not
investigated or changed in this milestone.

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``):
imports only ``alembic`` and ``sqlalchemy``, never ``apex.domain``/
``apex.application``/``apex.adapters``.

Revision ID: 0100_crypto_opportunity_lineage
Revises: 0099_followup_evidence_quality
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0100_crypto_opportunity_lineage"
down_revision: str | None = "0099_followup_evidence_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_FK = "crypto_research_opportunity_discovery_run_id_fkey"
_GUARD_FUNCTION = "apex_crypto_opportunity_lineage_guard"
_GUARD_TRIGGER = "apex_crypto_opportunity_lineage_guard_trigger"
_TRUNCATE_GUARD_FUNCTION = "apex_reject_untagged_producer_truncate"
_TRUNCATE_GUARD_SETTING = "apex.allow_producer_truncate"
_PRODUCER_TABLES = ("crypto_discovery_run", "crypto_stateful_current_signal")


def _truncate_trigger_name(table: str) -> str:
    return f"apex_immutable_{table}_truncate_trigger"


def upgrade() -> None:
    op.drop_constraint(_OLD_FK, "crypto_research_opportunity", type_="foreignkey")
    op.execute(
        f"""
        CREATE FUNCTION {_GUARD_FUNCTION}() RETURNS trigger AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM crypto_discovery_run WHERE run_id = NEW.discovery_run_id
          ) AND NOT EXISTS (
            SELECT 1 FROM crypto_stateful_current_signal
            WHERE signal_id = NEW.discovery_run_id
          ) THEN
            -- ERRCODE 23503 (foreign_key_violation) is deliberate, not
            -- decorative: every caller up through
            -- ``PostgresCryptoUniverseRepository._insert`` /
            -- ``application.operating_cycle.ExecuteOperatingCycle.
            -- _dispatch_task`` only ever catches ``IntegrityError``
            -- (``RuntimeError``'s DB-API sibling for a bare ``RAISE
            -- EXCEPTION`` is ``ProgrammingError``, which is *not* a
            -- ``RuntimeError`` subclass and would propagate uncaught out
            -- of the whole recurring cycle instead of failing one task
            -- closed) -- this trigger must fail exactly the same way the
            -- single-table foreign key it replaces already did.
            RAISE EXCEPTION
              'crypto_research_opportunity.discovery_run_id must reference an existing '
              'crypto_discovery_run or crypto_stateful_current_signal row'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"CREATE TRIGGER {_GUARD_TRIGGER} "
        "BEFORE INSERT ON crypto_research_opportunity FOR EACH ROW "
        f"EXECUTE FUNCTION {_GUARD_FUNCTION}()"
    )
    op.execute(
        f"""
        CREATE FUNCTION {_TRUNCATE_GUARD_FUNCTION}() RETURNS trigger AS $$
        BEGIN
          IF current_setting('{_TRUNCATE_GUARD_SETTING}', true) IS DISTINCT FROM 'true'
             OR right(current_database(), 5) <> '_test' THEN
            -- NULL-safe on purpose: ``current_setting(..., true)`` returns
            -- NULL (never a plain SQL-false) when the caller never opted in,
            -- and a NULL compared with ``<>`` would itself evaluate to NULL
            -- (PL/pgSQL treats a NULL ``IF`` condition as false, i.e. would
            -- silently *permit* the untagged case) -- ``IS DISTINCT FROM``
            -- is the only comparison here that stays fail-closed by default.
            --
            -- Test-bypass hardening (same milestone): the GUC alone is not a
            -- real access-control boundary. Postgres lets any role -- no
            -- privilege beyond CONNECT required -- ``SET`` a namespaced,
            -- placeholder GUC like this one, and this repo's production and
            -- test connections authenticate as the exact same role (``apex``);
            -- nothing stops a caller with ordinary production credentials
            -- from issuing ``SET LOCAL apex.allow_producer_truncate = 'true'``
            -- immediately before a TRUNCATE. ``current_database()`` is never
            -- NULL -- unlike the GUC above, there is no "unset" state to
            -- protect against -- so a plain ``<>`` is safe here. This second
            -- half additionally requires the connected database's name to
            -- end with the literal suffix ``_test``, mirroring the one
            -- existing repo-wide invariant that actually distinguishes a
            -- disposable test database from production
            -- (``PostgresSettings.__post_init__``'s
            -- ``test_database.endswith("_test")`` check, which also lets
            -- ``APEX_POSTGRES_TEST_DB`` rename the test database as long as
            -- it keeps that suffix). ``right(..., 5) = '_test'`` mirrors
            -- ``str.endswith`` directly instead of a ``LIKE '%\\_test'``
            -- pattern, which would need to escape the literal underscore for
            -- no benefit. A database actually named ``apex`` -- or anything
            -- else not ending in ``_test`` -- can never satisfy this half of
            -- the check no matter what the GUC is set to, so production stays
            -- unconditionally blocked even from a caller that opts in.
            RAISE EXCEPTION 'immutable Apex row cannot be changed';
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in _PRODUCER_TABLES:
        op.execute(
            f"CREATE TRIGGER {_truncate_trigger_name(table)} "
            f'BEFORE TRUNCATE ON "{table}" FOR EACH STATEMENT '
            f"EXECUTE FUNCTION {_TRUNCATE_GUARD_FUNCTION}()"
        )
    # Column-name semantic debt (referential-integrity audit, section 5):
    # deliberately not renamed this milestone -- the domain/repository
    # contract is unambiguous (see ``CryptoResearchOpportunity.discovery_run_id``'s
    # docstring in ``domain.crypto_universe``) and a rename would touch every
    # construction/read site across domain, application, and adapters for no
    # behavior change. Recorded here in the schema itself so the real
    # contract is discoverable without reading application code.
    op.execute(
        "COMMENT ON COLUMN crypto_research_opportunity.discovery_run_id IS "
        "'Union lineage identity, despite the name: resolves through either "
        "crypto_discovery_run.run_id or crypto_stateful_current_signal.signal_id, "
        "enforced by the apex_crypto_opportunity_lineage_guard BEFORE INSERT "
        "trigger (see migration 0100). Not a single-table foreign key.'"
    )


def downgrade() -> None:
    op.execute("COMMENT ON COLUMN crypto_research_opportunity.discovery_run_id IS NULL")
    for table in reversed(_PRODUCER_TABLES):
        op.execute(f'DROP TRIGGER {_truncate_trigger_name(table)} ON "{table}"')
    op.execute(f"DROP FUNCTION {_TRUNCATE_GUARD_FUNCTION}()")
    op.execute(f"DROP TRIGGER {_GUARD_TRIGGER} ON crypto_research_opportunity")
    op.execute(f"DROP FUNCTION {_GUARD_FUNCTION}()")
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS ("
        "  SELECT 1 FROM crypto_research_opportunity o "
        "  WHERE NOT EXISTS ("
        "    SELECT 1 FROM crypto_discovery_run d WHERE d.run_id = o.discovery_run_id"
        "  )"
        ") THEN "
        "RAISE EXCEPTION "
        "'cannot downgrade 0100: retained crypto_research_opportunity rows exist whose "
        "discovery_run_id only resolves through crypto_stateful_current_signal, which the "
        "prior single-table foreign key cannot represent'; "
        "END IF; END $$"
    )
    op.create_foreign_key(
        _OLD_FK,
        "crypto_research_opportunity",
        "crypto_discovery_run",
        ["discovery_run_id"],
        ["run_id"],
    )
