"""Persist bounded batch-learning orchestration and forward-window evidence.

Revision ID: 0030_batch_learning
Revises: 0029_candidate_generation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    BATCH_LEARNING_IMMUTABLE_TABLES,
    BATCH_LEARNING_TABLES,
    batch_learning_metadata,
    immutable_trigger_ddl,
)

revision: str = "0030_batch_learning"
down_revision: str | None = "0029_candidate_generation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    batch_learning_metadata.create_all(
        bind=op.get_bind(),
        tables=BATCH_LEARNING_TABLES,
        checkfirst=False,
    )
    for table_name in BATCH_LEARNING_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_batch_learning_plan() RETURNS trigger AS $$
            BEGIN
              IF OLD.plan_id <> NEW.plan_id
                 OR OLD.name <> NEW.name
                 OR OLD.vertical <> NEW.vertical
                 OR OLD.research_family <> NEW.research_family
                 OR OLD.source_adapter_id <> NEW.source_adapter_id
                 OR OLD.source_adapter_version <> NEW.source_adapter_version
                 OR OLD.source_scope <> NEW.source_scope
                 OR OLD.source_scope_hash <> NEW.source_scope_hash
                 OR OLD.feature_schema_id <> NEW.feature_schema_id
                 OR OLD.feature_schema_hash <> NEW.feature_schema_hash
                 OR OLD.dataset_policy_id <> NEW.dataset_policy_id
                 OR OLD.dataset_policy_hash <> NEW.dataset_policy_hash
                 OR OLD.experiment_template <> NEW.experiment_template
                 OR OLD.experiment_template_hash <> NEW.experiment_template_hash
                 OR OLD.permitted_model_families <> NEW.permitted_model_families
                 OR OLD.search_template <> NEW.search_template
                 OR OLD.search_template_hash <> NEW.search_template_hash
                 OR OLD.objective_policy_id <> NEW.objective_policy_id
                 OR OLD.objective_policy_hash <> NEW.objective_policy_hash
                 OR OLD.fold_policy_id <> NEW.fold_policy_id
                 OR OLD.fold_policy_hash <> NEW.fold_policy_hash
                 OR OLD.minimum_new_resolved_observations
                    <> NEW.minimum_new_resolved_observations
                 OR OLD.minimum_new_independent_groups
                    <> NEW.minimum_new_independent_groups
                 OR OLD.maximum_observations_per_cycle
                    <> NEW.maximum_observations_per_cycle
                 OR OLD.maximum_trials_per_cycle <> NEW.maximum_trials_per_cycle
                 OR OLD.maximum_failed_trials_per_cycle
                    <> NEW.maximum_failed_trials_per_cycle
                 OR OLD.maximum_cycles <> NEW.maximum_cycles
                 OR OLD.minimum_cooldown_seconds <> NEW.minimum_cooldown_seconds
                 OR OLD.drift_policy <> NEW.drift_policy
                 OR OLD.drift_policy_hash <> NEW.drift_policy_hash
                 OR OLD.forward_policy <> NEW.forward_policy
                 OR OLD.forward_policy_hash <> NEW.forward_policy_hash
                 OR OLD.champion_comparison_policy <> NEW.champion_comparison_policy
                 OR OLD.champion_comparison_policy_hash
                    <> NEW.champion_comparison_policy_hash
                 OR OLD.mlflow_mirror_policy_id <> NEW.mlflow_mirror_policy_id
                 OR OLD.mlflow_mirror_policy_hash <> NEW.mlflow_mirror_policy_hash
                 OR OLD.concurrency_limit <> NEW.concurrency_limit
                 OR OLD.created_at <> NEW.created_at
                 OR OLD.operator_action_id <> NEW.operator_action_id
                 OR OLD.idempotency_key <> NEW.idempotency_key
                 OR OLD.canonical_input_hash <> NEW.canonical_input_hash
              THEN
                RAISE EXCEPTION 'active batch learning plan scope and budgets are immutable';
              END IF;
              IF OLD.state <> NEW.state AND NOT (
                (OLD.state = 'DRAFT' AND NEW.state IN ('ACTIVE','REVOKED'))
                OR (OLD.state = 'ACTIVE' AND NEW.state IN
                    ('PAUSED','REVOKED','COMPLETED','BLOCKED_INTEGRITY'))
                OR (OLD.state = 'PAUSED' AND NEW.state IN
                    ('ACTIVE','REVOKED','BLOCKED_INTEGRITY'))
              ) THEN
                RAISE EXCEPTION 'invalid batch learning plan state transition';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_batch_learning_plan_guard
              BEFORE UPDATE ON batch_learning_plan
              FOR EACH ROW EXECUTE FUNCTION apex_guard_batch_learning_plan();
            CREATE TRIGGER apex_batch_learning_plan_no_delete
              BEFORE DELETE ON batch_learning_plan
              FOR EACH ROW EXECUTE FUNCTION apex_reject_mutation();
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_batch_learning_cycle() RETURNS trigger AS $$
            BEGIN
              IF OLD.cycle_id <> NEW.cycle_id
                 OR OLD.plan_id <> NEW.plan_id
                 OR OLD.ordinal <> NEW.ordinal
                 OR OLD.trigger_type <> NEW.trigger_type
                 OR OLD.trigger_evidence <> NEW.trigger_evidence
                 OR OLD.trigger_evidence_hash <> NEW.trigger_evidence_hash
                 OR OLD.evidence_window_id <> NEW.evidence_window_id
                 OR OLD.evidence_window_hash <> NEW.evidence_window_hash
                 OR OLD.idempotency_key <> NEW.idempotency_key
                 OR OLD.canonical_input_hash <> NEW.canonical_input_hash
              THEN
                RAISE EXCEPTION 'batch learning cycle trigger identity is immutable';
              END IF;
              IF OLD.dataset_id IS NOT NULL
                 AND (OLD.dataset_id IS DISTINCT FROM NEW.dataset_id
                      OR OLD.dataset_hash IS DISTINCT FROM NEW.dataset_hash)
              THEN
                RAISE EXCEPTION 'batch learning cycle dataset binding is immutable';
              END IF;
              IF OLD.experiment_id IS NOT NULL
                 AND (OLD.experiment_id IS DISTINCT FROM NEW.experiment_id
                      OR OLD.experiment_hash IS DISTINCT FROM NEW.experiment_hash)
              THEN
                RAISE EXCEPTION 'batch learning cycle experiment binding is immutable';
              END IF;
              IF OLD.search_run_id IS NOT NULL
                 AND OLD.search_run_id IS DISTINCT FROM NEW.search_run_id
              THEN
                RAISE EXCEPTION 'batch learning cycle search binding is immutable';
              END IF;
              IF OLD.state <> NEW.state AND NOT (
                (OLD.state = 'REQUESTED' AND NEW.state IN
                    ('BUILDING_DATASET','CANCELED','PAUSED','BLOCKED_INTEGRITY'))
                OR (OLD.state = 'BUILDING_DATASET' AND NEW.state IN
                    ('REQUESTED','SEARCHING','CANCELED','FAILED','BLOCKED_INTEGRITY'))
                OR (OLD.state = 'SEARCHING' AND NEW.state IN
                    ('REQUESTED','SEARCH_COMPLETED','NO_ELIGIBLE_CANDIDATE',
                     'ELIGIBLE_FOR_SHADOW_REVIEW','CANCELED','FAILED','BLOCKED_INTEGRITY'))
                OR (OLD.state = 'SEARCH_COMPLETED' AND NEW.state IN
                    ('NO_ELIGIBLE_CANDIDATE','ELIGIBLE_FOR_SHADOW_REVIEW',
                     'CANCELED','BLOCKED_INTEGRITY'))
                OR (OLD.state = 'ELIGIBLE_FOR_SHADOW_REVIEW' AND NEW.state IN
                    ('WAITING_FOR_SHADOW_APPROVAL','FORWARD_EVALUATING','CANCELED'))
                OR (OLD.state = 'WAITING_FOR_SHADOW_APPROVAL' AND NEW.state IN
                    ('FORWARD_EVALUATING','CANCELED'))
                OR (OLD.state = 'FORWARD_EVALUATING' AND NEW.state IN
                    ('COMPLETED','CANCELED','BLOCKED_INTEGRITY'))
                OR (OLD.state = 'PAUSED' AND NEW.state IN ('REQUESTED','CANCELED'))
              ) THEN
                RAISE EXCEPTION 'invalid batch learning cycle state transition';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_batch_learning_cycle_guard
              BEFORE UPDATE ON batch_learning_cycle
              FOR EACH ROW EXECUTE FUNCTION apex_guard_batch_learning_cycle();
            CREATE TRIGGER apex_batch_learning_cycle_no_delete
              BEFORE DELETE ON batch_learning_cycle
              FOR EACH ROW EXECUTE FUNCTION apex_reject_mutation();
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_batch_learning_binding() RETURNS trigger AS $$
            BEGIN
              IF OLD.binding_id <> NEW.binding_id
                 OR OLD.cycle_id <> NEW.cycle_id
                 OR OLD.evidence_window_id <> NEW.evidence_window_id
                 OR OLD.dataset_id <> NEW.dataset_id
                 OR OLD.experiment_id <> NEW.experiment_id
                 OR OLD.research_trial_ids <> NEW.research_trial_ids
                 OR OLD.search_plan_id <> NEW.search_plan_id
                 OR OLD.search_run_id <> NEW.search_run_id
                 OR OLD.candidate_ids <> NEW.candidate_ids
                 OR OLD.candidate_decision_id <> NEW.candidate_decision_id
              THEN
                RAISE EXCEPTION 'batch learning core cycle bindings are immutable';
              END IF;
              IF OLD.approved_deployment_id IS NOT NULL
                 AND OLD.approved_deployment_id IS DISTINCT FROM NEW.approved_deployment_id
              THEN
                RAISE EXCEPTION 'approved batch challenger binding is immutable';
              END IF;
              IF NOT OLD.inference_batch_ids <@ NEW.inference_batch_ids
                 OR NOT OLD.forward_evaluation_ids <@ NEW.forward_evaluation_ids
                 OR NOT OLD.mlflow_record_ids <@ NEW.mlflow_record_ids
              THEN
                RAISE EXCEPTION 'batch learning evidence bindings are append-only';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_batch_learning_binding_guard
              BEFORE UPDATE ON batch_learning_cycle_binding
              FOR EACH ROW EXECUTE FUNCTION apex_guard_batch_learning_binding();
            CREATE TRIGGER apex_batch_learning_binding_no_delete
              BEFORE DELETE ON batch_learning_cycle_binding
              FOR EACH ROW EXECUTE FUNCTION apex_reject_mutation();
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_forward_shadow_window() RETURNS trigger AS $$
            BEGIN
              IF OLD.window_id <> NEW.window_id
                 OR OLD.cycle_id <> NEW.cycle_id
                 OR OLD.champion_deployment_id IS DISTINCT FROM NEW.champion_deployment_id
                 OR OLD.champion_deployment_hash IS DISTINCT FROM NEW.champion_deployment_hash
                 OR OLD.challenger_deployment_id <> NEW.challenger_deployment_id
                 OR OLD.challenger_deployment_hash <> NEW.challenger_deployment_hash
                 OR OLD.source_scope <> NEW.source_scope
                 OR OLD.source_scope_hash <> NEW.source_scope_hash
                 OR OLD.window_start_at <> NEW.window_start_at
                 OR OLD.window_end_at <> NEW.window_end_at
                 OR OLD.minimum_predictions <> NEW.minimum_predictions
                 OR OLD.minimum_resolved_outcomes <> NEW.minimum_resolved_outcomes
                 OR OLD.maximum_duration_seconds <> NEW.maximum_duration_seconds
                 OR OLD.canonical_input_hash <> NEW.canonical_input_hash
              THEN
                RAISE EXCEPTION 'forward shadow evaluation window scope is immutable';
              END IF;
              IF NEW.champion_prediction_count < OLD.champion_prediction_count
                 OR NEW.challenger_prediction_count < OLD.challenger_prediction_count
                 OR NEW.champion_resolved_count < OLD.champion_resolved_count
                 OR NEW.challenger_resolved_count < OLD.challenger_resolved_count
                 OR NEW.unmatched_count < OLD.unmatched_count
                 OR NEW.excluded_count < OLD.excluded_count
              THEN
                RAISE EXCEPTION 'forward shadow evidence counters are monotonic';
              END IF;
              IF OLD.state IN ('COMPLETED','BLOCKED_INTEGRITY','CANCELED')
                 AND OLD.canonical_result_hash IS DISTINCT FROM NEW.canonical_result_hash
              THEN
                RAISE EXCEPTION 'terminal forward shadow window is immutable';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_forward_shadow_window_guard
              BEFORE UPDATE ON forward_shadow_evaluation_window
              FOR EACH ROW EXECUTE FUNCTION apex_guard_forward_shadow_window();
            CREATE TRIGGER apex_forward_shadow_window_no_delete
              BEFORE DELETE ON forward_shadow_evaluation_window
              FOR EACH ROW EXECUTE FUNCTION apex_reject_mutation();
            """
        )
    )


def downgrade() -> None:
    checks = " OR ".join(
        f'EXISTS (SELECT 1 FROM "{table.name}")' for table in BATCH_LEARNING_TABLES
    )
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0030 with retained batch learning plans, evidence "
            "windows, cycles, bindings, forward evaluations, or decisions'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_forward_shadow_window_no_delete "
            "ON forward_shadow_evaluation_window; "
            "DROP TRIGGER apex_forward_shadow_window_guard "
            "ON forward_shadow_evaluation_window; "
            "DROP FUNCTION apex_guard_forward_shadow_window(); "
            "DROP TRIGGER apex_batch_learning_binding_no_delete "
            "ON batch_learning_cycle_binding; "
            "DROP TRIGGER apex_batch_learning_binding_guard "
            "ON batch_learning_cycle_binding; "
            "DROP FUNCTION apex_guard_batch_learning_binding(); "
            "DROP TRIGGER apex_batch_learning_cycle_no_delete "
            "ON batch_learning_cycle; "
            "DROP TRIGGER apex_batch_learning_cycle_guard "
            "ON batch_learning_cycle; "
            "DROP FUNCTION apex_guard_batch_learning_cycle(); "
            "DROP TRIGGER apex_batch_learning_plan_no_delete "
            "ON batch_learning_plan; "
            "DROP TRIGGER apex_batch_learning_plan_guard "
            "ON batch_learning_plan; "
            "DROP FUNCTION apex_guard_batch_learning_plan();"
        )
    )
    for table_name in reversed(BATCH_LEARNING_IMMUTABLE_TABLES):
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table_name}_trigger ON "{table_name}"'))
    batch_learning_metadata.drop_all(bind=op.get_bind(), tables=BATCH_LEARNING_TABLES)
