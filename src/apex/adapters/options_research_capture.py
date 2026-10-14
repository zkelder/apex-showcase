"""Orchestrates one genuine, bounded, idempotent options-research-evidence
capture run against Alpaca (Fresh Options Evidence Operations and Structure
Research Baseline v1).

Deliberately an *adapter*, not an application service: it composes
provider-specific parsing (``alpaca_options_quote_source.
observation_from_alpaca_entry``) together with the already-existing,
already-reviewed application services
(``application.options_quote_intelligence.CreateOptionsChainSnapshot``,
``application.options_structures.BuildStructuresForUnderlying``,
``application.options_research_operations.
RecordOptionsForwardEvidenceCheckpoint``) exactly the way the standalone
``options-chain-capture`` CLI command already does -- this module exists so
that *both* the CLI (``scripts/dev.py``'s ``options-research-run-once``)
and the supervised operating-cycle executor
(``adapters.operating_cycle_tasks.ExistingCapabilityOperatingTaskExecutor``)
can invoke the identical reviewed orchestration without duplicating it.

No Order, Strategy, Shared Opportunity, PAPER order, or LIVE authority is
ever created here; every persisted fact keeps ``authority = "NONE"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from apex.adapters.alpaca_options_quote_source import (
    AlpacaOptionsChainCaptureClient,
    observation_from_alpaca_entry,
)
from apex.adapters.xnys_calendar import ReviewedXNYSCalendar
from apex.application.options_quote_intelligence import (
    CreateOptionsChainSnapshot,
    CreateOptionsChainSnapshotCommand,
    OptionsQuoteIntelligenceRepository,
    RecordOptionChainCaptureRun,
    ResolveRealizedVolatility,
)
from apex.application.options_research_operations import (
    ComputeOptionsHistoryReadiness,
    OptionsResearchOperationsRepository,
    RecordOptionsForwardEvidenceCheckpoint,
)
from apex.application.options_structures import (
    BuildStructuresForUnderlying,
    BuildStructuresForUnderlyingCommand,
    OptionsStructuresRepository,
)
from apex.domain.options_quote_intelligence import (
    CaptureRunStatus,
    OptionChainCapturePlan,
    OptionChainCaptureRun,
    OptionQuoteObservation,
    OptionsChainSnapshot,
    QuoteObservationStatus,
    derive_options_chain_snapshot_id,
)
from apex.domain.options_research_operations import (
    MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
    ChainCoverageDiagnostic,
    ChainCoverageQuality,
    ChainCoverageQualityPolicy,
    OptionsCaptureDueDecision,
    OptionsCaptureDueStatus,
    OptionsCaptureWindowPolicy,
    OptionsForwardEvidenceCheckpoint,
    ResearchReadinessState,
    build_chain_coverage_diagnostic,
    determine_options_capture_due_state,
)
from apex.domain.options_structures import (
    OptionsFeePolicy,
    OptionsSlippagePolicy,
    OptionsStructureResearchPolicy,
)
from apex.domain.research_cohort import ResolvedCohortMember
from apex.domain.securities import SecuritiesCalendarSession

OPEN_INTEREST_LIMITATION = "OPEN_INTEREST_UNAVAILABLE"
ALPACA_OPTIONS_PROVIDER = "ALPACA_OPTIONS_MARKET_DATA"
DEFAULT_CALENDAR_LOOKBACK_DAYS = 10
DEFAULT_CALENDAR_LOOKAHEAD_DAYS = 5


class OptionsResearchCaptureError(RuntimeError):
    pass


def resolve_options_capture_due_decision(
    *,
    quote_repository: OptionsQuoteIntelligenceRepository,
    checkpoint_repository: OptionsResearchOperationsRepository,
    as_of: datetime,
    calendar_lookback_days: int = DEFAULT_CALENDAR_LOOKBACK_DAYS,
    calendar_lookahead_days: int = DEFAULT_CALENDAR_LOOKAHEAD_DAYS,
) -> tuple[
    OptionChainCapturePlan | None,
    OptionsCaptureWindowPolicy,
    tuple[SecuritiesCalendarSession, ...],
    OptionsCaptureDueDecision,
]:
    """The single reviewed resolution of "is a genuine options-research
    capture due right now" -- shared by the CLI (``options-research-due-
    inspect``/``options-research-run-once``), the supervised operating-cycle
    executor, and ``PostgresOperatingStateSource`` (which threads just the
    outcome through ``RetainedOperatingState`` for ``plan_due_work``), so
    the three can never silently disagree.

    Returns ``(plan, window_policy, sessions, decision)``; ``plan`` is
    ``None`` -- with ``decision.status is UNAVAILABLE`` -- exactly when no
    options-chain capture plan has ever been registered.
    """

    plan = quote_repository.latest_plan()
    window_policy = OptionsCaptureWindowPolicy.create()
    calendar = ReviewedXNYSCalendar(
        start_date=(as_of - timedelta(days=calendar_lookback_days)).date().isoformat(),
        end_date=(as_of + timedelta(days=calendar_lookahead_days)).date().isoformat(),
    )
    if plan is None:
        decision = OptionsCaptureDueDecision(
            OptionsCaptureDueStatus.UNAVAILABLE,
            None,
            None,
            None,
            ("NO_RETAINED_OPTIONS_CAPTURE_PLAN",),
            None,
        )
        return None, window_policy, calendar.sessions, decision
    capability = quote_repository.latest_capability(ALPACA_OPTIONS_PROVIDER)
    capability_supported = bool(capability is not None and capability.credentials_available)
    decision = determine_options_capture_due_state(
        sessions=calendar.sessions,
        policy=window_policy,
        now=as_of,
        capability_supported=capability_supported,
        captured_session_ids=checkpoint_repository.completed_session_ids(plan.plan_id),
    )
    return plan, window_policy, calendar.sessions, decision


@dataclass(frozen=True, slots=True)
class OptionsResearchEvidenceCaptureCommand:
    plan: OptionChainCapturePlan
    due: OptionsCaptureDueDecision
    window_policy: OptionsCaptureWindowPolicy
    sessions: tuple[SecuritiesCalendarSession, ...]
    coverage_policy: ChainCoverageQualityPolicy
    structure_policy: OptionsStructureResearchPolicy
    fee_policy: OptionsFeePolicy
    slippage_policy: OptionsSlippagePolicy
    feed_entitlement_state: str
    now: datetime


@dataclass(frozen=True, slots=True)
class UnderlyingCaptureEvidence:
    symbol: str
    underlying_canonical_security_id: str
    status: str
    snapshot_id: str | None
    coverage: ChainCoverageDiagnostic | None
    structures_evaluated: int
    structures_retained: int
    structure_research_snapshot_id: str | None
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OptionsResearchEvidenceCaptureOutcome:
    checkpoint: OptionsForwardEvidenceCheckpoint
    per_underlying: tuple[UnderlyingCaptureEvidence, ...]
    provider_request_count: int


@dataclass(frozen=True, slots=True)
class _PendingFreshCapture:
    """One member's genuinely fresh evidence, gathered but *not yet*
    persisted as a snapshot -- see ``AlpacaOptionsResearchEvidenceCapture.
    run``'s Phase 1/2/3 split (Options Automatic Capture Parent Persistence
    & Partial-Evidence Recovery v1). ``predicted_snapshot_id`` is computed
    via ``derive_options_chain_snapshot_id`` -- the identical "know the id
    before the full object exists" primitive ``OptionChainCaptureRun.
    derive_run_id`` already uses -- so this run's own summary row can
    truthfully list every snapshot it produced before any of them are
    actually written."""

    member: ResolvedCohortMember
    snapshot_command: CreateOptionsChainSnapshotCommand
    predicted_snapshot_id: str
    coverage: ChainCoverageDiagnostic | None
    observations: tuple[OptionQuoteObservation, ...]


class AlpacaOptionsResearchEvidenceCapture:
    """Runs the bounded cohort capture exactly once, per already-declared
    plan members: reuse an already-retained snapshot whose ``captured_at``
    genuinely falls inside this session's due window (same-session replay,
    zero further provider requests for that underlying), else perform one
    genuine bounded Alpaca capture, then build structures (fresh-only, via
    the existing default eligibility policy) and fold every underlying's
    outcome into one retained checkpoint."""

    def __init__(
        self,
        *,
        quote_repository: OptionsQuoteIntelligenceRepository,
        structures_repository: OptionsStructuresRepository,
        checkpoint_repository: OptionsResearchOperationsRepository,
        rv_resolver: ResolveRealizedVolatility,
        capture_client: AlpacaOptionsChainCaptureClient | None = None,
    ) -> None:
        self._quote_repository = quote_repository
        self._structures_repository = structures_repository
        self._checkpoint_repository = checkpoint_repository
        self._rv_resolver = rv_resolver
        self._capture_client = capture_client or AlpacaOptionsChainCaptureClient()
        self._snapshot_service = CreateOptionsChainSnapshot(quote_repository)
        self._structure_service = BuildStructuresForUnderlying(structures_repository)
        self._checkpoint_service = RecordOptionsForwardEvidenceCheckpoint(checkpoint_repository)
        self._run_service = RecordOptionChainCaptureRun(quote_repository)
        self._history_service = ComputeOptionsHistoryReadiness(quote_repository=quote_repository)
        # Survives even when ``run`` raises: genuine provider requests made
        # before a later persistence failure must still be reportable by the
        # caller (``adapters.operating_cycle_tasks``) -- see Part 12 of
        # Options Automatic Capture Parent Persistence & Partial-Evidence
        # Recovery v1. Local variables inside ``run`` are lost on the way up
        # an exception; instance state is not. Reset at the start of every
        # ``run`` call, never accumulated across invocations.
        self.provider_requests_so_far = 0

    def run(
        self, command: OptionsResearchEvidenceCaptureCommand
    ) -> OptionsResearchEvidenceCaptureOutcome:
        if command.due.status is not OptionsCaptureDueStatus.DUE:
            raise OptionsResearchCaptureError("CAPTURE_INVOKED_WHILE_NOT_DUE")
        session_id = command.due.session_id
        window_open = command.due.window_open_at
        window_close = command.due.window_close_at
        if session_id is None or window_open is None or window_close is None:
            raise OptionsResearchCaptureError("CAPTURE_DUE_DECISION_MISSING_WINDOW")

        self.provider_requests_so_far = 0
        as_of: date = command.now.date()
        per_underlying: list[UnderlyingCaptureEvidence] = []
        fresh_snapshot_ids: list[str] = []
        structure_snapshot_ids: list[str] = []
        quote_quality_counts: dict[str, int] = {}
        coverage_quality_counts: dict[str, int] = {}
        cap_constrained: list[str] = []
        structures_evaluated_total = 0
        structures_retained_total = 0
        stale_count = 0
        errors: list[str] = []

        # Session-scoped, not wall-clock: a resumed invocation within the
        # identical still-open window reuses the identical run_id, rather
        # than minting a new one every retry (unchanged from before this
        # milestone). What changes here is that this run_id now genuinely
        # gets a persisted ``options_chain_capture_run`` parent row -- see
        # Phase 2 below -- *before* anything can reference it via foreign
        # key, closing the exact gap "Options Automatic Capture Parent
        # Persistence & Partial-Evidence Recovery v1" fixes.
        run_id = OptionChainCaptureRun.derive_run_id(
            plan_id=command.plan.plan_id, requested_at=window_open
        )

        def _finalize(
            *,
            member: ResolvedCohortMember,
            snapshot: OptionsChainSnapshot,
            observations: tuple[OptionQuoteObservation, ...],
            coverage: ChainCoverageDiagnostic | None,
            status: str,
        ) -> None:
            """Shared per-member bookkeeping once a member's snapshot is
            genuinely durable -- either because it was reused from an
            already-persisted in-window snapshot, or because Phase 3 just
            finished persisting a freshly-captured one. Identical to this
            method's pre-fix per-member tail, just no longer duplicated
            between the two call sites."""

            nonlocal stale_count, structures_evaluated_total, structures_retained_total
            fresh_snapshot_ids.append(snapshot.snapshot_id)
            if not (window_open <= snapshot.captured_at <= window_close):
                stale_count += 1
            for observation in observations:
                quote_quality_counts[observation.status.value] = (
                    quote_quality_counts.get(observation.status.value, 0) + 1
                )
            if coverage is not None:
                coverage_quality_counts[coverage.quality.value] = (
                    coverage_quality_counts.get(coverage.quality.value, 0) + 1
                )
                if coverage.quality is ChainCoverageQuality.CAP_CONSTRAINED:
                    cap_constrained.append(member.symbol)

            structure_outcome = self._structure_service.execute(
                BuildStructuresForUnderlyingCommand(
                    structure_policy=command.structure_policy,
                    fee_policy=command.fee_policy,
                    slippage_policy=command.slippage_policy,
                    snapshot=snapshot,
                    observations=observations,
                    as_of=as_of,
                    evaluated_at=command.now,
                    quantity=1,
                )
            )
            evaluated = structure_outcome.research_snapshot.generation_report.evaluated_structures
            structures_evaluated_total += evaluated
            structures_retained_total += len(structure_outcome.structures)
            structure_snapshot_ids.append(structure_outcome.research_snapshot.research_snapshot_id)

            per_underlying.append(
                UnderlyingCaptureEvidence(
                    member.symbol,
                    member.stable_security_id,
                    status,
                    snapshot.snapshot_id,
                    coverage,
                    evaluated,
                    len(structure_outcome.structures),
                    structure_outcome.research_snapshot.research_snapshot_id,
                    (),
                )
            )

        # -- Phase 1: resolve every member's evidence. A reused member's
        # snapshot already durably exists (from an earlier invocation this
        # same still-open window), so it is finalized immediately -- there
        # is no ordering hazard. A genuinely fresh capture's raw
        # observations are persisted right away too (``options_quote_
        # observation`` carries no foreign key to plan/run/snapshot, so
        # this is always safe, and matches the durable-incremental-evidence
        # behavior already proven in production on 2026-08-12: 200 genuine
        # AAPL observations survived even though the run/snapshot did not).
        # Its *snapshot* is deliberately NOT persisted yet -- that must wait
        # for Phase 2 to give this run a valid parent row first -- so only a
        # cheap, pure prediction of its id (``derive_options_chain_
        # snapshot_id``) is computed here and stashed in ``pending``.
        reused_snapshot_ids: list[str] = []
        pending: list[_PendingFreshCapture] = []
        credentials_absent_encountered = False
        contracts_considered_total = 0
        contracts_quoted_total = 0
        payload_bytes_total = 0

        for member in command.plan.members:
            reused = next(
                (
                    snapshot
                    for snapshot in self._quote_repository.snapshots_for_underlying(
                        member.stable_security_id
                    )
                    if window_open <= snapshot.captured_at <= window_close
                ),
                None,
            )
            if reused is not None:
                reused_snapshot_ids.append(reused.snapshot_id)
                observations = self._quote_repository.observations_by_ids(reused.observation_ids)
                _finalize(
                    member=member,
                    snapshot=reused,
                    observations=observations,
                    coverage=None,
                    status="REUSED_ALREADY_CAPTURED_THIS_SESSION",
                )
                continue

            if not self._capture_client.credentials_available:
                credentials_absent_encountered = True
                per_underlying.append(
                    UnderlyingCaptureEvidence(
                        member.symbol,
                        member.stable_security_id,
                        "BLOCKED_CREDENTIALS_ABSENT",
                        None,
                        None,
                        0,
                        0,
                        None,
                        ("ALPACA_CREDENTIALS_ABSENT",),
                    )
                )
                errors.append("ALPACA_CREDENTIALS_ABSENT")
                continue
            outcome = self._capture_client.capture_underlying(
                member=member,
                bounds=command.plan.bounds,
                feed=command.plan.feed,
                captured_at=command.now,
            )
            self.provider_requests_so_far += outcome.request_count
            payload_bytes_total += outcome.bytes_received
            if outcome.status != "CAPTURED" or outcome.underlying_reference_price is None:
                reason = outcome.reason or outcome.status
                per_underlying.append(
                    UnderlyingCaptureEvidence(
                        member.symbol,
                        member.stable_security_id,
                        outcome.status,
                        None,
                        None,
                        0,
                        0,
                        None,
                        (reason,),
                    )
                )
                errors.append(f"{member.symbol}:{reason}")
                continue
            receipt_id = outcome.source_receipt_ids[-1] if outcome.source_receipt_ids else "unknown"
            observations = tuple(
                parsed[1]
                for entry in outcome.contract_entries
                if (
                    parsed := observation_from_alpaca_entry(
                        entry=entry,
                        underlying_canonical_security_id=member.stable_security_id,
                        underlying_reference_price=outcome.underlying_reference_price,
                        captured_at=command.now,
                        receipt_id=receipt_id,
                    )
                )
                is not None
            )
            # Durable now, independent of whatever happens to this run's
            # lineage/snapshot below -- see the Phase 1 docstring above.
            if observations:
                self._quote_repository.save_observations(observations)
            contracts_considered_total += len(observations)
            contracts_quoted_total += sum(
                1 for item in observations if item.status is QuoteObservationStatus.OBSERVED
            )
            realized_volatility = self._rv_resolver.resolve(
                stable_security_id=member.stable_security_id, as_of=command.now
            )
            snapshot_command = CreateOptionsChainSnapshotCommand(
                plan=command.plan,
                run_id=run_id,
                member=member,
                underlying_reference_price=outcome.underlying_reference_price,
                underlying_price_source=outcome.underlying_price_source or "UNKNOWN",
                captured_at=command.now,
                observations=observations,
                as_of=as_of,
                provider="ALPACA_OPTIONS_MARKET_DATA",
                source_receipt_ids=outcome.source_receipt_ids,
                realized_volatility_annualized=realized_volatility.realized_volatility_primary,
                realized_volatility_status=realized_volatility.status,
            )
            predicted_snapshot_id = derive_options_chain_snapshot_id(
                underlying_canonical_security_id=member.stable_security_id,
                captured_at=command.now,
                observations=observations,
            )
            coverage: ChainCoverageDiagnostic | None = None
            if outcome.selection is not None:
                coverage = build_chain_coverage_diagnostic(
                    underlying_symbol=member.symbol,
                    underlying_canonical_security_id=member.stable_security_id,
                    contracts_returned_by_provider=outcome.contracts_returned_by_provider,
                    selection=outcome.selection,
                    policy=command.coverage_policy,
                )
            pending.append(
                _PendingFreshCapture(
                    member, snapshot_command, predicted_snapshot_id, coverage, observations
                )
            )

        # -- Phase 2: this invocation's own cohort pass is complete, so this
        # run's final outcome (for the members it just touched) is now
        # genuinely knowable -- exactly what ``OptionChainCaptureRun.
        # create`` requires (there is no "in progress" run state in this
        # domain; every ``CaptureRunStatus`` is terminal). Persist it now,
        # *before* any snapshot beneath it. Idempotent create-once
        # (``RecordOptionChainCaptureRun``): a resumed invocation later in
        # the identical still-open window derives this identical run_id and
        # safely reuses whatever an earlier invocation already recorded --
        # ``options_chain_capture_run`` is immutable, like every other table
        # in this evidence family, so this never overwrites it.
        underlyings_with_evidence = len(reused_snapshot_ids) + len(pending)
        if underlyings_with_evidence == 0:
            run_status = (
                CaptureRunStatus.BLOCKED_CREDENTIALS_ABSENT
                if credentials_absent_encountered
                else CaptureRunStatus.EMPTY
            )
            run_reason = (
                "ALPACA_CREDENTIALS_ABSENT"
                if credentials_absent_encountered
                else "NO_UNDERLYING_PRODUCED_QUOTABLE_EVIDENCE"
            )
        elif underlyings_with_evidence == len(command.plan.members):
            run_status = CaptureRunStatus.COMPLETE
            run_reason = None
        else:
            run_status = CaptureRunStatus.PARTIAL
            run_reason = None
        run = OptionChainCaptureRun.create(
            plan_id=command.plan.plan_id,
            requested_at=window_open,
            completed_at=command.now,
            status=run_status,
            underlyings_requested=len(command.plan.members),
            underlyings_captured=underlyings_with_evidence,
            provider_request_count=self.provider_requests_so_far,
            payload_bytes=payload_bytes_total,
            contracts_considered=contracts_considered_total,
            contracts_quoted=contracts_quoted_total,
            snapshot_ids=(
                *reused_snapshot_ids,
                *(item.predicted_snapshot_id for item in pending),
            ),
            reason=run_reason,
        )
        assert run.run_id == run_id, "run_id must stay derivable before the run object exists"
        self._run_service.execute(run=run)

        # -- Phase 3: this run's own parent row now durably exists, so every
        # freshly-captured member's snapshot (and, through ``_finalize``,
        # its structures) can be persisted for real -- the exact ordering
        # gap this milestone closes. ``CreateOptionsChainSnapshot.execute``
        # re-persists ``observations`` too, but that is an idempotent no-op
        # (Phase 1 already saved them): every row it touches is looked up by
        # id first and only inserted if genuinely absent.
        for item in pending:
            snapshot, _features = self._snapshot_service.execute(item.snapshot_command)
            assert snapshot.snapshot_id == item.predicted_snapshot_id, (
                "predicted and persisted snapshot ids must never diverge"
            )
            _finalize(
                member=item.member,
                snapshot=snapshot,
                observations=item.observations,
                coverage=item.coverage,
                status="CAPTURED_FRESH",
            )

        readiness = tuple(
            self._history_service.execute(
                underlying_canonical_security_id=member.stable_security_id,
                sessions=command.sessions,
                policy=command.window_policy,
                minimum_required_sessions=MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
            )
            for member in command.plan.members
        )
        ready_count = sum(
            1
            for item in readiness
            if item.state is ResearchReadinessState.LONGITUDINAL_RESEARCH_READY
        )
        forward_history_count = sum(1 for item in readiness if item.distinct_session_count > 0)

        checkpoint = OptionsForwardEvidenceCheckpoint.create(
            plan_id=command.plan.plan_id,
            session_id=session_id,
            capture_window_policy_id=command.window_policy.policy_id,
            window_open_at=window_open,
            window_close_at=window_close,
            provider="ALPACA_OPTIONS_MARKET_DATA",
            feed=command.plan.feed,
            feed_entitlement_state=command.feed_entitlement_state,
            underlying_symbols_captured=tuple(
                item.symbol for item in per_underlying if item.snapshot_id is not None
            ),
            underlyings_requested_count=len(command.plan.members),
            underlyings_captured_count=sum(
                1 for item in per_underlying if item.snapshot_id is not None
            ),
            fresh_snapshot_ids=tuple(fresh_snapshot_ids),
            stale_snapshot_count=stale_count,
            quote_quality_counts=tuple(sorted(quote_quality_counts.items())),
            chain_coverage_quality_counts=tuple(sorted(coverage_quality_counts.items())),
            cap_constrained_underlyings=tuple(sorted(cap_constrained)),
            structure_snapshot_ids=tuple(structure_snapshot_ids),
            structures_evaluated=structures_evaluated_total,
            structures_retained=structures_retained_total,
            fee_policy_id=command.fee_policy.policy_id,
            fee_evidence_status=command.fee_policy.fee_evidence_status,
            opra_entitlement_state=command.feed_entitlement_state,
            open_interest_limitation=OPEN_INTEREST_LIMITATION,
            iv_history_minimum_required=MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
            iv_history_ready_underlying_count=ready_count,
            forward_history_underlying_count=forward_history_count,
            created_at=command.now,
            errors=tuple(errors),
        )
        recorded = self._checkpoint_service.execute(checkpoint=checkpoint)
        return OptionsResearchEvidenceCaptureOutcome(
            recorded, tuple(per_underlying), self.provider_requests_so_far
        )
