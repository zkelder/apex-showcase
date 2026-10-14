"""Deterministic governed Securities PAPER product demonstration."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.adapters.securities_paper_external import (
    DeterministicLeanPaperFixtureAdapter,
    LeanSecuritiesPaperAdapter,
)
from apex.application.autopilot import (
    StrategyAuthorityState,
    StrategyDescriptor,
    StrategyEvidenceState,
    StrategyFamily,
    StrategyImplementationState,
)
from apex.application.evidence_review import (
    BuildStrategyEvidenceSnapshot,
    BuildStrategyEvidenceSnapshotCommand,
    DecideStrategyEvidenceReview,
    DecideStrategyEvidenceReviewCommand,
    EvidenceReviewSource,
    InMemoryEvidenceReviewStore,
    PaperTrialAuthorization,
    PaperTrialLimits,
    PaperTrialUsage,
    PredictionConfigurationLineage,
    RequestStrategyEvidenceReview,
    RequestStrategyEvidenceReviewCommand,
    StartStrategyEvidenceReview,
    StrategyConfigurationLineageState,
    StrategyEvidenceReviewPolicy,
    StrategyEvidenceReviewState,
    hard_gate_reasons,
    paper_trial_confirmation_text,
)
from apex.application.securities_paper import (
    ApplySecuritiesCorporateActions,
    BuildSecuritiesPaperConformance,
    BuildSecuritiesPaperOpportunities,
    BuildSecuritiesPaperOpportunityCommand,
    CreateSecuritiesPaperRun,
    CreateSecuritiesPaperRunCommand,
    ExecuteSecuritiesPaperOpportunityCommand,
    ExitSecuritiesPositionCommand,
    ExitSecuritiesPositions,
    InMemorySecuritiesPaperCampaignBoundary,
    InMemorySecuritiesPaperRepository,
    MarkSecuritiesPositions,
    RunSecuritiesPaperExecution,
    SecuritiesPaperApplicationError,
    complete_run,
    reconcile_securities_trial,
)
from apex.application.strategy_shadow import (
    ShadowPrediction,
    ShadowPredictionState,
    authority_transition_reasons,
    score_prediction,
)
from apex.domain.campaign import Campaign, CampaignPolicy, CampaignSnapshot
from apex.domain.ledger import JournalFactory
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesCorporateActionEvidence,
    SecuritiesSignal,
    SecuritiesSignalDirection,
)
from apex.domain.securities_paper import (
    EXTERNAL_ENGINE_WARNING,
    NO_BROKER_WARNING,
    SECURITIES_PAPER_WARNING,
    SecuritiesExecutionPolicy,
    SecuritiesExitReason,
)
from apex.domain.values import (
    ContentHash,
    Money,
    Quantity,
    RiskFraction,
    content_digest,
)

DEMO_CAMPAIGN_ID = "campaign-securities-paper-demo"
DEMO_STRATEGY_ID = "fixture-only-securities-paper-v1"
DEMO_NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _hash(value: object) -> ContentHash:
    return ContentHash(content_digest(value))


def _bar(
    ordinal: int,
    *,
    open_price: str,
    close_price: str,
    volume: str = "1000",
) -> SecuritiesBarEvidence:
    opens_at = datetime(2026, 7, 20, 14, 30, tzinfo=UTC) + timedelta(days=ordinal)
    closes_at = opens_at + timedelta(hours=6, minutes=30)
    open_value = Decimal(open_price)
    close_value = Decimal(close_price)
    return SecuritiesBarEvidence(
        instrument_id="fixture-security",
        interval="ONE_DAY",
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=opens_at,
        period_end=closes_at,
        available_at=closes_at + timedelta(minutes=1),
        open=open_price,
        high=str(max(open_value, close_value) + Decimal(1)),
        low=str(min(open_value, close_value) - Decimal(1)),
        close=close_price,
        volume=volume,
        source_identity="fixture-managed-securities-bars",
    )


def _signal(
    *,
    ordinal: int,
    source_bar: SecuritiesBarEvidence,
) -> SecuritiesSignal:
    payload = {
        "strategy_id": DEMO_STRATEGY_ID,
        "ordinal": ordinal,
        "source_bar_id": source_bar.bar_id,
    }
    return SecuritiesSignal(
        signal_id=content_digest({"securities_signal": payload})[:40],
        research_run_id="fixture-securities-research-run",
        specification_id="fixture-securities-specification",
        instrument_id=source_bar.instrument_id,
        signal_at=source_bar.available_at,
        direction=SecuritiesSignalDirection.LONG,
        target_weight=Decimal("0.1"),
        reference_close=source_bar.close,
        feature_start=source_bar.period_start,
        feature_end=source_bar.period_end,
        source_bar_ids=(source_bar.bar_id,),
        reason_codes=("FIXTURE_GOVERNED_LONG_SIGNAL",),
        model_deployment_id=None,
        model_artifact_hash=None,
        canonical_hash=content_digest(payload),
    )


def _descriptor(
    authority: StrategyAuthorityState,
) -> StrategyDescriptor:
    return StrategyDescriptor(
        strategy_id=DEMO_STRATEGY_ID,
        version="v1",
        display_name="Fixture-only governed Securities strategy",
        family=StrategyFamily.SECURITIES,
        implementation_state=StrategyImplementationState.FIXTURE,
        authority_state=authority,
        evidence_state=StrategyEvidenceState.FIXTURE,
        last_evidence_decision="FIXTURE_EVIDENCE_REVIEW_ONLY",
        supported_instrument_class="CONTINUOUS_SECURITIES",
        maximum_proposals_per_cycle=3,
        configuration_hash=_hash("fixture-securities-paper-configuration-v1"),
    )


def _review_source(
    descriptor: StrategyDescriptor,
) -> EvidenceReviewSource:
    predictions: list[ShadowPrediction] = []
    evaluations = []
    lineage: list[PredictionConfigurationLineage] = []
    probabilities = (
        Decimal("0.15"),
        Decimal("0.35"),
        Decimal("0.55"),
        Decimal("0.75"),
        Decimal("0.90"),
        Decimal("0.65"),
    )
    for ordinal, yes_probability in enumerate(probabilities):
        outcome = "YES" if yes_probability >= Decimal("0.5") else "NO"
        decision_at = DEMO_NOW - timedelta(days=3, minutes=ordinal)
        payload = {
            "strategy_id": descriptor.strategy_id,
            "ordinal": ordinal,
            "decision_at": decision_at,
        }
        prediction = ShadowPrediction(
            prediction_id=content_digest({"securities_review_prediction": payload})[:40],
            strategy_id=descriptor.strategy_id,
            strategy_version=descriptor.version,
            campaign_id=DEMO_CAMPAIGN_ID,
            cycle_id=None,
            invocation_id=None,
            proposal_id=None,
            provider="FIXTURE_MANAGED_SECURITIES_DATA",
            instrument_id=f"fixture-security-{ordinal}",
            event_id=f"fixture-session-{ordinal}",
            decision_at=decision_at,
            resolution_at=decision_at + timedelta(days=1),
            point_probabilities=(
                ("NO", Decimal(1) - yes_probability),
                ("YES", yes_probability),
            ),
            conservative_probabilities=(
                ("NO", Decimal(1) - yes_probability),
                ("YES", yes_probability),
            ),
            market_probabilities=(("NO", Decimal("0.5")), ("YES", Decimal("0.5"))),
            market_evidence_id=f"fixture-securities-evidence-{ordinal}",
            expected_fee=Decimal(0),
            slippage_reserve=Decimal(0),
            execution_uncertainty_reserve=Decimal(0),
            evidence_tier="STRONG",
            evidence_references=(f"fixture-securities-evidence-{ordinal}",),
            thesis_summary="Fixture-only evidence for the governed PAPER product proof.",
            correlation_buckets=(f"fixture-session-{ordinal}",),
            operator_note=NO_BROKER_WARNING,
            model_configuration_version="v1",
            canonical_input_hash=content_digest(payload),
            canonical_prediction_hash=content_digest(
                {"payload": payload, "probability": yes_probability}
            ),
            state=ShadowPredictionState.UNRESOLVED,
        )
        predictions.append(prediction)
        evaluations.append(
            score_prediction(
                prediction=prediction,
                settlement_fact_id=f"fixture-securities-outcome-{ordinal}",
                resolved_outcome=outcome,
                evaluated_at=DEMO_NOW - timedelta(hours=12, minutes=ordinal),
            )
        )
        lineage.append(
            PredictionConfigurationLineage(
                prediction_id=prediction.prediction_id,
                configuration_hash=descriptor.configuration_hash,
                state=(StrategyConfigurationLineageState.UNPLANNED_CONFIGURATION_COMPLETE),
                reason_codes=("FIXTURE_CONFIGURATION_EXACT",),
            )
        )
    return EvidenceReviewSource(
        descriptor=descriptor,
        predictions=tuple(predictions),
        evaluations=tuple(evaluations),
        opportunity_count=6,
        hypothetical_admission_count=6,
        configuration_lineage=tuple(lineage),
    )


def _governed_trial(
    *,
    policy: SecuritiesExecutionPolicy,
) -> tuple[PaperTrialAuthorization, dict[str, object]]:
    shadow_descriptor = _descriptor(StrategyAuthorityState.SHADOW)
    review_policy = StrategyEvidenceReviewPolicy(
        policy_id="fixture-securities-paper-review-policy",
        policy_version="v1",
        supported_strategy_family=None,
        supported_strategy_id=shadow_descriptor.strategy_id,
        minimum_resolved_predictions=6,
        minimum_distinct_events=6,
        minimum_distinct_markets=6,
        minimum_probability_range_coverage=3,
        maximum_unresolved_integrity_warnings=0,
        maximum_point_brier_difference=Decimal(1),
        maximum_conservative_brier_difference=Decimal(1),
        maximum_point_log_loss_difference=Decimal(1),
        maximum_conservative_log_loss_difference=Decimal(1),
        maximum_recent_degradation=Decimal(1),
        minimum_recent_resolved=6,
        minimum_calibration_coverage=Decimal("0.6"),
        maximum_conflict_frequency=Decimal(0),
        maximum_evidence_age=timedelta(days=30),
        require_configuration_lineage=True,
        require_outcome_lineage=True,
        configuration_hash=_hash("fixture-securities-paper-review-policy"),
        created_at=DEMO_NOW - timedelta(days=30),
    )
    store = InMemoryEvidenceReviewStore(
        policies=(review_policy,),
        sources=(_review_source(shadow_descriptor),),
    )
    snapshot, criteria = BuildStrategyEvidenceSnapshot(store).build(
        BuildStrategyEvidenceSnapshotCommand(
            strategy_id=shadow_descriptor.strategy_id,
            strategy_version=shadow_descriptor.version,
            configuration_hash=shadow_descriptor.configuration_hash,
            policy_id=review_policy.policy_id,
            policy_version=review_policy.policy_version,
            cutoff_at=DEMO_NOW,
            full_history_started_at=DEMO_NOW - timedelta(days=10),
            recent_history_started_at=DEMO_NOW - timedelta(days=7),
            idempotency_key="fixture-securities-snapshot-v1",
        )
    )
    if hard_gate_reasons(criteria):
        raise RuntimeError("fixture Securities evidence did not pass every hard gate")
    requested = RequestStrategyEvidenceReview(store).request(
        RequestStrategyEvidenceReviewCommand(
            strategy_id=shadow_descriptor.strategy_id,
            strategy_version=shadow_descriptor.version,
            snapshot_id=snapshot.snapshot_id,
            campaign_id=DEMO_CAMPAIGN_ID,
            expires_at=DEMO_NOW + timedelta(hours=4),
            operator_reason="review fixture-only Securities evidence",
            operator_note=NO_BROKER_WARNING,
            idempotency_key="fixture-securities-review-request-v1",
        ),
        now=DEMO_NOW,
    )
    started = StartStrategyEvidenceReview(store).start(
        review_id=requested.review_id,
        operator_reason="start explicit Securities fixture review",
        operator_note=NO_BROKER_WARNING,
        idempotency_key="fixture-securities-review-start-v1",
        now=DEMO_NOW,
    )
    limits = PaperTrialLimits(
        paper_execution_policy_id=policy.policy_id,
        paper_execution_policy_hash=policy.canonical_hash,
        starts_at=DEMO_NOW - timedelta(seconds=1),
        expires_at=DEMO_NOW + timedelta(days=10),
        maximum_paper_cycles=4,
        maximum_approved_opportunities=4,
        maximum_paper_orders=6,
        maximum_simultaneous_open_positions=3,
        maximum_per_opportunity_risk_fraction=RiskFraction("0.3"),
        maximum_strategy_active_risk_fraction=RiskFraction("0.8"),
        maximum_cumulative_authorized_risk=Money("9000"),
        maximum_cumulative_realized_paper_loss=Money("100"),
        maximum_paper_drawdown=Money("100"),
        maximum_capital_lockup_seconds=86400,
        allowed_instrument_class="CONTINUOUS_SECURITIES",
        allowed_provider="FIXTURE_MANAGED_SECURITIES_DATA",
    )
    approved, trial = DecideStrategyEvidenceReview(store).decide(
        DecideStrategyEvidenceReviewCommand(
            review_id=started.review_id,
            decision=StrategyEvidenceReviewState.APPROVED,
            operator_reason="approve bounded Securities fixture trial",
            operator_note=NO_BROKER_WARNING,
            idempotency_key="fixture-securities-review-approve-v1",
            confirmation_text=paper_trial_confirmation_text(
                strategy_id=shadow_descriptor.strategy_id,
                campaign_id=DEMO_CAMPAIGN_ID,
                snapshot_id=snapshot.snapshot_id,
            ),
        ),
        now=DEMO_NOW,
        trial_limits=limits,
    )
    if trial is None or approved.state is not StrategyEvidenceReviewState.APPROVED:
        raise RuntimeError("approved Securities review did not create a trial")
    transition_reasons = authority_transition_reasons(
        current=shadow_descriptor.authority_state,
        requested=StrategyAuthorityState.PAPER,
        evidence_state=shadow_descriptor.evidence_state,
        confirmation_text="explicit-fixture-operator-confirmation",
        paper_trial_authorization_id=trial.authorization_id,
    )
    if transition_reasons:
        raise RuntimeError("governed Securities authority transition was rejected")
    return trial, {
        "snapshot_id": snapshot.snapshot_id,
        "criterion_count": len(criteria),
        "all_hard_gates_pass": not hard_gate_reasons(criteria),
        "review_id": approved.review_id,
        "review_state": approved.state.value,
        "trial_id": trial.authorization_id,
        "trial_state": trial.state.value,
        "authority_before": shadow_descriptor.authority_state.value,
        "authority_after": StrategyAuthorityState.PAPER.value,
    }


def _campaign_boundary() -> InMemorySecuritiesPaperCampaignBoundary:
    campaign_policy = CampaignPolicy(
        policy_version_id="fixture-securities-campaign-policy",
        profile="FIXTURE_SECURITIES_PAPER",
        requested_risk_fraction="0.1",
        max_trade_loss_fraction="0.5",
        max_open_loss_fraction="0.8",
        min_synthetic_edge="0",
        max_spread="1",
        max_capital_lockup_seconds=86400,
        content_hash=_hash("fixture-securities-campaign-policy"),
    )
    campaign = Campaign(
        campaign_id=DEMO_CAMPAIGN_ID,
        contribution=Money("10000"),
        authorized_loss=Money("5000"),
        policy=campaign_policy,
    )
    return InMemorySecuritiesPaperCampaignBoundary(
        (
            CampaignSnapshot(
                campaign=campaign,
                ledger_equity=Money("10000"),
                settled_cash=Money("10000"),
                active_reservations=Money(0),
            ),
        )
    )


def _action(
    kind: str,
    *,
    suffix: str,
    **values: str,
) -> SecuritiesCorporateActionEvidence:
    return SecuritiesCorporateActionEvidence(
        instrument_id="fixture-security",
        action_type=kind,
        effective_date="2026-07-24",
        available_at=_bar(4, open_price="104", close_price="105").available_at,
        source_identity=f"fixture-securities-{suffix}",
        **values,
    )


def run_securities_paper_demo() -> dict[str, object]:
    """Run the bounded financial loop entirely through production services."""

    policy = SecuritiesExecutionPolicy.next_bar_cross_v1(created_at=DEMO_NOW)
    trial, governance = _governed_trial(policy=policy)
    descriptor = _descriptor(StrategyAuthorityState.PAPER)
    campaigns = _campaign_boundary()
    repository = InMemorySecuritiesPaperRepository()
    repository.ledger.post(
        JournalFactory.contribution(
            "fixture-securities-capital",
            Money("10000"),
            DEMO_NOW,
        )
    )
    run = CreateSecuritiesPaperRun(
        repository=repository,
        campaigns=campaigns,
    ).execute(
        CreateSecuritiesPaperRunCommand(
            campaign_id=DEMO_CAMPAIGN_ID,
            trial=trial,
            descriptor=descriptor,
            data_batch_id="fixture-managed-securities-batch",
            data_batch_hash=_hash("fixture-managed-securities-batch"),
            policy=policy,
            start_date="2026-07-20",
            end_date="2026-07-30",
            signal_count=3,
            idempotency_key="fixture-securities-paper-run-v1",
            requested_at=DEMO_NOW,
        )
    )
    source = _bar(0, open_price="99", close_price="100")
    eligible = _bar(1, open_price="100", close_price="101")
    builder = BuildSecuritiesPaperOpportunities(repository)
    executor = RunSecuritiesPaperExecution(
        repository=repository,
        campaigns=campaigns,
    )
    execution_results = []
    for ordinal, next_bar in (
        (0, eligible),
        (1, _bar(1, open_price="100", close_price="101", volume="50")),
        (2, None),
    ):
        signal = _signal(ordinal=ordinal, source_bar=source)
        opportunity = builder.execute(
            BuildSecuritiesPaperOpportunityCommand(
                run_id=run.run_id,
                signal=signal,
                next_bar=eligible,
                requested_quantity=Quantity("10", "1"),
                policy=policy,
                holding_horizon_sessions=3,
            )
        )
        execution_results.append(
            executor.execute(
                ExecuteSecuritiesPaperOpportunityCommand(
                    run_id=run.run_id,
                    opportunity=opportunity,
                    signal=signal,
                    next_bar=next_bar,
                    policy=policy,
                    trial=trial,
                    strategy_authority_decision_id="fixture-securities-paper-authority",
                    symbol="FIX",
                    idempotency_key=f"fixture-securities-order-{ordinal}",
                    expires_at=_bar(2, open_price="102", close_price="103").period_start,
                )
            )
        )
    replay = executor.execute(
        ExecuteSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,
            opportunity=repository.opportunities[execution_results[0].authorization.opportunity_id],
            signal=_signal(ordinal=0, source_bar=source),
            next_bar=eligible,
            policy=policy,
            trial=trial,
            strategy_authority_decision_id="fixture-securities-paper-authority",
            symbol="FIX",
            idempotency_key="fixture-securities-order-0",
            expires_at=_bar(2, open_price="102", close_price="103").period_start,
        )
    )
    conflict_rejected = False
    try:
        repository.save_run(
            replace(
                run,
                canonical_input_hash=_hash("conflicting-securities-paper-run"),
            )
        )
    except SecuritiesPaperApplicationError:
        conflict_rejected = True

    first_position = execution_results[0].position
    second_position = execution_results[1].position
    if first_position is None or second_position is None:
        raise RuntimeError("fixture full and partial fills did not create Positions")
    marker = MarkSecuritiesPositions(repository)
    first_mark = marker.execute(
        position_id=first_position.position_id,
        bar=_bar(2, open_price="102", close_price="104"),
        policy=policy,
        as_of=_bar(2, open_price="102", close_price="104").available_at,
    )
    second_mark = marker.execute(
        position_id=first_position.position_id,
        bar=_bar(3, open_price="104", close_price="106"),
        policy=policy,
        as_of=_bar(3, open_price="104", close_price="106").available_at,
    )

    actions = ApplySecuritiesCorporateActions(repository)
    split_action = _action("SPLIT", suffix="split", split_ratio="2")
    split = actions.execute(
        position_id=first_position.position_id,
        action=split_action,
        applied_at=_bar(4, open_price="104", close_price="105").available_at,
        quantity_increment=Decimal("1"),
    )
    actions.execute(
        position_id=second_position.position_id,
        action=split_action,
        applied_at=_bar(4, open_price="104", close_price="105").available_at,
        quantity_increment=Decimal("1"),
    )
    dividend_action = _action(
        "CASH_DIVIDEND",
        suffix="dividend",
        cash_amount="0.5",
    )
    dividend = actions.execute(
        position_id=first_position.position_id,
        action=dividend_action,
        applied_at=_bar(4, open_price="104", close_price="105").available_at + timedelta(minutes=1),
        quantity_increment=Decimal("1"),
    )
    second_dividend = actions.execute(
        position_id=second_position.position_id,
        action=dividend_action,
        applied_at=_bar(4, open_price="104", close_price="105").available_at + timedelta(minutes=1),
        quantity_increment=Decimal("1"),
    )
    symbol_action = _action(
        "SYMBOL_CHANGE",
        suffix="symbol",
        successor_symbol="NEW",
    )
    renamed = actions.execute(
        position_id=first_position.position_id,
        action=symbol_action,
        applied_at=_bar(4, open_price="104", close_price="105").available_at + timedelta(minutes=2),
        quantity_increment=Decimal("1"),
    )
    actions.execute(
        position_id=second_position.position_id,
        action=symbol_action,
        applied_at=_bar(4, open_price="104", close_price="105").available_at + timedelta(minutes=2),
        quantity_increment=Decimal("1"),
    )
    delisted = actions.execute(
        position_id=second_position.position_id,
        action=_action("DELISTING", suffix="delisting"),
        applied_at=_bar(4, open_price="104", close_price="105").available_at + timedelta(minutes=3),
        quantity_increment=Decimal("1"),
    )

    suspended_trial = reconcile_securities_trial(
        trial,
        usage=PaperTrialUsage(
            completed_cycles=4,
            approved_opportunities=3,
            paper_orders=3,
            open_positions=2,
            active_strategy_risk=Money("2000"),
            cumulative_authorized_risk=Money("9000"),
            cumulative_realized_result=Decimal("-100"),
            paper_drawdown=Money("100"),
        ),
    )
    blocked_new_activity = False
    try:
        last_opportunity = repository.opportunities[
            execution_results[2].authorization.opportunity_id
        ]
        executor.execute(
            ExecuteSecuritiesPaperOpportunityCommand(
                run_id=run.run_id,
                opportunity=last_opportunity,
                signal=_signal(ordinal=2, source_bar=source),
                next_bar=eligible,
                policy=policy,
                trial=suspended_trial,
                strategy_authority_decision_id="fixture-securities-paper-authority",
                symbol="FIX",
                idempotency_key="fixture-securities-suspended-opening",
                expires_at=_bar(2, open_price="102", close_price="103").period_start,
            )
        )
    except SecuritiesPaperApplicationError:
        blocked_new_activity = True
    mark_after_suspension = marker.execute(
        position_id=second_position.position_id,
        bar=_bar(5, open_price="53", close_price="54"),
        policy=policy,
        as_of=_bar(5, open_price="53", close_price="54").available_at,
    )

    exits = ExitSecuritiesPositions(repository)
    partial_exit = exits.execute(
        ExitSecuritiesPositionCommand(
            position_id=first_position.position_id,
            reason=SecuritiesExitReason.RISK_REDUCTION,
            decision_at=_bar(5, open_price="53", close_price="54").available_at,
            next_bar=_bar(6, open_price="55", close_price="56"),
            requested_quantity=Quantity("4", "1"),
            policy=policy,
            source_identity="fixture-risk-reduction",
            idempotency_key="fixture-securities-partial-exit",
            expires_at=_bar(7, open_price="56", close_price="57").period_start,
        )
    )
    complete_exit = exits.execute(
        ExitSecuritiesPositionCommand(
            position_id=first_position.position_id,
            reason=SecuritiesExitReason.OPERATOR_EXIT,
            decision_at=_bar(7, open_price="56", close_price="57").available_at,
            next_bar=_bar(8, open_price="57", close_price="58"),
            requested_quantity=Quantity("16", "1"),
            policy=policy,
            source_identity="fixture-operator-exit",
            idempotency_key="fixture-securities-complete-exit",
            expires_at=_bar(9, open_price="58", close_price="59").period_start,
        )
    )
    risk_exit = exits.execute(
        ExitSecuritiesPositionCommand(
            position_id=second_position.position_id,
            reason=SecuritiesExitReason.DELISTING,
            decision_at=_bar(6, open_price="55", close_price="56").available_at,
            next_bar=_bar(7, open_price="56", close_price="57"),
            requested_quantity=Quantity("10", "1"),
            policy=policy,
            source_identity="fixture-terminal-value-evidence",
            idempotency_key="fixture-securities-delisting-exit",
            expires_at=_bar(8, open_price="57", close_price="58").period_start,
        )
    )
    completed = complete_run(
        repository.run(run.run_id),
        repository=repository,
        completed_at=_bar(9, open_price="58", close_price="59").available_at,
    )
    events = tuple(
        sorted(
            (("ORDER", item.order_id) for item in repository.orders.values()),
        )
    ) + tuple(
        sorted(
            (
                ("FILL", item.fill_id)
                for item in repository.fills.values()
                if item.quantity.value > 0
            ),
        )
    )
    exact_external = DeterministicLeanPaperFixtureAdapter().run(
        apex_run=completed,
        apex_events=events,
    )
    explained_external = DeterministicLeanPaperFixtureAdapter(explained_difference=True).run(
        apex_run=completed,
        apex_events=events,
    )
    conformance = BuildSecuritiesPaperConformance(repository)
    exact_report = conformance.execute(
        run=completed,
        apex_events=events,
        external=exact_external,
    )
    explained_report = conformance.execute(
        run=completed,
        apex_events=events,
        external=explained_external,
    )
    lean = LeanSecuritiesPaperAdapter()
    lean_available, lean_state = lean.availability()
    ledger = repository.ledger.metrics()
    starting_cash = Decimal("10000")
    ending_cash_formula = (
        starting_cash
        - sum(
            (
                item.fill.notional.amount + item.fill.modeled_commission.amount
                for item in execution_results
            ),
            Decimal(0),
        )
        + sum(
            (
                item.fill.notional.amount - item.fill.modeled_commission.amount
                for item in (partial_exit, complete_exit, risk_exit)
            ),
            Decimal(0),
        )
        + dividend.application.cash_entitlement.amount
        + second_dividend.application.cash_entitlement.amount
    )
    return {
        "warnings": (
            SECURITIES_PAPER_WARNING,
            NO_BROKER_WARNING,
            EXTERNAL_ENGINE_WARNING,
        ),
        "governance": governance,
        "paper_run": {
            "run_id": completed.run_id,
            "state": completed.state.value,
            "strategy_id": completed.strategy_id,
            "execution_policy": policy.execution_model,
            "trial_id": completed.trial_authorization_id,
        },
        "execution": {
            "states": tuple(item.fill.state.value for item in execution_results),
            "quantities": tuple(item.fill.quantity.value for item in execution_results),
            "order_count": len(repository.orders),
            "fill_evidence_count": len(repository.fills),
            "position_count": len(repository._positions),
            "replay_same_order": replay.order.order_id == execution_results[0].order.order_id,
            "conflicting_replay_rejected": conflict_rejected,
            "reservations_released_or_consumed": all(
                item.order.reserved_amount == Money(0) for item in execution_results
            ),
        },
        "marks": {
            "sequential_mark_count": len(repository.marks),
            "first_unrealized_pnl": first_mark.mark.unrealized_pnl,
            "second_unrealized_pnl": second_mark.mark.unrealized_pnl,
            "mark_after_suspension": mark_after_suspension.mark.mark_id,
            "realized_journals_from_marks": 0,
        },
        "corporate_actions": {
            "split_quantity": split.position.open_quantity.value,
            "split_cost_preserved": (
                split.application.prior_cost_basis == split.application.resulting_cost_basis
            ),
            "dividend_cash": dividend.application.cash_entitlement.amount,
            "symbol": renamed.position.current_symbol,
            "delisting_state": delisted.position.state.value,
            "delisting_reason": delisted.application.reason_codes,
        },
        "exits": {
            "partial_remaining_quantity": partial_exit.position.open_quantity.value,
            "complete_state": complete_exit.position.state.value,
            "risk_exit_after_suspension_state": risk_exit.position.state.value,
        },
        "trial_limits": {
            "state": suspended_trial.state.value,
            "reason": suspended_trial.suspension_reason,
            "new_activity_blocked": blocked_new_activity,
            "marks_and_exits_remained_available": True,
        },
        "ledger": {
            "starting_cash": starting_cash,
            "ending_cash": ledger.cash.amount,
            "ending_cash_formula": ending_cash_formula,
            "cash_reconciled": ledger.cash.amount == ending_cash_formula,
            "position_cost": ledger.position_cost.amount,
            "realized_pnl_before_fees": (ledger.realized_trading_pnl_before_fees),
            "dividend_income": ledger.dividend_income.amount,
            "fees": ledger.fees.amount,
            "net_pnl": ledger.net_trading_pnl,
            "journal_count": len(repository.ledger.journals),
            "all_journals_balanced": all(item.balanced for item in repository.ledger.journals),
        },
        "conformance": {
            "exact": exact_report.classification.value,
            "explained": explained_report.classification.value,
            "actual_lean_available": lean_available,
            "actual_lean_state": lean_state,
            "fixture_labeled_non_lean": True,
        },
        "isolation": {
            "provider_calls": 0,
            "broker_calls": 0,
            "live_authority": False,
            "strategy_lab_financial_authority": False,
            "financial_campaign_ids": (DEMO_CAMPAIGN_ID,),
        },
    }
