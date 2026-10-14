"""Persist bounded automated candidate-search and local mirror evidence.

Revision ID: 0029_candidate_generation
Revises: 0028_research_overfitting
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    CANDIDATE_GENERATION_IMMUTABLE_TABLES,
    CANDIDATE_GENERATION_TABLES,
    candidate_generation_metadata,
    immutable_trigger_ddl,
)

revision: str = "0029_candidate_generation"
down_revision: str | None = "0028_research_overfitting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    candidate_generation_metadata.create_all(
        bind=op.get_bind(),
        tables=CANDIDATE_GENERATION_TABLES,
        checkfirst=False,
    )
    for table_name in CANDIDATE_GENERATION_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_candidate_search_plan() RETURNS trigger AS $$
            BEGIN
              IF OLD.search_plan_id <> NEW.search_plan_id
                 OR OLD.experiment_id <> NEW.experiment_id
                 OR OLD.experiment_hash <> NEW.experiment_hash
                 OR OLD.display_name <> NEW.display_name
                 OR OLD.objective_identity <> NEW.objective_identity
                 OR OLD.objective_version <> NEW.objective_version
                 OR OLD.objective_direction <> NEW.objective_direction
                 OR OLD.permitted_model_families <> NEW.permitted_model_families
                 OR OLD.parameter_space_payload <> NEW.parameter_space_payload
                 OR OLD.parameter_space_hash <> NEW.parameter_space_hash
                 OR OLD.theoretical_combinations <> NEW.theoretical_combinations
                 OR OLD.calibration_choices <> NEW.calibration_choices
                 OR OLD.feature_schema_id <> NEW.feature_schema_id
                 OR OLD.feature_schema_hash <> NEW.feature_schema_hash
                 OR OLD.dataset_id <> NEW.dataset_id
                 OR OLD.dataset_hash <> NEW.dataset_hash
                 OR OLD.fold_policy_id <> NEW.fold_policy_id
                 OR OLD.fold_policy_hash <> NEW.fold_policy_hash
                 OR OLD.random_seed <> NEW.random_seed
                 OR OLD.sampler_identity <> NEW.sampler_identity
                 OR OLD.sampler_version <> NEW.sampler_version
                 OR OLD.sampler_configuration <> NEW.sampler_configuration
                 OR OLD.sampler_hash <> NEW.sampler_hash
                 OR OLD.pruner_identity <> NEW.pruner_identity
                 OR OLD.pruner_version <> NEW.pruner_version
                 OR OLD.pruner_configuration <> NEW.pruner_configuration
                 OR OLD.pruner_hash <> NEW.pruner_hash
                 OR OLD.maximum_trials <> NEW.maximum_trials
                 OR OLD.maximum_failed_trials <> NEW.maximum_failed_trials
                 OR OLD.wall_clock_budget_seconds <> NEW.wall_clock_budget_seconds
                 OR OLD.per_trial_budget_seconds <> NEW.per_trial_budget_seconds
                 OR OLD.artifact_byte_budget <> NEW.artifact_byte_budget
                 OR OLD.concurrency_limit <> NEW.concurrency_limit
                 OR OLD.candidate_selection_policy_id <> NEW.candidate_selection_policy_id
                 OR OLD.candidate_selection_policy_hash <> NEW.candidate_selection_policy_hash
                 OR OLD.mlflow_mirror_policy_id <> NEW.mlflow_mirror_policy_id
                 OR OLD.mlflow_mirror_policy_hash <> NEW.mlflow_mirror_policy_hash
                 OR OLD.created_at <> NEW.created_at
                 OR OLD.opened_at <> NEW.opened_at
                 OR OLD.operator_action_id <> NEW.operator_action_id
                 OR OLD.operator_reason <> NEW.operator_reason
                 OR OLD.idempotency_key <> NEW.idempotency_key
                 OR OLD.canonical_input_hash <> NEW.canonical_input_hash
              THEN
                RAISE EXCEPTION 'candidate search plan scope and budgets are immutable';
              END IF;
              IF OLD.state <> NEW.state AND NOT (
                (OLD.state = 'READY' AND NEW.state IN ('RUNNING','CANCELED'))
                OR (OLD.state = 'RUNNING' AND NEW.state IN
                    ('COMPLETED','PARTIAL','CANCELED','FAILED','BLOCKED_INTEGRITY'))
              ) THEN
                RAISE EXCEPTION 'invalid candidate search plan state transition';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_candidate_search_plan_guard
              BEFORE UPDATE ON automated_candidate_search_plan
              FOR EACH ROW EXECUTE FUNCTION apex_guard_candidate_search_plan();
            CREATE TRIGGER apex_candidate_search_plan_no_delete
              BEFORE DELETE ON automated_candidate_search_plan
              FOR EACH ROW EXECUTE FUNCTION apex_reject_mutation();
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_candidate_search_run() RETURNS trigger AS $$
            BEGIN
              IF OLD.search_run_id <> NEW.search_run_id
                 OR OLD.search_plan_id <> NEW.search_plan_id
                 OR OLD.search_plan_hash <> NEW.search_plan_hash
                 OR OLD.experiment_id <> NEW.experiment_id
                 OR OLD.experiment_hash <> NEW.experiment_hash
                 OR OLD.optuna_study_identity <> NEW.optuna_study_identity
                 OR OLD.optuna_version <> NEW.optuna_version
                 OR OLD.mlflow_experiment_identity IS DISTINCT FROM NEW.mlflow_experiment_identity
                 OR OLD.requested_trial_count <> NEW.requested_trial_count
                 OR OLD.started_at <> NEW.started_at
                 OR OLD.idempotency_key <> NEW.idempotency_key
                 OR OLD.canonical_input_hash <> NEW.canonical_input_hash
              THEN
                RAISE EXCEPTION 'candidate search run input is immutable';
              END IF;
              IF NEW.reserved_trial_count < OLD.reserved_trial_count
                 OR NEW.completed_trial_count < OLD.completed_trial_count
                 OR NEW.pruned_trial_count < OLD.pruned_trial_count
                 OR NEW.failed_trial_count < OLD.failed_trial_count
                 OR NEW.integrity_rejected_trial_count < OLD.integrity_rejected_trial_count
                 OR NEW.artifact_bytes_written < OLD.artifact_bytes_written
              THEN
                RAISE EXCEPTION 'candidate search counters are monotonic';
              END IF;
              IF OLD.state <> NEW.state AND NOT (
                (OLD.state = 'REQUESTED' AND NEW.state IN ('RUNNING','CANCELED','FAILED'))
                OR (OLD.state = 'RUNNING' AND NEW.state IN
                    ('COMPLETED','PARTIAL','CANCELED','FAILED','BLOCKED_INTEGRITY'))
              ) THEN
                RAISE EXCEPTION 'invalid candidate search run state transition';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_candidate_search_run_guard
              BEFORE UPDATE ON automated_candidate_search_run
              FOR EACH ROW EXECUTE FUNCTION apex_guard_candidate_search_run();
            CREATE TRIGGER apex_candidate_search_run_no_delete
              BEFORE DELETE ON automated_candidate_search_run
              FOR EACH ROW EXECUTE FUNCTION apex_reject_mutation();
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_candidate_trial_binding() RETURNS trigger AS $$
            BEGIN
              IF OLD.binding_id <> NEW.binding_id
                 OR OLD.search_run_id <> NEW.search_run_id
                 OR OLD.optuna_trial_number <> NEW.optuna_trial_number
                 OR OLD.optimizer_token <> NEW.optimizer_token
                 OR OLD.research_trial_id <> NEW.research_trial_id
                 OR OLD.dataset_id <> NEW.dataset_id
                 OR OLD.feature_schema_id <> NEW.feature_schema_id
                 OR OLD.parameter_payload <> NEW.parameter_payload
                 OR OLD.parameter_hash <> NEW.parameter_hash
                 OR OLD.sampler_metadata <> NEW.sampler_metadata
                 OR OLD.canonical_input_hash <> NEW.canonical_input_hash
              THEN
                RAISE EXCEPTION 'candidate trial proposal binding is immutable';
              END IF;
              IF OLD.objective_state <> NEW.objective_state AND NOT (
                (OLD.objective_state = 'PROPOSED' AND NEW.objective_state IN
                    ('TRAINING','REJECTED_INTEGRITY','CANCELED'))
                OR (OLD.objective_state = 'TRAINING' AND NEW.objective_state IN
                    ('COMPLETED','PRUNED','FAILED','REJECTED_INTEGRITY','CANCELED'))
              ) THEN
                RAISE EXCEPTION 'invalid candidate trial objective transition';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_candidate_trial_binding_guard
              BEFORE UPDATE ON automated_candidate_trial_binding
              FOR EACH ROW EXECUTE FUNCTION apex_guard_candidate_trial_binding();
            CREATE TRIGGER apex_candidate_trial_binding_no_delete
              BEFORE DELETE ON automated_candidate_trial_binding
              FOR EACH ROW EXECUTE FUNCTION apex_reject_mutation();
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_mlflow_mirror_retry() RETURNS trigger AS $$
            BEGIN
              IF OLD.mirror_record_id <> NEW.mirror_record_id
                 OR OLD.source_type <> NEW.source_type
                 OR OLD.source_identity <> NEW.source_identity
                 OR OLD.source_hash <> NEW.source_hash
                 OR OLD.workspace_id <> NEW.workspace_id
                 OR OLD.mlflow_experiment_name <> NEW.mlflow_experiment_name
                 OR OLD.canonical_input_hash <> NEW.canonical_input_hash
                 OR NEW.retry_count < OLD.retry_count
              THEN
                RAISE EXCEPTION 'MLflow mirror source identity is immutable';
              END IF;
              IF OLD.state IN ('FAILED_PERMANENT','SKIPPED_DISABLED')
                 AND OLD.canonical_result_hash <> NEW.canonical_result_hash
              THEN
                RAISE EXCEPTION 'closed MLflow mirror record is immutable';
              END IF;
              IF OLD.state = 'MIRRORED' AND NEW.state <> 'MIRRORED' THEN
                RAISE EXCEPTION 'mirrored MLflow record cannot regress';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_mlflow_mirror_retry_guard
              BEFORE UPDATE ON mlflow_mirror_record
              FOR EACH ROW EXECUTE FUNCTION apex_guard_mlflow_mirror_retry();
            CREATE TRIGGER apex_mlflow_mirror_no_delete
              BEFORE DELETE ON mlflow_mirror_record
              FOR EACH ROW EXECUTE FUNCTION apex_reject_mutation();
            """
        )
    )


def downgrade() -> None:
    checks = " OR ".join(
        f'EXISTS (SELECT 1 FROM "{table.name}")' for table in CANDIDATE_GENERATION_TABLES
    )
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0029 with retained candidate search, Optuna binding, "
            "MLflow mirror, or candidate decision evidence'; END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_mlflow_mirror_no_delete ON mlflow_mirror_record; "
            "DROP TRIGGER apex_mlflow_mirror_retry_guard ON mlflow_mirror_record; "
            "DROP FUNCTION apex_guard_mlflow_mirror_retry(); "
            "DROP TRIGGER apex_candidate_trial_binding_no_delete "
            "ON automated_candidate_trial_binding; "
            "DROP TRIGGER apex_candidate_trial_binding_guard "
            "ON automated_candidate_trial_binding; "
            "DROP FUNCTION apex_guard_candidate_trial_binding(); "
            "DROP TRIGGER apex_candidate_search_run_no_delete "
            "ON automated_candidate_search_run; "
            "DROP TRIGGER apex_candidate_search_run_guard "
            "ON automated_candidate_search_run; "
            "DROP FUNCTION apex_guard_candidate_search_run(); "
            "DROP TRIGGER apex_candidate_search_plan_no_delete "
            "ON automated_candidate_search_plan; "
            "DROP TRIGGER apex_candidate_search_plan_guard "
            "ON automated_candidate_search_plan; "
            "DROP FUNCTION apex_guard_candidate_search_plan();"
        )
    )
    for table_name in reversed(CANDIDATE_GENERATION_IMMUTABLE_TABLES):
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table_name}_trigger ON "{table_name}"'))
    candidate_generation_metadata.drop_all(
        bind=op.get_bind(),
        tables=CANDIDATE_GENERATION_TABLES,
    )
