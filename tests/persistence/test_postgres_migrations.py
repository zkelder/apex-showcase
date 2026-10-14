from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex import ENGINE_VERSION
from apex.adapters.fixtures import FixtureReader
from apex.adapters.postgres import (
    PostgresAutopilotStore,
    durable_opportunity_proposal,
    market_dynamics_feature_snapshot,
    multi_strategy_shadow_session,
    normalized_market_snapshot,
    position_settlement,
    postgres_shadow_operations_store,
    postgres_strategy_shadow_store,
    postgres_uow_factory,
    provider_ingestion_run,
    raw_provider_response,
    securities_instrument,
    settlement_fact,
    shadow_prediction,
    strategy_governance,
    strategy_lab_operator_action,
)
from apex.adapters.simulator import DeterministicSimulator
from apex.application.authorize_order import (
    AuthorizeAndReserveCommand,
    CampaignInitializationCommand,
    DurableAuthorizationService,
    PersistAuthorizationInputsCommand,
)
from apex.application.execute_partial_fill_canceled_order import (
    ApplyPartialFillExecutionEventCommand,
    DurablePartialFillCanceledExecutionService,
)
from apex.application.interfaces import PartialFillCanceledExecutionScript, RunMetadata
from apex.application.run_simulation import GoldenSimulation, PartialExecutionCheckpoint
from apex.application.settle_partial_fill_canceled_no import (
    DurablePartialFillCanceledSettlementService,
    SettlePartialFillCanceledCommand,
)
from apex.application.shadow_operations import (
    CreateShadowEvidencePlanCommand,
    ShadowEvidencePlanConfiguration,
)
from apex.application.strategy_shadow import (
    RetainedMarketEvidence,
    build_market_consensus_baseline_prediction,
    production_shadow_strategy_descriptors,
)
from apex.cli import _decision_inputs, _execution_script, _settlement_inputs
from apex.domain.execution import (
    ClassifiedRuleBoundSettlementFact,
    ExecutionEvent,
    create_reserved_order,
)
from apex.domain.market import VoidContractRuleVersion
from apex.domain.values import ContentHash, Money
from scripts import dev as dev_script

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
REVISION_0001 = "0001_atomic_authorization"
REVISION_0002 = "0002_durable_execution_events"
REVISION_0003 = "0003_partial_fill_and_position"
REVISION_0004 = "0004_durable_no_settlement"
REVISION_0005 = "0005_durable_void_settlement"
REVISION_0006 = "0006_public_market_discovery"
REVISION_0007 = "0007_public_event_consistency"
REVISION_0008 = "0008_persisted_scan_comparison"
REVISION_0015 = "0015_paper_portfolio"
REVISION_0016 = "0016_operator_console"
REVISION_0017 = "0017_strategy_shadowing"
REVISION_0020 = "0020_multi_strategy_shadow"
REVISION_0021 = "0021_shadow_operations"
REVISION_0022 = "0022_evidence_review_trials"
REVISION_0023 = "0023_strategy_lab_foundation"
REVISION_0024 = "0024_strategy_lab_shadow"
REVISION_0025 = "0025_securities_research_shadow"
REVISION_0026 = "0026_securities_paper_execution"
REVISION_0047 = "0047_acceptance_isolation"
HEAD_REVISION = "0118_evidence_gap_recovery"
MIGRATION_NOW = datetime(2026, 7, 24, tzinfo=UTC)
UNSAFE_VOID_DOWNGRADE_ERROR = (
    "cannot downgrade 0005 to 0004: retained durable VOID evidence would be lost"
)
BASE_TABLES = {
    "campaign_policy_version",
    "campaign",
    "ledger_account",
    "ledger_journal",
    "ledger_posting",
    "opportunity_proposal",
    "allocation_decision",
    "risk_decision",
    "risk_reservation",
    "order",
}
DURABLE_TABLES = {
    *BASE_TABLES,
    "execution_event",
    "position",
    "settlement_fact",
    "position_settlement",
}
VOID_DOWNGRADE_SCHEMA_TABLES = (
    "opportunity_proposal",
    "settlement_fact",
    "position_settlement",
    "position",
    "ledger_journal",
    "ledger_posting",
)
VOID_DOWNGRADE_EVIDENCE_TABLES = (
    "opportunity_proposal",
    "settlement_fact",
    "position_settlement",
    "ledger_journal",
    "ledger_posting",
)
VOID_DOWNGRADE_PRIMARY_KEYS = {
    "opportunity_proposal": "proposal_id",
    "settlement_fact": "settlement_fact_id",
    "position_settlement": "position_settlement_id",
    "ledger_journal": "journal_id",
    "ledger_posting": "posting_id",
}


@dataclass(frozen=True, slots=True)
class VoidAuthorization:
    campaign_id: str
    order_id: str
    script: PartialFillCanceledExecutionScript
    events: tuple[ExecutionEvent, ...]
    fact: ClassifiedRuleBoundSettlementFact
    void_term_content_hash: ContentHash


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _migrate(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        command.upgrade(_config(connection), revision)
        connection.commit()


def _downgrade(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        command.downgrade(_config(connection), revision)
        connection.commit()


def _column_names(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _check_names(inspector, table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in inspector.get_check_constraints(table_name)
        if constraint["name"] is not None
    }


def _trigger_names(engine: Engine, table_name: str) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.scalars(
                sa.text(
                    "SELECT trigger.tgname "
                    "FROM pg_trigger trigger "
                    "JOIN pg_class relation ON relation.oid = trigger.tgrelid "
                    "JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = current_schema() "
                    "AND relation.relname = :table_name "
                    "AND NOT trigger.tgisinternal"
                ),
                {"table_name": table_name},
            )
        )


def _current_revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def test_0075_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    tables = {
        "security_reference_identity",
        "security_reference_universe_snapshot",
        "broad_equity_security_quality",
        "broad_equity_discovery_run",
        "broad_equity_shadow_plan",
        "options_intelligence_capability",
        "options_intelligence_contract",
        "options_intelligence_optionable_underlying",
        "options_intelligence_shadow_proposal",
    }
    assert _current_revision(postgres_engine) == HEAD_REVISION
    assert tables <= set(inspect(postgres_engine).get_table_names())
    _downgrade(postgres_engine, "0074_paper_activation")
    assert _current_revision(postgres_engine) == "0074_paper_activation"
    assert tables.isdisjoint(inspect(postgres_engine).get_table_names())
    _migrate(postgres_engine, HEAD_REVISION)
    assert _current_revision(postgres_engine) == HEAD_REVISION
    assert tables <= set(inspect(postgres_engine).get_table_names())


def test_0048_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    tables = {
        "research_hypothesis_version",
        "research_observation_capture",
        "research_observation",
        "research_capture_checkpoint",
        "research_dataset_snapshot",
        "research_experiment_run",
        "research_experiment_outcome",
    }
    assert _current_revision(postgres_engine) == HEAD_REVISION
    assert tables <= set(inspect(postgres_engine).get_table_names())
    _downgrade(postgres_engine, REVISION_0047)
    assert _current_revision(postgres_engine) == REVISION_0047
    assert tables.isdisjoint(inspect(postgres_engine).get_table_names())
    _migrate(postgres_engine, HEAD_REVISION)
    assert _current_revision(postgres_engine) == HEAD_REVISION
    assert tables <= set(inspect(postgres_engine).get_table_names())


def test_0026_empty_downgrade_reupgrades_and_preserves_0025(
    postgres_engine: Engine,
) -> None:
    stage_two_tables = {
        "securities_execution_policy",
        "securities_paper_run",
        "securities_paper_opportunity",
        "securities_paper_authorization",
        "securities_paper_order",
        "securities_fill_evidence",
        "securities_position_state",
        "securities_mark_evidence",
        "securities_corporate_action_application",
        "securities_exit_decision",
        "securities_execution_lineage",
        "external_securities_paper_run",
        "securities_paper_conformance_report",
    }
    _downgrade(postgres_engine, REVISION_0025)
    assert _current_revision(postgres_engine) == REVISION_0025
    assert stage_two_tables.isdisjoint(inspect(postgres_engine).get_table_names())
    assert "securities_signal" in inspect(postgres_engine).get_table_names()
    _migrate(postgres_engine, REVISION_0026)
    assert _current_revision(postgres_engine) == REVISION_0026
    assert stage_two_tables <= set(inspect(postgres_engine).get_table_names())


def test_0026_retained_policy_blocks_downgrade_before_any_ddl(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0026)
    assert _current_revision(postgres_engine) == REVISION_0026
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO securities_execution_policy "
                "(policy_id, policy_version, execution_model, permitted_directions, "
                "permitted_asset_types, fill_price_rule, decision_to_fill_delay_rule, "
                "half_spread_per_share, slippage_per_share, commission_per_share, "
                "commission_rule, volume_participation_cap, minimum_quantity, "
                "maximum_quantity, quantity_increment, unfilled_expiration_sessions, "
                "allow_partial_fills, session_policy, corporate_action_policy, "
                "canonical_payload, canonical_hash, created_at, superseded_at) VALUES "
                "('retained-policy', 'v1', 'SECURITIES_NEXT_BAR_CROSS_V1', "
                "ARRAY['BUY','SELL'], ARRAY['COMMON_STOCK'], "
                "'NEXT_ELIGIBLE_BAR_OPEN_PLUS_OR_MINUS_COSTS', "
                "'STRICTLY_LATER_ELIGIBLE_SESSION', 0.01, 0.01, 0.005, "
                "'PER_EXECUTED_SHARE', 0.1, 1, 100, 1, 1, true, "
                "'REGULAR_SESSION_ONLY', 'RETAINED_EVIDENCE_ONLY_V1', '{}', "
                "repeat('a', 64), :created_at, NULL)"
            ),
            {"created_at": MIGRATION_NOW},
        )
    tables_before = tuple(sorted(inspect(postgres_engine).get_table_names()))
    triggers_before = _trigger_names(postgres_engine, "securities_execution_policy")
    with pytest.raises(
        Exception,
        match="cannot downgrade 0026 with retained Securities PAPER execution evidence",
    ):
        _downgrade(postgres_engine, REVISION_0025)
    assert _current_revision(postgres_engine) == REVISION_0026
    assert tuple(sorted(inspect(postgres_engine).get_table_names())) == tables_before
    assert _trigger_names(postgres_engine, "securities_execution_policy") == triggers_before
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM securities_execution_policy "
                    "WHERE policy_id = 'retained-policy'"
                )
            )
            == 1
        )


def test_0026_authoritative_financial_columns_are_exact_numeric(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    columns = (
        ("securities_execution_policy", "half_spread_per_share"),
        ("securities_execution_policy", "commission_per_share"),
        ("securities_paper_run", "ending_cash"),
        ("securities_paper_run", "net_realized_pnl"),
        ("securities_fill_evidence", "fill_price"),
        ("securities_fill_evidence", "quantity"),
        ("securities_position_state", "average_cost"),
        ("securities_position_state", "realized_pnl"),
        ("securities_mark_evidence", "market_value"),
        ("securities_corporate_action_application", "cash_entitlement"),
    )
    for table_name, column_name in columns:
        column = next(
            item for item in inspector.get_columns(table_name) if item["name"] == column_name
        )
        assert isinstance(column["type"], sa.Numeric)
        assert column["type"].precision == 38
        assert column["type"].scale == 18


def test_0022_empty_downgrade_reupgrades(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0021)
    assert _current_revision(postgres_engine) == REVISION_0021
    _migrate(postgres_engine, HEAD_REVISION)
    assert _current_revision(postgres_engine) == HEAD_REVISION


def test_0023_empty_downgrade_reupgrades_and_retained_evidence_blocks(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0022)
    assert _current_revision(postgres_engine) == REVISION_0022
    _migrate(postgres_engine, HEAD_REVISION)
    assert _current_revision(postgres_engine) == HEAD_REVISION
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO strategy_lab_feature_schema "
                "(schema_id, vertical, research_family, schema_version, "
                "feature_definitions_payload, feature_names, created_at, "
                "canonical_payload, canonical_hash) "
                "VALUES ('retained-schema', 'EVENT_MARKETS', 'TEST', 'V1', "
                "'[]', ARRAY['feature'], now(), '{}', :hash)"
            ),
            {"hash": "a" * 64},
        )
    with pytest.raises(Exception, match="retained Strategy Lab research evidence"):
        _downgrade(postgres_engine, REVISION_0022)
    # 0025 and 0024 are empty and may safely roll back before 0023 refuses to discard
    # Foundation evidence, so the atomic failing revision is 0023.
    assert _current_revision(postgres_engine) == REVISION_0023


def test_0024_empty_downgrade_reupgrades_and_retained_shadow_evidence_blocks(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0023)
    assert _current_revision(postgres_engine) == REVISION_0023
    _migrate(postgres_engine, HEAD_REVISION)
    assert _current_revision(postgres_engine) == HEAD_REVISION
    with postgres_engine.begin() as connection:
        connection.execute(
            strategy_lab_operator_action.insert().values(
                action_id="retained-shadow-action",
                action_type="REQUEST_SHADOW_REVIEW",
                target_id="candidate",
                idempotency_key="migration-proof",
                reason="retain evidence",
                note="",
                created_at=datetime.now(tz=UTC),
                canonical_input_hash="b" * 64,
                canonical_result_hash="c" * 64,
            )
        )
    with pytest.raises(Exception, match="retained Strategy Lab SHADOW evidence"):
        _downgrade(postgres_engine, REVISION_0023)
    assert _current_revision(postgres_engine) == REVISION_0024
    _migrate(postgres_engine, HEAD_REVISION)


def test_0025_empty_downgrade_reupgrades_and_retained_securities_evidence_blocks(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0024)
    assert _current_revision(postgres_engine) == REVISION_0024
    _migrate(postgres_engine, HEAD_REVISION)
    assert _current_revision(postgres_engine) == HEAD_REVISION
    with postgres_engine.begin() as connection:
        connection.execute(
            securities_instrument.insert().values(
                instrument_id="retained-securities-instrument",
                symbol="APX",
                asset_class="COMMON_STOCK",
                exchange_mic="XNYS",
                currency="USD",
                timezone="America/New_York",
                valid_from="2026-01-01",
                valid_to=None,
                source_identity="migration-proof",
                content_hash="d" * 64,
            )
        )
    before_triggers = _trigger_names(postgres_engine, "securities_instrument")
    with pytest.raises(Exception, match="retained Securities research evidence"):
        _downgrade(postgres_engine, REVISION_0024)
    # Empty 0026 evidence may safely roll back before 0025 refuses to discard
    # retained Securities research evidence, so the atomic failing revision is 0025.
    assert _current_revision(postgres_engine) == REVISION_0025
    assert _trigger_names(postgres_engine, "securities_instrument") == before_triggers
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(securities_instrument)) == 1


def _install_legacy_shadow_ready_drift(engine: Engine, *, authority_state: str = "SHADOW") -> None:
    descriptors = tuple(
        item
        for item in production_shadow_strategy_descriptors()
        if item.strategy_id in {"operator-thesis-v1", "market-consensus-baseline-v1"}
    )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "ALTER TABLE strategy_governance DROP CONSTRAINT ck_strategy_governance_evidence"
            )
        )
        connection.execute(
            sa.text(
                "ALTER TABLE strategy_governance ADD CONSTRAINT ck_strategy_governance_evidence "
                "CHECK (evidence_state IN "
                "('INCOMPLETE', 'DEPRIORITIZED', 'FIXTURE', 'SHADOW_READY'))"
            )
        )
        for descriptor in descriptors:
            values = PostgresAutopilotStore._descriptor_values(descriptor, created_at=MIGRATION_NOW)
            values["authority_state"] = authority_state
            values["evidence_state"] = "SHADOW_READY"
            values["canonical_payload_hash"] = "9" * 64
            connection.execute(strategy_governance.insert().values(**values))


def test_0018_schema_cannot_normally_retain_shadow_ready_governance(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, "0018_orderbook_microstructure")
    try:
        definition = next(
            item["sqltext"]
            for item in inspect(postgres_engine).get_check_constraints("strategy_governance")
            if item["name"] == "ck_strategy_governance_evidence"
        )
        assert "SHADOW_READY" not in str(definition)
    finally:
        _migrate(postgres_engine, HEAD_REVISION)


def test_local_governance_reconciliation_dry_runs_applies_and_is_idempotent(
    postgres_engine: Engine,
) -> None:
    _install_legacy_shadow_ready_drift(postgres_engine)

    with postgres_engine.begin() as connection:
        dry_run = dev_script._local_governance_reconciliation_changes(connection, apply=False)
    assert [(item["strategy_id"], item["evidence_after"]) for item in dry_run] == [
        ("market-consensus-baseline-v1", "INCOMPLETE"),
        ("operator-thesis-v1", "INCOMPLETE"),
    ]
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM strategy_governance WHERE evidence_state = 'SHADOW_READY'"
                )
            )
            == 2
        )

    with postgres_engine.begin() as connection:
        applied = dev_script._local_governance_reconciliation_changes(connection, apply=True)
    assert applied == dry_run
    with postgres_engine.connect() as connection:
        rows = tuple(
            connection.execute(
                sa.text(
                    "SELECT authority_state, evidence_state FROM strategy_governance "
                    "ORDER BY strategy_id"
                )
            )
        )
    assert rows == (("SHADOW", "INCOMPLETE"), ("SHADOW", "INCOMPLETE"))
    with postgres_engine.begin() as connection:
        assert dev_script._local_governance_reconciliation_changes(connection, apply=True) == ()


def test_local_governance_reconciliation_rejects_ambiguous_rows_atomically(
    postgres_engine: Engine,
) -> None:
    _install_legacy_shadow_ready_drift(postgres_engine, authority_state="PAPER")

    with (
        pytest.raises(RuntimeError, match="ambiguous stale local strategy-governance row"),
        postgres_engine.begin() as connection,
    ):
        dev_script._local_governance_reconciliation_changes(connection, apply=True)

    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM strategy_governance WHERE evidence_state = 'SHADOW_READY'"
                )
            )
            == 2
        )
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM strategy_governance WHERE authority_state = 'PAPER'")
            )
            == 2
        )


def _schema_snapshot(engine: Engine) -> dict[str, object]:
    inspector = inspect(engine)
    tables = tuple(sorted(inspector.get_table_names()))
    schema: dict[str, object] = {"tables": tables}
    for table_name in VOID_DOWNGRADE_SCHEMA_TABLES:
        schema[table_name] = {
            "columns": tuple(
                sorted(
                    (column["name"], str(column["type"]), bool(column["nullable"]))
                    for column in inspector.get_columns(table_name)
                )
            ),
            "checks": tuple(
                sorted(
                    (str(constraint["name"]), str(constraint["sqltext"]))
                    for constraint in inspector.get_check_constraints(table_name)
                )
            ),
            "foreign_keys": tuple(
                sorted(
                    (
                        tuple(constraint["constrained_columns"]),
                        str(constraint["referred_table"]),
                        tuple(constraint["referred_columns"]),
                    )
                    for constraint in inspector.get_foreign_keys(table_name)
                )
            ),
            "unique_constraints": tuple(
                sorted(
                    tuple(constraint["column_names"])
                    for constraint in inspector.get_unique_constraints(table_name)
                )
            ),
            "indexes": tuple(
                sorted(
                    (str(index["name"]), tuple(index["column_names"]))
                    for index in inspector.get_indexes(table_name)
                )
            ),
            "triggers": tuple(sorted(_trigger_names(engine, table_name))),
        }
    return schema


def _evidence_snapshot(engine: Engine) -> dict[str, tuple[dict[str, object], ...]]:
    snapshot: dict[str, tuple[dict[str, object], ...]] = {}
    with engine.connect() as connection:
        for table_name in VOID_DOWNGRADE_EVIDENCE_TABLES:
            primary_key = VOID_DOWNGRADE_PRIMARY_KEYS[table_name]
            rows = connection.execute(
                sa.text(f'SELECT * FROM "{table_name}" ORDER BY "{primary_key}"')
            ).mappings()
            snapshot[table_name] = tuple(dict(row) for row in rows)
    return snapshot


def _initialize_fixture_campaign(
    service: DurableAuthorizationService,
    fixture_id: str,
) -> tuple[object, object, FixtureReader]:
    reader = FixtureReader()
    fixture = reader.load_decision(fixture_id)
    decision_inputs = _decision_inputs(fixture)
    service.initialize_campaign(
        CampaignInitializationCommand(
            campaign_id=decision_inputs.campaign_id,
            contribution=Money("1000"),
            authorized_loss=Money("1000"),
            policy=decision_inputs.policy,
            occurred_at=fixture.logical_time,
        )
    )
    return fixture, decision_inputs, reader


def _seed_settlement_fact(
    service: DurableAuthorizationService,
    engine: Engine,
    *,
    decision_fixture_id: str,
    settlement_fixture_id: str,
) -> None:
    fixture, decision_inputs, reader = _initialize_fixture_campaign(service, decision_fixture_id)
    fact = _settlement_inputs(reader.load_settlement(settlement_fixture_id)).fact
    void_term_content_hash: str | None = None
    classification = "NO"
    if isinstance(fact, ClassifiedRuleBoundSettlementFact):
        assert isinstance(decision_inputs.rule, VoidContractRuleVersion)
        classification = "VOID"
        void_term_content_hash = decision_inputs.rule.void_settlement_term.content_hash.value
    with engine.begin() as connection:
        connection.execute(
            settlement_fact.insert().values(
                settlement_fact_id=f"migration-{classification.lower()}-fact",
                campaign_id=decision_inputs.campaign_id,
                fixture_id=fact.fixture_id,
                schema_version=fact.schema_version,
                source_id=fact.source_id,
                instrument_id=fact.instrument_id,
                rule_version_id=fact.rule_version_id,
                rule_content_hash=fact.rule_content_hash.value,
                resolution_source=fact.resolution_source,
                classification=classification,
                void_term_content_hash=void_term_content_hash,
                settlement_value=fact.settlement_value,
                settlement_value_text=fact.settlement_value_text,
                determination_at=decision_inputs.rule.determination_at,
                source_event_at=fact.source_event_at,
                available_at=fact.available_at,
                raw_payload_hash=fact.raw_payload_hash.value,
                raw_payload=fact.raw_payload,
                canonical_evidence_hash=fact.content_hash.value,
                canonical_payload_hash="f" * 64,
                created_at=fixture.logical_time,
            )
        )


def _prepare_void_authorization(service: DurableAuthorizationService) -> VoidAuthorization:
    fixture_id = "partial-fill-canceled-settled-void"
    reader = FixtureReader()
    fixture = reader.load_decision(fixture_id)
    decision_inputs = _decision_inputs(fixture)
    script = _execution_script(fixture)
    assert isinstance(script, PartialFillCanceledExecutionScript)
    checkpoint = GoldenSimulation(
        decision_inputs=decision_inputs,
        execution_script=script,
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash(fixture_id)),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        capital=Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    ).run_decision_and_execution()
    assert isinstance(checkpoint, PartialExecutionCheckpoint)
    proposal = checkpoint.decision.strategy_evaluation.proposal
    assert proposal is not None
    initial_reservation, initial_order = create_reserved_order(
        campaign_id=checkpoint.decision.campaign.campaign_id,
        proposal=proposal,
        allocation=checkpoint.decision.allocation,
        risk_decision=checkpoint.decision.risk_decision,
    )
    service.initialize_campaign(
        CampaignInitializationCommand(
            campaign_id=checkpoint.decision.campaign.campaign_id,
            contribution=checkpoint.decision.campaign.contribution,
            authorized_loss=checkpoint.decision.campaign.authorized_loss,
            policy=checkpoint.decision.campaign.policy,
            occurred_at=fixture.logical_time,
        )
    )
    service.persist_authorization_inputs(
        PersistAuthorizationInputsCommand(
            campaign_id=checkpoint.decision.campaign.campaign_id,
            proposal=proposal,
            allocation=checkpoint.decision.allocation,
            information_set=checkpoint.decision.information_set,
            market_observation_source_id=decision_inputs.market_observation_source_id,
            recorded_at=fixture.logical_time,
        )
    )
    authorized = service.authorize_and_reserve(
        AuthorizeAndReserveCommand(
            campaign_id=checkpoint.decision.campaign.campaign_id,
            proposal_id=proposal.proposal_id,
            allocation_id=checkpoint.decision.allocation.allocation_id,
            client_order_id=initial_order.client_order_id,
            authorization_at=decision_inputs.risk_authorization_at,
        )
    )
    assert authorized.reservation == initial_reservation
    assert authorized.order == initial_order
    fact = _settlement_inputs(reader.load_settlement(fixture_id)).fact
    assert isinstance(fact, ClassifiedRuleBoundSettlementFact)
    rule = checkpoint.decision.information_set.rule
    assert isinstance(rule, VoidContractRuleVersion)
    return VoidAuthorization(
        campaign_id=checkpoint.decision.campaign.campaign_id,
        order_id=initial_order.order_id,
        script=script,
        events=checkpoint.events,
        fact=fact,
        void_term_content_hash=rule.void_settlement_term.content_hash,
    )


def _persist_void_position_settlement(
    service: DurableAuthorizationService,
    engine: Engine,
) -> VoidAuthorization:
    authorization = _prepare_void_authorization(service)
    execution = DurablePartialFillCanceledExecutionService(postgres_uow_factory(engine))
    for event in authorization.events:
        execution.apply_execution_event(
            ApplyPartialFillExecutionEventCommand(
                campaign_id=authorization.campaign_id,
                order_id=authorization.order_id,
                script=authorization.script,
                event=event,
                recorded_at=event.occurred_at + timedelta(seconds=1),
            )
        )
    DurablePartialFillCanceledSettlementService(postgres_uow_factory(engine)).settle(
        SettlePartialFillCanceledCommand(
            campaign_id=authorization.campaign_id,
            order_id=authorization.order_id,
            fact=authorization.fact,
            recorded_at=authorization.fact.available_at + timedelta(seconds=1),
            void_term_content_hash=authorization.void_term_content_hash,
        )
    )
    return authorization


def _assert_unsafe_void_downgrade_is_atomic(engine: Engine) -> None:
    assert _current_revision(engine) == REVISION_0005
    schema_before = _schema_snapshot(engine)
    evidence_before = _evidence_snapshot(engine)

    with pytest.raises(Exception, match=UNSAFE_VOID_DOWNGRADE_ERROR):
        _downgrade(engine, REVISION_0004)

    assert _current_revision(engine) == REVISION_0005
    assert _schema_snapshot(engine) == schema_before
    assert _evidence_snapshot(engine) == evidence_before


def test_empty_database_upgrade_has_expected_tables_constraints_and_indexes(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    assert set(inspector.get_table_names()) >= DURABLE_TABLES
    assert {index["name"] for index in inspector.get_indexes("risk_reservation")} >= {
        "ix_active_reservation"
    }
    assert {index["name"] for index in inspector.get_indexes("risk_decision")} >= {
        "ix_risk_fingerprint"
    }
    assert {index["name"] for index in inspector.get_indexes("ledger_journal")} >= {
        "ix_journal_campaign_status"
    }
    assert {index["name"] for index in inspector.get_indexes("execution_event")} >= {
        "ix_execution_event_order_sequence"
    }
    assert {index["name"] for index in inspector.get_indexes("position")} >= {
        "ix_position_campaign_open"
    }
    assert {index["name"] for index in inspector.get_indexes("settlement_fact")} >= {
        "ix_settlement_fact_campaign"
    }
    assert {index["name"] for index in inspector.get_indexes("position_settlement")} >= {
        "ix_position_settlement_campaign"
    }

    numeric_columns = [
        ("campaign", "contribution"),
        ("ledger_posting", "amount"),
        ("opportunity_proposal", "executable_ask"),
        ("opportunity_proposal", "void_term_settlement_value"),
        ("opportunity_proposal", "resting_limit_price"),
        ("allocation_decision", "maximum_loss"),
        ("risk_reservation", "current_amount"),
        ("order", "quantity"),
        ("order", "executable_ask"),
        ("execution_event", "fill_quantity"),
        ("execution_event", "fill_price"),
        ("execution_event", "execution_fee"),
        ("position", "open_quantity"),
        ("position", "quantity_increment"),
        ("position", "cost_basis"),
        ("position", "maximum_remaining_loss"),
        ("settlement_fact", "settlement_value"),
        ("position_settlement", "settled_quantity"),
        ("position_settlement", "settlement_value"),
        ("position_settlement", "settlement_cash"),
        ("position_settlement", "realized_contract_pnl"),
        ("strategy_lab_forward_prediction", "model_probability"),
        ("strategy_lab_forward_prediction", "calibrated_probability"),
        ("strategy_lab_forward_prediction", "baseline_probability"),
        ("strategy_lab_forward_prediction", "champion_probability"),
        ("strategy_lab_forward_evaluation", "model_brier"),
        ("strategy_lab_forward_evaluation", "calibrated_brier"),
        ("strategy_lab_forward_evaluation", "baseline_brier"),
        ("strategy_lab_forward_evaluation", "champion_brier"),
        ("strategy_lab_forward_evaluation", "model_log_loss"),
        ("strategy_lab_forward_evaluation", "calibrated_log_loss"),
        ("strategy_lab_forward_evaluation", "baseline_log_loss"),
        ("strategy_lab_forward_evaluation", "champion_log_loss"),
        ("strategy_lab_challenger_evaluation", "calibrated_brier"),
        ("strategy_lab_challenger_evaluation", "baseline_brier"),
        ("strategy_lab_challenger_evaluation", "champion_brier"),
        ("strategy_lab_challenger_evaluation", "calibrated_log_loss"),
        ("strategy_lab_challenger_evaluation", "baseline_log_loss"),
        ("strategy_lab_challenger_evaluation", "champion_log_loss"),
    ]
    for table_name, column_name in numeric_columns:
        column = next(
            item for item in inspector.get_columns(table_name) if item["name"] == column_name
        )
        assert isinstance(column["type"], sa.Numeric)
        assert column["type"].precision == 38
        assert column["type"].scale == 18

    event_columns = {column["name"]: column for column in inspector.get_columns("execution_event")}
    assert set(event_columns) == {
        "event_id",
        "campaign_id",
        "order_id",
        "sequence",
        "event_type",
        "occurred_at",
        "fill_quantity",
        "fill_price",
        "execution_fee",
        "canonical_payload_hash",
        "created_at",
    }
    assert inspector.get_pk_constraint("execution_event")["constrained_columns"] == ["event_id"]
    fill_columns = {"fill_quantity", "fill_price", "execution_fee"}
    assert all(
        not column["nullable"] for name, column in event_columns.items() if name not in fill_columns
    )
    assert all(event_columns[name]["nullable"] for name in fill_columns)
    assert isinstance(event_columns["occurred_at"]["type"], sa.DateTime)
    assert event_columns["occurred_at"]["type"].timezone is True
    assert isinstance(event_columns["created_at"]["type"], sa.DateTime)
    assert event_columns["created_at"]["type"].timezone is True
    assert _check_names(inspector, "execution_event") >= {
        "ck_execution_event_payload_hash",
        "ck_execution_event_positive_sequence",
        "ck_execution_event_supported_type",
        "ck_execution_event_shape",
    }
    assert any(
        constraint["column_names"] == ["order_id", "sequence"]
        for constraint in inspector.get_unique_constraints("execution_event")
    )
    foreign_keys = {
        (tuple(constraint["constrained_columns"]), constraint["referred_table"])
        for constraint in inspector.get_foreign_keys("execution_event")
    }
    assert foreign_keys >= {
        (("campaign_id",), "campaign"),
        (("order_id",), "order"),
    }
    assert _trigger_names(postgres_engine, "execution_event") == {
        "apex_immutable_execution_event_trigger"
    }

    position_columns = {column["name"]: column for column in inspector.get_columns("position")}
    assert set(position_columns) == {
        "position_id",
        "campaign_id",
        "originating_order_id",
        "originating_fill_event_id",
        "instrument_id",
        "rule_version_id",
        "side",
        "open_quantity",
        "quantity_increment",
        "cost_basis",
        "maximum_remaining_loss",
        "settlement_state",
        "canonical_payload_hash",
        "created_at",
    }
    assert all(not column["nullable"] for column in position_columns.values())
    assert _check_names(inspector, "position") >= {
        "ck_position_cost_basis",
        "ck_position_maximum_remaining_loss",
        "ck_position_settlement_lifecycle",
        "ck_position_open_quantity",
        "ck_position_payload_hash",
        "ck_position_quantity_increment",
        "ck_position_side",
    }
    assert _trigger_names(postgres_engine, "position") == {
        "apex_guard_position_insert_trigger",
        "apex_guard_position_settlement_transition_trigger",
    }

    fact_columns = {column["name"] for column in inspector.get_columns("settlement_fact")}
    assert fact_columns >= {
        "settlement_fact_id",
        "campaign_id",
        "source_id",
        "instrument_id",
        "rule_version_id",
        "rule_content_hash",
        "resolution_source",
        "classification",
        "void_term_content_hash",
        "settlement_value",
        "determination_at",
        "source_event_at",
        "available_at",
        "raw_payload_hash",
        "canonical_evidence_hash",
    }
    assert _check_names(inspector, "settlement_fact") >= {
        "ck_settlement_fact_classification",
        "ck_settlement_fact_value_bounds",
        "ck_settlement_fact_void_authority",
        "ck_settlement_fact_timing",
    }
    assert _check_names(inspector, "opportunity_proposal") >= {
        "ck_opportunity_proposal_void_rule_term"
    }
    assert _column_names(inspector, "opportunity_proposal") >= {
        "void_term_outcome_classification",
        "void_term_settlement_value",
        "void_term_settlement_value_text",
        "void_term_content_hash",
    }
    assert _check_names(inspector, "position_settlement") >= {
        "ck_position_settlement_value_bounds",
        "ck_position_settlement_cash_nonnegative",
        "ck_position_settlement_realized_nonzero",
    }
    assert _trigger_names(postgres_engine, "settlement_fact") == {
        "apex_immutable_settlement_fact_trigger"
    }
    assert _trigger_names(postgres_engine, "position_settlement") == {
        "apex_guard_position_settlement_insert_trigger",
        "apex_immutable_position_settlement_trigger",
    }


def test_revision_0001_is_unchanged_and_0002_adds_only_durable_lifecycle_schema(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0001)
    try:
        inspector = inspect(postgres_engine)
        assert "execution_event" not in inspector.get_table_names()
        assert "resting_limit_price" not in _column_names(inspector, "opportunity_proposal")
        assert "order_expires_at" not in _column_names(inspector, "opportunity_proposal")
        assert "executable_ask" not in _column_names(inspector, "order")
        assert "expires_at" not in _column_names(inspector, "order")
        assert "released_at" not in _column_names(inspector, "risk_reservation")
        assert _check_names(inspector, "opportunity_proposal") >= {
            "opportunity_proposal_proposal_type_check"
        }
        assert _check_names(inspector, "order") >= {
            "order_filled_quantity_check",
            "order_last_execution_sequence_check",
            "order_row_version_check",
            "order_state_check",
        }
        assert _check_names(inspector, "risk_reservation") >= {
            "risk_reservation_check",
            "risk_reservation_state_check",
        }
        assert _trigger_names(postgres_engine, "order") == {"apex_immutable_order_trigger"}
        assert _trigger_names(postgres_engine, "risk_reservation") == {
            "apex_immutable_risk_reservation_trigger"
        }
    finally:
        _migrate(postgres_engine, "head")

    inspector = inspect(postgres_engine)
    assert "execution_event" in inspector.get_table_names()
    assert _column_names(inspector, "opportunity_proposal") >= {
        "resting_limit_price",
        "order_expires_at",
    }
    assert _column_names(inspector, "order") >= {"executable_ask", "expires_at"}
    assert "released_at" in _column_names(inspector, "risk_reservation")
    assert _check_names(inspector, "opportunity_proposal") >= {
        "ck_opportunity_proposal_durable_type",
        "ck_opportunity_proposal_resting_terms",
    }
    assert _check_names(inspector, "order") >= {
        "ck_order_durable_expiring_terms",
        "ck_order_durable_filled_quantity",
        "ck_order_durable_lifecycle",
    }
    assert _check_names(inspector, "risk_reservation") >= {
        "ck_risk_reservation_current_amount",
        "ck_risk_reservation_lifecycle",
    }
    assert _trigger_names(postgres_engine, "order") == {
        "apex_guard_order_execution_transition_trigger"
    }
    assert _trigger_names(postgres_engine, "risk_reservation") == {
        "apex_guard_reservation_execution_transition_trigger"
    }


def test_revision_0002_is_unchanged_before_partial_fill_schema(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0002)
    try:
        inspector = inspect(postgres_engine)
        assert "position" not in inspector.get_table_names()
        assert _column_names(inspector, "execution_event") == {
            "event_id",
            "campaign_id",
            "order_id",
            "sequence",
            "event_type",
            "occurred_at",
            "canonical_payload_hash",
            "created_at",
        }
        assert "ck_execution_event_shape" not in _check_names(inspector, "execution_event")
        assert _check_names(inspector, "execution_event") >= {"ck_execution_event_supported_type"}
        assert _check_names(inspector, "order") >= {
            "ck_order_durable_filled_quantity",
            "ck_order_durable_lifecycle",
        }
        assert _check_names(inspector, "risk_reservation") >= {"ck_risk_reservation_lifecycle"}
    finally:
        _migrate(postgres_engine, "head")


def test_revision_0003_is_unchanged_before_no_settlement_schema(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0003)
    try:
        inspector = inspect(postgres_engine)
        assert "position" in inspector.get_table_names()
        assert "settlement_fact" not in inspector.get_table_names()
        assert "position_settlement" not in inspector.get_table_names()
        assert _check_names(inspector, "position") >= {"ck_position_open_only"}
        assert _trigger_names(postgres_engine, "position") == {
            "apex_guard_position_insert_trigger",
            "apex_immutable_position_trigger",
        }
    finally:
        _migrate(postgres_engine, "head")


def test_revision_0004_is_unchanged_before_void_settlement_schema(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0004)
    try:
        inspector = inspect(postgres_engine)
        assert "void_term_content_hash" not in _column_names(inspector, "settlement_fact")
        assert "void_term_content_hash" not in _column_names(inspector, "opportunity_proposal")
        assert _check_names(inspector, "settlement_fact") >= {"ck_settlement_fact_no_only"}
        assert _check_names(inspector, "position_settlement") >= {
            "ck_position_settlement_no_value",
            "ck_position_settlement_zero_cash",
            "ck_position_settlement_realized_loss",
        }
    finally:
        _migrate(postgres_engine, "head")


def test_empty_revision_0005_downgrades_to_0004_and_reupgrades(postgres_engine: Engine) -> None:
    _downgrade(postgres_engine, REVISION_0004)
    try:
        assert _current_revision(postgres_engine) == REVISION_0004
        assert "void_term_content_hash" not in _column_names(
            inspect(postgres_engine), "settlement_fact"
        )
        _migrate(postgres_engine, "head")
        assert _current_revision(postgres_engine) == HEAD_REVISION
        assert "void_term_content_hash" in _column_names(
            inspect(postgres_engine), "settlement_fact"
        )
    finally:
        _migrate(postgres_engine, "head")


def test_empty_revision_0016_downgrades_to_0015_and_reupgrades(postgres_engine: Engine) -> None:
    _downgrade(postgres_engine, REVISION_0015)
    try:
        assert _current_revision(postgres_engine) == REVISION_0015
        assert "operator_action" not in inspect(postgres_engine).get_table_names()
        _migrate(postgres_engine, "head")
        assert _current_revision(postgres_engine) == HEAD_REVISION
        assert {"campaign_operator_metadata", "operator_action"}.issubset(
            inspect(postgres_engine).get_table_names()
        )
    finally:
        _migrate(postgres_engine, "head")


def test_empty_revision_0018_downgrades_to_0017_and_reupgrades(postgres_engine: Engine) -> None:
    _downgrade(postgres_engine, REVISION_0017)
    try:
        assert _current_revision(postgres_engine) == REVISION_0017
        assert "microstructure_feature_snapshot" not in inspect(postgres_engine).get_table_names()
        _migrate(postgres_engine, "head")
        assert _current_revision(postgres_engine) == HEAD_REVISION
        assert {"microstructure_feature_snapshot", "microstructure_prediction_feature"}.issubset(
            inspect(postgres_engine).get_table_names()
        )
    finally:
        _migrate(postgres_engine, "head")


def test_empty_revision_0019_downgrades_to_0018_and_reupgrades(postgres_engine: Engine) -> None:
    _downgrade(postgres_engine, "0018_orderbook_microstructure")
    try:
        assert _current_revision(postgres_engine) == "0018_orderbook_microstructure"
        assert "market_dynamics_feature_snapshot" not in inspect(postgres_engine).get_table_names()
        _migrate(postgres_engine, "head")
        assert _current_revision(postgres_engine) == HEAD_REVISION
        assert {
            "market_dynamics_feature_snapshot",
            "market_dynamics_prediction_feature",
        }.issubset(inspect(postgres_engine).get_table_names())
    finally:
        _migrate(postgres_engine, "head")


def test_empty_revision_0020_downgrades_to_0019_and_reupgrades(postgres_engine: Engine) -> None:
    _downgrade(postgres_engine, "0019_market_dynamics")
    try:
        assert _current_revision(postgres_engine) == "0019_market_dynamics"
        assert "multi_strategy_shadow_cycle" not in inspect(postgres_engine).get_table_names()
        _migrate(postgres_engine, "head")
        assert _current_revision(postgres_engine) == HEAD_REVISION
        assert {
            "multi_strategy_shadow_cycle",
            "multi_strategy_shadow_invocation",
            "shadow_capital_allocation",
        }.issubset(inspect(postgres_engine).get_table_names())
    finally:
        _migrate(postgres_engine, "head")


def test_empty_revision_0021_downgrades_to_0020_and_reupgrades(postgres_engine: Engine) -> None:
    _downgrade(postgres_engine, REVISION_0020)
    try:
        assert _current_revision(postgres_engine) == REVISION_0020
        assert "shadow_evidence_plan" not in inspect(postgres_engine).get_table_names()
        _migrate(postgres_engine, "head")
        assert _current_revision(postgres_engine) == HEAD_REVISION
        assert {
            "shadow_evidence_plan",
            "shadow_evidence_cohort_member",
            "shadow_evidence_iteration",
            "shadow_worker_lease",
            "public_outcome_fact",
            "shadow_forward_evaluation",
            "performance_evaluation_run",
        }.issubset(inspect(postgres_engine).get_table_names())
    finally:
        _migrate(postgres_engine, "head")


def test_legacy_shadow_prediction_columns_upgrade_with_neutral_values_and_preserve_hashes(
    postgres_engine: Engine, service: DurableAuthorizationService
) -> None:
    """0021 repairs only the historical dynamic-metadata schema gap."""

    _downgrade(postgres_engine, REVISION_0020)
    try:
        _fixture, decision_inputs, _reader = _initialize_fixture_campaign(
            service, "partial-fill-canceled"
        )
        timestamp = datetime(2032, 1, 1, tzinfo=UTC)
        with postgres_engine.begin() as connection:
            connection.execute(
                provider_ingestion_run.insert().values(
                    ingestion_run_id="legacy-prediction-ingestion",
                    provider="KALSHI",
                    capability="PUBLIC_REST_MARKET_DISCOVERY",
                    started_at=timestamp,
                    completed_at=timestamp,
                    requested_status="open",
                    page_size=1,
                    maximum_pages=1,
                    state="COMPLETED",
                    page_count=1,
                    market_count=1,
                    application_version="test",
                    schema_version="1",
                    canonical_configuration_hash="a" * 64,
                )
            )
            connection.execute(
                raw_provider_response.insert().values(
                    raw_response_id="legacy-prediction-raw",
                    ingestion_run_id="legacy-prediction-ingestion",
                    provider="KALSHI",
                    endpoint_identity="KALSHI_GET_MARKETS",
                    request_page_ordinal=1,
                    request_cursor=None,
                    received_at=timestamp,
                    http_status=200,
                    response_content_type="application/json",
                    raw_payload=b'{"markets": []}',
                    raw_payload_sha256="b" * 64,
                    response_size=15,
                    provider_response_cursor=None,
                    schema_version="1",
                    created_at=timestamp,
                )
            )
            connection.execute(
                normalized_market_snapshot.insert().values(
                    snapshot_id="legacy-prediction-market",
                    ingestion_run_id="legacy-prediction-ingestion",
                    raw_response_id="legacy-prediction-raw",
                    provider="KALSHI",
                    provider_market_ticker="KX-LEGACY-PREDICTION",
                    event_ticker="KX-LEGACY-PREDICTION-EVENT",
                    title=None,
                    subtitle=None,
                    status="open",
                    open_time=timestamp - timedelta(minutes=1),
                    close_time=timestamp + timedelta(days=1),
                    latest_expiration_time=None,
                    observed_at=timestamp,
                    yes_bid=Decimal("0.49"),
                    yes_ask=Decimal("0.51"),
                    no_bid=Decimal("0.49"),
                    no_ask=Decimal("0.51"),
                    yes_bid_size=Decimal("1"),
                    yes_ask_size=Decimal("1"),
                    volume=None,
                    volume_24h=None,
                    open_interest=None,
                    liquidity=None,
                    rules_primary="retained exact legacy rule",
                    rules_secondary=None,
                    rule_content_hash="c" * 64,
                    provider_payload_ordinal=0,
                    canonical_snapshot_hash="d" * 64,
                    data_quality_state="ELIGIBLE",
                    created_at=timestamp,
                )
            )

        prediction = build_market_consensus_baseline_prediction(
            market=RetainedMarketEvidence(
                snapshot_id="legacy-prediction-market",
                provider="KALSHI",
                provider_market_ticker="KX-LEGACY-PREDICTION",
                provider_event_ticker="KX-LEGACY-PREDICTION-EVENT",
                observed_at=timestamp,
                available_at=timestamp,
                close_at=timestamp + timedelta(days=1),
                rule_text="retained exact legacy rule",
                rule_content_hash=ContentHash("c" * 64),
                yes_bid=Decimal("0.49"),
                yes_ask=Decimal("0.51"),
                no_ask=Decimal("0.51"),
                visible_yes_quantity=Decimal("1"),
                fee_model_version=None,
                expected_fee=Decimal("0"),
            ),
            campaign_id=decision_inputs.campaign_id,
            decision_at=timestamp,
        )
        store = postgres_strategy_shadow_store(postgres_engine)
        assert store.persist_prediction(prediction) is False
        with postgres_engine.connect() as connection:
            before_hashes = connection.execute(
                sa.select(
                    shadow_prediction.c.canonical_input_hash,
                    shadow_prediction.c.canonical_prediction_hash,
                ).where(shadow_prediction.c.prediction_id == prediction.prediction_id)
            ).one()
            financial_counts = tuple(
                int(connection.scalar(sa.text(f"SELECT count(*) FROM {table_name}")))
                for table_name in (
                    "risk_reservation",
                    '"order"',
                    "position",
                    "settlement_fact",
                    "ledger_journal",
                )
            )
        with postgres_engine.begin() as connection:
            connection.execute(
                sa.text("ALTER TABLE shadow_prediction DROP COLUMN correlation_buckets")
            )
            connection.execute(sa.text("ALTER TABLE shadow_prediction DROP COLUMN operator_note"))

        _migrate(postgres_engine, REVISION_0021)
        assert _current_revision(postgres_engine) == REVISION_0021
        assert {"correlation_buckets", "operator_note"}.issubset(
            _column_names(inspect(postgres_engine), "shadow_prediction")
        )
        loaded = store.prediction(prediction.prediction_id)
        assert loaded is not None
        assert loaded.correlation_buckets == ()
        assert loaded.operator_note is None
        with postgres_engine.connect() as connection:
            after_hashes = connection.execute(
                sa.select(
                    shadow_prediction.c.canonical_input_hash,
                    shadow_prediction.c.canonical_prediction_hash,
                ).where(shadow_prediction.c.prediction_id == prediction.prediction_id)
            ).one()
            assert after_hashes == before_hashes
            assert financial_counts == tuple(
                int(connection.scalar(sa.text(f"SELECT count(*) FROM {table_name}")))
                for table_name in (
                    "risk_reservation",
                    '"order"',
                    "position",
                    "settlement_fact",
                    "ledger_journal",
                )
            )

        _downgrade(postgres_engine, REVISION_0020)
        assert _current_revision(postgres_engine) == REVISION_0020
        assert {"correlation_buckets", "operator_note"}.isdisjoint(
            _column_names(inspect(postgres_engine), "shadow_prediction")
        )
        _migrate(postgres_engine, REVISION_0021)
        assert _current_revision(postgres_engine) == REVISION_0021
        assert store.prediction(prediction.prediction_id) is not None

        with postgres_engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(
                        shadow_prediction.c.canonical_input_hash,
                        shadow_prediction.c.canonical_prediction_hash,
                    ).where(shadow_prediction.c.prediction_id == prediction.prediction_id)
                ).one()
                == before_hashes
            )
            legacy_row = dict(
                connection.execute(
                    sa.select(shadow_prediction).where(
                        shadow_prediction.c.prediction_id == prediction.prediction_id
                    )
                )
                .mappings()
                .one()
            )

        invalid_buckets = dict(legacy_row)
        invalid_buckets["prediction_id"] = "legacy-prediction-invalid-buckets"
        invalid_buckets["correlation_buckets"] = ["macro", "macro"]
        with (
            pytest.raises(Exception, match="ck_shadow_prediction_correlation_buckets"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(shadow_prediction.insert().values(**invalid_buckets))

        retained_note = dict(legacy_row)
        retained_note["prediction_id"] = "legacy-prediction-retained-note"
        retained_note["operator_note"] = "retained operator note"
        retained_note["correlation_buckets"] = ["macro"]
        with postgres_engine.begin() as connection:
            connection.execute(shadow_prediction.insert().values(**retained_note))
        before_tables = tuple(sorted(inspect(postgres_engine).get_table_names()))
        with pytest.raises(Exception, match="retained correlation buckets would be lost"):
            _downgrade(postgres_engine, REVISION_0020)
        assert _current_revision(postgres_engine) == REVISION_0021
        assert tuple(sorted(inspect(postgres_engine).get_table_names())) == before_tables
    finally:
        _migrate(postgres_engine, "head")


def test_retained_shadow_plan_blocks_0021_downgrade_atomically(
    postgres_engine: Engine, service: DurableAuthorizationService
) -> None:
    _downgrade(postgres_engine, REVISION_0021)
    try:
        _fixture, decision_inputs, _reader = _initialize_fixture_campaign(
            service, "partial-fill-canceled"
        )
        store = postgres_shadow_operations_store(postgres_engine)
        plan, _replay = store.create_plan(
            CreateShadowEvidencePlanCommand(
                campaign_id=decision_inputs.campaign_id,
                name="Retained finite evidence",
                configuration=ShadowEvidencePlanConfiguration(
                    iterations=3, market_count=3, max_enrichments=3
                ),
                idempotency_key="retained-shadow-plan-v1",
                requested_at=MIGRATION_NOW,
            )
        )
        before_tables = tuple(sorted(inspect(postgres_engine).get_table_names()))
        with pytest.raises(
            Exception, match="cannot downgrade 0021 with retained shadow operations evidence"
        ):
            _downgrade(postgres_engine, REVISION_0020)
        assert _current_revision(postgres_engine) == REVISION_0021
        assert tuple(sorted(inspect(postgres_engine).get_table_names())) == before_tables
        assert store.plan(str(plan["plan_id"])) is not None
    finally:
        _migrate(postgres_engine, "head")


def test_retained_multi_strategy_evidence_blocks_0020_downgrade_atomically(
    postgres_engine: Engine, service: DurableAuthorizationService
) -> None:
    _downgrade(postgres_engine, REVISION_0020)
    try:
        _fixture, decision_inputs, _reader = _initialize_fixture_campaign(
            service, "partial-fill-canceled"
        )
        now = datetime(2031, 1, 1, tzinfo=UTC)
        with postgres_engine.begin() as connection:
            connection.execute(
                multi_strategy_shadow_session.insert().values(
                    session_id="multi-strategy-migration-session",
                    campaign_id=decision_inputs.campaign_id,
                    iterations_requested=3,
                    interval_seconds=10,
                    market_limit=3,
                    maximum_pages=1,
                    maximum_enrichments=3,
                    orderbook_depth=1,
                    started_at=now,
                    finalized_at=now,
                    state="COMPLETED",
                    completed_iterations=3,
                    failed_iterations=0,
                    last_completed_stage="MULTI_STRATEGY_CYCLE_COMPLETED",
                    interrupted_at=None,
                    interruption_reason=None,
                    canonical_request_hash="a" * 64,
                    canonical_result_hash="b" * 64,
                    last_error_code=None,
                    created_at=now,
                )
            )
        before_tables = tuple(sorted(inspect(postgres_engine).get_table_names()))
        with pytest.raises(
            Exception, match="cannot downgrade 0020 with retained multi-strategy shadow evidence"
        ):
            _downgrade(postgres_engine, "0019_market_dynamics")
        assert _current_revision(postgres_engine) == REVISION_0020
        assert tuple(sorted(inspect(postgres_engine).get_table_names())) == before_tables
        with postgres_engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(multi_strategy_shadow_session)
                )
                == 1
            )
    finally:
        _migrate(postgres_engine, "head")


def test_retained_market_dynamics_feature_blocks_0019_downgrade_atomically(
    postgres_engine: Engine,
) -> None:
    """The guard runs before DDL and preserves retained temporal evidence."""

    now = datetime(2030, 1, 1, tzinfo=UTC)
    postgres_strategy_shadow_store(postgres_engine).register_catalog()
    with postgres_engine.begin() as connection:
        connection.execute(
            provider_ingestion_run.insert().values(
                ingestion_run_id="dynamics-migration-run",
                provider="KALSHI",
                capability="PUBLIC_REST_MARKET_DISCOVERY",
                started_at=now,
                completed_at=now,
                requested_status="open",
                page_size=1,
                maximum_pages=1,
                state="COMPLETED",
                page_count=1,
                market_count=1,
                application_version="test",
                schema_version="1",
                canonical_configuration_hash="a" * 64,
            )
        )
        connection.execute(
            raw_provider_response.insert().values(
                raw_response_id="dynamics-migration-raw",
                ingestion_run_id="dynamics-migration-run",
                provider="KALSHI",
                endpoint_identity="KALSHI_GET_MARKETS",
                request_page_ordinal=1,
                request_cursor=None,
                received_at=now,
                http_status=200,
                response_content_type="application/json",
                raw_payload=b'{"markets": []}',
                raw_payload_sha256="b" * 64,
                response_size=15,
                provider_response_cursor=None,
                schema_version="1",
                created_at=now,
            )
        )
        connection.execute(
            normalized_market_snapshot.insert().values(
                snapshot_id="dynamics-migration-market",
                ingestion_run_id="dynamics-migration-run",
                raw_response_id="dynamics-migration-raw",
                provider="KALSHI",
                provider_market_ticker="KX-DYNAMICS-MIGRATION",
                event_ticker="KX-DYNAMICS-MIGRATION-EVENT",
                title=None,
                subtitle=None,
                status="open",
                open_time=now - timedelta(minutes=1),
                close_time=now + timedelta(days=1),
                latest_expiration_time=None,
                observed_at=now,
                yes_bid=Decimal("0.49"),
                yes_ask=Decimal("0.51"),
                no_bid=Decimal("0.49"),
                no_ask=Decimal("0.51"),
                yes_bid_size=Decimal("1"),
                yes_ask_size=Decimal("1"),
                volume=None,
                volume_24h=None,
                open_interest=None,
                liquidity=None,
                rules_primary="retained exact rule",
                rules_secondary=None,
                rule_content_hash="c" * 64,
                provider_payload_ordinal=0,
                canonical_snapshot_hash="d" * 64,
                data_quality_state="ELIGIBLE",
                created_at=now,
            )
        )
        connection.execute(
            market_dynamics_feature_snapshot.insert().values(
                feature_snapshot_id="dynamics-migration-feature",
                strategy_id="kalshi-market-dynamics-v1",
                strategy_version="v1",
                market_snapshot_id="dynamics-migration-market",
                first_market_snapshot_id="dynamics-migration-market",
                latest_market_snapshot_id="dynamics-migration-market",
                provider_market_ticker="KX-DYNAMICS-MIGRATION",
                provider_event_ticker="KX-DYNAMICS-MIGRATION-EVENT",
                series_ticker="KX-DYNAMICS-MIGRATION-SERIES",
                rule_content_hash="c" * 64,
                decision_at=now,
                observation_count=3,
                first_observed_at=now - timedelta(seconds=20),
                latest_observed_at=now,
                history_duration_seconds=20,
                maximum_adjacent_gap_seconds=10,
                minimum_adjacent_gap_seconds=10,
                earliest_midpoint=Decimal("0.40"),
                latest_midpoint=Decimal("0.50"),
                earliest_spread=Decimal("0.02"),
                latest_spread=Decimal("0.02"),
                window_momentum=Decimal("0.10"),
                recent_momentum=Decimal("0.05"),
                positive_move_count=2,
                negative_move_count=0,
                zero_move_count=0,
                nonzero_move_count=2,
                signed_directional_persistence=Decimal("1"),
                normalized_window_momentum=Decimal("1"),
                normalized_recent_momentum=Decimal("1"),
                window_momentum_contribution=Decimal("0.025"),
                recent_momentum_contribution=Decimal("0.025"),
                directional_persistence_contribution=Decimal("0.02"),
                raw_adjustment=Decimal("0.07"),
                clamped_adjustment=Decimal("0.05"),
                feature_state="VALID",
                reason_codes=["FEATURES_VALID"],
                configuration_hash="e" * 64,
                canonical_input_hash="f" * 64,
                canonical_feature_hash="0" * 64,
                created_at=now,
            )
        )
    with pytest.raises(
        Exception, match="cannot downgrade 0019 with retained market dynamics shadow evidence"
    ):
        _downgrade(postgres_engine, "0018_orderbook_microstructure")
    assert _current_revision(postgres_engine) == "0019_market_dynamics"
    assert "market_dynamics_feature_snapshot" in inspect(postgres_engine).get_table_names()
    assert "multi_strategy_shadow_cycle" not in inspect(postgres_engine).get_table_names()
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(market_dynamics_feature_snapshot)
            )
            == 1
        )
    _migrate(postgres_engine, "head")


def test_retained_operator_console_evidence_blocks_0016_downgrade_atomically(
    postgres_engine: Engine, service: DurableAuthorizationService
) -> None:
    _downgrade(postgres_engine, REVISION_0016)
    fixture, decision_inputs, _ = _initialize_fixture_campaign(service, "partial-fill-canceled")
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO campaign_operator_metadata "
                "(campaign_id, name, operating_mode, starts_at, expires_at, operator_note, "
                "canonical_payload_hash, created_at) "
                "VALUES (:campaign_id, 'retained console evidence', 'PAPER', :starts_at, "
                ":expires_at, 'migration guard proof', :content_hash, :created_at)"
            ),
            {
                "campaign_id": decision_inputs.campaign_id,
                "starts_at": fixture.logical_time,
                "expires_at": fixture.logical_time + timedelta(days=1),
                "content_hash": "a" * 64,
                "created_at": fixture.logical_time,
            },
        )
    before_tables = tuple(sorted(inspect(postgres_engine).get_table_names()))
    with pytest.raises(
        Exception, match="cannot downgrade 0016 with retained operator console evidence"
    ):
        _downgrade(postgres_engine, REVISION_0015)
    assert _current_revision(postgres_engine) == REVISION_0016
    assert tuple(sorted(inspect(postgres_engine).get_table_names())) == before_tables
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM campaign_operator_metadata")) == 1


def test_retained_no_settlement_evidence_permits_0005_downgrade_and_reupgrade(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
) -> None:
    _downgrade(postgres_engine, REVISION_0005)
    _seed_settlement_fact(
        service,
        postgres_engine,
        decision_fixture_id="partial-fill-canceled",
        settlement_fixture_id="partial-fill-canceled-settled-no",
    )

    _downgrade(postgres_engine, REVISION_0004)
    try:
        assert _current_revision(postgres_engine) == REVISION_0004
        with postgres_engine.connect() as connection:
            row = (
                connection.execute(
                    sa.text(
                        "SELECT classification, settlement_value FROM settlement_fact "
                        "WHERE settlement_fact_id = 'migration-no-fact'"
                    )
                )
                .mappings()
                .one()
            )
        assert row == {"classification": "NO", "settlement_value": Decimal(0)}
        _migrate(postgres_engine, "head")
        assert _current_revision(postgres_engine) == HEAD_REVISION
    finally:
        _migrate(postgres_engine, "head")


def test_retained_void_settlement_fact_blocks_0005_downgrade_atomically(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
) -> None:
    _downgrade(postgres_engine, REVISION_0005)
    _seed_settlement_fact(
        service,
        postgres_engine,
        decision_fixture_id="partial-fill-canceled-settled-void",
        settlement_fixture_id="partial-fill-canceled-settled-void",
    )
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM settlement_fact "
                    "WHERE classification = 'VOID' AND void_term_content_hash IS NOT NULL"
                )
            )
            == 1
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(position_settlement)) == 0

    try:
        _assert_unsafe_void_downgrade_is_atomic(postgres_engine)
    finally:
        _migrate(postgres_engine, "head")


def test_retained_void_position_settlement_blocks_0005_downgrade_through_its_fact(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
) -> None:
    _downgrade(postgres_engine, REVISION_0005)
    _persist_void_position_settlement(service, postgres_engine)
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM position_settlement settlement "
                    "JOIN settlement_fact fact "
                    "  ON fact.settlement_fact_id = settlement.settlement_fact_id "
                    "WHERE fact.classification = 'VOID' "
                    "  AND fact.void_term_content_hash IS NOT NULL"
                )
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM ledger_journal WHERE journal_type = 'SETTLEMENT'")
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM ledger_posting posting "
                    "JOIN ledger_journal journal ON journal.journal_id = posting.journal_id "
                    "WHERE journal.journal_type = 'SETTLEMENT'"
                )
            )
            == 3
        )

    try:
        _assert_unsafe_void_downgrade_is_atomic(postgres_engine)
    finally:
        _migrate(postgres_engine, "head")


def test_retained_void_proposal_term_evidence_blocks_0005_downgrade_atomically(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
) -> None:
    _downgrade(postgres_engine, REVISION_0005)
    authorization = _prepare_void_authorization(service)
    with postgres_engine.connect() as connection:
        row = (
            connection.execute(
                sa.select(
                    durable_opportunity_proposal.c.void_term_outcome_classification,
                    durable_opportunity_proposal.c.void_term_settlement_value,
                    durable_opportunity_proposal.c.void_term_settlement_value_text,
                    durable_opportunity_proposal.c.void_term_content_hash,
                ).where(durable_opportunity_proposal.c.campaign_id == authorization.campaign_id)
            )
            .mappings()
            .one()
        )
    assert row == {
        "void_term_outcome_classification": "VOID",
        "void_term_settlement_value": Decimal("0.40"),
        "void_term_settlement_value_text": "0.40",
        "void_term_content_hash": authorization.void_term_content_hash.value,
    }

    try:
        _assert_unsafe_void_downgrade_is_atomic(postgres_engine)
    finally:
        _migrate(postgres_engine, "head")


def test_clean_downgrade_then_upgrade_is_reversible(postgres_engine: Engine) -> None:
    _downgrade(postgres_engine, "base")
    assert DURABLE_TABLES.isdisjoint(inspect(postgres_engine).get_table_names())
    _migrate(postgres_engine, "head")
    assert set(inspect(postgres_engine).get_table_names()) >= DURABLE_TABLES


def test_database_rejects_a_posted_unbalanced_journal(
    postgres_engine: Engine,
    service,
    initialize_campaign,
) -> None:
    initialize_campaign(campaign_id="campaign-unbalanced")
    with postgres_engine.begin() as connection:
        account_id = connection.scalar(
            sa.text(
                "SELECT account_id FROM ledger_account "
                "WHERE campaign_id = 'campaign-unbalanced' AND account_code = 'CASH'"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO ledger_journal "
                "(journal_id, campaign_id, source_kind, source_id, journal_type, "
                "occurred_at, status, canonical_payload_hash) VALUES "
                "('unbalanced', 'campaign-unbalanced', 'test', 'unbalanced', "
                "'CONTRIBUTION', now(), 'PENDING', repeat('a', 64))"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO ledger_posting "
                "(posting_id, journal_id, account_id, sequence, side, amount) "
                "VALUES ('unbalanced-posting', 'unbalanced', :account_id, 1, 'DEBIT', :amount)"
            ),
            {"account_id": account_id, "amount": Decimal("1.25")},
        )
        with pytest.raises(sa.exc.DBAPIError):
            connection.execute(
                sa.text(
                    "UPDATE ledger_journal SET status = 'POSTED' WHERE journal_id = 'unbalanced'"
                )
            )
