"""CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
Remediation v1.

Adds ``followup_evidence_quality_assessment``: one immutable, append-only
row per genuinely assessed ``(observation_id, assessment_policy_version)``,
referencing an already-retained ``decision_followup_observation`` row by
``observation_id`` (foreign key) + ``observation_canonical_hash`` (embedded
in the row's own ``payload``, for tamper-evidence -- the same "reference by
id and hash" idiom migrations 0094/0095/0096 already established for
``counterfactual_decision``/``decision_followup_observation``).

This migration exists because a read-only-audited defect (confirmed against
real retained data: ``decision_followup_observation_
260a08ba994dca94910e1bf2``, a DOGE-USD ``FOLLOWUP_POLICY_V2`` observation)
proved that ``CryptoPriceSource.daily_close``/``.latest_close_before`` never
excluded synthetic ``EvidenceClass.FIXTURE`` evidence before this milestone
-- see ``domain.counterfactual_followup``'s own "CRYPTO Follow-Up Evidence
Eligibility & Contaminated Observation Remediation v1" section for the full
trace and the fail-closed allowlist fix.  The already-retained contaminated
row is historical truth about what Apex actually produced and is NEVER
rewritten, deleted, or replaced by this migration or any code in this
milestone -- this table instead adds a place to durably, additively record
an explicit, versioned, retroactive eligibility verdict about it (and about
every other already-retained follow-up observation, so "every retained
observation has exactly one assessment under a given policy version" holds
without exception).  This migration creates no new investment judgment and
grants no new authority; it never marks a row as if it had been corrected,
only as eligible or ineligible for downstream attribution/research use.

``evidence_defect_type`` includes ``'OBSERVATION_HASH_INVALID'`` (final-review
addition, same milestone): a pre-deployment audit found the assessment's own
``observation_canonical_hash`` was a plain, unverified copy of the assessed
observation's stored ``canonical_hash`` column, never independently
recomputed and checked -- now checked, fail-closed, before any venue or
evidence-class branching (``domain.counterfactual_followup.
observation_canonical_hash_matches``); a mismatch is always
``INELIGIBLE_EVIDENCE`` regardless of venue.  Re-verified against the real
retained 92-row corpus: zero mismatches (every row's stored hash still
matches its own fields exactly).

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``,
hardened by Migration 0095 Historical Reproducibility Hardening v1): this
migration imports only ``alembic``, ``sqlalchemy``, and
``apex.adapters.postgres.immutable_trigger_ddl`` -- never
``apex.domain``/``apex.application`` -- so a fresh ``0001 -> head`` replay
never depends on how live application code has changed since this migration
shipped.  It creates the table only; it does not backfill any data (running
the retroactive assessment itself is an explicit, separately-triggered
``followup-evidence-quality-assess`` CLI action -- see ``cli.py`` -- never
something a migration silently does on behalf of an operator).

Revision ID: 0099_followup_evidence_quality
Revises: 0098_followup_scan_cursor
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0099_followup_evidence_quality"
down_revision: str | None = "0098_followup_scan_cursor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOLLOWUP_EVIDENCE_QUALITY_TABLES = ("followup_evidence_quality_assessment",)


def upgrade() -> None:
    op.create_table(
        "followup_evidence_quality_assessment",
        sa.Column("assessment_id", sa.String(300), primary_key=True),
        sa.Column(
            "observation_id",
            sa.String(300),
            sa.ForeignKey("decision_followup_observation.observation_id"),
            nullable=False,
        ),
        sa.Column("assessment_policy_version", sa.String(60), nullable=False),
        sa.Column("verdict", sa.String(24), nullable=False),
        sa.Column("evidence_defect_type", sa.String(40), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "assessment_policy_version = 'FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1'",
            name="ck_followup_evidence_quality_policy_version",
        ),
        sa.CheckConstraint(
            "verdict IN ('ELIGIBLE','INELIGIBLE_EVIDENCE')",
            name="ck_followup_evidence_quality_verdict",
        ),
        sa.CheckConstraint(
            "evidence_defect_type IN ("
            "'NONE','BASELINE_EVIDENCE_INELIGIBLE','TARGET_EVIDENCE_INELIGIBLE',"
            "'BOTH_SIDES_EVIDENCE_INELIGIBLE','LINEAGE_NOT_RESOLVABLE',"
            "'OBSERVATION_HASH_INVALID')",
            name="ck_followup_evidence_quality_defect_type",
        ),
        # An ``ELIGIBLE`` verdict must carry no defect; an ``INELIGIBLE_
        # EVIDENCE`` verdict must always name one -- a defensive, DB-level
        # restatement of ``domain.counterfactual_followup.assess_followup_
        # evidence_quality``'s own pairing, so a row that violates it could
        # only come from bypassing that builder entirely.
        sa.CheckConstraint(
            "(verdict = 'ELIGIBLE' AND evidence_defect_type = 'NONE') OR "
            "(verdict = 'INELIGIBLE_EVIDENCE' AND evidence_defect_type != 'NONE')",
            name="ck_followup_evidence_quality_verdict_defect_agreement",
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_followup_evidence_quality_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_followup_evidence_quality_authority"),
        # Defensive, DB-level restatement of the domain identity formula
        # (``observation_id`` + ``assessment_policy_version`` ->
        # ``assessment_id``): even a caller that bypassed
        # ``evidence_quality_assessment_id`` and somehow computed a
        # different ``assessment_id`` string for the same pair could not
        # insert a second row for it.
        sa.UniqueConstraint(
            "observation_id",
            "assessment_policy_version",
            name="uq_followup_evidence_quality_identity",
        ),
    )
    op.create_index(
        "ix_followup_evidence_quality_observation_id",
        "followup_evidence_quality_assessment",
        ["observation_id"],
    )
    op.create_index(
        "ix_followup_evidence_quality_verdict",
        "followup_evidence_quality_assessment",
        ["verdict"],
    )
    for table in FOLLOWUP_EVIDENCE_QUALITY_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(FOLLOWUP_EVIDENCE_QUALITY_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                "'cannot downgrade 0099 with retained evidence-quality assessment evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(FOLLOWUP_EVIDENCE_QUALITY_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
