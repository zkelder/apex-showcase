"""PostgreSQL repository for quant research methodology and walk-forward
validation (migration ``0079``): holdout-by-default governance policies,
append-only holdout-exposure events, phase-labeled research trials,
multiple-testing-aware trial families, a durable/resumable walk-forward run
with append-only fold results, robustness-check runs, retained research
conclusions, and fail-closed promotion evaluations.

Mirrors the payload-column + narrow-indexed-column pattern established by
``adapters.historical_replay_postgres``: one JSON ``payload`` column
restores the exact original dataclass via ``canonical_rehydrate.rehydrate``,
and a handful of indexed columns exist only for querying, never as a second
source of truth.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.research_methodology import (
    HoldoutExposureEvent,
    PromotionEvaluation,
    ResearchConclusion,
    ResearchGovernancePolicy,
    ResearchTrial,
    ResearchTrialFamily,
    RobustnessCheckRun,
    WalkForwardFold,
    WalkForwardRun,
)
from apex.domain.values import canonical_json_bytes

RESEARCH_METHODOLOGY_TABLES = (
    "research_governance_policy",
    "research_governance_holdout_exposure",
    "research_trial",
    "research_trial_family",
    "walk_forward_run",
    "walk_forward_fold",
    "robustness_check_run",
    "research_conclusion",
    "research_promotion_evaluation",
)
RESEARCH_METHODOLOGY_IMMUTABLE_TABLES = (
    "research_governance_policy",
    "research_governance_holdout_exposure",
    "research_trial",
    "research_trial_family",
    "walk_forward_fold",
    "robustness_check_run",
    "research_conclusion",
    "research_promotion_evaluation",
)

metadata = sa.MetaData()

research_governance_policy = sa.Table(
    "research_governance_policy",
    metadata,
    sa.Column("governance_policy_id", sa.String(160), primary_key=True),
    sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
    sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
    sa.Column(
        "replay_run_id",
        sa.String(160),
        sa.ForeignKey("historical_replay_run.replay_run_id"),
        nullable=False,
    ),
    sa.Column("intent", sa.String(24), nullable=False),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column("supersedes_governance_policy_id", sa.String(160), nullable=True),
    sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
research_governance_holdout_exposure = sa.Table(
    "research_governance_holdout_exposure",
    metadata,
    sa.Column("exposure_id", sa.String(160), primary_key=True),
    sa.Column(
        "governance_policy_id",
        sa.String(160),
        sa.ForeignKey("research_governance_policy.governance_policy_id"),
        nullable=False,
    ),
    sa.Column("exposed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("actor", sa.String(160), nullable=False),
    sa.Column("purpose", sa.String(160), nullable=False),
    sa.Column("consumed_trial_id", sa.String(160), nullable=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
research_trial = sa.Table(
    "research_trial",
    metadata,
    sa.Column("trial_id", sa.String(160), primary_key=True),
    sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
    sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
    sa.Column(
        "replay_run_id",
        sa.String(160),
        sa.ForeignKey("historical_replay_run.replay_run_id"),
        nullable=False,
    ),
    sa.Column("phase", sa.String(24), nullable=False),
    sa.Column("method_id", sa.String(80), nullable=False),
    sa.Column("configuration_origin", sa.String(32), nullable=False),
    sa.Column("session_date_start", sa.String(10), nullable=False),
    sa.Column("session_date_end", sa.String(10), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
research_trial_family = sa.Table(
    "research_trial_family",
    metadata,
    sa.Column("trial_family_id", sa.String(160), primary_key=True),
    sa.Column("hypothesis_id", sa.String(160), nullable=False),
    sa.Column("candidate_count", sa.Integer(), nullable=False),
    sa.Column("test_count", sa.Integer(), nullable=False),
    sa.Column("selection_method", sa.String(32), nullable=False),
    sa.Column("correction_applied", sa.Boolean(), nullable=False),
    sa.Column("correction_method", sa.String(32), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
walk_forward_run = sa.Table(
    "walk_forward_run",
    metadata,
    sa.Column("walk_forward_run_id", sa.String(160), primary_key=True),
    sa.Column(
        "replay_run_id",
        sa.String(160),
        sa.ForeignKey("historical_replay_run.replay_run_id"),
        nullable=False,
    ),
    sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
    sa.Column("method_id", sa.String(80), nullable=False),
    sa.Column("window_mode", sa.String(16), nullable=False),
    sa.Column("training_window_sessions", sa.Integer(), nullable=False),
    sa.Column("validation_window_sessions", sa.Integer(), nullable=False),
    sa.Column("test_window_sessions", sa.Integer(), nullable=False),
    sa.Column("step_sessions", sa.Integer(), nullable=False),
    sa.Column("minimum_samples", sa.Integer(), nullable=False),
    sa.Column("start_date", sa.String(10), nullable=False),
    sa.Column("end_date", sa.String(10), nullable=False),
    sa.Column("hypothesis_version_id", sa.String(160), nullable=True),
    sa.Column("cursor_fold_ordinal", sa.Integer(), nullable=False),
    sa.Column("completed_fold_count", sa.Integer(), nullable=False),
    sa.Column("completion_state", sa.String(16), nullable=False),
    sa.Column("failure_reason", sa.String(500), nullable=True),
    sa.Column("operator_action_id", sa.String(160), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
walk_forward_fold = sa.Table(
    "walk_forward_fold",
    metadata,
    sa.Column("fold_id", sa.String(160), primary_key=True),
    sa.Column(
        "walk_forward_run_id",
        sa.String(160),
        sa.ForeignKey("walk_forward_run.walk_forward_run_id"),
        nullable=False,
    ),
    sa.Column("fold_ordinal", sa.Integer(), nullable=False),
    sa.Column("train_start", sa.String(10), nullable=False),
    sa.Column("train_end", sa.String(10), nullable=False),
    sa.Column("test_start", sa.String(10), nullable=False),
    sa.Column("test_end", sa.String(10), nullable=False),
    sa.Column("outcome_state", sa.String(24), nullable=False),
    sa.Column(
        "test_trial_id", sa.String(160), sa.ForeignKey("research_trial.trial_id"), nullable=True
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.UniqueConstraint("walk_forward_run_id", "fold_ordinal"),
)
robustness_check_run = sa.Table(
    "robustness_check_run",
    metadata,
    sa.Column("robustness_run_id", sa.String(160), primary_key=True),
    sa.Column(
        "base_trial_id", sa.String(160), sa.ForeignKey("research_trial.trial_id"), nullable=False
    ),
    sa.Column("checks_run", sa.Integer(), nullable=False),
    sa.Column("checks_consistent", sa.Integer(), nullable=False),
    sa.Column("checks_inconsistent", sa.Integer(), nullable=False),
    sa.Column("passed", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
research_conclusion = sa.Table(
    "research_conclusion",
    metadata,
    sa.Column("conclusion_id", sa.String(160), primary_key=True),
    sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
    sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
    sa.Column(
        "replay_run_id",
        sa.String(160),
        sa.ForeignKey("historical_replay_run.replay_run_id"),
        nullable=False,
    ),
    sa.Column("phase", sa.String(24), nullable=False),
    sa.Column("sample_quality_state", sa.String(40), nullable=False),
    sa.Column("conclusion_state", sa.String(24), nullable=False),
    sa.Column(
        "trial_family_id",
        sa.String(160),
        sa.ForeignKey("research_trial_family.trial_family_id"),
        nullable=True,
    ),
    sa.Column(
        "robustness_run_id",
        sa.String(160),
        sa.ForeignKey("robustness_check_run.robustness_run_id"),
        nullable=True,
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
research_promotion_evaluation = sa.Table(
    "research_promotion_evaluation",
    metadata,
    sa.Column("promotion_evaluation_id", sa.String(160), primary_key=True),
    sa.Column(
        "conclusion_id",
        sa.String(160),
        sa.ForeignKey("research_conclusion.conclusion_id"),
        nullable=False,
    ),
    sa.Column("policy_id", sa.String(80), nullable=False),
    sa.Column("eligible", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class ResearchMethodologyPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise ResearchMethodologyPersistenceError("STORED_PAYLOAD_INVALID")
    return value


class PostgresResearchMethodologyRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- governance policy (immutable, append-only) ------------------------

    def governance_policy_by_id(self, governance_policy_id: str) -> ResearchGovernancePolicy | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_governance_policy).where(
                        research_governance_policy.c.governance_policy_id == governance_policy_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ResearchGovernancePolicy, _decoded(row))

    def save_governance_policy(self, policy: ResearchGovernancePolicy) -> ResearchGovernancePolicy:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_governance_policy.c.governance_policy_id).where(
                    research_governance_policy.c.governance_policy_id == policy.governance_policy_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return policy
            connection.execute(
                research_governance_policy.insert().values(
                    governance_policy_id=policy.governance_policy_id,
                    hypothesis_version_id=policy.hypothesis_version_id,
                    dataset_snapshot_id=policy.dataset_snapshot_id,
                    replay_run_id=policy.replay_run_id,
                    intent=policy.intent.value,
                    revision=policy.revision,
                    supersedes_governance_policy_id=policy.supersedes_governance_policy_id,
                    declared_at=policy.declared_at,
                    payload=_payload(policy),
                    authority="NONE",
                )
            )
        return policy

    def governance_policies_for_replay_run(
        self, replay_run_id: str
    ) -> tuple[ResearchGovernancePolicy, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_governance_policy)
                .where(research_governance_policy.c.replay_run_id == replay_run_id)
                .order_by(research_governance_policy.c.declared_at)
            ).mappings()
            return tuple(rehydrate(ResearchGovernancePolicy, _decoded(row)) for row in rows)

    # --- holdout exposure (immutable, append-only) --------------------------

    def save_holdout_exposure(self, event: HoldoutExposureEvent) -> HoldoutExposureEvent:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_governance_holdout_exposure.c.exposure_id).where(
                    research_governance_holdout_exposure.c.exposure_id == event.exposure_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return event
            connection.execute(
                research_governance_holdout_exposure.insert().values(
                    exposure_id=event.exposure_id,
                    governance_policy_id=event.governance_policy_id,
                    exposed_at=event.exposed_at,
                    actor=event.actor,
                    purpose=event.purpose,
                    consumed_trial_id=event.consumed_trial_id,
                    payload=_payload(event),
                    authority="NONE",
                )
            )
        return event

    def holdout_exposures_for_policy(
        self, governance_policy_id: str
    ) -> tuple[HoldoutExposureEvent, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_governance_holdout_exposure)
                .where(
                    research_governance_holdout_exposure.c.governance_policy_id
                    == governance_policy_id
                )
                .order_by(research_governance_holdout_exposure.c.exposed_at)
            ).mappings()
            return tuple(rehydrate(HoldoutExposureEvent, _decoded(row)) for row in rows)

    # --- research trial (immutable, append-only) ----------------------------

    def trial_by_id(self, trial_id: str) -> ResearchTrial | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_trial).where(research_trial.c.trial_id == trial_id)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ResearchTrial, _decoded(row))

    def save_trial(self, trial: ResearchTrial) -> ResearchTrial:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_trial.c.trial_id).where(
                    research_trial.c.trial_id == trial.trial_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return trial
            connection.execute(
                research_trial.insert().values(
                    trial_id=trial.trial_id,
                    hypothesis_version_id=trial.hypothesis_version_id,
                    dataset_snapshot_id=trial.dataset_snapshot_id,
                    replay_run_id=trial.replay_run_id,
                    phase=trial.phase.value,
                    method_id=trial.configuration.method_id.value,
                    configuration_origin=trial.configuration.origin.value,
                    session_date_start=trial.session_date_range[0],
                    session_date_end=trial.session_date_range[1],
                    created_at=trial.created_at,
                    payload=_payload(trial),
                    authority="NONE",
                )
            )
        return trial

    def trials_for_replay_run(self, replay_run_id: str) -> tuple[ResearchTrial, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_trial)
                .where(research_trial.c.replay_run_id == replay_run_id)
                .order_by(research_trial.c.created_at)
            ).mappings()
            return tuple(rehydrate(ResearchTrial, _decoded(row)) for row in rows)

    # --- trial family (immutable, append-only) ------------------------------

    def trial_family_by_id(self, trial_family_id: str) -> ResearchTrialFamily | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_trial_family).where(
                        research_trial_family.c.trial_family_id == trial_family_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ResearchTrialFamily, _decoded(row))

    def save_trial_family(self, family: ResearchTrialFamily) -> ResearchTrialFamily:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_trial_family.c.trial_family_id).where(
                    research_trial_family.c.trial_family_id == family.trial_family_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return family
            connection.execute(
                research_trial_family.insert().values(
                    trial_family_id=family.trial_family_id,
                    hypothesis_id=family.hypothesis_id,
                    candidate_count=family.candidate_count,
                    test_count=family.test_count,
                    selection_method=family.selection_method.value,
                    correction_applied=family.correction_applied,
                    correction_method=family.correction_method.value,
                    created_at=family.created_at,
                    payload=_payload(family),
                    authority="NONE",
                )
            )
        return family

    def trial_families_for_hypothesis(self, hypothesis_id: str) -> tuple[ResearchTrialFamily, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_trial_family)
                .where(research_trial_family.c.hypothesis_id == hypothesis_id)
                .order_by(research_trial_family.c.created_at)
            ).mappings()
            return tuple(rehydrate(ResearchTrialFamily, _decoded(row)) for row in rows)

    # --- walk-forward run (mutable cursor/completion state) -----------------

    def walk_forward_run_by_idempotency(self, key: str) -> WalkForwardRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(walk_forward_run).where(walk_forward_run.c.idempotency_key == key)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(WalkForwardRun, _decoded(row))

    def save_walk_forward_run(self, run: WalkForwardRun) -> WalkForwardRun:
        values = {
            "replay_run_id": run.replay_run_id,
            "dataset_snapshot_id": run.dataset_snapshot_id,
            "method_id": run.method_id.value,
            "window_mode": run.window_mode.value,
            "training_window_sessions": run.training_window_sessions,
            "validation_window_sessions": run.validation_window_sessions,
            "test_window_sessions": run.test_window_sessions,
            "step_sessions": run.step_sessions,
            "minimum_samples": run.minimum_samples,
            "start_date": run.start_date,
            "end_date": run.end_date,
            "hypothesis_version_id": run.hypothesis_version_id,
            "cursor_fold_ordinal": run.cursor_fold_ordinal,
            "completed_fold_count": run.completed_fold_count,
            "completion_state": run.completion_state.value,
            "failure_reason": run.failure_reason,
            "operator_action_id": run.operator_action_id,
            "idempotency_key": run.idempotency_key,
            "created_at": run.created_at,
            "payload": _payload(run),
            "authority": "NONE",
        }
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(walk_forward_run.c.walk_forward_run_id).where(
                    walk_forward_run.c.walk_forward_run_id == run.walk_forward_run_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    walk_forward_run.insert().values(
                        walk_forward_run_id=run.walk_forward_run_id, **values
                    )
                )
            else:
                connection.execute(
                    walk_forward_run.update()
                    .where(walk_forward_run.c.walk_forward_run_id == run.walk_forward_run_id)
                    .values(**values)
                )
        return run

    def get_walk_forward_run(self, walk_forward_run_id: str) -> WalkForwardRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(walk_forward_run).where(
                        walk_forward_run.c.walk_forward_run_id == walk_forward_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(WalkForwardRun, _decoded(row))

    def list_walk_forward_runs(self) -> tuple[WalkForwardRun, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(walk_forward_run).order_by(walk_forward_run.c.created_at)
            ).mappings()
            return tuple(rehydrate(WalkForwardRun, _decoded(row)) for row in rows)

    # --- walk-forward fold (immutable, append-only) --------------------------

    def save_fold(self, fold: WalkForwardFold) -> WalkForwardFold:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(walk_forward_fold.c.fold_id).where(
                    walk_forward_fold.c.fold_id == fold.fold_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return fold
            connection.execute(
                walk_forward_fold.insert().values(
                    fold_id=fold.fold_id,
                    walk_forward_run_id=fold.walk_forward_run_id,
                    fold_ordinal=fold.fold_ordinal,
                    train_start=fold.train_start,
                    train_end=fold.train_end,
                    test_start=fold.test_start,
                    test_end=fold.test_end,
                    outcome_state=fold.outcome_state.value,
                    test_trial_id=fold.test_trial_id,
                    created_at=fold.created_at,
                    payload=_payload(fold),
                    authority="NONE",
                )
            )
        return fold

    def folds_for_run(self, walk_forward_run_id: str) -> tuple[WalkForwardFold, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(walk_forward_fold)
                .where(walk_forward_fold.c.walk_forward_run_id == walk_forward_run_id)
                .order_by(walk_forward_fold.c.fold_ordinal)
            ).mappings()
            return tuple(rehydrate(WalkForwardFold, _decoded(row)) for row in rows)

    # --- robustness check run (immutable, append-only) ------------------------

    def robustness_run_by_id(self, robustness_run_id: str) -> RobustnessCheckRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(robustness_check_run).where(
                        robustness_check_run.c.robustness_run_id == robustness_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(RobustnessCheckRun, _decoded(row))

    def save_robustness_run(self, run: RobustnessCheckRun) -> RobustnessCheckRun:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(robustness_check_run.c.robustness_run_id).where(
                    robustness_check_run.c.robustness_run_id == run.robustness_run_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return run
            connection.execute(
                robustness_check_run.insert().values(
                    robustness_run_id=run.robustness_run_id,
                    base_trial_id=run.base_trial_id,
                    checks_run=run.summary.checks_run,
                    checks_consistent=run.summary.checks_consistent,
                    checks_inconsistent=run.summary.checks_inconsistent,
                    passed=run.summary.passed,
                    created_at=run.created_at,
                    payload=_payload(run),
                    authority="NONE",
                )
            )
        return run

    # --- research conclusion (immutable, append-only) -------------------------

    def conclusion_by_id(self, conclusion_id: str) -> ResearchConclusion | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_conclusion).where(
                        research_conclusion.c.conclusion_id == conclusion_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ResearchConclusion, _decoded(row))

    def save_conclusion(self, conclusion: ResearchConclusion) -> ResearchConclusion:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_conclusion.c.conclusion_id).where(
                    research_conclusion.c.conclusion_id == conclusion.conclusion_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return conclusion
            connection.execute(
                research_conclusion.insert().values(
                    conclusion_id=conclusion.conclusion_id,
                    hypothesis_version_id=conclusion.hypothesis_version_id,
                    dataset_snapshot_id=conclusion.dataset_snapshot_id,
                    replay_run_id=conclusion.replay_run_id,
                    phase=conclusion.phase.value,
                    sample_quality_state=conclusion.sample_quality_state.value,
                    conclusion_state=conclusion.conclusion_state.value,
                    trial_family_id=conclusion.trial_family_id,
                    robustness_run_id=conclusion.robustness_run_id,
                    created_at=conclusion.created_at,
                    payload=_payload(conclusion),
                    authority="NONE",
                )
            )
        return conclusion

    def conclusions_for_hypothesis(
        self, hypothesis_version_id: str
    ) -> tuple[ResearchConclusion, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_conclusion)
                .where(research_conclusion.c.hypothesis_version_id == hypothesis_version_id)
                .order_by(research_conclusion.c.created_at)
            ).mappings()
            return tuple(rehydrate(ResearchConclusion, _decoded(row)) for row in rows)

    # --- promotion evaluation (immutable, append-only) -------------------------

    def promotion_evaluation_by_id(
        self, promotion_evaluation_id: str
    ) -> PromotionEvaluation | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_promotion_evaluation).where(
                        research_promotion_evaluation.c.promotion_evaluation_id
                        == promotion_evaluation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(PromotionEvaluation, _decoded(row))

    def save_promotion_evaluation(self, evaluation: PromotionEvaluation) -> PromotionEvaluation:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_promotion_evaluation.c.promotion_evaluation_id).where(
                    research_promotion_evaluation.c.promotion_evaluation_id
                    == evaluation.promotion_evaluation_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return evaluation
            connection.execute(
                research_promotion_evaluation.insert().values(
                    promotion_evaluation_id=evaluation.promotion_evaluation_id,
                    conclusion_id=evaluation.conclusion_id,
                    policy_id=evaluation.policy_id,
                    eligible=evaluation.eligible,
                    created_at=evaluation.created_at,
                    payload=_payload(evaluation),
                    authority="NONE",
                )
            )
        return evaluation

    def promotion_evaluations_for_conclusion(
        self, conclusion_id: str
    ) -> tuple[PromotionEvaluation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_promotion_evaluation)
                .where(research_promotion_evaluation.c.conclusion_id == conclusion_id)
                .order_by(research_promotion_evaluation.c.created_at)
            ).mappings()
            return tuple(rehydrate(PromotionEvaluation, _decoded(row)) for row in rows)
