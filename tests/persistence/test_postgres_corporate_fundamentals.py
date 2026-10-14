"""PostgreSQL persistence proofs for migration 0088: every retained
Corporate Fundamental, Filing, and Event Intelligence v1 table round-trips,
replays idempotently, enforces immutability, keeps authority 'NONE', and
blocks a destructive downgrade while evidence is retained.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.corporate_fundamentals_postgres import (
    CORPORATE_FUNDAMENTALS_TABLES,
    PostgresCorporateFundamentalsRepository,
)
from apex.domain.corporate_fundamentals import (
    CorporateDataQualityReport,
    CorporateEvidenceCaptureBounds,
    CorporateEvidenceCaptureCheckpoint,
    CorporateEvidenceCohortMember,
    CorporateEvidenceCohortPlan,
    CorporateEvidenceCohortResolution,
    CorporateFilingEvent,
    CorporateHistoryDatasetSnapshot,
    FilingForm,
    FiscalPeriod,
    FundamentalConcept,
    FundamentalFact,
    IssuerCaptureOutcome,
    IssuerCaptureStatus,
    ReplayCorporateEvidenceAttachment,
    assess_issuer_quality,
    build_corporate_fundamental_snapshot,
    compute_derived_features,
    resolve_fact_as_of,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
HEAD_REVISION = "0118_evidence_gap_recovery"
NOW = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _plan() -> CorporateEvidenceCohortPlan:
    resolution = CorporateEvidenceCohortResolution(
        members=(
            CorporateEvidenceCohortMember(
                "sec-aapl", "AAPL", "320193", "TECHNOLOGY", "COMMON_STOCK"
            ),
            CorporateEvidenceCohortMember(
                "sec-msft", "MSFT", "789019", "TECHNOLOGY", "COMMON_STOCK"
            ),
        ),
        excluded_missing_cik=("SPY",),
        excluded_unresolved=(),
    )
    return CorporateEvidenceCohortPlan.create(
        cohort_policy_id="genuine_multi_asset_cohort_policy_test",
        cohort_policy_hash="0" * 64,
        resolution=resolution,
        bounds=CorporateEvidenceCaptureBounds.create(),
        created_at=NOW,
    )


def _filing(*, accession: str = "0000320193-24-000123") -> CorporateFilingEvent:
    return CorporateFilingEvent.create(
        stable_security_id="sec-aapl",
        cik="320193",
        accession_number=accession,
        form=FilingForm.FORM_10K,
        report_period="2024-09-28",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, 16, 30, tzinfo=UTC),
        primary_document="aapl-20240928.htm",
        file_number="001-36743",
        is_xbrl=True,
        is_inline_xbrl=True,
    )


def _fact() -> FundamentalFact:
    return FundamentalFact.create(
        stable_security_id="sec-aapl",
        cik="320193",
        concept=FundamentalConcept.REVENUE,
        source_taxonomy="us-gaap",
        source_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        fiscal_year=2024,
        fiscal_period=FiscalPeriod.FY,
        period_start="2023-10-01",
        period_end="2024-09-28",
        value=Decimal("391035000000"),
        accession_number="0000320193-24-000123",
        form="10-K",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, 16, 30, tzinfo=UTC),
    )


def _seed_plan(engine: Engine) -> CorporateEvidenceCohortPlan:
    return PostgresCorporateFundamentalsRepository(engine).save_plan(_plan())


def _checkpoint(plan: CorporateEvidenceCohortPlan) -> CorporateEvidenceCaptureCheckpoint:
    outcome = IssuerCaptureOutcome(
        "sec-aapl",
        "320193",
        IssuerCaptureStatus.CAPTURED,
        1,
        1,
        5000,
        200000,
        2,
        (),
        ("raw-sha-1",),
    )
    return CorporateEvidenceCaptureCheckpoint.create(
        plan=plan,
        run_id="run-1",
        outcomes=(outcome,),
        previously_processed_ciks=(),
        total_requests_this_run=2,
        total_bytes_this_run=205000,
        runtime_seconds=Decimal("3.5"),
        created_at=NOW,
    )


def test_clean_database_is_at_current_head_with_corporate_fundamentals_tables(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(CORPORATE_FUNDAMENTALS_TABLES)
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        )


def test_plan_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    plan = _plan()
    repository.save_plan(plan)
    repository.save_plan(plan)  # idempotent replay
    fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
    assert fresh.plan_by_id(plan.plan_id) == plan
    assert fresh.latest_plan() == plan


def test_filing_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    filing = _filing()
    repository.save_filing(filing)
    repository.save_filing(filing)  # idempotent replay
    fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
    assert fresh.filings_for("sec-aapl") == (filing,)
    assert fresh.filing_by_accession(filing.accession_number) == filing
    assert fresh.all_filings_for_plan(_plan().plan_id) == (filing,)


def test_fact_round_trips_with_exact_decimal_precision(postgres_engine: Engine) -> None:
    _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    fact = _fact()
    repository.save_fact(fact)
    repository.save_fact(fact)  # idempotent replay
    fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
    retrieved = fresh.facts_for("sec-aapl")
    assert retrieved == (fact,)
    assert retrieved[0].value == Decimal("391035000000")
    assert fresh.all_facts_for_plan(_plan().plan_id) == (fact,)


def test_checkpoint_round_trips_and_tracks_processed_ciks(postgres_engine: Engine) -> None:
    plan = _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    checkpoint = _checkpoint(plan)
    repository.save_checkpoint(checkpoint)
    repository.save_checkpoint(checkpoint)  # idempotent replay
    fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
    assert fresh.checkpoints_for_plan(plan.plan_id) == (checkpoint,)
    assert fresh.latest_checkpoint(plan.plan_id) == checkpoint
    assert fresh.processed_ciks(plan.plan_id) == ("320193",)


def test_quality_report_and_dataset_snapshot_round_trip_and_bind_together(
    postgres_engine: Engine,
) -> None:
    plan = _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    filing = repository.save_filing(_filing())
    fact = repository.save_fact(_fact())
    checkpoint = repository.save_checkpoint(_checkpoint(plan))

    assessment = assess_issuer_quality(
        stable_security_id="sec-aapl", symbol="AAPL", cik="320193", filings=(filing,), facts=(fact,)
    )
    report = CorporateDataQualityReport.create(
        plan=plan,
        issuer_assessments=(assessment,),
        all_filings=(filing,),
        all_facts=(fact,),
        checkpoint_complete=False,
        created_at=NOW,
    )
    repository.save_quality_report(report)
    repository.save_quality_report(report)  # idempotent replay

    dataset = CorporateHistoryDatasetSnapshot.create(
        plan=plan,
        checkpoint_ids=(checkpoint.checkpoint_id,),
        filings=(filing,),
        facts=(fact,),
        quality_report=report,
        source_receipt_hashes=("raw-sha-1",),
        created_at=NOW,
    )
    repository.save_dataset_snapshot(dataset)

    fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
    assert fresh.quality_report_by_id(report.report_id) == report
    assert fresh.latest_quality_report(plan.plan_id) == report
    assert fresh.dataset_snapshot_by_id(dataset.dataset_id) == dataset
    assert fresh.latest_dataset_snapshot(plan.plan_id) == dataset
    assert dataset.quality_report_id == report.report_id
    assert filing.filing_event_id in dataset.filing_event_ids
    assert fact.fact_id in dataset.fact_ids


def test_legacy_quality_report_row_without_lookback_fields_still_rehydrates(
    postgres_engine: Engine,
) -> None:
    """A quality-report row persisted before this milestone added new
    fields (``issuers_not_yet_attempted`` and friends) must keep reading
    back cleanly with honest sentinel defaults -- proves the
    ``_decoded_with_defaults`` backward-compatibility shim genuinely
    works, not just that it exists."""
    import json

    from apex.domain.values import canonical_json_bytes

    plan = _seed_plan(postgres_engine)
    filing = _filing()
    fact = _fact()
    assessment = assess_issuer_quality(
        stable_security_id="sec-aapl", symbol="AAPL", cik="320193", filings=(filing,), facts=(fact,)
    )
    report = CorporateDataQualityReport.create(
        plan=plan,
        issuer_assessments=(assessment,),
        all_filings=(filing,),
        all_facts=(fact,),
        checkpoint_complete=False,
        created_at=NOW,
    )
    legacy_payload = json.loads(canonical_json_bytes(report))
    for key in (
        "issuers_not_yet_attempted",
        "issuers_attempted_zero_evidence",
        "quality_state_counts",
        "period_gap_count",
        "unit_mismatch_count",
    ):
        del legacy_payload[key]

    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO corporate_data_quality_report "
                "(report_id, plan_id, issuers_requested, issuers_captured, "
                "point_in_time_completeness, created_at, payload, authority) "
                "VALUES (:report_id, :plan_id, :issuers_requested, :issuers_captured, "
                ":completeness, :created_at, :payload, 'NONE')"
            ),
            {
                "report_id": report.report_id,
                "plan_id": plan.plan_id,
                "issuers_requested": report.issuers_requested,
                "issuers_captured": report.issuers_captured,
                "completeness": report.point_in_time_completeness,
                "created_at": NOW,
                "payload": json.dumps(legacy_payload),
            },
        )

    fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
    rehydrated = fresh.quality_report_by_id(report.report_id)
    assert rehydrated is not None
    assert rehydrated.issuers_not_yet_attempted == ()
    assert rehydrated.issuers_attempted_zero_evidence == ()
    assert rehydrated.quality_state_counts == ()
    assert rehydrated.period_gap_count == 0
    assert rehydrated.unit_mismatch_count == 0
    # Every genuinely pre-existing field is untouched by the shim.
    assert rehydrated.issuers_requested == report.issuers_requested
    assert rehydrated.forms_found == report.forms_found


def test_legacy_dataset_snapshot_row_without_lookback_fields_still_rehydrates(
    postgres_engine: Engine,
) -> None:
    import json

    from apex.domain.values import canonical_json_bytes

    plan = _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    filing = repository.save_filing(_filing())
    fact = repository.save_fact(_fact())
    checkpoint = repository.save_checkpoint(_checkpoint(plan))
    assessment = assess_issuer_quality(
        stable_security_id="sec-aapl", symbol="AAPL", cik="320193", filings=(filing,), facts=(fact,)
    )
    report = repository.save_quality_report(
        CorporateDataQualityReport.create(
            plan=plan,
            issuer_assessments=(assessment,),
            all_filings=(filing,),
            all_facts=(fact,),
            checkpoint_complete=False,
            created_at=NOW,
        )
    )
    dataset = CorporateHistoryDatasetSnapshot.create(
        plan=plan,
        checkpoint_ids=(checkpoint.checkpoint_id,),
        filings=(filing,),
        facts=(fact,),
        quality_report=report,
        source_receipt_hashes=("raw-sha-1",),
        created_at=NOW,
    )
    legacy_payload = json.loads(canonical_json_bytes(dataset))
    for key in (
        "lookback_years",
        "lookback_cutoff_date",
        "lookback_retention_floor_date",
        "lookback_excluded_filing_count",
        "lookback_excluded_fact_count",
        "lookback_predecessor_only_fact_count",
    ):
        del legacy_payload[key]

    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO corporate_history_dataset_snapshot "
                "(dataset_id, plan_id, fact_count, filing_count, quality_report_id, "
                "completeness, created_at, payload, authority) "
                "VALUES (:dataset_id, :plan_id, :fact_count, :filing_count, :quality_report_id, "
                ":completeness, :created_at, :payload, 'NONE')"
            ),
            {
                "dataset_id": dataset.dataset_id,
                "plan_id": plan.plan_id,
                "fact_count": len(dataset.fact_ids),
                "filing_count": len(dataset.filing_event_ids),
                "quality_report_id": report.report_id,
                "completeness": dataset.completeness,
                "created_at": NOW,
                "payload": json.dumps(legacy_payload),
            },
        )

    fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
    rehydrated = fresh.dataset_snapshot_by_id(dataset.dataset_id)
    assert rehydrated is not None
    assert rehydrated.lookback_years == 0
    assert rehydrated.lookback_cutoff_date == "UNKNOWN_PRE_LOOKBACK_POLICY"
    assert rehydrated.lookback_excluded_filing_count == 0
    assert rehydrated.fact_ids == dataset.fact_ids


def test_replay_attachment_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    filing = repository.save_filing(_filing())
    fact = repository.save_fact(_fact())
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl",
        cik="320193",
        as_of=datetime(2024, 11, 2, tzinfo=UTC),
        filings=(filing,),
        facts=(fact,),
    )
    features = compute_derived_features(snapshot=snapshot, facts=(fact,))
    attachment = ReplayCorporateEvidenceAttachment.create(
        replay_run_id="replay-1",
        frame_id="frame-1",
        snapshot=snapshot,
        features=features,
        created_at=NOW,
    )
    repository.save_replay_attachment(attachment)
    repository.save_replay_attachment(attachment)  # idempotent replay
    fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
    assert fresh.replay_attachment_by_id(attachment.attachment_id) == attachment
    assert fresh.attachments_for_run("replay-1") == (attachment,)


# --------------------------------------------------------------------------
# Replay-scaling bulk reads/writes (Corporate Evidence Completion and
# Replay Scaling v1, section 3): the batched attachment pass's real
# persistence-layer surface.
# --------------------------------------------------------------------------


def test_filings_for_securities_and_facts_for_securities_bulk_reads(
    postgres_engine: Engine,
) -> None:
    _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    aapl_filing = repository.save_filing(_filing())
    aapl_fact = repository.save_fact(_fact())
    msft_filing = repository.save_filing(
        CorporateFilingEvent.create(
            stable_security_id="sec-msft",
            cik="789019",
            accession_number="0000789019-24-000001",
            form=FilingForm.FORM_10K,
            report_period="2024-06-30",
            filed_at=datetime(2024, 9, 30, tzinfo=UTC),
            accepted_at=datetime(2024, 10, 1, tzinfo=UTC),
            primary_document="msft.htm",
            file_number="001-99999",
        )
    )

    fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
    assert fresh.filings_for_securities(()) == ()
    assert fresh.facts_for_securities(()) == ()
    bulk_filings = fresh.filings_for_securities(("sec-aapl", "sec-msft", "sec-none"))
    assert set(bulk_filings) == {aapl_filing, msft_filing}
    bulk_facts = fresh.facts_for_securities(("sec-aapl", "sec-none"))
    assert bulk_facts == (aapl_fact,)


def test_facts_for_securities_selection_is_deterministic_across_repeated_reads_with_a_tie(
    postgres_engine: Engine,
) -> None:
    """Corporate Research Panel Determinism v1: a genuine same-accession
    dual-tag tie (real shape found among retained evidence -- see the
    milestone's regression harness) must round-trip through Postgres and
    resolve through ``resolve_fact_as_of``'s canonical precedence
    identically regardless of insertion order or how many times the bulk
    read runs. ``facts_for``/``facts_for_securities`` must also agree
    with each other."""
    _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    lower_priority_tag = FundamentalFact.create(
        stable_security_id="sec-aapl",
        cik="320193",
        concept=FundamentalConcept.NET_INCOME,
        source_taxonomy="us-gaap",
        source_tag="ProfitLoss",
        fiscal_year=2024,
        fiscal_period=FiscalPeriod.FY,
        period_start="2023-10-01",
        period_end="2024-09-28",
        value=Decimal("3249000000"),
        accession_number="0000320193-24-000123",
        form="10-K",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, 16, 30, tzinfo=UTC),
    )
    higher_priority_tag = FundamentalFact.create(
        stable_security_id="sec-aapl",
        cik="320193",
        concept=FundamentalConcept.NET_INCOME,
        source_taxonomy="us-gaap",
        source_tag="NetIncomeLoss",
        fiscal_year=2024,
        fiscal_period=FiscalPeriod.FY,
        period_start="2023-10-01",
        period_end="2024-09-28",
        value=Decimal("3209000000"),
        accession_number="0000320193-24-000123",
        form="10-K",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, 16, 30, tzinfo=UTC),
    )
    # Insert the *lower*-priority tag first -- proves the selection never
    # depends on insertion/physical row order, only on genuinely retained
    # fields.
    repository.save_fact(lower_priority_tag)
    repository.save_fact(higher_priority_tag)

    for _ in range(3):
        fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
        bulk = fresh.facts_for_securities(("sec-aapl",))
        single = fresh.facts_for("sec-aapl")
        assert bulk == single
        chosen = resolve_fact_as_of(bulk, as_of=datetime(2024, 11, 2, tzinfo=UTC))
        assert chosen is not None
        assert chosen.fact_id == higher_priority_tag.fact_id


def test_save_replay_attachments_bulk_inserts_and_is_idempotent(postgres_engine: Engine) -> None:
    _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    filing = repository.save_filing(_filing())
    fact = repository.save_fact(_fact())
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl",
        cik="320193",
        as_of=datetime(2024, 11, 2, tzinfo=UTC),
        filings=(filing,),
        facts=(fact,),
    )
    features = compute_derived_features(snapshot=snapshot, facts=(fact,))
    attachments = tuple(
        ReplayCorporateEvidenceAttachment.create(
            replay_run_id="replay-bulk",
            frame_id=f"frame-{index}",
            snapshot=snapshot,
            features=features,
            created_at=NOW,
        )
        for index in range(5)
    )

    first_saved = repository.save_replay_attachments(attachments)
    assert first_saved == 5
    fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
    assert len(fresh.attachments_for_run("replay-bulk")) == 5

    # Re-saving the identical bulk set inserts nothing new (falls back to
    # the per-row insert-if-absent path on the unique-index conflict).
    second_saved = repository.save_replay_attachments(attachments)
    assert second_saved == 0
    assert len(fresh.attachments_for_run("replay-bulk")) == 5

    assert repository.save_replay_attachments(()) == 0


def test_attachment_run_summary_reports_one_row_per_run(postgres_engine: Engine) -> None:
    _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    filing = repository.save_filing(_filing())
    fact = repository.save_fact(_fact())
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl",
        cik="320193",
        as_of=datetime(2024, 11, 2, tzinfo=UTC),
        filings=(filing,),
        facts=(fact,),
    )
    features = compute_derived_features(snapshot=snapshot, facts=(fact,))
    for run_id, frame_count in (("replay-a", 2), ("replay-b", 1)):
        repository.save_replay_attachments(
            tuple(
                ReplayCorporateEvidenceAttachment.create(
                    replay_run_id=run_id,
                    frame_id=f"frame-{index}",
                    snapshot=snapshot,
                    features=features,
                    created_at=NOW,
                )
                for index in range(frame_count)
            )
        )
    fresh = PostgresCorporateFundamentalsRepository(postgres_engine)
    assert fresh.attachment_run_summary() == (("replay-a", 2), ("replay-b", 1))


# --------------------------------------------------------------------------
# Immutability
# --------------------------------------------------------------------------


def test_fact_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    fact = repository.save_fact(_fact())
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text("UPDATE corporate_fundamental_fact SET value = 1 WHERE fact_id = :id"),
            {"id": fact.fact_id},
        )


def test_fact_immutable_trigger_rejects_delete(postgres_engine: Engine) -> None:
    _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    fact = repository.save_fact(_fact())
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text("DELETE FROM corporate_fundamental_fact WHERE fact_id = :id"),
            {"id": fact.fact_id},
        )


def test_filing_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    filing = repository.save_filing(_filing())
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text(
                "UPDATE corporate_filing_event SET primary_document = 'x' "
                "WHERE filing_event_id = :id"
            ),
            {"id": filing.filing_event_id},
        )


def test_authority_is_always_none_across_every_table(postgres_engine: Engine) -> None:
    plan = _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    repository.save_filing(_filing())
    repository.save_fact(_fact())
    checkpoint = repository.save_checkpoint(_checkpoint(plan))
    assert checkpoint.authority == "NONE"
    with postgres_engine.connect() as connection:
        for table in CORPORATE_FUNDAMENTALS_TABLES:
            rows = connection.execute(
                sa.text(f'SELECT DISTINCT authority FROM "{table}"')
            ).fetchall()
            assert all(row[0] == "NONE" for row in rows)


# --------------------------------------------------------------------------
# Migration downgrade protection
# --------------------------------------------------------------------------


def test_retained_evidence_blocks_destructive_0088_downgrade(postgres_engine: Engine) -> None:
    _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    repository.save_filing(_filing())
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0088 with retained corporate fundamental evidence",
        ):
            command.downgrade(_config(connection), "0087_options_forward_evidence")
        connection.rollback()
    with postgres_engine.connect() as connection:
        # Since 0089 (Cross-Market Research v3), real head is one revision
        # above 0088: downgrading from head to "0087_options_forward_
        # evidence" is a two-step request (0089 -> 0088 succeeds, then
        # 0088 -> 0087 is the one blocked above), so alembic leaves the
        # database exactly where the block occurred -- "0088_corporate_
        # fundamentals" -- not at ``HEAD_REVISION`` itself.
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0088_corporate_fundamentals"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0088_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(CORPORATE_FUNDAMENTALS_TABLES)
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0087_options_forward_evidence")
    assert set(CORPORATE_FUNDAMENTALS_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(CORPORATE_FUNDAMENTALS_TABLES)


# --------------------------------------------------------------------------
# Coinbase/Kalshi/options independence
# --------------------------------------------------------------------------


def test_corporate_fundamentals_never_touches_crypto_kalshi_or_options_tables(
    postgres_engine: Engine,
) -> None:
    other_tables = (
        "apex_crypto_paper_account",
        "apex_kalshi_paper_account",
        "options_chain_capture_plan",
        "options_forward_evidence_checkpoint",
    )
    with postgres_engine.connect() as connection:
        existing = set(sa.inspect(postgres_engine).get_table_names())
        before = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
            for table in other_tables
            if table in existing
        }
    plan = _seed_plan(postgres_engine)
    repository = PostgresCorporateFundamentalsRepository(postgres_engine)
    repository.save_filing(_filing())
    repository.save_fact(_fact())
    repository.save_checkpoint(_checkpoint(plan))
    with postgres_engine.connect() as connection:
        after = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) for table in before
        }
    assert before == after
