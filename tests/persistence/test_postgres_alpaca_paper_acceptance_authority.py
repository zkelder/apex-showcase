"""Persistent engineering-only Alpaca PAPER acceptance authority proof."""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.alpaca_paper_broker import AlpacaPaperReadiness
from apex.adapters.external_paper_postgres import (
    PostgresAlpacaPaperAcceptanceAuthorityAdapter,
    PostgresExternalPaperRepository,
)
from apex.adapters.postgres import PostgresSettings
from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter, securities_record
from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.application.external_paper import (
    CreatePersistentAlpacaPaperAcceptanceWorkflow,
    ExternalPaperApplicationError,
    ExternalPaperReadinessResult,
    ready_capability,
)
from apex.application.securities import ImportSecuritiesData, ImportSecuritiesDataCommand
from apex.domain.external_paper import ExternalPaperCapabilityState, PaperBrokerCapabilitySnapshot
from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesCalendarSession,
    SecuritiesInstrument,
)

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
_DEV_PATH = Path(__file__).resolve().parents[2] / "scripts" / "dev.py"
_DEV_SPEC = importlib.util.spec_from_file_location("apex_acceptance_dev", _DEV_PATH)
assert _DEV_SPEC is not None and _DEV_SPEC.loader is not None
dev = importlib.util.module_from_spec(_DEV_SPEC)
_DEV_SPEC.loader.exec_module(dev)


class _DeterministicReadinessBroker:
    def __init__(self, state: ExternalPaperCapabilityState) -> None:
        self._state = state
        self.calls = 0

    def check_readiness(self, *, checked_at: datetime) -> AlpacaPaperReadiness:
        self.calls += 1
        return AlpacaPaperReadiness(
            capability=PaperBrokerCapabilitySnapshot.create(
                state=self._state,
                credentials_available=self._state
                is not ExternalPaperCapabilityState.CREDENTIALS_REQUIRED,
                authentication_state="READY"
                if self._state is ExternalPaperCapabilityState.READY
                else "NOT_READY",
                account_read=self._state is ExternalPaperCapabilityState.READY,
                order_submit=self._state is ExternalPaperCapabilityState.READY,
                order_read=self._state is ExternalPaperCapabilityState.READY,
                cancel=self._state is ExternalPaperCapabilityState.READY,
                position_read=self._state is ExternalPaperCapabilityState.READY,
                activity_read=self._state is ExternalPaperCapabilityState.READY,
                checked_at=checked_at,
                reason_codes=(f"{self._state.value}_FIXTURE",),
            ),
            evidence=(),
            endpoint_states=(),
        )


def _cli_database(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    settings = PostgresSettings.from_env().for_test()
    monkeypatch.setattr(dev, "migrate", lambda: None)
    monkeypatch.setattr(
        PostgresSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    from apex.adapters import postgres as postgres_adapter

    monkeypatch.setattr(postgres_adapter, "create_postgres_engine", lambda _: engine)
    monkeypatch.setattr(dev, "_strategy_lab_json", output.append)
    return output


def _load_spy(engine: Engine, tmp_path: Path) -> None:
    root = tmp_path / "securities"
    data = LocalSecuritiesDatasetAdapter(root)
    session_day = date(2026, 7, 28)
    session = SecuritiesCalendarSession.create(
        calendar_identity="XNYS",
        calendar_version="alpaca-acceptance-v1",
        timezone="America/New_York",
        session_date=session_day.isoformat(),
        opens_at=datetime.combine(session_day, time(14, 30), tzinfo=UTC),
        closes_at=datetime.combine(session_day, time(21), tzinfo=UTC),
        is_early_close=False,
        source_identity="alpaca-acceptance-test",
    )
    instrument = SecuritiesInstrument(
        instrument_id="alpaca-acceptance-spy",
        symbol="SPY",
        asset_class="ETF",
        exchange_mic="XNYS",
        currency="USD",
        timezone="America/New_York",
        valid_from=session_day.isoformat(),
        valid_to=None,
        source_identity="alpaca-acceptance-test",
    )
    bar = SecuritiesBarEvidence(
        instrument_id=instrument.instrument_id,
        interval="ONE_DAY",
        adjustment_state="RAW",
        period_start=session.opens_at,
        period_end=session.closes_at,
        available_at=session.closes_at + timedelta(minutes=1),
        open="500",
        high="501",
        low="499",
        close="500",
        volume="10000",
        source_identity="alpaca-acceptance-test",
    )
    source = data.write_generated_jsonl(
        relative_location="incoming/spy.jsonl",
        records=(securities_record(value) for value in (instrument, session, bar)),
    )
    ImportSecuritiesData(repository=PostgresSecuritiesRepository(engine), market_data=data).execute(
        ImportSecuritiesDataCommand(
            managed_source=source,
            dataset_name="alpaca acceptance SPY",
            idempotency_key="alpaca-acceptance-spy-data",
            imported_at=NOW,
        )
    )


def _workflow(engine: Engine) -> CreatePersistentAlpacaPaperAcceptanceWorkflow:
    external = PostgresExternalPaperRepository(engine)
    return CreatePersistentAlpacaPaperAcceptanceWorkflow(
        repository=external,
        securities_repository=PostgresSecuritiesPaperRepository(engine),
        authority_port=PostgresAlpacaPaperAcceptanceAuthorityAdapter(engine),
    )


def test_persistent_acceptance_authority_is_one_prefilled_preflight(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    _load_spy(postgres_engine, tmp_path)
    result = _workflow(postgres_engine).execute(
        symbol="SPY",
        idempotency_key="alpaca-acceptance-v1",
        readiness=ExternalPaperReadinessResult(ready_capability(NOW), (), ()),
        now=NOW,
    )
    assert result.preflight.state == "READY_FOR_OPERATOR_CONFIRMATION"
    assert result.preflight.confirmation == (
        f"SUBMIT_ALPACA_PAPER:{result.instruction.instruction_id}:{result.instruction.canonical_hash}"
    )
    assert result.instruction.quantity == 1
    assert result.instruction.order_type == "MARKET"
    assert result.binding.state.value == "ORDER_PENDING_EXTERNAL_FILL"
    replay = _workflow(postgres_engine).execute(
        symbol="SPY",
        idempotency_key="alpaca-acceptance-v1",
        readiness=ExternalPaperReadinessResult(ready_capability(NOW), (), ()),
        now=NOW,
    )
    assert replay.acceptance == result.acceptance
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM securities_fill_evidence")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM securities_position_state")) == 0
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM external_paper_submission_attempt"))
            == 0
        )
    with pytest.raises(ExternalPaperApplicationError, match="IDEMPOTENCY_CONFLICT"):
        _workflow(postgres_engine).execute(
            symbol="TLT",
            idempotency_key="alpaca-acceptance-v1",
            readiness=ExternalPaperReadinessResult(ready_capability(NOW), (), ()),
            now=NOW,
        )


def test_acceptance_cli_preserves_absent_and_blocked_readiness(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _cli_database(monkeypatch, postgres_engine)
    absent = _DeterministicReadinessBroker(ExternalPaperCapabilityState.CREDENTIALS_REQUIRED)
    dev.alpaca_paper_acceptance_create(
        symbol="SPY", idempotency_key="cli-credentials-absent", broker=absent, now=NOW
    )
    assert output[-1]["state"] == "CREDENTIALS_REQUIRED"
    assert output[-1]["network_reads_performed"] == 0
    assert output[-1]["next_action"] == "CONFIGURE_PRIVATE_SEPARATE_PAPER_CREDENTIALS"
    blocked = _DeterministicReadinessBroker(ExternalPaperCapabilityState.PROVIDER_UNAVAILABLE)
    dev.alpaca_paper_acceptance_create(
        symbol="SPY", idempotency_key="cli-readiness-blocked", broker=blocked, now=NOW
    )
    assert output[-1]["state"] == "PROVIDER_UNAVAILABLE"
    assert output[-1]["blockers"] == ("PROVIDER_UNAVAILABLE_FIXTURE",)
    assert output[-1]["next_action"] == "RESOLVE_PAPER_READINESS_BLOCKER"
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM campaign")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM external_paper_instruction")) == 0


def test_acceptance_cli_runs_persistent_adapter_and_replays(
    postgres_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _load_spy(postgres_engine, tmp_path)
    output = _cli_database(monkeypatch, postgres_engine)
    ready = _DeterministicReadinessBroker(ExternalPaperCapabilityState.READY)
    dev.alpaca_paper_acceptance_create(
        symbol="SPY", idempotency_key="cli-ready-v1", broker=ready, now=NOW
    )
    first = output[-1]
    assert first["state"] == "READY_FOR_OPERATOR_CONFIRMATION"
    assert first["side"] == "BUY"
    assert first["quantity"] == 1
    assert first["order_type"] == "MARKET"
    assert first["time_in_force"] == "DAY"
    assert first["confirmation"] == (
        f"SUBMIT_ALPACA_PAPER:{first['instruction_id']}:"
        f"{PostgresExternalPaperRepository(postgres_engine).instruction(first['instruction_id']).canonical_hash}"
    )
    dev.alpaca_paper_acceptance_create(
        symbol="SPY", idempotency_key="cli-ready-v1", broker=ready, now=NOW
    )
    assert output[-1]["acceptance_id"] == first["acceptance_id"]
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM securities_fill_evidence")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM securities_position_state")) == 0
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM external_paper_submission_attempt"))
            == 0
        )
    with pytest.raises(ExternalPaperApplicationError, match="IDEMPOTENCY_CONFLICT"):
        dev.alpaca_paper_acceptance_create(
            symbol="TLT", idempotency_key="cli-ready-v1", broker=ready, now=NOW
        )
