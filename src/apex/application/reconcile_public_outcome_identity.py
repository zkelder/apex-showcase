"""Strict, versioned reconciliation for public final-market outcome evidence.

The exact final market endpoint can omit identity fields that were present in
the earlier active-market response.  This module deliberately permits only an
exact event lookup with exact nested-market membership to supply that missing
context.  It owns no provider client, persistence, prediction mutation, or
financial authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from apex.domain.public_event import EventSettlementSource
from apex.domain.values import ValueValidationError, content_digest

PUBLIC_OUTCOME_IDENTITY_RECONCILIATION_VERSION = "public-outcome-identity-reconciliation-v1"


class PublicOutcomeReconciliationState(StrEnum):
    """The only states yielded before a durable outcome fact is constructed."""

    PENDING_EXACT_EVENT = "PENDING_EXACT_EVENT"
    UNRESOLVED = "UNRESOLVED"
    INVALID = "INVALID"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True, slots=True)
class RetainedPublicOutcomeIdentity:
    """Prediction-time identity recovered from frozen exact enrichment evidence."""

    market_ticker: str
    event_ticker: str
    series_ticker: str
    rule_content_hash: str
    settlement_source_hash: str

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("market ticker", self.market_ticker, 300),
            ("event ticker", self.event_ticker, 300),
            ("series ticker", self.series_ticker, 300),
        ):
            if not isinstance(value, str) or not value or len(value) > maximum:
                raise ValueValidationError(f"retained {label} must be non-empty")
        for label, value in (
            ("rule content hash", self.rule_content_hash),
            ("settlement source hash", self.settlement_source_hash),
        ):
            if not isinstance(value, str) or len(value) != 64:
                raise ValueValidationError(f"retained {label} must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class PublicOutcomeIdentityReconciliation:
    """A non-financial final-outcome compatibility decision with exact provenance."""

    state: PublicOutcomeReconciliationState
    resolved_outcome: str | None
    rule_content_hash: str | None
    event_ticker: str
    series_ticker: str
    reason_codes: tuple[str, ...]
    reconciliation_version: str = PUBLIC_OUTCOME_IDENTITY_RECONCILIATION_VERSION

    @property
    def requires_exact_event_lookup(self) -> bool:
        return self.state is PublicOutcomeReconciliationState.PENDING_EXACT_EVENT


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _rule_hash(value: object) -> str | None:
    rule = _text(value)
    return sha256(rule.encode("utf-8")).hexdigest() if rule is not None else None


def _result(value: Mapping[str, object]) -> str | None:
    raw = value.get("result") or value.get("settlement_value")
    if not isinstance(raw, str):
        return None
    normalized = raw.upper()
    return normalized if normalized in {"YES", "NO"} else None


def _event_settlement_source_hash(event: Mapping[str, object]) -> str | None:
    raw_sources = event.get("settlement_sources")
    if not isinstance(raw_sources, list):
        return None
    sources: list[EventSettlementSource] = []
    for raw_source in raw_sources:
        if not isinstance(raw_source, Mapping):
            return None
        name = _text(raw_source.get("name"))
        url = _text(raw_source.get("url"))
        if name is None or url is None:
            return None
        sources.append(EventSettlementSource(name=name, url=url))
    return content_digest(tuple(sources))


def _invalid(
    retained: RetainedPublicOutcomeIdentity,
    *,
    rule_content_hash: str | None,
    reason_codes: list[str],
) -> PublicOutcomeIdentityReconciliation:
    return PublicOutcomeIdentityReconciliation(
        state=PublicOutcomeReconciliationState.INVALID,
        resolved_outcome=None,
        rule_content_hash=rule_content_hash,
        event_ticker=retained.event_ticker,
        series_ticker=retained.series_ticker,
        reason_codes=tuple(sorted(set(reason_codes))),
    )


def reconcile_public_final_outcome(
    *,
    retained: RetainedPublicOutcomeIdentity,
    final_market: object,
    exact_event: object | None,
    exact_series: object | None = None,
) -> PublicOutcomeIdentityReconciliation:
    """Reconcile one final exact-market response against frozen enriched identity.

    A missing final event or series field is not accepted by itself.  It remains
    pending until the caller supplies the response from the exact retained event
    endpoint.  That response must name the retained event, contain exactly one
    matching nested market, and carry the retained series, rule, and settlement
    source identities.  An optional exact series response can add one more
    strict identity check; no title, ticker-prefix, or fuzzy inference exists.
    """

    reasons = ["PUBLIC_OUTCOME_IDENTITY_RECONCILIATION_V1", "PUBLIC_EXACT_MARKET_GET"]
    if not isinstance(final_market, Mapping):
        return _invalid(
            retained, rule_content_hash=None, reason_codes=[*reasons, "MARKET_PAYLOAD_INVALID"]
        )

    final_rule_hash = _rule_hash(final_market.get("rules_primary"))
    lifecycle = _text(final_market.get("status"))
    if lifecycle not in {"finalized", "settled"}:
        return PublicOutcomeIdentityReconciliation(
            state=PublicOutcomeReconciliationState.UNRESOLVED,
            resolved_outcome=None,
            rule_content_hash=final_rule_hash,
            event_ticker=retained.event_ticker,
            series_ticker=retained.series_ticker,
            reason_codes=tuple(sorted((*reasons, "PUBLIC_MARKET_NOT_FINAL"))),
        )

    if _text(final_market.get("ticker")) != retained.market_ticker:
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "MARKET_IDENTITY_MISMATCH"],
        )
    final_event_ticker = _text(final_market.get("event_ticker"))
    if final_event_ticker is not None and final_event_ticker != retained.event_ticker:
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "EVENT_IDENTITY_MISMATCH"],
        )
    final_series_ticker = _text(final_market.get("series_ticker"))
    if final_series_ticker is not None and final_series_ticker != retained.series_ticker:
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "SERIES_IDENTITY_MISMATCH"],
        )
    if final_rule_hash is None:
        return _invalid(
            retained,
            rule_content_hash=None,
            reason_codes=[*reasons, "RULE_IDENTITY_MISSING"],
        )
    if final_rule_hash != retained.rule_content_hash:
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "RULE_IDENTITY_MISMATCH"],
        )
    resolved_outcome = _result(final_market)
    if resolved_outcome is None:
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "UNSUPPORTED_FINAL_RESULT"],
        )

    if final_event_ticker is None:
        reasons.append("EVENT_IDENTITY_MISSING_FINAL_RESPONSE")
    if final_series_ticker is None:
        reasons.append("SERIES_IDENTITY_MISSING_FINAL_RESPONSE")
    if exact_event is None:
        return PublicOutcomeIdentityReconciliation(
            state=PublicOutcomeReconciliationState.PENDING_EXACT_EVENT,
            resolved_outcome=None,
            rule_content_hash=final_rule_hash,
            event_ticker=retained.event_ticker,
            series_ticker=retained.series_ticker,
            reason_codes=tuple(sorted((*reasons, "EXACT_EVENT_LOOKUP_REQUIRED"))),
        )
    if not isinstance(exact_event, Mapping):
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "EVENT_ENRICHMENT_PAYLOAD_INVALID"],
        )

    reasons.extend(("PUBLIC_EXACT_EVENT_GET", "PUBLIC_EXACT_NESTED_MARKET_MEMBERSHIP"))
    if _text(exact_event.get("event_ticker")) != retained.event_ticker:
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "EVENT_IDENTITY_MISMATCH"],
        )
    if _text(exact_event.get("series_ticker")) != retained.series_ticker:
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "SERIES_IDENTITY_MISMATCH"],
        )
    raw_nested_markets = exact_event.get("markets")
    if not isinstance(raw_nested_markets, list):
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "EVENT_NESTED_MARKETS_MISSING"],
        )
    nested_matches = tuple(
        value
        for value in raw_nested_markets
        if isinstance(value, Mapping)
        and _text(value.get("ticker")) == retained.market_ticker
        and _text(value.get("event_ticker")) == retained.event_ticker
    )
    if len(nested_matches) != 1:
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[
                *reasons,
                "EVENT_NESTED_MARKET_MISSING"
                if not nested_matches
                else "EVENT_NESTED_MARKET_AMBIGUOUS",
            ],
        )
    nested_rule_hash = _rule_hash(nested_matches[0].get("rules_primary"))
    if nested_rule_hash is None:
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "RULE_IDENTITY_MISSING"],
        )
    if nested_rule_hash != retained.rule_content_hash:
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "RULE_IDENTITY_MISMATCH"],
        )
    settlement_source_hash = _event_settlement_source_hash(exact_event)
    if settlement_source_hash != retained.settlement_source_hash:
        return _invalid(
            retained,
            rule_content_hash=final_rule_hash,
            reason_codes=[*reasons, "SETTLEMENT_SOURCE_MISMATCH"],
        )
    if exact_series is not None:
        if (
            not isinstance(exact_series, Mapping)
            or _text(exact_series.get("ticker")) != retained.series_ticker
        ):
            return _invalid(
                retained,
                rule_content_hash=final_rule_hash,
                reason_codes=[*reasons, "SERIES_IDENTITY_MISMATCH"],
            )
        reasons.append("PUBLIC_EXACT_SERIES_GET")
    if final_event_ticker is None:
        reasons.append("EVENT_IDENTITY_RECONCILED_FROM_EXACT_EVENT")
    if final_series_ticker is None:
        reasons.append("SERIES_IDENTITY_RECONCILED_FROM_EXACT_EVENT")
    return PublicOutcomeIdentityReconciliation(
        state=PublicOutcomeReconciliationState.RESOLVED,
        resolved_outcome=resolved_outcome,
        rule_content_hash=final_rule_hash,
        event_ticker=retained.event_ticker,
        series_ticker=retained.series_ticker,
        reason_codes=tuple(sorted(set(reasons))),
    )
