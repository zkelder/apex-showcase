"""Durable external PAPER projections; never owns a ledger, Order, or Position."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.postgres import (
    PostgresAutopilotStore,
    PostgresOperatorConsoleStore,
    PostgresPaperPortfolioStore,
    alpaca_paper_acceptance_campaign,
    alpaca_paper_acceptance_preflight,
    external_paper_authorization,
    external_paper_broker_evidence,
    external_paper_campaign_control,
    external_paper_campaign_observation,
    external_paper_capability_snapshot,
    external_paper_fill_application,
    external_paper_instruction,
    external_paper_internal_execution_binding,
    external_paper_reconciliation,
    external_paper_submission_attempt,
    external_paper_validated_fill,
    paper_trial_authorization,
    securities_bar_evidence,
    securities_instrument,
    securities_market_data_batch,
)
from apex.adapters.securities_paper_postgres import (
    PostgresSecuritiesPaperCampaignBoundary,
    PostgresSecuritiesPaperRepository,
)
from apex.application.autopilot import (
    StrategyAuthorityState,
    StrategyDescriptor,
    StrategyEvidenceState,
    StrategyFamily,
    StrategyImplementationState,
)
from apex.application.evidence_review import (
    PaperTrialAuthorization,
    PaperTrialAuthorizationState,
    PaperTrialLimits,
    PaperTrialUsage,
)
from apex.application.external_paper import (
    AlpacaPaperAcceptanceCampaign,
    AlpacaPaperAcceptancePreflight,
    CreateAuthorizedExternalPaperInstruction,
    CreateExternalPaperAuthorization,
    ExternalPaperApplicationError,
    ExternalPaperFillApplication,
    ExternalPaperReadinessResult,
    PrepareAuthorizedExternalPaperExecution,
    external_paper_authorization_confirmation,
)
from apex.application.operator_console import (
    CreateMoonshotCampaign,
    CreateMoonshotCampaignCommand,
    OperatorMode,
)
from apex.application.securities_paper import (
    CreateSecuritiesPaperRun,
    CreateSecuritiesPaperRunCommand,
)
from apex.domain.autopilot import EvidenceTier
from apex.domain.external_paper import (
    ExternalPaperAuthorization,
    ExternalPaperBrokerEvidence,
    ExternalPaperCampaignObservation,
    ExternalPaperInstruction,
    ExternalPaperInstructionState,
    ExternalPaperInternalExecutionBinding,
    ExternalPaperInternalExecutionBindingState,
    ExternalPaperReconciliation,
    ExternalPaperReconciliationClassification,
    ExternalPaperSubmissionAttempt,
    ExternalPaperSubmissionState,
    PaperBrokerCapabilitySnapshot,
    ValidatedExternalPaperFill,
    ValidatedExternalPaperFillState,
)
from apex.domain.paper import PaperExecutionModel, PaperExecutionPolicy, PaperUnfilledBehavior
from apex.domain.securities_paper import (
    SecuritiesExecutionPolicy,
    SecuritiesOrderAction,
    SecuritiesPaperOpportunity,
    authorize_paper_opportunity,
    create_securities_order,
)
from apex.domain.values import (
    ContentHash,
    Money,
    Quantity,
    RiskFraction,
    canonical_json_bytes,
    content_digest,
    deterministic_id,
    parse_decimal,
)


class PostgresExternalPaperRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def acceptance_by_key(self, idempotency_key: str) -> AlpacaPaperAcceptanceCampaign | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(alpaca_paper_acceptance_campaign.c.payload).where(
                    alpaca_paper_acceptance_campaign.c.idempotency_key == idempotency_key
                )
            ).scalar_one_or_none()
        return None if row is None else _acceptance(_json(row))

    def acceptance(self, acceptance_id: str) -> AlpacaPaperAcceptanceCampaign | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(alpaca_paper_acceptance_campaign.c.payload).where(
                    alpaca_paper_acceptance_campaign.c.acceptance_id == acceptance_id
                )
            ).scalar_one_or_none()
        return None if row is None else _acceptance(_json(row))

    def acceptance_for_campaign(self, campaign_id: str) -> AlpacaPaperAcceptanceCampaign | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(alpaca_paper_acceptance_campaign.c.payload).where(
                    alpaca_paper_acceptance_campaign.c.campaign_id == campaign_id
                )
            ).scalar_one_or_none()
        return None if row is None else _acceptance(_json(row))

    def acceptance_for_instruction(
        self, instruction_id: str
    ) -> AlpacaPaperAcceptanceCampaign | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(alpaca_paper_acceptance_campaign.c.payload).where(
                    alpaca_paper_acceptance_campaign.c.instruction_id == instruction_id
                )
            ).scalar_one_or_none()
        return None if row is None else _acceptance(_json(row))

    def save_acceptance(
        self, acceptance: AlpacaPaperAcceptanceCampaign
    ) -> AlpacaPaperAcceptanceCampaign:
        existing = self.acceptance_by_key(acceptance.idempotency_key)
        if existing is not None:
            if existing.canonical_input_hash != acceptance.canonical_input_hash:
                raise ExternalPaperApplicationError(
                    "EXTERNAL_PAPER_ACCEPTANCE_IDEMPOTENCY_CONFLICT"
                )
            return existing
        with self.engine.begin() as connection:
            connection.execute(
                alpaca_paper_acceptance_campaign.insert().values(
                    acceptance_id=acceptance.acceptance_id,
                    idempotency_key=acceptance.idempotency_key,
                    campaign_id=acceptance.campaign_id,
                    instruction_id=acceptance.instruction_id,
                    payload=canonical_json_bytes(acceptance).decode(),
                    canonical_input_hash=acceptance.canonical_input_hash,
                    canonical_result_hash=acceptance.canonical_result_hash,
                    expires_at=acceptance.expires_at,
                )
            )
        return acceptance

    def save_acceptance_preflight(
        self, preflight: AlpacaPaperAcceptancePreflight
    ) -> AlpacaPaperAcceptancePreflight:
        preflight_id = f"{preflight.instruction_id}:{preflight.canonical_hash}"
        with self.engine.begin() as connection:
            existing = connection.execute(
                sa.select(alpaca_paper_acceptance_preflight.c.canonical_hash).where(
                    alpaca_paper_acceptance_preflight.c.preflight_id == preflight_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    alpaca_paper_acceptance_preflight.insert().values(
                        preflight_id=preflight_id,
                        acceptance_id=preflight.acceptance_id,
                        instruction_id=preflight.instruction_id,
                        payload=canonical_json_bytes(preflight).decode(),
                        canonical_hash=preflight.canonical_hash,
                        checked_at=preflight.checked_at,
                        freshness_expires_at=preflight.freshness_expires_at,
                    )
                )
            elif existing != preflight.canonical_hash:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_ACCEPTANCE_PREFLIGHT_CONFLICT")
        return preflight

    def latest_acceptance_preflight(
        self, instruction_id: str
    ) -> AlpacaPaperAcceptancePreflight | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(alpaca_paper_acceptance_preflight.c.payload)
                .where(alpaca_paper_acceptance_preflight.c.instruction_id == instruction_id)
                .order_by(alpaca_paper_acceptance_preflight.c.checked_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        return None if row is None else _acceptance_preflight(_json(row))

    def save_capability_snapshot(
        self, snapshot: PaperBrokerCapabilitySnapshot
    ) -> PaperBrokerCapabilitySnapshot:
        with self.engine.begin() as connection:
            existing = connection.execute(
                sa.select(external_paper_capability_snapshot.c.canonical_hash).where(
                    external_paper_capability_snapshot.c.snapshot_id == snapshot.snapshot_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    external_paper_capability_snapshot.insert().values(
                        snapshot_id=snapshot.snapshot_id,
                        payload=canonical_json_bytes(snapshot).decode(),
                        canonical_hash=snapshot.canonical_hash,
                        checked_at=snapshot.checked_at,
                    )
                )
            elif existing != snapshot.canonical_hash:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_CAPABILITY_CONFLICT")
        return snapshot

    def authorization(self, authorization_id: str) -> ExternalPaperAuthorization | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(external_paper_authorization.c.payload).where(
                    external_paper_authorization.c.authorization_id == authorization_id
                )
            ).scalar_one_or_none()
        return None if row is None else _authorization(_json(row))

    def save_authorization(
        self, authorization: ExternalPaperAuthorization
    ) -> ExternalPaperAuthorization:
        old = self.authorization(authorization.authorization_id)
        if old is not None:
            if old.canonical_hash != authorization.canonical_hash:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_AUTHORIZATION_CONFLICT")
            return old
        with self.engine.begin() as connection:
            connection.execute(
                external_paper_authorization.insert().values(
                    authorization_id=authorization.authorization_id,
                    campaign_id=authorization.campaign_id,
                    paper_trial_authorization_id=authorization.paper_trial_authorization_id,
                    portfolio_run_id=authorization.portfolio_run_id,
                    portfolio_instruction_id=authorization.portfolio_instruction_id,
                    payload=canonical_json_bytes(authorization).decode(),
                    canonical_hash=authorization.canonical_hash,
                    created_at=datetime.now(tz=UTC),
                )
            )
        return authorization

    def instruction(self, instruction_id: str) -> ExternalPaperInstruction | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(external_paper_instruction.c.payload).where(
                    external_paper_instruction.c.instruction_id == instruction_id
                )
            ).scalar_one_or_none()
        return None if row is None else _instruction(_json(row))

    def instruction_by_key(self, key: str) -> ExternalPaperInstruction | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(external_paper_instruction.c.payload).where(
                    external_paper_instruction.c.idempotency_key == key
                )
            ).scalar_one_or_none()
        return None if row is None else _instruction(_json(row))

    def instructions_for_campaign(self, campaign_id: str) -> tuple[ExternalPaperInstruction, ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(external_paper_instruction.c.payload)
                .where(external_paper_instruction.c.campaign_id == campaign_id)
                .order_by(external_paper_instruction.c.instruction_id)
            ).scalars()
        return tuple(_instruction(_json(row)) for row in rows)

    def instructions(
        self, *, limit: int = 100, offset: int = 0
    ) -> tuple[ExternalPaperInstruction, ...]:
        """Return bounded retained broker instructions without contacting a broker."""

        if not 1 <= limit <= 100 or offset < 0:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_PAGINATION_INVALID")
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(external_paper_instruction.c.payload)
                .order_by(external_paper_instruction.c.instruction_id)
                .limit(limit)
                .offset(offset)
            ).scalars()
        return tuple(_instruction(_json(row)) for row in rows)

    def save_instruction(self, instruction: ExternalPaperInstruction) -> ExternalPaperInstruction:
        old = self.instruction_by_key(instruction.idempotency_key)
        if old is not None and old.instruction_id != instruction.instruction_id:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_IDEMPOTENCY_CONFLICT")
        with self.engine.begin() as connection:
            existing = self.instruction(instruction.instruction_id)
            values = dict(
                authorization_id=instruction.authorization_id,
                campaign_id=instruction.campaign_id,
                idempotency_key=instruction.idempotency_key,
                state=instruction.state.value,
                broker_order_id=instruction.broker_order_id,
                payload=canonical_json_bytes(instruction).decode(),
                canonical_hash=instruction.canonical_hash,
                updated_at=datetime.now(tz=UTC),
            )
            if existing is None:
                connection.execute(
                    external_paper_instruction.insert().values(
                        instruction_id=instruction.instruction_id, **values
                    )
                )
            else:
                connection.execute(
                    external_paper_instruction.update()
                    .where(
                        external_paper_instruction.c.instruction_id == instruction.instruction_id
                    )
                    .values(**values)
                )
        return instruction

    def save_evidence(self, evidence: ExternalPaperBrokerEvidence) -> ExternalPaperBrokerEvidence:
        with self.engine.begin() as connection:
            existing = connection.execute(
                sa.select(external_paper_broker_evidence.c.canonical_hash).where(
                    external_paper_broker_evidence.c.evidence_id == evidence.evidence_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing != evidence.canonical_hash:
                    raise ExternalPaperApplicationError("EXTERNAL_PAPER_EVIDENCE_CONFLICT")
                return evidence
            connection.execute(
                external_paper_broker_evidence.insert().values(
                    evidence_id=evidence.evidence_id,
                    instruction_id=evidence.instruction_id,
                    broker_order_id=evidence.sanitized_payload.get("id"),
                    observation_type=evidence.observation_type,
                    endpoint_identity=evidence.endpoint_identity,
                    payload=canonical_json_bytes(evidence).decode(),
                    payload_hash=evidence.payload_hash,
                    canonical_hash=evidence.canonical_hash,
                    observed_at=evidence.observed_at,
                )
            )
        return evidence

    def broker_evidence(self, evidence_id: str) -> ExternalPaperBrokerEvidence | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(external_paper_broker_evidence.c.payload).where(
                    external_paper_broker_evidence.c.evidence_id == evidence_id
                )
            ).scalar_one_or_none()
        return None if row is None else _evidence(_json(row))

    def save_campaign_observation(
        self, observation: ExternalPaperCampaignObservation
    ) -> ExternalPaperCampaignObservation:
        with self.engine.begin() as connection:
            existing = connection.execute(
                sa.select(external_paper_campaign_observation.c.canonical_hash).where(
                    external_paper_campaign_observation.c.observation_id
                    == observation.observation_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing != observation.canonical_hash:
                    raise ExternalPaperApplicationError(
                        "EXTERNAL_PAPER_CAMPAIGN_OBSERVATION_CONFLICT"
                    )
                return observation
            evidence = connection.execute(
                sa.select(external_paper_broker_evidence.c.observed_at).where(
                    external_paper_broker_evidence.c.evidence_id == observation.evidence_id
                )
            ).scalar_one_or_none()
            if evidence is None:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_OBSERVATION_EVIDENCE_MISSING")
            connection.execute(
                external_paper_campaign_observation.insert().values(
                    observation_id=observation.observation_id,
                    campaign_id=observation.campaign_id,
                    evidence_id=observation.evidence_id,
                    observation_type=observation.observation_type,
                    unknown_broker_activity=observation.unknown_broker_activity,
                    payload=canonical_json_bytes(observation).decode(),
                    canonical_hash=observation.canonical_hash,
                    observed_at=evidence,
                )
            )
        return observation

    def has_unknown_broker_activity(self, campaign_id: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    sa.select(external_paper_campaign_observation.c.observation_id)
                    .where(
                        external_paper_campaign_observation.c.campaign_id == campaign_id,
                        external_paper_campaign_observation.c.unknown_broker_activity.is_(True),
                    )
                    .limit(1)
                ).scalar_one_or_none()
            )

    def evidence_for_instruction(
        self, instruction_id: str
    ) -> tuple[ExternalPaperBrokerEvidence, ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(external_paper_broker_evidence.c.payload)
                .where(external_paper_broker_evidence.c.instruction_id == instruction_id)
                .order_by(external_paper_broker_evidence.c.observed_at)
            ).scalars()
        return tuple(_evidence(_json(row)) for row in rows)

    def reserve_attempt(
        self, attempt: ExternalPaperSubmissionAttempt
    ) -> ExternalPaperSubmissionAttempt:
        old = self.submission_attempt(attempt.instruction_id)
        if old is not None:
            if old.canonical_hash != attempt.canonical_hash:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_SUBMISSION_ATTEMPT_CONFLICT")
            return old
        with self.engine.begin() as connection:
            connection.execute(
                external_paper_submission_attempt.insert().values(
                    attempt_id=attempt.attempt_id,
                    instruction_id=attempt.instruction_id,
                    ordinal=attempt.ordinal,
                    state=attempt.state.value,
                    payload=canonical_json_bytes(attempt).decode(),
                    canonical_hash=attempt.canonical_hash,
                    reserved_at=attempt.reserved_at,
                )
            )
        return attempt

    def conclude_attempt(
        self, attempt: ExternalPaperSubmissionAttempt
    ) -> ExternalPaperSubmissionAttempt:
        with self.engine.begin() as connection:
            result = connection.execute(
                external_paper_submission_attempt.update()
                .where(external_paper_submission_attempt.c.attempt_id == attempt.attempt_id)
                .values(state=attempt.state.value, payload=canonical_json_bytes(attempt).decode())
            )
            if result.rowcount != 1:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_SUBMISSION_ATTEMPT_NOT_FOUND")
        return attempt

    def submission_attempt(self, instruction_id: str) -> ExternalPaperSubmissionAttempt | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(external_paper_submission_attempt.c.payload).where(
                    external_paper_submission_attempt.c.instruction_id == instruction_id
                )
            ).scalar_one_or_none()
        return None if row is None else _attempt(_json(row))

    def external_paper_killed(self, campaign_id: str) -> bool:
        with self.engine.connect() as connection:
            state = connection.execute(
                sa.select(external_paper_campaign_control.c.kill_state).where(
                    external_paper_campaign_control.c.campaign_id == campaign_id
                )
            ).scalar_one_or_none()
        return state == "KILLED"

    def kill(self, *, campaign_id: str, reason: str, operator_action_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                sa.delete(external_paper_campaign_control).where(
                    external_paper_campaign_control.c.campaign_id == campaign_id
                )
            )
            connection.execute(
                external_paper_campaign_control.insert().values(
                    campaign_id=campaign_id,
                    kill_state="KILLED",
                    reason=reason,
                    operator_action_id=operator_action_id,
                    updated_at=datetime.now(tz=UTC),
                )
            )

    def save_reconciliation(
        self, report: ExternalPaperReconciliation
    ) -> ExternalPaperReconciliation:
        with self.engine.begin() as connection:
            existing = connection.execute(
                sa.select(external_paper_reconciliation.c.canonical_hash).where(
                    external_paper_reconciliation.c.reconciliation_id == report.reconciliation_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    external_paper_reconciliation.insert().values(
                        reconciliation_id=report.reconciliation_id,
                        instruction_id=report.instruction_id,
                        classification=report.classification.value,
                        payload=canonical_json_bytes(report).decode(),
                        canonical_hash=report.canonical_hash,
                        created_at=datetime.now(tz=UTC),
                    )
                )
            elif existing != report.canonical_hash:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_RECONCILIATION_CONFLICT")
        return report

    def reconciliations_for_instruction(
        self, instruction_id: str
    ) -> tuple[ExternalPaperReconciliation, ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(external_paper_reconciliation.c.payload)
                .where(external_paper_reconciliation.c.instruction_id == instruction_id)
                .order_by(external_paper_reconciliation.c.created_at)
            ).scalars()
        return tuple(_reconciliation(_json(row)) for row in rows)

    def validated_fill_for_evidence(self, evidence_id: str) -> ValidatedExternalPaperFill | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(external_paper_validated_fill.c.payload).where(
                    external_paper_validated_fill.c.evidence_id == evidence_id
                )
            ).scalar_one_or_none()
        return None if row is None else _validated_fill(_json(row))

    def validated_fill(self, validated_fill_id: str) -> ValidatedExternalPaperFill | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(external_paper_validated_fill.c.payload).where(
                    external_paper_validated_fill.c.validated_fill_id == validated_fill_id
                )
            ).scalar_one_or_none()
        return None if row is None else _validated_fill(_json(row))

    def validated_fills_for_instruction(
        self, instruction_id: str
    ) -> tuple[ValidatedExternalPaperFill, ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(external_paper_validated_fill.c.payload)
                .where(external_paper_validated_fill.c.instruction_id == instruction_id)
                .order_by(external_paper_validated_fill.c.occurred_at)
            ).scalars()
        return tuple(_validated_fill(_json(row)) for row in rows)

    def save_validated_fill(self, fill: ValidatedExternalPaperFill) -> ValidatedExternalPaperFill:
        existing = self.validated_fill_for_evidence(fill.evidence_id)
        if existing is not None:
            if existing.canonical_hash != fill.canonical_hash:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_VALIDATED_FILL_CONFLICT")
            return existing
        with self.engine.begin() as connection:
            connection.execute(
                external_paper_validated_fill.insert().values(
                    validated_fill_id=fill.validated_fill_id,
                    instruction_id=fill.instruction_id,
                    evidence_id=fill.evidence_id,
                    state=fill.state.value,
                    payload=canonical_json_bytes(fill).decode(),
                    canonical_hash=fill.canonical_hash,
                    occurred_at=fill.occurred_at,
                )
            )
        return fill

    def fill_application(self, validated_fill_id: str) -> ExternalPaperFillApplication | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(external_paper_fill_application).where(
                        external_paper_fill_application.c.validated_fill_id == validated_fill_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return ExternalPaperFillApplication(
            validated_fill_id=str(row["validated_fill_id"]),
            internal_fill_id=str(row["internal_fill_id"]),
            internal_position_id=(
                str(row["internal_position_id"])
                if row["internal_position_id"] is not None
                else None
            ),
            journal_ids=tuple(cast(list[str], json.loads(str(row["journal_ids"])))),
            applied_at=row["applied_at"],
            canonical_hash=str(row["canonical_hash"]),
        )

    def save_fill_application(
        self, application: ExternalPaperFillApplication
    ) -> ExternalPaperFillApplication:
        with self.engine.begin() as connection:
            existing = connection.execute(
                sa.select(external_paper_fill_application.c.canonical_hash).where(
                    external_paper_fill_application.c.validated_fill_id
                    == application.validated_fill_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    external_paper_fill_application.insert().values(
                        validated_fill_id=application.validated_fill_id,
                        internal_fill_id=application.internal_fill_id,
                        internal_position_id=application.internal_position_id,
                        journal_ids=canonical_json_bytes(application.journal_ids).decode(),
                        canonical_hash=application.canonical_hash,
                        applied_at=application.applied_at,
                    )
                )
            elif str(existing) != application.canonical_hash:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_FILL_APPLICATION_CONFLICT")
        return application

    def binding_for_instruction(
        self, instruction_id: str
    ) -> ExternalPaperInternalExecutionBinding | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(external_paper_internal_execution_binding.c.payload).where(
                    external_paper_internal_execution_binding.c.instruction_id == instruction_id
                )
            ).scalar_one_or_none()
        return None if row is None else _binding(_json(row))

    def save_binding(
        self, binding: ExternalPaperInternalExecutionBinding
    ) -> ExternalPaperInternalExecutionBinding:
        existing = self.binding_for_instruction(binding.instruction_id)
        if existing is not None:
            if existing.canonical_hash != binding.canonical_hash:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_INTERNAL_BINDING_CONFLICT")
            if existing.state == binding.state:
                return existing
            with self.engine.begin() as connection:
                connection.execute(
                    external_paper_internal_execution_binding.update()
                    .where(
                        external_paper_internal_execution_binding.c.instruction_id
                        == binding.instruction_id
                    )
                    .values(
                        state=binding.state.value,
                        payload=canonical_json_bytes(binding).decode(),
                    )
                )
            return binding
        with self.engine.begin() as connection:
            connection.execute(
                external_paper_internal_execution_binding.insert().values(
                    binding_id=binding.binding_id,
                    authorization_id=binding.authorization_id,
                    instruction_id=binding.instruction_id,
                    internal_order_id=binding.internal_order_id,
                    risk_reservation_id=binding.risk_reservation_id,
                    state=binding.state.value,
                    payload=canonical_json_bytes(binding).decode(),
                    canonical_hash=binding.canonical_hash,
                    created_at=datetime.now(tz=UTC),
                )
            )
        return binding


class PostgresAlpacaPaperAcceptanceAuthorityAdapter:
    """Create the one engineering-only, pre-submission Alpaca PAPER authority.

    This adapter intentionally stops at an unfilled internal READY Order.  It
    uses the ordinary campaign, strategy catalog, Securities PAPER, and
    external-PAPER services; its only narrow persistence addition is the
    isolated trial row required before a normal Securities PAPER run exists.
    """

    _STRATEGY_ID = "alpaca-paper-engineering-acceptance-v1"
    _STRATEGY_VERSION = "v1"

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._external = PostgresExternalPaperRepository(engine)
        self._securities = PostgresSecuritiesPaperRepository(engine)
        self._campaigns = PostgresSecuritiesPaperCampaignBoundary(engine)

    @staticmethod
    def _campaign_id(idempotency_key: str) -> str:
        return deterministic_id(
            "alpaca_paper_acceptance_campaign", {"idempotency_key": idempotency_key}
        )

    def _descriptor(self) -> StrategyDescriptor:
        return StrategyDescriptor(
            strategy_id=self._STRATEGY_ID,
            version=self._STRATEGY_VERSION,
            display_name="Alpaca PAPER engineering acceptance",
            family=StrategyFamily.OPERATOR_SUPPLIED,
            implementation_state=StrategyImplementationState.IMPLEMENTED,
            authority_state=StrategyAuthorityState.PAPER,
            evidence_state=StrategyEvidenceState.INCOMPLETE,
            last_evidence_decision="ENGINEERING_ACCEPTANCE_ONLY",
            supported_instrument_class="CONTINUOUS_SECURITIES",
            maximum_proposals_per_cycle=1,
            configuration_hash=ContentHash(
                content_digest("alpaca-paper-engineering-acceptance-config-v1")
            ),
        )

    def _create_campaign(self, *, campaign_id: str, idempotency_key: str, now: datetime) -> None:
        CreateMoonshotCampaign(PostgresOperatorConsoleStore(self._engine)).create(
            CreateMoonshotCampaignCommand(
                campaign_id=campaign_id,
                name="Alpaca PAPER engineering acceptance",
                initial_capital=Money("1000"),
                maximum_authorized_loss=Money("1000"),
                starts_at=now,
                expires_at=now + timedelta(days=1),
                operating_mode=OperatorMode.PAPER,
                maximum_active_risk_fraction=RiskFraction("1"),
                maximum_per_opportunity_risk_fraction=RiskFraction("1"),
                maximum_per_strategy_risk_fraction=RiskFraction("1"),
                maximum_per_correlation_bucket_risk_fraction=RiskFraction("1"),
                maximum_simultaneous_reservations=1,
                maximum_simultaneous_positions=1,
                maximum_capital_lockup_seconds=86_400,
                minimum_conservative_expected_value=Money("0"),
                minimum_evidence_tier=EvidenceTier.UNVERIFIED,
                allow_profit_compounding=False,
                operator_note="ENGINEERING_ACCEPTANCE_ONLY",
                idempotency_key=f"{idempotency_key}:campaign",
            )
        )

    def _trial(
        self,
        *,
        campaign_id: str,
        idempotency_key: str,
        policy: SecuritiesExecutionPolicy,
        now: datetime,
    ) -> PaperTrialAuthorization:
        descriptor = self._descriptor()
        limits = PaperTrialLimits(
            paper_execution_policy_id=policy.policy_id,
            paper_execution_policy_hash=policy.canonical_hash,
            starts_at=now,
            expires_at=now + timedelta(days=1),
            maximum_paper_cycles=1,
            maximum_approved_opportunities=1,
            maximum_paper_orders=1,
            maximum_simultaneous_open_positions=1,
            maximum_per_opportunity_risk_fraction=RiskFraction("1"),
            maximum_strategy_active_risk_fraction=RiskFraction("1"),
            maximum_cumulative_authorized_risk=Money("1000"),
            maximum_cumulative_realized_paper_loss=Money("1000"),
            maximum_paper_drawdown=Money("1000"),
            maximum_capital_lockup_seconds=86_400,
            allowed_instrument_class="CONTINUOUS_SECURITIES",
            allowed_provider="MANAGED_LOCAL_SECURITIES_DATA",
        )
        payload = {"campaign_id": campaign_id, "idempotency_key": idempotency_key, "limits": limits}
        trial = PaperTrialAuthorization(
            authorization_id=deterministic_id("alpaca_paper_acceptance_trial", payload),
            review_id=f"engineering-acceptance:{idempotency_key}",
            strategy_id=descriptor.strategy_id,
            strategy_version=descriptor.version,
            campaign_id=campaign_id,
            strategy_configuration_hash=descriptor.configuration_hash,
            limits=limits,
            state=PaperTrialAuthorizationState.ACTIVE,
            authorized_at=now,
            suspension_reason=None,
            revocation_reason=None,
            operator_action_id=deterministic_id("alpaca_paper_acceptance_authority", payload),
            configuration_hash=ContentHash(content_digest({"limits": limits})),
            canonical_input_hash=ContentHash(content_digest(payload)),
            canonical_result_hash=ContentHash(content_digest({"trial": payload})),
            usage=PaperTrialUsage(),
        )
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(paper_trial_authorization.c.canonical_input_hash).where(
                    paper_trial_authorization.c.authorization_id == trial.authorization_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    paper_trial_authorization.insert().values(
                        authorization_id=trial.authorization_id,
                        review_id=None,
                        strategy_id=trial.strategy_id,
                        strategy_version=trial.strategy_version,
                        campaign_id=trial.campaign_id,
                        strategy_configuration_hash=trial.strategy_configuration_hash.value,
                        state=trial.state.value,
                        authorized_at=trial.authorized_at,
                        completed_cycle_count=0,
                        paper_order_count=0,
                        settled_position_count=0,
                        cumulative_realized_result=Decimal(),
                        suspension_reason=None,
                        revocation_reason=None,
                        operator_action_id=trial.operator_action_id,
                        configuration_hash=trial.configuration_hash.value,
                        canonical_input_hash=trial.canonical_input_hash.value,
                        canonical_result_hash=trial.canonical_result_hash.value,
                        created_at=now,
                        paper_execution_policy_id=limits.paper_execution_policy_id,
                        paper_execution_policy_hash=limits.paper_execution_policy_hash.value,
                        starts_at=limits.starts_at,
                        expires_at=limits.expires_at,
                        maximum_paper_cycles=1,
                        maximum_approved_opportunities=1,
                        maximum_paper_orders=1,
                        maximum_simultaneous_open_positions=1,
                        maximum_per_opportunity_risk_fraction=Decimal("1"),
                        maximum_strategy_active_risk_fraction=Decimal("1"),
                        maximum_cumulative_authorized_risk=Decimal("1000"),
                        maximum_cumulative_realized_paper_loss=Decimal("1000"),
                        maximum_paper_drawdown=Decimal("1000"),
                        maximum_capital_lockup_seconds=86_400,
                        allowed_instrument_class="CONTINUOUS_SECURITIES",
                        allowed_provider="MANAGED_LOCAL_SECURITIES_DATA",
                    )
                )
            elif str(existing) != trial.canonical_input_hash.value:
                raise ExternalPaperApplicationError(
                    "EXTERNAL_PAPER_ACCEPTANCE_IDEMPOTENCY_CONFLICT"
                )
        return trial

    def _instrument(self, symbol: str) -> tuple[str, str, ContentHash, Decimal]:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(
                        securities_instrument.c.instrument_id,
                        securities_market_data_batch.c.batch_id,
                        securities_market_data_batch.c.canonical_hash,
                        securities_bar_evidence.c.close,
                    )
                    .select_from(
                        securities_instrument.join(
                            securities_bar_evidence,
                            securities_bar_evidence.c.instrument_id
                            == securities_instrument.c.instrument_id,
                        ).join(
                            securities_market_data_batch,
                            securities_market_data_batch.c.batch_id
                            == securities_bar_evidence.c.batch_id,
                        )
                    )
                    .where(securities_instrument.c.symbol == symbol)
                    .order_by(securities_bar_evidence.c.available_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ExternalPaperApplicationError(
                "ENGINEERING_ACCEPTANCE_FIXED_UNIVERSE_DATA_REQUIRED"
            )
        return (
            str(row["instrument_id"]),
            str(row["batch_id"]),
            ContentHash(str(row["canonical_hash"])),
            parse_decimal(Decimal(row["close"]).normalize(), allow_zero=False),
        )

    def create_acceptance_components(
        self,
        *,
        symbol: str,
        idempotency_key: str,
        now: datetime,
        readiness: ExternalPaperReadinessResult,
    ) -> tuple[
        AlpacaPaperAcceptanceCampaign,
        ExternalPaperInstruction,
        ExternalPaperInternalExecutionBinding,
    ]:
        del readiness
        campaign_id = self._campaign_id(idempotency_key)
        existing = self._external.acceptance_by_key(idempotency_key)
        if existing is not None:
            if existing.symbol != symbol:
                raise ExternalPaperApplicationError(
                    "EXTERNAL_PAPER_ACCEPTANCE_IDEMPOTENCY_CONFLICT"
                )
            instruction = self._external.instruction(existing.instruction_id)
            binding = self._external.binding_for_instruction(existing.instruction_id)
            if instruction is None or binding is None:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_ACCEPTANCE_REPLAY_INCOMPLETE")
            return existing, instruction, binding
        self._create_campaign(campaign_id=campaign_id, idempotency_key=idempotency_key, now=now)
        descriptor = self._descriptor()
        PostgresAutopilotStore(
            sa.orm.sessionmaker(bind=self._engine, expire_on_commit=False, future=True)
        ).register_descriptors((descriptor,))
        policy = SecuritiesExecutionPolicy.next_bar_cross_v1(created_at=now, quantity_increment="1")
        self._securities.save_policy(policy)
        PostgresPaperPortfolioStore(self._engine).ensure_execution_policy(
            PaperExecutionPolicy(
                policy_version_id=policy.policy_id,
                execution_model=PaperExecutionModel.IMMEDIATE_CROSS_V1,
                maximum_evidence_age_seconds=300,
                maximum_spread=Decimal("1"),
                maximum_orderbook_depth=1,
                maximum_visible_size_participation=RiskFraction("1"),
                slippage_reserve_per_contract=Money(0),
                fee_model_version="ENGINEERING_ACCEPTANCE_ONLY",
                time_in_force_seconds=86_400,
                unfilled_behavior=PaperUnfilledBehavior.EXPIRE,
                configuration_version="ENGINEERING_ACCEPTANCE_ONLY",
                configuration_hash=policy.canonical_hash,
            ),
            recorded_at=now,
        )
        trial = self._trial(
            campaign_id=campaign_id, idempotency_key=idempotency_key, policy=policy, now=now
        )
        instrument_id, batch_id, batch_hash, reference_price = self._instrument(symbol)
        run = CreateSecuritiesPaperRun(
            repository=self._securities, campaigns=self._campaigns
        ).execute(
            CreateSecuritiesPaperRunCommand(
                campaign_id=campaign_id,
                trial=trial,
                descriptor=descriptor,
                data_batch_id=batch_id,
                data_batch_hash=batch_hash,
                policy=policy,
                start_date=now.date().isoformat(),
                end_date=(now + timedelta(days=1)).date().isoformat(),
                signal_count=0,
                idempotency_key=f"{idempotency_key}:run",
                requested_at=now,
            )
        )
        earliest = now + timedelta(minutes=1)
        opportunity_payload = {
            "run_id": run.run_id,
            "instrument_id": instrument_id,
            "decision_at": now,
            "earliest_fill_at": earliest,
            "symbol": symbol,
        }
        opportunity = SecuritiesPaperOpportunity(
            opportunity_id=deterministic_id(
                "alpaca_paper_acceptance_opportunity", opportunity_payload
            ),
            run_id=run.run_id,
            signal_id=None,
            instrument_id=instrument_id,
            decision_at=now,
            earliest_fill_at=earliest,
            reference_price=reference_price,
            requested_quantity=Quantity("1", "1"),
            intended_exposure=Money(reference_price),
            modeled_spread=Money(0),
            modeled_slippage=Money(0),
            modeled_commission=Money(0),
            maximum_loss=Money(reference_price),
            holding_horizon_sessions=1,
            evidence_bar_ids=(),
            configuration_hash=policy.canonical_hash,
            canonical_hash=ContentHash(content_digest(opportunity_payload)),
        )
        self._securities.save_opportunity(opportunity)
        authorization = authorize_paper_opportunity(
            opportunity=opportunity,
            settled_cash=Money("1000"),
            risk_capacity=Money("1000"),
            active_risk=Money(0),
            maximum_per_opportunity_risk=Money("1000"),
            maximum_strategy_active_risk=Money("1000"),
            trial_cumulative_remaining_risk=Money("1000"),
        )
        if not authorization.accepted or authorization.quantity.value != 1:
            raise ExternalPaperApplicationError("ENGINEERING_ACCEPTANCE_ONE_SHARE_RISK_REJECTED")
        expiry = now + timedelta(hours=1)
        external_authorization = ExternalPaperAuthorization.create(
            campaign_id=campaign_id,
            paper_trial_authorization_id=trial.authorization_id,
            portfolio_run_id=run.run_id,
            portfolio_instruction_id=opportunity.opportunity_id,
            allowed_symbols=(symbol,),
            maximum_capital="1000",
            maximum_position_quantity="1",
            maximum_order_count=1,
            starts_at=now,
            expires_at=expiry,
            operator_action_id=trial.operator_action_id,
        )
        CreateExternalPaperAuthorization(
            repository=self._external, campaigns=self._campaigns
        ).execute(
            authorization=external_authorization,
            trial=trial,
            descriptor=descriptor,
            portfolio_instruction=SimpleNamespace(
                instruction_id=opportunity.opportunity_id, permitted_allocation=Decimal("1")
            ),
            confirmation_text=external_paper_authorization_confirmation(
                campaign_id=campaign_id,
                trial_id=trial.authorization_id,
                portfolio_instruction_id=opportunity.opportunity_id,
            ),
            now=now,
        )
        instruction = CreateAuthorizedExternalPaperInstruction(repository=self._external).execute(
            instruction=ExternalPaperInstruction.authorized(
                authorization=external_authorization,
                symbol=symbol,
                quantity="1",
                maximum_notional="1000",
                expires_at=expiry,
                idempotency_key=f"{idempotency_key}:instruction",
            ),
            now=now,
        )
        order = create_securities_order(
            run_id=run.run_id,
            instrument_id=instrument_id,
            action=SecuritiesOrderAction.BUY,
            quantity=authorization.quantity,
            reserved_amount=authorization.maximum_loss,
            decision_at=now,
            earliest_fill_at=earliest,
            expires_at=expiry,
            idempotency_key=f"{idempotency_key}:order",
            authorization_id=authorization.authorization_id,
            opportunity_id=opportunity.opportunity_id,
        )
        binding = ExternalPaperInternalExecutionBinding.pending(
            authorization=external_authorization,
            instruction=instruction,
            securities_paper_run_id=run.run_id,
            internal_order_id=order.order_id,
            risk_reservation_id=authorization.reservation_id,
            execution_policy_id=policy.policy_id,
            execution_policy_hash=policy.canonical_hash.value,
            instrument_id=instrument_id,
            intended_session=earliest.date().isoformat(),
            idempotency_key=f"{idempotency_key}:binding",
        )
        binding = PrepareAuthorizedExternalPaperExecution(
            repository=self._external, securities_repository=self._securities
        ).execute(
            binding=binding,
            now=now,
            securities_authorization=authorization,
            pending_order=order,
        )
        acceptance = AlpacaPaperAcceptanceCampaign.create(
            idempotency_key=idempotency_key,
            campaign_id=campaign_id,
            trial_id=trial.authorization_id,
            authorization_id=external_authorization.authorization_id,
            instruction_id=instruction.instruction_id,
            reservation_id=authorization.reservation_id,
            internal_order_id=order.order_id,
            binding_id=binding.binding_id,
            symbol=symbol,
            expires_at=expiry,
            readiness_evidence_ids=(),
        )
        return acceptance, instruction, binding


def _json(value: object) -> dict[str, object]:
    return cast(dict[str, object], json.loads(cast(str, value)))


def _when(value: object) -> datetime:
    return datetime.fromisoformat(cast(str, value).replace("Z", "+00:00"))


def _authorization(value: dict[str, object]) -> ExternalPaperAuthorization:
    return ExternalPaperAuthorization(
        cast(str, value["authorization_id"]),
        cast(str, value["campaign_id"]),
        cast(str, value["paper_trial_authorization_id"]),
        cast(str, value["portfolio_run_id"]),
        cast(str, value["portfolio_instruction_id"]),
        tuple(cast(list[str], value["allowed_symbols"])),
        parse_decimal(cast(str, value["maximum_capital"])),
        parse_decimal(cast(str, value["maximum_position_quantity"])),
        int(cast(str, value["maximum_order_count"])),
        _when(value["starts_at"]),
        _when(value["expires_at"]),
        cast(str, value["operator_action_id"]),
        cast(str, value["canonical_hash"]),
    )


def _instruction(value: dict[str, object]) -> ExternalPaperInstruction:
    return ExternalPaperInstruction(
        cast(str, value["instruction_id"]),
        cast(str, value["authorization_id"]),
        cast(str, value["campaign_id"]),
        cast(str, value["symbol"]),
        cast(str, value["side"]),
        parse_decimal(cast(str, value["quantity"])),
        cast(str, value["order_type"]),
        cast(str, value["time_in_force"]),
        parse_decimal(cast(str, value["maximum_notional"])),
        _when(value["expires_at"]),
        cast(str, value["idempotency_key"]),
        ExternalPaperInstructionState(cast(str, value["state"])),
        cast(str | None, value.get("broker_order_id")),
        cast(str | None, value.get("reason_code")),
        cast(str, value["canonical_hash"]),
    )


def _acceptance(value: dict[str, object]) -> AlpacaPaperAcceptanceCampaign:
    return AlpacaPaperAcceptanceCampaign(
        cast(str, value["acceptance_id"]),
        cast(str, value["idempotency_key"]),
        cast(str, value["campaign_id"]),
        cast(str, value["trial_id"]),
        cast(str, value["authorization_id"]),
        cast(str, value["instruction_id"]),
        cast(str, value["reservation_id"]),
        cast(str, value["internal_order_id"]),
        cast(str, value["binding_id"]),
        cast(str, value["symbol"]),
        _when(value["expires_at"]),
        tuple(cast(list[str], value["readiness_evidence_ids"])),
        cast(str, value["canonical_input_hash"]),
        cast(str, value["canonical_result_hash"]),
    )


def _acceptance_preflight(value: dict[str, object]) -> AlpacaPaperAcceptancePreflight:
    return AlpacaPaperAcceptancePreflight(
        cast(str, value["acceptance_id"]),
        cast(str, value["instruction_id"]),
        cast(str, value["state"]),
        tuple(cast(list[str], value["blockers"])),
        cast(str | None, value.get("confirmation")),
        _when(value["checked_at"]),
        _when(value["freshness_expires_at"]),
        cast(str, value["canonical_hash"]),
    )


def _reconciliation(value: dict[str, object]) -> ExternalPaperReconciliation:
    return ExternalPaperReconciliation(
        cast(str, value["reconciliation_id"]),
        cast(str, value["instruction_id"]),
        ExternalPaperReconciliationClassification(cast(str, value["classification"])),
        cast(str | None, value.get("differing_field")),
        cast(str | None, value.get("apex_value")),
        cast(str | None, value.get("broker_value")),
        cast(str, value["required_action"]),
        cast(str, value["canonical_hash"]),
    )


def _attempt(value: dict[str, object]) -> ExternalPaperSubmissionAttempt:
    return ExternalPaperSubmissionAttempt(
        cast(str, value["attempt_id"]),
        cast(str, value["instruction_id"]),
        int(cast(str, value["ordinal"])),
        _when(value["reserved_at"]),
        cast(str, value["endpoint_identity"]),
        cast(str, value["request_hash"]),
        ExternalPaperSubmissionState(cast(str, value["state"])),
        cast(str | None, value.get("broker_order_id")),
        cast(str | None, value.get("broker_request_id")),
        cast(str | None, value.get("ambiguity_reason")),
        cast(str, value["canonical_hash"]),
    )


def _evidence(value: dict[str, object]) -> ExternalPaperBrokerEvidence:
    return ExternalPaperBrokerEvidence(
        cast(str, value["evidence_id"]),
        cast(str | None, value.get("instruction_id")),
        cast(str, value["endpoint_identity"]),
        cast(str, value["observation_type"]),
        _when(value["observed_at"]),
        _when(value["broker_reported_at"]) if value.get("broker_reported_at") else None,
        cast(str | None, value.get("request_id")),
        cast(dict[str, str | None], value["sanitized_payload"]),
        cast(str, value["payload_hash"]),
        cast(str, value["canonical_hash"]),
    )


def _validated_fill(value: dict[str, object]) -> ValidatedExternalPaperFill:
    return ValidatedExternalPaperFill(
        cast(str, value["validated_fill_id"]),
        cast(str, value["instruction_id"]),
        cast(str, value["authorization_id"]),
        cast(str, value["submission_attempt_id"]),
        cast(str, value["broker_order_id"]),
        cast(str, value["evidence_id"]),
        cast(str, value["evidence_hash"]),
        cast(str, value["symbol"]),
        cast(str, value["side"]),
        parse_decimal(cast(str, value["cumulative_quantity"])),
        parse_decimal(cast(str, value["newly_observed_quantity"])),
        parse_decimal(cast(str, value["fill_price"])),
        parse_decimal(cast(str, value["fee"])),
        _when(value["occurred_at"]),
        ValidatedExternalPaperFillState(cast(str, value["state"])),
        cast(str | None, value.get("reason_code")),
        cast(str, value["canonical_hash"]),
    )


def _binding(value: dict[str, object]) -> ExternalPaperInternalExecutionBinding:
    return ExternalPaperInternalExecutionBinding(
        cast(str, value["binding_id"]),
        cast(str, value["authorization_id"]),
        cast(str, value["instruction_id"]),
        cast(str, value["campaign_id"]),
        cast(str, value["paper_trial_authorization_id"]),
        cast(str, value["portfolio_instruction_id"]),
        cast(str, value["securities_paper_run_id"]),
        cast(str, value["internal_order_id"]),
        cast(str, value["risk_reservation_id"]),
        cast(str, value["execution_policy_id"]),
        cast(str, value["execution_policy_hash"]),
        cast(str, value["instrument_id"]),
        cast(str, value["symbol"]),
        cast(str, value["side"]),
        parse_decimal(cast(str, value["intended_quantity"])),
        parse_decimal(cast(str, value["intended_notional"])),
        cast(str, value["intended_session"]),
        cast(str | None, value.get("expected_execution_hash")),
        cast(str, value["idempotency_key"]),
        ExternalPaperInternalExecutionBindingState(cast(str, value["state"])),
        cast(str, value["canonical_hash"]),
    )
