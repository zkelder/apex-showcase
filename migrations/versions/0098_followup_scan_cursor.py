"""Counterfactual Follow-Up Long-Run Fairness v1.

Adds ``followup_scan_cursor``: durable resume bookkeeping for
``SyncFollowUpObservations``' own bounded, oldest-decision-first walk over
``(decision, horizon_kind, baseline_kind)`` candidates -- one row per
``policy_version``, recording the last candidate a *completed* bounded scan
examined so the next cycle can resume immediately after it instead of
unconditionally restarting at the single oldest decision's first candidate
every time.

This closes a second, independent starvation path a final-review audit of
Counterfactual Follow-Up Starvation Fix v1 (migration 0096/0097) found:
classifying a CRYPTO baseline ``PERMANENTLY_UNAVAILABLE`` costs zero
``sample_limit``/attempt budget, but every candidate a bounded scan looks
at -- ``OBSERVED``, ``NOT_YET_DUE``, ``UNSUPPORTED``, permanently blocked,
transiently blocked, and genuinely attempted alike -- still costs one unit
of ``candidate_scan_limit``, and nothing remembered where the previous
cycle's scan actually stopped.  Any oldest-sorting prefix that is not a
fresh genuine attempt -- permanently blocked, already observed, or simply
not yet due for a long declared horizon -- therefore starves every
later-sorting satisfiable pair identically on every cycle, forever, once
that prefix alone exceeds ``candidate_scan_limit``, no matter how generous
a fixed constant that bound is given (``OBSERVED`` pairs alone accumulate at
the front of this same order for as long as Apex keeps operating).  See
``application.counterfactual_followup``'s own module docstring and
``FollowUpScanPosition``/``_rotate_from_cursor`` for the full mechanism and
proof.

``followup_scan_cursor`` is deliberately **not** append-only/immutable like
every other table this repository owns (``decision_followup_observation``
included) -- it is genuinely mutable scan-position *bookkeeping*, upserted
every cycle, never evidence and never reported to an operator as if it
were.  This repository has exactly one narrower precedent for a genuinely
mutable table (``securities_position_state``, migration 0026, guarded by a
bespoke state-machine trigger because a live position's *economic* state
must obey specific transition rules); this table needs no such guard --
losing or rolling back a row here is never a correctness hazard, only
bounded, rare, wasted re-scanning the next time ``SyncFollowUpObservations.
execute`` runs (every write it makes is already idempotent) -- so it
carries no immutability trigger and no custom guard trigger, only a plain
primary key and a foreign key to the decision it names.

Historically self-contained per the migration-boundary rule (mirrors
migration 0096/0097's own doctrine, hardened by Migration 0095
Historical Reproducibility Hardening v1): this migration imports only
``alembic``/``sqlalchemy`` -- never ``apex.domain``/``apex.application`` --
so a fresh ``0001 -> head`` replay never depends on how live application
code has changed since this migration shipped.  It creates the table only;
there is no data to backfill (a wholly new, forward-only scheduling
artifact, not a correction to already-retained evidence).

Revision ID: 0098_followup_scan_cursor
Revises: 0097_followup_policy_v2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0098_followup_scan_cursor"
down_revision: str | None = "0097_followup_policy_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "followup_scan_cursor"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("policy_version", sa.String(40), primary_key=True),
        sa.Column(
            "decision_id",
            sa.String(300),
            sa.ForeignKey("counterfactual_decision.decision_id"),
            nullable=False,
        ),
        sa.Column("horizon_kind", sa.String(20), nullable=False),
        sa.Column("baseline_kind", sa.String(20), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "horizon_kind IN ('SHORT_HORIZON','STRATEGY_HORIZON')",
            name="ck_followup_scan_cursor_horizon_kind",
        ),
        sa.CheckConstraint(
            "baseline_kind IN ('ORIGIN_EVIDENCE','DECISION_TIME')",
            name="ck_followup_scan_cursor_baseline_kind",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_followup_scan_cursor_authority"),
    )


def downgrade() -> None:
    # Unlike every other downgrade guard in this repository, no "refuse if
    # retained evidence would be lost" check is needed: this table holds no
    # evidence, only a resume pointer that is safe to lose (Part: see this
    # migration's own docstring). Dropping it merely means the next
    # ``SyncFollowUpObservations.execute`` call resumes at the single oldest
    # decision's first candidate, exactly as every call did before this
    # migration existed.
    op.drop_table(_TABLE)
