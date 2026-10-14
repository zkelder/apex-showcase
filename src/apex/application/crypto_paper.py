"""Adapter-free orchestration for internal Crypto Spot PAPER operations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from apex.domain.crypto_paper import (
    CryptoPaperAccount,
    CryptoPaperAccountState,
    CryptoPaperAuthorization,
    CryptoPaperCancellation,
    CryptoPaperCapability,
    CryptoPaperCloseAccountingCorrection,
    CryptoPaperEligibility,
    CryptoPaperEligibilityScan,
    CryptoPaperError,
    CryptoPaperFill,
    CryptoPaperFillApplication,
    CryptoPaperJournal,
    CryptoPaperOperation,
    CryptoPaperOrder,
    CryptoPaperOrderIntent,
    CryptoPaperPreview,
    CryptoPaperProviderContract,
    CryptoPaperReconciliation,
    CryptoPaperReservation,
    CryptoPaperState,
    PaperOrderState,
    PaperOrderType,
    PaperSide,
    RetainedPaperPriceEvidence,
    apply_retained_evidence,
    build_close_accounting_correction,
    build_paper_preview,
    cancel_paper_order,
    close_accounting_correction_journal_id,
    construct_order_intent,
    expire_paper_order,
    ledger_contract_discrepancies,
    primary_fill_journal_id,
    reconcile_paper_state,
    submit_paper_order,
)
from apex.domain.values import DecimalInput, parse_decimal


class CryptoPaperRepository(Protocol):
    def contract(self, contract_id: str) -> CryptoPaperProviderContract | None: ...

    def save_contract(
        self,
        *,
        contract: CryptoPaperProviderContract,
        idempotency_key: str,
    ) -> bool: ...

    def capability(self, capability_id: str) -> CryptoPaperCapability | None: ...

    def save_capability(
        self,
        *,
        capability: CryptoPaperCapability,
        idempotency_key: str,
    ) -> bool: ...

    def account(self, account_id: str) -> CryptoPaperAccount | None: ...

    def state(self, account_id: str) -> CryptoPaperState | None: ...

    def save_account(
        self,
        *,
        account: CryptoPaperAccount,
        state: CryptoPaperState,
        idempotency_key: str,
    ) -> bool: ...

    def save_state(
        self,
        *,
        state: CryptoPaperState,
        prior_state_hash: str,
        operation: CryptoPaperOperation,
    ) -> bool: ...

    def scan(self, scan_id: str) -> CryptoPaperEligibilityScan | None: ...

    def scan_decisions(self, scan_id: str) -> tuple[CryptoPaperEligibility, ...]: ...

    def save_scan(
        self,
        *,
        scan: CryptoPaperEligibilityScan,
        decisions: tuple[CryptoPaperEligibility, ...],
        idempotency_key: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class CryptoPaperResult[T]:
    value: T
    idempotent_replay: bool
    provider_requests: int = 0
    genuine_financial_mutation: bool = False
    paper_authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"


def _operation(
    *, key: str, operation_type: str, result_id: str, result_hash: str
) -> CryptoPaperOperation:
    return CryptoPaperOperation.create(
        idempotency_key=key,
        operation_type=operation_type,
        result_id=result_id,
        result_hash=result_hash,
    )


def _prior_operation(state: CryptoPaperState, key: str) -> CryptoPaperOperation | None:
    return next((item for item in state.operations if item.idempotency_key == key), None)


def _with_operation(state: CryptoPaperState, operation: CryptoPaperOperation) -> CryptoPaperState:
    return replace(state, operations=(*state.operations, operation)).sealed()


def _one[T](values: tuple[T, ...], predicate: object, reason: str) -> T:
    if not callable(predicate):
        raise CryptoPaperError(reason)
    found = tuple(item for item in values if predicate(item))
    if len(found) != 1:
        raise CryptoPaperError(reason)
    return found[0]


def _replace[T](
    values: tuple[T, ...],
    *,
    predicate: object,
    replacement: T,
    reason: str,
) -> tuple[T, ...]:
    if not callable(predicate):
        raise CryptoPaperError(reason)
    matched = False
    result: list[T] = []
    for item in values:
        if predicate(item):
            if matched:
                raise CryptoPaperError(reason)
            matched = True
            result.append(replacement)
        else:
            result.append(item)
    if not matched:
        raise CryptoPaperError(reason)
    return tuple(result)


class RegisterCryptoPaperProviderContract:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        contract: CryptoPaperProviderContract,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperProviderContract]:
        replay = self._repository.save_contract(
            contract=contract,
            idempotency_key=idempotency_key,
        )
        return CryptoPaperResult(contract, replay)


class RetainCryptoPaperCapability:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        capability: CryptoPaperCapability,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperCapability]:
        replay = self._repository.save_capability(
            capability=capability,
            idempotency_key=idempotency_key,
        )
        return CryptoPaperResult(capability, replay)


class CreateCryptoPaperAccount:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        opening_cash: DecimalInput,
        created_at: datetime,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperState]:
        account = CryptoPaperAccount.create(
            account_id=account_id,
            opening_cash=opening_cash,
            created_at=created_at,
        )
        state = CryptoPaperState(
            account=account,
            account_state=CryptoPaperAccountState.opening(
                account=account,
                event_id=f"{account_id}-created",
            ),
        ).sealed()
        replay = self._repository.save_account(
            account=account,
            state=state,
            idempotency_key=idempotency_key,
        )
        retained = self._repository.state(account_id)
        if retained is None:
            raise CryptoPaperError("CRYPTO_PAPER_ACCOUNT_PERSISTENCE_FAILED")
        return CryptoPaperResult(retained, replay)


class RetainCryptoPaperEligibilityScan:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        scan: CryptoPaperEligibilityScan,
        decisions: tuple[CryptoPaperEligibility, ...],
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperEligibilityScan]:
        if scan.eligible_count != sum(item.state.value == "ELIGIBLE" for item in decisions):
            raise CryptoPaperError("CRYPTO_PAPER_SCAN_DECISION_MISMATCH")
        replay = self._repository.save_scan(
            scan=scan,
            decisions=decisions,
            idempotency_key=idempotency_key,
        )
        return CryptoPaperResult(scan, replay)


class AuthorizeCryptoPaperOpportunity:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        authorization: CryptoPaperAuthorization,
    ) -> CryptoPaperResult[CryptoPaperAuthorization]:
        state = _required_state(self._repository, account_id)
        prior = _prior_operation(state, authorization.idempotency_key)
        retained = next(
            (
                item
                for item in state.authorizations
                if item.authorization_id == authorization.authorization_id
            ),
            None,
        )
        if prior is not None:
            if (
                retained is None
                or prior.result_id != authorization.authorization_id
                or prior.result_hash != authorization.canonical_hash
            ):
                raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
            return CryptoPaperResult(retained, True)
        if retained is not None:
            raise CryptoPaperError("CRYPTO_PAPER_AUTHORIZATION_IDENTITY_CONFLICT")
        operation = _operation(
            key=authorization.idempotency_key,
            operation_type="AUTHORIZE",
            result_id=authorization.authorization_id,
            result_hash=authorization.canonical_hash,
        )
        next_state = _with_operation(
            replace(state, authorizations=(*state.authorizations, authorization)),
            operation,
        )
        replay = self._repository.save_state(
            state=next_state,
            prior_state_hash=state.canonical_hash,
            operation=operation,
        )
        return CryptoPaperResult(authorization, replay)


class ConstructCryptoPaperOrderIntent:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        intent_id: str,
        authorization_id: str,
        contract_id: str,
        side: PaperSide,
        order_type: PaperOrderType,
        quote_size: DecimalInput | None,
        base_size: DecimalInput | None,
        limit_price: DecimalInput | None,
        created_at: datetime,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperOrderIntent]:
        state = _required_state(self._repository, account_id)
        replay = _prior_operation(state, idempotency_key)
        retained = next((item for item in state.intents if item.intent_id == intent_id), None)
        if replay is not None:
            if retained is None or replay.result_id != intent_id:
                raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
            return CryptoPaperResult(retained, True)
        authorization = _one(
            state.authorizations,
            lambda item: item.authorization_id == authorization_id,
            "CRYPTO_PAPER_AUTHORIZATION_NOT_FOUND",
        )
        contract = self._repository.contract(contract_id)
        if contract is None:
            raise CryptoPaperError("CRYPTO_PAPER_CONTRACT_NOT_FOUND")
        intent = construct_order_intent(
            intent_id=intent_id,
            authorization=authorization,
            contract=contract,
            account_state=state.account_state,
            side=side,
            order_type=order_type,
            quote_size=quote_size,
            base_size=base_size,
            limit_price=limit_price,
            created_at=created_at,
        )
        if retained is not None:
            raise CryptoPaperError("CRYPTO_PAPER_INTENT_IDENTITY_CONFLICT")
        operation = _operation(
            key=idempotency_key,
            operation_type="CONSTRUCT_INTENT",
            result_id=intent.intent_id,
            result_hash=intent.canonical_hash,
        )
        next_state = _with_operation(
            replace(state, intents=(*state.intents, intent)),
            operation,
        )
        self._repository.save_state(
            state=next_state,
            prior_state_hash=state.canonical_hash,
            operation=operation,
        )
        return CryptoPaperResult(intent, False)


class PreviewCryptoPaperOrder:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        preview_id: str,
        intent_id: str,
        evidence: RetainedPaperPriceEvidence,
        fee_bps: DecimalInput,
        spread_bps: DecimalInput,
        slippage_bps: DecimalInput,
        created_at: datetime,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperPreview]:
        state = _required_state(self._repository, account_id)
        replay = _prior_operation(state, idempotency_key)
        retained = next(
            (item for item in state.previews if item.preview_id == preview_id),
            None,
        )
        if replay is not None:
            if retained is None or replay.result_id != preview_id:
                raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
            return CryptoPaperResult(retained, True)
        intent = _one(
            state.intents,
            lambda item: item.intent_id == intent_id,
            "CRYPTO_PAPER_INTENT_NOT_FOUND",
        )
        contract = self._repository.contract(intent.contract_id)
        if contract is None:
            raise CryptoPaperError("CRYPTO_PAPER_CONTRACT_NOT_FOUND")
        authorization = _one(
            state.authorizations,
            lambda item: item.authorization_id == intent.authorization_id,
            "CRYPTO_PAPER_AUTHORIZATION_NOT_FOUND",
        )
        preview = build_paper_preview(
            preview_id=preview_id,
            intent=intent,
            authorization=authorization,
            contract=contract,
            account_state=state.account_state,
            evidence=evidence,
            fee_bps=fee_bps,
            spread_bps=spread_bps,
            slippage_bps=slippage_bps,
            created_at=created_at,
        )
        operation = _operation(
            key=idempotency_key,
            operation_type="PREVIEW",
            result_id=preview.preview_id,
            result_hash=preview.canonical_hash,
        )
        next_state = _with_operation(
            replace(
                state,
                evidence=(*state.evidence, evidence),
                previews=(*state.previews, preview),
            ),
            operation,
        )
        self._repository.save_state(
            state=next_state,
            prior_state_hash=state.canonical_hash,
            operation=operation,
        )
        return CryptoPaperResult(preview, False)


@dataclass(frozen=True, slots=True)
class CryptoPaperSubmissionResult:
    order: CryptoPaperOrder
    reservation: CryptoPaperReservation
    account_state: CryptoPaperAccountState


class SubmitCryptoPaperOrder:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        order_id: str,
        reservation_id: str,
        preview_id: str,
        submitted_at: datetime,
        valid_until: datetime,
        actor: str,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperSubmissionResult]:
        state = _required_state(self._repository, account_id)
        replay = _prior_operation(state, idempotency_key)
        retained_order = next(
            (item for item in state.orders if item.order_id == order_id),
            None,
        )
        if replay is not None:
            if retained_order is None or replay.result_id != order_id:
                raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
            reservation = _one(
                state.reservations,
                lambda item: item.order_id == order_id,
                "CRYPTO_PAPER_RESERVATION_NOT_FOUND",
            )
            return CryptoPaperResult(
                CryptoPaperSubmissionResult(
                    retained_order,
                    reservation,
                    state.account_state,
                ),
                True,
            )
        preview = _one(
            state.previews,
            lambda item: item.preview_id == preview_id,
            "CRYPTO_PAPER_PREVIEW_NOT_FOUND",
        )
        intent = _one(
            state.intents,
            lambda item: item.intent_id == preview.intent_id,
            "CRYPTO_PAPER_INTENT_NOT_FOUND",
        )
        contract = self._repository.contract(intent.contract_id)
        if contract is None:
            raise CryptoPaperError("CRYPTO_PAPER_CONTRACT_NOT_FOUND")
        authorization = _one(
            state.authorizations,
            lambda item: item.authorization_id == intent.authorization_id,
            "CRYPTO_PAPER_AUTHORIZATION_NOT_FOUND",
        )
        submission = submit_paper_order(
            order_id=order_id,
            reservation_id=reservation_id,
            preview=preview,
            intent=intent,
            authorization=authorization,
            contract=contract,
            account_state=state.account_state,
            open_orders=state.orders,
            submitted_at=submitted_at,
            valid_until=valid_until,
            actor=actor,
        )
        operation = _operation(
            key=idempotency_key,
            operation_type="SUBMIT",
            result_id=submission.order.order_id,
            result_hash=submission.order.canonical_hash,
        )
        next_state = _with_operation(
            replace(
                state,
                account_state=submission.account_state,
                orders=(*state.orders, submission.order),
                reservations=(*state.reservations, submission.reservation),
                events=(*state.events, *submission.events),
            ),
            operation,
        )
        self._repository.save_state(
            state=next_state,
            prior_state_hash=state.canonical_hash,
            operation=operation,
        )
        return CryptoPaperResult(
            CryptoPaperSubmissionResult(
                submission.order,
                submission.reservation,
                submission.account_state,
            ),
            False,
        )


class ApplyCryptoPaperMarketEvidence:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        order_id: str,
        fill_id: str,
        evidence: RetainedPaperPriceEvidence,
        fee_bps: DecimalInput,
        slippage_bps: DecimalInput,
        actor: str,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperFillApplication | None]:
        state = _required_state(self._repository, account_id)
        replay = _prior_operation(state, idempotency_key)
        retained_fill = next(
            (item for item in state.fills if item.fill_id == fill_id),
            None,
        )
        if replay is not None:
            if replay.result_id == f"{order_id}-no-fill":
                return CryptoPaperResult(None, True)
            if retained_fill is None or replay.result_id != fill_id:
                raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
            return CryptoPaperResult(
                _retained_fill_application(state, retained_fill),
                True,
            )
        if retained_fill is not None:
            raise CryptoPaperError("CRYPTO_PAPER_DUPLICATE_FILL")
        order = _one(
            state.orders,
            lambda item: item.order_id == order_id,
            "CRYPTO_PAPER_ORDER_NOT_FOUND",
        )
        reservation = _one(
            state.reservations,
            lambda item: item.order_id == order_id,
            "CRYPTO_PAPER_RESERVATION_NOT_FOUND",
        )
        position = next(
            (item for item in state.positions if item.product_id == order.product_id),
            None,
        )
        intent = _one(
            state.intents,
            lambda item: item.intent_id == order.intent_id,
            "CRYPTO_PAPER_INTENT_NOT_FOUND",
        )
        contract = self._repository.contract(intent.contract_id)
        if contract is None:
            raise CryptoPaperError("CRYPTO_PAPER_CONTRACT_NOT_FOUND")
        applied = apply_retained_evidence(
            fill_id=fill_id,
            order=order,
            contract=contract,
            reservation=reservation,
            account_state=state.account_state,
            position=position,
            evidence=evidence,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            actor=actor,
            prior_fills=state.fills,
        )
        result_id = fill_id if applied is not None else f"{order_id}-no-fill"
        result_hash = evidence.canonical_hash if applied is None else applied.fill.canonical_hash
        operation = _operation(
            key=idempotency_key,
            operation_type="APPLY_EVIDENCE",
            result_id=result_id,
            result_hash=result_hash,
        )
        if applied is None:
            next_state = _with_operation(
                replace(state, evidence=(*state.evidence, evidence)),
                operation,
            )
        else:
            positions = (
                (*state.positions, applied.position)
                if position is None
                else _replace(
                    state.positions,
                    predicate=lambda item: item.position_id == position.position_id,
                    replacement=applied.position,
                    reason="CRYPTO_PAPER_POSITION_NOT_FOUND",
                )
            )
            next_state = _with_operation(
                replace(
                    state,
                    account_state=applied.account_state,
                    evidence=(*state.evidence, evidence),
                    orders=_replace(
                        state.orders,
                        predicate=lambda item: item.order_id == order_id,
                        replacement=applied.order,
                        reason="CRYPTO_PAPER_ORDER_NOT_FOUND",
                    ),
                    reservations=_replace(
                        state.reservations,
                        predicate=lambda item: item.order_id == order_id,
                        replacement=applied.reservation,
                        reason="CRYPTO_PAPER_RESERVATION_NOT_FOUND",
                    ),
                    events=(*state.events, applied.event),
                    fills=(*state.fills, applied.fill),
                    positions=positions,
                    journals=(*state.journals, applied.journal),
                ),
                operation,
            )
        self._repository.save_state(
            state=next_state,
            prior_state_hash=state.canonical_hash,
            operation=operation,
        )
        return CryptoPaperResult(applied, False)


def _retained_fill_application(
    state: CryptoPaperState, fill: CryptoPaperFill
) -> CryptoPaperFillApplication:
    order = _one(
        state.orders,
        lambda item: item.order_id == fill.order_id,
        "CRYPTO_PAPER_ORDER_NOT_FOUND",
    )
    reservation = _one(
        state.reservations,
        lambda item: item.order_id == fill.order_id,
        "CRYPTO_PAPER_RESERVATION_NOT_FOUND",
    )
    position = _one(
        state.positions,
        lambda item: item.product_id == fill.product_id,
        "CRYPTO_PAPER_POSITION_NOT_FOUND",
    )
    event = _one(
        state.events,
        lambda item: fill.fill_id in item.evidence_ids,
        "CRYPTO_PAPER_FILL_EVENT_NOT_FOUND",
    )
    journal = _one(
        state.journals,
        lambda item: item.journal_id == primary_fill_journal_id(fill.fill_id),
        "CRYPTO_PAPER_FILL_JOURNAL_NOT_FOUND",
    )
    return CryptoPaperFillApplication(
        order,
        reservation,
        state.account_state,
        position,
        fill,
        event,
        journal,
    )


class CancelCryptoPaperOrder:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        order_id: str,
        canceled_at: datetime,
        actor: str,
        reason: str,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperCancellation]:
        state = _required_state(self._repository, account_id)
        replay = _prior_operation(state, idempotency_key)
        order = _one(
            state.orders,
            lambda item: item.order_id == order_id,
            "CRYPTO_PAPER_ORDER_NOT_FOUND",
        )
        reservation = _one(
            state.reservations,
            lambda item: item.order_id == order_id,
            "CRYPTO_PAPER_RESERVATION_NOT_FOUND",
        )
        if replay is not None:
            if replay.result_id != order_id or order.state is not PaperOrderState.CANCELED:
                raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
            return CryptoPaperResult(
                CryptoPaperCancellation(order, reservation, state.account_state, ()),
                True,
            )
        canceled = cancel_paper_order(
            order=order,
            reservation=reservation,
            account_state=state.account_state,
            canceled_at=canceled_at,
            actor=actor,
            reason=reason,
        )
        operation = _operation(
            key=idempotency_key,
            operation_type="CANCEL",
            result_id=order_id,
            result_hash=canceled.order.canonical_hash,
        )
        next_state = _with_operation(
            replace(
                state,
                account_state=canceled.account_state,
                orders=_replace(
                    state.orders,
                    predicate=lambda item: item.order_id == order_id,
                    replacement=canceled.order,
                    reason="CRYPTO_PAPER_ORDER_NOT_FOUND",
                ),
                reservations=_replace(
                    state.reservations,
                    predicate=lambda item: item.order_id == order_id,
                    replacement=canceled.reservation,
                    reason="CRYPTO_PAPER_RESERVATION_NOT_FOUND",
                ),
                events=(*state.events, *canceled.events),
            ),
            operation,
        )
        self._repository.save_state(
            state=next_state,
            prior_state_hash=state.canonical_hash,
            operation=operation,
        )
        return CryptoPaperResult(canceled, False)


class ExpireCryptoPaperOrder:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        order_id: str,
        expired_at: datetime,
        actor: str,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperCancellation]:
        state = _required_state(self._repository, account_id)
        replay = _prior_operation(state, idempotency_key)
        order = _one(
            state.orders,
            lambda item: item.order_id == order_id,
            "CRYPTO_PAPER_ORDER_NOT_FOUND",
        )
        reservation = _one(
            state.reservations,
            lambda item: item.order_id == order_id,
            "CRYPTO_PAPER_RESERVATION_NOT_FOUND",
        )
        if replay is not None:
            if replay.result_id != order_id or order.state is not PaperOrderState.EXPIRED:
                raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
            return CryptoPaperResult(
                CryptoPaperCancellation(order, reservation, state.account_state, ()),
                True,
            )
        expired = expire_paper_order(
            order=order,
            reservation=reservation,
            account_state=state.account_state,
            expired_at=expired_at,
            actor=actor,
        )
        operation = _operation(
            key=idempotency_key,
            operation_type="EXPIRE",
            result_id=order_id,
            result_hash=expired.order.canonical_hash,
        )
        next_state = _with_operation(
            replace(
                state,
                account_state=expired.account_state,
                orders=_replace(
                    state.orders,
                    predicate=lambda item: item.order_id == order_id,
                    replacement=expired.order,
                    reason="CRYPTO_PAPER_ORDER_NOT_FOUND",
                ),
                reservations=_replace(
                    state.reservations,
                    predicate=lambda item: item.order_id == order_id,
                    replacement=expired.reservation,
                    reason="CRYPTO_PAPER_RESERVATION_NOT_FOUND",
                ),
                events=(*state.events, *expired.events),
            ),
            operation,
        )
        self._repository.save_state(
            state=next_state,
            prior_state_hash=state.canonical_hash,
            operation=operation,
        )
        return CryptoPaperResult(expired, False)


class ReconcileCryptoPaperAccount:
    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        reconciliation_id: str,
        evaluated_at: datetime,
        order_id: str | None,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperReconciliation]:
        state = _required_state(self._repository, account_id)
        replay = _prior_operation(state, idempotency_key)
        retained = next(
            (item for item in state.reconciliations if item.reconciliation_id == reconciliation_id),
            None,
        )
        if replay is not None:
            if retained is None or replay.result_id != reconciliation_id:
                raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
            return CryptoPaperResult(retained, True)
        reconciliation = reconcile_paper_state(
            reconciliation_id=reconciliation_id,
            account=state.account,
            account_state=state.account_state,
            orders=state.orders,
            reservations=state.reservations,
            fills=state.fills,
            positions=state.positions,
            journals=state.journals,
            evaluated_at=evaluated_at,
            order_id=order_id,
        )
        operation = _operation(
            key=idempotency_key,
            operation_type="RECONCILE",
            result_id=reconciliation.reconciliation_id,
            result_hash=reconciliation.canonical_hash,
        )
        next_state = _with_operation(
            replace(
                state,
                reconciliations=(*state.reconciliations, reconciliation),
            ),
            operation,
        )
        self._repository.save_state(
            state=next_state,
            prior_state_hash=state.canonical_hash,
            operation=operation,
        )
        return CryptoPaperResult(reconciliation, False)


class CorrectCryptoPaperCloseAccounting:
    """Guarded, append-only repair of one pre-fix SELL journal (Crypto PAPER
    Ledger Close Accounting Integrity v1).

    Appends exactly one deterministic correction journal (see
    ``domain.crypto_paper.build_close_accounting_correction``) in one new
    state snapshot. It refuses unless: the operator's expected source-journal
    hash and restatement amount match exactly; the account currently shows
    a ledger-contract discrepancy for the SELL's product; and, with the
    correction applied, the ledger contract holds with no discrepancy left.
    Account state, orders, reservations, fills, positions, and every retained
    journal stay byte-identical. Replay by the same idempotency key is a
    no-op; any other second attempt is refused."""

    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def plan(
        self,
        *,
        account_id: str,
        sell_fill_id: str,
        expected_source_journal_hash: str,
        expected_restatement: DecimalInput,
    ) -> CryptoPaperCloseAccountingCorrection:
        state = _required_state(self._repository, account_id)
        return self._validated(
            state,
            sell_fill_id=sell_fill_id,
            expected_source_journal_hash=expected_source_journal_hash,
            expected_restatement=expected_restatement,
        )

    def execute(
        self,
        *,
        account_id: str,
        sell_fill_id: str,
        expected_source_journal_hash: str,
        expected_restatement: DecimalInput,
        idempotency_key: str,
    ) -> CryptoPaperResult[CryptoPaperJournal]:
        state = _required_state(self._repository, account_id)
        correction_id = close_accounting_correction_journal_id(sell_fill_id)
        replay = _prior_operation(state, idempotency_key)
        retained = next(
            (item for item in state.journals if item.journal_id == correction_id),
            None,
        )
        if replay is not None:
            if (
                retained is None
                or replay.result_id != correction_id
                or replay.result_hash != retained.canonical_hash
            ):
                raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
            return CryptoPaperResult(retained, True)
        correction = self._validated(
            state,
            sell_fill_id=sell_fill_id,
            expected_source_journal_hash=expected_source_journal_hash,
            expected_restatement=expected_restatement,
        )
        operation = _operation(
            key=idempotency_key,
            operation_type="CORRECT_CLOSE_ACCOUNTING",
            result_id=correction.journal.journal_id,
            result_hash=correction.journal.canonical_hash,
        )
        next_state = _with_operation(
            replace(state, journals=(*state.journals, correction.journal)),
            operation,
        )
        self._repository.save_state(
            state=next_state,
            prior_state_hash=state.canonical_hash,
            operation=operation,
        )
        return CryptoPaperResult(correction.journal, False)

    @staticmethod
    def _validated(
        state: CryptoPaperState,
        *,
        sell_fill_id: str,
        expected_source_journal_hash: str,
        expected_restatement: DecimalInput,
    ) -> CryptoPaperCloseAccountingCorrection:
        try:
            expected = parse_decimal(expected_restatement, allow_negative=True)
        except ValueError as error:
            raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_EXPECTED_AMOUNT_INVALID") from error
        correction = build_close_accounting_correction(
            account_id=state.account.account_id,
            fills=state.fills,
            journals=state.journals,
            sell_fill_id=sell_fill_id,
        )
        if correction.source_journal_hash != expected_source_journal_hash:
            raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_SOURCE_HASH_MISMATCH")
        if correction.restatement != expected:
            raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_AMOUNT_MISMATCH")
        product_id = next(item.product_id for item in state.fills if item.fill_id == sell_fill_id)
        before = ledger_contract_discrepancies(
            account_state=state.account_state,
            fills=state.fills,
            positions=state.positions,
            journals=state.journals,
        )
        if f"LEDGER_ASSET_COST_MISMATCH:{product_id}" not in before:
            raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_DEFECT_NOT_PRESENT")
        after = ledger_contract_discrepancies(
            account_state=state.account_state,
            fills=state.fills,
            positions=state.positions,
            journals=(*state.journals, correction.journal),
        )
        if after:
            raise CryptoPaperError("CRYPTO_PAPER_CORRECTION_DOES_NOT_RESTORE_LEDGER_CONTRACT")
        return correction


class InspectCryptoPaperOperatingSeam:
    """Read-only disabled-by-default operating-cycle seam."""

    def __init__(self, repository: CryptoPaperRepository) -> None:
        self._repository = repository

    def execute(self, *, account_id: str) -> dict[str, object]:
        state = self._repository.state(account_id)
        if state is None:
            return {
                "enabled": False,
                "reason": "CRYPTO_PAPER_ACCOUNT_NOT_CONFIGURED",
                "open_order_ids": (),
                "provider_requests": 0,
                "paper_authority": "PAPER_ONLY",
                "live_authority": "NONE",
            }
        open_ids = tuple(
            item.order_id
            for item in state.orders
            if item.state in {PaperOrderState.OPEN, PaperOrderState.PARTIALLY_FILLED}
        )
        return {
            "enabled": False,
            "reason": "CRYPTO_PAPER_OPERATING_TASK_REQUIRES_EXPLICIT_FUTURE_PLAN_CONTROL",
            "inspectable_open_order_ids": open_ids,
            "can_apply_retained_evidence": bool(open_ids),
            "can_expire": bool(open_ids),
            "can_reconcile": True,
            "provider_requests": 0,
            "paper_authority": "PAPER_ONLY",
            "live_authority": "NONE",
        }


def _required_state(repository: CryptoPaperRepository, account_id: str) -> CryptoPaperState:
    state = repository.state(account_id)
    if state is None:
        raise CryptoPaperError("CRYPTO_PAPER_ACCOUNT_NOT_FOUND")
    return state


class InMemoryCryptoPaperRepository:
    def __init__(self) -> None:
        self._contracts: dict[str, CryptoPaperProviderContract] = {}
        self._contract_keys: dict[str, str] = {}
        self._capabilities: dict[str, CryptoPaperCapability] = {}
        self._capability_keys: dict[str, str] = {}
        self._accounts: dict[str, CryptoPaperAccount] = {}
        self._account_keys: dict[str, str] = {}
        self._states: dict[str, CryptoPaperState] = {}
        self._scans: dict[str, CryptoPaperEligibilityScan] = {}
        self._scan_decisions: dict[str, tuple[CryptoPaperEligibility, ...]] = {}
        self._scan_keys: dict[str, str] = {}

    def contract(self, contract_id: str) -> CryptoPaperProviderContract | None:
        return self._contracts.get(contract_id)

    def save_contract(
        self,
        *,
        contract: CryptoPaperProviderContract,
        idempotency_key: str,
    ) -> bool:
        prior = self._contracts.get(contract.contract_id)
        if prior is not None:
            if (
                prior.canonical_hash != contract.canonical_hash
                or self._contract_keys.get(idempotency_key) != contract.contract_id
            ):
                raise CryptoPaperError("CRYPTO_PAPER_CONTRACT_IDENTITY_CONFLICT")
            return True
        if idempotency_key in self._contract_keys:
            raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
        self._contracts[contract.contract_id] = contract
        self._contract_keys[idempotency_key] = contract.contract_id
        return False

    def capability(self, capability_id: str) -> CryptoPaperCapability | None:
        return self._capabilities.get(capability_id)

    def save_capability(
        self,
        *,
        capability: CryptoPaperCapability,
        idempotency_key: str,
    ) -> bool:
        prior = self._capabilities.get(capability.capability_id)
        if prior is not None:
            if (
                prior.canonical_hash != capability.canonical_hash
                or self._capability_keys.get(idempotency_key) != capability.capability_id
            ):
                raise CryptoPaperError("CRYPTO_PAPER_CAPABILITY_IDENTITY_CONFLICT")
            return True
        if idempotency_key in self._capability_keys:
            raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
        self._capabilities[capability.capability_id] = capability
        self._capability_keys[idempotency_key] = capability.capability_id
        return False

    def account(self, account_id: str) -> CryptoPaperAccount | None:
        return self._accounts.get(account_id)

    def state(self, account_id: str) -> CryptoPaperState | None:
        return self._states.get(account_id)

    def save_account(
        self,
        *,
        account: CryptoPaperAccount,
        state: CryptoPaperState,
        idempotency_key: str,
    ) -> bool:
        prior = self._accounts.get(account.account_id)
        if prior is not None:
            if (
                prior.canonical_hash != account.canonical_hash
                or self._account_keys.get(idempotency_key) != account.account_id
            ):
                raise CryptoPaperError("CRYPTO_PAPER_ACCOUNT_IDENTITY_CONFLICT")
            return True
        if idempotency_key in self._account_keys:
            raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
        self._accounts[account.account_id] = account
        self._states[account.account_id] = state
        self._account_keys[idempotency_key] = account.account_id
        return False

    def save_state(
        self,
        *,
        state: CryptoPaperState,
        prior_state_hash: str,
        operation: CryptoPaperOperation,
    ) -> bool:
        retained = self._states.get(state.account.account_id)
        if retained is None:
            raise CryptoPaperError("CRYPTO_PAPER_ACCOUNT_NOT_FOUND")
        existing = next(
            (
                item
                for item in retained.operations
                if item.idempotency_key == operation.idempotency_key
            ),
            None,
        )
        if existing is not None:
            if existing != operation:
                raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
            return True
        if retained.canonical_hash != prior_state_hash:
            raise CryptoPaperError("CRYPTO_PAPER_STATE_CONCURRENCY_CONFLICT")
        self._states[state.account.account_id] = state
        return False

    def scan(self, scan_id: str) -> CryptoPaperEligibilityScan | None:
        return self._scans.get(scan_id)

    def scan_decisions(self, scan_id: str) -> tuple[CryptoPaperEligibility, ...]:
        return self._scan_decisions.get(scan_id, ())

    def save_scan(
        self,
        *,
        scan: CryptoPaperEligibilityScan,
        decisions: tuple[CryptoPaperEligibility, ...],
        idempotency_key: str,
    ) -> bool:
        prior = self._scans.get(scan.scan_id)
        if prior is not None:
            if (
                prior.canonical_hash != scan.canonical_hash
                or self._scan_keys.get(idempotency_key) != scan.scan_id
            ):
                raise CryptoPaperError("CRYPTO_PAPER_SCAN_IDENTITY_CONFLICT")
            return True
        if idempotency_key in self._scan_keys:
            raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
        self._scans[scan.scan_id] = scan
        self._scan_decisions[scan.scan_id] = decisions
        self._scan_keys[idempotency_key] = scan.scan_id
        return False


def paper_workspace(state: CryptoPaperState | None) -> dict[str, object]:
    if state is None:
        return {
            "account": None,
            "open_orders": (),
            "fills": (),
            "positions": (),
            "reconciliations": (),
            "modeled_fees": "0",
            "realized_paper_pnl": "0",
            "warnings": ("NO_CRYPTO_PAPER_ACCOUNT_RETAINED",),
            "provider_requests": 0,
            "paper_authority": "PAPER_ONLY",
            "live_authority": "NONE",
            "genuine_financial_mutation": False,
        }
    return {
        "account": state.account,
        "account_state": state.account_state,
        "authorizations": state.authorizations,
        "previews": state.previews,
        "open_orders": tuple(
            item
            for item in state.orders
            if item.state in {PaperOrderState.OPEN, PaperOrderState.PARTIALLY_FILLED}
        ),
        "orders": state.orders,
        "fills": state.fills,
        "positions": state.positions,
        "reservations": state.reservations,
        "reconciliations": state.reconciliations,
        "modeled_fees": state.account_state.modeled_fees,
        "realized_paper_pnl": state.account_state.realized_paper_pnl,
        "warnings": ("PAPER BALANCES ARE APEX SIMULATION, NOT COINBASE OR GENUINE MONEY",),
        "provider_requests": 0,
        "paper_authority": "PAPER_ONLY",
        "live_authority": "NONE",
        "genuine_financial_mutation": False,
    }
