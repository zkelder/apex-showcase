"""Application services for crypto dataset exposure and sealed holdout governance."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, TypeVar, cast

from apex.application.crypto_challenger import (
    CryptoChallengerRepository,
    InMemoryCryptoChallengerRepository,
    crypto_challenger_workspace,
)
from apex.application.crypto_research import (
    CaptureCoinbaseHistory,
    CoinbaseHistoricalSource,
    CryptoResearchConflict,
    CryptoResearchNotFound,
    MaterializeCryptoHistoricalSnapshot,
)
from apex.domain.crypto_campaign import (
    FIXED_CAMPAIGN_ID,
    FIXED_HOLDOUT_END,
    FIXED_HOLDOUT_SNAPSHOT_ID,
    FIXED_HOLDOUT_START,
    CampaignBudgetStatus,
    CampaignCandidate,
    CampaignDatasetAttachment,
    CampaignDatasetRole,
    CampaignEvent,
    CampaignEventType,
    CampaignExperimentUse,
    CampaignLifecycle,
    CryptoResearchCampaign,
    DatasetExposureRecord,
    DatasetExposureSummary,
    ExposureCategory,
    HoldoutLifecycleEvent,
    HoldoutMetadata,
    HoldoutState,
    SnapshotAccessDecision,
    SnapshotUsePurpose,
    campaign_state,
    decide_snapshot_access,
    holdout_state,
)
from apex.domain.crypto_dossier import NO_APPROVED_CHAMPION
from apex.domain.crypto_research import (
    CryptoCaptureQuality,
    CryptoDailyCandle,
    CryptoHistoricalSnapshot,
)
from apex.domain.research_evidence import CaptureStatus, EvidenceClass


def _key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise CryptoResearchConflict("CRYPTO_IDEMPOTENCY_KEY_REQUIRED")
    return normalized


class CryptoCampaignRepository(CryptoChallengerRepository, Protocol):
    def exposure(self, exposure_id: str) -> DatasetExposureRecord | None: ...
    def save_exposure(self, value: DatasetExposureRecord) -> DatasetExposureRecord: ...
    def exposures(self, snapshot_id: str | None = None) -> tuple[DatasetExposureRecord, ...]: ...

    def campaign(self, campaign_id: str) -> CryptoResearchCampaign | None: ...
    def save_campaign(self, value: CryptoResearchCampaign) -> CryptoResearchCampaign: ...
    def campaigns(self) -> tuple[CryptoResearchCampaign, ...]: ...

    def campaign_attachment(self, attachment_id: str) -> CampaignDatasetAttachment | None: ...
    def save_campaign_attachment(
        self, value: CampaignDatasetAttachment
    ) -> CampaignDatasetAttachment: ...
    def campaign_attachments(self, campaign_id: str) -> tuple[CampaignDatasetAttachment, ...]: ...

    def campaign_candidate(self, candidate_id: str) -> CampaignCandidate | None: ...
    def save_campaign_candidate(self, value: CampaignCandidate) -> CampaignCandidate: ...
    def campaign_candidates(self, campaign_id: str) -> tuple[CampaignCandidate, ...]: ...

    def campaign_experiment(self, consumption_id: str) -> CampaignExperimentUse | None: ...
    def save_campaign_experiment(self, value: CampaignExperimentUse) -> CampaignExperimentUse: ...
    def campaign_experiments(self, campaign_id: str) -> tuple[CampaignExperimentUse, ...]: ...

    def campaign_event(self, event_id: str) -> CampaignEvent | None: ...
    def save_campaign_event(self, value: CampaignEvent) -> CampaignEvent: ...
    def campaign_events(self, campaign_id: str) -> tuple[CampaignEvent, ...]: ...

    def holdout_event(self, event_id: str) -> HoldoutLifecycleEvent | None: ...
    def save_holdout_event(self, value: HoldoutLifecycleEvent) -> HoldoutLifecycleEvent: ...
    def holdout_events(self, holdout_id: str) -> tuple[HoldoutLifecycleEvent, ...]: ...
    def authorized_holdout_candles(
        self, *, capture_id: str, campaign_id: str, candidate_id: str
    ) -> tuple[CryptoDailyCandle, ...]: ...


class _Canonical(Protocol):
    @property
    def canonical_hash(self) -> str: ...


_CanonicalT = TypeVar("_CanonicalT", bound=_Canonical)


class InMemoryCryptoCampaignRepository(InMemoryCryptoChallengerRepository):
    def __init__(self) -> None:
        super().__init__()
        self._exposures: dict[str, DatasetExposureRecord] = {}
        self._campaigns: dict[str, CryptoResearchCampaign] = {}
        self._campaign_attachments: dict[str, CampaignDatasetAttachment] = {}
        self._campaign_candidates: dict[str, CampaignCandidate] = {}
        self._campaign_experiments: dict[str, CampaignExperimentUse] = {}
        self._campaign_events: dict[str, CampaignEvent] = {}
        self._holdout_events: dict[str, HoldoutLifecycleEvent] = {}

    @staticmethod
    def _immutable(prior: _CanonicalT | None, value: _CanonicalT, code: str) -> _CanonicalT:
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict(code)
        return prior or value

    def exposure(self, exposure_id: str) -> DatasetExposureRecord | None:
        return self._exposures.get(exposure_id)

    def save_exposure(self, value: DatasetExposureRecord) -> DatasetExposureRecord:
        prior = self.exposure(value.exposure_id)
        retained = self._immutable(prior, value, "CRYPTO_EXPOSURE_IDENTITY_CONFLICT")
        self._exposures[value.exposure_id] = retained
        return retained

    def exposures(self, snapshot_id: str | None = None) -> tuple[DatasetExposureRecord, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._exposures.values()
                    if snapshot_id is None or item.snapshot_id == snapshot_id
                ),
                key=lambda item: (item.recorded_at, item.exposure_id),
            )
        )

    def campaign(self, campaign_id: str) -> CryptoResearchCampaign | None:
        return self._campaigns.get(campaign_id)

    def save_campaign(self, value: CryptoResearchCampaign) -> CryptoResearchCampaign:
        prior = self.campaign(value.campaign_id)
        retained = self._immutable(prior, value, "CRYPTO_CAMPAIGN_IDENTITY_CONFLICT")
        self._campaigns[value.campaign_id] = retained
        return retained

    def campaigns(self) -> tuple[CryptoResearchCampaign, ...]:
        return tuple(sorted(self._campaigns.values(), key=lambda item: item.campaign_id))

    def campaign_attachment(self, attachment_id: str) -> CampaignDatasetAttachment | None:
        return self._campaign_attachments.get(attachment_id)

    def save_campaign_attachment(
        self, value: CampaignDatasetAttachment
    ) -> CampaignDatasetAttachment:
        prior = self.campaign_attachment(value.attachment_id)
        duplicate = next(
            (
                item
                for item in self._campaign_attachments.values()
                if item.campaign_id == value.campaign_id
                and item.snapshot_id == value.snapshot_id
                and item.role is value.role
            ),
            None,
        )
        if duplicate is not None and duplicate.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_DATASET_ALREADY_ATTACHED")
        retained = self._immutable(prior, value, "CRYPTO_CAMPAIGN_ATTACHMENT_CONFLICT")
        self._campaign_attachments[value.attachment_id] = retained
        return retained

    def campaign_attachments(self, campaign_id: str) -> tuple[CampaignDatasetAttachment, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._campaign_attachments.values()
                    if item.campaign_id == campaign_id
                ),
                key=lambda item: item.attachment_id,
            )
        )

    def campaign_candidate(self, candidate_id: str) -> CampaignCandidate | None:
        return self._campaign_candidates.get(candidate_id)

    def save_campaign_candidate(self, value: CampaignCandidate) -> CampaignCandidate:
        prior = self.campaign_candidate(value.candidate_id)
        retained = self._immutable(prior, value, "CRYPTO_CAMPAIGN_CANDIDATE_CONFLICT")
        self._campaign_candidates[value.candidate_id] = retained
        return retained

    def campaign_candidates(self, campaign_id: str) -> tuple[CampaignCandidate, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._campaign_candidates.values()
                    if item.campaign_id == campaign_id
                ),
                key=lambda item: item.candidate_id,
            )
        )

    def campaign_experiment(self, consumption_id: str) -> CampaignExperimentUse | None:
        return self._campaign_experiments.get(consumption_id)

    def save_campaign_experiment(self, value: CampaignExperimentUse) -> CampaignExperimentUse:
        prior = self.campaign_experiment(value.consumption_id)
        retained = self._immutable(prior, value, "CRYPTO_CAMPAIGN_EXPERIMENT_CONFLICT")
        self._campaign_experiments[value.consumption_id] = retained
        return retained

    def campaign_experiments(self, campaign_id: str) -> tuple[CampaignExperimentUse, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._campaign_experiments.values()
                    if item.campaign_id == campaign_id
                ),
                key=lambda item: item.consumption_id,
            )
        )

    def campaign_event(self, event_id: str) -> CampaignEvent | None:
        return self._campaign_events.get(event_id)

    def save_campaign_event(self, value: CampaignEvent) -> CampaignEvent:
        prior = self.campaign_event(value.event_id)
        retained = self._immutable(prior, value, "CRYPTO_CAMPAIGN_EVENT_CONFLICT")
        self._campaign_events[value.event_id] = retained
        return retained

    def campaign_events(self, campaign_id: str) -> tuple[CampaignEvent, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._campaign_events.values()
                    if item.campaign_id == campaign_id
                ),
                key=lambda item: (item.occurred_at, item.event_id),
            )
        )

    def holdout_event(self, event_id: str) -> HoldoutLifecycleEvent | None:
        return self._holdout_events.get(event_id)

    def save_holdout_event(self, value: HoldoutLifecycleEvent) -> HoldoutLifecycleEvent:
        prior = self.holdout_event(value.event_id)
        retained = self._immutable(prior, value, "CRYPTO_HOLDOUT_EVENT_CONFLICT")
        self._holdout_events[value.event_id] = retained
        return retained

    def holdout_events(self, holdout_id: str) -> tuple[HoldoutLifecycleEvent, ...]:
        return tuple(
            sorted(
                (item for item in self._holdout_events.values() if item.holdout_id == holdout_id),
                key=lambda item: (item.occurred_at, item.event_id),
            )
        )

    def _sealed_capture(self, capture_id: str) -> bool:
        campaign = next(
            (item for item in self._campaigns.values() if item.holdout_capture_id == capture_id),
            None,
        )
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
                for campaign in self._campaigns.values()
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


class RecordDatasetExposure:
    def __init__(self, repository: CryptoCampaignRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        exposure_id: str,
        snapshot_id: str,
        purpose: str,
        category: ExposureCategory,
        first_known_exposure_at: datetime,
        recorded_at: datetime,
        actor: str,
        reason: str,
        idempotency_key: str,
        related_hypothesis_ids: tuple[str, ...] = (),
        related_strategy_ids: tuple[str, ...] = (),
        related_protocol_ids: tuple[str, ...] = (),
        related_experiment_ids: tuple[str, ...] = (),
        related_dossier_ids: tuple[str, ...] = (),
        known_limitations: tuple[str, ...] = (),
    ) -> DatasetExposureRecord:
        _key(idempotency_key)
        prior = self._repository.exposure(exposure_id)
        if prior is not None:
            return prior
        snapshot = self._repository.snapshot(snapshot_id)
        if snapshot is None:
            raise CryptoResearchNotFound("CRYPTO_SNAPSHOT_NOT_FOUND")
        summary = DatasetExposureSummary.from_records(
            snapshot_id, self._repository.exposures(snapshot_id)
        )
        if summary.current_state == "EXPOSED_DEVELOPMENT_EVIDENCE" and category is (
            ExposureCategory.SEALED_HOLDOUT
        ):
            raise CryptoResearchConflict("CRYPTO_EXPOSURE_CANNOT_REVERSE_TO_SEALED")
        if category in {
            ExposureCategory.HOLDOUT_RELEASED,
            ExposureCategory.HOLDOUT_EVALUATED,
        } and (
            not summary.categories
            or summary.categories[-1]
            not in {
                ExposureCategory.SEALED_HOLDOUT,
                ExposureCategory.HOLDOUT_RELEASED,
            }
        ):
            raise CryptoResearchConflict("CRYPTO_EXPOSURE_HOLDOUT_SEQUENCE_INVALID")
        candidate = DatasetExposureRecord.create(
            exposure_id=exposure_id,
            snapshot=snapshot,
            purpose=purpose,
            category=category,
            first_known_exposure_at=first_known_exposure_at,
            recorded_at=recorded_at,
            actor=actor,
            related_hypothesis_ids=related_hypothesis_ids,
            related_strategy_ids=related_strategy_ids,
            related_protocol_ids=related_protocol_ids,
            related_experiment_ids=related_experiment_ids,
            related_dossier_ids=related_dossier_ids,
            independent_validation_eligible=(
                category is ExposureCategory.SEALED_HOLDOUT
                and snapshot.evidence_class is EvidenceClass.HISTORICAL_PUBLIC
            ),
            reason=reason,
            known_limitations=known_limitations,
        )
        return self._repository.save_exposure(candidate)


class RegisterCryptoCampaign:
    def __init__(self, repository: CryptoCampaignRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        campaign_id: str,
        actor: str,
        created_at: datetime,
        idempotency_key: str,
    ) -> CryptoResearchCampaign:
        _key(idempotency_key)
        prior = self._repository.campaign(campaign_id)
        if prior is not None:
            return prior
        return self._repository.save_campaign(
            CryptoResearchCampaign.fixed(
                campaign_id=campaign_id, actor=actor, created_at=created_at
            )
        )


class AttachCampaignDataset:
    def __init__(self, repository: CryptoCampaignRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        attachment_id: str,
        campaign_id: str,
        snapshot_id: str,
        role: CampaignDatasetRole,
        actor: str,
        reason: str,
        attached_at: datetime,
        idempotency_key: str,
    ) -> CampaignDatasetAttachment:
        _key(idempotency_key)
        prior = self._repository.campaign_attachment(attachment_id)
        if prior is not None:
            return prior
        campaign = self._required_campaign(campaign_id)
        snapshot = self._required_snapshot(snapshot_id)
        exposure = DatasetExposureSummary.from_records(
            snapshot_id, self._repository.exposures(snapshot_id)
        )
        return self._repository.save_campaign_attachment(
            CampaignDatasetAttachment.create(
                attachment_id=attachment_id,
                campaign=campaign,
                snapshot=snapshot,
                role=role,
                exposure_state_at_attachment=exposure.current_state,
                attached_at=attached_at,
                actor=actor,
                reason=reason,
            )
        )

    def _required_campaign(self, campaign_id: str) -> CryptoResearchCampaign:
        campaign = self._repository.campaign(campaign_id)
        if campaign is None:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_NOT_FOUND")
        return campaign

    def _required_snapshot(self, snapshot_id: str) -> CryptoHistoricalSnapshot:
        snapshot = self._repository.snapshot(snapshot_id)
        if snapshot is None:
            raise CryptoResearchNotFound("CRYPTO_SNAPSHOT_NOT_FOUND")
        return snapshot


class RegisterCampaignCandidate:
    def __init__(self, repository: CryptoCampaignRepository) -> None:
        self._repository = repository

    def execute(self, value: CampaignCandidate, *, idempotency_key: str) -> CampaignCandidate:
        _key(idempotency_key)
        prior = self._repository.campaign_candidate(value.candidate_id)
        if prior is not None:
            if prior.canonical_hash != value.canonical_hash:
                raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_CONFLICT")
            return prior
        campaign = _campaign(self._repository, value.campaign_id)
        if campaign_state(campaign, self._repository.campaign_events(value.campaign_id)) is not (
            CampaignLifecycle.DEVELOPMENT_ACTIVE
        ):
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_NOT_DEVELOPMENT_ACTIVE")
        candidates = self._repository.campaign_candidates(value.campaign_id)
        if len(candidates) >= campaign.candidate_budget:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_BUDGET_EXHAUSTED")
        if any(
            item.strategy_family == value.strategy_family
            or item.strategy_semantics_hash == value.strategy_semantics_hash
            for item in candidates
        ):
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_DUPLICATE_FAMILY_OR_SEMANTICS")
        tested = self._repository.strategy_by_semantics(value.strategy_semantics_hash)
        if tested is not None and tested.specification_id != value.strategy_specification_id:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_PRIOR_SEMANTICS_ALREADY_TESTED")
        return self._repository.save_campaign_candidate(value)


class RecordCampaignExperiment:
    def __init__(self, repository: CryptoCampaignRepository) -> None:
        self._repository = repository

    def execute(
        self, value: CampaignExperimentUse, *, idempotency_key: str
    ) -> CampaignExperimentUse:
        _key(idempotency_key)
        prior = self._repository.campaign_experiment(value.consumption_id)
        if prior is not None:
            if prior.canonical_hash != value.canonical_hash:
                raise CryptoResearchConflict("CRYPTO_CAMPAIGN_EXPERIMENT_CONFLICT")
            return prior
        campaign = _campaign(self._repository, value.campaign_id)
        candidate = self._repository.campaign_candidate(value.candidate_id)
        if candidate is None or candidate.strategy_semantics_hash != value.strategy_semantics_hash:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_BINDING_INVALID")
        if value.snapshot_id not in campaign.allowed_development_snapshot_ids:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_DEVELOPMENT_SNAPSHOT_NOT_ALLOWED")
        exact_run = next(
            (
                item
                for item in self._repository.campaign_experiments(value.campaign_id)
                if item.run_id == value.run_id
            ),
            None,
        )
        if exact_run is not None:
            if (
                exact_run.result_hash == value.result_hash
                and exact_run.strategy_semantics_hash == value.strategy_semantics_hash
                and exact_run.snapshot_id == value.snapshot_id
            ):
                return exact_run
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_RUN_IDENTITY_CONFLICT")
        if (
            len(self._repository.campaign_experiments(value.campaign_id))
            >= campaign.experiment_budget
        ):
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_EXPERIMENT_BUDGET_EXHAUSTED")
        return self._repository.save_campaign_experiment(value)


class NominateCampaignCandidate:
    def __init__(self, repository: CryptoCampaignRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        event_id: str,
        campaign_id: str,
        candidate_id: str,
        actor: str,
        reason: str,
        nominated_at: datetime,
        idempotency_key: str,
    ) -> CampaignEvent:
        _key(idempotency_key)
        prior = self._repository.campaign_event(event_id)
        if prior is not None:
            return prior
        campaign = _campaign(self._repository, campaign_id)
        candidate = self._repository.campaign_candidate(candidate_id)
        if candidate is None or candidate.campaign_id != campaign_id:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_NOT_FOUND")
        if campaign_state(campaign, self._repository.campaign_events(campaign_id)) is not (
            CampaignLifecycle.DEVELOPMENT_ACTIVE
        ):
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_NOT_DEVELOPMENT_ACTIVE")
        uses = tuple(
            item
            for item in self._repository.campaign_experiments(campaign_id)
            if item.candidate_id == candidate_id
        )
        if (
            len(uses) != 2
            or {item.snapshot_id for item in uses} != set(campaign.allowed_development_snapshot_ids)
            or any(
                not item.complete
                or not item.reproducible
                or not item.window_policy_passed
                or item.strategy_semantics_hash != candidate.strategy_semantics_hash
                for item in uses
            )
        ):
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_NOMINATION_POLICY_NOT_SATISFIED")
        return self._repository.save_campaign_event(
            CampaignEvent.create(
                event_id=event_id,
                campaign_id=campaign_id,
                event_type=CampaignEventType.CANDIDATE_NOMINATED,
                candidate_id=candidate_id,
                prior_state=CampaignLifecycle.DEVELOPMENT_ACTIVE,
                actor=actor,
                reason=reason,
                occurred_at=nominated_at,
            )
        )


class CloseCampaignDevelopment:
    def __init__(self, repository: CryptoCampaignRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        event_id: str,
        campaign_id: str,
        candidate_id: str,
        actor: str,
        reason: str,
        closed_at: datetime,
        idempotency_key: str,
    ) -> CampaignEvent:
        _key(idempotency_key)
        prior = self._repository.campaign_event(event_id)
        if prior is not None:
            return prior
        campaign = _campaign(self._repository, campaign_id)
        events = self._repository.campaign_events(campaign_id)
        if campaign_state(campaign, events) is not CampaignLifecycle.CANDIDATE_NOMINATED:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_NOT_NOMINATED")
        nomination = next(
            item for item in events if item.event_type is CampaignEventType.CANDIDATE_NOMINATED
        )
        if nomination.candidate_id != candidate_id:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_NOMINATED_CANDIDATE_MISMATCH")
        return self._repository.save_campaign_event(
            CampaignEvent.create(
                event_id=event_id,
                campaign_id=campaign_id,
                event_type=CampaignEventType.DEVELOPMENT_CLOSED,
                candidate_id=candidate_id,
                prior_state=CampaignLifecycle.CANDIDATE_NOMINATED,
                actor=actor,
                reason=reason,
                occurred_at=closed_at,
            )
        )


def _campaign(repository: CryptoCampaignRepository, campaign_id: str) -> CryptoResearchCampaign:
    value = repository.campaign(campaign_id)
    if value is None:
        raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_NOT_FOUND")
    return value


def snapshot_access(
    repository: CryptoCampaignRepository,
    *,
    snapshot_id: str,
    purpose: SnapshotUsePurpose,
    campaign_id: str | None = None,
    candidate_id: str | None = None,
) -> SnapshotAccessDecision:
    records = repository.exposures(snapshot_id)
    exposure = DatasetExposureSummary.from_records(snapshot_id, records)
    campaign = repository.campaign(campaign_id or FIXED_CAMPAIGN_ID)
    attachments = (
        repository.campaign_attachments(campaign.campaign_id) if campaign is not None else ()
    )
    events = repository.holdout_events(campaign.holdout_id) if campaign is not None else ()
    return decide_snapshot_access(
        snapshot_id=snapshot_id,
        purpose=purpose,
        exposure=exposure,
        campaign=campaign,
        attachments=attachments,
        holdout_events=events if snapshot_id == FIXED_HOLDOUT_SNAPSHOT_ID else (),
        campaign_id=campaign_id,
        candidate_id=candidate_id,
    )


def load_released_holdout_for_evaluation(
    repository: CryptoCampaignRepository,
    *,
    snapshot_id: str,
    campaign_id: str,
    candidate_id: str,
) -> tuple[CryptoDailyCandle, ...]:
    decision = snapshot_access(
        repository,
        snapshot_id=snapshot_id,
        purpose=SnapshotUsePurpose.HOLDOUT_EVALUATION,
        campaign_id=campaign_id,
        candidate_id=candidate_id,
    )
    if not decision.allowed:
        raise CryptoResearchConflict(decision.reason)
    campaign = _campaign(repository, campaign_id)
    if snapshot_id != campaign.holdout_snapshot_id:
        raise CryptoResearchConflict("CRYPTO_HOLDOUT_RELEASE_BINDING_MISMATCH")
    return repository.authorized_holdout_candles(
        capture_id=campaign.holdout_capture_id,
        campaign_id=campaign_id,
        candidate_id=candidate_id,
    )


class CaptureSealedHoldout:
    def __init__(
        self, repository: CryptoCampaignRepository, source: CoinbaseHistoricalSource
    ) -> None:
        self._repository = repository
        self._source = source
        self._capture = CaptureCoinbaseHistory(repository, source)

    def start(
        self,
        *,
        campaign_id: str,
        capture_id: str,
        actor: str,
        requested_at: datetime,
        idempotency_key: str,
    ) -> object:
        _key(idempotency_key)
        campaign = _campaign(self._repository, campaign_id)
        if capture_id != campaign.holdout_capture_id:
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_CAPTURE_ID_MISMATCH")
        prior = self._repository.capture(capture_id)
        if prior is not None:
            if (
                prior.start != FIXED_HOLDOUT_START
                or prior.end != FIXED_HOLDOUT_END
                or prior.evidence_class is not self._source.evidence_class
            ):
                raise CryptoResearchConflict("CRYPTO_HOLDOUT_CAPTURE_IDENTITY_CONFLICT")
            return self._finish(campaign, prior, actor)
        if not self._repository.holdout_events(campaign.holdout_id):
            self._repository.save_holdout_event(
                HoldoutLifecycleEvent.create(
                    event_id=f"{campaign.holdout_id}:capture-started",
                    campaign=campaign,
                    state=HoldoutState.CAPTURE_IN_PROGRESS,
                    prior_state=None,
                    candidate_id=None,
                    actor=actor,
                    reason="Exact bounded public Coinbase holdout capture started.",
                    occurred_at=requested_at,
                )
            )
        capture = self._capture.start(
            capture_id=capture_id,
            start=FIXED_HOLDOUT_START,
            end=FIXED_HOLDOUT_END,
            requested_at=requested_at,
        )
        return self._finish(campaign, capture, actor)

    def resume(
        self,
        *,
        campaign_id: str,
        capture_id: str,
        actor: str,
        requested_at: datetime,
        idempotency_key: str,
    ) -> object:
        _key(idempotency_key)
        campaign = _campaign(self._repository, campaign_id)
        if capture_id != campaign.holdout_capture_id:
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_CAPTURE_ID_MISMATCH")
        capture = self._capture.resume(capture_id=capture_id, requested_at=requested_at)
        return self._finish(campaign, capture, actor)

    def _finish(self, campaign: CryptoResearchCampaign, capture: object, actor: str) -> object:
        retained = self._repository.capture(campaign.holdout_capture_id)
        if retained is None:
            raise CryptoResearchNotFound("CRYPTO_CAPTURE_NOT_FOUND")
        prior_state = holdout_state(self._repository.holdout_events(campaign.holdout_id))
        target = (
            HoldoutState.READY_TO_SEAL
            if retained.status is CaptureStatus.COMPLETED
            and retained.quality is CryptoCaptureQuality.VALID
            else HoldoutState.CAPTURE_FAILED
            if retained.status in {CaptureStatus.FAILED, CaptureStatus.CAPABILITY_UNAVAILABLE}
            else None
        )
        if target is not None and prior_state is HoldoutState.CAPTURE_IN_PROGRESS:
            self._repository.save_holdout_event(
                HoldoutLifecycleEvent.create(
                    event_id=f"{campaign.holdout_id}:{target.value.lower()}",
                    campaign=campaign,
                    state=target,
                    prior_state=prior_state,
                    candidate_id=None,
                    actor=actor,
                    reason=(
                        "Capture completed with exact valid coverage and is ready to seal."
                        if target is HoldoutState.READY_TO_SEAL
                        else (
                            "Capture terminated without sealable evidence: "
                            f"{retained.failure_reason}."
                        )
                    ),
                    occurred_at=retained.completed_at or retained.requested_at,
                )
            )
        return capture


class MaterializeSealedHoldout:
    def __init__(self, repository: CryptoCampaignRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        campaign_id: str,
        snapshot_id: str,
        created_at: datetime,
        idempotency_key: str,
    ) -> CryptoHistoricalSnapshot:
        _key(idempotency_key)
        campaign = _campaign(self._repository, campaign_id)
        if snapshot_id != campaign.holdout_snapshot_id:
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_SNAPSHOT_ID_MISMATCH")
        if holdout_state(self._repository.holdout_events(campaign.holdout_id)) is not (
            HoldoutState.READY_TO_SEAL
        ):
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_NOT_READY_TO_SEAL")
        return MaterializeCryptoHistoricalSnapshot(self._repository).execute(
            snapshot_id=snapshot_id,
            capture_id=campaign.holdout_capture_id,
            created_at=created_at,
        )


class SealHoldout:
    def __init__(self, repository: CryptoCampaignRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        event_id: str,
        exposure_id: str,
        attachment_id: str,
        campaign_id: str,
        snapshot_id: str,
        actor: str,
        reason: str,
        sealed_at: datetime,
        idempotency_key: str,
    ) -> HoldoutLifecycleEvent:
        _key(idempotency_key)
        prior = self._repository.holdout_event(event_id)
        if prior is not None:
            return prior
        campaign = _campaign(self._repository, campaign_id)
        snapshot = self._repository.snapshot(snapshot_id)
        capture = self._repository.capture(campaign.holdout_capture_id)
        if snapshot is None or capture is None:
            raise CryptoResearchNotFound("CRYPTO_HOLDOUT_EVIDENCE_NOT_FOUND")
        if (
            snapshot_id != campaign.holdout_snapshot_id
            or snapshot.capture_id != campaign.holdout_capture_id
            or snapshot.start != campaign.holdout_start
            or snapshot.end != campaign.holdout_end
            or snapshot.quality is not CryptoCaptureQuality.VALID
            or capture.status is not CaptureStatus.COMPLETED
        ):
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_NOT_SEALABLE")
        prior_state = holdout_state(self._repository.holdout_events(campaign.holdout_id))
        if prior_state is not HoldoutState.READY_TO_SEAL:
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_NOT_READY_TO_SEAL")
        RecordDatasetExposure(self._repository).execute(
            exposure_id=exposure_id,
            snapshot_id=snapshot_id,
            purpose="SINGLE_USE_OPERATIONALLY_SEALED_HISTORICAL_HOLDOUT",
            category=ExposureCategory.SEALED_HOLDOUT,
            first_known_exposure_at=sealed_at,
            recorded_at=sealed_at,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            known_limitations=(
                "RETROSPECTIVE_HISTORY_NOT_FORWARD_EVIDENCE",
                "APPLICATION_ENFORCED_OPERATIONAL_SEALING",
            ),
        )
        AttachCampaignDataset(self._repository).execute(
            attachment_id=attachment_id,
            campaign_id=campaign_id,
            snapshot_id=snapshot_id,
            role=CampaignDatasetRole.HOLDOUT,
            actor=actor,
            reason=reason,
            attached_at=sealed_at,
            idempotency_key=idempotency_key,
        )
        event = HoldoutLifecycleEvent.create(
            event_id=event_id,
            campaign=campaign,
            state=HoldoutState.SEALED,
            prior_state=prior_state,
            candidate_id=None,
            actor=actor,
            reason=reason,
            occurred_at=sealed_at,
        )
        return self._repository.save_holdout_event(event)


class AuthorizeHoldoutRelease:
    def __init__(self, repository: CryptoCampaignRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        campaign_event_id: str,
        holdout_event_id: str,
        exposure_id: str,
        campaign_id: str,
        candidate_id: str,
        actor: str,
        reason: str,
        authorized_at: datetime,
        idempotency_key: str,
    ) -> HoldoutLifecycleEvent:
        _key(idempotency_key)
        campaign = _campaign(self._repository, campaign_id)
        if self._repository.campaign_candidate(candidate_id) is None:
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_NOMINATED_CANDIDATE_REQUIRED")
        current_campaign_state = campaign_state(
            campaign, self._repository.campaign_events(campaign_id)
        )
        if current_campaign_state is not CampaignLifecycle.DEVELOPMENT_CLOSED:
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_DEVELOPMENT_NOT_CLOSED")
        current_holdout_state = holdout_state(self._repository.holdout_events(campaign.holdout_id))
        if current_holdout_state is not HoldoutState.SEALED:
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_NOT_SEALED")
        RecordDatasetExposure(self._repository).execute(
            exposure_id=exposure_id,
            snapshot_id=campaign.holdout_snapshot_id,
            purpose="EXACT_CANDIDATE_BOUND_SINGLE_USE_HOLDOUT_RELEASE",
            category=ExposureCategory.HOLDOUT_RELEASED,
            first_known_exposure_at=authorized_at,
            recorded_at=authorized_at,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            related_strategy_ids=(candidate_id,),
        )
        self._repository.save_campaign_event(
            CampaignEvent.create(
                event_id=campaign_event_id,
                campaign_id=campaign_id,
                event_type=CampaignEventType.HOLDOUT_RELEASE_AUTHORIZED,
                candidate_id=candidate_id,
                prior_state=current_campaign_state,
                actor=actor,
                reason=reason,
                occurred_at=authorized_at,
            )
        )
        return self._repository.save_holdout_event(
            HoldoutLifecycleEvent.create(
                event_id=holdout_event_id,
                campaign=campaign,
                state=HoldoutState.RELEASE_AUTHORIZED,
                prior_state=current_holdout_state,
                candidate_id=candidate_id,
                actor=actor,
                reason=reason,
                occurred_at=authorized_at,
            )
        )


class RecordHoldoutEvaluation:
    def __init__(self, repository: CryptoCampaignRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        campaign_event_id: str,
        holdout_event_id: str,
        exposure_id: str,
        campaign_id: str,
        candidate_id: str,
        actor: str,
        reason: str,
        evaluated_at: datetime,
        idempotency_key: str,
    ) -> HoldoutLifecycleEvent:
        _key(idempotency_key)
        prior = self._repository.holdout_event(holdout_event_id)
        if prior is not None:
            return prior
        campaign = _campaign(self._repository, campaign_id)
        campaign_current = campaign_state(campaign, self._repository.campaign_events(campaign_id))
        holdout_events = self._repository.holdout_events(campaign.holdout_id)
        holdout_current = holdout_state(holdout_events)
        release = next(
            (item for item in holdout_events if item.state is HoldoutState.RELEASE_AUTHORIZED),
            None,
        )
        if (
            campaign_current is not CampaignLifecycle.HOLDOUT_RELEASE_AUTHORIZED
            or holdout_current is not HoldoutState.RELEASE_AUTHORIZED
            or release is None
            or release.candidate_id != candidate_id
        ):
            raise CryptoResearchConflict("CRYPTO_HOLDOUT_EVALUATION_BINDING_INVALID")
        snapshot = self._repository.snapshot(campaign.holdout_snapshot_id)
        if snapshot is None:
            raise CryptoResearchNotFound("CRYPTO_HOLDOUT_EVIDENCE_NOT_FOUND")
        RecordDatasetExposure(self._repository).execute(
            exposure_id=exposure_id,
            snapshot_id=snapshot.snapshot_id,
            purpose="EXACT_SINGLE_USE_HOLDOUT_EVALUATION",
            category=ExposureCategory.HOLDOUT_EVALUATED,
            first_known_exposure_at=evaluated_at,
            recorded_at=evaluated_at,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            related_strategy_ids=(candidate_id,),
        )
        self._repository.save_campaign_event(
            CampaignEvent.create(
                event_id=campaign_event_id,
                campaign_id=campaign_id,
                event_type=CampaignEventType.HOLDOUT_EVALUATED,
                candidate_id=candidate_id,
                prior_state=campaign_current,
                actor=actor,
                reason=reason,
                occurred_at=evaluated_at,
            )
        )
        return self._repository.save_holdout_event(
            HoldoutLifecycleEvent.create(
                event_id=holdout_event_id,
                campaign=campaign,
                state=HoldoutState.EVALUATED,
                prior_state=holdout_current,
                candidate_id=candidate_id,
                actor=actor,
                reason=reason,
                occurred_at=evaluated_at,
            )
        )


def campaign_workspace(repository: CryptoCampaignRepository) -> dict[str, object]:
    base = crypto_challenger_workspace(repository)
    campaigns = repository.campaigns()
    campaign = next((item for item in campaigns if item.campaign_id == FIXED_CAMPAIGN_ID), None)
    if campaign is None:
        base.update(
            {
                "research_campaign": None,
                "campaign_state": "NOT_REGISTERED",
                "dataset_exposures": repository.exposures(),
                "holdout": None,
            }
        )
        return base
    candidates = repository.campaign_candidates(campaign.campaign_id)
    experiments = repository.campaign_experiments(campaign.campaign_id)
    events = repository.campaign_events(campaign.campaign_id)
    holdout_events = repository.holdout_events(campaign.holdout_id)
    capture = repository.capture(campaign.holdout_capture_id)
    snapshot = repository.snapshot(campaign.holdout_snapshot_id)
    base_captures = cast(tuple[object, ...], base.get("captures", ()))
    base_attempts = cast(tuple[object, ...], base.get("attempts", ()))
    base_checkpoints = cast(tuple[object, ...], base.get("checkpoints", ()))
    base_snapshots = cast(tuple[object, ...], base.get("snapshots", ()))
    base["captures"] = tuple(
        item
        for item in base_captures
        if getattr(item, "capture_id", None) != campaign.holdout_capture_id
    )
    base["attempts"] = tuple(
        item
        for item in base_attempts
        if getattr(item, "capture_id", None) != campaign.holdout_capture_id
    )
    base["checkpoints"] = tuple(
        item
        for item in base_checkpoints
        if getattr(item, "capture_id", None) != campaign.holdout_capture_id
    )
    base["snapshots"] = tuple(
        item
        for item in base_snapshots
        if getattr(item, "snapshot_id", None) != campaign.holdout_snapshot_id
    )
    exposures = tuple(
        DatasetExposureSummary.from_records(snapshot_id, repository.exposures(snapshot_id))
        for snapshot_id in (
            *campaign.allowed_development_snapshot_ids,
            campaign.holdout_snapshot_id,
        )
    )
    base.update(
        {
            "research_campaign": campaign,
            "campaign_state": campaign_state(campaign, events),
            "campaign_attachments": repository.campaign_attachments(campaign.campaign_id),
            "dataset_exposures": exposures,
            "budget": CampaignBudgetStatus.calculate(campaign, candidates, experiments),
            "campaign_candidates": candidates,
            "campaign_experiments": experiments,
            "nominated_candidate": next(
                (
                    item.candidate_id
                    for item in events
                    if item.event_type is CampaignEventType.CANDIDATE_NOMINATED
                ),
                None,
            ),
            "holdout": HoldoutMetadata.create(
                campaign=campaign,
                capture=capture,
                snapshot=snapshot,
                events=holdout_events,
            ),
            "holdout_events": holdout_events,
            "leading_challenger_status": "NO_LEADING_CHALLENGER",
            "approved_champion_status": NO_APPROVED_CHAMPION,
            "research_authority": "HISTORICAL_RESEARCH_GOVERNANCE_ONLY",
            "paper_eligible": False,
            "live_eligible": False,
            "capital_authority": "NONE",
            "opportunity_authority": "NONE",
            "order_authority": "NONE",
            "execution_authority": "NONE",
            "broker_authority": "NONE",
            "financial_authority": "NONE",
            "financial_mutations_possible": False,
        }
    )
    return base
