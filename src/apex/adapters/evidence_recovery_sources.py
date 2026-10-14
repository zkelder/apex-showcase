"""Adapters composing Historical Evidence Gap Recovery v1 over retained state.

Everything here reads retained evidence or composes the *existing* historical
capture pipeline and public Coinbase candle adapter. Nothing writes outside the
existing canonical evidence tables plus the recovery ledger, and nothing uses a
provider write, an account, or a credential beyond the already-reviewed
read-only Alpaca market-data path.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Any

from apex.adapters.xnys_calendar import ReviewedXNYSCalendar
from apex.application.crypto_universe import CapturedObservationPage
from apex.application.evidence_recovery import (
    CryptoCandleValue,
    CryptoFetchOutcome,
    CryptoProductCoverage,
    ExchangeSession,
    SecuritiesFetchOutcome,
)
from apex.domain.crypto_universe import CryptoProductObservation
from apex.domain.evidence_recovery import RECOVERY_CAPTURE_ID_PREFIX
from apex.domain.research_evidence import EvidenceClass

RECOVERY_CHUNK_SIZE = 20
COINBASE_CANDLE_MINIMUM_SPAN_DAYS = 91
COINBASE_CANDLE_MAXIMUM_SPAN_DAYS = 300


class XnysExchangeCalendar:
    """Trading sessions from the reviewed XNYS calendar -- never weekdays."""

    def sessions(self, *, start: date, end: date) -> tuple[ExchangeSession, ...]:
        calendar = ReviewedXNYSCalendar(start_date=start.isoformat(), end_date=end.isoformat())
        return tuple(
            ExchangeSession(date.fromisoformat(item.session_date), item.closes_at)
            for item in calendar.sessions
        )


class HistoricalPipelineSecuritiesFetcher:
    """Recovery fetch through the *existing* plan -> chunk -> canonicalize
    pipeline (``RunHistoricalCaptureChunk``), bounded by a chunk budget. A
    chunk is attempted at most once per recovery run; there is no retry loop."""

    def __init__(
        self,
        *,
        historical_repository: Any,
        identity_source: Any,
        real_repository: Any,
        securities_repository: Any,
        provider: Any,
        artifacts: Any,
        dataset: Any,
        chunk_size: int = RECOVERY_CHUNK_SIZE,
    ) -> None:
        self._historical = historical_repository
        self._identity = identity_source
        self._real = real_repository
        self._securities = securities_repository
        self._provider = provider
        self._artifacts = artifacts
        self._dataset = dataset
        self._chunk_size = chunk_size

    def fetch(
        self,
        *,
        symbols: tuple[str, ...],
        etf_symbols: frozenset[str],
        start: date,
        end: date,
        run_key: str,
        chunk_budget: int,
        requested_at: datetime,
    ) -> SecuritiesFetchOutcome:
        from apex.adapters.alpaca_canonicalization import HistoricalAlpacaCanonicalizer
        from apex.application.historical_market_data import (
            CreateHistoricalCapturePlan,
            CreateHistoricalCapturePlanCommand,
            RunHistoricalCaptureChunk,
            RunHistoricalCaptureChunkCommand,
        )
        from apex.application.real_securities_data import FetchSecuritiesProviderOperation
        from apex.domain.historical_market_data import HistoricalCaptureChunkState

        configuration = self._real.active_configuration()
        if configuration is None:
            return SecuritiesFetchOutcome(
                "FAILED", 0, 0, 0, failure_code="REAL_SECURITIES_PROVIDER_CONFIGURATION_NOT_ACTIVE"
            )
        by_symbol: dict[str, int] = {}
        for identity in self._identity.latest_stable_identities():
            by_symbol[identity.current_symbol] = by_symbol.get(identity.current_symbol, 0) + 1
        resolvable = tuple(item for item in symbols if by_symbol.get(item) == 1)
        unresolved = tuple(sorted(set(symbols) - set(resolvable)))
        if not resolvable:
            return SecuritiesFetchOutcome(
                "FAILED",
                0,
                0,
                0,
                failure_code="NO_RECOVERABLE_INSTRUMENT_IDENTITY",
                unresolved_symbols=unresolved,
            )
        chunk_size = max(1, min(self._chunk_size, configuration.maximum_symbols_per_operation))
        chunk_total = -(-len(resolvable) // chunk_size)
        key = "evidence-recovery:" + hashlib.sha256(run_key.encode()).hexdigest()[:48]
        plan = CreateHistoricalCapturePlan(
            repository=self._historical, identity_source=self._identity
        ).execute(
            CreateHistoricalCapturePlanCommand(
                symbols=resolvable,
                etf_symbols=frozenset(etf_symbols) & frozenset(resolvable),
                provider="ALPACA_MARKET_DATA_V2",
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                chunk_size=chunk_size,
                maximum_instruments=len(resolvable),
                maximum_requests=chunk_total * configuration.maximum_pages_per_operation,
                maximum_bytes=configuration.maximum_response_bytes * chunk_total,
                evidence_class="HISTORICAL_PUBLIC",
                operator_action_id="evidence-gap-recovery-v1",
                idempotency_key=key,
                created_at=requested_at,
            )
        )
        runner = RunHistoricalCaptureChunk(
            repository=self._historical,
            fetch_operation=FetchSecuritiesProviderOperation(
                repository=self._real, provider=self._provider, artifacts=self._artifacts
            ),
            canonicalizer=HistoricalAlpacaCanonicalizer(
                securities_repository=self._securities, dataset=self._dataset
            ),
            configuration_id=configuration.configuration_id,
        )
        batch_ids: list[str] = []
        requests = completed = 0
        state = "COMPLETED"
        failure: str | None = None
        for ordinal in range(min(chunk_total, chunk_budget)):
            chunk = runner.execute(
                RunHistoricalCaptureChunkCommand(
                    plan_id=plan.plan_id, ordinal=ordinal, requested_at=requested_at
                )
            )
            operation = self._real.operation_by_idempotency(chunk.attempt_idempotency_key)
            requests += 0 if operation is None else operation.request_count
            if chunk.state is HistoricalCaptureChunkState.COMPLETED:
                completed += 1
                if chunk.batch_id:
                    batch_ids.append(chunk.batch_id)
                continue
            state = (
                "BLOCKED_CREDENTIALS"
                if chunk.state is HistoricalCaptureChunkState.CREDENTIALS_REQUIRED
                else "FAILED"
            )
            failure = chunk.failure_code
            break
        if state == "COMPLETED" and completed < chunk_total:
            state = "PARTIAL"
        return SecuritiesFetchOutcome(
            state=state,
            provider_requests=requests,
            chunks_completed=completed,
            chunks_total=chunk_total,
            new_batch_ids=tuple(batch_ids),
            acquired_at=requested_at,
            failure_code=failure,
            unresolved_symbols=unresolved,
        )


class PostgresCryptoCoverage:
    """Union of retained candle days per product across *every* observation,
    including recovery observations (the only reader that opts in)."""

    def __init__(
        self, repository: Any, *, active_product_ids: frozenset[str] | None = None
    ) -> None:
        self._repository = repository
        # Products still in the latest genuine universe. A route the pulse no
        # longer refreshes (dropped representative, delisting) is out of scope,
        # so recovery never chases it daily forever. ``None`` means no filter.
        self._active = active_product_ids

    PULSE_REFRESHES_CONSIDERED = 5

    def coverage(self) -> tuple[CryptoProductCoverage, ...]:
        days: dict[str, set[date]] = {}
        values: dict[str, dict[date, tuple[str, str]]] = {}

        def absorb(observation: CryptoProductObservation) -> None:
            if observation.evidence_class is EvidenceClass.FIXTURE:
                return
            if self._active is not None and observation.product_id not in self._active:
                return
            bucket = days.setdefault(observation.product_id, set())
            held = values.setdefault(observation.product_id, {})
            for candle in observation.candles:
                day = candle.bucket_start.astimezone(UTC).date()
                bucket.add(day)
                held.setdefault(day, (str(candle.close), str(candle.volume)))

        for observation in self._repository.observations(include_recovered=True):
            absorb(observation)
        # The pulse keeps its rolling-window candles inside its checkpoints;
        # the latest few refreshes are what "self-healing" actually retained.
        refreshes = sorted(self._repository.refresh_runs(), key=lambda item: item.as_of)
        for refresh in refreshes[-self.PULSE_REFRESHES_CONSIDERED :]:
            for checkpoint in self._repository.pulse_checkpoints(refresh.refresh_id):
                if checkpoint.observation is not None:
                    absorb(checkpoint.observation)
        return tuple(
            CryptoProductCoverage(
                product_id=product,
                first_day=min(retained) if retained else None,
                retained_days=frozenset(retained),
                retained_values=values[product],
            )
            for product, retained in sorted(days.items())
        )


class CoinbaseCryptoRecoveryFetcher:
    """Bounded public Coinbase daily-candle GET (via the existing universe
    source), retained as a quarantined recovery observation."""

    def __init__(self, *, source: Any, repository: Any) -> None:
        self._source = source
        self._repository = repository

    def fetch_missing(
        self,
        *,
        product_id: str,
        days: tuple[date, ...],
        run_key: str,
        requested_at: datetime,
    ) -> CryptoFetchOutcome:
        if not days:
            return CryptoFetchOutcome("COMPLETED", 0)
        ordered = sorted(days)
        # The existing adapter bounds a request to 91..300 days. Pad backwards
        # to the minimum span; if the gap is longer, take its oldest 300 days
        # (the remainder stays open and is picked up by a later run).
        last = ordered[min(len(ordered), COINBASE_CANDLE_MAXIMUM_SPAN_DAYS) - 1]
        first = ordered[0]
        end = datetime.combine(last + timedelta(days=1), datetime.min.time(), UTC)
        span = max((last - first).days + 1, COINBASE_CANDLE_MINIMUM_SPAN_DAYS)
        start = end - timedelta(days=span)
        try:
            page: CapturedObservationPage = self._source.capture_daily_observations(
                product_id=product_id,
                interval_start=start,
                interval_end=end,
                requested_at=requested_at,
            )
        except Exception as error:  # fail closed, never retry
            return CryptoFetchOutcome("FAILED", 1, failure_code=type(error).__name__)
        wanted = {datetime.combine(day, datetime.min.time(), UTC) for day in ordered}
        kept = tuple(item for item in page.candles if item.bucket_start in wanted)
        capture = RECOVERY_CAPTURE_ID_PREFIX + hashlib.sha256(run_key.encode()).hexdigest()[:24]
        observation_id = f"{capture}-{product_id}-{ordered[0].isoformat()}"
        if kept:
            observation = CryptoProductObservation.create(
                observation_id=observation_id,
                source_capture_id=capture,
                product_id=product_id,
                interval_start=min(item.bucket_start for item in kept),
                interval_end=max(item.bucket_start for item in kept) + timedelta(days=1),
                evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
                candles=kept,
                missing_buckets=max(0, len(wanted) - len(kept)),
                duplicate_records=page.duplicate_records,
                rejected_records=page.rejected_records,
                request_count=1,
                raw_evidence_hashes=(hashlib.sha256(page.raw_payload).hexdigest(),),
                source_contract_version="COINBASE_ADVANCED_TRADE_PUBLIC_CANDLES_V1",
                parser_version="COINBASE_PUBLIC_DAILY_CANDLE_PARSER_V1",
                checkpoint_ordinal=1,
                completed_at=page.captured_at,
                canonical_asset_id=product_id.split("-")[0],
            )
            self._repository.save_observation(observation)
        return CryptoFetchOutcome(
            "COMPLETED",
            1,
            candles=tuple(
                CryptoCandleValue(item.bucket_start.date(), str(item.close), str(item.volume))
                for item in page.candles
            ),
            observation_ids=(observation_id,) if kept else (),
            acquired_at=page.captured_at,
        )
