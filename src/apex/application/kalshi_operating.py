"""Application services for bounded Kalshi refresh and internal PAPER."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from apex.application.discover_public_markets import (
    PublicMarketScanRequest,
    PublicMarketScanSummary,
)
from apex.application.scan_public_binary_complements import (
    BinaryComplementScanRequest,
    PublicBinaryComplementReport,
)
from apex.domain.kalshi_operating import (
    KalshiCapability,
    KalshiCurrentOpportunity,
    KalshiEvidenceClass,
    KalshiOperatingError,
    KalshiPaperAuthorization,
    KalshiPaperEligibility,
    KalshiPaperEligibilityScan,
    KalshiPaperFill,
    KalshiPaperOperation,
    KalshiPaperOrder,
    KalshiPaperPreview,
    KalshiPaperReconciliation,
    KalshiPaperSettlement,
    KalshiPaperState,
    KalshiRefreshRecord,
    KalshiResolutionFact,
    apply_paper_fill,
    cancel_paper_order,
    expire_paper_order,
    preview_paper_order,
    reconcile_paper_state,
    settle_paper_position,
    submit_paper_order,
)
from apex.domain.values import DecimalInput


class KalshiOperatingRepository(Protocol):
    def capability(self, capability_id: str) -> KalshiCapability | None: ...

    def save_capability(self, *, capability: KalshiCapability, idempotency_key: str) -> bool: ...

    def latest_capability(self) -> KalshiCapability | None: ...

    def refresh(self, refresh_id: str) -> KalshiRefreshRecord | None: ...

    def latest_refresh(self) -> KalshiRefreshRecord | None: ...

    def opportunities_for_refresh(
        self, refresh_id: str
    ) -> tuple[KalshiCurrentOpportunity, ...]: ...

    def save_refresh(
        self,
        *,
        refresh: KalshiRefreshRecord,
        opportunities: tuple[KalshiCurrentOpportunity, ...],
        idempotency_key: str,
    ) -> bool: ...

    def scan(self, scan_id: str) -> KalshiPaperEligibilityScan | None: ...

    def scan_decisions(self, scan_id: str) -> tuple[KalshiPaperEligibility, ...]: ...

    def save_scan(
        self,
        *,
        scan: KalshiPaperEligibilityScan,
        decisions: tuple[KalshiPaperEligibility, ...],
        idempotency_key: str,
    ) -> bool: ...

    def state(self, account_id: str) -> KalshiPaperState | None: ...

    def save_account(self, *, state: KalshiPaperState, idempotency_key: str) -> bool: ...

    def save_state(
        self,
        *,
        state: KalshiPaperState,
        prior_state_hash: str,
        operation: KalshiPaperOperation,
    ) -> bool: ...


class MarketDiscovery(Protocol):
    def discover(self, request: PublicMarketScanRequest) -> PublicMarketScanSummary: ...


class ComplementScanner(Protocol):
    def scan(self, request: BinaryComplementScanRequest) -> PublicBinaryComplementReport: ...


class CurrentOpportunitySource(Protocol):
    def build(
        self,
        *,
        refresh_id: str,
        evidence_class: KalshiEvidenceClass,
        evaluation_ids: tuple[str, ...],
        as_of: datetime,
    ) -> tuple[KalshiCurrentOpportunity, ...]: ...


@dataclass(frozen=True, slots=True)
class KalshiResult[T]:
    value: T
    idempotent_replay: bool
    provider_requests: int = 0
    provider_writes: int = 0
    genuine_financial_mutation: bool = False
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class KalshiCurrentRefreshCommand:
    refresh_id: str
    as_of: datetime
    evidence_class: KalshiEvidenceClass
    market_request: PublicMarketScanRequest
    complement_request: BinaryComplementScanRequest
    idempotency_key: str


class ExecuteKalshiCurrentRefresh:
    """Delegate to existing discovery/complement services and bind their IDs."""

    def __init__(
        self,
        *,
        repository: KalshiOperatingRepository,
        discovery: MarketDiscovery,
        complements: ComplementScanner,
        opportunity_source: CurrentOpportunitySource,
        provider_request_count: Callable[[], int],
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._discovery = discovery
        self._complements = complements
        self._opportunities = opportunity_source
        self._request_count = provider_request_count
        self._clock = clock

    def execute(self, command: KalshiCurrentRefreshCommand) -> KalshiResult[KalshiRefreshRecord]:
        retained = self._repository.refresh(command.refresh_id)
        if retained is not None:
            expected = (
                command.as_of,
                command.evidence_class,
                command.market_request.ingestion_run_id,
                command.market_request.canonical_configuration_hash,
                command.complement_request.canonical_configuration_hash.value,
            )
            actual = (
                retained.as_of,
                retained.evidence_class,
                retained.market_discovery_run_id,
                retained.market_configuration_hash,
                retained.complement_configuration_hash,
            )
            if actual != expected:
                raise KalshiOperatingError("KALSHI_REFRESH_IDENTITY_CONFLICT")
            return KalshiResult(retained, True)
        market = self._discovery.discover(command.market_request)
        if market.ingestion_run_id != command.complement_request.market_discovery_run_id:
            raise KalshiOperatingError("KALSHI_REFRESH_DISCOVERY_BINDING_MISMATCH")
        complement = self._complements.scan(command.complement_request)
        evaluation_ids = tuple(item.evaluation_id for item in complement.evaluations)
        opportunities = self._opportunities.build(
            refresh_id=command.refresh_id,
            evidence_class=command.evidence_class,
            evaluation_ids=evaluation_ids,
            as_of=command.as_of,
        )
        calls = self._request_count()
        warnings = tuple(
            value
            for value in (
                "INVALID_ORDERBOOK_EVIDENCE_RETAINED" if complement.invalid_orderbooks else None,
                "NO_CURRENT_ELIGIBLE_KALSHI_OPPORTUNITY"
                if not any(item.eligibility.value == "ELIGIBLE" for item in opportunities)
                else None,
            )
            if value is not None
        )
        refresh = KalshiRefreshRecord.create(
            refresh_id=command.refresh_id,
            as_of=command.as_of,
            evidence_class=command.evidence_class,
            market_discovery_run_id=market.ingestion_run_id,
            market_configuration_hash=command.market_request.canonical_configuration_hash,
            complement_configuration_hash=(
                command.complement_request.canonical_configuration_hash.value
            ),
            page_count=market.page_count,
            market_count=market.market_count,
            markets_evaluated=complement.markets_evaluated,
            opportunities=opportunities,
            evaluation_ids=evaluation_ids,
            provider_requests=calls,
            request_bounds=(
                ("MAX_MARKET_PAGES", command.market_request.maximum_pages),
                ("MARKET_PAGE_SIZE", command.market_request.page_size),
                ("MAX_ORDERBOOK_MARKETS", command.complement_request.market_limit),
                ("MAX_ORDERBOOK_DEPTH", command.complement_request.maximum_depth),
                (
                    "TIMEOUT_SECONDS",
                    max(
                        command.market_request.timeout_seconds,
                        command.complement_request.timeout_seconds,
                    ),
                ),
            ),
            checkpoints=(market.ingestion_run_id, *evaluation_ids),
            warnings=warnings,
            completed_at=self._clock(),
        )
        replay = self._repository.save_refresh(
            refresh=refresh,
            opportunities=opportunities,
            idempotency_key=command.idempotency_key,
        )
        return KalshiResult(refresh, replay, 0 if replay else calls)


class RetainKalshiCapability:
    def __init__(self, repository: KalshiOperatingRepository) -> None:
        self._repository = repository

    def execute(
        self, *, capability: KalshiCapability, idempotency_key: str
    ) -> KalshiResult[KalshiCapability]:
        replay = self._repository.save_capability(
            capability=capability, idempotency_key=idempotency_key
        )
        return KalshiResult(capability, replay)


class RetainKalshiPaperEligibilityScan:
    def __init__(self, repository: KalshiOperatingRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        scan: KalshiPaperEligibilityScan,
        decisions: tuple[KalshiPaperEligibility, ...],
        idempotency_key: str,
    ) -> KalshiResult[KalshiPaperEligibilityScan]:
        if (
            scan.source_count != len(decisions)
            or scan.eligible_count != sum(item.eligible for item in decisions)
            or scan.decision_ids != tuple(item.decision_id for item in decisions)
        ):
            raise KalshiOperatingError("KALSHI_PAPER_SCAN_DECISION_MISMATCH")
        replay = self._repository.save_scan(
            scan=scan, decisions=decisions, idempotency_key=idempotency_key
        )
        return KalshiResult(scan, replay)


def _operation(
    *, key: str, operation_type: str, result_id: str, result_hash: str
) -> KalshiPaperOperation:
    return KalshiPaperOperation.create(
        idempotency_key=key,
        operation_type=operation_type,
        result_id=result_id,
        result_hash=result_hash,
    )


def _prior(state: KalshiPaperState, key: str) -> KalshiPaperOperation | None:
    return next((item for item in state.operations if item.idempotency_key == key), None)


def _required_state(repository: KalshiOperatingRepository, account_id: str) -> KalshiPaperState:
    state = repository.state(account_id)
    if state is None:
        raise KalshiOperatingError("KALSHI_PAPER_ACCOUNT_NOT_FOUND")
    return state


def _persist(
    repository: KalshiOperatingRepository,
    *,
    prior: KalshiPaperState,
    state: KalshiPaperState,
    operation: KalshiPaperOperation,
) -> bool:
    sealed = replace(state, operations=(*state.operations, operation), canonical_hash="").sealed()
    return repository.save_state(
        state=sealed,
        prior_state_hash=prior.canonical_hash,
        operation=operation,
    )


class CreateKalshiPaperAccount:
    def __init__(self, repository: KalshiOperatingRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        opening_cash: DecimalInput,
        created_at: datetime,
        idempotency_key: str,
    ) -> KalshiResult[KalshiPaperState]:
        from apex.domain.kalshi_operating import KalshiPaperAccount

        state = KalshiPaperState.opening(
            KalshiPaperAccount.create(
                account_id=account_id,
                opening_cash=opening_cash,
                created_at=created_at,
            )
        )
        KalshiPaperOperation.create(
            idempotency_key=idempotency_key,
            operation_type="CREATE_ACCOUNT",
            result_id=state.account.account_id,
            result_hash=state.canonical_hash,
        )
        replay = self._repository.save_account(state=state, idempotency_key=idempotency_key)
        retained = _required_state(self._repository, account_id)
        return KalshiResult(retained, replay)


class AuthorizeKalshiPaperOpportunity:
    def __init__(self, repository: KalshiOperatingRepository) -> None:
        self._repository = repository

    def execute(
        self, *, authorization: KalshiPaperAuthorization
    ) -> KalshiResult[KalshiPaperAuthorization]:
        state = _required_state(self._repository, authorization.account_id)
        prior = _prior(state, authorization.idempotency_key)
        retained = next(
            (
                item
                for item in state.authorizations
                if item.authorization_id == authorization.authorization_id
            ),
            None,
        )
        if prior is not None:
            if retained is None or prior.result_hash != retained.canonical_hash:
                raise KalshiOperatingError("KALSHI_PAPER_IDEMPOTENCY_CONFLICT")
            return KalshiResult(retained, True)
        if retained is not None:
            raise KalshiOperatingError("KALSHI_PAPER_AUTHORIZATION_IDENTITY_CONFLICT")
        operation = _operation(
            key=authorization.idempotency_key,
            operation_type="AUTHORIZE",
            result_id=authorization.authorization_id,
            result_hash=authorization.canonical_hash,
        )
        replay = _persist(
            self._repository,
            prior=state,
            state=replace(
                state,
                authorizations=(*state.authorizations, authorization),
                updated_at=authorization.created_at,
            ),
            operation=operation,
        )
        return KalshiResult(authorization, replay)


class PreviewKalshiPaperOrder:
    def __init__(self, repository: KalshiOperatingRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        opportunity: KalshiCurrentOpportunity,
        preview_id: str,
        authorization_id: str,
        limit_price: DecimalInput,
        contracts: int,
        available_liquidity: int,
        evidence_id: str,
        evidence_observed_at: datetime,
        created_at: datetime,
        idempotency_key: str,
    ) -> KalshiResult[KalshiPaperPreview]:
        state = _required_state(self._repository, account_id)
        prior = _prior(state, idempotency_key)
        retained = next((item for item in state.previews if item.preview_id == preview_id), None)
        if prior is not None:
            if retained is None or prior.result_hash != retained.canonical_hash:
                raise KalshiOperatingError("KALSHI_PAPER_IDEMPOTENCY_CONFLICT")
            return KalshiResult(retained, True)
        authorization = next(
            (item for item in state.authorizations if item.authorization_id == authorization_id),
            None,
        )
        if authorization is None:
            raise KalshiOperatingError("KALSHI_PAPER_AUTHORIZATION_NOT_FOUND")
        preview = preview_paper_order(
            preview_id=preview_id,
            authorization=authorization,
            opportunity=opportunity,
            limit_price=limit_price,
            contracts=contracts,
            available_cash=state.available_cash,
            available_liquidity=available_liquidity,
            evidence_id=evidence_id,
            evidence_observed_at=evidence_observed_at,
            created_at=created_at,
        )
        operation = _operation(
            key=idempotency_key,
            operation_type="PREVIEW",
            result_id=preview.preview_id,
            result_hash=preview.canonical_hash,
        )
        replay = _persist(
            self._repository,
            prior=state,
            state=replace(
                state,
                previews=(*state.previews, preview),
                updated_at=created_at,
            ),
            operation=operation,
        )
        return KalshiResult(preview, replay)


class SubmitKalshiPaperOrder:
    def __init__(self, repository: KalshiOperatingRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        order_id: str,
        preview_id: str,
        submitted_at: datetime,
        idempotency_key: str,
    ) -> KalshiResult[KalshiPaperOrder]:
        state = _required_state(self._repository, account_id)
        prior_operation = _prior(state, idempotency_key)
        retained = next((item for item in state.orders if item.order_id == order_id), None)
        if prior_operation is not None:
            if retained is None or prior_operation.result_id != retained.order_id:
                raise KalshiOperatingError("KALSHI_PAPER_IDEMPOTENCY_CONFLICT")
            return KalshiResult(retained, True)
        preview = next((item for item in state.previews if item.preview_id == preview_id), None)
        if preview is None:
            raise KalshiOperatingError("KALSHI_PAPER_PREVIEW_NOT_FOUND")
        order, _, next_state = submit_paper_order(
            state=state,
            order_id=order_id,
            preview=preview,
            idempotency_key=idempotency_key,
            submitted_at=submitted_at,
        )
        operation = _operation(
            key=idempotency_key,
            operation_type="SUBMIT_INTERNAL",
            result_id=order.order_id,
            result_hash=order.canonical_hash,
        )
        replay = _persist(
            self._repository,
            prior=state,
            state=next_state,
            operation=operation,
        )
        return KalshiResult(order, replay)


class ApplyKalshiPaperFill:
    def __init__(self, repository: KalshiOperatingRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        order_id: str,
        fill_id: str,
        contracts: int,
        price: DecimalInput,
        evidence_id: str,
        evidence_observed_at: datetime,
        ingested_at: datetime,
        idempotency_key: str,
    ) -> KalshiResult[KalshiPaperFill]:
        state = _required_state(self._repository, account_id)
        prior_operation = _prior(state, idempotency_key)
        retained = next((item for item in state.fills if item.fill_id == fill_id), None)
        if prior_operation is not None:
            if retained is None or prior_operation.result_hash != retained.canonical_hash:
                raise KalshiOperatingError("KALSHI_PAPER_IDEMPOTENCY_CONFLICT")
            return KalshiResult(retained, True)
        fill, next_state = apply_paper_fill(
            state=state,
            order_id=order_id,
            fill_id=fill_id,
            contracts=contracts,
            price=price,
            evidence_id=evidence_id,
            evidence_observed_at=evidence_observed_at,
            ingested_at=ingested_at,
            idempotency_key=idempotency_key,
        )
        operation = _operation(
            key=idempotency_key,
            operation_type="APPLY_RETAINED_FILL",
            result_id=fill.fill_id,
            result_hash=fill.canonical_hash,
        )
        replay = _persist(
            self._repository,
            prior=state,
            state=next_state,
            operation=operation,
        )
        return KalshiResult(fill, replay)


class CancelKalshiPaperOrder:
    def __init__(self, repository: KalshiOperatingRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        order_id: str,
        canceled_at: datetime,
        idempotency_key: str,
    ) -> KalshiResult[KalshiPaperOrder]:
        state = _required_state(self._repository, account_id)
        prior_operation = _prior(state, idempotency_key)
        retained = next((item for item in state.orders if item.order_id == order_id), None)
        if prior_operation is not None:
            if (
                retained is None
                or prior_operation.operation_type != "CANCEL_INTERNAL"
                or prior_operation.result_id != retained.order_id
            ):
                raise KalshiOperatingError("KALSHI_PAPER_IDEMPOTENCY_CONFLICT")
            return KalshiResult(retained, True)
        next_state = cancel_paper_order(state=state, order_id=order_id, canceled_at=canceled_at)
        order = next(item for item in next_state.orders if item.order_id == order_id)
        operation = _operation(
            key=idempotency_key,
            operation_type="CANCEL_INTERNAL",
            result_id=order.order_id,
            result_hash=order.canonical_hash,
        )
        replay = _persist(
            self._repository,
            prior=state,
            state=next_state,
            operation=operation,
        )
        return KalshiResult(order, replay)


class ExpireKalshiPaperOrder:
    def __init__(self, repository: KalshiOperatingRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        order_id: str,
        expired_at: datetime,
        idempotency_key: str,
    ) -> KalshiResult[KalshiPaperOrder]:
        state = _required_state(self._repository, account_id)
        prior_operation = _prior(state, idempotency_key)
        retained = next((item for item in state.orders if item.order_id == order_id), None)
        if prior_operation is not None:
            if (
                retained is None
                or prior_operation.operation_type != "EXPIRE_INTERNAL"
                or prior_operation.result_id != retained.order_id
            ):
                raise KalshiOperatingError("KALSHI_PAPER_IDEMPOTENCY_CONFLICT")
            return KalshiResult(retained, True)
        next_state = expire_paper_order(state=state, order_id=order_id, expired_at=expired_at)
        order = next(item for item in next_state.orders if item.order_id == order_id)
        operation = _operation(
            key=idempotency_key,
            operation_type="EXPIRE_INTERNAL",
            result_id=order.order_id,
            result_hash=order.canonical_hash,
        )
        replay = _persist(
            self._repository,
            prior=state,
            state=next_state,
            operation=operation,
        )
        return KalshiResult(order, replay)


class SettleKalshiPaperPosition:
    def __init__(self, repository: KalshiOperatingRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        position_id: str,
        fact: KalshiResolutionFact,
        settlement_id: str,
        idempotency_key: str,
    ) -> KalshiResult[KalshiPaperSettlement]:
        state = _required_state(self._repository, account_id)
        prior_operation = _prior(state, idempotency_key)
        retained = next(
            (item for item in state.settlements if item.settlement_id == settlement_id),
            None,
        )
        if prior_operation is not None:
            if retained is None or prior_operation.result_hash != retained.canonical_hash:
                raise KalshiOperatingError("KALSHI_PAPER_IDEMPOTENCY_CONFLICT")
            return KalshiResult(retained, True)
        settlement, next_state = settle_paper_position(
            state=state,
            position_id=position_id,
            fact=fact,
            settlement_id=settlement_id,
            idempotency_key=idempotency_key,
        )
        operation = _operation(
            key=idempotency_key,
            operation_type="SETTLE_FROM_RETAINED_RESOLUTION",
            result_id=settlement.settlement_id,
            result_hash=settlement.canonical_hash,
        )
        replay = _persist(
            self._repository,
            prior=state,
            state=next_state,
            operation=operation,
        )
        return KalshiResult(settlement, replay)


class ReconcileKalshiPaperAccount:
    def __init__(self, repository: KalshiOperatingRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        account_id: str,
        reconciliation_id: str,
        reconciled_at: datetime,
        idempotency_key: str,
    ) -> KalshiResult[KalshiPaperReconciliation]:
        state = _required_state(self._repository, account_id)
        prior_operation = _prior(state, idempotency_key)
        retained = next(
            (item for item in state.reconciliations if item.reconciliation_id == reconciliation_id),
            None,
        )
        if prior_operation is not None:
            if retained is None or prior_operation.result_hash != retained.canonical_hash:
                raise KalshiOperatingError("KALSHI_PAPER_IDEMPOTENCY_CONFLICT")
            return KalshiResult(retained, True)
        value = reconcile_paper_state(
            state=state,
            reconciliation_id=reconciliation_id,
            reconciled_at=reconciled_at,
        )
        operation = _operation(
            key=idempotency_key,
            operation_type="RECONCILE",
            result_id=value.reconciliation_id,
            result_hash=value.canonical_hash,
        )
        replay = _persist(
            self._repository,
            prior=state,
            state=replace(
                state,
                reconciliations=(*state.reconciliations, value),
                updated_at=reconciled_at,
            ),
            operation=operation,
        )
        return KalshiResult(value, replay)


def kalshi_workspace(
    *,
    repository: KalshiOperatingRepository,
    account_id: str,
    capability_id: str | None = None,
    refresh_id: str | None = None,
) -> dict[str, object]:
    state = repository.state(account_id)
    return {
        "warning": "KALSHI PAPER — SIMULATED CAPITAL ONLY",
        "paper_live_separation": {
            "paper_authority": "PAPER_ONLY",
            "live_authority": "NONE",
            "genuine_provider_mutation": False,
        },
        "provider_capability": (
            None if capability_id is None else repository.capability(capability_id)
        ),
        "latest_refresh": (None if refresh_id is None else repository.refresh(refresh_id)),
        "current_opportunities": (
            () if refresh_id is None else repository.opportunities_for_refresh(refresh_id)
        ),
        "paper_account": None if state is None else state.account,
        "paper_state": state,
    }


class InMemoryKalshiOperatingRepository(KalshiOperatingRepository):
    """Deterministic fixture repository with the same immutable contracts."""

    def __init__(self) -> None:
        self.capabilities: dict[str, tuple[KalshiCapability, str]] = {}
        self.refreshes: dict[
            str,
            tuple[
                KalshiRefreshRecord,
                tuple[KalshiCurrentOpportunity, ...],
                str,
            ],
        ] = {}
        self.scans: dict[
            str,
            tuple[
                KalshiPaperEligibilityScan,
                tuple[KalshiPaperEligibility, ...],
                str,
            ],
        ] = {}
        self.states: dict[str, KalshiPaperState] = {}
        self.account_keys: dict[str, str] = {}

    def capability(self, capability_id: str) -> KalshiCapability | None:
        value = self.capabilities.get(capability_id)
        return None if value is None else value[0]

    def latest_capability(self) -> KalshiCapability | None:
        values = tuple(value[0] for value in self.capabilities.values())
        return max(values, key=lambda item: item.inspected_at) if values else None

    def save_capability(self, *, capability: KalshiCapability, idempotency_key: str) -> bool:
        retained = self.capabilities.get(capability.capability_id)
        if retained is not None:
            if retained != (capability, idempotency_key):
                raise KalshiOperatingError("KALSHI_CAPABILITY_IDENTITY_CONFLICT")
            return True
        self.capabilities[capability.capability_id] = (capability, idempotency_key)
        return False

    def refresh(self, refresh_id: str) -> KalshiRefreshRecord | None:
        value = self.refreshes.get(refresh_id)
        return None if value is None else value[0]

    def latest_refresh(self) -> KalshiRefreshRecord | None:
        values = tuple(value[0] for value in self.refreshes.values())
        return max(values, key=lambda item: item.completed_at) if values else None

    def opportunities_for_refresh(self, refresh_id: str) -> tuple[KalshiCurrentOpportunity, ...]:
        value = self.refreshes.get(refresh_id)
        return () if value is None else value[1]

    def save_refresh(
        self,
        *,
        refresh: KalshiRefreshRecord,
        opportunities: tuple[KalshiCurrentOpportunity, ...],
        idempotency_key: str,
    ) -> bool:
        retained = self.refreshes.get(refresh.refresh_id)
        value = (refresh, opportunities, idempotency_key)
        if retained is not None:
            if retained != value:
                raise KalshiOperatingError("KALSHI_REFRESH_IDENTITY_CONFLICT")
            return True
        self.refreshes[refresh.refresh_id] = value
        return False

    def scan(self, scan_id: str) -> KalshiPaperEligibilityScan | None:
        value = self.scans.get(scan_id)
        return None if value is None else value[0]

    def scan_decisions(self, scan_id: str) -> tuple[KalshiPaperEligibility, ...]:
        value = self.scans.get(scan_id)
        return () if value is None else value[1]

    def save_scan(
        self,
        *,
        scan: KalshiPaperEligibilityScan,
        decisions: tuple[KalshiPaperEligibility, ...],
        idempotency_key: str,
    ) -> bool:
        retained = self.scans.get(scan.scan_id)
        value = (scan, decisions, idempotency_key)
        if retained is not None:
            if retained != value:
                raise KalshiOperatingError("KALSHI_PAPER_SCAN_IDENTITY_CONFLICT")
            return True
        self.scans[scan.scan_id] = value
        return False

    def state(self, account_id: str) -> KalshiPaperState | None:
        return self.states.get(account_id)

    def save_account(self, *, state: KalshiPaperState, idempotency_key: str) -> bool:
        account_id = state.account.account_id
        if idempotency_key in self.account_keys.values() and account_id not in self.account_keys:
            raise KalshiOperatingError("KALSHI_PAPER_ACCOUNT_IDEMPOTENCY_CONFLICT")
        retained = self.states.get(account_id)
        if retained is not None:
            if (
                retained.account != state.account
                or self.account_keys[account_id] != idempotency_key
            ):
                raise KalshiOperatingError("KALSHI_PAPER_ACCOUNT_IDENTITY_CONFLICT")
            return True
        self.states[account_id] = state
        self.account_keys[account_id] = idempotency_key
        return False

    def save_state(
        self,
        *,
        state: KalshiPaperState,
        prior_state_hash: str,
        operation: KalshiPaperOperation,
    ) -> bool:
        retained = self.states.get(state.account.account_id)
        if retained is None:
            raise KalshiOperatingError("KALSHI_PAPER_ACCOUNT_NOT_FOUND")
        prior_operation = next(
            (
                item
                for item in retained.operations
                if item.idempotency_key == operation.idempotency_key
            ),
            None,
        )
        if prior_operation is not None:
            if prior_operation != operation:
                raise KalshiOperatingError("KALSHI_PAPER_IDEMPOTENCY_CONFLICT")
            return True
        if retained.canonical_hash != prior_state_hash:
            raise KalshiOperatingError("KALSHI_PAPER_STATE_CONCURRENCY_CONFLICT")
        self.states[state.account.account_id] = state
        return False
