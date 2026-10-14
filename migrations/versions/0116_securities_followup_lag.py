"""Securities Follow-Up Temporal Integrity v1 -- constraint widening only.

``domain.counterfactual_followup`` gains ``FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2``,
a SECURITIES-only, additive sibling of the frozen V1 evidence-quality
policy. V2 rejects a terminal options-chain snapshot that was not captured
inside the first eligible post-close capture window at or after the
follow-up's target. Each V2 verdict is a new, additional row in the
existing append-only ``followup_evidence_quality_assessment`` table, one per
``(observation_id, assessment_policy_version)``, which is already the
table's identity. Its timing provenance lives in the existing schemaless
``payload``. ``domain.securities_structure_outcome`` may now aggregate over
V2 verdicts. ``evidence_quality_policy_version`` is already part of that
table's cohort id, so a V2 aggregate never shares a row identity with a V1
one.

This migration therefore only widens CHECK constraints:

* ``ck_followup_evidence_quality_policy_version``: V1 -> V1, V2.
* ``ck_followup_evidence_quality_defect_type``: adds
  ``TARGET_EVIDENCE_TEMPORALLY_INVALID`` and ``TARGET_SESSION_NOT_RESOLVABLE``.
* New ``ck_followup_evidence_quality_defect_by_version``: V1 rows may never
  carry a V2-only defect type. This is a defensive restatement of the
  domain's pairing, and it adds no constraint any retained row could violate,
  since every retained row is V1 with a V1 defect type.
* ``ck_securities_structure_outcome_evidence_quality_policy_version``:
  V1 -> V1, V2.

No column is added, and no retained row is read, altered, or reinterpreted.
Both tables' row-level append-only triggers are unaffected.

Downgrade is guarded, symmetric with 0103/0113/0114. It refuses while any
V2 assessment or V2 aggregate is retained, rather than deleting evidence or
leaving a CHECK the data violates.

Historically self-contained: imports only ``alembic``/``sqlalchemy`` and
hardcodes every literal.

Revision ID: 0116_securities_followup_lag
Revises: 0115_strategy_provenance
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0116_securities_followup_lag"
down_revision: str | None = "0115_strategy_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ASSESSMENT_TABLE = "followup_evidence_quality_assessment"
_AGGREGATE_TABLE = "securities_structure_outcome_aggregate"
_V1 = "FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1"
_V2 = "FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2"

_V1_DEFECTS = (
    "'NONE','BASELINE_EVIDENCE_INELIGIBLE','TARGET_EVIDENCE_INELIGIBLE',"
    "'BOTH_SIDES_EVIDENCE_INELIGIBLE','LINEAGE_NOT_RESOLVABLE',"
    "'OBSERVATION_HASH_INVALID'"
)
_V2_ONLY_DEFECTS = "'TARGET_EVIDENCE_TEMPORALLY_INVALID','TARGET_SESSION_NOT_RESOLVABLE'"


def upgrade() -> None:
    op.drop_constraint("ck_followup_evidence_quality_policy_version", _ASSESSMENT_TABLE, "check")
    op.create_check_constraint(
        "ck_followup_evidence_quality_policy_version",
        _ASSESSMENT_TABLE,
        f"assessment_policy_version IN ('{_V1}', '{_V2}')",
    )
    op.drop_constraint("ck_followup_evidence_quality_defect_type", _ASSESSMENT_TABLE, "check")
    op.create_check_constraint(
        "ck_followup_evidence_quality_defect_type",
        _ASSESSMENT_TABLE,
        f"evidence_defect_type IN ({_V1_DEFECTS},{_V2_ONLY_DEFECTS})",
    )
    op.create_check_constraint(
        "ck_followup_evidence_quality_defect_by_version",
        _ASSESSMENT_TABLE,
        f"assessment_policy_version = '{_V2}' OR evidence_defect_type NOT IN ({_V2_ONLY_DEFECTS})",
    )
    op.drop_constraint(
        "ck_securities_structure_outcome_evidence_quality_policy_version",
        _AGGREGATE_TABLE,
        "check",
    )
    op.create_check_constraint(
        "ck_securities_structure_outcome_evidence_quality_policy_version",
        _AGGREGATE_TABLE,
        f"evidence_quality_policy_version IN ('{_V1}', '{_V2}')",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {_ASSESSMENT_TABLE} "  # nosec B608
            f"WHERE assessment_policy_version = '{_V2}') "
            f"OR EXISTS (SELECT 1 FROM {_AGGREGATE_TABLE} "  # nosec B608
            f"WHERE evidence_quality_policy_version = '{_V2}') THEN "
            "RAISE EXCEPTION "
            "'cannot downgrade 0116 with retained FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2 evidence'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint(
        "ck_securities_structure_outcome_evidence_quality_policy_version",
        _AGGREGATE_TABLE,
        "check",
    )
    op.create_check_constraint(
        "ck_securities_structure_outcome_evidence_quality_policy_version",
        _AGGREGATE_TABLE,
        f"evidence_quality_policy_version = '{_V1}'",
    )
    op.drop_constraint("ck_followup_evidence_quality_defect_by_version", _ASSESSMENT_TABLE, "check")
    op.drop_constraint("ck_followup_evidence_quality_defect_type", _ASSESSMENT_TABLE, "check")
    op.create_check_constraint(
        "ck_followup_evidence_quality_defect_type",
        _ASSESSMENT_TABLE,
        f"evidence_defect_type IN ({_V1_DEFECTS})",
    )
    op.drop_constraint("ck_followup_evidence_quality_policy_version", _ASSESSMENT_TABLE, "check")
    op.create_check_constraint(
        "ck_followup_evidence_quality_policy_version",
        _ASSESSMENT_TABLE,
        f"assessment_policy_version = '{_V1}'",
    )
