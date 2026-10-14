"""PostgreSQL repository for the point-in-time historical replay engine
(migration ``0078``): durable/resumable replay runs, append-only checkpoints,
deterministic point-in-time feature frames, research-method evaluations with
a guarded monotonic outcome sub-state, a research period (train/validation/
holdout) policy, and lineage linking a replay run to the existing
``research_evidence`` hypothesis/experiment lifecycle.

Mirrors the payload-column + narrow-indexed-column pattern established by
``adapters.historical_market_data_postgres``: one JSON ``payload`` column
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
from apex.domain.historical_replay import (
    HistoricalResearchEvaluation,
    OutcomeAssessment,
    ReplayCheckpoint,
    ReplayExperimentLink,
    ReplayFeatureFrame,
    ReplayRun,
    ResearchPeriodPolicy,
)
from apex.domain.values import canonical_json_bytes

HISTORICAL_REPLAY_TABLES = (
    "historical_replay_run",
    "historical_replay_checkpoint",
    "historical_replay_feature_frame",
    "historical_replay_evaluation",
    "historical_replay_research_period_policy",
    "historical_replay_experiment_link",
)
HISTORICAL_REPLAY_IMMUTABLE_TABLES = (
    "historical_replay_checkpoint",
    "historical_replay_feature_frame",
    "historical_replay_research_period_policy",
    "historical_replay_experiment_link",
)

metadata = sa.MetaData()

historical_replay_run = sa.Table(
    "historical_replay_run",
    metadata,
    sa.Column("replay_run_id", sa.String(160), primary_key=True),
    sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
    sa.Column("start_date", sa.String(10), nullable=False),
    sa.Column("end_date", sa.String(10), nullable=False),
    sa.Column("cadence", sa.String(16), nullable=False),
    sa.Column("calendar_identity", sa.String(24), nullable=True),
    sa.Column("feature_policy_id", sa.String(160), nullable=False),
    sa.Column("feature_policy_hash", sa.String(64), nullable=False),
    sa.Column("research_policy_id", sa.String(160), nullable=False),
    sa.Column("research_policy_hash", sa.String(64), nullable=False),
    sa.Column("initial_as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("final_as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("calendar_coverage_state", sa.String(40), nullable=False),
    sa.Column("cursor_as_of", sa.DateTime(timezone=True), nullable=True),
    sa.Column("cursor_session_date", sa.String(10), nullable=True),
    sa.Column("completed_step_count", sa.Integer(), nullable=False),
    sa.Column("completion_state", sa.String(16), nullable=False),
    sa.Column("failure_reason", sa.String(500), nullable=True),
    sa.Column("hypothesis_version_id", sa.String(160), nullable=True),
    sa.Column("operator_action_id", sa.String(160), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
historical_replay_checkpoint = sa.Table(
    "historical_replay_checkpoint",
    metadata,
    sa.Column("checkpoint_id", sa.String(160), primary_key=True),
    sa.Column(
        "replay_run_id",
        sa.String(160),
        sa.ForeignKey("historical_replay_run.replay_run_id"),
        nullable=False,
    ),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("session_date", sa.String(10), nullable=False),
    sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
    sa.Column("feature_policy_hash", sa.String(64), nullable=False),
    sa.Column("research_policy_hash", sa.String(64), nullable=False),
    sa.Column("completed_step_count", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.UniqueConstraint("replay_run_id", "session_date"),
)
historical_replay_feature_frame = sa.Table(
    "historical_replay_feature_frame",
    metadata,
    sa.Column("frame_id", sa.String(160), primary_key=True),
    sa.Column(
        "replay_run_id",
        sa.String(160),
        sa.ForeignKey("historical_replay_run.replay_run_id"),
        nullable=False,
    ),
    sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("session_date", sa.String(10), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.UniqueConstraint("replay_run_id", "session_date"),
)
historical_replay_evaluation = sa.Table(
    "historical_replay_evaluation",
    metadata,
    sa.Column("evaluation_id", sa.String(160), primary_key=True),
    sa.Column(
        "replay_run_id",
        sa.String(160),
        sa.ForeignKey("historical_replay_run.replay_run_id"),
        nullable=False,
    ),
    sa.Column(
        "frame_id",
        sa.String(160),
        sa.ForeignKey("historical_replay_feature_frame.frame_id"),
        nullable=False,
    ),
    sa.Column("method_id", sa.String(80), nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("session_date", sa.String(10), nullable=False),
    sa.Column("outcome_completeness", sa.String(16), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.UniqueConstraint("replay_run_id", "frame_id", "method_id"),
)
historical_replay_research_period_policy = sa.Table(
    "historical_replay_research_period_policy",
    metadata,
    sa.Column("policy_id", sa.String(160), primary_key=True),
    sa.Column(
        "replay_run_id",
        sa.String(160),
        sa.ForeignKey("historical_replay_run.replay_run_id"),
        nullable=False,
        unique=True,
    ),
    sa.Column("minimum_sample_count", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
historical_replay_experiment_link = sa.Table(
    "historical_replay_experiment_link",
    metadata,
    sa.Column("link_id", sa.String(160), primary_key=True),
    sa.Column(
        "replay_run_id",
        sa.String(160),
        sa.ForeignKey("historical_replay_run.replay_run_id"),
        nullable=False,
    ),
    sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
    sa.Column("research_dataset_snapshot_id", sa.String(160), nullable=False),
    sa.Column("research_experiment_run_id", sa.String(160), nullable=False),
    sa.Column("research_experiment_outcome_id", sa.String(160), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class HistoricalReplayPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise HistoricalReplayPersistenceError("STORED_PAYLOAD_INVALID")
    return value


class PostgresHistoricalReplayRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- replay run (mutable cursor/completion state) ---------------------

    def run_by_idempotency(self, key: str) -> ReplayRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(historical_replay_run).where(
                        historical_replay_run.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ReplayRun, _decoded(row))

    def save_run(self, run: ReplayRun) -> ReplayRun:
        values = {
            "dataset_snapshot_id": run.dataset_snapshot_id,
            "start_date": run.start_date,
            "end_date": run.end_date,
            "cadence": run.cadence.value,
            "calendar_identity": run.calendar_identity,
            "feature_policy_id": run.feature_policy_id,
            "feature_policy_hash": run.feature_policy_hash,
            "research_policy_id": run.research_policy_id,
            "research_policy_hash": run.research_policy_hash,
            "initial_as_of": run.initial_as_of,
            "final_as_of": run.final_as_of,
            "calendar_coverage_state": run.calendar_coverage_state.value,
            "cursor_as_of": run.cursor_as_of,
            "cursor_session_date": run.cursor_session_date,
            "completed_step_count": run.completed_step_count,
            "completion_state": run.completion_state.value,
            "failure_reason": run.failure_reason,
            "hypothesis_version_id": run.hypothesis_version_id,
            "operator_action_id": run.operator_action_id,
            "idempotency_key": run.idempotency_key,
            "created_at": run.created_at,
            "payload": _payload(run),
            "authority": "NONE",
        }
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(historical_replay_run.c.replay_run_id).where(
                    historical_replay_run.c.replay_run_id == run.replay_run_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    historical_replay_run.insert().values(replay_run_id=run.replay_run_id, **values)
                )
            else:
                connection.execute(
                    historical_replay_run.update()
                    .where(historical_replay_run.c.replay_run_id == run.replay_run_id)
                    .values(**values)
                )
        return run

    def get_run(self, replay_run_id: str) -> ReplayRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(historical_replay_run).where(
                        historical_replay_run.c.replay_run_id == replay_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ReplayRun, _decoded(row))

    def list_runs(self) -> tuple[ReplayRun, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(historical_replay_run).order_by(historical_replay_run.c.created_at)
            ).mappings()
            return tuple(rehydrate(ReplayRun, _decoded(row)) for row in rows)

    # --- checkpoints (immutable, append-only) ------------------------------

    def save_checkpoint(self, checkpoint: ReplayCheckpoint) -> ReplayCheckpoint:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(historical_replay_checkpoint.c.checkpoint_id).where(
                    historical_replay_checkpoint.c.checkpoint_id == checkpoint.checkpoint_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return checkpoint
            connection.execute(
                historical_replay_checkpoint.insert().values(
                    checkpoint_id=checkpoint.checkpoint_id,
                    replay_run_id=checkpoint.replay_run_id,
                    as_of=checkpoint.as_of,
                    session_date=checkpoint.session_date,
                    dataset_snapshot_id=checkpoint.dataset_snapshot_id,
                    feature_policy_hash=checkpoint.feature_policy_hash,
                    research_policy_hash=checkpoint.research_policy_hash,
                    completed_step_count=checkpoint.completed_step_count,
                    created_at=checkpoint.created_at,
                    payload=_payload(checkpoint),
                    authority="NONE",
                )
            )
        return checkpoint

    def list_checkpoints(self, replay_run_id: str) -> tuple[ReplayCheckpoint, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(historical_replay_checkpoint)
                .where(historical_replay_checkpoint.c.replay_run_id == replay_run_id)
                .order_by(historical_replay_checkpoint.c.as_of)
            ).mappings()
            return tuple(rehydrate(ReplayCheckpoint, _decoded(row)) for row in rows)

    def latest_checkpoint(self, replay_run_id: str) -> ReplayCheckpoint | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(historical_replay_checkpoint)
                    .where(historical_replay_checkpoint.c.replay_run_id == replay_run_id)
                    .order_by(historical_replay_checkpoint.c.as_of.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ReplayCheckpoint, _decoded(row))

    # --- feature frames (immutable, content-addressed) ---------------------

    def frame_by_id(self, frame_id: str) -> ReplayFeatureFrame | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(historical_replay_feature_frame).where(
                        historical_replay_feature_frame.c.frame_id == frame_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ReplayFeatureFrame, _decoded(row))

    def save_frame(self, frame: ReplayFeatureFrame) -> ReplayFeatureFrame:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(historical_replay_feature_frame.c.frame_id).where(
                    historical_replay_feature_frame.c.frame_id == frame.frame_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return frame
            connection.execute(
                historical_replay_feature_frame.insert().values(
                    frame_id=frame.frame_id,
                    replay_run_id=frame.replay_run_id,
                    dataset_snapshot_id=frame.dataset_snapshot_id,
                    as_of=frame.as_of,
                    session_date=frame.session_date,
                    created_at=frame.created_at,
                    payload=_payload(frame),
                    authority="NONE",
                )
            )
        return frame

    def list_frames(self, replay_run_id: str, *, limit: int) -> tuple[ReplayFeatureFrame, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(historical_replay_feature_frame)
                .where(historical_replay_feature_frame.c.replay_run_id == replay_run_id)
                .order_by(historical_replay_feature_frame.c.as_of)
                .limit(limit)
            ).mappings()
            return tuple(rehydrate(ReplayFeatureFrame, _decoded(row)) for row in rows)

    # --- research-method evaluations (guarded outcome sub-state) -----------

    def evaluation_by_id(self, evaluation_id: str) -> HistoricalResearchEvaluation | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(historical_replay_evaluation).where(
                        historical_replay_evaluation.c.evaluation_id == evaluation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(HistoricalResearchEvaluation, _decoded(row))

    def save_evaluation(
        self, evaluation: HistoricalResearchEvaluation
    ) -> HistoricalResearchEvaluation:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(historical_replay_evaluation.c.evaluation_id).where(
                    historical_replay_evaluation.c.evaluation_id == evaluation.evaluation_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return evaluation
            connection.execute(
                historical_replay_evaluation.insert().values(
                    evaluation_id=evaluation.evaluation_id,
                    replay_run_id=evaluation.replay_run_id,
                    frame_id=evaluation.frame_id,
                    method_id=evaluation.method_id.value,
                    as_of=evaluation.as_of,
                    session_date=evaluation.session_date,
                    outcome_completeness=None,
                    created_at=evaluation.created_at,
                    payload=_payload(evaluation),
                    authority="NONE",
                )
            )
        return evaluation

    def apply_outcome(
        self, evaluation_id: str, outcome: OutcomeAssessment
    ) -> HistoricalResearchEvaluation:
        current = self.evaluation_by_id(evaluation_id)
        if current is None:
            raise HistoricalReplayPersistenceError("HISTORICAL_REPLAY_EVALUATION_NOT_FOUND")
        updated = current.attach_outcome(outcome)
        with self._engine.begin() as connection:
            connection.execute(
                historical_replay_evaluation.update()
                .where(historical_replay_evaluation.c.evaluation_id == evaluation_id)
                .values(
                    outcome_completeness=outcome.completeness.value,
                    payload=_payload(updated),
                )
            )
        return updated

    def list_evaluations(
        self, replay_run_id: str, *, limit: int
    ) -> tuple[HistoricalResearchEvaluation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(historical_replay_evaluation)
                .where(historical_replay_evaluation.c.replay_run_id == replay_run_id)
                .order_by(historical_replay_evaluation.c.as_of)
                .limit(limit)
            ).mappings()
            return tuple(rehydrate(HistoricalResearchEvaluation, _decoded(row)) for row in rows)

    # --- research period (train/validation/holdout) policy -----------------

    def save_research_period_policy(self, policy: ResearchPeriodPolicy) -> ResearchPeriodPolicy:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(historical_replay_research_period_policy.c.policy_id).where(
                    historical_replay_research_period_policy.c.policy_id == policy.policy_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return policy
            connection.execute(
                historical_replay_research_period_policy.insert().values(
                    policy_id=policy.policy_id,
                    replay_run_id=policy.replay_run_id,
                    minimum_sample_count=policy.minimum_sample_count,
                    created_at=policy.created_at,
                    payload=_payload(policy),
                    authority="NONE",
                )
            )
        return policy

    def research_period_policy(self, replay_run_id: str) -> ResearchPeriodPolicy | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(historical_replay_research_period_policy).where(
                        historical_replay_research_period_policy.c.replay_run_id == replay_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ResearchPeriodPolicy, _decoded(row))

    # --- historical experiment integration lineage --------------------------

    def save_experiment_link(self, link: ReplayExperimentLink) -> ReplayExperimentLink:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(historical_replay_experiment_link.c.link_id).where(
                    historical_replay_experiment_link.c.link_id == link.link_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return link
            connection.execute(
                historical_replay_experiment_link.insert().values(
                    link_id=link.link_id,
                    replay_run_id=link.replay_run_id,
                    hypothesis_version_id=link.hypothesis_version_id,
                    research_dataset_snapshot_id=link.research_dataset_snapshot_id,
                    research_experiment_run_id=link.research_experiment_run_id,
                    research_experiment_outcome_id=link.research_experiment_outcome_id,
                    created_at=link.created_at,
                    payload=_payload(link),
                    authority="NONE",
                )
            )
        return link

    def experiment_links(self, replay_run_id: str) -> tuple[ReplayExperimentLink, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(historical_replay_experiment_link)
                .where(historical_replay_experiment_link.c.replay_run_id == replay_run_id)
                .order_by(historical_replay_experiment_link.c.created_at)
            ).mappings()
            return tuple(rehydrate(ReplayExperimentLink, _decoded(row)) for row in rows)
