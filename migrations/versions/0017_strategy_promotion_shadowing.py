# ruff: noqa: E501
"""Persist controlled strategy promotion and forward-only SHADOW evidence.

Revision ID: 0017_strategy_shadowing
Revises: 0016_operator_console
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    STRATEGY_SHADOW_IMMUTABLE_TABLES,
    STRATEGY_SHADOW_TABLES,
    immutable_trigger_ddl,
    strategy_shadow_metadata,
)

revision: str = "0017_strategy_shadowing"
down_revision: str | None = "0016_operator_console"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SHADOW_SESSION_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_shadow_observation_session() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'shadow observation session cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'completed_at', 'state', 'completed_iterations', 'failed_iterations',
        'canonical_result_hash', 'last_error_code'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'completed_at', 'state', 'completed_iterations', 'failed_iterations',
        'canonical_result_hash', 'last_error_code'
    ]) THEN
        RAISE EXCEPTION 'shadow observation session configuration is immutable';
    END IF;
    IF OLD.state <> 'RUNNING' THEN
        RAISE EXCEPTION 'handled shadow observation session is immutable';
    END IF;
    IF NEW.state = 'COMPLETED' THEN
        IF NEW.completed_at IS NULL OR NEW.canonical_result_hash IS NULL
           OR NEW.completed_iterations + NEW.failed_iterations <> NEW.iterations_requested THEN
            RAISE EXCEPTION 'completed shadow observation session requires a complete result';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state = 'PARTIAL' THEN
        IF NEW.completed_at IS NULL OR NEW.canonical_result_hash IS NULL
           OR NEW.failed_iterations < 1 OR NEW.last_error_code IS NULL
           OR NEW.completed_iterations + NEW.failed_iterations > NEW.iterations_requested THEN
            RAISE EXCEPTION 'partial shadow observation session requires a retained failure';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state IN ('FAILED', 'INTERRUPTED') THEN
        IF NEW.completed_at IS NULL OR NEW.last_error_code IS NULL THEN
            RAISE EXCEPTION 'failed shadow observation session requires a handled error';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'shadow observation session transition is invalid';
END;
$$;

CREATE TRIGGER apex_guard_shadow_observation_session_trigger
BEFORE UPDATE OR DELETE ON shadow_observation_session
FOR EACH ROW EXECUTE FUNCTION apex_guard_shadow_observation_session();
"""

SHADOW_PREDICTION_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_shadow_prediction() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'shadow prediction cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY['state']) <> (to_jsonb(NEW) - ARRAY['state']) THEN
        RAISE EXCEPTION 'shadow prediction terms and probabilities are immutable';
    END IF;
    IF OLD.state = 'UNRESOLVED' AND NEW.state = 'RESOLVED' THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'shadow prediction state transition is invalid';
END;
$$;

CREATE TRIGGER apex_guard_shadow_prediction_trigger
BEFORE UPDATE OR DELETE ON shadow_prediction
FOR EACH ROW EXECUTE FUNCTION apex_guard_shadow_prediction();
"""


# Keep the existing PAPER evidence conversion, but preserve the durable expiry
# invariants for synthetic resting orders at the new head revision.  The
# matching pre-0017 guard is restored explicitly during downgrade below.
STRATEGY_SHADOW_ORDER_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_order_execution_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE e record; loss numeric(38,18); auth_at timestamptz;
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'durable order cannot be deleted'; END IF;
  IF (to_jsonb(OLD) - ARRAY['filled_quantity','reserved_maximum_loss','state','last_execution_sequence','row_version','execution_evidence','canonical_payload_hash'])
     <> (to_jsonb(NEW) - ARRAY['filled_quantity','reserved_maximum_loss','state','last_execution_sequence','row_version','execution_evidence','canonical_payload_hash'])
  THEN RAISE EXCEPTION 'immutable order terms cannot change'; END IF;
  IF OLD.state = 'READY' AND NEW.state = 'READY' AND OLD.execution_evidence = 'SYNTHETIC' AND NEW.execution_evidence = 'PAPER'
     AND OLD.last_execution_sequence = NEW.last_execution_sequence AND OLD.row_version = NEW.row_version
  THEN RETURN NEW; END IF;
  SELECT event_type, occurred_at, fill_quantity, fill_price INTO e FROM execution_event
    WHERE order_id = NEW.order_id AND sequence = NEW.last_execution_sequence;
  SELECT maximum_loss_per_unit, authorization_at INTO loss, auth_at FROM opportunity_proposal p
    JOIN risk_decision d ON d.risk_decision_id = NEW.risk_decision_id WHERE p.proposal_id = NEW.proposal_id;
  IF e.event_type IS NULL THEN RAISE EXCEPTION 'order transition requires immutable execution event'; END IF;
  IF NEW.state = 'OPEN' THEN
    IF NOT (OLD.state='READY' AND NEW.last_execution_sequence=1 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=0 AND NEW.reserved_maximum_loss=OLD.reserved_maximum_loss
      AND e.event_type='OPENED' AND e.occurred_at >= auth_at
      AND (NEW.execution_evidence = 'PAPER' OR (
        NEW.expires_at IS NOT NULL AND e.occurred_at < NEW.expires_at)))
    THEN RAISE EXCEPTION 'invalid durable order open'; END IF;
  ELSIF NEW.state = 'EXPIRED' THEN
    IF NOT (OLD.state='OPEN' AND NEW.last_execution_sequence=2 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=0 AND NEW.reserved_maximum_loss=0 AND e.event_type='EXPIRED'
      AND (NEW.execution_evidence = 'PAPER' OR (
        NEW.expires_at IS NOT NULL AND e.occurred_at = NEW.expires_at)))
    THEN RAISE EXCEPTION 'invalid durable order expiration'; END IF;
  ELSIF NEW.state = 'PARTIALLY_FILLED' THEN
    IF NOT (OLD.state='OPEN' AND NEW.last_execution_sequence=2 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity>0 AND NEW.filled_quantity<NEW.quantity AND NEW.reserved_maximum_loss=loss*(NEW.quantity-NEW.filled_quantity)
      AND e.event_type='FILL' AND e.fill_quantity=NEW.filled_quantity AND e.fill_price<=NEW.limit_price
      AND (NEW.execution_evidence = 'PAPER' OR (
        NEW.expires_at IS NOT NULL AND e.occurred_at < NEW.expires_at)))
    THEN RAISE EXCEPTION 'invalid durable partial fill'; END IF;
  ELSIF NEW.state = 'CANCELED' THEN
    IF NOT (OLD.state='PARTIALLY_FILLED' AND NEW.last_execution_sequence=3 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=OLD.filled_quantity AND NEW.reserved_maximum_loss=0
      AND e.event_type='CANCELED' AND (NEW.execution_evidence = 'PAPER' OR (
        NEW.expires_at IS NOT NULL AND e.occurred_at < NEW.expires_at)))
    THEN RAISE EXCEPTION 'invalid durable cancellation'; END IF;
  ELSIF NEW.state = 'FILLED' THEN
    IF NOT (OLD.state='OPEN' AND NEW.last_execution_sequence=2 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=NEW.quantity AND NEW.reserved_maximum_loss=0 AND e.event_type='FILL'
      AND e.fill_quantity=NEW.quantity AND e.fill_price<=NEW.limit_price)
    THEN RAISE EXCEPTION 'invalid durable full fill'; END IF;
  ELSE RAISE EXCEPTION 'invalid durable order transition'; END IF;
  RETURN NEW;
END;
$$;
"""


PAPER_ORDER_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_order_execution_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE e record; loss numeric(38,18); auth_at timestamptz;
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'durable order cannot be deleted'; END IF;
  IF (to_jsonb(OLD) - ARRAY['filled_quantity','reserved_maximum_loss','state','last_execution_sequence','row_version','execution_evidence','canonical_payload_hash'])
     <> (to_jsonb(NEW) - ARRAY['filled_quantity','reserved_maximum_loss','state','last_execution_sequence','row_version','execution_evidence','canonical_payload_hash'])
  THEN RAISE EXCEPTION 'immutable order terms cannot change'; END IF;
  IF OLD.state = 'READY' AND NEW.state = 'READY' AND OLD.execution_evidence = 'SYNTHETIC' AND NEW.execution_evidence = 'PAPER'
     AND OLD.last_execution_sequence = NEW.last_execution_sequence AND OLD.row_version = NEW.row_version
  THEN RETURN NEW; END IF;
  SELECT event_type, occurred_at, fill_quantity, fill_price INTO e FROM execution_event
    WHERE order_id = NEW.order_id AND sequence = NEW.last_execution_sequence;
  SELECT maximum_loss_per_unit, authorization_at INTO loss, auth_at FROM opportunity_proposal p
    JOIN risk_decision d ON d.risk_decision_id = NEW.risk_decision_id WHERE p.proposal_id = NEW.proposal_id;
  IF e.event_type IS NULL THEN RAISE EXCEPTION 'order transition requires immutable execution event'; END IF;
  IF NEW.state = 'OPEN' THEN
    IF NOT (OLD.state='READY' AND NEW.last_execution_sequence=1 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=0 AND NEW.reserved_maximum_loss=OLD.reserved_maximum_loss
      AND e.event_type='OPENED' AND e.occurred_at >= auth_at) THEN RAISE EXCEPTION 'invalid durable order open'; END IF;
  ELSIF NEW.state = 'EXPIRED' THEN
    IF NOT (OLD.state='OPEN' AND NEW.last_execution_sequence=2 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=0 AND NEW.reserved_maximum_loss=0 AND e.event_type='EXPIRED') THEN RAISE EXCEPTION 'invalid durable order expiration'; END IF;
  ELSIF NEW.state = 'PARTIALLY_FILLED' THEN
    IF NOT (OLD.state='OPEN' AND NEW.last_execution_sequence=2 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity>0 AND NEW.filled_quantity<NEW.quantity AND NEW.reserved_maximum_loss=loss*(NEW.quantity-NEW.filled_quantity)
      AND e.event_type='FILL' AND e.fill_quantity=NEW.filled_quantity AND e.fill_price<=NEW.limit_price) THEN RAISE EXCEPTION 'invalid durable partial fill'; END IF;
  ELSIF NEW.state = 'CANCELED' THEN
    IF NOT (OLD.state='PARTIALLY_FILLED' AND NEW.last_execution_sequence=3 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=OLD.filled_quantity AND NEW.reserved_maximum_loss=0 AND e.event_type='CANCELED') THEN RAISE EXCEPTION 'invalid durable cancellation'; END IF;
  ELSIF NEW.state = 'FILLED' THEN
    IF NOT (OLD.state='OPEN' AND NEW.last_execution_sequence=2 AND NEW.row_version=OLD.row_version+1
      AND NEW.filled_quantity=NEW.quantity AND NEW.reserved_maximum_loss=0 AND e.event_type='FILL'
      AND e.fill_quantity=NEW.quantity AND e.fill_price<=NEW.limit_price) THEN RAISE EXCEPTION 'invalid durable full fill'; END IF;
  ELSE RAISE EXCEPTION 'invalid durable order transition'; END IF;
  RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    """Add only append-only governance, forecast, scoring, and session evidence."""

    op.drop_constraint("ck_operator_action_type", "operator_action", type_="check")
    op.create_check_constraint(
        "ck_operator_action_type",
        "operator_action",
        "action_type IN ('CREATE_CAMPAIGN', 'RUN_SHADOW_CYCLE', 'RUN_PAPER_CYCLE', "
        "'PAUSE_CAMPAIGN', 'RESUME_CAMPAIGN', 'KILL_CAMPAIGN', 'MARK_POSITIONS', "
        "'SETTLE_POSITION', 'SUBMIT_THESIS', 'EVALUATE_PREDICTION', "
        "'CHANGE_STRATEGY_AUTHORITY', 'RUN_LIVE_SHADOW_SESSION')",
    )
    strategy_shadow_metadata.create_all(
        bind=op.get_bind(), tables=STRATEGY_SHADOW_TABLES, checkfirst=False
    )
    for table_name in STRATEGY_SHADOW_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(STRATEGY_SHADOW_ORDER_GUARD_DDL))
    op.execute(sa.text(SHADOW_PREDICTION_GUARD_DDL))
    op.execute(sa.text(SHADOW_SESSION_GUARD_DDL))


def downgrade() -> None:
    """Refuse before DDL whenever retained forward evidence would be lost."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM strategy_authority_decision) "
            "OR EXISTS (SELECT 1 FROM shadow_prediction) "
            "OR EXISTS (SELECT 1 FROM shadow_outcome_evaluation) "
            "OR EXISTS (SELECT 1 FROM shadow_observation_session) "
            "OR EXISTS (SELECT 1 FROM shadow_observation_iteration) "
            "OR EXISTS (SELECT 1 FROM operator_action WHERE action_type IN "
            "('SUBMIT_THESIS', 'EVALUATE_PREDICTION', 'CHANGE_STRATEGY_AUTHORITY', "
            "'RUN_LIVE_SHADOW_SESSION')) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0017 with retained strategy-promotion shadow evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(sa.text("DROP TRIGGER apex_guard_shadow_prediction_trigger ON shadow_prediction"))
    op.execute(sa.text("DROP FUNCTION apex_guard_shadow_prediction()"))
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_shadow_observation_session_trigger "
            "ON shadow_observation_session"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_shadow_observation_session()"))
    for table_name in STRATEGY_SHADOW_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    strategy_shadow_metadata.drop_all(bind=op.get_bind(), tables=STRATEGY_SHADOW_TABLES)
    op.execute(sa.text(PAPER_ORDER_GUARD_DDL))
    op.drop_constraint("ck_operator_action_type", "operator_action", type_="check")
    op.create_check_constraint(
        "ck_operator_action_type",
        "operator_action",
        "action_type IN ('CREATE_CAMPAIGN', 'RUN_SHADOW_CYCLE', 'RUN_PAPER_CYCLE', "
        "'PAUSE_CAMPAIGN', 'RESUME_CAMPAIGN', 'KILL_CAMPAIGN', 'MARK_POSITIONS', "
        "'SETTLE_POSITION')",
    )
