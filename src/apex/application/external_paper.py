"""Idempotent external PAPER submission orchestration with no LIVE authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable

from apex.application.autopilot import StrategyAuthorityState, StrategyDescriptor
from apex.application.evidence_review import PaperTrialAuthorization
from apex.application.securities_paper import (
    ApplySecuritiesPaperFillIncrement,
    ApplySecuritiesPaperFillIncrementCommand,
    SecuritiesPaperCampaignBoundary,
    SecuritiesPaperRepository,
)
from apex.domain.external_paper import (
    ALPACA_PAPER_BROKER_V1,
    ExternalPaperAuthorization,
    ExternalPaperBrokerError,
    ExternalPaperBrokerEvidence,
    ExternalPaperCampaignObservation,
    ExternalPaperCapabilityState,
    ExternalPaperInstruction,
    ExternalPaperInstructionState,
    ExternalPaperInternalExecutionBinding,
    ExternalPaperInternalExecutionBindingState,
    ExternalPaperReconciliation,
    ExternalPaperSubmissionAmbiguous,
    ExternalPaperSubmissionAttempt,
    ExternalPaperSubmissionState,
    PaperBrokerCapabilitySnapshot,
    ValidatedExternalPaperFill,
    ValidatedExternalPaperFillState,
    reconcile_external_paper,
)
from apex.domain.securities_paper import (
    SecuritiesExecutionSource,
    SecuritiesOrderAction,
    SecuritiesOrderState,
    SecuritiesPaperAuthorization,
    SecuritiesPaperOrder,
    create_securities_order,
)
from apex.domain.values import ContentHash, Money, Quantity, content_digest, deterministic_id


class ExternalPaperApplicationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PersistentAlpacaPaperAcceptanceWorkflowResult:
    """The no-submission terminal result of the engineering acceptance flow."""

    acceptance: AlpacaPaperAcceptanceCampaign
    instruction: ExternalPaperInstruction
    binding: ExternalPaperInternalExecutionBinding
    preflight: AlpacaPaperPreflightResult


class PersistentAlpacaPaperAcceptanceWorkflowPort(Protocol):
    """Normal authority/preparation owner; deliberately not a SQL shortcut."""

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
    ]: ...


ACCEPTANCE_PURPOSE = "ENGINEERING_ACCEPTANCE_ONLY"
ALPACA_PAPER_ENVIRONMENT = "ALPACA_PAPER"


@dataclass(frozen=True, slots=True)
class AlpacaPaperAcceptanceCampaign:
    """Immutable engineering-only projection over normal PAPER authority.

    This is deliberately an evidence/status record, not a second Campaign or
    authorization authority.  The referenced IDs must already have been
    produced by the normal Campaign, trial, authorization, Order, reservation,
    and binding services.
    """

    acceptance_id: str
    idempotency_key: str
    campaign_id: str
    trial_id: str
    authorization_id: str
    instruction_id: str
    reservation_id: str
    internal_order_id: str
    binding_id: str
    symbol: str
    expires_at: datetime
    readiness_evidence_ids: tuple[str, ...]
    canonical_input_hash: str
    canonical_result_hash: str

    @classmethod
    def create(
        cls,
        *,
        idempotency_key: str,
        campaign_id: str,
        trial_id: str,
        authorization_id: str,
        instruction_id: str,
        reservation_id: str,
        internal_order_id: str,
        binding_id: str,
        symbol: str,
        expires_at: datetime,
        readiness_evidence_ids: tuple[str, ...],
    ) -> AlpacaPaperAcceptanceCampaign:
        payload = {
            "purpose": ACCEPTANCE_PURPOSE,
            "environment": ALPACA_PAPER_ENVIRONMENT,
            "idempotency_key": idempotency_key,
            "campaign_id": campaign_id,
            "trial_id": trial_id,
            "authorization_id": authorization_id,
            "instruction_id": instruction_id,
            "reservation_id": reservation_id,
            "internal_order_id": internal_order_id,
            "binding_id": binding_id,
            "symbol": symbol,
            "quantity": "1",
            "maximum_notional": "1000",
            "expires_at": expires_at,
            "readiness_evidence_ids": readiness_evidence_ids,
        }
        input_hash = content_digest(payload)
        return cls(
            deterministic_id("alpaca_paper_acceptance", payload),
            idempotency_key,
            campaign_id,
            trial_id,
            authorization_id,
            instruction_id,
            reservation_id,
            internal_order_id,
            binding_id,
            symbol,
            expires_at,
            readiness_evidence_ids,
            input_hash,
            content_digest(
                {
                    "acceptance_id": deterministic_id("alpaca_paper_acceptance", payload),
                    "input": input_hash,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class AlpacaPaperAcceptancePreflight:
    acceptance_id: str
    instruction_id: str
    state: str
    blockers: tuple[str, ...]
    confirmation: str | None
    checked_at: datetime
    freshness_expires_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls, *, acceptance_id: str, result: AlpacaPaperPreflightResult, checked_at: datetime
    ) -> AlpacaPaperAcceptancePreflight:
        # A five-minute freshness window is intentionally shorter than the 24h
        # acceptance expiry and only scopes the operator confirmation surface.
        expires = checked_at + timedelta(minutes=5)
        payload = {
            "acceptance_id": acceptance_id,
            "instruction_id": result.instruction_id,
            "state": result.state,
            "blockers": result.blockers,
            "confirmation": result.confirmation,
            "checked_at": checked_at,
            "freshness_expires_at": expires,
        }
        return cls(
            acceptance_id,
            result.instruction_id,
            result.state,
            result.blockers,
            result.confirmation,
            checked_at,
            expires,
            content_digest(payload),
        )


class ExternalPaperBrokerPort(Protocol):
    def submit(self, instruction: ExternalPaperInstruction) -> ExternalPaperBrokerEvidence: ...


class ExternalPaperReadPort(Protocol):
    def order(
        self, *, instruction_id: str, broker_order_id: str
    ) -> ExternalPaperBrokerEvidence: ...

    def cancel(
        self, *, instruction_id: str, broker_order_id: str
    ) -> ExternalPaperBrokerEvidence: ...


@runtime_checkable
class ExternalPaperCampaignReadPort(Protocol):
    def account(self) -> ExternalPaperBrokerEvidence: ...

    def orders(self) -> tuple[ExternalPaperBrokerEvidence, ...]: ...

    def positions(self) -> tuple[ExternalPaperBrokerEvidence, ...]: ...

    def activities(self) -> tuple[ExternalPaperBrokerEvidence, ...]: ...


class AlpacaPaperSynchronizationReadPort(Protocol):
    """The narrow read surface required by one exact Alpaca PAPER sync."""

    identity: str

    @property
    def credentials_available(self) -> bool: ...

    @property
    def network_reads(self) -> int: ...

    @property
    def network_writes(self) -> int: ...

    def order(
        self, *, instruction_id: str, broker_order_id: str
    ) -> ExternalPaperBrokerEvidence: ...

    def account(self) -> ExternalPaperBrokerEvidence: ...

    def position(self, *, symbol: str) -> ExternalPaperBrokerEvidence | None: ...

    def activities(self) -> tuple[ExternalPaperBrokerEvidence, ...]: ...


@runtime_checkable
class ExternalPaperAmbiguityRecoveryPort(Protocol):
    def recover_submission(self, *, instruction_id: str) -> ExternalPaperBrokerEvidence: ...


class ExternalPaperAuthorityPort(Protocol):
    """Read-only authority revalidation; implementation owns Campaign/trial lookup."""

    def blockers(
        self, *, authorization: ExternalPaperAuthorization, now: datetime
    ) -> tuple[str, ...]: ...


class ExternalPaperRepository(Protocol):
    def acceptance_by_key(self, idempotency_key: str) -> AlpacaPaperAcceptanceCampaign | None: ...
    def acceptance(self, acceptance_id: str) -> AlpacaPaperAcceptanceCampaign | None: ...
    def acceptance_for_campaign(self, campaign_id: str) -> AlpacaPaperAcceptanceCampaign | None: ...
    def acceptance_for_instruction(
        self, instruction_id: str
    ) -> AlpacaPaperAcceptanceCampaign | None: ...
    def save_acceptance(
        self, acceptance: AlpacaPaperAcceptanceCampaign
    ) -> AlpacaPaperAcceptanceCampaign: ...
    def save_acceptance_preflight(
        self, preflight: AlpacaPaperAcceptancePreflight
    ) -> AlpacaPaperAcceptancePreflight: ...
    def latest_acceptance_preflight(
        self, instruction_id: str
    ) -> AlpacaPaperAcceptancePreflight | None: ...
    def authorization(self, authorization_id: str) -> ExternalPaperAuthorization | None: ...
    def save_authorization(
        self, authorization: ExternalPaperAuthorization
    ) -> ExternalPaperAuthorization: ...
    def instruction(self, instruction_id: str) -> ExternalPaperInstruction | None: ...
    def instruction_by_key(self, key: str) -> ExternalPaperInstruction | None: ...
    def instructions_for_campaign(
        self, campaign_id: str
    ) -> tuple[ExternalPaperInstruction, ...]: ...
    def save_instruction(
        self, instruction: ExternalPaperInstruction
    ) -> ExternalPaperInstruction: ...
    def save_evidence(
        self, evidence: ExternalPaperBrokerEvidence
    ) -> ExternalPaperBrokerEvidence: ...
    def save_capability_snapshot(
        self, snapshot: PaperBrokerCapabilitySnapshot
    ) -> PaperBrokerCapabilitySnapshot: ...
    def broker_evidence(self, evidence_id: str) -> ExternalPaperBrokerEvidence | None: ...
    def save_campaign_observation(
        self, observation: ExternalPaperCampaignObservation
    ) -> ExternalPaperCampaignObservation: ...
    def has_unknown_broker_activity(self, campaign_id: str) -> bool: ...
    def evidence_for_instruction(
        self, instruction_id: str
    ) -> tuple[ExternalPaperBrokerEvidence, ...]: ...
    def reserve_attempt(
        self, attempt: ExternalPaperSubmissionAttempt
    ) -> ExternalPaperSubmissionAttempt: ...
    def conclude_attempt(
        self, attempt: ExternalPaperSubmissionAttempt
    ) -> ExternalPaperSubmissionAttempt: ...
    def submission_attempt(self, instruction_id: str) -> ExternalPaperSubmissionAttempt | None: ...
    def external_paper_killed(self, campaign_id: str) -> bool: ...
    def kill(self, *, campaign_id: str, reason: str, operator_action_id: str) -> None: ...
    def save_reconciliation(
        self, report: ExternalPaperReconciliation
    ) -> ExternalPaperReconciliation: ...
    def reconciliations_for_instruction(
        self, instruction_id: str
    ) -> tuple[ExternalPaperReconciliation, ...]: ...
    def validated_fill_for_evidence(
        self, evidence_id: str
    ) -> ValidatedExternalPaperFill | None: ...
    def validated_fill(self, validated_fill_id: str) -> ValidatedExternalPaperFill | None: ...
    def validated_fills_for_instruction(
        self, instruction_id: str
    ) -> tuple[ValidatedExternalPaperFill, ...]: ...
    def save_validated_fill(
        self, fill: ValidatedExternalPaperFill
    ) -> ValidatedExternalPaperFill: ...
    def binding_for_instruction(
        self, instruction_id: str
    ) -> ExternalPaperInternalExecutionBinding | None: ...
    def save_binding(
        self, binding: ExternalPaperInternalExecutionBinding
    ) -> ExternalPaperInternalExecutionBinding: ...
    def fill_application(self, validated_fill_id: str) -> ExternalPaperFillApplication | None: ...
    def save_fill_application(
        self, application: ExternalPaperFillApplication
    ) -> ExternalPaperFillApplication: ...


@dataclass(frozen=True, slots=True)
class ExternalPaperFillApplication:
    """Immutable link from validated broker evidence to the shared fill increment."""

    validated_fill_id: str
    internal_fill_id: str
    internal_position_id: str | None
    journal_ids: tuple[str, ...]
    applied_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        validated_fill_id: str,
        internal_fill_id: str,
        internal_position_id: str | None,
        journal_ids: tuple[str, ...],
        applied_at: datetime,
    ) -> ExternalPaperFillApplication:
        payload = {
            "validated_fill_id": validated_fill_id,
            "internal_fill_id": internal_fill_id,
            "internal_position_id": internal_position_id,
            "journal_ids": journal_ids,
            "applied_at": applied_at,
        }
        return cls(
            validated_fill_id,
            internal_fill_id,
            internal_position_id,
            journal_ids,
            applied_at,
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class ExternalPaperReadinessResult:
    capability: PaperBrokerCapabilitySnapshot
    evidence_ids: tuple[str, ...]
    endpoint_states: tuple[tuple[str, str], ...]


class CheckAlpacaPaperReadiness:
    """Persist bounded authenticated PAPER observations without financial action."""

    def __init__(self, *, repository: ExternalPaperRepository, broker: object) -> None:
        self._repository = repository
        self._broker = broker

    def execute(self, *, now: datetime) -> ExternalPaperReadinessResult:
        checker = getattr(self._broker, "check_readiness", None)
        if checker is None:
            raise ExternalPaperApplicationError("ALPACA_PAPER_READINESS_PORT_REQUIRED")
        result = checker(checked_at=now)
        capability = self._repository.save_capability_snapshot(result.capability)
        evidence = tuple(self._repository.save_evidence(item) for item in result.evidence)
        return ExternalPaperReadinessResult(
            capability=capability,
            evidence_ids=tuple(item.evidence_id for item in evidence),
            endpoint_states=result.endpoint_states,
        )


def alpaca_paper_submission_confirmation(*, instruction: ExternalPaperInstruction) -> str:
    return f"SUBMIT_ALPACA_PAPER:{instruction.instruction_id}:{instruction.canonical_hash}"


@dataclass(frozen=True, slots=True)
class AlpacaPaperPreflightResult:
    state: str
    instruction_id: str
    blockers: tuple[str, ...]
    confirmation: str | None
    account_state: str | None
    open_order_count: int
    open_position_count: int
    estimated_notional: Decimal | None
    maximum_notional: Decimal | None


class PreflightAlpacaPaperInstruction:
    """Check one prepared opening against a fresh read-only PAPER snapshot.

    The preflight deliberately does not reserve a submission attempt and never
    calls the broker submit endpoint.  That remains a later, confirmed action.
    """

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        securities_repository: SecuritiesPaperRepository,
    ) -> None:
        self._repository = repository
        self._securities_repository = securities_repository

    def execute(
        self,
        *,
        instruction_id: str,
        readiness: ExternalPaperReadinessResult,
        now: datetime,
    ) -> AlpacaPaperPreflightResult:
        instruction = self._repository.instruction(instruction_id)
        blockers: list[str] = []
        if instruction is None:
            return AlpacaPaperPreflightResult(
                "BLOCKED", instruction_id, ("INSTRUCTION_NOT_FOUND",), None, None, 0, 0, None, None
            )
        binding = self._repository.binding_for_instruction(instruction_id)
        authorization = self._repository.authorization(instruction.authorization_id)
        if instruction.side != "BUY":
            blockers.append("OPENING_BUY_REQUIRED")
        if binding is None:
            blockers.append("PREPARED_BINDING_REQUIRED")
        if authorization is None or not authorization.active_at(now):
            blockers.append("AUTHORIZATION_INACTIVE")
        if self._repository.external_paper_killed(instruction.campaign_id):
            blockers.append("EXTERNAL_PAPER_KILLED")
        if self._repository.has_unknown_broker_activity(instruction.campaign_id):
            blockers.append("UNKNOWN_BROKER_ACTIVITY_BLOCK")
        if instruction.state is not ExternalPaperInstructionState.AUTHORIZED:
            blockers.append("INSTRUCTION_NOT_AUTHORIZED")
        if instruction.quantity != Decimal("1") or instruction.maximum_notional > Decimal("1000"):
            blockers.append("ACCEPTANCE_ONE_SHARE_OR_NOTIONAL_CAP")
        if instruction.order_type != "MARKET" or instruction.time_in_force != "DAY":
            blockers.append("ACCEPTANCE_DAY_MARKET_POLICY_REQUIRED")
        if instruction.expires_at <= now:
            blockers.append("INSTRUCTION_EXPIRED")
        if readiness.capability.state is not ExternalPaperCapabilityState.READY:
            blockers.append("PAPER_READINESS_NOT_READY")
        if binding is not None:
            order = self._securities_repository.order(binding.internal_order_id)
            if order.state is not SecuritiesOrderState.READY:
                blockers.append("INTERNAL_ORDER_NOT_PENDING")
        account_state: str | None = None
        open_orders = 0
        open_positions = 0
        for evidence_id in readiness.evidence_ids:
            evidence = self._repository.broker_evidence(evidence_id)
            if evidence is None:
                continue
            if evidence.observation_type == "ACCOUNT_READINESS":
                account_state = evidence.sanitized_payload.get("status")
                if evidence.sanitized_payload.get("trading_blocked") == "True":
                    blockers.append("PAPER_ACCOUNT_TRADING_BLOCKED")
                if evidence.sanitized_payload.get("account_blocked") == "True":
                    blockers.append("PAPER_ACCOUNT_BLOCKED")
            elif evidence.observation_type == "ORDER_LIST_ITEM":
                open_orders += 1
            elif evidence.observation_type == "POSITION_SNAPSHOT_ITEM":
                open_positions += 1
        if open_orders:
            blockers.append("PAPER_OPEN_ORDER_PRESENT")
        if open_positions:
            blockers.append("PAPER_POSITION_PRESENT")
        result = AlpacaPaperPreflightResult(
            "READY_FOR_OPERATOR_CONFIRMATION" if not blockers else "BLOCKED",
            instruction.instruction_id,
            tuple(sorted(set(blockers))),
            alpaca_paper_submission_confirmation(instruction=instruction) if not blockers else None,
            account_state,
            open_orders,
            open_positions,
            instruction.maximum_notional,
            authorization.maximum_capital if authorization is not None else None,
        )
        acceptance = self._repository.acceptance_for_instruction(instruction_id)
        if acceptance is not None:
            self._repository.save_acceptance_preflight(
                AlpacaPaperAcceptancePreflight.create(
                    acceptance_id=acceptance.acceptance_id, result=result, checked_at=now
                )
            )
        return result


class CreateAlpacaPaperAcceptanceCampaign:
    """Persist the exact result of an already-governed acceptance workflow.

    Callers must execute the normal production services before this final
    projection.  Keeping those operations outside this small persistence step
    prevents this feature from approving Campaign/trial/financial state by
    direct SQL insertion.
    """

    def __init__(self, *, repository: ExternalPaperRepository) -> None:
        self._repository = repository

    def execute(
        self, *, acceptance: AlpacaPaperAcceptanceCampaign
    ) -> AlpacaPaperAcceptanceCampaign:
        if acceptance.symbol not in FIXED_ACCEPTANCE_SYMBOLS:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_ACCEPTANCE_SYMBOL_INVALID")
        existing = self._repository.acceptance_by_key(acceptance.idempotency_key)
        if existing is not None:
            if existing.canonical_input_hash != acceptance.canonical_input_hash:
                raise ExternalPaperApplicationError(
                    "EXTERNAL_PAPER_ACCEPTANCE_IDEMPOTENCY_CONFLICT"
                )
            return existing
        return self._repository.save_acceptance(acceptance)


class CreatePersistentAlpacaPaperAcceptanceWorkflow:
    """Compose normal authority/preparation work with retained acceptance preflight.

    The injected port owns Campaign, strategy, trial, risk, Order and binding
    transitions.  This service owns acceptance policy validation, idempotency
    projection, and the final no-submission preflight.
    """

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        securities_repository: SecuritiesPaperRepository,
        authority_port: PersistentAlpacaPaperAcceptanceWorkflowPort,
    ) -> None:
        self._repository = repository
        self._securities_repository = securities_repository
        self._authority_port = authority_port

    def execute(
        self,
        *,
        symbol: str,
        idempotency_key: str,
        readiness: ExternalPaperReadinessResult,
        now: datetime,
    ) -> PersistentAlpacaPaperAcceptanceWorkflowResult:
        if readiness.capability.state is not ExternalPaperCapabilityState.READY:
            raise ExternalPaperApplicationError(
                ",".join(readiness.capability.reason_codes) or "PAPER_READINESS_NOT_READY"
            )
        if symbol not in FIXED_ACCEPTANCE_SYMBOLS or not idempotency_key:
            raise ExternalPaperApplicationError(
                "EXTERNAL_PAPER_ACCEPTANCE_SYMBOL_OR_IDEMPOTENCY_INVALID"
            )
        acceptance, instruction, binding = self._authority_port.create_acceptance_components(
            symbol=symbol, idempotency_key=idempotency_key, now=now, readiness=readiness
        )
        acceptance = CreateAlpacaPaperAcceptanceCampaign(repository=self._repository).execute(
            acceptance=acceptance
        )
        preflight = PreflightAlpacaPaperInstruction(
            repository=self._repository, securities_repository=self._securities_repository
        ).execute(instruction_id=instruction.instruction_id, readiness=readiness, now=now)
        return PersistentAlpacaPaperAcceptanceWorkflowResult(
            acceptance, instruction, binding, preflight
        )


FIXED_ACCEPTANCE_SYMBOLS = frozenset(
    {
        "SPY",
        "QQQ",
        "DIA",
        "IWM",
        "EFA",
        "EEM",
        "TLT",
        "LQD",
        "GLD",
        "USO",
        "XLF",
        "XLK",
        "XLE",
        "XLV",
        "XLI",
        "XLP",
    }
)


@dataclass(frozen=True, slots=True)
class ExternalPaperSubmissionResult:
    instruction: ExternalPaperInstruction
    evidence: ExternalPaperBrokerEvidence | None
    replayed: bool


class CreateAuthorizedExternalPaperInstruction:
    """Persist a bounded instruction only under its retained external authority."""

    def __init__(self, *, repository: ExternalPaperRepository) -> None:
        self._repository = repository

    def execute(
        self, *, instruction: ExternalPaperInstruction, now: datetime
    ) -> ExternalPaperInstruction:
        replay = self._repository.instruction_by_key(instruction.idempotency_key)
        if replay is not None:
            if replay.canonical_hash != instruction.canonical_hash:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_IDEMPOTENCY_CONFLICT")
            return replay
        authorization = self._repository.authorization(instruction.authorization_id)
        if (
            authorization is None
            or authorization.campaign_id != instruction.campaign_id
            or not authorization.active_at(now)
            or instruction.symbol not in authorization.allowed_symbols
            or len(self._repository.instructions_for_campaign(instruction.campaign_id))
            >= authorization.maximum_order_count
            or (
                instruction.side == "BUY"
                and (
                    self._repository.external_paper_killed(instruction.campaign_id)
                    or self._repository.has_unknown_broker_activity(instruction.campaign_id)
                )
            )
        ):
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_INSTRUCTION_AUTHORITY_INVALID")
        return self._repository.save_instruction(instruction)


class ValidateExternalPaperFill:
    """Validate immutable cumulative broker fill evidence before accounting."""

    def __init__(self, *, repository: ExternalPaperRepository) -> None:
        self._repository = repository

    def execute(self, *, evidence: ExternalPaperBrokerEvidence) -> ValidatedExternalPaperFill:
        if evidence.instruction_id is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_FILL_INSTRUCTION_REQUIRED")
        existing = self._repository.validated_fill_for_evidence(evidence.evidence_id)
        if existing is not None:
            return existing
        instruction = self._repository.instruction(evidence.instruction_id)
        if instruction is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND")
        authorization = self._repository.authorization(instruction.authorization_id)
        attempt = self._repository.submission_attempt(instruction.instruction_id)
        if authorization is None or attempt is None:
            raise ExternalPaperApplicationError(
                "EXTERNAL_PAPER_FILL_AUTHORIZATION_OR_ATTEMPT_MISSING"
            )
        prior = max(
            (
                item.cumulative_quantity
                for item in self._repository.validated_fills_for_instruction(
                    instruction.instruction_id
                )
            ),
            default=Decimal(),
        )
        result = ValidatedExternalPaperFill.observed(
            instruction=instruction,
            authorization=authorization,
            submission_attempt=attempt,
            evidence=evidence,
            prior_cumulative_quantity=str(prior),
        )
        if result.state is ValidatedExternalPaperFillState.OBSERVED:
            result = result.validate()
        return self._repository.save_validated_fill(result)


class ApplyValidatedExternalPaperFill:
    """Translate validated broker evidence into the one shared PAPER accounting command."""

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        securities_repository: SecuritiesPaperRepository,
    ) -> None:
        self._repository = repository
        self._securities_repository = securities_repository
        self._apply_increment = ApplySecuritiesPaperFillIncrement(securities_repository)

    def execute(self, *, validated_fill_id: str) -> ExternalPaperFillApplication:
        existing = self._repository.fill_application(validated_fill_id)
        if existing is not None:
            return existing
        validated = self._repository.validated_fill(validated_fill_id)
        if validated is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_VALIDATED_FILL_NOT_FOUND")
        if validated.state is not ValidatedExternalPaperFillState.VALIDATED:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_FILL_NOT_VALIDATED")
        binding = self._repository.binding_for_instruction(validated.instruction_id)
        instruction = self._repository.instruction(validated.instruction_id)
        authorization = self._repository.authorization(validated.authorization_id)
        if binding is None or instruction is None or authorization is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_FILL_BINDING_MISSING")
        if (
            binding.authorization_id != validated.authorization_id
            or binding.symbol != validated.symbol
            or binding.side.lower() != validated.side.lower()
            or instruction.authorization_id != validated.authorization_id
            or instruction.symbol != validated.symbol
            or instruction.side.lower() != validated.side.lower()
            or authorization.campaign_id != binding.campaign_id
            or (
                instruction.side == "BUY"
                and self._repository.external_paper_killed(binding.campaign_id)
            )
        ):
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_FILL_BINDING_MISMATCH")
        order = self._securities_repository.order(binding.internal_order_id)
        run = self._securities_repository.run(order.run_id)
        if (
            order.instrument_id != binding.instrument_id
            or order.action
            is not (
                SecuritiesOrderAction.BUY if binding.side == "BUY" else SecuritiesOrderAction.SELL
            )
            or order.run_id != binding.securities_paper_run_id
            or order.requested_quantity.value != binding.intended_quantity
            or run.campaign_id != binding.campaign_id
            or run.trial_authorization_id != binding.paper_trial_authorization_id
            or run.execution_policy_id != binding.execution_policy_id
            or run.execution_policy_hash.value != binding.execution_policy_hash
        ):
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_INTERNAL_ORDER_MISMATCH")
        command = ApplySecuritiesPaperFillIncrementCommand(
            campaign_id=binding.campaign_id,
            paper_trial_authorization_id=binding.paper_trial_authorization_id,
            strategy_id=run.strategy_id,
            strategy_version=run.strategy_version,
            strategy_configuration_hash=run.strategy_configuration_hash,
            order_id=order.order_id,
            risk_reservation_id=binding.risk_reservation_id,
            position_id=order.position_id,
            instrument_id=binding.instrument_id,
            symbol=binding.symbol,
            action=order.action,
            incremental_quantity=Quantity(
                validated.newly_observed_quantity,
                order.requested_quantity.increment,
            ),
            expected_cumulative_quantity=Quantity(
                validated.cumulative_quantity,
                order.requested_quantity.increment,
            ),
            authorized_quantity=order.requested_quantity,
            fill_price=validated.fill_price,
            fee=Money(validated.fee),
            fill_at=validated.occurred_at,
            execution_source=SecuritiesExecutionSource.EXTERNAL_PAPER_VALIDATED_FILL,
            source_evidence_id=validated.validated_fill_id,
            economic_identity_hash=ContentHash(validated.evidence_hash),
            external_validated_fill_id=validated.validated_fill_id,
            internal_simulation_evidence_id=None,
            execution_policy_hash=ContentHash(binding.execution_policy_hash),
            idempotency_key=validated.validated_fill_id,
            canonical_input_hash=ContentHash("0" * 64),
        )
        result = self._apply_increment.execute(
            replace(command, canonical_input_hash=command.expected_hash())
        )
        application = ExternalPaperFillApplication.create(
            validated_fill_id=validated.validated_fill_id,
            internal_fill_id=result.fill.fill_id,
            internal_position_id=(result.position.position_id if result.position else None),
            journal_ids=tuple(journal.journal_id for journal in result.journals),
            applied_at=validated.occurred_at,
        )
        saved = self._repository.save_fill_application(application)
        target = (
            ExternalPaperInternalExecutionBindingState.FULLY_APPLIED
            if result.order.state is SecuritiesOrderState.FILLED
            else ExternalPaperInternalExecutionBindingState.PARTIALLY_APPLIED
        )
        self._repository.save_binding(binding.transition(target))
        return saved


class PrepareAuthorizedExternalPaperExecution:
    """Create one unfilled internal reservation/order bridge before broker I/O."""

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        securities_repository: SecuritiesPaperRepository | None = None,
    ) -> None:
        self._repository = repository
        self._securities_repository = securities_repository

    def execute(
        self,
        *,
        binding: ExternalPaperInternalExecutionBinding,
        now: datetime,
        securities_authorization: SecuritiesPaperAuthorization | None = None,
        pending_order: SecuritiesPaperOrder | None = None,
    ) -> ExternalPaperInternalExecutionBinding:
        existing = self._repository.binding_for_instruction(binding.instruction_id)
        if existing is not None:
            if existing.canonical_hash != binding.canonical_hash:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_INTERNAL_BINDING_CONFLICT")
            return existing
        instruction = self._repository.instruction(binding.instruction_id)
        authorization = self._repository.authorization(binding.authorization_id)
        if instruction is None or authorization is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_BINDING_INPUT_MISSING")
        if (
            instruction.authorization_id != authorization.authorization_id
            or instruction.campaign_id != binding.campaign_id
            or instruction.state is not ExternalPaperInstructionState.AUTHORIZED
            or not authorization.active_at(now)
            or (
                instruction.side == "BUY"
                and self._repository.external_paper_killed(binding.campaign_id)
            )
        ):
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_BINDING_AUTHORITY_INVALID")
        if (securities_authorization is None) != (pending_order is None):
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_PREPARATION_INPUT_INCOMPLETE")
        if securities_authorization is not None and pending_order is not None:
            if self._securities_repository is None:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_SECURITIES_REPOSITORY_REQUIRED")
            run = self._securities_repository.run(pending_order.run_id)
            if (
                not securities_authorization.accepted
                or securities_authorization.reservation_id != binding.risk_reservation_id
                or pending_order.order_id != binding.internal_order_id
                or pending_order.authorization_id != securities_authorization.authorization_id
                or pending_order.run_id != binding.securities_paper_run_id
                or pending_order.instrument_id != binding.instrument_id
                or pending_order.action
                is not (
                    SecuritiesOrderAction.BUY
                    if instruction.side == "BUY"
                    else SecuritiesOrderAction.SELL
                )
                or pending_order.state is not SecuritiesOrderState.READY
                or pending_order.filled_quantity.value != 0
                or pending_order.requested_quantity.value != binding.intended_quantity
                or run.campaign_id != binding.campaign_id
                or run.trial_authorization_id != binding.paper_trial_authorization_id
                or run.execution_policy_id != binding.execution_policy_id
                or run.execution_policy_hash.value != binding.execution_policy_hash
            ):
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_PREPARATION_LINEAGE_MISMATCH")
            self._securities_repository.save_authorization(securities_authorization)
            self._securities_repository.save_pending_order(pending_order)
        return self._repository.save_binding(binding)


@dataclass(frozen=True, slots=True)
class ExternalPaperReductionPreparation:
    """The retained, unfilled internal SELL order and its external binding."""

    order: SecuritiesPaperOrder
    binding: ExternalPaperInternalExecutionBinding


class PrepareAuthorizedExternalPaperReduction:
    """Prepare one bounded risk-reducing external SELL without creating new risk.

    The inherited binding field is a durable exit reference, not a new Campaign
    reservation.  Existing validated-fill accounting consumes the reference only
    as lineage while `apply_sell_fill` enforces the Position's no-short bound.
    """

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        securities_repository: SecuritiesPaperRepository,
    ) -> None:
        self._repository = repository
        self._securities_repository = securities_repository

    def execute(
        self,
        *,
        instruction_id: str,
        position_id: str,
        decision_at: datetime,
        earliest_fill_at: datetime,
        expires_at: datetime,
    ) -> ExternalPaperReductionPreparation:
        instruction = self._repository.instruction(instruction_id)
        if instruction is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND")
        authorization = self._repository.authorization(instruction.authorization_id)
        position = self._securities_repository.position(position_id)
        run = self._securities_repository.run(position.run_id)
        if (
            authorization is None
            or instruction.side != "SELL"
            or instruction.state is not ExternalPaperInstructionState.AUTHORIZED
            or instruction.campaign_id != position.campaign_id
            or instruction.symbol != position.current_symbol
            or instruction.quantity > position.open_quantity.value
            or position.open_quantity.value <= 0
            or run.campaign_id != position.campaign_id
            or run.trial_authorization_id != authorization.paper_trial_authorization_id
            or not authorization.active_at(decision_at)
        ):
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_REDUCTION_AUTHORITY_INVALID")
        quantity = Quantity(instruction.quantity, position.open_quantity.increment)
        order = create_securities_order(
            run_id=run.run_id,
            instrument_id=position.instrument_id,
            action=SecuritiesOrderAction.SELL,
            quantity=quantity,
            reserved_amount=Money(position.average_cost * quantity.value),
            decision_at=decision_at,
            earliest_fill_at=earliest_fill_at,
            expires_at=expires_at,
            idempotency_key=f"{instruction.idempotency_key}:reduce:{position.position_id}",
            authorization_id=deterministic_id(
                "external_paper_exit_authorization",
                {"instruction_id": instruction.instruction_id, "position_id": position.position_id},
            ),
            position_id=position.position_id,
        )
        binding = ExternalPaperInternalExecutionBinding.pending(
            authorization=authorization,
            instruction=instruction,
            securities_paper_run_id=run.run_id,
            internal_order_id=order.order_id,
            risk_reservation_id=deterministic_id(
                "external_paper_exit_reference",
                {"instruction_id": instruction.instruction_id, "position_id": position.position_id},
            ),
            execution_policy_id=run.execution_policy_id,
            execution_policy_hash=run.execution_policy_hash.value,
            instrument_id=position.instrument_id,
            intended_session=earliest_fill_at.date().isoformat(),
            idempotency_key=f"{instruction.idempotency_key}:reduce-binding:{position.position_id}",
        )
        existing = self._repository.binding_for_instruction(instruction.instruction_id)
        if existing is not None:
            if existing.canonical_hash != binding.canonical_hash:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_INTERNAL_BINDING_CONFLICT")
            return ExternalPaperReductionPreparation(
                order=self._securities_repository.order(existing.internal_order_id),
                binding=existing,
            )
        self._securities_repository.save_pending_order(order)
        return ExternalPaperReductionPreparation(
            order=order,
            binding=self._repository.save_binding(binding),
        )


def external_paper_authorization_confirmation(
    *, campaign_id: str, trial_id: str, portfolio_instruction_id: str
) -> str:
    return f"AUTHORIZE_EXTERNAL_PAPER:{campaign_id}:{trial_id}:{portfolio_instruction_id}"


def external_paper_kill_confirmation(*, campaign_id: str) -> str:
    return f"KILL_EXTERNAL_PAPER:{campaign_id}"


class KillExternalPaper:
    """Durably block new external-PAPER openings without affecting observation or exits."""

    def __init__(self, *, repository: ExternalPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        campaign_id: str,
        reason: str,
        confirmation_text: str,
        operator_action_id: str,
    ) -> bool:
        if not campaign_id or not reason or not operator_action_id:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_KILL_INPUT_REQUIRED")
        if confirmation_text != external_paper_kill_confirmation(campaign_id=campaign_id):
            raise ExternalPaperApplicationError("EXACT_OPERATOR_CONFIRMATION_REQUIRED")
        already_killed = self._repository.external_paper_killed(campaign_id)
        if not already_killed:
            self._repository.kill(
                campaign_id=campaign_id,
                reason=reason,
                operator_action_id=operator_action_id,
            )
        return already_killed


class CreateExternalPaperAuthorization:
    """Create a broker binding only after the existing PAPER authority is active."""

    def __init__(
        self, *, repository: ExternalPaperRepository, campaigns: SecuritiesPaperCampaignBoundary
    ) -> None:
        self._repository = repository
        self._campaigns = campaigns

    def execute(
        self,
        *,
        authorization: ExternalPaperAuthorization,
        trial: PaperTrialAuthorization,
        descriptor: StrategyDescriptor,
        portfolio_instruction: object,
        confirmation_text: str,
        now: datetime,
    ) -> ExternalPaperAuthorization:
        expected = external_paper_authorization_confirmation(
            campaign_id=authorization.campaign_id,
            trial_id=authorization.paper_trial_authorization_id,
            portfolio_instruction_id=authorization.portfolio_instruction_id,
        )
        reasons: list[str] = []
        if confirmation_text != expected:
            reasons.append("EXACT_OPERATOR_CONFIRMATION_REQUIRED")
        if self._campaigns.operating_state(authorization.campaign_id) != "RUNNING":
            reasons.append("CAMPAIGN_NOT_RUNNING")
        if (
            not trial.active_at(now)
            or trial.authorization_id != authorization.paper_trial_authorization_id
        ):
            reasons.append("ACTIVE_PAPER_TRIAL_REQUIRED")
        if trial.campaign_id != authorization.campaign_id:
            reasons.append("TRIAL_CAMPAIGN_BINDING_MISMATCH")
        if descriptor.authority_state is not StrategyAuthorityState.PAPER:
            reasons.append("GLOBAL_PAPER_AUTHORITY_REQUIRED")
        if (
            trial.strategy_id != descriptor.strategy_id
            or trial.strategy_version != descriptor.version
            or trial.strategy_configuration_hash != descriptor.configuration_hash
        ):
            reasons.append("TRIAL_STRATEGY_BINDING_MISMATCH")
        if (
            not trial.limits.paper_execution_policy_id
            or not trial.limits.paper_execution_policy_hash
        ):
            reasons.append("PAPER_EXECUTION_POLICY_REQUIRED")
        permitted = getattr(portfolio_instruction, "permitted_allocation", None)
        instruction_id = getattr(portfolio_instruction, "instruction_id", None)
        if (
            instruction_id != authorization.portfolio_instruction_id
            or permitted is None
            or permitted <= 0
        ):
            reasons.append("AUTHORITY_PERMITTED_PORTFOLIO_ALLOCATION_REQUIRED")
        if not authorization.active_at(now):
            reasons.append("EXTERNAL_PAPER_AUTHORIZATION_INACTIVE")
        if reasons:
            raise ExternalPaperApplicationError(",".join(sorted(set(reasons))))
        return self._repository.save_authorization(authorization)


class SubmitAuthorizedPaperInstruction:
    """Reserve before network I/O; ambiguous transport never retries a submission."""

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        broker: ExternalPaperBrokerPort,
        authority: ExternalPaperAuthorityPort | None = None,
    ) -> None:
        self._repository = repository
        self._broker = broker
        self._authority = authority

    def execute(
        self,
        *,
        instruction_id: str,
        capability: PaperBrokerCapabilitySnapshot,
        now: datetime,
    ) -> ExternalPaperSubmissionResult:
        instruction = self._repository.instruction(instruction_id)
        if instruction is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND")
        if self._repository.binding_for_instruction(instruction_id) is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_PREPARED_BINDING_REQUIRED")
        if instruction.state is not ExternalPaperInstructionState.AUTHORIZED:
            return ExternalPaperSubmissionResult(instruction, None, True)
        authorization = self._repository.authorization(instruction.authorization_id)
        blockers: tuple[str, ...] = () if authorization is not None else ("AUTHORIZATION_MISSING",)
        if authorization is not None and not authorization.active_at(now):
            blockers = ("AUTHORIZATION_INACTIVE",)
        if authorization is not None and self._authority is not None:
            blockers += self._authority.blockers(authorization=authorization, now=now)
        if instruction.side == "BUY" and self._repository.external_paper_killed(
            instruction.campaign_id
        ):
            blockers += ("EXTERNAL_PAPER_KILLED",)
        if instruction.side == "BUY" and self._repository.has_unknown_broker_activity(
            instruction.campaign_id
        ):
            blockers += ("UNKNOWN_BROKER_ACTIVITY_BLOCK",)
        if blockers:
            blocked = self._repository.save_instruction(
                instruction.transition(
                    state=ExternalPaperInstructionState.BLOCKED,
                    reason_code=",".join(sorted(set(blockers))),
                )
            )
            return ExternalPaperSubmissionResult(blocked, None, False)
        if (
            capability.state is not ExternalPaperCapabilityState.READY
            or not capability.order_submit
        ):
            blocked = self._repository.save_instruction(
                instruction.transition(
                    state=ExternalPaperInstructionState.BLOCKED,
                    reason_code="PAPER_BROKER_CAPABILITY_NOT_READY",
                )
            )
            return ExternalPaperSubmissionResult(blocked, None, False)
        existing_attempt = self._repository.submission_attempt(instruction.instruction_id)
        if existing_attempt is not None:
            if existing_attempt.state is ExternalPaperSubmissionState.AMBIGUOUS:
                return ExternalPaperSubmissionResult(instruction, None, True)
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_SUBMISSION_ATTEMPT_EXISTS")
        pending = self._repository.save_instruction(
            instruction.transition(state=ExternalPaperInstructionState.SUBMISSION_PENDING)
        )
        attempt = self._repository.reserve_attempt(
            ExternalPaperSubmissionAttempt.reserve(
                instruction_id=pending.instruction_id,
                ordinal=1,
                reserved_at=now,
                request_hash=pending.canonical_hash,
            )
        )
        try:
            evidence = self._broker.submit(pending)
        except ExternalPaperSubmissionAmbiguous:
            self._repository.conclude_attempt(
                attempt.conclude(
                    state=ExternalPaperSubmissionState.AMBIGUOUS,
                    ambiguity_reason="PAPER_SUBMISSION_AMBIGUOUS",
                )
            )
            return ExternalPaperSubmissionResult(
                self._repository.save_instruction(
                    pending.transition(
                        state=ExternalPaperInstructionState.RECONCILIATION_REQUIRED,
                        reason_code="PAPER_SUBMISSION_AMBIGUOUS",
                    )
                ),
                None,
                False,
            )
        except ExternalPaperBrokerError as error:
            self._repository.conclude_attempt(
                attempt.conclude(state=ExternalPaperSubmissionState.REJECTED)
            )
            return ExternalPaperSubmissionResult(
                self._repository.save_instruction(
                    pending.transition(
                        state=ExternalPaperInstructionState.REJECTED,
                        reason_code=str(error),
                    )
                ),
                None,
                False,
            )
        saved_evidence = self._repository.save_evidence(evidence)
        status = saved_evidence.sanitized_payload.get("status", "accepted")
        broker_order_id = saved_evidence.sanitized_payload.get("id")
        state = (
            ExternalPaperInstructionState.REJECTED
            if status == "rejected"
            else ExternalPaperInstructionState.SUBMITTED
        )
        completed = self._repository.save_instruction(
            pending.transition(state=state, broker_order_id=broker_order_id, reason_code=status)
        )
        self._repository.conclude_attempt(
            attempt.conclude(
                state=ExternalPaperSubmissionState.REJECTED
                if state is ExternalPaperInstructionState.REJECTED
                else ExternalPaperSubmissionState.ACKNOWLEDGED,
                broker_order_id=broker_order_id,
                broker_request_id=saved_evidence.request_id,
            )
        )
        return ExternalPaperSubmissionResult(completed, saved_evidence, False)


class ResolveAmbiguousExternalPaperSubmission:
    """Resolve a retained ambiguous submission from exact broker evidence only."""

    def __init__(
        self, *, repository: ExternalPaperRepository, broker: ExternalPaperAmbiguityRecoveryPort
    ) -> None:
        self._repository = repository
        self._broker = broker

    def execute(self, *, instruction_id: str) -> ExternalPaperInstruction:
        instruction = self._repository.instruction(instruction_id)
        attempt = self._repository.submission_attempt(instruction_id)
        if (
            instruction is None
            or attempt is None
            or instruction.state is not ExternalPaperInstructionState.RECONCILIATION_REQUIRED
            or attempt.state is not ExternalPaperSubmissionState.AMBIGUOUS
        ):
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_AMBIGUITY_RESOLUTION_NOT_ALLOWED")
        evidence = self._repository.save_evidence(
            self._broker.recover_submission(instruction_id=instruction_id)
        )
        broker_order_id = evidence.sanitized_payload.get("id")
        status = evidence.sanitized_payload.get("status")
        target = (
            {
                "accepted": ExternalPaperInstructionState.SUBMITTED,
                "new": ExternalPaperInstructionState.SUBMITTED,
                "partially_filled": ExternalPaperInstructionState.PARTIALLY_FILLED,
                "filled": ExternalPaperInstructionState.FILLED,
                "canceled": ExternalPaperInstructionState.CANCELED,
                "rejected": ExternalPaperInstructionState.REJECTED,
            }.get(status)
            if status is not None
            else None
        )
        if broker_order_id is None or target is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_AMBIGUITY_EVIDENCE_INCOMPLETE")
        self._repository.conclude_attempt(
            attempt.conclude(
                state=ExternalPaperSubmissionState.ACKNOWLEDGED,
                broker_order_id=broker_order_id,
                broker_request_id=evidence.request_id,
            )
        )
        return self._repository.save_instruction(
            instruction.transition(
                state=target, broker_order_id=broker_order_id, reason_code="RECOVERED"
            )
        )


class SyncExternalPaperInstruction:
    """Observe broker state and reconcile it; this never silently changes Apex finance."""

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        broker: ExternalPaperReadPort,
        securities_repository: SecuritiesPaperRepository | None = None,
        allow_execution_binding_transition: bool = True,
    ) -> None:
        self._repository = repository
        self._broker = broker
        self._securities_repository = securities_repository
        self._allow_execution_binding_transition = allow_execution_binding_transition

    def execute(
        self, *, instruction_id: str
    ) -> tuple[ExternalPaperInstruction, ExternalPaperReconciliation]:
        instruction = self._repository.instruction(instruction_id)
        if instruction is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND")
        if instruction.broker_order_id is None:
            report = self._repository.save_reconciliation(
                reconcile_external_paper(instruction=instruction, broker_order=None)
            )
            return instruction, report
        evidence = self._repository.save_evidence(
            self._broker.order(
                instruction_id=instruction.instruction_id,
                broker_order_id=instruction.broker_order_id,
            )
        )
        broker_order = {
            key: value for key, value in evidence.sanitized_payload.items() if value is not None
        }
        broker_order_unmatched = broker_order.get("id") != instruction.broker_order_id
        if broker_order_unmatched:
            if instruction.state in {
                ExternalPaperInstructionState.SUBMITTED,
                ExternalPaperInstructionState.PARTIALLY_FILLED,
            }:
                instruction = self._repository.save_instruction(
                    instruction.transition(
                        state=ExternalPaperInstructionState.RECONCILIATION_REQUIRED,
                        reason_code="BROKER_ORDER_ID_MISMATCH",
                    )
                )
            else:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_BROKER_ORDER_UNMATCHED")
        status = broker_order.get("status")
        state_by_status = {
            "filled": ExternalPaperInstructionState.FILLED,
            "partially_filled": ExternalPaperInstructionState.PARTIALLY_FILLED,
            "canceled": ExternalPaperInstructionState.CANCELED,
            "rejected": ExternalPaperInstructionState.REJECTED,
            "accepted": ExternalPaperInstructionState.SUBMITTED,
            "new": ExternalPaperInstructionState.SUBMITTED,
            "pending_new": ExternalPaperInstructionState.SUBMITTED,
        }
        target = (
            None
            if broker_order_unmatched
            else state_by_status.get(status)
            if status is not None
            else None
        )
        if target is None and not broker_order_unmatched:
            if instruction.state in {
                ExternalPaperInstructionState.SUBMITTED,
                ExternalPaperInstructionState.PARTIALLY_FILLED,
            }:
                instruction = self._repository.save_instruction(
                    instruction.transition(
                        state=ExternalPaperInstructionState.RECONCILIATION_REQUIRED,
                        reason_code="UNKNOWN_BROKER_ORDER_STATE",
                    )
                )
            else:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_BROKER_STATE_UNKNOWN")
        if target is not None and target is not instruction.state:
            instruction = self._repository.save_instruction(instruction.transition(state=target))
        if target is not None and self._allow_execution_binding_transition:
            binding = self._repository.binding_for_instruction(instruction.instruction_id)
            if (
                binding is not None
                and target
                in {
                    ExternalPaperInstructionState.CANCELED,
                    ExternalPaperInstructionState.REJECTED,
                }
                and binding.state
                in {
                    ExternalPaperInternalExecutionBindingState.ORDER_PENDING_EXTERNAL_FILL,
                    ExternalPaperInternalExecutionBindingState.PARTIALLY_APPLIED,
                }
            ):
                pending_broker_fill = False
                if self._securities_repository is not None and target is (
                    ExternalPaperInstructionState.CANCELED
                ):
                    order = self._securities_repository.order(binding.internal_order_id)
                    try:
                        pending_broker_fill = Decimal(str(broker_order.get("filled_qty", "0"))) > (
                            order.filled_quantity.value
                        )
                    except (ArithmeticError, ValueError):
                        # A malformed cumulative broker quantity must not release an
                        # internal Order before retained validation can fail closed.
                        pending_broker_fill = True
                if not pending_broker_fill:
                    self._repository.save_binding(
                        binding.transition(
                            ExternalPaperInternalExecutionBindingState.CANCELED
                            if target is ExternalPaperInstructionState.CANCELED
                            else ExternalPaperInternalExecutionBindingState.REJECTED
                        )
                    )
                    if self._securities_repository is not None:
                        self._securities_repository.cancel_open_order(binding.internal_order_id)
        report = self._repository.save_reconciliation(
            reconcile_external_paper(instruction=instruction, broker_order=broker_order)
        )
        return instruction, report


@dataclass(frozen=True, slots=True)
class AlpacaPaperSynchronizationResult:
    """Read-only synchronization outcome for one exact submitted PAPER instruction."""

    instruction: ExternalPaperInstruction
    broker_order_state: str | None
    cumulative_filled_quantity: str | None
    remaining_quantity: str | None
    average_fill_price: str | None
    order_observation_ids: tuple[str, ...]
    fill_activity_observation_ids: tuple[str, ...]
    position_observation_ids: tuple[str, ...]
    account_observation_ids: tuple[str, ...]
    request_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    next_action: str
    network_reads: int
    broker_writes: bool = False
    financial_mutations: int = 0
    actual_paper_orders_submitted: int = 0
    live_authority: str = "NONE"


class _RetainedOrderReadPort:
    """Pass one already-read broker order into the generic lifecycle service."""

    def __init__(self, evidence: ExternalPaperBrokerEvidence) -> None:
        self._evidence = evidence

    def order(self, *, instruction_id: str, broker_order_id: str) -> ExternalPaperBrokerEvidence:
        if instruction_id != self._evidence.instruction_id:
            raise ExternalPaperBrokerError("PAPER_EXACT_ORDER_MISMATCH")
        return self._evidence

    def cancel(self, *, instruction_id: str, broker_order_id: str) -> ExternalPaperBrokerEvidence:
        raise ExternalPaperBrokerError("PAPER_READ_ONLY_SYNCHRONIZATION")


class SynchronizeAlpacaPaperInstruction:
    """Synchronize one real Alpaca PAPER order without applying any fill or finance.

    Broker I/O occurs only before individual immutable writes.  The existing
    ``SyncExternalPaperInstruction`` owns lifecycle mapping and reconciliation;
    this wrapper only collects the bounded exact-order observation set.
    """

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        broker: AlpacaPaperSynchronizationReadPort,
    ) -> None:
        self._repository = repository
        self._broker = broker

    def execute(self, *, instruction_id: str) -> AlpacaPaperSynchronizationResult:
        if self._broker.identity != ALPACA_PAPER_BROKER_V1:
            raise ExternalPaperApplicationError("ALPACA_PAPER_ADAPTER_REQUIRED")
        instruction = self._repository.instruction(instruction_id)
        if instruction is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND")
        if instruction.broker_order_id is None:
            report = self._repository.save_reconciliation(
                reconcile_external_paper(instruction=instruction, broker_order=None)
            )
            return self._result(
                instruction=instruction,
                broker_order=None,
                evidence=(),
                blockers=("BROKER_ORDER_ID_REQUIRED", report.required_action),
                next_action="RECONCILIATION_REQUIRED",
            )
        if not self._broker.credentials_available:
            return self._result(
                instruction=instruction,
                broker_order=None,
                evidence=(),
                blockers=("PAPER_CREDENTIALS_REQUIRED",),
                next_action="AUTHENTICATION_REJECTED",
            )
        try:
            order = _bind_sync_evidence(
                self._broker.order(
                    instruction_id=instruction.instruction_id,
                    broker_order_id=instruction.broker_order_id,
                ),
                instruction_id=instruction.instruction_id,
            )
            activities = tuple(
                _bind_sync_evidence(item, instruction_id=instruction.instruction_id)
                for item in self._broker.activities()
                if item.sanitized_payload.get("order_id") == instruction.broker_order_id
            )
            position = self._broker.position(symbol=instruction.symbol)
            observations = (
                order,
                *activities,
                *(
                    (_bind_sync_evidence(position, instruction_id=instruction.instruction_id),)
                    if position is not None
                    else ()
                ),
                _bind_sync_evidence(
                    self._broker.account(), instruction_id=instruction.instruction_id
                ),
            )
        except ExternalPaperBrokerError as error:
            blocker = str(error)
            if blocker == "HTTP_404":
                if instruction.state in {
                    ExternalPaperInstructionState.SUBMITTED,
                    ExternalPaperInstructionState.PARTIALLY_FILLED,
                }:
                    instruction = self._repository.save_instruction(
                        instruction.transition(
                            state=ExternalPaperInstructionState.RECONCILIATION_REQUIRED,
                            reason_code="BROKER_ORDER_NOT_FOUND",
                        )
                    )
                self._repository.save_reconciliation(
                    reconcile_external_paper(instruction=instruction, broker_order=None)
                )
            return self._result(
                instruction=instruction,
                broker_order=None,
                evidence=(),
                blockers=(blocker,),
                next_action=(
                    "RECONCILIATION_REQUIRED"
                    if blocker == "HTTP_404"
                    else "AUTHENTICATION_REJECTED"
                    if blocker in {"HTTP_401", "HTTP_403", "PAPER_CREDENTIALS_REQUIRED"}
                    else "PROVIDER_UNAVAILABLE"
                ),
            )
        retained = _retain_unique_sync_evidence(
            repository=self._repository,
            instruction_id=instruction.instruction_id,
            observations=observations,
        )
        retained_order = next(item for item in retained if item.observation_type == "ORDER_STATUS")
        synchronized, reconciliation = SyncExternalPaperInstruction(
            repository=self._repository,
            broker=_RetainedOrderReadPort(retained_order),
            allow_execution_binding_transition=False,
        ).execute(instruction_id=instruction.instruction_id)
        broker_order = {
            key: value for key, value in order.sanitized_payload.items() if value is not None
        }
        has_fill = _positive_decimal(broker_order.get("filled_qty"))
        next_action = (
            "RECONCILIATION_REQUIRED"
            if synchronized.state is ExternalPaperInstructionState.RECONCILIATION_REQUIRED
            else "VALIDATE_RETAINED_FILL"
            if has_fill
            else "WAIT_AND_SYNCHRONIZE_AGAIN"
        )
        blockers = (
            (reconciliation.required_action,) if reconciliation.required_action != "NONE" else ()
        )
        return self._result(
            instruction=synchronized,
            broker_order=broker_order,
            evidence=retained,
            blockers=blockers,
            next_action=next_action,
        )

    def _result(
        self,
        *,
        instruction: ExternalPaperInstruction,
        broker_order: dict[str, str] | None,
        evidence: tuple[ExternalPaperBrokerEvidence, ...],
        blockers: tuple[str, ...],
        next_action: str,
    ) -> AlpacaPaperSynchronizationResult:
        order_ids = tuple(
            item.evidence_id for item in evidence if item.observation_type == "ORDER_STATUS"
        )
        activity_ids = tuple(
            item.evidence_id for item in evidence if item.observation_type == "FILL_ACTIVITY_ITEM"
        )
        position_ids = tuple(
            item.evidence_id
            for item in evidence
            if item.observation_type == "POSITION_SNAPSHOT_ITEM"
        )
        account_ids = tuple(
            item.evidence_id for item in evidence if item.observation_type == "ACCOUNT_READINESS"
        )
        filled = broker_order.get("filled_qty") if broker_order is not None else None
        remaining = None
        if filled is not None:
            try:
                remaining = str(instruction.quantity - Decimal(filled))
            except (ArithmeticError, ValueError):
                remaining = None
        return AlpacaPaperSynchronizationResult(
            instruction=instruction,
            broker_order_state=broker_order.get("status") if broker_order is not None else None,
            cumulative_filled_quantity=filled,
            remaining_quantity=remaining,
            average_fill_price=(broker_order.get("filled_avg_price") if broker_order else None),
            order_observation_ids=order_ids,
            fill_activity_observation_ids=activity_ids,
            position_observation_ids=position_ids,
            account_observation_ids=account_ids,
            request_ids=tuple(
                sorted({item.request_id for item in evidence if item.request_id is not None})
            ),
            blockers=blockers,
            next_action=next_action,
            network_reads=self._broker.network_reads,
            broker_writes=bool(self._broker.network_writes),
        )


def _bind_sync_evidence(
    evidence: ExternalPaperBrokerEvidence, *, instruction_id: str
) -> ExternalPaperBrokerEvidence:
    """Attach a bounded observation to its exact local instruction identity."""
    return ExternalPaperBrokerEvidence.create(
        instruction_id=instruction_id,
        endpoint_identity=evidence.endpoint_identity,
        observation_type=evidence.observation_type,
        observed_at=evidence.observed_at,
        broker_reported_at=evidence.broker_reported_at,
        request_id=evidence.request_id,
        sanitized_payload=evidence.sanitized_payload,
    )


def _positive_decimal(value: str | None) -> bool:
    try:
        return value is not None and Decimal(value) > 0
    except (ArithmeticError, ValueError):
        return False


def _retain_unique_sync_evidence(
    *,
    repository: ExternalPaperRepository,
    instruction_id: str,
    observations: tuple[ExternalPaperBrokerEvidence, ...],
) -> tuple[ExternalPaperBrokerEvidence, ...]:
    """Deduplicate unchanged retained broker snapshots without changing core evidence IDs."""
    existing = repository.evidence_for_instruction(instruction_id)
    retained: list[ExternalPaperBrokerEvidence] = []
    for observation in observations:
        duplicate = next(
            (
                item
                for item in existing
                if item.endpoint_identity == observation.endpoint_identity
                and item.observation_type == observation.observation_type
                and item.broker_reported_at == observation.broker_reported_at
                and item.request_id == observation.request_id
                and item.sanitized_payload == observation.sanitized_payload
            ),
            None,
        )
        saved = duplicate or repository.save_evidence(observation)
        retained.append(saved)
        if duplicate is None:
            existing = (*existing, saved)
    return tuple(retained)


@dataclass(frozen=True, slots=True)
class ExternalPaperFillProcessingResult:
    instruction_id: str
    validated_fill_ids: tuple[str, ...]
    applied_fill_ids: tuple[str, ...]
    blocker: str | None


class ProcessExternalPaperFills:
    """Apply only retained, validated evidence through the shared accounting kernel."""

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        securities_repository: SecuritiesPaperRepository,
    ) -> None:
        self._repository = repository
        self._validator = ValidateExternalPaperFill(repository=repository)
        self._applier = ApplyValidatedExternalPaperFill(
            repository=repository,
            securities_repository=securities_repository,
        )

    def execute(
        self, *, instruction_ids: tuple[str, ...]
    ) -> tuple[ExternalPaperFillProcessingResult, ...]:
        results: list[ExternalPaperFillProcessingResult] = []
        for instruction_id in sorted(set(instruction_ids)):
            instruction = self._repository.instruction(instruction_id)
            if instruction is None:
                results.append(
                    ExternalPaperFillProcessingResult(
                        instruction_id, (), (), "INSTRUCTION_NOT_FOUND"
                    )
                )
                continue
            validated: list[str] = []
            applied: list[str] = []
            blocker: str | None = None
            for evidence in self._repository.evidence_for_instruction(instruction_id):
                payload = evidence.sanitized_payload
                if payload.get("filled_qty") in (None, "0", 0) or payload.get(
                    "filled_avg_price"
                ) in (None, ""):
                    continue
                try:
                    fill = self._validator.execute(evidence=evidence)
                    validated.append(fill.validated_fill_id)
                    if fill.state is ValidatedExternalPaperFillState.VALIDATED:
                        application = self._applier.execute(
                            validated_fill_id=fill.validated_fill_id
                        )
                        applied.append(application.internal_fill_id)
                except (ExternalPaperApplicationError, ValueError) as error:
                    blocker = type(error).__name__
                    break
            results.append(
                ExternalPaperFillProcessingResult(
                    instruction_id, tuple(validated), tuple(applied), blocker
                )
            )
        return tuple(results)


def _genuine_alpaca_evidence(evidence: ExternalPaperBrokerEvidence) -> bool:
    return evidence.endpoint_identity.startswith("PAPER:GET:/v2/")


class ValidateAlpacaPaperFill:
    """Validate real-PAPER evidence only; fixture observations never cross this boundary."""

    def __init__(self, *, repository: ExternalPaperRepository) -> None:
        self._repository = repository
        self._validator = ValidateExternalPaperFill(repository=repository)

    def execute(self, *, instruction_id: str, evidence_id: str) -> ValidatedExternalPaperFill:
        evidence = self._repository.broker_evidence(evidence_id)
        if evidence is None or evidence.instruction_id != instruction_id:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_EVIDENCE_NOT_FOUND")
        if not _genuine_alpaca_evidence(evidence):
            raise ExternalPaperApplicationError("ALPACA_PAPER_GENUINE_EVIDENCE_REQUIRED")
        return self._validator.execute(evidence=evidence)


class ProcessAlpacaPaperFills:
    """Apply validated genuine increments through the shared accounting kernel."""

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        securities_repository: SecuritiesPaperRepository,
    ) -> None:
        self._repository = repository
        self._applier = ApplyValidatedExternalPaperFill(
            repository=repository, securities_repository=securities_repository
        )

    def execute(self, *, instruction_id: str) -> ExternalPaperFillProcessingResult:
        instruction = self._repository.instruction(instruction_id)
        if instruction is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND")
        applied: list[str] = []
        blocker: str | None = None
        validated = self._repository.validated_fills_for_instruction(instruction_id)
        for fill in validated:
            evidence = self._repository.broker_evidence(fill.evidence_id)
            if evidence is None or not _genuine_alpaca_evidence(evidence):
                blocker = "ALPACA_PAPER_GENUINE_EVIDENCE_REQUIRED"
                break
            if fill.state is not ValidatedExternalPaperFillState.VALIDATED:
                continue
            if self._repository.fill_application(fill.validated_fill_id) is not None:
                continue
            try:
                application = self._applier.execute(validated_fill_id=fill.validated_fill_id)
                applied.append(application.internal_fill_id)
            except (ExternalPaperApplicationError, ValueError) as error:
                blocker = type(error).__name__
                break
        return ExternalPaperFillProcessingResult(
            instruction_id=instruction_id,
            validated_fill_ids=tuple(fill.validated_fill_id for fill in validated),
            applied_fill_ids=tuple(applied),
            blocker=blocker,
        )


@dataclass(frozen=True, slots=True)
class ExternalPaperSynchronizationResult:
    instruction_id: str
    state: ExternalPaperInstructionState | None
    reconciliation_id: str | None
    blocker: str | None


class SynchronizeExternalPaperCampaign:
    """Bounded campaign orchestration over exact retained instruction identities."""

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        broker: ExternalPaperReadPort,
        securities_repository: SecuritiesPaperRepository | None = None,
    ) -> None:
        self._repository = repository
        self._broker = broker
        self._sync = SyncExternalPaperInstruction(
            repository=repository,
            broker=broker,
            securities_repository=securities_repository,
        )

    def execute(self, *, campaign_id: str) -> tuple[ExternalPaperSynchronizationResult, ...]:
        results: list[ExternalPaperSynchronizationResult] = []
        instructions = self._repository.instructions_for_campaign(campaign_id)
        by_broker_order = {
            instruction.broker_order_id: instruction.instruction_id
            for instruction in instructions
            if instruction.broker_order_id is not None
        }
        if isinstance(self._broker, ExternalPaperCampaignReadPort):
            self._retain_campaign_observations(
                campaign_id=campaign_id,
                instructions=instructions,
                by_broker_order=by_broker_order,
            )
        for instruction in instructions:
            try:
                synchronized, reconciliation = self._sync.execute(
                    instruction_id=instruction.instruction_id
                )
                results.append(
                    ExternalPaperSynchronizationResult(
                        instruction.instruction_id,
                        synchronized.state,
                        reconciliation.reconciliation_id,
                        None,
                    )
                )
            except ExternalPaperApplicationError as error:
                results.append(
                    ExternalPaperSynchronizationResult(
                        instruction.instruction_id, None, None, type(error).__name__
                    )
                )
        return tuple(results)

    def _retain_campaign_observations(
        self,
        *,
        campaign_id: str,
        instructions: tuple[ExternalPaperInstruction, ...],
        by_broker_order: dict[str, str],
    ) -> None:
        broker = self._broker
        assert isinstance(broker, ExternalPaperCampaignReadPort)
        evidence = (broker.account(), *broker.orders(), *broker.positions(), *broker.activities())
        symbols = {instruction.symbol: instruction.instruction_id for instruction in instructions}
        for item in evidence:
            order_id = item.sanitized_payload.get("order_id") or item.sanitized_payload.get("id")
            known_instruction = by_broker_order.get(order_id or "")
            if known_instruction is None and item.observation_type == "POSITION_SNAPSHOT_ITEM":
                known_instruction = symbols.get(item.sanitized_payload.get("symbol") or "")
            unknown_activity = (
                item.observation_type == "FILL_ACTIVITY_ITEM" and known_instruction is None
            )
            self._repository.save_evidence(item)
            self._repository.save_campaign_observation(
                ExternalPaperCampaignObservation.create(
                    campaign_id=campaign_id,
                    evidence=item,
                    known_instruction_ids=(known_instruction,) if known_instruction else (),
                    unknown_broker_activity=unknown_activity,
                )
            )


class CancelExternalPaperInstruction:
    def __init__(
        self, *, repository: ExternalPaperRepository, broker: ExternalPaperReadPort
    ) -> None:
        self._repository = repository
        self._broker = broker

    def execute(self, *, instruction_id: str) -> ExternalPaperBrokerEvidence:
        instruction = self._repository.instruction(instruction_id)
        if instruction is None or instruction.broker_order_id is None:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_OPEN_BROKER_ORDER_REQUIRED")
        existing = next(
            (
                evidence
                for evidence in self._repository.evidence_for_instruction(instruction_id)
                if evidence.observation_type == "ORDER_CANCELLATION"
            ),
            None,
        )
        if existing is not None:
            return existing
        if instruction.state not in {
            ExternalPaperInstructionState.SUBMITTED,
            ExternalPaperInstructionState.PARTIALLY_FILLED,
        }:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_CANCEL_NOT_ALLOWED")
        return self._repository.save_evidence(
            self._broker.cancel(
                instruction_id=instruction.instruction_id,
                broker_order_id=instruction.broker_order_id,
            )
        )


@dataclass(frozen=True, slots=True)
class ExternalPaperFlattenResult:
    campaign_id: str
    canceled_instruction_ids: tuple[str, ...]
    synchronized_instruction_ids: tuple[str, ...]
    applied_fill_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    completed: bool


def external_paper_flatten_confirmation(*, campaign_id: str) -> str:
    return f"FLATTEN_EXTERNAL_PAPER:{campaign_id}"


class FlattenExternalPaperCampaign:
    """Kill new openings and safely drain outstanding external opening instructions.

    This service intentionally does not invent a closing instruction: a close must
    be separately prepared from an exact reconciled Position and authority chain.
    It therefore cannot report completion while any Position remains open.
    """

    def __init__(
        self,
        *,
        repository: ExternalPaperRepository,
        broker: ExternalPaperReadPort,
        securities_repository: SecuritiesPaperRepository,
    ) -> None:
        self._repository = repository
        self._securities_repository = securities_repository
        self._cancel = CancelExternalPaperInstruction(repository=repository, broker=broker)
        self._sync = SyncExternalPaperInstruction(
            repository=repository,
            broker=broker,
            securities_repository=securities_repository,
        )
        self._process = ProcessExternalPaperFills(
            repository=repository,
            securities_repository=securities_repository,
        )

    def execute(
        self,
        *,
        campaign_id: str,
        reason: str,
        confirmation_text: str,
        operator_action_id: str,
    ) -> ExternalPaperFlattenResult:
        if confirmation_text != external_paper_flatten_confirmation(campaign_id=campaign_id):
            raise ExternalPaperApplicationError("EXACT_OPERATOR_CONFIRMATION_REQUIRED")
        KillExternalPaper(repository=self._repository).execute(
            campaign_id=campaign_id,
            reason=reason,
            confirmation_text=external_paper_kill_confirmation(campaign_id=campaign_id),
            operator_action_id=operator_action_id,
        )
        canceled: list[str] = []
        synchronized: list[str] = []
        blockers: list[str] = []
        for instruction in self._repository.instructions_for_campaign(campaign_id):
            if instruction.side != "BUY":
                continue
            if instruction.state in {
                ExternalPaperInstructionState.SUBMITTED,
                ExternalPaperInstructionState.PARTIALLY_FILLED,
            }:
                try:
                    self._cancel.execute(instruction_id=instruction.instruction_id)
                    canceled.append(instruction.instruction_id)
                except ExternalPaperApplicationError as error:
                    blockers.append(str(error))
            current = self._repository.instruction(instruction.instruction_id)
            if current is not None and current.broker_order_id is not None:
                try:
                    self._sync.execute(instruction_id=current.instruction_id)
                    synchronized.append(current.instruction_id)
                except ExternalPaperApplicationError as error:
                    blockers.append(str(error))
        processed = self._process.execute(
            instruction_ids=tuple(
                item.instruction_id
                for item in self._repository.instructions_for_campaign(campaign_id)
            )
        )
        applied = tuple(fill_id for result in processed for fill_id in result.applied_fill_ids)
        pending = tuple(
            item.instruction_id
            for item in self._repository.instructions_for_campaign(campaign_id)
            if item.side == "BUY"
            and item.state
            in {
                ExternalPaperInstructionState.AUTHORIZED,
                ExternalPaperInstructionState.SUBMISSION_PENDING,
                ExternalPaperInstructionState.SUBMITTED,
                ExternalPaperInstructionState.PARTIALLY_FILLED,
                ExternalPaperInstructionState.RECONCILIATION_REQUIRED,
            }
        )
        open_positions = tuple(
            position.position_id
            for position in self._securities_repository.positions(campaign_id)
            if position.open_quantity.value > 0
        )
        return ExternalPaperFlattenResult(
            campaign_id,
            tuple(canceled),
            tuple(synchronized),
            applied,
            tuple(
                sorted(
                    set(
                        (
                            *blockers,
                            *(f"PENDING:{item}" for item in pending),
                            *(f"OPEN_POSITION:{item}" for item in open_positions),
                        )
                    )
                )
            ),
            not pending and not blockers and not open_positions,
        )


class InMemoryExternalPaperRepository:
    def __init__(self) -> None:
        self.capabilities: dict[str, PaperBrokerCapabilitySnapshot] = {}
        self.authorizations: dict[str, ExternalPaperAuthorization] = {}
        self.instructions: dict[str, ExternalPaperInstruction] = {}
        self.keys: dict[str, str] = {}
        self.evidence: dict[str, ExternalPaperBrokerEvidence] = {}
        self.attempts: dict[str, ExternalPaperSubmissionAttempt] = {}
        self.killed_campaigns: set[str] = set()
        self.reconciliations: dict[str, ExternalPaperReconciliation] = {}
        self.validated_fills: dict[str, ValidatedExternalPaperFill] = {}
        self.fill_applications: dict[str, ExternalPaperFillApplication] = {}
        self.bindings: dict[str, ExternalPaperInternalExecutionBinding] = {}
        self.campaign_observations: dict[str, ExternalPaperCampaignObservation] = {}
        self.acceptances: dict[str, AlpacaPaperAcceptanceCampaign] = {}
        self.acceptance_keys: dict[str, str] = {}
        self.acceptance_preflights: dict[str, list[AlpacaPaperAcceptancePreflight]] = {}

    def acceptance_by_key(self, idempotency_key: str) -> AlpacaPaperAcceptanceCampaign | None:
        identifier = self.acceptance_keys.get(idempotency_key)
        return self.acceptances.get(identifier) if identifier is not None else None

    def acceptance(self, acceptance_id: str) -> AlpacaPaperAcceptanceCampaign | None:
        return self.acceptances.get(acceptance_id)

    def acceptance_for_campaign(self, campaign_id: str) -> AlpacaPaperAcceptanceCampaign | None:
        return next(
            (item for item in self.acceptances.values() if item.campaign_id == campaign_id), None
        )

    def acceptance_for_instruction(
        self, instruction_id: str
    ) -> AlpacaPaperAcceptanceCampaign | None:
        return next(
            (item for item in self.acceptances.values() if item.instruction_id == instruction_id),
            None,
        )

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
        self.acceptances[acceptance.acceptance_id] = acceptance
        self.acceptance_keys[acceptance.idempotency_key] = acceptance.acceptance_id
        return acceptance

    def save_acceptance_preflight(
        self, preflight: AlpacaPaperAcceptancePreflight
    ) -> AlpacaPaperAcceptancePreflight:
        values = self.acceptance_preflights.setdefault(preflight.instruction_id, [])
        if values and values[-1].canonical_hash == preflight.canonical_hash:
            return values[-1]
        values.append(preflight)
        return preflight

    def latest_acceptance_preflight(
        self, instruction_id: str
    ) -> AlpacaPaperAcceptancePreflight | None:
        values = self.acceptance_preflights.get(instruction_id, [])
        return values[-1] if values else None

    def save_capability_snapshot(
        self, snapshot: PaperBrokerCapabilitySnapshot
    ) -> PaperBrokerCapabilitySnapshot:
        existing = self.capabilities.get(snapshot.snapshot_id)
        if existing is not None and existing.canonical_hash != snapshot.canonical_hash:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_CAPABILITY_CONFLICT")
        self.capabilities[snapshot.snapshot_id] = snapshot
        return snapshot

    def authorization(self, authorization_id: str) -> ExternalPaperAuthorization | None:
        return self.authorizations.get(authorization_id)

    def save_authorization(
        self, authorization: ExternalPaperAuthorization
    ) -> ExternalPaperAuthorization:
        old = self.authorizations.get(authorization.authorization_id)
        if old is not None and old.canonical_hash != authorization.canonical_hash:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_AUTHORIZATION_CONFLICT")
        self.authorizations[authorization.authorization_id] = old or authorization
        return self.authorizations[authorization.authorization_id]

    def instruction(self, instruction_id: str) -> ExternalPaperInstruction | None:
        return self.instructions.get(instruction_id)

    def instruction_by_key(self, key: str) -> ExternalPaperInstruction | None:
        identifier = self.keys.get(key)
        return self.instructions.get(identifier) if identifier is not None else None

    def instructions_for_campaign(self, campaign_id: str) -> tuple[ExternalPaperInstruction, ...]:
        return tuple(
            sorted(
                (item for item in self.instructions.values() if item.campaign_id == campaign_id),
                key=lambda item: item.instruction_id,
            )
        )

    def save_instruction(self, instruction: ExternalPaperInstruction) -> ExternalPaperInstruction:
        old = self.instruction_by_key(instruction.idempotency_key)
        if old is not None and old.canonical_hash != instruction.canonical_hash:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_IDEMPOTENCY_CONFLICT")
        self.instructions[instruction.instruction_id] = instruction
        self.keys[instruction.idempotency_key] = instruction.instruction_id
        return instruction

    def save_evidence(self, evidence: ExternalPaperBrokerEvidence) -> ExternalPaperBrokerEvidence:
        old = self.evidence.get(evidence.evidence_id)
        if old is not None and old.canonical_hash != evidence.canonical_hash:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_EVIDENCE_CONFLICT")
        self.evidence[evidence.evidence_id] = evidence
        return evidence

    def broker_evidence(self, evidence_id: str) -> ExternalPaperBrokerEvidence | None:
        return self.evidence.get(evidence_id)

    def save_campaign_observation(
        self, observation: ExternalPaperCampaignObservation
    ) -> ExternalPaperCampaignObservation:
        old = self.campaign_observations.get(observation.observation_id)
        if old is not None and old.canonical_hash != observation.canonical_hash:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_CAMPAIGN_OBSERVATION_CONFLICT")
        self.campaign_observations[observation.observation_id] = old or observation
        return self.campaign_observations[observation.observation_id]

    def has_unknown_broker_activity(self, campaign_id: str) -> bool:
        return any(
            item.campaign_id == campaign_id and item.unknown_broker_activity
            for item in self.campaign_observations.values()
        )

    def evidence_for_instruction(
        self, instruction_id: str
    ) -> tuple[ExternalPaperBrokerEvidence, ...]:
        return tuple(
            sorted(
                (item for item in self.evidence.values() if item.instruction_id == instruction_id),
                key=lambda item: (item.observed_at, item.evidence_id),
            )
        )

    def reserve_attempt(
        self, attempt: ExternalPaperSubmissionAttempt
    ) -> ExternalPaperSubmissionAttempt:
        old = self.attempts.get(attempt.instruction_id)
        if old is not None and old.canonical_hash != attempt.canonical_hash:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_SUBMISSION_ATTEMPT_CONFLICT")
        self.attempts[attempt.instruction_id] = old or attempt
        return self.attempts[attempt.instruction_id]

    def conclude_attempt(
        self, attempt: ExternalPaperSubmissionAttempt
    ) -> ExternalPaperSubmissionAttempt:
        old = self.attempts.get(attempt.instruction_id)
        if old is None or old.attempt_id != attempt.attempt_id:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_SUBMISSION_ATTEMPT_NOT_FOUND")
        self.attempts[attempt.instruction_id] = attempt
        return attempt

    def submission_attempt(self, instruction_id: str) -> ExternalPaperSubmissionAttempt | None:
        return self.attempts.get(instruction_id)

    def external_paper_killed(self, campaign_id: str) -> bool:
        return campaign_id in self.killed_campaigns

    def kill(self, *, campaign_id: str, reason: str, operator_action_id: str) -> None:
        if not reason or not operator_action_id:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_KILL_INPUT_REQUIRED")
        self.killed_campaigns.add(campaign_id)

    def save_reconciliation(
        self, report: ExternalPaperReconciliation
    ) -> ExternalPaperReconciliation:
        old = self.reconciliations.get(report.reconciliation_id)
        if old is not None and old.canonical_hash != report.canonical_hash:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_RECONCILIATION_CONFLICT")
        self.reconciliations[report.reconciliation_id] = old or report
        return self.reconciliations[report.reconciliation_id]

    def reconciliations_for_instruction(
        self, instruction_id: str
    ) -> tuple[ExternalPaperReconciliation, ...]:
        return tuple(
            item for item in self.reconciliations.values() if item.instruction_id == instruction_id
        )

    def validated_fill_for_evidence(self, evidence_id: str) -> ValidatedExternalPaperFill | None:
        return next(
            (item for item in self.validated_fills.values() if item.evidence_id == evidence_id),
            None,
        )

    def validated_fill(self, validated_fill_id: str) -> ValidatedExternalPaperFill | None:
        return self.validated_fills.get(validated_fill_id)

    def validated_fills_for_instruction(
        self, instruction_id: str
    ) -> tuple[ValidatedExternalPaperFill, ...]:
        return tuple(
            item for item in self.validated_fills.values() if item.instruction_id == instruction_id
        )

    def save_validated_fill(self, fill: ValidatedExternalPaperFill) -> ValidatedExternalPaperFill:
        old = self.validated_fills.get(fill.validated_fill_id)
        if old is not None and old.canonical_hash != fill.canonical_hash:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_VALIDATED_FILL_CONFLICT")
        self.validated_fills[fill.validated_fill_id] = old or fill
        return self.validated_fills[fill.validated_fill_id]

    def fill_application(self, validated_fill_id: str) -> ExternalPaperFillApplication | None:
        return self.fill_applications.get(validated_fill_id)

    def save_fill_application(
        self, application: ExternalPaperFillApplication
    ) -> ExternalPaperFillApplication:
        old = self.fill_applications.get(application.validated_fill_id)
        if old is not None and old.canonical_hash != application.canonical_hash:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_FILL_APPLICATION_CONFLICT")
        self.fill_applications[application.validated_fill_id] = old or application
        return self.fill_applications[application.validated_fill_id]

    def binding_for_instruction(
        self, instruction_id: str
    ) -> ExternalPaperInternalExecutionBinding | None:
        return self.bindings.get(instruction_id)

    def save_binding(
        self, binding: ExternalPaperInternalExecutionBinding
    ) -> ExternalPaperInternalExecutionBinding:
        old = self.bindings.get(binding.instruction_id)
        if old is not None and old.canonical_hash != binding.canonical_hash:
            raise ExternalPaperApplicationError("EXTERNAL_PAPER_INTERNAL_BINDING_CONFLICT")
        self.bindings[binding.instruction_id] = binding
        return self.bindings[binding.instruction_id]


def ready_capability(now: datetime) -> PaperBrokerCapabilitySnapshot:
    """Fixture-only ready state; production readiness must come from the adapter."""
    return PaperBrokerCapabilitySnapshot.create(
        state=ExternalPaperCapabilityState.READY,
        credentials_available=True,
        authentication_state="FIXTURE",
        account_read=True,
        order_submit=True,
        order_read=True,
        cancel=True,
        position_read=True,
        activity_read=True,
        checked_at=now,
        reason_codes=("FIXTURE_ONLY",),
    )
