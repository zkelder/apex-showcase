"""Strategy PAPER Trial Outcome Assessment v1.

Adds one immutable, append-only table, ``strategy_trial_outcome_assessment``
(see ``domain.strategy_trial_outcome_assessment`` and
``docs/STRATEGY_TRIAL_OUTCOME_ASSESSMENT_V1.md``): one row per closed
strategy PAPER trial and assessment policy version, binding the trial's
realized outcome to its exact retained lineage.

Composite foreign keys make the lineage a database fact, not an inference:

* the trial row, including its ``legacy_adopted`` flag and record hash;
* the trial's own closure row, including terminal reason, close time, and
  record hash;
* for a provenance-bound trial, its own ``strategy_paper_trial_provenance``
  row (transition, registry, version, definition hash, record hash), the
  transition's exact justifying evaluation, and that evaluation row itself;
* the exact final attribution rows (id and record hash).

CHECK constraints make a legacy trial's row carry no strategy version,
transition, evaluation, or population, and restate the status/economics/
sample-floor pairing the domain enforces.

Additive only. No existing row or column changes. Four UNIQUE constraints are
added to existing tables purely as composite foreign-key targets; each is a
superset of that table's primary key, so it holds for every retained row.

The revision id is shortened from the descriptive file name to fit
``alembic_version.version_num`` (VARCHAR(32)), as 0086 did.

Downgrade refuses while any assessment is retained.

Revision ID: 0117_trial_outcome_assessment
Revises: 0116_securities_followup_lag
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0117_trial_outcome_assessment"
down_revision: str | None = "0116_securities_followup_lag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "strategy_trial_outcome_assessment"
POLICY_V1 = "STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1"

_STATUSES = (
    "PREDATES_STRATEGY_PROVENANCE",
    "PROVENANCE_MISSING",
    "NO_ECONOMIC_OUTCOME",
    "ECONOMICS_UNRECONCILED",
    "INSUFFICIENT_SAMPLE",
    "ASSESSED",
)
_PROVENANCE_CLASSES = ("BOUND", "PREDATES_STRATEGY_PROVENANCE", "PROVENANCE_MISSING")
_ECONOMICS_STATES = ("RECONCILED", "UNRECONCILED", "NO_EXPOSURE")
_TERMINAL_REASONS = ("TRIAL_HORIZON_EXIT", "OPENING_ORDER_EXPIRED_UNFILLED")
_EXPECTED_BASES = (
    "EVALUATION_WINDOW_NET_RETURN_NOT_HORIZON_MATCHED",
    "NO_JUSTIFYING_EVALUATION_RETAINED",
)
_BOUND_COLUMNS = (
    "version_registry",
    "definition_hash",
    "provenance_hash",
    "transition_id",
    "transition_hash",
    "transition_kind",
    "gate_policy_version",
    "evaluation_source",
    "evaluation_id",
    "evaluation_hash",
    "evaluated_version",
    "evaluation_as_of",
    "evaluation_kind",
    "evaluation_window_end",
    "evaluation_sample_size",
    "population_id",
    "population_members_hash",
)

# (existing table, constraint name, columns) -- composite-FK targets only.
_EXISTING_TABLE_UNIQUES = (
    (
        "internal_strategy_paper_trial_closure",
        "uq_internal_strategy_paper_trial_closure_outcome_target",
        ("closure_id", "trial_id", "terminal_reason", "closed_at", "canonical_hash"),
    ),
    (
        "strategy_paper_trial_provenance",
        "uq_strategy_paper_trial_provenance_outcome_target",
        (
            "trial_id",
            "transition_id",
            "registry",
            "strategy_id",
            "strategy_version",
            "definition_hash",
            "canonical_hash",
        ),
    ),
    (
        "strategy_version_transition",
        "uq_strategy_version_transition_outcome_target",
        (
            "transition_id",
            "canonical_hash",
            "evaluation_source",
            "evaluation_id",
            "evaluation_hash",
            "strategy_id",
            "from_version",
        ),
    ),
    (
        "shared_performance_attribution",
        "uq_shared_performance_attribution_outcome_target",
        ("attribution_id", "canonical_hash"),
    ),
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ",".join(f"'{value}'" for value in values) + ")"


def _all(template: str, columns: Sequence[str]) -> str:
    return "(" + " AND ".join(template.format(column) for column in columns) + ")"


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=f"ck_strategy_trial_outcome_{name}")


def upgrade() -> None:
    for table, name, columns in _EXISTING_TABLE_UNIQUES:
        op.create_unique_constraint(name, table, list(columns))

    numeric = sa.Numeric
    op.create_table(
        TABLE,
        sa.Column("assessment_id", sa.String(300), primary_key=True),
        sa.Column("policy_version", sa.String(60), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("reason_codes", sa.Text(), nullable=False),
        sa.Column("trial_id", sa.String(300), nullable=False),
        sa.Column("trial_hash", sa.String(64), nullable=False),
        sa.Column("strategy_id", sa.String(300), nullable=False),
        sa.Column("strategy_version", sa.Integer, nullable=False),
        sa.Column("instrument_id", sa.String(60), nullable=False),
        sa.Column("account_id", sa.String(300), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("trial_legacy_adopted", sa.Boolean, nullable=False),
        sa.Column("trial_policy_version", sa.String(60), nullable=False),
        sa.Column("horizon_days", sa.Integer, nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closure_id", sa.String(300), nullable=False),
        sa.Column("closure_hash", sa.String(64), nullable=False),
        sa.Column("terminal_reason", sa.String(40), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance_class", sa.String(40), nullable=False),
        sa.Column("version_registry", sa.String(40), nullable=True),
        sa.Column("definition_hash", sa.String(64), nullable=True),
        sa.Column("provenance_hash", sa.String(64), nullable=True),
        sa.Column("transition_id", sa.String(300), nullable=True),
        sa.Column("transition_hash", sa.String(64), nullable=True),
        sa.Column("transition_kind", sa.String(40), nullable=True),
        sa.Column("gate_policy_version", sa.String(60), nullable=True),
        sa.Column("evaluation_source", sa.String(40), nullable=True),
        sa.Column("evaluation_id", sa.String(300), nullable=True),
        sa.Column("evaluation_hash", sa.String(64), nullable=True),
        sa.Column("evaluated_version", sa.Integer, nullable=True),
        sa.Column("evaluation_as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluation_kind", sa.String(40), nullable=True),
        sa.Column("evaluation_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluation_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluation_sample_size", sa.Integer, nullable=True),
        sa.Column("evaluation_net_return", numeric, nullable=True),
        sa.Column("evaluation_average_trade_return", numeric, nullable=True),
        sa.Column("expected_return_basis", sa.String(60), nullable=False),
        sa.Column("opening_attribution_id", sa.String(300), nullable=False),
        sa.Column("opening_attribution_hash", sa.String(64), nullable=False),
        sa.Column("closing_attribution_id", sa.String(300), nullable=True),
        sa.Column("closing_attribution_hash", sa.String(64), nullable=True),
        sa.Column("economics_state", sa.String(20), nullable=False),
        sa.Column("opening_fill_count", sa.Integer, nullable=False),
        sa.Column("opening_base_quantity", numeric, nullable=False),
        sa.Column("opening_quote_quantity", numeric, nullable=False),
        sa.Column("opening_fees", numeric, nullable=False),
        sa.Column("opening_cost", numeric, nullable=False),
        sa.Column("closing_fill_count", sa.Integer, nullable=False),
        sa.Column("closing_base_quantity", numeric, nullable=False),
        sa.Column("closing_quote_quantity", numeric, nullable=False),
        sa.Column("closing_fees", numeric, nullable=False),
        sa.Column("closing_net_proceeds", numeric, nullable=False),
        sa.Column("cash_flow_pnl", numeric, nullable=True),
        sa.Column("ledger_realized_pnl", numeric, nullable=True),
        sa.Column("rounding_residual", numeric, nullable=True),
        sa.Column("reconciliation_tolerance", numeric, nullable=False),
        sa.Column("realized_return", numeric, nullable=True),
        sa.Column("realized_return_basis", sa.String(60), nullable=False),
        sa.Column("population_id", sa.String(300), nullable=True),
        sa.Column("population_members_hash", sa.String(64), nullable=True),
        sa.Column("population_size", sa.Integer, nullable=False),
        sa.Column("minimum_sample_size", sa.Integer, nullable=False),
        sa.Column("population_mean_realized_return", numeric, nullable=True),
        sa.Column("population_positive_count", sa.Integer, nullable=True),
        sa.Column("population_total_realized_pnl", numeric, nullable=True),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "trial_id", "policy_version", name="uq_strategy_trial_outcome_assessment_trial_policy"
        ),
        _check(f"policy_version = '{POLICY_V1}'", "policy"),
        _check(_in("status", _STATUSES), "status"),
        _check(_in("provenance_class", _PROVENANCE_CLASSES), "provenance_class"),
        _check(_in("economics_state", _ECONOMICS_STATES), "economics_state"),
        _check(_in("terminal_reason", _TERMINAL_REASONS), "terminal_reason"),
        _check(_in("expected_return_basis", _EXPECTED_BASES), "expected_basis"),
        _check(
            "realized_return_basis = 'LEDGER_REALIZED_PAPER_PNL_OVER_OPENING_COST'",
            "realized_basis",
        ),
        _check("authority = 'NONE'", "authority"),
        _check(
            "evaluation_source IS NULL OR evaluation_source = 'INTERNAL_STRATEGY_EVALUATION'",
            "evaluation_source",
        ),
        # A legacy trial is exactly a PREDATES row, and never bound.
        _check(
            "trial_legacy_adopted = (provenance_class = 'PREDATES_STRATEGY_PROVENANCE')",
            "legacy_class",
        ),
        _check(
            "(provenance_class = 'BOUND') = "
            + _all("{} IS NOT NULL", _BOUND_COLUMNS)
            + " AND (provenance_class <> 'BOUND') = "
            + _all("{} IS NULL", _BOUND_COLUMNS),
            "bound_shape",
        ),
        _check(
            "(provenance_class = 'BOUND') = (expected_return_basis = "
            "'EVALUATION_WINDOW_NET_RETURN_NOT_HORIZON_MATCHED')",
            "expected_basis_shape",
        ),
        _check(
            "provenance_class = 'BOUND' OR (population_size = 0 AND evaluation_window_start "
            "IS NULL AND evaluation_net_return IS NULL AND evaluation_average_trade_return "
            "IS NULL)",
            "unbound_empty",
        ),
        _check(
            "(status = 'PREDATES_STRATEGY_PROVENANCE') = "
            "(provenance_class = 'PREDATES_STRATEGY_PROVENANCE') "
            "AND (status = 'PROVENANCE_MISSING') = (provenance_class = 'PROVENANCE_MISSING')",
            "status_provenance",
        ),
        _check(
            "(status <> 'NO_ECONOMIC_OUTCOME' OR economics_state = 'NO_EXPOSURE') "
            "AND (status <> 'ECONOMICS_UNRECONCILED' OR economics_state = 'UNRECONCILED') "
            "AND (status NOT IN ('INSUFFICIENT_SAMPLE', 'ASSESSED') "
            "OR (provenance_class = 'BOUND' AND economics_state = 'RECONCILED'))",
            "status_economics",
        ),
        _check(
            "(status <> 'ASSESSED' OR population_size >= minimum_sample_size) "
            "AND (status <> 'INSUFFICIENT_SAMPLE' OR population_size < minimum_sample_size)",
            "status_sample",
        ),
        # Statistics are withheld below the floor, never merely flagged.
        _check(
            "(status = 'ASSESSED') = ("
            "population_mean_realized_return IS NOT NULL "
            "AND population_positive_count IS NOT NULL "
            "AND population_total_realized_pnl IS NOT NULL) "
            "AND (status = 'ASSESSED' OR (population_mean_realized_return IS NULL "
            "AND population_positive_count IS NULL AND population_total_realized_pnl IS NULL))",
            "sample_statistics_withheld",
        ),
        _check("(realized_return IS NOT NULL) = (economics_state = 'RECONCILED')", "return"),
        _check(
            "economics_state <> 'NO_EXPOSURE' OR (terminal_reason = "
            "'OPENING_ORDER_EXPIRED_UNFILLED' AND opening_fill_count = 0 "
            "AND closing_fill_count = 0 AND cash_flow_pnl IS NULL "
            "AND ledger_realized_pnl IS NULL)",
            "no_exposure",
        ),
        _check(
            "(terminal_reason = 'TRIAL_HORIZON_EXIT') = "
            "(closing_attribution_id IS NOT NULL AND closing_attribution_hash IS NOT NULL) "
            "AND (closing_attribution_id IS NULL) = (closing_attribution_hash IS NULL)",
            "closing_attribution",
        ),
        _check(
            "population_size >= 0 AND minimum_sample_size >= 1 AND opening_fill_count >= 0 "
            "AND closing_fill_count >= 0 AND generation >= 1 AND horizon_days >= 1",
            "counts",
        ),
        _check(
            "length(trial_hash) = 64 AND length(closure_hash) = 64 "
            "AND length(opening_attribution_hash) = 64 AND length(canonical_hash) = 64",
            "hashes",
        ),
        sa.ForeignKeyConstraint(
            ["trial_id", "strategy_id", "strategy_version", "trial_legacy_adopted", "trial_hash"],
            [
                "internal_strategy_paper_trial.trial_id",
                "internal_strategy_paper_trial.strategy_id",
                "internal_strategy_paper_trial.strategy_version",
                "internal_strategy_paper_trial.legacy_adopted",
                "internal_strategy_paper_trial.canonical_hash",
            ],
            name="fk_strategy_trial_outcome_trial",
        ),
        sa.ForeignKeyConstraint(
            ["closure_id", "trial_id", "terminal_reason", "closed_at", "closure_hash"],
            [
                "internal_strategy_paper_trial_closure.closure_id",
                "internal_strategy_paper_trial_closure.trial_id",
                "internal_strategy_paper_trial_closure.terminal_reason",
                "internal_strategy_paper_trial_closure.closed_at",
                "internal_strategy_paper_trial_closure.canonical_hash",
            ],
            name="fk_strategy_trial_outcome_closure",
        ),
        sa.ForeignKeyConstraint(
            [
                "trial_id",
                "transition_id",
                "version_registry",
                "strategy_id",
                "strategy_version",
                "definition_hash",
                "provenance_hash",
            ],
            [
                "strategy_paper_trial_provenance.trial_id",
                "strategy_paper_trial_provenance.transition_id",
                "strategy_paper_trial_provenance.registry",
                "strategy_paper_trial_provenance.strategy_id",
                "strategy_paper_trial_provenance.strategy_version",
                "strategy_paper_trial_provenance.definition_hash",
                "strategy_paper_trial_provenance.canonical_hash",
            ],
            name="fk_strategy_trial_outcome_provenance",
        ),
        sa.ForeignKeyConstraint(
            [
                "transition_id",
                "transition_hash",
                "evaluation_source",
                "evaluation_id",
                "evaluation_hash",
                "strategy_id",
                "evaluated_version",
            ],
            [
                "strategy_version_transition.transition_id",
                "strategy_version_transition.canonical_hash",
                "strategy_version_transition.evaluation_source",
                "strategy_version_transition.evaluation_id",
                "strategy_version_transition.evaluation_hash",
                "strategy_version_transition.strategy_id",
                "strategy_version_transition.from_version",
            ],
            name="fk_strategy_trial_outcome_transition",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id", "strategy_id", "evaluated_version", "evaluation_hash"],
            [
                "internal_strategy_evaluation.evaluation_id",
                "internal_strategy_evaluation.strategy_id",
                "internal_strategy_evaluation.strategy_version",
                "internal_strategy_evaluation.canonical_hash",
            ],
            name="fk_strategy_trial_outcome_evaluation",
        ),
        sa.ForeignKeyConstraint(
            ["opening_attribution_id", "opening_attribution_hash"],
            [
                "shared_performance_attribution.attribution_id",
                "shared_performance_attribution.canonical_hash",
            ],
            name="fk_strategy_trial_outcome_opening_attribution",
        ),
        sa.ForeignKeyConstraint(
            ["closing_attribution_id", "closing_attribution_hash"],
            [
                "shared_performance_attribution.attribution_id",
                "shared_performance_attribution.canonical_hash",
            ],
            name="fk_strategy_trial_outcome_closing_attribution",
        ),
    )
    op.create_index("ix_strategy_trial_outcome_assessment_strategy", TABLE, ["strategy_id"])
    op.create_index("ix_strategy_trial_outcome_assessment_population", TABLE, ["population_id"])
    op.execute(sa.text(immutable_trigger_ddl(TABLE)))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {TABLE}) THEN "  # nosec B608
            "RAISE EXCEPTION "
            f"'cannot downgrade 0117 with retained {TABLE} evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(sa.text(f"DROP TRIGGER apex_immutable_{TABLE}_trigger ON {TABLE}"))
    op.drop_table(TABLE)
    for table, name, _columns in reversed(_EXISTING_TABLE_UNIQUES):
        op.drop_constraint(name, table, type_="unique")
