"""Strict Alpaca Paper Trading transport; no LIVE host or credentials are accepted."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from apex.domain.external_paper import (
    ALPACA_PAPER_BROKER_V1,
    ExternalPaperBrokerError,
    ExternalPaperBrokerEvidence,
    ExternalPaperCapabilityState,
    ExternalPaperInstruction,
    ExternalPaperSubmissionAmbiguous,
    PaperBrokerCapabilitySnapshot,
)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
PAPER_ENDPOINTS = frozenset(
    {
        "GET:/v2/account",
        "GET:/v2/orders",
        "POST:/v2/orders",
        "GET:/v2/orders/{order_id}",
        "DELETE:/v2/orders/{order_id}",
        "GET:/v2/positions",
        "GET:/v2/positions/{symbol}",
        "GET:/v2/account/activities",
    }
)


@dataclass(frozen=True, slots=True)
class AlpacaPaperCredentials:
    source: str
    key_id: str | None
    secret: str | None

    @classmethod
    def from_environment(cls) -> AlpacaPaperCredentials:
        key_id = os.environ.get("APEX_ALPACA_PAPER_API_KEY_ID")
        secret = os.environ.get("APEX_ALPACA_PAPER_API_SECRET_KEY")
        if bool(key_id) != bool(secret):
            return cls("ENVIRONMENT", None, None)
        return cls("ENVIRONMENT", key_id, secret)

    @property
    def available(self) -> bool:
        return self.key_id is not None and self.secret is not None


@dataclass(frozen=True, slots=True)
class AlpacaPaperResponse:
    status: int
    payload: dict[str, Any] | list[dict[str, Any]]
    request_id: str | None


@dataclass(frozen=True, slots=True)
class AlpacaPaperReadiness:
    """Sanitized, bounded read-only PAPER readiness result."""

    capability: PaperBrokerCapabilitySnapshot
    evidence: tuple[ExternalPaperBrokerEvidence, ...]
    endpoint_states: tuple[tuple[str, str], ...]


class AlpacaPaperBroker:
    """Transport only approved instructions to the official Paper API host."""

    identity = ALPACA_PAPER_BROKER_V1

    def __init__(
        self,
        *,
        credentials: AlpacaPaperCredentials | None = None,
        request: Callable[[str, str, dict[str, str], bytes | None], AlpacaPaperResponse]
        | None = None,
        allow_submission: bool = False,
    ) -> None:
        self.credentials = credentials or AlpacaPaperCredentials.from_environment()
        self._request = request or _urllib_request
        self._allow_submission = allow_submission
        self._network_reads = 0
        self._network_writes = 0

    @property
    def network_reads(self) -> int:
        return self._network_reads

    @property
    def network_writes(self) -> int:
        return self._network_writes

    @property
    def credentials_available(self) -> bool:
        return self.credentials.available

    def capability_snapshot(self, *, checked_at: datetime) -> PaperBrokerCapabilitySnapshot:
        if not self.credentials.available:
            return PaperBrokerCapabilitySnapshot.create(
                state=ExternalPaperCapabilityState.CREDENTIALS_REQUIRED,
                credentials_available=False,
                authentication_state="NOT_ATTEMPTED",
                account_read=False,
                order_submit=False,
                order_read=False,
                cancel=False,
                position_read=False,
                activity_read=False,
                checked_at=checked_at,
                reason_codes=("PAPER_CREDENTIALS_REQUIRED",),
            )
        try:
            self._call("GET", "/v2/account")
        except ExternalPaperBrokerError as error:
            code = str(error)
            state = (
                ExternalPaperCapabilityState.AUTHENTICATION_REJECTED
                if "HTTP_401" in code or "HTTP_403" in code
                else ExternalPaperCapabilityState.PROVIDER_UNAVAILABLE
            )
            return PaperBrokerCapabilitySnapshot.create(
                state=state,
                credentials_available=True,
                authentication_state="REJECTED"
                if state is ExternalPaperCapabilityState.AUTHENTICATION_REJECTED
                else "UNKNOWN",
                account_read=False,
                order_submit=False,
                order_read=False,
                cancel=False,
                position_read=False,
                activity_read=False,
                checked_at=checked_at,
                reason_codes=(code,),
            )
        return PaperBrokerCapabilitySnapshot.create(
            state=ExternalPaperCapabilityState.PARTIAL,
            credentials_available=True,
            authentication_state="ACCOUNT_READ_CONFIRMED",
            account_read=True,
            order_submit=False,
            order_read=False,
            cancel=False,
            position_read=False,
            activity_read=False,
            checked_at=checked_at,
            reason_codes=("ORDER_CAPABILITIES_UNVERIFIED_WITHOUT_SUBMISSION",),
        )

    def check_readiness(self, *, checked_at: datetime) -> AlpacaPaperReadiness:
        """Read account, open orders, positions, and fills independently.

        No write endpoint is reached here.  Credentials are only used as HTTP
        headers inside `_call` and never become evidence or a return value.
        """
        if not self.credentials.available:
            capability = self.capability_snapshot(checked_at=checked_at)
            return AlpacaPaperReadiness(capability, (), (("credentials", "CREDENTIALS_REQUIRED"),))
        readers: tuple[tuple[str, Callable[[], tuple[ExternalPaperBrokerEvidence, ...]]], ...] = (
            ("account", lambda: (self.account(),)),
            ("orders", self.orders),
            ("positions", self.positions),
            ("activities", self.activities),
        )
        evidence: list[ExternalPaperBrokerEvidence] = []
        states: list[tuple[str, str]] = []
        success: dict[str, bool] = {}
        reasons: list[str] = []
        for name, reader in readers:
            try:
                evidence.extend(reader())
                success[name] = True
                states.append((name, "READY"))
            except ExternalPaperBrokerError as error:
                code = str(error)
                success[name] = False
                states.append((name, code))
                reasons.append(f"{name.upper()}:{code}")
        account_ready = success["account"]
        authentication_rejected = any(
            "HTTP_401" in state or "HTTP_403" in state for _, state in states
        )
        if authentication_rejected and not account_ready:
            state = ExternalPaperCapabilityState.AUTHENTICATION_REJECTED
            authentication_state = "REJECTED"
        elif all(success.values()):
            state = ExternalPaperCapabilityState.READY
            authentication_state = "ACCOUNT_READ_CONFIRMED"
        elif any("HTTP_401" in item or "HTTP_403" in item for item in reasons):
            state = ExternalPaperCapabilityState.PERMISSION_LIMITED
            authentication_state = "ACCOUNT_READ_CONFIRMED" if account_ready else "UNKNOWN"
        elif any("TRANSPORT" in item for item in reasons):
            state = ExternalPaperCapabilityState.PROVIDER_UNAVAILABLE
            authentication_state = "ACCOUNT_READ_CONFIRMED" if account_ready else "UNKNOWN"
        else:
            state = ExternalPaperCapabilityState.PARTIAL
            authentication_state = "ACCOUNT_READ_CONFIRMED" if account_ready else "UNKNOWN"
        capability = PaperBrokerCapabilitySnapshot.create(
            state=state,
            credentials_available=True,
            authentication_state=authentication_state,
            account_read=account_ready,
            order_submit=state is ExternalPaperCapabilityState.READY,
            order_read=success["orders"],
            cancel=False,
            position_read=success["positions"],
            activity_read=success["activities"],
            checked_at=checked_at,
            reason_codes=tuple(reasons)
            or ("PAPER_SUBMISSION_REQUIRES_EXACT_OPERATOR_CONFIRMATION",),
        )
        return AlpacaPaperReadiness(capability, tuple(evidence), tuple(states))

    def submit(self, instruction: ExternalPaperInstruction) -> ExternalPaperBrokerEvidence:
        if not self._allow_submission:
            raise ExternalPaperBrokerError("PAPER_SUBMISSION_DISABLED")
        body = {
            "symbol": instruction.symbol,
            "qty": str(instruction.quantity),
            "side": instruction.side.lower(),
            "type": "market",
            "time_in_force": "day",
            "extended_hours": False,
            "client_order_id": f"apx-{instruction.instruction_id.rsplit('_', 1)[-1]}",
        }
        try:
            response = self._call("POST", "/v2/orders", body)
        except ExternalPaperBrokerError as error:
            if "TRANSPORT" in str(error):
                raise ExternalPaperSubmissionAmbiguous("PAPER_SUBMISSION_AMBIGUOUS") from error
            raise
        return _evidence(
            instruction_id=instruction.instruction_id,
            endpoint="PAPER:POST:/v2/orders",
            observation_type="ORDER_ACKNOWLEDGMENT",
            response=response,
        )

    def order(self, *, instruction_id: str, broker_order_id: str) -> ExternalPaperBrokerEvidence:
        response = self._call("GET", f"/v2/orders/{broker_order_id}")
        return _evidence(
            instruction_id=instruction_id,
            endpoint="PAPER:GET:/v2/orders/{order_id}",
            observation_type="ORDER_STATUS",
            response=response,
        )

    def account(self) -> ExternalPaperBrokerEvidence:
        return _account_evidence(self._call("GET", "/v2/account"))

    def orders(self) -> tuple[ExternalPaperBrokerEvidence, ...]:
        response = self._call("GET", "/v2/orders?status=all&limit=500&direction=desc")
        return _collection_evidence(
            endpoint="PAPER:GET:/v2/orders",
            observation_type="ORDER_LIST_ITEM",
            response=response,
            selected_keys=_ORDER_KEYS,
        )

    def positions(self) -> tuple[ExternalPaperBrokerEvidence, ...]:
        response = self._call("GET", "/v2/positions")
        return _collection_evidence(
            endpoint="PAPER:GET:/v2/positions",
            observation_type="POSITION_SNAPSHOT_ITEM",
            response=response,
            selected_keys=_POSITION_KEYS,
        )

    def position(self, *, symbol: str) -> ExternalPaperBrokerEvidence | None:
        """Read one exact PAPER position; an absent position is normal evidence."""
        try:
            response = self._call("GET", f"/v2/positions/{symbol}")
        except ExternalPaperBrokerError as error:
            if str(error) == "HTTP_404":
                return None
            raise
        return _position_evidence(response)

    def activities(self) -> tuple[ExternalPaperBrokerEvidence, ...]:
        response = self._call(
            "GET", "/v2/account/activities?activity_types=FILL&direction=desc&page_size=100"
        )
        return _collection_evidence(
            endpoint="PAPER:GET:/v2/account/activities",
            observation_type="FILL_ACTIVITY_ITEM",
            response=response,
            selected_keys=_ACTIVITY_KEYS,
        )

    def cancel(self, *, instruction_id: str, broker_order_id: str) -> ExternalPaperBrokerEvidence:
        response = self._call("DELETE", f"/v2/orders/{broker_order_id}")
        return _evidence(
            instruction_id=instruction_id,
            endpoint="PAPER:DELETE:/v2/orders/{order_id}",
            observation_type="ORDER_CANCELLATION",
            response=response,
        )

    def _call(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> AlpacaPaperResponse:
        endpoint = _endpoint_identity(method, path)
        if endpoint not in PAPER_ENDPOINTS:
            raise ExternalPaperBrokerError("PAPER_ENDPOINT_POLICY_REJECTED")
        if not self.credentials.available:
            raise ExternalPaperBrokerError("PAPER_CREDENTIALS_REQUIRED")
        headers = {
            "APCA-API-KEY-ID": self.credentials.key_id or "",
            "APCA-API-SECRET-KEY": self.credentials.secret or "",
            "Accept": "application/json",
        }
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        if method == "GET":
            self._network_reads += 1
        else:
            self._network_writes += 1
        return self._request(method, f"{PAPER_BASE_URL}{path}", headers, data)


def _endpoint_identity(method: str, path: str) -> str:
    path = path.split("?", 1)[0]
    if path.startswith("/v2/orders/"):
        path = "/v2/orders/{order_id}"
    if path.startswith("/v2/positions/"):
        path = "/v2/positions/{symbol}"
    return f"{method}:{path}"


def _urllib_request(
    method: str, url: str, headers: dict[str, str], data: bytes | None
) -> AlpacaPaperResponse:
    if not url.startswith(f"{PAPER_BASE_URL}/"):
        raise ExternalPaperBrokerError("PAPER_ENDPOINT_POLICY_REJECTED")
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            parsed = json.loads(response.read().decode() or "{}")
            return AlpacaPaperResponse(
                response.status,
                parsed if isinstance(parsed, (dict, list)) else {},
                response.headers.get("X-Request-ID"),
            )
    except HTTPError as error:
        raise ExternalPaperBrokerError(f"HTTP_{error.code}") from error
    except URLError as error:
        raise ExternalPaperBrokerError("TRANSPORT_UNAVAILABLE") from error


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> Request | None:
        return None


def _evidence(
    *,
    instruction_id: str,
    endpoint: str,
    observation_type: str,
    response: AlpacaPaperResponse,
) -> ExternalPaperBrokerEvidence:
    payload = response.payload if isinstance(response.payload, dict) else {}
    selected = _select(payload, _ORDER_KEYS)
    reported = payload.get("updated_at") or payload.get("submitted_at")
    timestamp = datetime.now(tz=UTC)
    return ExternalPaperBrokerEvidence.create(
        instruction_id=instruction_id,
        endpoint_identity=endpoint,
        observation_type=observation_type,
        observed_at=timestamp,
        broker_reported_at=datetime.fromisoformat(str(reported).replace("Z", "+00:00"))
        if reported
        else None,
        request_id=response.request_id,
        sanitized_payload=selected,
    )


_ORDER_KEYS = (
    "id",
    "client_order_id",
    "symbol",
    "side",
    "status",
    "qty",
    "filled_qty",
    "filled_avg_price",
    "fee",
    "submitted_at",
    "filled_at",
    "canceled_at",
    "updated_at",
)
_POSITION_KEYS = (
    "symbol",
    "qty",
    "avg_entry_price",
    "market_value",
    "cost_basis",
    "side",
    "asset_class",
    "updated_at",
)
_ACTIVITY_KEYS = (
    "id",
    "activity_type",
    "transaction_time",
    "order_id",
    "symbol",
    "side",
    "qty",
    "price",
)
_ACCOUNT_KEYS = (
    "status",
    "cash",
    "buying_power",
    "equity",
    "trading_blocked",
    "account_blocked",
    "trade_suspended_by_user",
)


def _select(payload: dict[str, Any], keys: tuple[str, ...]) -> dict[str, str | None]:
    return {
        key: str(payload[key]) if payload.get(key) is not None else None
        for key in keys
        if key in payload
    }


def _account_evidence(response: AlpacaPaperResponse) -> ExternalPaperBrokerEvidence:
    payload = response.payload if isinstance(response.payload, dict) else {}
    reported = payload.get("updated_at")
    return ExternalPaperBrokerEvidence.create(
        instruction_id=None,
        endpoint_identity="PAPER:GET:/v2/account",
        observation_type="ACCOUNT_READINESS",
        observed_at=datetime.now(tz=UTC),
        broker_reported_at=datetime.fromisoformat(str(reported).replace("Z", "+00:00"))
        if reported
        else None,
        request_id=response.request_id,
        sanitized_payload=_select(payload, _ACCOUNT_KEYS),
    )


def _position_evidence(response: AlpacaPaperResponse) -> ExternalPaperBrokerEvidence:
    payload = response.payload if isinstance(response.payload, dict) else {}
    reported = payload.get("updated_at")
    return ExternalPaperBrokerEvidence.create(
        instruction_id=None,
        endpoint_identity="PAPER:GET:/v2/positions/{symbol}",
        observation_type="POSITION_SNAPSHOT_ITEM",
        observed_at=datetime.now(tz=UTC),
        broker_reported_at=datetime.fromisoformat(str(reported).replace("Z", "+00:00"))
        if reported
        else None,
        request_id=response.request_id,
        sanitized_payload=_select(payload, _POSITION_KEYS),
    )


def _collection_evidence(
    *,
    endpoint: str,
    observation_type: str,
    response: AlpacaPaperResponse,
    selected_keys: tuple[str, ...],
) -> tuple[ExternalPaperBrokerEvidence, ...]:
    payload = response.payload if isinstance(response.payload, list) else []
    observed = datetime.now(tz=UTC)
    evidence: list[ExternalPaperBrokerEvidence] = []
    for item in payload:
        reported = item.get("updated_at") or item.get("transaction_time")
        evidence.append(
            ExternalPaperBrokerEvidence.create(
                instruction_id=None,
                endpoint_identity=endpoint,
                observation_type=observation_type,
                observed_at=observed,
                broker_reported_at=datetime.fromisoformat(str(reported).replace("Z", "+00:00"))
                if reported
                else None,
                request_id=response.request_id,
                sanitized_payload=_select(item, selected_keys),
            )
        )
    return tuple(evidence)
