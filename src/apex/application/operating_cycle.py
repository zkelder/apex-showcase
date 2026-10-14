"""Application orchestration for one exact bounded Apex operating cycle."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from apex.domain.evidence_recovery import (
    EvidenceRecoveryError,
    assert_backdated_execution_allowed,
)
from apex.domain.operating_cycle import (
    DueDisposition,
    DueTask,
    DueWorkPlan,
    OperatingBrief,
    OperatingControlEvent,
    OperatingControlState,
    OperatingCycleError,
    OperatingCycleEvent,
    OperatingCyclePlan,
    OperatingCycleRun,
    OperatingCycleState,
    OperatingHealth,
    OperatingTaskResult,
    OperatingTaskStatus,
    OperatingTaskType,
    PaperActivationEvent,
    PaperActivationState,
    RetainedOperatingState,
    TaskExecution,
    build_operating_brief,
    build_operating_health,
    effective_paper_activation,
    options_capture_awaits_same_day_reevaluation,
    plan_due_work,
    terminal_cycle_state,
)
from apex.domain.operational_reliability import AlertSeverity


class OperatingStateSource(Protocol):
    def snapshot(
        self, *, as_of: datetime, evaluated_at: datetime | None = None
    ) -> RetainedOperatingState: ...

    def brief_inputs(
        self,
    ) -> tuple[str | None, tuple[tuple[str, int], ...], str | None, tuple[str, ...]]: ...


class OperatingTaskExecutor(Protocol):
    def execute(
        self,
        *,
        task: DueTask,
        cycle_id: str,
        plan: OperatingCyclePlan,
        as_of: datetime,
        executed_at: datetime,
    ) -> TaskExecution: ...


class OperatingCycleRepository(Protocol):
    def plan(self, plan_id: str) -> OperatingCyclePlan | None: ...
    def plans(self) -> tuple[OperatingCyclePlan, ...]: ...
    def save_plan(self, *, idempotency_key: str, plan: OperatingCyclePlan) -> bool: ...
    def control_events(self, plan_id: str) -> tuple[OperatingControlEvent, ...]: ...
    def save_control_event(self, *, idempotency_key: str, event: OperatingControlEvent) -> bool: ...
    def paper_activation_events(self, plan_id: str) -> tuple[PaperActivationEvent, ...]: ...
    def save_paper_activation_event(
        self, *, idempotency_key: str, event: PaperActivationEvent
    ) -> bool: ...
    def cycle(self, cycle_id: str) -> OperatingCycleRun | None: ...
    def cycle_idempotency_key(self, cycle_id: str) -> str | None: ...
    def cycle_for_plan_as_of(self, plan_id: str, as_of: datetime) -> OperatingCycleRun | None: ...
    def cycles(self) -> tuple[OperatingCycleRun, ...]: ...
    def save_cycle(
        self,
        *,
        idempotency_key: str,
        cycle: OperatingCycleRun,
        due_work: DueWorkPlan,
    ) -> bool: ...
    def due_work(self, cycle_id: str) -> DueWorkPlan | None: ...
    def cycle_events(self, cycle_id: str) -> tuple[OperatingCycleEvent, ...]: ...
    def save_cycle_event(self, event: OperatingCycleEvent) -> bool: ...
    def task_results(self, cycle_id: str) -> tuple[OperatingTaskResult, ...]: ...
    def task_result(self, task_result_id: str) -> OperatingTaskResult | None: ...
    def save_task_result(self, result: OperatingTaskResult) -> bool: ...
    def brief(self, brief_id: str) -> OperatingBrief | None: ...
    def briefs(self) -> tuple[OperatingBrief, ...]: ...
    def save_brief(self, brief: OperatingBrief) -> bool: ...
    def health(self, health_id: str) -> OperatingHealth | None: ...
    def health_summaries(self) -> tuple[OperatingHealth, ...]: ...
    def save_health(self, health: OperatingHealth) -> bool: ...


@dataclass(frozen=True, slots=True)
class OperatingCycleResult:
    cycle: OperatingCycleRun
    due_work: DueWorkPlan
    state: OperatingCycleState
    tasks: tuple[OperatingTaskResult, ...]
    brief: OperatingBrief | None
    health: OperatingHealth
    idempotent_replay: bool


class RegisterOperatingCyclePlan:
    def __init__(self, repository: OperatingCycleRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        plan_id: str,
        crypto_pulse_plan_id: str,
        actor: str,
        created_at: datetime,
        idempotency_key: str,
    ) -> tuple[OperatingCyclePlan, bool]:
        plan = OperatingCyclePlan.create(
            plan_id=plan_id,
            crypto_pulse_plan_id=crypto_pulse_plan_id,
            actor=actor,
            created_at=created_at,
        )
        replay = self._repository.save_plan(idempotency_key=idempotency_key, plan=plan)
        return plan, replay


class SetOperatingCycleControl:
    def __init__(self, repository: OperatingCycleRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        event_id: str,
        plan_id: str,
        state: OperatingControlState,
        reason: str,
        actor: str,
        occurred_at: datetime,
        idempotency_key: str,
    ) -> tuple[OperatingControlEvent, bool]:
        if self._repository.plan(plan_id) is None:
            raise OperatingCycleError("OPERATING_PLAN_NOT_FOUND")
        events = self._repository.control_events(plan_id)
        # An exact replay of an already-retained event_id must reuse its
        # original ordinal so it reconstructs the identical canonical_hash;
        # only a genuinely new event_id gets the next ordinal in sequence.
        # (Mirrors the same fix applied to SetPaperActivation below.)
        existing = next((item for item in events if item.event_id == event_id), None)
        event = OperatingControlEvent.create(
            event_id=event_id,
            plan_id=plan_id,
            ordinal=existing.ordinal if existing is not None else len(events) + 1,
            state=state,
            reason=reason,
            actor=actor,
            occurred_at=occurred_at,
        )
        replay = self._repository.save_control_event(idempotency_key=idempotency_key, event=event)
        return event, replay


class SetPaperActivation:
    """Governed, auditable operator action that enables or disables internal
    PAPER consideration for one exact retained OperatingCyclePlan.  Never
    mutates or replaces the plan; appends an immutable event that later cycles
    fold into the effective ``paper_eligible`` used only for internal PAPER
    consideration.  Entirely separate from LIVE authority; grants no
    provider-write permission."""

    def __init__(self, repository: OperatingCycleRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        event_id: str,
        plan_id: str,
        state: PaperActivationState,
        reason: str,
        actor: str,
        occurred_at: datetime,
        idempotency_key: str,
    ) -> tuple[PaperActivationEvent, bool]:
        if self._repository.plan(plan_id) is None:
            raise OperatingCycleError("OPERATING_PLAN_NOT_FOUND")
        events = self._repository.paper_activation_events(plan_id)
        # An exact replay of an already-retained event_id must reuse its
        # original ordinal so it reconstructs the identical canonical_hash;
        # only a genuinely new event_id gets the next ordinal in sequence.
        existing = next((item for item in events if item.event_id == event_id), None)
        event = PaperActivationEvent.create(
            event_id=event_id,
            plan_id=plan_id,
            ordinal=existing.ordinal if existing is not None else len(events) + 1,
            state=state,
            reason=reason,
            actor=actor,
            occurred_at=occurred_at,
        )
        replay = self._repository.save_paper_activation_event(
            idempotency_key=idempotency_key, event=event
        )
        return event, replay


class PlanOperatingDueWork:
    def __init__(
        self,
        repository: OperatingCycleRepository,
        state_source: OperatingStateSource,
    ) -> None:
        self._repository = repository
        self._state_source = state_source

    def execute(
        self,
        *,
        cycle_id: str,
        plan_id: str,
        as_of: datetime,
        evaluated_at: datetime | None = None,
    ) -> DueWorkPlan:
        plan = self._repository.plan(plan_id)
        if plan is None:
            raise OperatingCycleError("OPERATING_PLAN_NOT_FOUND")
        retained = self._state_source.snapshot(as_of=as_of, evaluated_at=evaluated_at)
        return plan_due_work(
            plan=plan,
            cycle_id=cycle_id,
            as_of=as_of,
            retained=retained,
        )


class ExecuteOperatingCycle:
    def __init__(
        self,
        repository: OperatingCycleRepository,
        state_source: OperatingStateSource,
        executor: OperatingTaskExecutor,
    ) -> None:
        self._repository = repository
        self._state_source = state_source
        self._executor = executor

    def _retained_result(self, cycle: OperatingCycleRun) -> OperatingCycleResult:
        due = self._repository.due_work(cycle.cycle_id)
        if due is None:
            raise OperatingCycleError("OPERATING_DUE_WORK_MISSING")
        events = self._repository.cycle_events(cycle.cycle_id)
        if not events:
            raise OperatingCycleError("OPERATING_CYCLE_EVENT_MISSING")
        tasks = self._repository.task_results(cycle.cycle_id)
        brief = self._repository.brief(f"{cycle.cycle_id}-operating-brief")
        health = self._repository.health(f"{cycle.cycle_id}-operating-health")
        if health is None:
            raise OperatingCycleError("OPERATING_HEALTH_MISSING")
        return OperatingCycleResult(
            cycle,
            due,
            events[-1].state,
            tasks,
            brief,
            health,
            True,
        )

    def _dispatch_task(
        self,
        *,
        task: DueTask,
        cycle_id: str,
        plan: OperatingCyclePlan,
        as_of: datetime,
        executed_at: datetime,
        by_type: dict[str, OperatingTaskResult],
    ) -> TaskExecution:
        """One task's disposition dispatch -- extracted, not changed, from
        the per-task loop so both a fresh cycle's own loop and
        ``_resume_cycle`` share the identical rules (Recurring Operating
        Health & Unattended Evidence Closure v1)."""

        dependency_failed = task.task_type.value != "OPERATING_BRIEF_BUILD" and any(
            by_type.get(dependency.value) is not None
            and by_type[dependency.value].status
            in {
                OperatingTaskStatus.FAILED,
                OperatingTaskStatus.BLOCKED,
            }
            for dependency in task.dependency_types
        )
        if dependency_failed:
            return TaskExecution(
                OperatingTaskStatus.BLOCKED,
                (),
                (),
                False,
                0,
                ("DEPENDENCY_FAILED",),
                "OPERATING_TASK_DEPENDENCY_BLOCKED",
            )
        if task.disposition is DueDisposition.NOT_DUE:
            return TaskExecution(
                OperatingTaskStatus.SKIPPED_NOT_DUE,
                (),
                task.source_evidence_ids,
                False,
                0,
            )
        if task.disposition is DueDisposition.UNAVAILABLE:
            return TaskExecution(
                OperatingTaskStatus.UNAVAILABLE,
                (),
                task.source_evidence_ids,
                False,
                0,
                task.reasons,
            )
        if task.disposition is DueDisposition.BLOCKED:
            return TaskExecution(
                OperatingTaskStatus.BLOCKED,
                (),
                task.source_evidence_ids,
                False,
                0,
                task.reasons,
                "OPERATING_TASK_PLANNING_BLOCK",
            )
        if task.task_type.value == "OPERATING_BRIEF_BUILD":
            return TaskExecution(
                OperatingTaskStatus.COMPLETED,
                (f"{cycle_id}-operating-brief",),
                (),
                True,
                0,
            )
        try:
            return self._executor.execute(
                task=task,
                cycle_id=cycle_id,
                plan=plan,
                as_of=as_of,
                executed_at=executed_at,
            )
        except (OSError, RuntimeError, ValueError) as error:
            # Usually genuinely zero (nothing reached a provider before this
            # task type's failure), but not always: a task whose executor
            # already made bounded provider reads before a later step failed
            # can annotate the propagating exception with
            # ``network_requests_before_failure`` (see
            # ``adapters.operating_cycle_tasks``'s options-capture branch)
            # so that genuine count survives here instead of being silently
            # reported as zero. Absent for every other task type today, so
            # ``getattr`` keeps their behavior byte-for-byte unchanged.
            return TaskExecution(
                OperatingTaskStatus.FAILED,
                (),
                (),
                False,
                getattr(error, "network_requests_before_failure", 0),
                (),
                type(error).__name__,
                True,
            )

    def _resume_cycle(
        self,
        cycle: OperatingCycleRun,
        plan: OperatingCyclePlan,
        executed_at: datetime,
    ) -> OperatingCycleResult:
        """Reconcile an already-retained cycle for today against current
        reality instead of blindly replaying it. Two genuine gaps this
        closes (Recurring Operating Health & Unattended Evidence Closure
        v1):

        1. A task type whose due-ness can genuinely change later the same
           UTC day (today: only ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE``, while
           its session capture window has not yet opened) is never
           persisted as a frozen ``NOT_DUE`` result in the first place --
           see ``options_capture_awaits_same_day_reevaluation`` -- so it can
           still be genuinely absent from this cycle's retained task
           results and resolved here once it becomes due, missed, or
           otherwise terminal.
        2. A process crash between ``save_cycle`` committing and
           ``save_health`` committing (e.g. a host restart mid-cycle)
           previously left a cycle permanently unrecoverable: every later
           call found ``occupied is not None`` and immediately raised
           ``OPERATING_HEALTH_MISSING`` from ``_retained_result``. This
           method resumes any genuinely unexecuted task -- using its
           already-frozen due disposition, never re-derived -- exactly as
           the original per-task loop would have, then completes
           finalization if it never finished.

        A cycle already fully resolved (every due task has a persisted
        result, and health already exists) costs exactly the same reads as
        ``_retained_result`` and returns byte-identical output -- this is
        the overwhelmingly common case for a supervised worker polling
        every few minutes across a day, so it is checked first and returned
        via the unchanged, cheap fast path."""

        due = self._repository.due_work(cycle.cycle_id)
        if due is None:
            raise OperatingCycleError("OPERATING_DUE_WORK_MISSING")
        events = self._repository.cycle_events(cycle.cycle_id)
        if not events:
            raise OperatingCycleError("OPERATING_CYCLE_EVENT_MISSING")
        if events[-1].state is OperatingCycleState.SKIPPED_PAUSED:
            # A paused cycle never reopens on its own -- an operator must
            # explicitly resume the plan and let a genuinely new cycle
            # react. Unchanged from before this fix.
            return self._retained_result(cycle)

        persisted = {
            result.task_type.value: result
            for result in self._repository.task_results(cycle.cycle_id)
        }
        due_task_types = {task.task_type.value for task in due.tasks}
        if due_task_types <= persisted.keys() and self._repository.health(
            f"{cycle.cycle_id}-operating-health"
        ):
            return self._retained_result(cycle)

        by_type: dict[str, OperatingTaskResult] = dict(persisted)
        results: list[OperatingTaskResult] = list(persisted.values())
        fresh_due_work: DueWorkPlan | None = None
        newly_resolved = False

        for task in due.tasks:
            if task.task_type.value in by_type:
                continue
            effective_task = task
            if options_capture_awaits_same_day_reevaluation(task):
                if fresh_due_work is None:
                    fresh_retained = self._state_source.snapshot(
                        as_of=cycle.as_of, evaluated_at=executed_at
                    )
                    fresh_due_work = plan_due_work(
                        plan=plan,
                        cycle_id=cycle.cycle_id,
                        as_of=cycle.as_of,
                        retained=fresh_retained,
                    )
                effective_task = next(
                    item
                    for item in fresh_due_work.tasks
                    if item.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE
                )
                if options_capture_awaits_same_day_reevaluation(effective_task):
                    # Still genuinely not due -- leave unresolved for a
                    # later call rather than persisting a premature outcome.
                    continue
            elif task.task_type is OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH:
                # Counterfactual Follow-Up Backlog Throughput v1: this task
                # type only ever reaches this branch on a continuation tick
                # -- a prior tick already dispatched it this same UTC day and
                # its own execution reported ``awaits_intraday_continuation``
                # (see that field's own docstring), so it was deliberately
                # left absent from ``by_type`` instead of frozen. Re-derive
                # its disposition fresh here, exactly like options capture
                # immediately above and reusing the identical ``fresh_due_
                # work`` snapshot when both need one this same tick, so a
                # same-day options-capture window that has since opened or
                # is imminent still wins:
                # ``counterfactual_followup_deferred_for_options_priority``
                # is re-evaluated against *current* reality on every tick,
                # never the stale disposition this cycle's original
                # ``due_work`` recorded back at cycle-creation time.
                if fresh_due_work is None:
                    fresh_retained = self._state_source.snapshot(
                        as_of=cycle.as_of, evaluated_at=executed_at
                    )
                    fresh_due_work = plan_due_work(
                        plan=plan,
                        cycle_id=cycle.cycle_id,
                        as_of=cycle.as_of,
                        retained=fresh_retained,
                    )
                effective_task = next(
                    item
                    for item in fresh_due_work.tasks
                    if item.task_type is OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH
                )
                if effective_task.disposition is DueDisposition.NOT_DUE:
                    # Currently yielding to a same-day options-capture
                    # window (follow-up's only possible NOT_DUE reason --
                    # see ``plan_due_work``) -- stay unresolved and try
                    # again on a later tick, exactly like a still-not-due
                    # options capture itself.
                    continue
            execution = self._dispatch_task(
                task=effective_task,
                cycle_id=cycle.cycle_id,
                plan=plan,
                as_of=cycle.as_of,
                executed_at=executed_at,
                by_type=by_type,
            )
            if execution.awaits_intraday_continuation:
                # Counterfactual Follow-Up Backlog Throughput v1: real,
                # permanently-committed bounded work already happened this
                # tick (see ``TaskExecution.awaits_intraday_continuation``'s
                # own docstring); simply leave this task unresolved again so
                # the next tick's ``_resume_cycle`` call takes this exact
                # same branch and dispatches one more bounded round --
                # mirroring the ``continue`` immediately above for a
                # still-not-due options capture task exactly.
                continue
            result = OperatingTaskResult.create(
                cycle_id=cycle.cycle_id,
                task=effective_task,
                execution=execution,
                started_at=executed_at,
                completed_at=executed_at,
            )
            self._repository.save_task_result(result)
            results.append(result)
            by_type[effective_task.task_type.value] = result
            newly_resolved = True

        ordered_results = tuple(sorted(results, key=lambda item: item.ordinal))
        computed_final_state = terminal_cycle_state(ordered_results)
        if newly_resolved or events[-1].state in {
            OperatingCycleState.PLANNED,
            OperatingCycleState.RUNNING,
        }:
            self._repository.save_cycle_event(
                OperatingCycleEvent.create(
                    cycle_id=cycle.cycle_id,
                    ordinal=len(events) + 1,
                    state=computed_final_state,
                    occurred_at=executed_at,
                    warnings=tuple(
                        sorted(
                            {warning for item in ordered_results for warning in item.warning_codes}
                        )
                    ),
                    failures=tuple(
                        item.error_code for item in ordered_results if item.error_code is not None
                    ),
                )
            )

        health = self._repository.health(f"{cycle.cycle_id}-operating-health")
        brief = self._repository.brief(f"{cycle.cycle_id}-operating-brief")
        wrote_finalization = False
        if health is None:
            final_retained = self._state_source.snapshot(
                as_of=cycle.as_of, evaluated_at=executed_at
            )
            total_market_state, opportunity_counts, unallocated, score_changes = (
                self._state_source.brief_inputs()
            )
            brief_result = next(
                (
                    item
                    for item in ordered_results
                    if item.task_type.value == "OPERATING_BRIEF_BUILD"
                    and item.status is OperatingTaskStatus.COMPLETED
                ),
                None,
            )
            if brief_result is not None and brief is None:
                brief = build_operating_brief(
                    cycle_id=cycle.cycle_id,
                    as_of=cycle.as_of,
                    retained=final_retained,
                    task_results=ordered_results,
                    total_market_state=total_market_state,
                    opportunity_counts=opportunity_counts,
                    unallocated_cash=unallocated,
                    largest_crypto_score_changes=score_changes,
                )
                self._repository.save_brief(brief)
            controls = self._repository.control_events(cycle.plan_id)
            control_state = controls[-1].state if controls else OperatingControlState.ENABLED
            health = build_operating_health(
                cycle_id=cycle.cycle_id,
                as_of=cycle.as_of,
                retained=final_retained,
                task_results=ordered_results,
                control_state=control_state,
                paper_eligible=plan.paper_eligible,
            )
            self._repository.save_health(health)
            wrote_finalization = True

        # The persisted ``due_work_payload`` blob stays exactly what
        # cycle-creation time computed (an immutable, point-in-time-of-
        # creation audit record); once a reevaluation has genuinely run,
        # the *returned* due_work instead reflects live reality as of this
        # exact call -- e.g. so ``derive_operating_cycle_alert_candidates``
        # genuinely observes a freshly-discovered ``MISSED_CAPTURE_WINDOW``
        # on the same call that discovers it, not one call later.
        returned_due_work = fresh_due_work if fresh_due_work is not None else due
        return OperatingCycleResult(
            cycle,
            returned_due_work,
            computed_final_state,
            ordered_results,
            brief,
            health,
            not (newly_resolved or wrote_finalization),
        )

    def execute(
        self,
        *,
        cycle_id: str,
        plan_id: str,
        as_of: datetime,
        executed_at: datetime,
        idempotency_key: str,
    ) -> OperatingCycleResult:
        retained_plan = self._repository.plan(plan_id)
        if retained_plan is None:
            raise OperatingCycleError("OPERATING_PLAN_NOT_FOUND")
        paper_enabled = effective_paper_activation(
            self._repository.paper_activation_events(plan_id)
        )
        # The retained plan itself is immutable; PAPER activation is folded from
        # the latest retained PaperActivationEvent for this exact plan_id (see
        # SetPaperActivation), never by mutating or replacing the stored plan.
        plan = replace(retained_plan, paper_eligible=paper_enabled)
        occupied = self._repository.cycle_for_plan_as_of(plan_id, as_of)
        if occupied is not None:
            if occupied.cycle_id != cycle_id:
                raise OperatingCycleError("OPERATING_PLAN_AS_OF_CONFLICT")
            if self._repository.cycle_idempotency_key(cycle_id) != idempotency_key:
                raise OperatingCycleError("OPERATING_CYCLE_IDENTITY_CONFLICT")
            exact = self._repository.cycle(cycle_id)
            if exact is None:
                raise OperatingCycleError("OPERATING_CYCLE_LINEAGE_MISSING")
            return self._resume_cycle(exact, plan, executed_at)
        # Historical Evidence Gap Recovery v1: a *new* production cycle for a
        # past UTC day would stamp present-day retained evidence onto that day
        # and masquerade as historical truth. Replay of a retained cycle (above)
        # is exempt; recovering missed intervals is the evidence-recovery
        # task's job, never a backdated cycle.
        try:
            assert_backdated_execution_allowed(
                as_of=as_of, executed_at=executed_at, cycle_already_retained=False
            )
        except EvidenceRecoveryError as error:
            raise OperatingCycleError(str(error)) from error
        retained = self._state_source.snapshot(as_of=as_of, evaluated_at=executed_at)
        due = plan_due_work(
            plan=plan,
            cycle_id=cycle_id,
            as_of=as_of,
            retained=retained,
        )
        prior_cycles = self._repository.cycles()
        cycle = OperatingCycleRun.create(
            cycle_id=cycle_id,
            plan_id=plan_id,
            as_of=as_of,
            due_work=due,
            prior_cycle_id=prior_cycles[-1].cycle_id if prior_cycles else None,
            created_at=executed_at,
        )
        replay = self._repository.save_cycle(
            idempotency_key=idempotency_key,
            cycle=cycle,
            due_work=due,
        )
        if replay:
            return self._retained_result(cycle)
        self._repository.save_cycle_event(
            OperatingCycleEvent.create(
                cycle_id=cycle_id,
                ordinal=1,
                state=OperatingCycleState.PLANNED,
                occurred_at=executed_at,
            )
        )
        controls = self._repository.control_events(plan_id)
        control_state = controls[-1].state if controls else OperatingControlState.ENABLED
        if not plan.enabled or control_state is OperatingControlState.PAUSED:
            self._repository.save_cycle_event(
                OperatingCycleEvent.create(
                    cycle_id=cycle_id,
                    ordinal=2,
                    state=OperatingCycleState.SKIPPED_PAUSED,
                    occurred_at=executed_at,
                    warnings=("OPERATING_CYCLE_PAUSED",),
                )
            )
            health = build_operating_health(
                cycle_id=cycle_id,
                as_of=as_of,
                retained=retained,
                task_results=(),
                control_state=OperatingControlState.PAUSED,
                paper_eligible=plan.paper_eligible,
            )
            self._repository.save_health(health)
            return OperatingCycleResult(
                cycle,
                due,
                OperatingCycleState.SKIPPED_PAUSED,
                (),
                None,
                health,
                False,
            )
        self._repository.save_cycle_event(
            OperatingCycleEvent.create(
                cycle_id=cycle_id,
                ordinal=2,
                state=OperatingCycleState.RUNNING,
                occurred_at=executed_at,
            )
        )
        results: list[OperatingTaskResult] = []
        by_type: dict[str, OperatingTaskResult] = {}
        for task in due.tasks:
            prior = self._repository.task_result(f"{cycle_id}-{task.ordinal}-task-result")
            if prior is not None:
                results.append(prior)
                by_type[task.task_type.value] = prior
                continue
            if options_capture_awaits_same_day_reevaluation(task):
                # Genuinely time-sensitive: today's capture window has not
                # opened yet. Do not freeze a NOT_DUE result for the rest of
                # the day -- leave this one task unresolved so
                # ``_resume_cycle`` can reevaluate and execute it once the
                # window genuinely opens, later this same UTC day (Recurring
                # Operating Health & Unattended Evidence Closure v1). Every
                # other disposition for this task type -- and every other
                # task type entirely -- is unaffected and still resolved
                # exactly once, immediately, below.
                continue
            execution = self._dispatch_task(
                task=task,
                cycle_id=cycle_id,
                plan=plan,
                as_of=as_of,
                executed_at=executed_at,
                by_type=by_type,
            )
            if execution.awaits_intraday_continuation:
                # Counterfactual Follow-Up Backlog Throughput v1: this
                # dispatch already did real, permanently-committed bounded
                # work (new observations, cursor advancement -- see
                # ``TaskExecution.awaits_intraday_continuation``'s own
                # docstring) and genuinely filled its own attempt budget.
                # Leave this one task unresolved -- exactly like a
                # not-yet-due ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE`` above --
                # so a later same-day tick's ``_resume_cycle`` can dispatch
                # one more bounded round instead of freezing today's result
                # at this round's count. Every other task type is
                # unaffected: this field defaults ``False`` everywhere else.
                continue
            result = OperatingTaskResult.create(
                cycle_id=cycle_id,
                task=task,
                execution=execution,
                started_at=executed_at,
                completed_at=executed_at,
            )
            self._repository.save_task_result(result)
            results.append(result)
            by_type[task.task_type.value] = result
        final_state = terminal_cycle_state(tuple(results))
        self._repository.save_cycle_event(
            OperatingCycleEvent.create(
                cycle_id=cycle_id,
                ordinal=3,
                state=final_state,
                occurred_at=executed_at,
                warnings=tuple(
                    sorted({warning for item in results for warning in item.warning_codes})
                ),
                failures=tuple(item.error_code for item in results if item.error_code is not None),
            )
        )
        final_retained = self._state_source.snapshot(as_of=as_of, evaluated_at=executed_at)
        total_market_state, opportunity_counts, unallocated, score_changes = (
            self._state_source.brief_inputs()
        )
        brief_result = next(
            (
                item
                for item in results
                if item.task_type.value == "OPERATING_BRIEF_BUILD"
                and item.status is OperatingTaskStatus.COMPLETED
            ),
            None,
        )
        brief = None
        if brief_result is not None:
            brief = build_operating_brief(
                cycle_id=cycle_id,
                as_of=as_of,
                retained=final_retained,
                task_results=tuple(results),
                total_market_state=total_market_state,
                opportunity_counts=opportunity_counts,
                unallocated_cash=unallocated,
                largest_crypto_score_changes=score_changes,
            )
            self._repository.save_brief(brief)
        health = build_operating_health(
            cycle_id=cycle_id,
            as_of=as_of,
            retained=final_retained,
            task_results=tuple(results),
            control_state=control_state,
            paper_eligible=plan.paper_eligible,
        )
        self._repository.save_health(health)
        return OperatingCycleResult(
            cycle,
            due,
            final_state,
            tuple(results),
            brief,
            health,
            False,
        )


@dataclass(frozen=True, slots=True)
class OperatingCycleAlertCandidate:
    """One condition a completed operating cycle genuinely warrants surfacing
    through the existing operational alert outbox for. Deliberately narrow:
    an ordinary ``NOT_DUE``, or an ``UNAVAILABLE`` for an already-explained,
    expected reason (no mandate authored yet, no provider capability),
    is not a candidate here -- only a genuine task ``FAILED``, a capture
    window that closed without ever running, or health already showing
    repeated cross-cycle failures ever produces one."""

    category: str
    severity: AlertSeverity
    message: str
    source_evidence_ids: tuple[str, ...]


def derive_operating_cycle_alert_candidates(
    result: OperatingCycleResult,
) -> tuple[OperatingCycleAlertCandidate, ...]:
    """Pure decision over one already-completed ``OperatingCycleResult``:
    which alerts, if any, should be delivered. Never performs I/O and never
    delivers anything itself -- callers compose this with the existing
    ``DeliverOperationalAlert`` application service. Kept intentionally
    separate from ``ExecuteOperatingCycle`` so it can run against a fully
    replayed (``idempotent_replay=True``) result too without re-deriving
    anything from a live database."""

    candidates: list[OperatingCycleAlertCandidate] = []
    failed = tuple(item for item in result.tasks if item.status is OperatingTaskStatus.FAILED)
    if failed:
        candidates.append(
            OperatingCycleAlertCandidate(
                category="OPERATING_TASK_FAILED",
                severity=AlertSeverity.HIGH,
                message=(
                    f"Cycle {result.cycle.cycle_id} recorded {len(failed)} FAILED task(s): "
                    + ", ".join(sorted(item.task_type.value for item in failed))
                    + "."
                ),
                source_evidence_ids=tuple(sorted(item.task_result_id for item in failed)),
            )
        )
    # ``MISSED_CAPTURE_WINDOW`` is a planning-time ``DueTask`` reason, not an
    # executed ``OperatingTaskResult`` warning code: a NOT_DUE task never
    # reaches the executor, so its warnings are always empty and the reason
    # only survives on the immutable ``due_work`` plan this cycle already
    # retains -- read it from there rather than a nonexistent task-result field.
    missed_capture = tuple(
        task
        for task in result.due_work.tasks
        if task.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE
        and "MISSED_CAPTURE_WINDOW" in task.reasons
    )
    if missed_capture:
        missed_evidence = tuple(
            sorted({value for item in missed_capture for value in item.source_evidence_ids})
        )
        candidates.append(
            OperatingCycleAlertCandidate(
                category="OPTIONS_CAPTURE_WINDOW_MISSED",
                severity=AlertSeverity.WARNING,
                message=(
                    f"Cycle {result.cycle.cycle_id} closed a required options-research capture "
                    "window without a genuine capture: " + ", ".join(missed_evidence) + "."
                ),
                source_evidence_ids=missed_evidence,
            )
        )
    if result.health.repeated_failures > 0:
        candidates.append(
            OperatingCycleAlertCandidate(
                category="REPEATED_CYCLE_FAILURE",
                severity=AlertSeverity.CRITICAL,
                message=(
                    f"{result.health.repeated_failures} repeated cycle failure(s) retained as of "
                    f"cycle {result.cycle.cycle_id}."
                ),
                source_evidence_ids=(result.health.health_id,),
            )
        )
    return tuple(candidates)
