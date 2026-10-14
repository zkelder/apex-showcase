"""Deterministic isolated fixtures for generic crypto-campaign acceptance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apex.application.crypto_campaign_generic import (
    GenericCampaignWorkflow,
    GenericSnapshotEvidence,
    GenericWorkflowReport,
    InMemoryGenericCampaignRepository,
)
from apex.domain.crypto_campaign_generic import (
    ComparisonOperator,
    DecisionCadence,
    FeatureKind,
    GenericCandidateDefinition,
    GenericFeatureDefinition,
    GenericOperand,
    GenericRuleDefinition,
    MissingDataAction,
    PositionState,
    RuleMode,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.research_evidence import EvidenceClass
from apex.domain.values import content_digest


@dataclass(frozen=True, slots=True)
class GenericFixtureAcceptance:
    stateless: GenericWorkflowReport
    stateful: GenericWorkflowReport
    stateless_replay_hashes: tuple[str, str]
    stateful_replay_hashes: tuple[str, str]
    stateless_retry_hash: str
    stateful_retry_hash: str
    production_identity_used: bool
    network_request_performed: bool
    authority: str = "NONE"


def _candles(
    *,
    capture_id: str,
    start: datetime,
    prices: tuple[str, ...],
) -> tuple[CryptoDailyCandle, ...]:
    result: list[CryptoDailyCandle] = []
    for index, price in enumerate(prices):
        bucket = start + timedelta(days=index)
        result.append(
            CryptoDailyCandle.create(
                capture_id=capture_id,
                attempt_id=f"{capture_id}-attempt",
                evidence_class=EvidenceClass.FIXTURE,
                bucket_start=bucket,
                open=price,
                high=price,
                low=price,
                close=price,
                volume="1",
                ingested_at=datetime(2030, 1, 1, tzinfo=UTC),
                raw_response_hash=content_digest((capture_id, bucket, price)),
            )
        )
    return tuple(result)


def _snapshots(
    *,
    prefix: str,
    validation_prices: tuple[str, ...],
) -> tuple[GenericSnapshotEvidence, GenericSnapshotEvidence]:
    prehistory = ("110", "110", "110", "110", "110")
    first_start = datetime(2024, 1, 1, tzinfo=UTC)
    second_start = datetime(2025, 1, 1, tzinfo=UTC)
    values = prehistory + validation_prices
    first = _candles(
        capture_id=f"{prefix}-capture-earlier",
        start=first_start,
        prices=values,
    )
    second = _candles(
        capture_id=f"{prefix}-capture-later",
        start=second_start,
        prices=values,
    )
    return (
        GenericSnapshotEvidence(
            f"{prefix}-snapshot-earlier",
            content_digest((prefix, "manifest", "earlier", values)),
            "FIXTURE",
            first_start + timedelta(days=len(prehistory)),
            first_start + timedelta(days=len(values)),
            first,
        ),
        GenericSnapshotEvidence(
            f"{prefix}-snapshot-later",
            content_digest((prefix, "manifest", "later", values)),
            "FIXTURE",
            second_start + timedelta(days=len(prehistory)),
            second_start + timedelta(days=len(values)),
            second,
        ),
    )


def _stateless_definition(
    snapshots: tuple[GenericSnapshotEvidence, GenericSnapshotEvidence],
) -> GenericCandidateDefinition:
    feature = GenericFeatureDefinition.create(
        name="mean_3",
        kind=FeatureKind.ROLLING_MEAN,
        observations=3,
        exclude_decision_observation=False,
        missing_data_action=MissingDataAction.SELECT_CASH,
    )
    rule = GenericRuleDefinition.create(
        rule_id="close_above_mean_3",
        source_state=None,
        left=GenericOperand.decision_close(),
        operator=ComparisonOperator.STRICTLY_GREATER_THAN,
        right=GenericOperand.feature("mean_3"),
        trigger_state=PositionState.LONG,
        non_trigger_state=PositionState.CASH,
        missing_data_action=MissingDataAction.SELECT_CASH,
    )
    return GenericCandidateDefinition.create(
        definition_id="isolated-stateless-definition-v1",
        campaign_id="isolated-generic-stateless-campaign-v1",
        candidate_id="isolated-stateless-candidate-v1",
        hypothesis_id="isolated-stateless-hypothesis",
        hypothesis_version_id="isolated-stateless-hypothesis-v1",
        strategy_specification_id="isolated-stateless-strategy-v1",
        strategy_version=1,
        family="ISOLATED_STATELESS_CLOSE_ABOVE_MEAN",
        expected_mechanism=(
            "An isolated fixture mechanism used only to prove bounded generic "
            "stateless campaign operability."
        ),
        approved_snapshot_ids=(snapshots[0].snapshot_id, snapshots[1].snapshot_id),
        validation_windows=(
            (snapshots[0].validation_start, snapshots[0].validation_end),
            (snapshots[1].validation_start, snapshots[1].validation_end),
        ),
        cadence=DecisionCadence.DAILY,
        initial_state=PositionState.CASH,
        rule_mode=RuleMode.STATELESS_LONG_CASH,
        features=(feature,),
        rules=(rule,),
    )


def _stateful_definition(
    snapshots: tuple[GenericSnapshotEvidence, GenericSnapshotEvidence],
) -> GenericCandidateDefinition:
    maximum = GenericFeatureDefinition.create(
        name="maximum_3",
        kind=FeatureKind.ROLLING_MAXIMUM,
        observations=3,
        exclude_decision_observation=True,
        missing_data_action=MissingDataAction.PRESERVE_CASH,
    )
    minimum = GenericFeatureDefinition.create(
        name="minimum_2",
        kind=FeatureKind.ROLLING_MINIMUM,
        observations=2,
        exclude_decision_observation=True,
        missing_data_action=MissingDataAction.PROTECTIVE_EXIT_TO_CASH,
    )
    entry = GenericRuleDefinition.create(
        rule_id="entry_maximum_3",
        source_state=PositionState.CASH,
        left=GenericOperand.decision_close(),
        operator=ComparisonOperator.STRICTLY_GREATER_THAN,
        right=GenericOperand.feature("maximum_3"),
        trigger_state=PositionState.LONG,
        non_trigger_state=PositionState.CASH,
        missing_data_action=MissingDataAction.PRESERVE_CASH,
    )
    exit_rule = GenericRuleDefinition.create(
        rule_id="exit_minimum_2",
        source_state=PositionState.LONG,
        left=GenericOperand.decision_close(),
        operator=ComparisonOperator.STRICTLY_LESS_THAN,
        right=GenericOperand.feature("minimum_2"),
        trigger_state=PositionState.CASH,
        non_trigger_state=PositionState.LONG,
        missing_data_action=MissingDataAction.PROTECTIVE_EXIT_TO_CASH,
    )
    return GenericCandidateDefinition.create(
        definition_id="isolated-stateful-definition-v1",
        campaign_id="isolated-generic-stateful-campaign-v1",
        candidate_id="isolated-stateful-candidate-v1",
        hypothesis_id="isolated-stateful-hypothesis",
        hypothesis_version_id="isolated-stateful-hypothesis-v1",
        strategy_specification_id="isolated-stateful-strategy-v1",
        strategy_version=1,
        family="ISOLATED_STATEFUL_EXTREMA",
        expected_mechanism=(
            "An isolated fixture mechanism used only to prove bounded generic "
            "stateful campaign operability."
        ),
        approved_snapshot_ids=(snapshots[0].snapshot_id, snapshots[1].snapshot_id),
        validation_windows=(
            (snapshots[0].validation_start, snapshots[0].validation_end),
            (snapshots[1].validation_start, snapshots[1].validation_end),
        ),
        cadence=DecisionCadence.DAILY,
        initial_state=PositionState.CASH,
        rule_mode=RuleMode.STATEFUL_ENTRY_EXIT,
        features=(maximum, minimum),
        rules=(entry, exit_rule),
    )


def _orchestrate_fixture(
    *,
    prefix: str,
    definition: GenericCandidateDefinition,
    snapshots: tuple[GenericSnapshotEvidence, GenericSnapshotEvidence],
) -> tuple[
    GenericCampaignWorkflow,
    GenericWorkflowReport,
    GenericWorkflowReport,
    tuple[str, str],
]:
    repository = InMemoryGenericCampaignRepository()
    workflow = GenericCampaignWorkflow(repository)
    arguments = {
        "workflow_id": f"{prefix}-workflow-v1",
        "definition": definition,
        "prior_semantics": (("isolated-prior-strategy-v1", content_digest(("prior", prefix))),),
        "protocol_id": f"{prefix}-protocol-v1",
        "snapshots": snapshots,
        "run_ids": (f"{prefix}-run-earlier-v1", f"{prefix}-run-later-v1"),
        "outcome_ids": (
            f"{prefix}-outcome-earlier-v1",
            f"{prefix}-outcome-later-v1",
        ),
        "dossier_id": f"{prefix}-dossier-v1",
        "evaluation_id": f"{prefix}-nomination-v1",
        "executed_at": datetime(2030, 1, 2, tzinfo=UTC),
        "idempotency_key": f"{prefix}-workflow-key-v1",
    }
    first = workflow.orchestrate(**arguments)  # type: ignore[arg-type]
    replay_hashes = tuple(
        workflow.replay(
            definition=definition,
            plan=first.plan,
            protocol=first.protocol,
            snapshot=snapshots[index],
            run_id=first.runs[index].run_id,
        ).canonical_hash
        for index in range(2)
    )
    retry = workflow.orchestrate(**arguments)  # type: ignore[arg-type]
    return workflow, first, retry, (replay_hashes[0], replay_hashes[1])


def generic_fixture_acceptance() -> GenericFixtureAcceptance:
    stateless_snapshots = _snapshots(
        prefix="isolated-stateless",
        validation_prices=(
            "100",
            "90",
            "80",
            "85",
            "90",
            "100",
            "110",
            "120",
            "130",
            "140",
        ),
    )
    stateful_snapshots = _snapshots(
        prefix="isolated-stateful",
        validation_prices=(
            "100",
            "90",
            "80",
            "85",
            "90",
            "100",
            "110",
            "120",
            "130",
            "120",
            "100",
            "90",
        ),
    )
    stateless = _stateless_definition(stateless_snapshots)
    stateful = _stateful_definition(stateful_snapshots)
    _, stateless_report, stateless_retry, stateless_replays = _orchestrate_fixture(
        prefix="isolated-stateless",
        definition=stateless,
        snapshots=stateless_snapshots,
    )
    _, stateful_report, stateful_retry, stateful_replays = _orchestrate_fixture(
        prefix="isolated-stateful",
        definition=stateful,
        snapshots=stateful_snapshots,
    )
    return GenericFixtureAcceptance(
        stateless_report,
        stateful_report,
        stateless_replays,
        stateful_replays,
        stateless_retry.canonical_hash,
        stateful_retry.canonical_hash,
        False,
        False,
    )
