"""Persist governed research families and multiple-testing evidence.

Revision ID: 0028_research_overfitting
Revises: 0027_securities_baselines
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    RESEARCH_INTEGRITY_IMMUTABLE_TABLES,
    RESEARCH_INTEGRITY_TABLES,
    immutable_trigger_ddl,
    research_integrity_metadata,
)

revision: str = "0028_research_overfitting"
down_revision: str | None = "0027_securities_baselines"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    research_integrity_metadata.create_all(
        bind=op.get_bind(),
        tables=RESEARCH_INTEGRITY_TABLES,
        checkfirst=False,
    )
    for table_name in RESEARCH_INTEGRITY_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_research_family_scope() RETURNS trigger AS $$
            BEGIN
              IF OLD.experiment_id <> NEW.experiment_id
                 OR OLD.name <> NEW.name
                 OR OLD.vertical <> NEW.vertical
                 OR OLD.research_family <> NEW.research_family
                 OR OLD.research_question <> NEW.research_question
                 OR OLD.target_label_identity <> NEW.target_label_identity
                 OR OLD.benchmark_identities <> NEW.benchmark_identities
                 OR OLD.data_scope_identities <> NEW.data_scope_identities
                 OR OLD.data_scope_hashes <> NEW.data_scope_hashes
                 OR OLD.universe_hash <> NEW.universe_hash
                 OR OLD.period_start <> NEW.period_start
                 OR OLD.period_end <> NEW.period_end
                 OR OLD.cutoff_at <> NEW.cutoff_at
                 OR OLD.feature_schema_hashes <> NEW.feature_schema_hashes
                 OR OLD.permitted_families <> NEW.permitted_families
                 OR OLD.parameter_space_payload <> NEW.parameter_space_payload
                 OR OLD.parameter_space_hash <> NEW.parameter_space_hash
                 OR OLD.maximum_trials <> NEW.maximum_trials
                 OR OLD.maximum_feature_variants <> NEW.maximum_feature_variants
                 OR OLD.maximum_model_variants <> NEW.maximum_model_variants
                 OR OLD.maximum_simulator_variants <> NEW.maximum_simulator_variants
                 OR OLD.evaluation_policy_id <> NEW.evaluation_policy_id
                 OR OLD.evaluation_policy_hash <> NEW.evaluation_policy_hash
                 OR OLD.cost_execution_assumptions <> NEW.cost_execution_assumptions
                 OR OLD.cost_execution_hash <> NEW.cost_execution_hash
                 OR OLD.frequency <> NEW.frequency
                 OR OLD.created_at <> NEW.created_at
                 OR OLD.opened_at IS DISTINCT FROM NEW.opened_at
                 OR OLD.operator_action_id <> NEW.operator_action_id
                 OR OLD.operator_reason <> NEW.operator_reason
                 OR OLD.idempotency_key <> NEW.idempotency_key
                 OR OLD.canonical_input_hash <> NEW.canonical_input_hash
              THEN
                RAISE EXCEPTION 'research experiment scope and budget are immutable';
              END IF;
              IF OLD.state <> NEW.state AND NOT (
                (OLD.state = 'DRAFT' AND NEW.state IN ('OPEN','CANCELED'))
                OR (OLD.state = 'OPEN' AND NEW.state IN ('CLOSED','CANCELED','INVALIDATED'))
              ) THEN
                RAISE EXCEPTION 'invalid research experiment state transition';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_research_family_scope_trigger
              BEFORE UPDATE ON research_experiment_family
              FOR EACH ROW EXECUTE FUNCTION apex_guard_research_family_scope();
            CREATE TRIGGER apex_research_family_no_delete_trigger
              BEFORE DELETE ON research_experiment_family
              FOR EACH ROW EXECUTE FUNCTION apex_reject_mutation();
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION apex_guard_research_trial_transition() RETURNS trigger AS $$
            BEGIN
              IF OLD.trial_id <> NEW.trial_id
                 OR OLD.experiment_id <> NEW.experiment_id
                 OR OLD.parent_trial_id IS DISTINCT FROM NEW.parent_trial_id
                 OR OLD.trial_ordinal <> NEW.trial_ordinal
                 OR OLD.source_type <> NEW.source_type
                 OR OLD.source_identity <> NEW.source_identity
                 OR OLD.source_hash <> NEW.source_hash
                 OR OLD.strategy_model_family <> NEW.strategy_model_family
                 OR OLD.configuration_payload <> NEW.configuration_payload
                 OR OLD.configuration_hash <> NEW.configuration_hash
                 OR OLD.feature_set_hash <> NEW.feature_set_hash
                 OR OLD.dataset_hash <> NEW.dataset_hash
                 OR OLD.simulator_identity <> NEW.simulator_identity
                 OR OLD.simulator_version <> NEW.simulator_version
                 OR OLD.execution_cost_policy_hash <> NEW.execution_cost_policy_hash
                 OR OLD.random_seed IS DISTINCT FROM NEW.random_seed
                 OR OLD.started_at <> NEW.started_at
                 OR OLD.selected_for_comparison <> NEW.selected_for_comparison
                 OR OLD.idempotency_key <> NEW.idempotency_key
                 OR OLD.canonical_input_hash <> NEW.canonical_input_hash
              THEN
                RAISE EXCEPTION 'research trial input and ordinal are immutable';
              END IF;
              IF OLD.state <> 'REQUESTED'
                 OR NEW.state NOT IN ('COMPLETED','FAILED','REJECTED_INTEGRITY','CANCELED')
                 OR NEW.completed_at IS NULL
                 OR NEW.canonical_result_hash IS NULL
              THEN
                RAISE EXCEPTION 'research trial permits one requested-to-terminal transition';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER apex_research_trial_transition_trigger
              BEFORE UPDATE ON research_trial_attempt
              FOR EACH ROW EXECUTE FUNCTION apex_guard_research_trial_transition();
            CREATE TRIGGER apex_research_trial_no_delete_trigger
              BEFORE DELETE ON research_trial_attempt
              FOR EACH ROW EXECUTE FUNCTION apex_reject_mutation();
            """
        )
    )


def downgrade() -> None:
    """Refuse before DDL whenever retained research-integrity evidence exists."""

    checks = " OR ".join(
        f'EXISTS (SELECT 1 FROM "{table.name}")' for table in RESEARCH_INTEGRITY_TABLES
    )
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0028 with retained research experiment, trial, "
            "return-series, metric, selection, partition, or multiple-testing evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_research_trial_no_delete_trigger ON research_trial_attempt; "
            "DROP TRIGGER apex_research_trial_transition_trigger ON research_trial_attempt; "
            "DROP FUNCTION apex_guard_research_trial_transition(); "
            "DROP TRIGGER apex_research_family_no_delete_trigger ON research_experiment_family; "
            "DROP TRIGGER apex_research_family_scope_trigger ON research_experiment_family; "
            "DROP FUNCTION apex_guard_research_family_scope();"
        )
    )
    for table_name in reversed(RESEARCH_INTEGRITY_IMMUTABLE_TABLES):
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table_name}_trigger ON "{table_name}"'))
    research_integrity_metadata.drop_all(
        bind=op.get_bind(),
        tables=RESEARCH_INTEGRITY_TABLES,
    )
