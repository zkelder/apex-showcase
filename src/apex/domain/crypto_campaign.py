"""Pure dataset-exposure, research-campaign, and sealed-holdout rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from apex.domain.crypto_research import (
    COINBASE_GRANULARITY,
    COINBASE_PRODUCT,
    CONSERVATIVE_FEE_BPS,
    CONSERVATIVE_SLIPPAGE_BPS,
    CryptoCaptureQuality,
    CryptoHistoricalCapture,
    CryptoHistoricalSnapshot,
)
from apex.domain.research_evidence import CaptureStatus, EvidenceClass
from apex.domain.values import content_digest

CRYPTO_DATASET_EXPOSURE_V1 = "CRYPTO_DATASET_EXPOSURE_V1"
CRYPTO_RESEARCH_CAMPAIGN_V1 = "CRYPTO_RESEARCH_CAMPAIGN_V1"
CRYPTO_CAMPAIGN_DATASET_V1 = "CRYPTO_CAMPAIGN_DATASET_V1"
CRYPTO_CAMPAIGN_CANDIDATE_V1 = "CRYPTO_CAMPAIGN_CANDIDATE_V1"
CRYPTO_CAMPAIGN_EXPERIMENT_V1 = "CRYPTO_CAMPAIGN_EXPERIMENT_V1"
CRYPTO_CAMPAIGN_EVENT_V1 = "CRYPTO_CAMPAIGN_EVENT_V1"
CRYPTO_HOLDOUT_EVENT_V1 = "CRYPTO_HOLDOUT_EVENT_V1"
CRYPTO_HOLDOUT_POLICY_V1 = "CRYPTO_OPERATIONALLY_SEALED_SINGLE_USE_HOLDOUT_V1"

EARLIER_EXPOSED_SNAPSHOT_ID = "btc-usd-genuine-replication-snapshot-2022-07-01-2024-07-01-v1"
LATER_EXPOSED_SNAPSHOT_ID = "btc-usd-genuine-snapshot-2024-07-01-2026-07-01-v1"
FIXED_CAMPAIGN_ID = "btc-usd-risk-managed-long-cash-campaign-v1"
FIXED_HOLDOUT_ID = "btc-usd-sealed-holdout-2020-07-01-2022-07-01-v1"
FIXED_HOLDOUT_CAPTURE_ID = "btc-usd-sealed-holdout-capture-2020-07-01-2022-07-01-v1"
FIXED_HOLDOUT_SNAPSHOT_ID = "btc-usd-sealed-holdout-snapshot-2020-07-01-2022-07-01-v1"
FIXED_HOLDOUT_START = datetime(2020, 7, 1, tzinfo=UTC)
FIXED_HOLDOUT_END = datetime(2022, 7, 1, tzinfo=UTC)
FIXED_CANDIDATE_BUDGET = 3
FIXED_EXPERIMENT_BUDGET = 6

OPERATIONAL_SEALING_WARNING = (
    "APPLICATION-ENFORCED OPERATIONAL SEALING — DATABASE ADMINISTRATORS RETAIN ACCESS"
)
HOLDOUT_LIMITATIONS = (
    "RETROSPECTIVE_HISTORY_NOT_FORWARD_EVIDENCE",
    "NOT_VINTAGE_POINT_IN_TIME_POSSESSION",
    "DOES_NOT_PREVENT_HUMAN_PRIOR_KNOWLEDGE",
    "SINGLE_USE_APPLICATION_LEVEL_HOLDOUT_ONLY",
    OPERATIONAL_SEALING_WARNING,
)
CAMPAIGN_WARNINGS = (
    "DEVELOPMENT EVIDENCE IS ALREADY EXPOSED",
    "SEALED HISTORICAL HOLDOUT IS NOT FORWARD OR VINTAGE EVIDENCE",
    "NO PAPER, LIVE, CAPITAL, OPPORTUNITY, ORDER, BROKER, OR FINANCIAL AUTHORITY",
)

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")


class CryptoCampaignError(ValueError):
    pass


class ExposureCategory(StrEnum):
    DEVELOPMENT_EXPOSED = "DEVELOPMENT_EXPOSED"
    VALIDATION_EXPOSED = "VALIDATION_EXPOSED"
    REPLICATION_EXPOSED = "REPLICATION_EXPOSED"
    OPERATOR_EXPOSED = "OPERATOR_EXPOSED"
    DOSSIER_EXPOSED = "DOSSIER_EXPOSED"
    SEALED_HOLDOUT = "SEALED_HOLDOUT"
    HOLDOUT_RELEASED = "HOLDOUT_RELEASED"
    HOLDOUT_EVALUATED = "HOLDOUT_EVALUATED"
    INVALIDATED = "INVALIDATED"


class CampaignDatasetRole(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    HOLDOUT = "HOLDOUT"


class SnapshotUsePurpose(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    RETROSPECTIVE_COMPARISON = "RETROSPECTIVE_COMPARISON"
    REPLAY = "REPLAY"
    INDEPENDENT_VALIDATION = "INDEPENDENT_VALIDATION"
    HOLDOUT_EVALUATION = "HOLDOUT_EVALUATION"
    METADATA_INSPECTION = "METADATA_INSPECTION"


class CampaignLifecycle(StrEnum):
    DEVELOPMENT_ACTIVE = "DEVELOPMENT_ACTIVE"
    CANDIDATE_NOMINATED = "CANDIDATE_NOMINATED"
    DEVELOPMENT_CLOSED = "DEVELOPMENT_CLOSED"
    HOLDOUT_RELEASE_AUTHORIZED = "HOLDOUT_RELEASE_AUTHORIZED"
    HOLDOUT_EVALUATED = "HOLDOUT_EVALUATED"
    COMPLETED = "COMPLETED"
    NO_CANDIDATE = "NO_CANDIDATE"
    INVALIDATED = "INVALIDATED"


class CampaignEventType(StrEnum):
    CANDIDATE_NOMINATED = "CANDIDATE_NOMINATED"
    DEVELOPMENT_CLOSED = "DEVELOPMENT_CLOSED"
    HOLDOUT_RELEASE_AUTHORIZED = "HOLDOUT_RELEASE_AUTHORIZED"
    HOLDOUT_EVALUATED = "HOLDOUT_EVALUATED"
    COMPLETED = "COMPLETED"
    NO_CANDIDATE = "NO_CANDIDATE"
    INVALIDATED = "INVALIDATED"


class HoldoutState(StrEnum):
    CAPTURE_IN_PROGRESS = "CAPTURE_IN_PROGRESS"
    CAPTURE_FAILED = "CAPTURE_FAILED"
    READY_TO_SEAL = "READY_TO_SEAL"
    SEALED = "SEALED"
    RELEASE_AUTHORIZED = "RELEASE_AUTHORIZED"
    EVALUATED = "EVALUATED"
    INVALIDATED = "INVALIDATED"


def _identity(value: str, code: str) -> str:
    normalized = value.strip()
    if _IDENTITY.fullmatch(normalized) is None:
        raise CryptoCampaignError(code)
    return normalized


def _aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CryptoCampaignError(code)
    return value.astimezone(UTC)


def _identities(values: tuple[str, ...], code: str) -> tuple[str, ...]:
    normalized = tuple(sorted({_identity(item, code) for item in values}))
    return normalized


@dataclass(frozen=True, slots=True)
class DatasetExposureRecord:
    exposure_id: str
    snapshot_id: str
    evidence_class: EvidenceClass
    purpose: str
    category: ExposureCategory
    first_known_exposure_at: datetime
    recorded_at: datetime
    actor: str
    related_hypothesis_ids: tuple[str, ...]
    related_strategy_ids: tuple[str, ...]
    related_protocol_ids: tuple[str, ...]
    related_experiment_ids: tuple[str, ...]
    related_dossier_ids: tuple[str, ...]
    independent_validation_eligible: bool
    reason: str
    known_limitations: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        exposure_id: str,
        snapshot: CryptoHistoricalSnapshot,
        purpose: str,
        category: ExposureCategory,
        first_known_exposure_at: datetime,
        recorded_at: datetime,
        actor: str,
        related_hypothesis_ids: tuple[str, ...] = (),
        related_strategy_ids: tuple[str, ...] = (),
        related_protocol_ids: tuple[str, ...] = (),
        related_experiment_ids: tuple[str, ...] = (),
        related_dossier_ids: tuple[str, ...] = (),
        independent_validation_eligible: bool = False,
        reason: str,
        known_limitations: tuple[str, ...] = (),
    ) -> DatasetExposureRecord:
        if snapshot.evidence_class not in {
            EvidenceClass.HISTORICAL_PUBLIC,
            EvidenceClass.FIXTURE,
        }:
            raise CryptoCampaignError("CRYPTO_EXPOSURE_EVIDENCE_CLASS_INVALID")
        if snapshot.evidence_class is EvidenceClass.FIXTURE and independent_validation_eligible:
            raise CryptoCampaignError("CRYPTO_EXPOSURE_FIXTURE_NOT_INDEPENDENT")
        if category is not ExposureCategory.SEALED_HOLDOUT and independent_validation_eligible:
            raise CryptoCampaignError("CRYPTO_EXPOSURE_INDEPENDENT_ELIGIBILITY_INVALID")
        if not purpose.strip() or not reason.strip():
            raise CryptoCampaignError("CRYPTO_EXPOSURE_REASON_REQUIRED")
        first = _aware(first_known_exposure_at, "CRYPTO_EXPOSURE_FIRST_TIME_INVALID")
        recorded = _aware(recorded_at, "CRYPTO_EXPOSURE_RECORDED_TIME_INVALID")
        if recorded < first:
            raise CryptoCampaignError("CRYPTO_EXPOSURE_TIME_ORDER_INVALID")
        payload = (
            CRYPTO_DATASET_EXPOSURE_V1,
            _identity(exposure_id, "CRYPTO_EXPOSURE_ID_INVALID"),
            snapshot.snapshot_id,
            snapshot.manifest_hash,
            snapshot.evidence_class,
            purpose.strip(),
            category,
            first,
            recorded,
            _identity(actor, "CRYPTO_EXPOSURE_ACTOR_INVALID"),
            _identities(related_hypothesis_ids, "CRYPTO_EXPOSURE_HYPOTHESIS_INVALID"),
            _identities(related_strategy_ids, "CRYPTO_EXPOSURE_STRATEGY_INVALID"),
            _identities(related_protocol_ids, "CRYPTO_EXPOSURE_PROTOCOL_INVALID"),
            _identities(related_experiment_ids, "CRYPTO_EXPOSURE_EXPERIMENT_INVALID"),
            _identities(related_dossier_ids, "CRYPTO_EXPOSURE_DOSSIER_INVALID"),
            independent_validation_eligible,
            reason.strip(),
            tuple(sorted(set(known_limitations))),
        )
        return cls(
            exposure_id=payload[1],
            snapshot_id=payload[2],
            evidence_class=payload[4],
            purpose=payload[5],
            category=payload[6],
            first_known_exposure_at=payload[7],
            recorded_at=payload[8],
            actor=payload[9],
            related_hypothesis_ids=payload[10],
            related_strategy_ids=payload[11],
            related_protocol_ids=payload[12],
            related_experiment_ids=payload[13],
            related_dossier_ids=payload[14],
            independent_validation_eligible=payload[15],
            reason=payload[16],
            known_limitations=payload[17],
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class DatasetExposureSummary:
    snapshot_id: str
    categories: tuple[ExposureCategory, ...]
    first_known_exposure_at: datetime | None
    independent_validation_eligible: bool
    untouched_validation_claim_permitted: bool
    exposure_record_ids: tuple[str, ...]
    current_state: str

    @classmethod
    def from_records(
        cls, snapshot_id: str, records: tuple[DatasetExposureRecord, ...]
    ) -> DatasetExposureSummary:
        ordered = tuple(sorted(records, key=lambda item: (item.recorded_at, item.exposure_id)))
        categories = tuple(item.category for item in ordered)
        released = any(
            item.category
            in {
                ExposureCategory.HOLDOUT_RELEASED,
                ExposureCategory.HOLDOUT_EVALUATED,
                ExposureCategory.INVALIDATED,
            }
            for item in ordered
        )
        exposed = any(
            item.category
            in {
                ExposureCategory.DEVELOPMENT_EXPOSED,
                ExposureCategory.VALIDATION_EXPOSED,
                ExposureCategory.REPLICATION_EXPOSED,
                ExposureCategory.OPERATOR_EXPOSED,
                ExposureCategory.DOSSIER_EXPOSED,
            }
            for item in ordered
        )
        sealed = bool(ordered) and ordered[-1].category is ExposureCategory.SEALED_HOLDOUT
        eligible = (
            sealed
            and not released
            and not exposed
            and any(item.independent_validation_eligible for item in ordered)
        )
        return cls(
            snapshot_id,
            categories,
            min((item.first_known_exposure_at for item in ordered), default=None),
            eligible,
            not ordered,
            tuple(item.exposure_id for item in ordered),
            (
                "SEALED_HOLDOUT"
                if sealed
                else "EXPOSED_DEVELOPMENT_EVIDENCE"
                if exposed
                else ordered[-1].category.value
                if ordered
                else "UNCLASSIFIED"
            ),
        )


@dataclass(frozen=True, slots=True)
class CryptoResearchCampaign:
    campaign_id: str
    version: int
    question: str
    motivation: str
    product_id: str
    granularity: str
    allowed_development_snapshot_ids: tuple[str, str]
    forbidden_snapshot_ids: tuple[str, ...]
    allowed_evidence_classes: tuple[EvidenceClass, ...]
    allowed_feature_families: tuple[str, ...]
    strategy_constraints: tuple[str, ...]
    candidate_budget: int
    experiment_budget: int
    comparison_baselines: tuple[str, ...]
    fee_bps_per_side: str
    slippage_bps_per_side: str
    selection_policy: tuple[str, ...]
    stop_policy: str
    holdout_id: str
    holdout_capture_id: str
    holdout_snapshot_id: str
    holdout_start: datetime
    holdout_end: datetime
    holdout_release_policy: tuple[str, ...]
    actor: str
    created_at: datetime
    lifecycle_state: CampaignLifecycle
    warnings: tuple[str, ...]
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    opportunity_authority: str = "NONE"
    order_authority: str = "NONE"
    authority: str = "NONE"

    @classmethod
    def fixed(cls, *, campaign_id: str, actor: str, created_at: datetime) -> CryptoResearchCampaign:
        identity = _identity(campaign_id, "CRYPTO_CAMPAIGN_ID_INVALID")
        if identity != FIXED_CAMPAIGN_ID:
            raise CryptoCampaignError("CRYPTO_CAMPAIGN_FIXED_ID_REQUIRED")
        payload = (
            CRYPTO_RESEARCH_CAMPAIGN_V1,
            identity,
            1,
            (
                "Can one materially different, predeclared BTC-USD daily-data long-or-cash "
                "strategy produce non-negative conservative returns and lower drawdown "
                "than buy-and-hold across both exposed development windows, while "
                "remaining simple, low-turnover, and free of leverage or shorting?"
            ),
            (
                "Bound development reuse, prevent parameter search, nominate at most one "
                "candidate, and preserve one operationally sealed historical holdout."
            ),
            COINBASE_PRODUCT,
            COINBASE_GRANULARITY,
            (EARLIER_EXPOSED_SNAPSHOT_ID, LATER_EXPOSED_SNAPSHOT_ID),
            (FIXED_HOLDOUT_SNAPSHOT_ID,),
            (EvidenceClass.HISTORICAL_PUBLIC, EvidenceClass.FIXTURE),
            ("PREDECLARED_MATERIALLY_DISTINCT_LONG_CASH_DAILY_DATA",),
            (
                "LONG_OR_CASH_ONLY",
                "NO_SHORT",
                "NO_LEVERAGE",
                "NO_DERIVATIVES",
                "NO_PARAMETER_SEARCH",
                "ONE_VERSION_PER_STRATEGY_FAMILY",
            ),
            FIXED_CANDIDATE_BUDGET,
            FIXED_EXPERIMENT_BUDGET,
            (
                "CASH",
                "BUY_AND_HOLD",
                "REJECTED_SMA_20_100",
                "REJECTED_MONTHLY_252_ABSOLUTE_MOMENTUM",
            ),
            str(CONSERVATIVE_FEE_BPS),
            str(CONSERVATIVE_SLIPPAGE_BPS),
            (
                "REGISTERED_BEFORE_CAMPAIGN_RUN",
                "MATERIALLY_DIFFERENT_FROM_ALL_PRIOR_STRATEGIES",
                "ONLY_APPROVED_DEVELOPMENT_SNAPSHOTS",
                "WITHIN_CANDIDATE_AND_EXPERIMENT_BUDGETS",
                "IDENTICAL_SEMANTICS_ACROSS_BOTH_WINDOWS",
                "BOTH_RUNS_COMPLETE_AND_REPRODUCIBLE",
                "BOTH_CONSERVATIVE_NET_RETURNS_AT_LEAST_ZERO",
                "BOTH_DRAWDOWNS_STRICTLY_BELOW_BUY_AND_HOLD",
                "NO_DISQUALIFYING_QUALITY_OR_LEAKAGE_WARNING",
                "POSITIVE_AGGREGATE_COMPOUNDED_RETURN",
                "AT_MOST_ONE_NOMINATED_CANDIDATE",
            ),
            "CLOSE_NO_CANDIDATE_WHEN_BUDGET_EXHAUSTED_WITHOUT_ELIGIBLE_CANDIDATE",
            FIXED_HOLDOUT_ID,
            FIXED_HOLDOUT_CAPTURE_ID,
            FIXED_HOLDOUT_SNAPSHOT_ID,
            FIXED_HOLDOUT_START,
            FIXED_HOLDOUT_END,
            (
                "EXACTLY_ONE_NOMINATED_CANDIDATE",
                "CANDIDATE_SELECTION_STAGE_CLOSED",
                "EXACT_CAMPAIGN_CANDIDATE_AND_SNAPSHOT_BINDING",
                "ONE_RELEASE_AND_ONE_EVALUATION_MAXIMUM",
                "RELEASE_GRANTS_HISTORICAL_RESEARCH_ACCESS_ONLY",
            ),
            _identity(actor, "CRYPTO_CAMPAIGN_ACTOR_INVALID"),
            _aware(created_at, "CRYPTO_CAMPAIGN_CREATED_AT_INVALID"),
            CampaignLifecycle.DEVELOPMENT_ACTIVE,
            CAMPAIGN_WARNINGS,
        )
        return cls(*payload[1:], content_digest(payload))


@dataclass(frozen=True, slots=True)
class CampaignDatasetAttachment:
    attachment_id: str
    campaign_id: str
    snapshot_id: str
    manifest_hash: str
    role: CampaignDatasetRole
    exposure_state_at_attachment: str
    attached_at: datetime
    actor: str
    reason: str
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        attachment_id: str,
        campaign: CryptoResearchCampaign,
        snapshot: CryptoHistoricalSnapshot,
        role: CampaignDatasetRole,
        exposure_state_at_attachment: str,
        attached_at: datetime,
        actor: str,
        reason: str,
    ) -> CampaignDatasetAttachment:
        if role is CampaignDatasetRole.DEVELOPMENT:
            if snapshot.snapshot_id not in campaign.allowed_development_snapshot_ids:
                raise CryptoCampaignError("CRYPTO_CAMPAIGN_DEVELOPMENT_SNAPSHOT_NOT_ALLOWED")
            if exposure_state_at_attachment != "EXPOSED_DEVELOPMENT_EVIDENCE":
                raise CryptoCampaignError("CRYPTO_CAMPAIGN_DEVELOPMENT_EXPOSURE_REQUIRED")
        elif snapshot.snapshot_id != campaign.holdout_snapshot_id:
            raise CryptoCampaignError("CRYPTO_CAMPAIGN_HOLDOUT_IDENTITY_MISMATCH")
        if snapshot.evidence_class not in campaign.allowed_evidence_classes:
            raise CryptoCampaignError("CRYPTO_CAMPAIGN_EVIDENCE_CLASS_NOT_ALLOWED")
        payload = (
            CRYPTO_CAMPAIGN_DATASET_V1,
            _identity(attachment_id, "CRYPTO_CAMPAIGN_ATTACHMENT_ID_INVALID"),
            campaign.campaign_id,
            snapshot.snapshot_id,
            snapshot.manifest_hash,
            role,
            exposure_state_at_attachment,
            _aware(attached_at, "CRYPTO_CAMPAIGN_ATTACHMENT_TIME_INVALID"),
            _identity(actor, "CRYPTO_CAMPAIGN_ATTACHMENT_ACTOR_INVALID"),
            reason.strip(),
        )
        if not reason.strip():
            raise CryptoCampaignError("CRYPTO_CAMPAIGN_ATTACHMENT_REASON_REQUIRED")
        return cls(*payload[1:], content_digest(payload))


@dataclass(frozen=True, slots=True)
class CampaignCandidate:
    candidate_id: str
    campaign_id: str
    hypothesis_version_id: str
    strategy_specification_id: str
    strategy_family: str
    strategy_semantics_hash: str
    rationale: str
    expected_mechanism: str
    failure_conditions: tuple[str, ...]
    exact_configuration_hash: str
    registered_at: datetime
    actor: str
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        candidate_id: str,
        campaign_id: str,
        hypothesis_version_id: str,
        strategy_specification_id: str,
        strategy_family: str,
        strategy_semantics_hash: str,
        rationale: str,
        expected_mechanism: str,
        failure_conditions: tuple[str, ...],
        exact_configuration_hash: str,
        registered_at: datetime,
        actor: str,
    ) -> CampaignCandidate:
        if not rationale.strip() or not expected_mechanism.strip() or not failure_conditions:
            raise CryptoCampaignError("CRYPTO_CAMPAIGN_CANDIDATE_PREDECLARATION_REQUIRED")
        payload = (
            CRYPTO_CAMPAIGN_CANDIDATE_V1,
            _identity(candidate_id, "CRYPTO_CAMPAIGN_CANDIDATE_ID_INVALID"),
            _identity(campaign_id, "CRYPTO_CAMPAIGN_ID_INVALID"),
            _identity(hypothesis_version_id, "CRYPTO_CAMPAIGN_HYPOTHESIS_INVALID"),
            _identity(strategy_specification_id, "CRYPTO_CAMPAIGN_STRATEGY_INVALID"),
            _identity(strategy_family, "CRYPTO_CAMPAIGN_FAMILY_INVALID"),
            strategy_semantics_hash,
            rationale.strip(),
            expected_mechanism.strip(),
            tuple(failure_conditions),
            exact_configuration_hash,
            _aware(registered_at, "CRYPTO_CAMPAIGN_CANDIDATE_TIME_INVALID"),
            _identity(actor, "CRYPTO_CAMPAIGN_CANDIDATE_ACTOR_INVALID"),
        )
        return cls(*payload[1:], content_digest(payload))


@dataclass(frozen=True, slots=True)
class CampaignExperimentUse:
    consumption_id: str
    campaign_id: str
    candidate_id: str
    run_id: str
    snapshot_id: str
    snapshot_exposure_state: str
    strategy_semantics_hash: str
    use_purpose: SnapshotUsePurpose
    complete: bool
    reproducible: bool
    window_policy_passed: bool
    result_hash: str
    recorded_at: datetime
    actor: str
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        consumption_id: str,
        campaign_id: str,
        candidate_id: str,
        run_id: str,
        snapshot_id: str,
        snapshot_exposure_state: str,
        strategy_semantics_hash: str,
        use_purpose: SnapshotUsePurpose,
        complete: bool,
        reproducible: bool,
        window_policy_passed: bool,
        result_hash: str,
        recorded_at: datetime,
        actor: str,
    ) -> CampaignExperimentUse:
        if use_purpose is not SnapshotUsePurpose.DEVELOPMENT:
            raise CryptoCampaignError("CRYPTO_CAMPAIGN_EXPERIMENT_PURPOSE_INVALID")
        payload = (
            CRYPTO_CAMPAIGN_EXPERIMENT_V1,
            _identity(consumption_id, "CRYPTO_CAMPAIGN_CONSUMPTION_ID_INVALID"),
            _identity(campaign_id, "CRYPTO_CAMPAIGN_ID_INVALID"),
            _identity(candidate_id, "CRYPTO_CAMPAIGN_CANDIDATE_ID_INVALID"),
            _identity(run_id, "CRYPTO_CAMPAIGN_RUN_ID_INVALID"),
            _identity(snapshot_id, "CRYPTO_CAMPAIGN_SNAPSHOT_ID_INVALID"),
            snapshot_exposure_state,
            strategy_semantics_hash,
            use_purpose,
            complete,
            reproducible,
            window_policy_passed,
            result_hash,
            _aware(recorded_at, "CRYPTO_CAMPAIGN_EXPERIMENT_TIME_INVALID"),
            _identity(actor, "CRYPTO_CAMPAIGN_EXPERIMENT_ACTOR_INVALID"),
        )
        return cls(*payload[1:], content_digest(payload))


@dataclass(frozen=True, slots=True)
class CampaignEvent:
    event_id: str
    campaign_id: str
    event_type: CampaignEventType
    candidate_id: str | None
    prior_state: CampaignLifecycle
    resulting_state: CampaignLifecycle
    actor: str
    reason: str
    occurred_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        campaign_id: str,
        event_type: CampaignEventType,
        candidate_id: str | None,
        prior_state: CampaignLifecycle,
        actor: str,
        reason: str,
        occurred_at: datetime,
    ) -> CampaignEvent:
        transitions = {
            (CampaignLifecycle.DEVELOPMENT_ACTIVE, CampaignEventType.CANDIDATE_NOMINATED): (
                CampaignLifecycle.CANDIDATE_NOMINATED
            ),
            (CampaignLifecycle.CANDIDATE_NOMINATED, CampaignEventType.DEVELOPMENT_CLOSED): (
                CampaignLifecycle.DEVELOPMENT_CLOSED
            ),
            (CampaignLifecycle.DEVELOPMENT_ACTIVE, CampaignEventType.NO_CANDIDATE): (
                CampaignLifecycle.NO_CANDIDATE
            ),
            (
                CampaignLifecycle.DEVELOPMENT_CLOSED,
                CampaignEventType.HOLDOUT_RELEASE_AUTHORIZED,
            ): CampaignLifecycle.HOLDOUT_RELEASE_AUTHORIZED,
            (
                CampaignLifecycle.HOLDOUT_RELEASE_AUTHORIZED,
                CampaignEventType.HOLDOUT_EVALUATED,
            ): CampaignLifecycle.HOLDOUT_EVALUATED,
            (CampaignLifecycle.HOLDOUT_EVALUATED, CampaignEventType.COMPLETED): (
                CampaignLifecycle.COMPLETED
            ),
        }
        resulting: CampaignLifecycle | None
        if event_type is CampaignEventType.INVALIDATED:
            resulting = CampaignLifecycle.INVALIDATED
        else:
            resulting = transitions.get((prior_state, event_type))
            if resulting is None:
                raise CryptoCampaignError("CRYPTO_CAMPAIGN_TRANSITION_INVALID")
        if (
            event_type
            in {
                CampaignEventType.CANDIDATE_NOMINATED,
                CampaignEventType.HOLDOUT_RELEASE_AUTHORIZED,
                CampaignEventType.HOLDOUT_EVALUATED,
            }
            and candidate_id is None
        ):
            raise CryptoCampaignError("CRYPTO_CAMPAIGN_EVENT_CANDIDATE_REQUIRED")
        payload = (
            CRYPTO_CAMPAIGN_EVENT_V1,
            _identity(event_id, "CRYPTO_CAMPAIGN_EVENT_ID_INVALID"),
            _identity(campaign_id, "CRYPTO_CAMPAIGN_ID_INVALID"),
            event_type,
            _identity(candidate_id, "CRYPTO_CAMPAIGN_CANDIDATE_ID_INVALID")
            if candidate_id
            else None,
            prior_state,
            resulting,
            _identity(actor, "CRYPTO_CAMPAIGN_EVENT_ACTOR_INVALID"),
            reason.strip(),
            _aware(occurred_at, "CRYPTO_CAMPAIGN_EVENT_TIME_INVALID"),
        )
        if not reason.strip():
            raise CryptoCampaignError("CRYPTO_CAMPAIGN_EVENT_REASON_REQUIRED")
        return cls(*payload[1:], content_digest(payload))


@dataclass(frozen=True, slots=True)
class HoldoutLifecycleEvent:
    event_id: str
    holdout_id: str
    campaign_id: str
    state: HoldoutState
    prior_state: HoldoutState | None
    capture_id: str
    snapshot_id: str
    candidate_id: str | None
    actor: str
    reason: str
    occurred_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        campaign: CryptoResearchCampaign,
        state: HoldoutState,
        prior_state: HoldoutState | None,
        candidate_id: str | None,
        actor: str,
        reason: str,
        occurred_at: datetime,
    ) -> HoldoutLifecycleEvent:
        transitions: dict[HoldoutState | None, tuple[HoldoutState, ...]] = {
            None: (HoldoutState.CAPTURE_IN_PROGRESS,),
            HoldoutState.CAPTURE_IN_PROGRESS: (
                HoldoutState.CAPTURE_FAILED,
                HoldoutState.READY_TO_SEAL,
                HoldoutState.INVALIDATED,
            ),
            HoldoutState.READY_TO_SEAL: (HoldoutState.SEALED, HoldoutState.INVALIDATED),
            HoldoutState.SEALED: (HoldoutState.RELEASE_AUTHORIZED, HoldoutState.INVALIDATED),
            HoldoutState.RELEASE_AUTHORIZED: (
                HoldoutState.EVALUATED,
                HoldoutState.INVALIDATED,
            ),
            HoldoutState.CAPTURE_FAILED: (HoldoutState.INVALIDATED,),
            HoldoutState.EVALUATED: (HoldoutState.INVALIDATED,),
            HoldoutState.INVALIDATED: (),
        }
        if state not in transitions[prior_state]:
            raise CryptoCampaignError("CRYPTO_HOLDOUT_TRANSITION_INVALID")
        if state in {HoldoutState.RELEASE_AUTHORIZED, HoldoutState.EVALUATED} and not candidate_id:
            raise CryptoCampaignError("CRYPTO_HOLDOUT_CANDIDATE_BINDING_REQUIRED")
        payload = (
            CRYPTO_HOLDOUT_EVENT_V1,
            _identity(event_id, "CRYPTO_HOLDOUT_EVENT_ID_INVALID"),
            campaign.holdout_id,
            campaign.campaign_id,
            state,
            prior_state,
            campaign.holdout_capture_id,
            campaign.holdout_snapshot_id,
            _identity(candidate_id, "CRYPTO_CAMPAIGN_CANDIDATE_ID_INVALID")
            if candidate_id
            else None,
            _identity(actor, "CRYPTO_HOLDOUT_EVENT_ACTOR_INVALID"),
            reason.strip(),
            _aware(occurred_at, "CRYPTO_HOLDOUT_EVENT_TIME_INVALID"),
        )
        if not reason.strip():
            raise CryptoCampaignError("CRYPTO_HOLDOUT_EVENT_REASON_REQUIRED")
        return cls(*payload[1:], content_digest(payload))


def campaign_state(
    campaign: CryptoResearchCampaign, events: tuple[CampaignEvent, ...]
) -> CampaignLifecycle:
    state = campaign.lifecycle_state
    for event in sorted(events, key=lambda item: (item.occurred_at, item.event_id)):
        if event.prior_state is not state:
            raise CryptoCampaignError("CRYPTO_CAMPAIGN_EVENT_HISTORY_INVALID")
        state = event.resulting_state
    return state


def holdout_state(events: tuple[HoldoutLifecycleEvent, ...]) -> HoldoutState | None:
    state: HoldoutState | None = None
    for event in sorted(events, key=lambda item: (item.occurred_at, item.event_id)):
        if event.prior_state is not state:
            raise CryptoCampaignError("CRYPTO_HOLDOUT_EVENT_HISTORY_INVALID")
        state = event.state
    return state


@dataclass(frozen=True, slots=True)
class CampaignBudgetStatus:
    candidate_budget: int
    consumed_candidates: int
    remaining_candidates: int
    experiment_budget: int
    consumed_experiments: int
    remaining_experiments: int
    exhausted: bool

    @classmethod
    def calculate(
        cls,
        campaign: CryptoResearchCampaign,
        candidates: tuple[CampaignCandidate, ...],
        experiments: tuple[CampaignExperimentUse, ...],
    ) -> CampaignBudgetStatus:
        consumed_candidates = len(candidates)
        consumed_experiments = len(experiments)
        if (
            consumed_candidates > campaign.candidate_budget
            or consumed_experiments > campaign.experiment_budget
        ):
            raise CryptoCampaignError("CRYPTO_CAMPAIGN_RETAINED_BUDGET_INVALID")
        return cls(
            campaign.candidate_budget,
            consumed_candidates,
            campaign.candidate_budget - consumed_candidates,
            campaign.experiment_budget,
            consumed_experiments,
            campaign.experiment_budget - consumed_experiments,
            consumed_candidates == campaign.candidate_budget
            or consumed_experiments == campaign.experiment_budget,
        )


@dataclass(frozen=True, slots=True)
class SnapshotAccessDecision:
    snapshot_id: str
    purpose: SnapshotUsePurpose
    allowed: bool
    exposure_state: str
    reason: str
    campaign_id: str | None
    candidate_id: str | None
    authority: str = "NONE"


def decide_snapshot_access(
    *,
    snapshot_id: str,
    purpose: SnapshotUsePurpose,
    exposure: DatasetExposureSummary,
    campaign: CryptoResearchCampaign | None,
    attachments: tuple[CampaignDatasetAttachment, ...],
    holdout_events: tuple[HoldoutLifecycleEvent, ...],
    campaign_id: str | None = None,
    candidate_id: str | None = None,
) -> SnapshotAccessDecision:
    state = holdout_state(holdout_events)
    if purpose is SnapshotUsePurpose.METADATA_INSPECTION:
        return SnapshotAccessDecision(
            snapshot_id,
            purpose,
            True,
            exposure.current_state,
            "METADATA_ONLY",
            campaign_id,
            candidate_id,
        )
    if state in {HoldoutState.CAPTURE_IN_PROGRESS, HoldoutState.READY_TO_SEAL, HoldoutState.SEALED}:
        return SnapshotAccessDecision(
            snapshot_id,
            purpose,
            False,
            state.value,
            "CRYPTO_SEALED_HOLDOUT_VALUES_REDACTED",
            campaign_id,
            candidate_id,
        )
    if state is HoldoutState.RELEASE_AUTHORIZED:
        release = next(
            item
            for item in reversed(
                sorted(holdout_events, key=lambda value: (value.occurred_at, value.event_id))
            )
            if item.state is HoldoutState.RELEASE_AUTHORIZED
        )
        allowed = (
            purpose is SnapshotUsePurpose.HOLDOUT_EVALUATION
            and campaign is not None
            and campaign_id == campaign.campaign_id
            and candidate_id == release.candidate_id
        )
        return SnapshotAccessDecision(
            snapshot_id,
            purpose,
            allowed,
            state.value,
            "EXACT_SINGLE_USE_RELEASE_BINDING_MATCHED"
            if allowed
            else "CRYPTO_HOLDOUT_RELEASE_BINDING_MISMATCH",
            campaign_id,
            candidate_id,
        )
    if purpose is SnapshotUsePurpose.INDEPENDENT_VALIDATION:
        return SnapshotAccessDecision(
            snapshot_id,
            purpose,
            exposure.independent_validation_eligible,
            exposure.current_state,
            "ELIGIBLE" if exposure.independent_validation_eligible else "SNAPSHOT_ALREADY_EXPOSED",
            campaign_id,
            candidate_id,
        )
    if purpose is SnapshotUsePurpose.DEVELOPMENT:
        attached = any(
            item.snapshot_id == snapshot_id and item.role is CampaignDatasetRole.DEVELOPMENT
            for item in attachments
        )
        allowed = (
            campaign is not None
            and campaign_id == campaign.campaign_id
            and attached
            and snapshot_id in campaign.allowed_development_snapshot_ids
        )
        return SnapshotAccessDecision(
            snapshot_id,
            purpose,
            allowed,
            exposure.current_state,
            "APPROVED_EXPOSED_DEVELOPMENT_EVIDENCE"
            if allowed
            else "CRYPTO_CAMPAIGN_DEVELOPMENT_ACCESS_DENIED",
            campaign_id,
            candidate_id,
        )
    return SnapshotAccessDecision(
        snapshot_id,
        purpose,
        purpose in {SnapshotUsePurpose.REPLAY, SnapshotUsePurpose.RETROSPECTIVE_COMPARISON},
        exposure.current_state,
        "EXPOSED_REPLAY_OR_RETROSPECTIVE_COMPARISON_ONLY",
        campaign_id,
        candidate_id,
    )


@dataclass(frozen=True, slots=True)
class HoldoutMetadata:
    holdout_id: str
    campaign_id: str
    capture_id: str
    snapshot_id: str
    product_id: str
    granularity: str
    requested_start: datetime
    requested_end: datetime
    evidence_class: EvidenceClass | None
    capture_status: CaptureStatus | None
    expected_buckets: int
    accepted_buckets: int
    missing_buckets: int
    rejected_records: int
    duplicate_records: int
    quality: CryptoCaptureQuality | None
    manifest_hash: str | None
    canonical_hash: str | None
    provider_contract_version: str | None
    parser_version: str | None
    quality_policy_version: str | None
    sealing_state: HoldoutState | None
    release_policy: tuple[str, ...]
    limitations: tuple[str, ...]
    values_redacted: bool
    raw_responses_redacted: bool
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        campaign: CryptoResearchCampaign,
        capture: CryptoHistoricalCapture | None,
        snapshot: CryptoHistoricalSnapshot | None,
        events: tuple[HoldoutLifecycleEvent, ...],
    ) -> HoldoutMetadata:
        return cls(
            campaign.holdout_id,
            campaign.campaign_id,
            campaign.holdout_capture_id,
            campaign.holdout_snapshot_id,
            COINBASE_PRODUCT,
            COINBASE_GRANULARITY,
            campaign.holdout_start,
            campaign.holdout_end,
            snapshot.evidence_class if snapshot else capture.evidence_class if capture else None,
            capture.status if capture else None,
            snapshot.expected_buckets if snapshot else capture.expected_buckets if capture else 0,
            snapshot.accepted_buckets if snapshot else capture.accepted_buckets if capture else 0,
            len(snapshot.missing_bucket_starts)
            if snapshot
            else len(capture.missing_bucket_starts)
            if capture
            else 0,
            snapshot.rejected_records if snapshot else capture.rejected_records if capture else 0,
            snapshot.duplicate_records if snapshot else capture.duplicate_records if capture else 0,
            snapshot.quality if snapshot else capture.quality if capture else None,
            snapshot.manifest_hash if snapshot else None,
            snapshot.canonical_hash if snapshot else None,
            snapshot.provider_contract_version
            if snapshot
            else capture.source_contract_version
            if capture
            else None,
            snapshot.parser_version if snapshot else capture.parser_version if capture else None,
            snapshot.quality_policy_version
            if snapshot
            else capture.quality_policy_version
            if capture
            else None,
            holdout_state(events),
            campaign.holdout_release_policy,
            HOLDOUT_LIMITATIONS,
            True,
            True,
        )
