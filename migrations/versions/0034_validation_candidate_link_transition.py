# ruff: noqa: E501
"""Permit one completed real-data validation to retain later research blockers.

Revision ID: 0034_validation_candidate_link
Revises: 0033_genuine_securities_dataset
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_validation_candidate_link"
down_revision: str | None = "0033_genuine_securities_dataset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_GUARD = r"""
CREATE OR REPLACE FUNCTION apex_guard_securities_historical_validation_run() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'historical validation run cannot be deleted'; END IF;
    IF (to_jsonb(OLD) - ARRAY['state','baseline_run_ids','scorecard_ids','simulator_conformance_ids','dataset_id','dataset_hash','experiment_id','experiment_hash','candidate_search_run_id','multiple_testing_evaluation_id','recommendation','reason_codes','completed_at','canonical_result_hash'])
       <> (to_jsonb(NEW) - ARRAY['state','baseline_run_ids','scorecard_ids','simulator_conformance_ids','dataset_id','dataset_hash','experiment_id','experiment_hash','candidate_search_run_id','multiple_testing_evaluation_id','recommendation','reason_codes','completed_at','canonical_result_hash']) THEN
        RAISE EXCEPTION 'historical validation inputs are immutable';
    END IF;
    IF OLD.state = 'REQUESTED' AND NEW.state IN ('RUNNING','FAILED','BLOCKED_INTEGRITY') THEN RETURN NEW; END IF;
    IF OLD.state = 'RUNNING' AND NEW.state IN ('COMPLETED','FAILED','BLOCKED_INTEGRITY') THEN RETURN NEW; END IF;
    IF OLD.state = 'COMPLETED' AND NEW.state = 'BLOCKED_INTEGRITY'
       AND NEW.dataset_id IS NOT NULL AND NEW.experiment_id IS NOT NULL
       AND NEW.candidate_search_run_id IS NOT NULL AND NEW.multiple_testing_evaluation_id IS NOT NULL
       AND NEW.recommendation = 'BLOCKED_SIMULATOR_MISMATCH' THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'historical validation transition is invalid';
END;
$$;
"""


def upgrade() -> None:
    op.execute(sa.text(_GUARD))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM securities_historical_validation_run "
            "WHERE state = 'BLOCKED_INTEGRITY' AND candidate_search_run_id IS NOT NULL) "
            "THEN RAISE EXCEPTION 'cannot downgrade 0034 with retained candidate validation linkage'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION apex_guard_securities_historical_validation_run() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'historical validation run cannot be deleted'; END IF; "
            "IF (to_jsonb(OLD) - ARRAY['state','baseline_run_ids','scorecard_ids','simulator_conformance_ids','dataset_id','dataset_hash','experiment_id','experiment_hash','candidate_search_run_id','multiple_testing_evaluation_id','recommendation','reason_codes','completed_at','canonical_result_hash']) <> (to_jsonb(NEW) - ARRAY['state','baseline_run_ids','scorecard_ids','simulator_conformance_ids','dataset_id','dataset_hash','experiment_id','experiment_hash','candidate_search_run_id','multiple_testing_evaluation_id','recommendation','reason_codes','completed_at','canonical_result_hash']) THEN RAISE EXCEPTION 'historical validation inputs are immutable'; END IF; "
            "IF OLD.state = 'REQUESTED' AND NEW.state IN ('RUNNING','FAILED','BLOCKED_INTEGRITY') THEN RETURN NEW; END IF; "
            "IF OLD.state = 'RUNNING' AND NEW.state IN ('COMPLETED','FAILED','BLOCKED_INTEGRITY') THEN RETURN NEW; END IF; "
            "RAISE EXCEPTION 'historical validation transition is invalid'; END; $$;"
        )
    )
