"""PostgreSQL persistence and sealing guard for crypto campaign evidence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.crypto_challenger_postgres import PostgresCryptoChallengerRepository
from apex.adapters.crypto_research_postgres import _data, _payload, _strings, _time
from apex.application.crypto_research import CryptoResearchConflict
from apex.domain.crypto_campaign import (
    CampaignCandidate,
    CampaignDatasetAttachment,
    CampaignDatasetRole,
    CampaignEvent,
    CampaignEventType,
    CampaignExperimentUse,
    CampaignLifecycle,
    CryptoResearchCampaign,
    DatasetExposureRecord,
    ExposureCategory,
    HoldoutLifecycleEvent,
    HoldoutState,
    SnapshotUsePurpose,
    holdout_state,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.research_evidence import EvidenceClass

metadata = sa.MetaData()


def _evidence_table(name: str, identity: str, *columns: Any) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(identity, sa.String(160), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
    )


crypto_dataset_exposure = _evidence_table(
    "crypto_dataset_exposure",
    "exposure_id",
    sa.Column("snapshot_id", sa.String(160), nullable=False),
    sa.Column("category", sa.String(48), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
)
crypto_research_campaign = _evidence_table(
    "crypto_research_campaign",
    "campaign_id",
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("lifecycle_state", sa.String(48), nullable=False),
    sa.Column("candidate_budget", sa.Integer(), nullable=False),
    sa.Column("experiment_budget", sa.Integer(), nullable=False),
    sa.Column("holdout_snapshot_id", sa.String(160), nullable=False),
)
crypto_campaign_dataset = _evidence_table(
    "crypto_campaign_dataset",
    "attachment_id",
    sa.Column("campaign_id", sa.String(160), nullable=False),
    sa.Column("snapshot_id", sa.String(160), nullable=False),
    sa.Column("role", sa.String(32), nullable=False),
)
crypto_campaign_candidate = _evidence_table(
    "crypto_campaign_candidate",
    "candidate_id",
    sa.Column("campaign_id", sa.String(160), nullable=False),
    sa.Column("strategy_family", sa.String(160), nullable=False),
    sa.Column("strategy_semantics_hash", sa.String(64), nullable=False),
)
crypto_campaign_experiment = _evidence_table(
    "crypto_campaign_experiment",
    "consumption_id",
    sa.Column("campaign_id", sa.String(160), nullable=False),
    sa.Column("candidate_id", sa.String(160), nullable=False),
    sa.Column("run_id", sa.String(160), nullable=False),
    sa.Column("snapshot_id", sa.String(160), nullable=False),
)
crypto_campaign_event = _evidence_table(
    "crypto_campaign_event",
    "event_id",
    sa.Column("campaign_id", sa.String(160), nullable=False),
    sa.Column("event_type", sa.String(48), nullable=False),
    sa.Column("candidate_id", sa.String(160)),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
)
crypto_holdout_event = _evidence_table(
    "crypto_holdout_event",
    "event_id",
    sa.Column("holdout_id", sa.String(160), nullable=False),
    sa.Column("campaign_id", sa.String(160), nullable=False),
    sa.Column("state", sa.String(48), nullable=False),
    sa.Column("candidate_id", sa.String(160)),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
)


def _exposure(row: RowMapping) -> DatasetExposureRecord:
    value = _data(row)
    return DatasetExposureRecord(
        exposure_id=str(value["exposure_id"]),
        snapshot_id=str(value["snapshot_id"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        purpose=str(value["purpose"]),
        category=ExposureCategory(value["category"]),
        first_known_exposure_at=_time(value["first_known_exposure_at"]),
        recorded_at=_time(value["recorded_at"]),
        actor=str(value["actor"]),
        related_hypothesis_ids=_strings(value["related_hypothesis_ids"]),
        related_strategy_ids=_strings(value["related_strategy_ids"]),
        related_protocol_ids=_strings(value["related_protocol_ids"]),
        related_experiment_ids=_strings(value["related_experiment_ids"]),
        related_dossier_ids=_strings(value["related_dossier_ids"]),
        independent_validation_eligible=bool(value["independent_validation_eligible"]),
        reason=str(value["reason"]),
        known_limitations=_strings(value["known_limitations"]),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
    )


def _campaign(row: RowMapping) -> CryptoResearchCampaign:
    value = _data(row)
    development = _strings(value["allowed_development_snapshot_ids"])
    if len(development) != 2:
        raise ValueError("CRYPTO_CAMPAIGN_DEVELOPMENT_SNAPSHOTS_INVALID")
    return CryptoResearchCampaign(
        campaign_id=str(value["campaign_id"]),
        version=int(value["version"]),
        question=str(value["question"]),
        motivation=str(value["motivation"]),
        product_id=str(value["product_id"]),
        granularity=str(value["granularity"]),
        allowed_development_snapshot_ids=(development[0], development[1]),
        forbidden_snapshot_ids=_strings(value["forbidden_snapshot_ids"]),
        allowed_evidence_classes=tuple(
            EvidenceClass(item) for item in _strings(value["allowed_evidence_classes"])
        ),
        allowed_feature_families=_strings(value["allowed_feature_families"]),
        strategy_constraints=_strings(value["strategy_constraints"]),
        candidate_budget=int(value["candidate_budget"]),
        experiment_budget=int(value["experiment_budget"]),
        comparison_baselines=_strings(value["comparison_baselines"]),
        fee_bps_per_side=str(value["fee_bps_per_side"]),
        slippage_bps_per_side=str(value["slippage_bps_per_side"]),
        selection_policy=_strings(value["selection_policy"]),
        stop_policy=str(value["stop_policy"]),
        holdout_id=str(value["holdout_id"]),
        holdout_capture_id=str(value["holdout_capture_id"]),
        holdout_snapshot_id=str(value["holdout_snapshot_id"]),
        holdout_start=_time(value["holdout_start"]),
        holdout_end=_time(value["holdout_end"]),
        holdout_release_policy=_strings(value["holdout_release_policy"]),
        actor=str(value["actor"]),
        created_at=_time(value["created_at"]),
        lifecycle_state=CampaignLifecycle(value["lifecycle_state"]),
        warnings=_strings(value["warnings"]),
        canonical_hash=str(value["canonical_hash"]),
        paper_eligible=bool(value["paper_eligible"]),
        live_eligible=bool(value["live_eligible"]),
        capital_authority=str(value["capital_authority"]),
        opportunity_authority=str(value["opportunity_authority"]),
        order_authority=str(value["order_authority"]),
        authority=str(value["authority"]),
    )


def _attachment(row: RowMapping) -> CampaignDatasetAttachment:
    value = _data(row)
    return CampaignDatasetAttachment(
        attachment_id=str(value["attachment_id"]),
        campaign_id=str(value["campaign_id"]),
        snapshot_id=str(value["snapshot_id"]),
        manifest_hash=str(value["manifest_hash"]),
        role=CampaignDatasetRole(value["role"]),
        exposure_state_at_attachment=str(value["exposure_state_at_attachment"]),
        attached_at=_time(value["attached_at"]),
        actor=str(value["actor"]),
        reason=str(value["reason"]),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
    )


def _candidate(row: RowMapping) -> CampaignCandidate:
    value = _data(row)
    return CampaignCandidate(
        candidate_id=str(value["candidate_id"]),
        campaign_id=str(value["campaign_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        strategy_family=str(value["strategy_family"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        rationale=str(value["rationale"]),
        expected_mechanism=str(value["expected_mechanism"]),
        failure_conditions=_strings(value["failure_conditions"]),
        exact_configuration_hash=str(value["exact_configuration_hash"]),
        registered_at=_time(value["registered_at"]),
        actor=str(value["actor"]),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
    )


def _experiment(row: RowMapping) -> CampaignExperimentUse:
    value = _data(row)
    return CampaignExperimentUse(
        consumption_id=str(value["consumption_id"]),
        campaign_id=str(value["campaign_id"]),
        candidate_id=str(value["candidate_id"]),
        run_id=str(value["run_id"]),
        snapshot_id=str(value["snapshot_id"]),
        snapshot_exposure_state=str(value["snapshot_exposure_state"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        use_purpose=SnapshotUsePurpose(value["use_purpose"]),
        complete=bool(value["complete"]),
        reproducible=bool(value["reproducible"]),
        window_policy_passed=bool(value["window_policy_passed"]),
        result_hash=str(value["result_hash"]),
        recorded_at=_time(value["recorded_at"]),
        actor=str(value["actor"]),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
    )


def _campaign_event(row: RowMapping) -> CampaignEvent:
    value = _data(row)
    return CampaignEvent(
        event_id=str(value["event_id"]),
        campaign_id=str(value["campaign_id"]),
        event_type=CampaignEventType(value["event_type"]),
        candidate_id=str(value["candidate_id"]) if value["candidate_id"] else None,
        prior_state=CampaignLifecycle(value["prior_state"]),
        resulting_state=CampaignLifecycle(value["resulting_state"]),
        actor=str(value["actor"]),
        reason=str(value["reason"]),
        occurred_at=_time(value["occurred_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
    )


def _holdout_event(row: RowMapping) -> HoldoutLifecycleEvent:
    value = _data(row)
    return HoldoutLifecycleEvent(
        event_id=str(value["event_id"]),
        holdout_id=str(value["holdout_id"]),
        campaign_id=str(value["campaign_id"]),
        state=HoldoutState(value["state"]),
        prior_state=HoldoutState(value["prior_state"]) if value["prior_state"] else None,
        capture_id=str(value["capture_id"]),
        snapshot_id=str(value["snapshot_id"]),
        candidate_id=str(value["candidate_id"]) if value["candidate_id"] else None,
        actor=str(value["actor"]),
        reason=str(value["reason"]),
        occurred_at=_time(value["occurred_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
    )


T = TypeVar("T")


class PostgresCryptoCampaignRepository(PostgresCryptoChallengerRepository):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

    def _stored(
        self,
        table: sa.Table,
        column: sa.Column[Any],
        identity: str,
        parser: Callable[[RowMapping], T],
    ) -> T | None:
        row = self._one(sa.select(table).where(column == identity))
        return parser(row) if row is not None else None

    def exposure(self, exposure_id: str) -> DatasetExposureRecord | None:
        return self._stored(
            crypto_dataset_exposure,
            crypto_dataset_exposure.c.exposure_id,
            exposure_id,
            _exposure,
        )

    def save_exposure(self, value: DatasetExposureRecord) -> DatasetExposureRecord:
        self._insert(
            crypto_dataset_exposure,
            {
                "exposure_id": value.exposure_id,
                "snapshot_id": value.snapshot_id,
                "category": value.category.value,
                "recorded_at": value.recorded_at,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_EXPOSURE_PERSISTENCE_CONFLICT",
        )
        return value

    def exposures(self, snapshot_id: str | None = None) -> tuple[DatasetExposureRecord, ...]:
        query = sa.select(crypto_dataset_exposure)
        if snapshot_id is not None:
            query = query.where(crypto_dataset_exposure.c.snapshot_id == snapshot_id)
        query = query.order_by(
            crypto_dataset_exposure.c.recorded_at, crypto_dataset_exposure.c.exposure_id
        )
        return tuple(_exposure(row) for row in self._rows(query))

    def campaign(self, campaign_id: str) -> CryptoResearchCampaign | None:
        return self._stored(
            crypto_research_campaign,
            crypto_research_campaign.c.campaign_id,
            campaign_id,
            _campaign,
        )

    def save_campaign(self, value: CryptoResearchCampaign) -> CryptoResearchCampaign:
        self._insert(
            crypto_research_campaign,
            {
                "campaign_id": value.campaign_id,
                "version": value.version,
                "lifecycle_state": value.lifecycle_state.value,
                "candidate_budget": value.candidate_budget,
                "experiment_budget": value.experiment_budget,
                "holdout_snapshot_id": value.holdout_snapshot_id,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_PERSISTENCE_CONFLICT",
        )
        return value

    def campaigns(self) -> tuple[CryptoResearchCampaign, ...]:
        return tuple(
            _campaign(row)
            for row in self._rows(
                sa.select(crypto_research_campaign).order_by(crypto_research_campaign.c.campaign_id)
            )
        )

    def campaign_attachment(self, attachment_id: str) -> CampaignDatasetAttachment | None:
        return self._stored(
            crypto_campaign_dataset,
            crypto_campaign_dataset.c.attachment_id,
            attachment_id,
            _attachment,
        )

    def save_campaign_attachment(
        self, value: CampaignDatasetAttachment
    ) -> CampaignDatasetAttachment:
        self._insert(
            crypto_campaign_dataset,
            {
                "attachment_id": value.attachment_id,
                "campaign_id": value.campaign_id,
                "snapshot_id": value.snapshot_id,
                "role": value.role.value,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_DATASET_PERSISTENCE_CONFLICT",
        )
        return value

    def campaign_attachments(self, campaign_id: str) -> tuple[CampaignDatasetAttachment, ...]:
        return tuple(
            _attachment(row)
            for row in self._rows(
                sa.select(crypto_campaign_dataset)
                .where(crypto_campaign_dataset.c.campaign_id == campaign_id)
                .order_by(crypto_campaign_dataset.c.attachment_id)
            )
        )

    def campaign_candidate(self, candidate_id: str) -> CampaignCandidate | None:
        return self._stored(
            crypto_campaign_candidate,
            crypto_campaign_candidate.c.candidate_id,
            candidate_id,
            _candidate,
        )

    def save_campaign_candidate(self, value: CampaignCandidate) -> CampaignCandidate:
        self._insert(
            crypto_campaign_candidate,
            {
                "candidate_id": value.candidate_id,
                "campaign_id": value.campaign_id,
                "strategy_family": value.strategy_family,
                "strategy_semantics_hash": value.strategy_semantics_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_PERSISTENCE_CONFLICT",
        )
        return value

    def campaign_candidates(self, campaign_id: str) -> tuple[CampaignCandidate, ...]:
        return tuple(
            _candidate(row)
            for row in self._rows(
                sa.select(crypto_campaign_candidate)
                .where(crypto_campaign_candidate.c.campaign_id == campaign_id)
                .order_by(crypto_campaign_candidate.c.candidate_id)
            )
        )

    def campaign_experiment(self, consumption_id: str) -> CampaignExperimentUse | None:
        return self._stored(
            crypto_campaign_experiment,
            crypto_campaign_experiment.c.consumption_id,
            consumption_id,
            _experiment,
        )

    def save_campaign_experiment(self, value: CampaignExperimentUse) -> CampaignExperimentUse:
        self._insert(
            crypto_campaign_experiment,
            {
                "consumption_id": value.consumption_id,
                "campaign_id": value.campaign_id,
                "candidate_id": value.candidate_id,
                "run_id": value.run_id,
                "snapshot_id": value.snapshot_id,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_EXPERIMENT_PERSISTENCE_CONFLICT",
        )
        return value

    def campaign_experiments(self, campaign_id: str) -> tuple[CampaignExperimentUse, ...]:
        return tuple(
            _experiment(row)
            for row in self._rows(
                sa.select(crypto_campaign_experiment)
                .where(crypto_campaign_experiment.c.campaign_id == campaign_id)
                .order_by(crypto_campaign_experiment.c.consumption_id)
            )
        )

    def campaign_event(self, event_id: str) -> CampaignEvent | None:
        return self._stored(
            crypto_campaign_event,
            crypto_campaign_event.c.event_id,
            event_id,
            _campaign_event,
        )

    def save_campaign_event(self, value: CampaignEvent) -> CampaignEvent:
        self._insert(
            crypto_campaign_event,
            {
                "event_id": value.event_id,
                "campaign_id": value.campaign_id,
                "event_type": value.event_type.value,
                "candidate_id": value.candidate_id,
                "occurred_at": value.occurred_at,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_EVENT_PERSISTENCE_CONFLICT",
        )
        return value

    def campaign_events(self, campaign_id: str) -> tuple[CampaignEvent, ...]:
        return tuple(
            _campaign_event(row)
            for row in self._rows(
                sa.select(crypto_campaign_event)
                .where(crypto_campaign_event.c.campaign_id == campaign_id)
                .order_by(crypto_campaign_event.c.occurred_at, crypto_campaign_event.c.event_id)
            )
        )

    def holdout_event(self, event_id: str) -> HoldoutLifecycleEvent | None:
        return self._stored(
            crypto_holdout_event,
            crypto_holdout_event.c.event_id,
            event_id,
            _holdout_event,
        )

    def save_holdout_event(self, value: HoldoutLifecycleEvent) -> HoldoutLifecycleEvent:
        self._insert(
            crypto_holdout_event,
            {
                "event_id": value.event_id,
                "holdout_id": value.holdout_id,
                "campaign_id": value.campaign_id,
                "state": value.state.value,
                "candidate_id": value.candidate_id,
                "occurred_at": value.occurred_at,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_HOLDOUT_EVENT_PERSISTENCE_CONFLICT",
        )
        return value

    def holdout_events(self, holdout_id: str) -> tuple[HoldoutLifecycleEvent, ...]:
        return tuple(
            _holdout_event(row)
            for row in self._rows(
                sa.select(crypto_holdout_event)
                .where(crypto_holdout_event.c.holdout_id == holdout_id)
                .order_by(crypto_holdout_event.c.occurred_at, crypto_holdout_event.c.event_id)
            )
        )

    def _sealed_capture(self, capture_id: str) -> bool:
        campaigns = self.campaigns()
        campaign = next((item for item in campaigns if item.holdout_capture_id == capture_id), None)
        return campaign is not None and holdout_state(self.holdout_events(campaign.holdout_id)) in {
            HoldoutState.SEALED,
            HoldoutState.RELEASE_AUTHORIZED,
            HoldoutState.EVALUATED,
        }

    def candles(self, capture_id: str) -> tuple[CryptoDailyCandle, ...]:
        if self._sealed_capture(capture_id):
            raise CryptoResearchConflict("CRYPTO_SEALED_HOLDOUT_VALUES_REDACTED")
        return super().candles(capture_id)

    def raw_response(self, attempt_id: str) -> bytes | None:
        attempt = next(
            (
                item
                for campaign in self.campaigns()
                for item in self.attempts(campaign.holdout_capture_id)
                if item.attempt_id == attempt_id
            ),
            None,
        )
        if attempt is not None and self._sealed_capture(attempt.capture_id):
            raise CryptoResearchConflict("CRYPTO_SEALED_HOLDOUT_RAW_RESPONSE_REDACTED")
        return super().raw_response(attempt_id)

    def authorized_holdout_candles(
        self, *, capture_id: str, campaign_id: str, candidate_id: str
    ) -> tuple[CryptoDailyCandle, ...]:
        campaign = self.campaign(campaign_id)
        if campaign is None or campaign.holdout_capture_id != capture_id:
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_RELEASE_BINDING_MISMATCH")
        events = self.holdout_events(campaign.holdout_id)
        if holdout_state(events) is not HoldoutState.RELEASE_AUTHORIZED:
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_NOT_RELEASE_AUTHORIZED")
        release = next(item for item in events if item.state is HoldoutState.RELEASE_AUTHORIZED)
        if release.candidate_id != candidate_id:
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_RELEASE_BINDING_MISMATCH")
        return super().candles(capture_id)


__all__ = ["PostgresCryptoCampaignRepository", "metadata"]
