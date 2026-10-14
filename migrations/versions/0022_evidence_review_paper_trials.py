"""Persist immutable evidence reviews and campaign-scoped PAPER trials.

Revision ID: 0022_evidence_review_trials
Revises: 0021_shadow_operations
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    EVIDENCE_REVIEW_IMMUTABLE_TABLES,
    EVIDENCE_REVIEW_TABLES,
    evidence_review_metadata,
    immutable_trigger_ddl,
)

revision: str = "0022_evidence_review_trials"
down_revision: str | None = "0021_shadow_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EVIDENCE_REVIEW_ACTIONS = (
    "'BUILD_EVIDENCE_SNAPSHOT', 'REQUEST_STRATEGY_EVIDENCE_REVIEW', "
    "'START_STRATEGY_EVIDENCE_REVIEW', 'DECIDE_STRATEGY_EVIDENCE_REVIEW', "
    "'DEFER_STRATEGY_EVIDENCE_REVIEW', 'CANCEL_STRATEGY_EVIDENCE_REVIEW', "
    "'EXPIRE_STRATEGY_EVIDENCE_REVIEWS', 'CREATE_PAPER_TRIAL_AUTHORIZATION', "
    "'PAUSE_PAPER_TRIAL_AUTHORIZATION', 'RESUME_PAPER_TRIAL_AUTHORIZATION', "
    "'REVOKE_PAPER_TRIAL_AUTHORIZATION', 'COMPLETE_PAPER_TRIAL_AUTHORIZATION', "
    "'RECONCILE_PAPER_TRIAL_LIMITS'"
)
PREVIOUS_OPERATOR_ACTIONS = (
    "'CREATE_CAMPAIGN', 'RUN_SHADOW_CYCLE', 'RUN_PAPER_CYCLE', 'PAUSE_CAMPAIGN', "
    "'RESUME_CAMPAIGN', 'KILL_CAMPAIGN', 'MARK_POSITIONS', 'SETTLE_POSITION', "
    "'SUBMIT_THESIS', 'EVALUATE_PREDICTION', 'CHANGE_STRATEGY_AUTHORITY', "
    "'RUN_LIVE_SHADOW_SESSION', 'CREATE_SHADOW_EVIDENCE_PLAN', "
    "'PAUSE_SHADOW_EVIDENCE_PLAN', 'RESUME_SHADOW_EVIDENCE_PLAN', "
    "'CANCEL_SHADOW_EVIDENCE_PLAN', 'RUN_SHADOW_EVIDENCE_ITERATION', "
    "'COLLECT_PUBLIC_OUTCOME', 'EVALUATE_FORWARD_PERFORMANCE'"
)


REVIEW_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_strategy_evidence_review() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'strategy evidence review cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'state', 'review_started_at', 'decided_at', 'decision_action_id',
        'operator_reason', 'reviewer_note', 'canonical_result_hash', 'reason_codes'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'state', 'review_started_at', 'decided_at', 'decision_action_id',
        'operator_reason', 'reviewer_note', 'canonical_result_hash', 'reason_codes'
    ]) THEN
        RAISE EXCEPTION 'strategy evidence review request is immutable';
    END IF;
    IF OLD.state = 'REQUESTED' AND NEW.state IN ('IN_REVIEW', 'CANCELED', 'EXPIRED') THEN
        RETURN NEW;
    END IF;
    IF OLD.state = 'IN_REVIEW' AND NEW.state IN (
        'BLOCKED', 'APPROVED', 'REJECTED', 'DEFERRED', 'CANCELED', 'EXPIRED'
    ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'strategy evidence review transition is invalid';
END;
$$;

CREATE TRIGGER apex_guard_strategy_evidence_review_trigger
BEFORE UPDATE OR DELETE ON strategy_evidence_review
FOR EACH ROW EXECUTE FUNCTION apex_guard_strategy_evidence_review();
"""


TRIAL_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_paper_trial_authorization() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'paper trial authorization cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'state', 'completed_cycle_count', 'paper_order_count', 'settled_position_count',
        'cumulative_realized_result', 'suspension_reason', 'revocation_reason',
        'canonical_result_hash'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'state', 'completed_cycle_count', 'paper_order_count', 'settled_position_count',
        'cumulative_realized_result', 'suspension_reason', 'revocation_reason',
        'canonical_result_hash'
    ]) THEN
        RAISE EXCEPTION 'paper trial authorization terms are immutable';
    END IF;
    IF OLD.state = 'SCHEDULED' AND NEW.state IN ('ACTIVE', 'EXPIRED', 'REVOKED') THEN RETURN NEW; END IF;
    IF OLD.state = 'ACTIVE' AND NEW.state IN ('ACTIVE', 'PAUSED', 'SUSPENDED_LIMIT', 'COMPLETED', 'EXPIRED', 'REVOKED') THEN RETURN NEW; END IF;
    IF OLD.state = 'PAUSED' AND NEW.state IN ('ACTIVE', 'COMPLETED', 'EXPIRED', 'REVOKED') THEN RETURN NEW; END IF;
    IF OLD.state = 'SUSPENDED_LIMIT' AND NEW.state IN ('REVOKED', 'COMPLETED') THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'paper trial authorization transition is invalid';
END;
$$;

CREATE TRIGGER apex_guard_paper_trial_authorization_trigger
BEFORE UPDATE OR DELETE ON paper_trial_authorization
FOR EACH ROW EXECUTE FUNCTION apex_guard_paper_trial_authorization();
"""


def _operator_action_check(values: str) -> str:
    return f"action_type IN ({values})"


def upgrade() -> None:
    evidence_review_metadata.create_all(
        bind=op.get_bind(), tables=EVIDENCE_REVIEW_TABLES, checkfirst=False
    )
    for table_name in EVIDENCE_REVIEW_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(REVIEW_GUARD_DDL))
    op.execute(sa.text(TRIAL_GUARD_DDL))
    op.alter_column(
        "operator_action",
        "action_type",
        existing_type=sa.String(length=30),
        type_=sa.String(length=80),
        existing_nullable=False,
    )
    op.drop_constraint("ck_operator_action_type", "operator_action", type_="check")
    op.create_check_constraint(
        "ck_operator_action_type",
        "operator_action",
        _operator_action_check(f"{PREVIOUS_OPERATOR_ACTIONS}, {EVIDENCE_REVIEW_ACTIONS}"),
    )


def downgrade() -> None:
    """Fail before DDL whenever review evidence or trial authority is retained."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM strategy_evidence_review_policy) "
            "OR EXISTS (SELECT 1 FROM strategy_evidence_snapshot) "
            "OR EXISTS (SELECT 1 FROM evidence_criterion_result) "
            "OR EXISTS (SELECT 1 FROM strategy_evidence_review) "
            "OR EXISTS (SELECT 1 FROM paper_trial_authorization) "
            "OR EXISTS (SELECT 1 FROM paper_trial_lineage) "
            "OR EXISTS (SELECT 1 FROM operator_action WHERE action_type IN ("
            + EVIDENCE_REVIEW_ACTIONS
            + ")) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0022 with retained evidence review or paper trial evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_paper_trial_authorization_trigger ON paper_trial_authorization"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_paper_trial_authorization()"))
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_strategy_evidence_review_trigger ON strategy_evidence_review"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_strategy_evidence_review()"))
    for table_name in EVIDENCE_REVIEW_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    evidence_review_metadata.drop_all(bind=op.get_bind(), tables=EVIDENCE_REVIEW_TABLES)
    op.drop_constraint("ck_operator_action_type", "operator_action", type_="check")
    op.alter_column(
        "operator_action",
        "action_type",
        existing_type=sa.String(length=80),
        type_=sa.String(length=30),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_operator_action_type",
        "operator_action",
        _operator_action_check(PREVIOUS_OPERATOR_ACTIONS),
    )
