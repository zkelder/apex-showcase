"""Kalshi Settlement & Resolution Evidence Maturation v1.

Closes a Kalshi outcome/settlement evidence gap this codebase's own
conservatism had already surfaced (see ``domain.counterfactual_outcome``'s
"Kalshi (Phase 8)" docstring section and ``domain.kalshi_baseline_quote``'s
module docstring): every Kalshi follow-up observation already retains a
``settlement_result`` string (``domain.counterfactual_followup.
DecisionFollowUpObservation.settlement_result``), sourced from a genuine,
bounded, unauthenticated public Kalshi read (``adapters.
counterfactual_followup_sources.KalshiFollowUpPublicMarketSource``) -- but
until this milestone, that read trusted the provider's raw ``result`` field
regardless of the market's own ``status``. Confirmed by direct inspection of
the real adapter code (``counterfactual_followup_sources._settlement_
result``): it parses ``market["result"]`` with **no gate on
``market["status"]`` at all**.

Kalshi's own documented lifecycle (``docs/KALSHI_PUBLIC_API_FACTS.md``)
names market states beyond initialization that this codebase had never
modeled distinctly: ``determined`` (a result exists but has not yet cleared
its dispute window), ``disputed`` (a determined result under formal
challenge), and ``amended`` (a corrected result, presumably still moving
toward re-``finalized``) -- alongside the one genuinely authoritative
terminal state, ``finalized``. The existing complement-scan canonical status
map (``adapters.kalshi_public._CANONICAL_MARKET_STATUS``) deliberately
collapses all four post-close states into one ``"closed"`` bucket --
correct and sufficient for its own purpose (a complement scanner only needs
to know "not tradeable right now"), but wrong to reuse for a
resolution-evidence purpose, where exactly those collapsed distinctions are
what "is this result actually safe to trust" turns on.

This module is deliberately independent of that map (never imports or
extends it) and introduces its own, narrower, resolution-purpose
classification, ``KalshiResolutionState`` -- because collapsing determined/
disputed/amended together for *this* purpose would silently reintroduce the
exact governing-constraint risk this milestone exists to close: a
``determined``-but-not-yet-``finalized`` (and, per Kalshi's own vocabulary,
potentially still-``disputed``/``amended``) result treated as if Apex
already knew the market's true outcome.

``KalshiMarketResolutionFact`` is the immutable, replay-safe evidence unit
this module adds. Its identity (``resolution_fact_id``) is deliberately
**content-derived** -- a documented departure from this codebase's more
common "extrinsic key (e.g. ``decision_id``) plus a separate
``canonical_hash`` for conflict detection" idiom (see ``domain.
kalshi_baseline_quote.KalshiBaselineQuoteEvidence``, whose ``quote_id`` is a
pure function of ``(policy_version, decision_id)``). A resolution fact has
no natural single-owner extrinsic key: many decisions (and many follow-up
observations, across horizons) can legitimately reference the very same
Kalshi market, and the same market can genuinely be observed many times
before ("still closed, no result"), during ("determined, awaiting dispute
window"), and after ("finalized") its real resolution. Deriving
``resolution_fact_id`` from the market's own observed content (ticker + raw
status + raw result + settlement value + settlement/close timestamps) gives
idempotent ingestion for free -- an identical repeated provider response
always re-derives the identical id, so re-saving it is a genuine no-op --
while a real change (the market progressing from ``determined`` to
``finalized``, or Kalshi later correcting a result) always derives a
*different* id, so it lands as a new, additional row rather than mutating
or replacing the old one. Every prior observation therefore remains exactly
as retained forever (immutable, DB-trigger-enforced, matching every other
evidence table in this codebase) -- both the historical observation lineage
and the current authoritative truth (``latest_fact``, point-in-time
bounded) stay honestly available, satisfying this milestone's explicit
correction-handling requirement without ever rewriting history.

Only ``KalshiResolutionState.FINALIZED`` authorizes trusting a result:
``resolved_result``/``settlement_value_dollars``/``settlement_ts`` are all
``None`` on every fact whose ``resolution_state`` is anything else, even
when the raw provider payload already carried a non-null ``result`` (that
raw value is separately retained, unconsumed, as ``raw_result`` -- honest
lineage, never authoritative). This is a deliberately conservative reading
of a lifecycle this codebase cannot independently verify beyond what
``docs/KALSHI_PUBLIC_API_FACTS.md`` already documents: nothing in the public
API facts proves ``result`` is only ever populated once ``finalized``, so
this module does not assume it -- Governing Constraint: Apex must never
treat an unresolved Kalshi market as if its outcome were known.

Grants no capital or execution authority: ``authority`` is always
``"NONE"``, exactly like every other evidence type in this codebase. This
module owns no provider client, credential, or network capability of its
own -- it is fed already-captured raw fields by callers that already
performed the one genuine, bounded, read-only, unauthenticated Kalshi
request this evidence piggybacks on (``application.counterfactual_followup.
SyncFollowUpObservations``); this milestone adds no new HTTP capability or
additional request volume of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.research_evidence import EvidenceClass
from apex.domain.values import (
    ValueValidationError,
    canonical_timestamp,
    content_digest,
    deterministic_id,
    parse_decimal,
)

KALSHI_RESOLUTION_POLICY_V1 = "KALSHI_RESOLUTION_POLICY_V1"
KALSHI_RESOLUTION_WARNING = (
    "KALSHI RESOLUTION EVIDENCE — AUTHORITATIVE ONLY AT FINALIZED — "
    "NO EXECUTION OR ALLOCATION AUTHORITY — NOT A LIVE QUOTE"
)
# Stamped as ``DecisionFollowUpObservation.settlement_source`` whenever a
# follow-up read is answered from an already-retained resolution fact
# instead of a fresh live request -- lets a reader distinguish "this cycle
# made a genuine request" from "this cycle reused already-retained,
# already-authoritative evidence" without inspecting request counts.
KALSHI_RESOLUTION_FACT_SOURCE = "KALSHI_RESOLUTION_FACT_CACHE"


class KalshiResolutionError(ValueError):
    """Raised when retained resolution-fact inputs are invalid or conflict
    with already-retained evidence."""


class KalshiResolutionState(StrEnum):
    """Non-collapsing classification of a Kalshi market's raw provider
    ``status`` for resolution-evidence purposes only -- see module
    docstring for why this is deliberately not ``adapters.kalshi_public.
    _CANONICAL_MARKET_STATUS``.

    Only ``FINALIZED`` is authoritative enough to trust a settlement
    result; every other member -- including ``RESULT_DETERMINED``, which
    can carry a preliminary result on the real Kalshi API -- is treated as
    not yet safe to treat as Apex's knowledge of the market's true
    outcome."""

    NOT_YET_CLOSED = "NOT_YET_CLOSED"
    CLOSED_AWAITING_RESULT = "CLOSED_AWAITING_RESULT"
    RESULT_DETERMINED = "RESULT_DETERMINED"
    DISPUTED = "DISPUTED"
    AMENDED = "AMENDED"
    FINALIZED = "FINALIZED"
    UNRECOGNIZED = "UNRECOGNIZED"


# Maps Kalshi's own documented raw ``status`` lifecycle values
# (``docs/KALSHI_PUBLIC_API_FACTS.md``: "initialized, active, inactive,
# closed, determined, disputed, amended, and finalized", plus the
# GET-/markets-filter vocabulary "unopened, open, paused, closed, settled")
# to ``KalshiResolutionState``. Deliberately local to this module -- never
# shared with, and never derived from, ``adapters.kalshi_public.
# _CANONICAL_MARKET_STATUS`` (see module docstring).
_RAW_STATUS_TO_RESOLUTION_STATE: dict[str, KalshiResolutionState] = {
    "initialized": KalshiResolutionState.NOT_YET_CLOSED,
    "unopened": KalshiResolutionState.NOT_YET_CLOSED,
    "active": KalshiResolutionState.NOT_YET_CLOSED,
    "open": KalshiResolutionState.NOT_YET_CLOSED,
    "inactive": KalshiResolutionState.NOT_YET_CLOSED,
    "paused": KalshiResolutionState.NOT_YET_CLOSED,
    "closed": KalshiResolutionState.CLOSED_AWAITING_RESULT,
    "determined": KalshiResolutionState.RESULT_DETERMINED,
    "disputed": KalshiResolutionState.DISPUTED,
    "amended": KalshiResolutionState.AMENDED,
    "finalized": KalshiResolutionState.FINALIZED,
    "settled": KalshiResolutionState.FINALIZED,
}


def classify_kalshi_resolution_state(provider_status: str | None) -> KalshiResolutionState:
    """Fail-closed: any raw status this module does not recognize --
    including ``None`` or empty -- classifies ``UNRECOGNIZED``, never a
    guess at which known state it might resemble (Part 13: fail closed
    when outcome truth is ambiguous)."""

    if not provider_status:
        return KalshiResolutionState.UNRECOGNIZED
    return _RAW_STATUS_TO_RESOLUTION_STATE.get(
        provider_status.strip().lower(), KalshiResolutionState.UNRECOGNIZED
    )


def _normalized_result(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    return normalized or None


def _aware(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return value
    if value.tzinfo is None or value.utcoffset() is None:
        raise KalshiResolutionError(f"KALSHI_RESOLUTION_{name}_NOT_AWARE")
    return value


@dataclass(frozen=True, slots=True)
class KalshiMarketResolutionFact:
    """One immutable, append-only, content-addressed observation of a
    single Kalshi market's resolution/settlement state. See module
    docstring for the full identity/idempotency/correction-handling
    rationale. Never constructed directly by application/adapter code --
    always through ``build_kalshi_resolution_fact``, the one place the
    authoritative-gating rule below is enforced."""

    resolution_fact_id: str
    market_ticker: str
    event_ticker: str | None
    provider_status: str
    resolution_state: KalshiResolutionState
    raw_result: str | None
    resolved_result: str | None
    settlement_value_dollars: Decimal | None
    settlement_ts: datetime | None
    close_time: datetime | None
    source_endpoint_identity: str
    evidence_class: EvidenceClass
    captured_at: datetime
    run_id: str
    canonical_hash: str
    authority: str = "NONE"

    def __post_init__(self) -> None:
        if not self.resolution_fact_id or not self.market_ticker:
            raise ValueValidationError("resolution fact identity must not be empty")
        if not self.provider_status:
            raise ValueValidationError("resolution fact provider status must not be empty")
        if not self.source_endpoint_identity or not self.run_id:
            raise ValueValidationError("resolution fact lineage must not be empty")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueValidationError("resolution fact captured_at must be timezone-aware")
        if self.settlement_ts is not None and (
            self.settlement_ts.tzinfo is None or self.settlement_ts.utcoffset() is None
        ):
            raise ValueValidationError("resolution fact settlement_ts must be timezone-aware")
        if self.close_time is not None and (
            self.close_time.tzinfo is None or self.close_time.utcoffset() is None
        ):
            raise ValueValidationError("resolution fact close_time must be timezone-aware")
        # Governing Constraint, restated as an invariant a caller cannot
        # bypass by hand-constructing this frozen dataclass directly:
        # nothing below FINALIZED may carry an authoritative field, no
        # matter what the caller passes.
        authoritative = self.resolution_state is KalshiResolutionState.FINALIZED
        if not authoritative and (
            self.resolved_result is not None
            or self.settlement_value_dollars is not None
            or self.settlement_ts is not None
        ):
            raise ValueValidationError(
                "resolution fact cannot carry authoritative fields below FINALIZED"
            )
        if self.resolved_result is not None and self.resolved_result != self.raw_result:
            raise ValueValidationError(
                "resolution fact resolved_result must echo raw_result exactly when authoritative"
            )
        if self.authority != "NONE":
            raise ValueValidationError("resolution fact authority must be NONE")
        if len(self.canonical_hash) != 64:
            raise ValueValidationError("resolution fact canonical_hash must be a sha256 hex digest")


def kalshi_resolution_fact_id(
    *,
    market_ticker: str,
    provider_status: str,
    raw_result: str | None,
    settlement_value_dollars: Decimal | None,
    settlement_ts: datetime | None,
    close_time: datetime | None,
) -> str:
    """Content-derived identity -- see module docstring for why this is
    deliberately not a function of an extrinsic key. Excludes
    ``event_ticker``/``source_endpoint_identity``/``evidence_class``
    deliberately: those describe *how* a fact was captured, not *what* was
    observed, so two reads of the identical substantive outcome (e.g. one
    via the event endpoint, a later one via the single-market fallback)
    still idempotently collide on this id."""

    return deterministic_id(
        "kalshi_resolution_fact",
        {
            "market_ticker": market_ticker,
            "provider_status": provider_status,
            "raw_result": raw_result,
            "settlement_value_dollars": (
                None if settlement_value_dollars is None else str(settlement_value_dollars)
            ),
            "settlement_ts": (
                None if settlement_ts is None else canonical_timestamp(settlement_ts)
            ),
            "close_time": None if close_time is None else canonical_timestamp(close_time),
        },
    )


def build_kalshi_resolution_fact(
    *,
    market_ticker: str,
    event_ticker: str | None,
    provider_status: str,
    raw_result: str | None,
    settlement_value_dollars: Decimal | str | None,
    settlement_ts: datetime | None,
    close_time: datetime | None,
    source_endpoint_identity: str,
    evidence_class: EvidenceClass,
    captured_at: datetime,
    run_id: str,
) -> KalshiMarketResolutionFact:
    """Pure; performs no I/O. Every raw field must already have been parsed
    from a genuine bounded Kalshi public read by the caller -- this builder
    never contacts a provider and never infers resolution from price,
    mirroring ``domain.counterfactual_followup.
    build_kalshi_market_status_observation``'s own "never infer" discipline.

    The single place the authoritative-gating rule is enforced: whenever
    ``classify_kalshi_resolution_state(provider_status)`` is anything other
    than ``FINALIZED``, ``resolved_result``/``settlement_value_dollars``/
    ``settlement_ts`` on the returned fact are unconditionally ``None`` --
    regardless of what the caller passed in -- even though ``raw_result``
    (and, separately, ``close_time``, which is a factual trading-close
    timestamp rather than a resolution claim) are retained verbatim
    either way, for honest lineage."""

    if not market_ticker:
        raise KalshiResolutionError("KALSHI_RESOLUTION_MARKET_TICKER_EMPTY")
    if not provider_status:
        raise KalshiResolutionError("KALSHI_RESOLUTION_PROVIDER_STATUS_EMPTY")
    if not source_endpoint_identity or not run_id:
        raise KalshiResolutionError("KALSHI_RESOLUTION_LINEAGE_EMPTY")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise KalshiResolutionError("KALSHI_RESOLUTION_CAPTURED_AT_NOT_AWARE")
    _aware(settlement_ts, "SETTLEMENT_TS")
    _aware(close_time, "CLOSE_TIME")

    resolution_state = classify_kalshi_resolution_state(provider_status)
    normalized_raw_result = _normalized_result(raw_result)
    authoritative = resolution_state is KalshiResolutionState.FINALIZED
    resolved_result = normalized_raw_result if authoritative else None
    settlement_value = (
        parse_decimal(settlement_value_dollars)
        if authoritative and settlement_value_dollars is not None
        else None
    )
    settlement_moment = settlement_ts if authoritative else None

    fact_id = kalshi_resolution_fact_id(
        market_ticker=market_ticker,
        provider_status=provider_status,
        raw_result=normalized_raw_result,
        settlement_value_dollars=settlement_value,
        settlement_ts=settlement_moment,
        close_time=close_time,
    )
    canonical_hash = content_digest(
        {
            "resolution_fact_id": fact_id,
            "market_ticker": market_ticker,
            "event_ticker": event_ticker,
            "provider_status": provider_status,
            "resolution_state": resolution_state.value,
            "raw_result": normalized_raw_result,
            "resolved_result": resolved_result,
            "settlement_value_dollars": (
                None if settlement_value is None else str(settlement_value)
            ),
            "settlement_ts": (
                None if settlement_moment is None else canonical_timestamp(settlement_moment)
            ),
            "close_time": None if close_time is None else canonical_timestamp(close_time),
            "source_endpoint_identity": source_endpoint_identity,
            "evidence_class": evidence_class.value,
        }
    )
    return KalshiMarketResolutionFact(
        resolution_fact_id=fact_id,
        market_ticker=market_ticker,
        event_ticker=event_ticker,
        provider_status=provider_status,
        resolution_state=resolution_state,
        raw_result=normalized_raw_result,
        resolved_result=resolved_result,
        settlement_value_dollars=settlement_value,
        settlement_ts=settlement_moment,
        close_time=close_time,
        source_endpoint_identity=source_endpoint_identity,
        evidence_class=evidence_class,
        captured_at=captured_at,
        run_id=run_id,
        canonical_hash=canonical_hash,
        authority="NONE",
    )
