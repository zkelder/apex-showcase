from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine

from apex.adapters.live_preflight_acceptance import ACCEPTANCE_AT, _run_internal
from apex.adapters.live_preflight_postgres import (
    LIVE_PREFLIGHT_TABLES,
    PostgresLivePreflightRepository,
    live_intent_table,
)
from apex.adapters.live_readiness_acceptance import financial_counts
from apex.application.live_preflight import (
    ApproveCoinbaseProviderPreview,
    MaterializeLivePreflightAssessment,
)
from apex.domain.live_preflight import (
    LivePreflightAssessment,
    LivePreflightError,
    ProviderPreviewAttempt,
    ProviderPreviewCapture,
    ProviderPreviewStatus,
)

pytestmark = pytest.mark.postgres


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def test_0073_empty_downgrade_reupgrade_and_retained_rows_refuse_destruction(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        command.downgrade(_config(connection), "0072_kalshi_read_only")
    assert set(LIVE_PREFLIGHT_TABLES).isdisjoint(sa.inspect(postgres_engine).get_table_names())
    with postgres_engine.begin() as connection:
        command.upgrade(_config(connection), "head")

    _persist_complete_fixture(postgres_engine)
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError, match="cannot downgrade 0073"):
            command.downgrade(_config(connection), "0072_kalshi_read_only")
        transaction.rollback()


def _persist_complete_fixture(
    engine: Engine,
) -> tuple[PostgresLivePreflightRepository, LivePreflightAssessment]:
    source, intent, preflight = _run_internal(suffix="postgres")
    eligibility = source.eligibility(preflight.eligibility_decision_id)
    assert eligibility is not None
    repository = PostgresLivePreflightRepository(engine)
    repository.save_intent(intent)
    repository.save_eligibility(eligibility)
    repository.save_preflight(preflight)
    approval = ApproveCoinbaseProviderPreview(repository).execute(
        approval_id="postgres-preview-approval",
        credential_reference_id="postgres-opaque-view-reference",
        intent_id=intent.intent_id,
        maximum_request_count=1,
        timeout_seconds=5,
        approved_at=ACCEPTANCE_AT + timedelta(minutes=2),
        expires_at=ACCEPTANCE_AT + timedelta(minutes=10),
        actor="operator:persistence-test",
        reason="Persistence fixture preview-only scope.",
        idempotency_key="postgres-preview-approval",
        confirmation=(
            f"APPROVE_COINBASE_PROVIDER_PREVIEW:postgres-preview-approval:{intent.intent_id}"
        ),
    )
    capture = repository.save_preview_capture(
        ProviderPreviewCapture.create_coinbase(
            capture_id="postgres-preview-capture",
            approval=approval,
            intent=intent,
            request_identity="1" * 64,
            sanitized_response_hash="2" * 64,
            provider_validation_results=(),
            estimated_fees="0.001",
            product_constraints=(("BEST_ASK", "10.00"),),
            warnings=(),
            captured_at=ACCEPTANCE_AT + timedelta(minutes=3),
        )
    )
    repository.save_preview_attempt(
        ProviderPreviewAttempt.create(
            attempt_id="postgres-preview-capture-attempt",
            approval_id=approval.approval_id,
            intent=intent,
            status=ProviderPreviewStatus.SUCCEEDED,
            capture_id=capture.capture_id,
            blocked_reason=None,
            attempted_at=ACCEPTANCE_AT + timedelta(minutes=3),
            request_count=1,
        )
    )
    assessment = MaterializeLivePreflightAssessment(repository).execute(
        assessment_id="postgres-live-preflight-assessment",
        preflight_id=preflight.preflight_id,
        preview_capture_id=capture.capture_id,
        assessed_at=ACCEPTANCE_AT + timedelta(minutes=3),
    )
    return repository, assessment


def test_postgres_round_trip_replay_and_zero_financial_mutation(postgres_engine: Engine) -> None:
    before = financial_counts(postgres_engine)
    repository, assessment = _persist_complete_fixture(postgres_engine)
    reloaded = PostgresLivePreflightRepository(postgres_engine)
    assert reloaded.intents() == repository.intents()
    assert reloaded.preflights() == repository.preflights()
    assert reloaded.preview_approvals() == repository.preview_approvals()
    assert reloaded.preview_captures() == repository.preview_captures()
    assert reloaded.preview_attempts() == repository.preview_attempts()
    assert reloaded.assessment(assessment.assessment_id) == assessment
    assert repository.save_assessment(assessment) == assessment
    assert financial_counts(postgres_engine) == before
    assert sum(item.provider_write_count for item in repository.preview_captures()) == 0
    assert all(item.provider_order_id is None for item in repository.intents())
    assert all(item.reservation_id is None for item in repository.intents())


def test_postgres_evidence_is_immutable_and_conflicting_replay_fails(
    postgres_engine: Engine,
) -> None:
    repository, _ = _persist_complete_fixture(postgres_engine)
    intent = repository.intents()[0]
    with pytest.raises(LivePreflightError, match="ID_CONFLICT"):
        repository.save_intent(replace(intent, canonical_hash="f" * 64))
    with pytest.raises(Exception, match="immutable"), postgres_engine.begin() as connection:
        connection.execute(
            live_intent_table.update()
            .where(live_intent_table.c.intent_id == intent.intent_id)
            .values(authority="LIVE")
        )


def test_database_constraints_reject_provider_order_or_reservation_linkage(
    postgres_engine: Engine,
) -> None:
    repository, _ = _persist_complete_fixture(postgres_engine)
    intent = repository.intents()[0]
    with pytest.raises(sa.exc.IntegrityError), postgres_engine.begin() as connection:
        connection.execute(
            live_intent_table.insert().values(
                intent_id="unsafe-linked-live-intent",
                allocation_amount="1",
                provider_order_id="provider-order-prohibited",
                reservation_id=None,
                payload="{}",
                canonical_hash=intent.canonical_hash,
                provider_write_count=0,
                authority="NONE",
            )
        )
