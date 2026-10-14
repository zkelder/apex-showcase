"""One composed, bounded Historical Evidence Gap Recovery pass.

Shared by the operating-cycle task and the operator CLI so the two can never
diverge. Authority ``NONE``; zero provider writes; no PAPER dependency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from apex.adapters.evidence_recovery_postgres import (
    PostgresEvidenceRecoveryRepository,
    PostgresSecuritiesCoverage,
    evidence_ledger_present,
    retained_kalshi_refresh_dates,
    retained_live_observation_rounds,
    retained_options_session_ids,
    retained_paper_closures,
)
from apex.adapters.evidence_recovery_sources import (
    CoinbaseCryptoRecoveryFetcher,
    HistoricalPipelineSecuritiesFetcher,
    PostgresCryptoCoverage,
    XnysExchangeCalendar,
)
from apex.application.evidence_recovery import (
    LIVE_OBSERVATION_LOOKBACK,
    RecoverCryptoCandles,
    RecoverSecuritiesBars,
    RecoveryBudget,
    RecoverySummary,
    kalshi_refresh_date_gaps,
    live_observation_gaps,
    options_session_gaps,
    paper_execution_gaps,
    record_irrecoverable_gaps,
)
from apex.domain.evidence_recovery import EvidenceFamily
from apex.domain.options_research_operations import OptionsCaptureWindowPolicy


@dataclass(frozen=True, slots=True)
class EvidenceGapRecoveryReport:
    irrecoverable_recorded: dict[str, int]
    securities: RecoverySummary | None
    crypto: RecoverySummary | None
    warnings: tuple[str, ...] = ()
    provider_requests: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_live_only_gaps(
    *,
    engine: Any,
    repository: PostgresEvidenceRecoveryRepository,
    as_of: datetime,
    now: datetime,
) -> dict[str, int]:
    """Record gaps in evidence that can never be reconstructed. Never calls a
    provider and never approximates; every gap gets an explicit reason."""

    recorded: dict[str, int] = {}
    # Options-forward capture sessions whose capture window closed uncaptured.
    option_ids = retained_options_session_ids(engine)
    if option_ids:
        first = date.fromisoformat(option_ids[0])
        sessions = ReviewedCalendar.sessions(start=first, end=as_of.date())
        policy = OptionsCaptureWindowPolicy.create()
        closed = {
            item.session_date: item.closes_at + timedelta(seconds=policy.post_close_grace_seconds)
            < as_of
            for item in sessions
        }
        recorded[EvidenceFamily.OPTIONS_CHAIN_SESSION.value] = record_irrecoverable_gaps(
            repository=repository,
            family=EvidenceFamily.OPTIONS_CHAIN_SESSION,
            intervals=options_session_gaps(
                expected_sessions=[item.session_date for item in sessions],
                retained_session_ids=option_ids,
                window_closed=closed,
            ),
            recorded_at=now,
        )
    recorded[EvidenceFamily.KALSHI_REFRESH_DATE.value] = record_irrecoverable_gaps(
        repository=repository,
        family=EvidenceFamily.KALSHI_REFRESH_DATE,
        intervals=kalshi_refresh_date_gaps(
            retained_dates=retained_kalshi_refresh_dates(engine),
            through_exclusive=as_of.astimezone(UTC).date(),
        ),
        recorded_at=now,
    )
    rounds = retained_live_observation_rounds(engine, since=as_of - LIVE_OBSERVATION_LOOKBACK)
    live_intervals = tuple(
        gap
        for subject, starts in sorted(rounds.items())
        for gap in live_observation_gaps(subject_id=subject, window_starts=starts, as_of=now)
    )
    recorded[EvidenceFamily.LIVE_OBSERVATION_WINDOW.value] = record_irrecoverable_gaps(
        repository=repository,
        family=EvidenceFamily.LIVE_OBSERVATION_WINDOW,
        intervals=live_intervals,
        recorded_at=now,
    )
    recorded[EvidenceFamily.PAPER_EXECUTION_FACT.value] = record_irrecoverable_gaps(
        repository=repository,
        family=EvidenceFamily.PAPER_EXECUTION_FACT,
        intervals=paper_execution_gaps(closures=retained_paper_closures(engine)),
        recorded_at=now,
    )
    return recorded


ReviewedCalendar = XnysExchangeCalendar()


def run_evidence_gap_recovery(
    *,
    engine: Any,
    as_of: datetime,
    executed_at: datetime,
    cycle_key: str,
    securities_fetcher: Any,
    crypto_fetcher: Any | None,
    crypto_repository: Any,
    budget: RecoveryBudget | None = None,
    detect_only: bool = False,
) -> EvidenceGapRecoveryReport:
    """Detect live-only gaps, then recover bounded securities/crypto gaps.

    Each stage is isolated: one stage's failure is retained as an error and
    never blocks the others. ``detect_only`` performs no provider request."""

    repository = PostgresEvidenceRecoveryRepository(engine)
    budget = budget or RecoveryBudget()
    errors: list[str] = []
    recorded: dict[str, int] = {}
    securities: RecoverySummary | None = None
    crypto: RecoverySummary | None = None
    try:
        recorded = detect_live_only_gaps(
            engine=engine, repository=repository, as_of=as_of, now=executed_at
        )
    except Exception as error:
        errors.append(f"LIVE_ONLY_GAP_DETECTION_FAILED:{type(error).__name__}")
    if not detect_only:
        try:
            securities = RecoverSecuritiesBars(
                repository=repository,
                coverage=PostgresSecuritiesCoverage(engine),
                calendar=ReviewedCalendar,
                fetcher=securities_fetcher,
            ).execute(as_of=as_of, executed_at=executed_at, cycle_key=cycle_key, budget=budget)
        except Exception as error:
            errors.append(f"SECURITIES_RECOVERY_FAILED:{type(error).__name__}")
        if crypto_fetcher is not None:
            try:
                crypto = RecoverCryptoCandles(
                    repository=repository,
                    coverage=PostgresCryptoCoverage(
                        crypto_repository,
                        active_product_ids=active_crypto_product_ids(crypto_repository),
                    ),
                    fetcher=crypto_fetcher,
                ).execute(as_of=as_of, executed_at=executed_at, cycle_key=cycle_key, budget=budget)
            except Exception as error:
                errors.append(f"CRYPTO_RECOVERY_FAILED:{type(error).__name__}")
    requests = sum(item.provider_requests for item in (securities, crypto) if item is not None)
    return EvidenceGapRecoveryReport(
        irrecoverable_recorded=recorded,
        securities=securities,
        crypto=crypto,
        provider_requests=requests,
        errors=tuple(errors),
    )


def active_crypto_product_ids(crypto_repository: Any) -> frozenset[str]:
    """The routes the pulse currently refreshes (its latest refresh run's
    requested products). Only those are expected to have continuous daily
    coverage: a route the pulse dropped (for example after a representative
    route swap) or never tracked is out of scope, so recovery never chases it."""

    runs = sorted(crypto_repository.refresh_runs(), key=lambda item: item.as_of)
    if runs:
        return frozenset(runs[-1].request_product_ids)
    return frozenset()


def build_securities_fetcher(engine: Any) -> HistoricalPipelineSecuritiesFetcher:
    """The production composition: the reviewed Alpaca read-only adapter, the
    local raw-artifact store, and the existing historical capture pipeline."""

    from apex.adapters.alpaca_market_data import (
        AlpacaMarketDataHttpAdapter,
        LocalAlpacaRawArtifactStore,
    )
    from apex.adapters.historical_market_data_postgres import (
        PostgresHistoricalMarketDataRepository,
    )
    from apex.adapters.real_securities_postgres import PostgresRealSecuritiesDataRepository
    from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.adapters.security_identity_postgres import PostgresSecurityIdentityRepository

    return HistoricalPipelineSecuritiesFetcher(
        historical_repository=PostgresHistoricalMarketDataRepository(engine),
        identity_source=PostgresSecurityIdentityRepository(engine),
        real_repository=PostgresRealSecuritiesDataRepository(engine),
        securities_repository=PostgresSecuritiesRepository(engine),
        provider=AlpacaMarketDataHttpAdapter(),
        artifacts=LocalAlpacaRawArtifactStore(),
        dataset=LocalSecuritiesDatasetAdapter(),
    )


def build_crypto_fetcher(crypto_repository: Any) -> CoinbaseCryptoRecoveryFetcher:
    from apex.adapters.coinbase_universe_public import CoinbasePublicUniverseSource

    return CoinbaseCryptoRecoveryFetcher(
        source=CoinbasePublicUniverseSource(), repository=crypto_repository
    )


class _EmptyLedger:
    """Preview before migration 0118 exists: nothing has been settled yet."""

    def current_gaps(self, *, evidence_family: Any = None) -> tuple[Any, ...]:
        return ()


def _preview_repository(engine: Any) -> Any:
    return (
        PostgresEvidenceRecoveryRepository(engine)
        if evidence_ledger_present(engine)
        else _EmptyLedger()
    )


def preview_gaps(*, engine: Any, as_of: datetime, now: datetime) -> dict[str, Any]:
    """Read-only preview of every gap a pass would record or target. Writes
    nothing and makes no provider request."""

    option_ids = retained_options_session_ids(engine)
    options: tuple[tuple[str, str, str], ...] = ()
    if option_ids:
        sessions = ReviewedCalendar.sessions(
            start=date.fromisoformat(option_ids[0]), end=as_of.date()
        )
        policy = OptionsCaptureWindowPolicy.create()
        options = options_session_gaps(
            expected_sessions=[item.session_date for item in sessions],
            retained_session_ids=option_ids,
            window_closed={
                item.session_date: item.closes_at
                + timedelta(seconds=policy.post_close_grace_seconds)
                < as_of
                for item in sessions
            },
        )
    rounds = retained_live_observation_rounds(engine, since=as_of - LIVE_OBSERVATION_LOOKBACK)
    return {
        "options_chain_gaps": options,
        "kalshi_refresh_gaps": kalshi_refresh_date_gaps(
            retained_dates=retained_kalshi_refresh_dates(engine),
            through_exclusive=as_of.astimezone(UTC).date(),
        ),
        "live_observation_gaps": tuple(
            gap
            for subject, starts in sorted(rounds.items())
            for gap in live_observation_gaps(subject_id=subject, window_starts=starts, as_of=now)
        ),
        "paper_execution_gaps": paper_execution_gaps(closures=retained_paper_closures(engine)),
        "securities": RecoverSecuritiesBars(
            repository=_preview_repository(engine),
            coverage=PostgresSecuritiesCoverage(engine),
            calendar=ReviewedCalendar,
            fetcher=None,  # type: ignore[arg-type]
        ).preview(as_of=as_of),
    }
