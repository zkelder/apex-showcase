"""Pure rules for the bounded, externally invoked Apex operating cycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from apex.domain.values import content_digest

APEX_OPERATING_CYCLE_PLAN_V1 = "APEX_OPERATING_CYCLE_PLAN_V1"
APEX_OPERATING_DUE_WORK_V1 = "APEX_OPERATING_DUE_WORK_V1"
APEX_OPERATING_CYCLE_RUN_V1 = "APEX_OPERATING_CYCLE_RUN_V1"
APEX_OPERATING_TASK_RESULT_V1 = "APEX_OPERATING_TASK_RESULT_V1"
APEX_OPERATING_BRIEF_V1 = "APEX_OPERATING_BRIEF_V1"
APEX_OPERATING_HEALTH_V1 = "APEX_OPERATING_HEALTH_V1"
APEX_OPERATING_CONTROL_V1 = "APEX_OPERATING_CONTROL_V1"
APEX_OPERATING_PAPER_ACTIVATION_V1 = "APEX_OPERATING_PAPER_ACTIVATION_V1"
OPERATING_WARNING = "APEX OPERATING CYCLE — RESEARCH COORDINATION ONLY — AUTHORITY NONE"
TASK_ORDER = (
    "CRYPTO_UNIVERSE_REFRESH",
    "CRYPTO_MARKET_PULSE",
    "TOTAL_MARKET_CONTEXT_REFRESH",
    "KALSHI_MARKET_REFRESH",
    "OPTIONS_RESEARCH_EVIDENCE_CAPTURE",
    "SHARED_OPPORTUNITY_REBUILD",
    "CONTEXT_ENRICHMENT_REBUILD",
    "RESEARCH_ALLOCATION_REBUILD",
    "CRYPTO_PAPER_TRIAL_EXECUTION",
    "KALSHI_PAPER_TRIAL_EXECUTION",
    "STRATEGY_PAPER_TRIAL_EXECUTION",
    "STRATEGY_TRIAL_OUTCOME_ASSESSMENT",
    "PORTFOLIO_SNAPSHOT_REFRESH",
    "MANDATE_EVALUATION_REBUILD",
    "PERFORMANCE_ATTRIBUTION_REBUILD",
    "RESEARCH_OPERATIONS_REFRESH",
    "COUNTERFACTUAL_FOLLOWUP_REFRESH",
    "COUNTERFACTUAL_OUTCOME_REFRESH",
    "EVIDENCE_GAP_RECOVERY",
    "OPERATING_BRIEF_BUILD",
)


class OperatingCycleError(ValueError):
    """Raised when exact operating-cycle evidence is invalid or conflicts."""


class OperatingTaskType(StrEnum):
    CRYPTO_UNIVERSE_REFRESH = "CRYPTO_UNIVERSE_REFRESH"
    CRYPTO_MARKET_PULSE = "CRYPTO_MARKET_PULSE"
    TOTAL_MARKET_CONTEXT_REFRESH = "TOTAL_MARKET_CONTEXT_REFRESH"
    KALSHI_MARKET_REFRESH = "KALSHI_MARKET_REFRESH"
    OPTIONS_RESEARCH_EVIDENCE_CAPTURE = "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
    SHARED_OPPORTUNITY_REBUILD = "SHARED_OPPORTUNITY_REBUILD"
    CONTEXT_ENRICHMENT_REBUILD = "CONTEXT_ENRICHMENT_REBUILD"
    RESEARCH_ALLOCATION_REBUILD = "RESEARCH_ALLOCATION_REBUILD"
    CRYPTO_PAPER_TRIAL_EXECUTION = "CRYPTO_PAPER_TRIAL_EXECUTION"
    KALSHI_PAPER_TRIAL_EXECUTION = "KALSHI_PAPER_TRIAL_EXECUTION"
    STRATEGY_PAPER_TRIAL_EXECUTION = "STRATEGY_PAPER_TRIAL_EXECUTION"
    STRATEGY_TRIAL_OUTCOME_ASSESSMENT = "STRATEGY_TRIAL_OUTCOME_ASSESSMENT"
    PORTFOLIO_SNAPSHOT_REFRESH = "PORTFOLIO_SNAPSHOT_REFRESH"
    MANDATE_EVALUATION_REBUILD = "MANDATE_EVALUATION_REBUILD"
    PERFORMANCE_ATTRIBUTION_REBUILD = "PERFORMANCE_ATTRIBUTION_REBUILD"
    RESEARCH_OPERATIONS_REFRESH = "RESEARCH_OPERATIONS_REFRESH"
    COUNTERFACTUAL_FOLLOWUP_REFRESH = "COUNTERFACTUAL_FOLLOWUP_REFRESH"
    COUNTERFACTUAL_OUTCOME_REFRESH = "COUNTERFACTUAL_OUTCOME_REFRESH"
    EVIDENCE_GAP_RECOVERY = "EVIDENCE_GAP_RECOVERY"
    OPERATING_BRIEF_BUILD = "OPERATING_BRIEF_BUILD"


class DueDisposition(StrEnum):
    DUE = "DUE"
    NOT_DUE = "NOT_DUE"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


class OperatingTaskStatus(StrEnum):
    COMPLETED = "COMPLETED"
    REUSED = "REUSED"
    SKIPPED_NOT_DUE = "SKIPPED_NOT_DUE"
    SKIPPED_PAUSED = "SKIPPED_PAUSED"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class OperatingCycleState(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED_PAUSED = "SKIPPED_PAUSED"
    SKIPPED_NOT_DUE = "SKIPPED_NOT_DUE"


class OperatingControlState(StrEnum):
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"


class PaperActivationState(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class OperatingHealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    UNAVAILABLE = "UNAVAILABLE"


def _identity(value: str, reason: str, maximum: int = 300) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise OperatingCycleError(reason)
    return normalized


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None:
        raise OperatingCycleError(reason)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class OperatingCyclePlan:
    plan_id: str
    version: int
    policy_id: str
    enabled: bool
    timezone: str
    utc_boundary_hour: int
    crypto_pulse_cadence_hours: int
    crypto_universe_cadence_days: int
    total_market_cadence: str
    kalshi_cadence: str
    shared_rebuild_trigger: str
    context_rebuild_trigger: str
    allocation_rebuild_trigger: str
    performance_attribution_trigger: str
    freshness_policies: tuple[tuple[str, int], ...]
    request_bounds: tuple[tuple[str, int], ...]
    failure_behavior: tuple[str, ...]
    retry_policy: tuple[str, ...]
    crypto_pulse_plan_id: str
    actor: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    reservation_authority: str = "NONE"
    order_authority: str = "NONE"
    broker_authority: str = "NONE"
    financial_mutation_capability: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        crypto_pulse_plan_id: str,
        actor: str,
        created_at: datetime,
        enabled: bool = True,
    ) -> OperatingCyclePlan:
        values = (
            _identity(plan_id, "OPERATING_PLAN_ID_INVALID"),
            1,
            APEX_OPERATING_CYCLE_PLAN_V1,
            enabled,
            "UTC",
            0,
            24,
            7,
            "ON_NEW_COMPLETED_ELIGIBLE_MARKET_DATA_DAY",
            "ON_COMPLETED_UTC_DAY_WHEN_PUBLIC_CAPABILITY_AVAILABLE",
            "WHEN_CRYPTO_OR_KALSHI_SOURCE_FAMILY_CHANGES",
            "WHEN_SHARED_OPPORTUNITY_OR_TOTAL_MARKET_CONTEXT_CHANGES",
            "WHEN_CONTEXT_ENRICHED_OPPORTUNITY_SET_CHANGES",
            "WHEN_PAPER_ORDER_FILL_RECONCILIATION_OR_SETTLEMENT_EVIDENCE_CHANGES",
            (
                ("CRYPTO_PULSE_SECONDS", 129_600),
                ("TOTAL_MARKET_CONTEXT_SECONDS", 259_200),
                ("KALSHI_SHARED_SECONDS", 300),
            ),
            (
                ("CRYPTO_PULSE_MAX_REQUESTS", 10),
                ("CRYPTO_PULSE_MAX_BYTES", 20_000_000),
                ("CRYPTO_PULSE_MAX_RUNTIME_MILLISECONDS", 150_000),
                ("KALSHI_MAX_REQUESTS", 21),
                ("KALSHI_MAX_BYTES", 21_000_000),
                ("KALSHI_MAX_RUNTIME_MILLISECONDS", 210_000),
                ("TASK_CONCURRENCY", 1),
            ),
            (
                "SOURCE_FAILURE_BLOCKS_ONLY_DEPENDENTS",
                "INDEPENDENT_TASKS_CONTINUE",
                "PARTIAL_OUTPUTS_RETAINED",
                "CONFLICTS_FAIL_CLOSED",
            ),
            (
                "ZERO_AUTOMATIC_PROVIDER_RETRIES",
                "EXACT_OPERATOR_INITIATED_CYCLE_REPLAY_ONLY",
                "COMPLETED_EXACT_TASKS_REUSED",
            ),
            _identity(crypto_pulse_plan_id, "OPERATING_CRYPTO_PLAN_ID_INVALID"),
            _identity(actor, "OPERATING_PLAN_ACTOR_INVALID"),
            _time(created_at, "OPERATING_PLAN_TIME_INVALID"),
            "NONE",
            False,
            False,
            "NONE",
            "NONE",
            "NONE",
            "NONE",
            "NONE",
        )
        return cls(*values[:-8], content_digest(values), *values[-8:])


@dataclass(frozen=True, slots=True)
class OperatingControlEvent:
    event_id: str
    plan_id: str
    ordinal: int
    state: OperatingControlState
    reason: str
    actor: str
    occurred_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        plan_id: str,
        ordinal: int,
        state: OperatingControlState,
        reason: str,
        actor: str,
        occurred_at: datetime,
    ) -> OperatingControlEvent:
        if ordinal <= 0:
            raise OperatingCycleError("OPERATING_CONTROL_ORDINAL_INVALID")
        values = (
            _identity(event_id, "OPERATING_CONTROL_ID_INVALID"),
            _identity(plan_id, "OPERATING_PLAN_ID_INVALID"),
            ordinal,
            state,
            _identity(reason, "OPERATING_CONTROL_REASON_INVALID"),
            _identity(actor, "OPERATING_CONTROL_ACTOR_INVALID"),
            _time(occurred_at, "OPERATING_CONTROL_TIME_INVALID"),
            "NONE",
        )
        return cls(*values[:-1], content_digest((APEX_OPERATING_CONTROL_V1, *values)), "NONE")


@dataclass(frozen=True, slots=True)
class PaperActivationEvent:
    """An explicit, auditable, append-only operator decision to enable or
    disable internal PAPER consideration on one exact retained
    ``OperatingCyclePlan``.  Mirrors ``OperatingControlEvent`` exactly: the
    immutable plan itself is never mutated or replaced; the *effective*
    PAPER-activation state for a cycle is folded from the latest retained
    event for that plan_id (defaulting to DISABLED when none exists), the same
    way ``OperatingControlState`` is already folded for pause/resume.  This
    stays entirely separate from LIVE authority and grants no provider-write
    permission of any kind."""

    event_id: str
    plan_id: str
    ordinal: int
    state: PaperActivationState
    reason: str
    actor: str
    occurred_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        plan_id: str,
        ordinal: int,
        state: PaperActivationState,
        reason: str,
        actor: str,
        occurred_at: datetime,
    ) -> PaperActivationEvent:
        if ordinal <= 0:
            raise OperatingCycleError("OPERATING_PAPER_ACTIVATION_ORDINAL_INVALID")
        values = (
            _identity(event_id, "OPERATING_PAPER_ACTIVATION_ID_INVALID"),
            _identity(plan_id, "OPERATING_PLAN_ID_INVALID"),
            ordinal,
            state,
            _identity(reason, "OPERATING_PAPER_ACTIVATION_REASON_INVALID"),
            _identity(actor, "OPERATING_PAPER_ACTIVATION_ACTOR_INVALID"),
            _time(occurred_at, "OPERATING_PAPER_ACTIVATION_TIME_INVALID"),
            "NONE",
        )
        return cls(
            *values[:-1], content_digest((APEX_OPERATING_PAPER_ACTIVATION_V1, *values)), "NONE"
        )


def effective_paper_activation(events: tuple[PaperActivationEvent, ...]) -> bool:
    """Fold the latest retained PaperActivationEvent for one plan into the
    effective PAPER-consideration flag for the next cycle.  Absent any
    explicit operator activation, PAPER consideration stays disabled -- this
    never defaults to enabled."""

    return bool(events) and events[-1].state is PaperActivationState.ENABLED


@dataclass(frozen=True, slots=True)
class RetainedOperatingState:
    captured_at: datetime
    crypto_universe_id: str | None = None
    crypto_universe_hash: str | None = None
    crypto_universe_created_at: datetime | None = None
    crypto_universe_refresh_supported: bool = False
    crypto_pulse_id: str | None = None
    crypto_pulse_hash: str | None = None
    crypto_pulse_as_of: datetime | None = None
    total_market_snapshot_id: str | None = None
    total_market_hash: str | None = None
    total_market_source_batch_id: str | None = None
    securities_batch_id: str | None = None
    securities_batch_hash: str | None = None
    securities_last_period_end: datetime | None = None
    kalshi_evaluation_ids: tuple[str, ...] = ()
    kalshi_evidence_hash: str | None = None
    kalshi_latest_at: datetime | None = None
    kalshi_refresh_supported: bool = False
    # Genuine calendar-plus-freshness due state for the options-research
    # evidence-capture task, pre-resolved by ``PostgresOperatingStateSource``
    # (see ``domain.options_research_operations.
    # determine_options_capture_due_state``) and simply mapped to the
    # shared ``DueDisposition`` vocabulary below -- unlike crypto/kalshi's
    # single-scalar freshness comparisons, options due-ness genuinely
    # depends on a bounded exchange-calendar session window, not one
    # retained timestamp, so the decision itself is computed once by the
    # already independently-tested pure domain function rather than
    # re-derived inline here.
    options_capture_plan_id: str | None = None
    options_capture_due_status: str | None = None
    options_capture_session_id: str | None = None
    options_capture_window_open_at: datetime | None = None
    options_capture_window_close_at: datetime | None = None
    options_capture_reasons: tuple[str, ...] = ()
    # Options Capture Temporal Reevaluation Correctness v2: the earliest
    # still-eligible session's window-open instant strictly after this
    # snapshot's own ``evaluated_at`` (see ``domain.options_research_
    # operations.OptionsCaptureDueDecision.next_capture_window_open_at``),
    # threaded through unchanged so ``options_capture_due_task`` can decide
    # whether *some* future capture window -- not necessarily the one
    # ``options_capture_session_id`` itself refers to -- still falls inside
    # the current operating-cycle horizon, without this module needing to
    # know anything about sessions/calendars/windows itself.
    options_capture_next_window_open_at: datetime | None = None
    # Genuine normalized-portfolio and investment-mandate state, pre-resolved
    # by ``PostgresOperatingStateSource`` exactly like the options fields
    # above: ``active_mandate_id`` is the mandate version genuinely active
    # (``effective_at <= evaluated_at``) for the default lineage, ``None``
    # whenever no version has ever been authored *or* none is effective yet
    # -- both cases fail closed to the identical ``UNAVAILABLE`` disposition
    # in ``plan_due_work`` below, since neither is a recoverable-this-cycle
    # dependency the way a missing Shared Opportunity set is.
    portfolio_snapshot_id: str | None = None
    portfolio_snapshot_hash: str | None = None
    portfolio_snapshot_as_of: datetime | None = None
    mandate_lineage_has_any_version: bool = False
    active_mandate_id: str | None = None
    active_mandate_lineage_id: str | None = None
    active_mandate_version: int | None = None
    active_mandate_hash: str | None = None
    shared_set_id: str | None = None
    shared_set_hash: str | None = None
    shared_crypto_pulse_id: str | None = None
    shared_kalshi_evaluation_ids: tuple[str, ...] = ()
    enriched_set_id: str | None = None
    enriched_set_hash: str | None = None
    enriched_shared_set_id: str | None = None
    enriched_context_snapshot_id: str | None = None
    allocation_id: str | None = None
    allocation_hash: str | None = None
    paper_evidence_ids: tuple[str, ...] = ()
    paper_evidence_hash: str | None = None
    unattributed_paper_evidence_ids: tuple[str, ...] = ()
    research_agenda_ids: tuple[str, ...] = ()
    research_proposal_awaiting_review_ids: tuple[str, ...] = ()
    research_approved_proposal_ids: tuple[str, ...] = ()
    latest_brief_as_of: datetime | None = None
    latest_successful_cycle_id: str | None = None
    incomplete_cycle_count: int = 0
    repeated_failure_count: int = 0
    schema_revision: str | None = None
    canonical_hash: str = ""

    @classmethod
    def create(cls, **values: object) -> RetainedOperatingState:
        captured = _time(values["captured_at"], "OPERATING_STATE_TIME_INVALID")  # type: ignore[arg-type]
        normalized = {**values, "captured_at": captured}
        normalized["canonical_hash"] = content_digest(
            (APEX_OPERATING_DUE_WORK_V1, tuple(sorted(normalized.items())))
        )
        return cls(**normalized)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class DueTask:
    task_type: OperatingTaskType
    ordinal: int
    disposition: DueDisposition
    reasons: tuple[str, ...]
    dependency_types: tuple[OperatingTaskType, ...]
    source_evidence_ids: tuple[str, ...]
    planned_identity: str
    network_behavior: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        task_type: OperatingTaskType,
        disposition: DueDisposition,
        reasons: tuple[str, ...],
        dependency_types: tuple[OperatingTaskType, ...],
        source_evidence_ids: tuple[str, ...],
        cycle_id: str,
        network_behavior: str,
    ) -> DueTask:
        ordinal = TASK_ORDER.index(task_type.value) + 1
        values = (
            task_type,
            ordinal,
            disposition,
            tuple(sorted(set(reasons))),
            dependency_types,
            tuple(sorted(set(source_evidence_ids))),
            f"{cycle_id}-{task_type.value.lower().replace('_', '-')}",
            network_behavior,
        )
        return cls(*values, content_digest(values))


@dataclass(frozen=True, slots=True)
class DueWorkPlan:
    due_work_id: str
    plan_id: str
    cycle_id: str
    as_of: datetime
    retained_state_hash: str
    tasks: tuple[DueTask, ...]
    dependency_order: tuple[OperatingTaskType, ...]
    canonical_hash: str
    authority: str = "NONE"


def _ids(*values: str | None) -> tuple[str, ...]:
    return tuple(value for value in values if value is not None)


# Options Capture Temporal Reevaluation Correctness v2: the one reason token
# ``options_capture_due_task`` appends -- and ``options_capture_awaits_same_
# day_reevaluation`` alone inspects -- to mean "some future capture window
# still opens before this operating cycle's own UTC-day horizon ends", as
# opposed to every other ``options_capture_reasons`` token (which describes
# *what the single most relevant session's own state is*, for an operator,
# and is never used for this decision; see the docstrings below).
FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON = "FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON"


def options_capture_due_task(
    *, cycle_id: str, as_of: datetime, retained: RetainedOperatingState
) -> DueTask:
    """Pure construction of the one ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE``
    ``DueTask`` for one exact retained state -- extracted out of
    ``plan_due_work`` (Recurring Operating Health & Unattended Evidence
    Closure v1) so ``application.operating_cycle`` can re-derive this one
    task's disposition later the same UTC day, against a freshly resolved
    ``RetainedOperatingState``, without duplicating this mapping or
    re-deriving the rest of the due-work plan.

    Options Capture Temporal Reevaluation Correctness v2: ``as_of`` is the
    day-scoped cutoff that already gives this whole cycle its identity (see
    ``plan_due_work``'s own ``cutoff`` requirement) -- never true wall-clock
    time, which is exactly why it is safe/deterministic to derive this
    cycle's own horizon end from it (start of ``as_of``'s own UTC day, plus
    one day). When ``retained.options_capture_next_window_open_at`` falls
    strictly before that horizon end, this task's current ``NOT_DUE``
    disposition -- whatever single session ``options_capture_due_status``
    itself is reporting, DUE/UNAVAILABLE excepted -- is provisional for this
    cycle: some capture window, not necessarily the one reported, can still
    genuinely become due before this cycle's own horizon closes, so
    ``FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON`` is appended alongside the
    existing reasons (which are left completely unchanged, preserving every
    existing operator-facing diagnostic)."""

    options_disposition = (
        DueDisposition.DUE
        if retained.options_capture_due_status == "DUE"
        else DueDisposition.UNAVAILABLE
        if retained.options_capture_due_status == "UNAVAILABLE"
        else DueDisposition.NOT_DUE
    )
    reasons = (
        (retained.options_capture_due_status, *retained.options_capture_reasons)
        if retained.options_capture_due_status is not None
        else ("OPTIONS_CAPTURE_DUE_STATE_NOT_YET_RESOLVED",)
    )
    if options_disposition is DueDisposition.NOT_DUE:
        horizon_end = _time(as_of, "OPERATING_CYCLE_AS_OF_INVALID").replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        next_open = retained.options_capture_next_window_open_at
        if next_open is not None and next_open < horizon_end:
            reasons = (*reasons, FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON)
    return DueTask.create(
        task_type=OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE,
        disposition=options_disposition,
        reasons=reasons,
        dependency_types=(),
        source_evidence_ids=_ids(
            retained.options_capture_plan_id, retained.options_capture_session_id
        ),
        cycle_id=cycle_id,
        network_behavior="BOUNDED_ALPACA_OPTIONS_GET_ONLY_WHEN_DUE_AND_SESSION_WINDOW_OPEN",
    )


def options_capture_awaits_same_day_reevaluation(task: DueTask) -> bool:
    """True only when the current ``NOT_DUE`` outcome is genuinely
    provisional for this operating cycle -- some capture window can still
    become due before this cycle's own UTC-day horizon closes (see
    ``options_capture_due_task``'s ``FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_
    HORIZON``). This single horizon-based check subsumes what used to be a
    narrower, hand-rolled ``"BEFORE_CAPTURE_WINDOW" in task.reasons`` special
    case: today's own not-yet-open session is itself always the earliest
    future window, so it was always a special case of "a future window
    exists within horizon" -- never a separate rule. Every genuinely
    terminal-for-this-cycle ``NOT_DUE`` reason -- no eligible session left
    before this cycle's horizon ends, whether that is because the window
    already closed (``MISSED_CAPTURE_WINDOW``) or no session evidence exists
    in range at all -- and every ``ALREADY_CAPTURED`` outcome, correctly
    never carries this reason, because a genuine XNYS calendar never places
    a second eligible session inside the same still-open cycle."""

    return (
        task.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE
        and task.disposition is DueDisposition.NOT_DUE
        and FUTURE_CAPTURE_WINDOW_WITHIN_CYCLE_HORIZON in task.reasons
    )


# Scheduling/Operational Priority for Recurring Follow-Up vs. Time-Critical
# Options Capture: how long an opportunistic COUNTERFACTUAL_FOLLOWUP_REFRESH
# cycle is allowed to make an options-capture window's *next* genuine
# evaluation wait.  Chosen to comfortably exceed several
# ``apex-operating-cycle-worker.timer`` ticks (``OnCalendar=*:0/5`` -- a
# 5-minute cadence): systemd does not queue a second concurrent firing of an
# already-active oneshot service, it silently no-ops an overlapping tick, so
# one long-running cycle can cost more than one tick's worth of delay before
# the options window is next genuinely re-evaluated.  Matches the real
# capture window's own order of magnitude (this milestone's own acceptance
# evidence: a genuine capture window is itself on this same ~15-minute
# scale) -- not an arbitrary shorter or longer figure.
COUNTERFACTUAL_FOLLOWUP_OPTIONS_GUARD_INTERVAL = timedelta(minutes=15)

COUNTERFACTUAL_FOLLOWUP_DEFERRED_FOR_OPTIONS_PRIORITY = (
    "COUNTERFACTUAL_FOLLOWUP_DEFERRED_FOR_OPTIONS_CAPTURE_PRIORITY"
)


def counterfactual_followup_deferred_for_options_priority(
    *, options_task: DueTask, next_window_open_at: datetime | None, as_of: datetime
) -> bool:
    """True when opportunistic ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` work must
    yield this cycle so a genuinely time-critical
    ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE`` window is never put at risk
    (Scheduling/Operational Priority for Recurring Follow-Up vs.
    Time-Critical Options Capture).

    ``TASK_ORDER`` already places ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE``
    (ordinal 5) strictly before ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` (ordinal
    15) -- within one cycle, options is always evaluated and dispatched
    before follow-up even starts, so same-cycle ordering is never the risk
    this guards against.  The real risk is *cross-cycle*: a follow-up
    backlog can make one ``--once`` invocation run long, and a systemd timer
    does not queue a second concurrent firing of an already-active oneshot
    service -- it silently no-ops an overlapping tick -- so a slow cycle can
    cost the options task one or more genuine re-evaluations right when a
    window is opening.  Two conditions defer follow-up, the smallest guard
    sufficient to close that gap without touching options' own cohort,
    capture window, methodology, or session-freshness semantics at all:

    * ``options_task`` is itself genuinely ``DUE`` this cycle (its capture
      window is open right now) -- this cycle's own remaining wall-clock
      budget should go to letting the process exit promptly once options and
      every task ordered before follow-up have run, not to an opportunistic
      backlog, so the *next* tick is never at risk of finding this process
      still active; or
    * the next eligible capture window (``next_window_open_at`` --
      ``RetainedOperatingState.options_capture_next_window_open_at``,
      already proven strictly after ``as_of`` by Options Capture Temporal
      Reevaluation Correctness v2) opens within
      ``COUNTERFACTUAL_FOLLOWUP_OPTIONS_GUARD_INTERVAL``.

    Never reads wall-clock time itself and never inspects the follow-up
    backlog (Part: this module deliberately does not read
    ``decision_followup_observation`` -- see the ``COUNTERFACTUAL_FOLLOWUP_
    REFRESH`` task construction below); both inputs are already-resolved,
    already-tested facts computed once per cycle, not re-derived here."""

    if options_task.disposition is DueDisposition.DUE:
        return True
    return (
        next_window_open_at is not None
        and next_window_open_at - as_of <= COUNTERFACTUAL_FOLLOWUP_OPTIONS_GUARD_INTERVAL
    )


def plan_due_work(
    *,
    plan: OperatingCyclePlan,
    cycle_id: str,
    as_of: datetime,
    retained: RetainedOperatingState,
) -> DueWorkPlan:
    cutoff = _time(as_of, "OPERATING_CYCLE_AS_OF_INVALID")
    if retained.captured_at != cutoff:
        raise OperatingCycleError("OPERATING_RETAINED_STATE_CUTOFF_MISMATCH")
    tasks: list[DueTask] = []

    universe_age_due = (
        retained.crypto_universe_created_at is None
        or cutoff - retained.crypto_universe_created_at >= timedelta(days=7)
    )
    universe_disposition = (
        DueDisposition.DUE
        if universe_age_due and retained.crypto_universe_refresh_supported
        else DueDisposition.UNAVAILABLE
        if universe_age_due
        else DueDisposition.NOT_DUE
    )
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.CRYPTO_UNIVERSE_REFRESH,
            disposition=universe_disposition,
            reasons=(
                ("SEVEN_DAY_UNIVERSE_CADENCE_DUE",)
                if universe_age_due
                else ("SEVEN_DAY_UNIVERSE_CADENCE_NOT_DUE",)
            )
            + (
                ("NO_REUSABLE_CYCLE_SAFE_UNIVERSE_REFRESH",)
                if universe_disposition is DueDisposition.UNAVAILABLE
                else ()
            ),
            dependency_types=(),
            source_evidence_ids=_ids(retained.crypto_universe_id, retained.crypto_universe_hash),
            cycle_id=cycle_id,
            network_behavior="BOUNDED_PUBLIC_COINBASE_GET_WHEN_SUPPORTED_AND_DUE",
        )
    )
    completed_day = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    pulse_due = retained.crypto_pulse_as_of is None or retained.crypto_pulse_as_of < completed_day
    pulse_disposition = (
        DueDisposition.BLOCKED
        if pulse_due and universe_disposition is DueDisposition.UNAVAILABLE
        else DueDisposition.DUE
        if pulse_due
        else DueDisposition.NOT_DUE
    )
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.CRYPTO_MARKET_PULSE,
            disposition=pulse_disposition,
            reasons=(
                ("COMPLETED_UTC_DAY_NOT_OBSERVED",)
                if pulse_due
                else ("COMPLETED_UTC_DAY_ALREADY_OBSERVED",)
            )
            + (
                ("DUE_UNIVERSE_REFRESH_UNAVAILABLE",)
                if pulse_disposition is DueDisposition.BLOCKED
                else ()
            ),
            dependency_types=(OperatingTaskType.CRYPTO_UNIVERSE_REFRESH,),
            source_evidence_ids=_ids(
                retained.crypto_universe_id,
                retained.crypto_universe_hash,
                retained.crypto_pulse_id,
                retained.crypto_pulse_hash,
            ),
            cycle_id=cycle_id,
            network_behavior="BOUNDED_PUBLIC_COINBASE_GET_ONLY_WHEN_DUE",
        )
    )
    context_due = (
        retained.securities_batch_id is not None
        and retained.securities_batch_id != retained.total_market_source_batch_id
    )
    context_disposition = (
        DueDisposition.UNAVAILABLE
        if retained.securities_batch_id is None
        else DueDisposition.DUE
        if context_due or retained.total_market_snapshot_id is None
        else DueDisposition.NOT_DUE
    )
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.TOTAL_MARKET_CONTEXT_REFRESH,
            disposition=context_disposition,
            reasons=(
                ("NO_ELIGIBLE_RETAINED_SECURITIES_BATCH",)
                if context_disposition is DueDisposition.UNAVAILABLE
                else ("NEW_COMPLETED_ELIGIBLE_MARKET_DATA_DAY",)
                if context_disposition is DueDisposition.DUE
                else ("LATEST_ELIGIBLE_MARKET_DATA_ALREADY_CONTEXTUALIZED",)
            ),
            dependency_types=(),
            source_evidence_ids=_ids(
                retained.securities_batch_id,
                retained.securities_batch_hash,
                retained.total_market_snapshot_id,
                retained.total_market_hash,
            ),
            cycle_id=cycle_id,
            network_behavior="RETAINED_SECURITIES_EVIDENCE_ONLY",
        )
    )
    kalshi_fresh = (
        retained.kalshi_latest_at is not None
        and bool(retained.kalshi_evaluation_ids)
        and cutoff - retained.kalshi_latest_at < timedelta(seconds=300)
    )
    kalshi_disposition = (
        DueDisposition.NOT_DUE
        if kalshi_fresh
        else DueDisposition.DUE
        if retained.kalshi_refresh_supported
        else DueDisposition.UNAVAILABLE
    )
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.KALSHI_MARKET_REFRESH,
            disposition=kalshi_disposition,
            reasons=(
                ("RETAINED_KALSHI_EVIDENCE_WITHIN_FIVE_MINUTE_BOUND",)
                if kalshi_disposition is DueDisposition.NOT_DUE
                else ("EXACT_BOUNDED_KALSHI_REFRESH_SUPPORTED",)
                if kalshi_disposition is DueDisposition.DUE
                else (
                    "KALSHI_PUBLIC_REFRESH_CAPABILITY_NOT_RETAINED",
                    "PAPER_AND_LIVE_EXECUTION_REMAIN_DISABLED",
                )
            ),
            dependency_types=(),
            source_evidence_ids=(
                *retained.kalshi_evaluation_ids,
                *_ids(retained.kalshi_evidence_hash),
            ),
            cycle_id=cycle_id,
            network_behavior=(
                "BOUNDED_PUBLIC_GET_ZERO_RETRIES"
                if retained.kalshi_refresh_supported
                else "ZERO_REQUESTS_WHILE_CAPABILITY_UNAVAILABLE"
            ),
        )
    )
    # Independently due, independently fail/skip (milestone: "Preserve
    # exact operating separation"): ``dependency_types=()`` below means a
    # provider/options-data failure here can never block or be blocked by
    # any crypto or Kalshi task. Only the final ``OPERATING_BRIEF_BUILD``
    # task depends on this one, via the identical "depends on every prior
    # task" rule it already applies to every other task type.
    options_task = options_capture_due_task(cycle_id=cycle_id, as_of=cutoff, retained=retained)
    tasks.append(options_task)
    shared_inputs_changed = (
        retained.shared_set_id is None
        or retained.shared_crypto_pulse_id != retained.crypto_pulse_id
        or retained.shared_kalshi_evaluation_ids != retained.kalshi_evaluation_ids
        or pulse_disposition is DueDisposition.DUE
        or kalshi_disposition is DueDisposition.DUE
    )
    shared_disposition = (
        DueDisposition.BLOCKED
        if retained.crypto_pulse_id is None
        else DueDisposition.DUE
        if shared_inputs_changed
        else DueDisposition.NOT_DUE
    )
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.SHARED_OPPORTUNITY_REBUILD,
            disposition=shared_disposition,
            reasons=(
                ("CRYPTO_PULSE_REQUIRED",)
                if shared_disposition is DueDisposition.BLOCKED
                else ("SOURCE_OPPORTUNITY_FAMILY_CHANGED_OR_DUE",)
                if shared_disposition is DueDisposition.DUE
                else ("SOURCE_OPPORTUNITY_FAMILIES_UNCHANGED",)
            ),
            dependency_types=(
                OperatingTaskType.CRYPTO_MARKET_PULSE,
                OperatingTaskType.KALSHI_MARKET_REFRESH,
            ),
            source_evidence_ids=(
                *_ids(retained.crypto_pulse_id, retained.crypto_pulse_hash),
                *retained.kalshi_evaluation_ids,
            ),
            cycle_id=cycle_id,
            network_behavior="RETAINED_EVIDENCE_ONLY",
        )
    )
    enrichment_changed = (
        retained.enriched_set_id is None
        or retained.enriched_shared_set_id != retained.shared_set_id
        or retained.enriched_context_snapshot_id != retained.total_market_snapshot_id
        or shared_disposition is DueDisposition.DUE
        or context_disposition is DueDisposition.DUE
    )
    enrichment_disposition = (
        DueDisposition.BLOCKED
        if retained.shared_set_id is None or retained.total_market_snapshot_id is None
        else DueDisposition.DUE
        if enrichment_changed
        else DueDisposition.NOT_DUE
    )
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.CONTEXT_ENRICHMENT_REBUILD,
            disposition=enrichment_disposition,
            reasons=(
                ("SHARED_SET_OR_CONTEXT_REQUIRED",)
                if enrichment_disposition is DueDisposition.BLOCKED
                else ("SHARED_SET_OR_CONTEXT_CHANGED_OR_DUE",)
                if enrichment_disposition is DueDisposition.DUE
                else ("SHARED_SET_AND_CONTEXT_UNCHANGED",)
            ),
            dependency_types=(
                OperatingTaskType.SHARED_OPPORTUNITY_REBUILD,
                OperatingTaskType.TOTAL_MARKET_CONTEXT_REFRESH,
            ),
            source_evidence_ids=_ids(
                retained.shared_set_id,
                retained.shared_set_hash,
                retained.total_market_snapshot_id,
                retained.total_market_hash,
            ),
            cycle_id=cycle_id,
            network_behavior="RETAINED_EVIDENCE_ONLY",
        )
    )
    allocation_disposition = (
        DueDisposition.BLOCKED
        if retained.shared_set_id is None or retained.total_market_snapshot_id is None
        else DueDisposition.DUE
        if enrichment_disposition is DueDisposition.DUE or retained.allocation_id is None
        else DueDisposition.NOT_DUE
    )
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.RESEARCH_ALLOCATION_REBUILD,
            disposition=allocation_disposition,
            reasons=(
                ("SHARED_SET_AND_CONTEXT_REQUIRED",)
                if allocation_disposition is DueDisposition.BLOCKED
                else ("CONTEXT_ENRICHED_SET_CHANGED_OR_ALLOCATION_MISSING",)
                if allocation_disposition is DueDisposition.DUE
                else ("CONTEXT_ENRICHED_SET_AND_ALLOCATION_UNCHANGED",)
            ),
            dependency_types=(OperatingTaskType.CONTEXT_ENRICHMENT_REBUILD,),
            source_evidence_ids=_ids(
                retained.enriched_set_id,
                retained.enriched_set_hash,
                retained.allocation_id,
                retained.allocation_hash,
            ),
            cycle_id=cycle_id,
            network_behavior="RETAINED_EVIDENCE_ONLY",
        )
    )
    paper_trial_disposition = (
        DueDisposition.BLOCKED if retained.allocation_id is None else DueDisposition.DUE
    )
    for venue_task_type, reason in (
        (OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION, "CRYPTO_PAPER_TRIAL_MANAGED_EVERY_CYCLE"),
        (OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION, "KALSHI_PAPER_TRIAL_MANAGED_EVERY_CYCLE"),
    ):
        tasks.append(
            DueTask.create(
                task_type=venue_task_type,
                disposition=paper_trial_disposition,
                reasons=(
                    ("CROSS_VENUE_ALLOCATION_REQUIRED",)
                    if paper_trial_disposition is DueDisposition.BLOCKED
                    else (reason,)
                ),
                dependency_types=(OperatingTaskType.RESEARCH_ALLOCATION_REBUILD,),
                source_evidence_ids=_ids(retained.allocation_id, retained.allocation_hash),
                cycle_id=cycle_id,
                network_behavior="RETAINED_EVIDENCE_ONLY_ZERO_PROVIDER_WRITES",
            )
        )
    # Recurring Strategy PAPER Orchestration v1: independently due every
    # cycle, exactly like PORTFOLIO_SNAPSHOT_REFRESH immediately below -- no
    # capability or dependency gate that would ever make it BLOCKED or
    # UNAVAILABLE. Its candidates are the canonical EXTERNAL benchmark
    # strategies (``domain.strategy_benchmark.canonical_strategy_
    # benchmarks``), evaluated fresh each cycle against genuinely retained
    # CRYPTO_MARKET_PULSE close history plus the canonical Live Market
    # Observation substrate -- deliberately NOT sourced from
    # SHARED_OPPORTUNITY_REBUILD/RESEARCH_ALLOCATION_REBUILD's own
    # Cross-Venue-Allocation pipeline (see the executor branch's own
    # docstring for why this stays a structurally independent, second PAPER
    # path). ``dependency_types=()`` so a genuine failure in either pipeline
    # can never block, and is never blocked by, this one -- the same
    # "independently due, independently fail/skip" contract
    # OPTIONS_RESEARCH_EVIDENCE_CAPTURE/COUNTERFACTUAL_FOLLOWUP_REFRESH
    # already established.
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION,
            disposition=DueDisposition.DUE,
            reasons=("STRATEGY_PAPER_TRIAL_MANAGED_EVERY_CYCLE",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id=cycle_id,
            network_behavior="RETAINED_EVIDENCE_ONLY_ZERO_PROVIDER_WRITES",
        )
    )
    # Strategy PAPER Trial Outcome Assessment v1: due every cycle, placed
    # immediately after the trial lifecycle it reads. Deliberately NO declared
    # dependency: it reads only immutable closure/fill/provenance/attribution
    # facts, so a STRATEGY_PAPER_TRIAL_EXECUTION failure never needs to block
    # it, and -- the property that matters -- nothing in the PAPER lifecycle
    # declares a dependency on it, so its own failure can never block, delay,
    # or alter a close, fill, reconciliation, or later trial.
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.STRATEGY_TRIAL_OUTCOME_ASSESSMENT,
            disposition=DueDisposition.DUE,
            reasons=("STRATEGY_TRIAL_OUTCOME_ASSESSMENT_MANAGED_EVERY_CYCLE",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id=cycle_id,
            network_behavior="LOCAL_RETAINED_READS_ONLY_NO_PROVIDER_REQUEST",
        )
    )
    # Portfolio-state refresh is unconditionally due every cycle (mirroring
    # the paper-trial tasks' "managed every cycle" cadence): it only ever
    # normalizes already-retained Crypto/Kalshi PAPER evidence, degrades
    # honestly to an ``UnavailablePortfolioSource`` entry when an account
    # genuinely does not exist yet, and is cheap, read-only, and idempotent
    # per exact cycle_id -- there is no capability or dependency gate that
    # would ever make it BLOCKED or UNAVAILABLE.
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.PORTFOLIO_SNAPSHOT_REFRESH,
            disposition=DueDisposition.DUE,
            reasons=("PORTFOLIO_SNAPSHOT_MANAGED_EVERY_CYCLE",),
            dependency_types=(),
            source_evidence_ids=retained.paper_evidence_ids,
            cycle_id=cycle_id,
            network_behavior="RETAINED_EVIDENCE_ONLY_ZERO_PROVIDER_REQUESTS",
        )
    )
    # Mandate evaluation genuinely depends on an active mandate version --
    # unlike a missing Shared Opportunity set (a same-cycle dependency this
    # planner already models as BLOCKED), a mandate lineage with zero
    # authored versions, or one whose earliest version is not yet
    # ``effective_at``, is not something a later task *within this cycle*
    # can ever produce; it fails closed to UNAVAILABLE exactly like a
    # missing provider capability, never to a silently-skipped NOT_DUE.
    mandate_active = retained.active_mandate_id is not None
    mandate_disposition = (
        DueDisposition.UNAVAILABLE
        if not mandate_active
        else DueDisposition.BLOCKED
        if retained.shared_set_id is None
        else DueDisposition.DUE
    )
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.MANDATE_EVALUATION_REBUILD,
            disposition=mandate_disposition,
            reasons=(
                ("MANDATE_LINEAGE_EXISTS_BUT_NOT_YET_EFFECTIVE",)
                if not mandate_active and retained.mandate_lineage_has_any_version
                else ("MANDATE_LINEAGE_NEVER_CREATED",)
                if not mandate_active
                else ("SHARED_OPPORTUNITY_SET_REQUIRED",)
                if mandate_disposition is DueDisposition.BLOCKED
                else ("MANDATE_EVALUATION_MANAGED_EVERY_CYCLE",)
            ),
            dependency_types=(OperatingTaskType.SHARED_OPPORTUNITY_REBUILD,),
            source_evidence_ids=_ids(
                retained.active_mandate_id,
                retained.active_mandate_hash,
                retained.shared_set_id,
            ),
            cycle_id=cycle_id,
            network_behavior="RETAINED_EVIDENCE_ONLY_ZERO_PROVIDER_REQUESTS",
        )
    )
    attribution_due = bool(retained.unattributed_paper_evidence_ids)
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.PERFORMANCE_ATTRIBUTION_REBUILD,
            disposition=DueDisposition.DUE if attribution_due else DueDisposition.NOT_DUE,
            reasons=(
                ("PAPER_ORDER_FILL_RECONCILIATION_OR_SETTLEMENT_EVIDENCE_CHANGED",)
                if attribution_due
                else ("PAPER_EVIDENCE_UNCHANGED_OR_ABSENT",)
            ),
            dependency_types=(),
            source_evidence_ids=(
                *retained.paper_evidence_ids,
                *_ids(retained.paper_evidence_hash),
            ),
            cycle_id=cycle_id,
            network_behavior="RETAINED_PAPER_EVIDENCE_ONLY_ZERO_PROVIDER_REQUESTS",
        )
    )
    research_inputs_available = bool(
        retained.shared_set_id or retained.allocation_id or retained.paper_evidence_ids
    )
    research_operations_due = (
        (
            research_inputs_available
            and retained.schema_revision
            in {
                "0070_operational_reliability",
                "0071_coinbase_read_only",
                "0072_kalshi_read_only",
                "0073_live_preflight",
            }
            and not retained.research_agenda_ids
        )
        or shared_disposition is DueDisposition.DUE
        or allocation_disposition is DueDisposition.DUE
        or attribution_due
    )
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.RESEARCH_OPERATIONS_REFRESH,
            disposition=(DueDisposition.DUE if research_operations_due else DueDisposition.NOT_DUE),
            reasons=(
                ("RETAINED_BLOCKER_INPUTS_CHANGED_OR_AGENDA_MISSING",)
                if research_operations_due
                else ("RESEARCH_AGENDA_INPUTS_UNCHANGED",)
            ),
            dependency_types=(
                OperatingTaskType.SHARED_OPPORTUNITY_REBUILD,
                OperatingTaskType.RESEARCH_ALLOCATION_REBUILD,
                OperatingTaskType.PERFORMANCE_ATTRIBUTION_REBUILD,
            ),
            source_evidence_ids=(
                *retained.research_agenda_ids,
                *retained.research_proposal_awaiting_review_ids,
                *retained.research_approved_proposal_ids,
                *_ids(retained.shared_set_id, retained.allocation_id),
                *retained.paper_evidence_ids,
            ),
            cycle_id=cycle_id,
            network_behavior="RETAINED_EVIDENCE_ONLY_NO_AUTOMATIC_EXPERIMENT_EXECUTION",
        )
    )
    # Counterfactual Follow-Up Recurring Integration v1: due every cycle,
    # exactly like ``PORTFOLIO_SNAPSHOT_REFRESH`` above -- no capability flag
    # gates it. Its input (already-retained ``CounterfactualDecision`` rows)
    # was, until Counterfactual Outcome Feedback v1's Phase 0, produced only
    # by a deliberately separate, CLI-triggered-only ledger sync; that sync
    # now runs first inside this same task's own executor branch (composed,
    # not a new ``OperatingTaskType``, mirroring how the evidence-quality
    # sweep below was already folded in the same way), so the recurring
    # worker keeps the ledger current without any CLI/human step.
    # ``dependency_types=()`` so a bounded Kalshi read failure here can never
    # block, and is never blocked by, any crypto, Kalshi, options, or
    # mandate task's own *evidence* -- the same "independently due,
    # independently fail/skip" contract ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE``
    # already established. The executor bounds its own genuinely-attempted
    # work and Kalshi provider-request budget per cycle; this planning layer
    # only ever says "some genuinely due backlog may exist, go look" -- it
    # never counts or reasons about the backlog itself, matching how this
    # module deliberately does not read ``decision_followup_observation``.
    #
    # Scheduling/Operational Priority for Recurring Follow-Up vs.
    # Time-Critical Options Capture: evidence-level independence from
    # ``options_task`` (above) is not the same thing as *scheduling-priority*
    # independence, and this one axis is deliberately not independent --
    # ``counterfactual_followup_deferred_for_options_priority`` reports
    # ``NOT_DUE`` (never ``BLOCKED``: nothing is actually wrong, opportunistic
    # work is simply yielding) whenever the options capture window is open
    # right now or opens imminently, so a follow-up backlog can never make
    # the cycle run long enough to cost a genuine options-window
    # re-evaluation on a later timer tick (see that function's own docstring
    # for the full mechanism).
    followup_deferred_for_options = counterfactual_followup_deferred_for_options_priority(
        options_task=options_task,
        next_window_open_at=retained.options_capture_next_window_open_at,
        as_of=cutoff,
    )
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH,
            disposition=(
                DueDisposition.NOT_DUE if followup_deferred_for_options else DueDisposition.DUE
            ),
            reasons=(
                (COUNTERFACTUAL_FOLLOWUP_DEFERRED_FOR_OPTIONS_PRIORITY,)
                if followup_deferred_for_options
                else ("COUNTERFACTUAL_FOLLOWUP_MANAGED_EVERY_CYCLE",)
            ),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id=cycle_id,
            network_behavior=("LOCAL_RETAINED_CRYPTO_READS_PLUS_BOUNDED_KALSHI_GETS_WHEN_DUE"),
        )
    )
    # Counterfactual Outcome Feedback v1: due every cycle, exactly like
    # ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` immediately above -- no capability
    # flag gates it, and (like that task) this planning layer deliberately
    # does not read ``counterfactual_outcome_aggregate``/``decision_
    # followup_observation`` itself, so ``source_evidence_ids`` stays empty
    # here too. Unlike follow-up sync, this task declares a genuine
    # same-cycle dependency on it (``dependency_types``): the aggregation
    # this task performs consumes exactly the decisions/classifications/
    # observations/assessments that task's own executor branch keeps
    # current (ledger sync, classification sync, follow-up sync, and
    # evidence-quality assessment are all composed inside that one
    # branch -- see its own comment) -- if that upstream task genuinely
    # FAILED or was BLOCKED this cycle, aggregating now would only recompute
    # over the same already-stale evidence, so this task is blocked instead
    # (``application.operating_cycle._dispatch_task`` already implements
    # "blocked if a declared dependency's persisted result is FAILED/
    # BLOCKED" generically; no new mechanism was added for this).
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH,
            disposition=DueDisposition.DUE,
            reasons=("COUNTERFACTUAL_OUTCOME_MANAGED_EVERY_CYCLE",),
            dependency_types=(OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH,),
            source_evidence_ids=(),
            cycle_id=cycle_id,
            network_behavior="LOCAL_RETAINED_READS_ONLY_NO_PROVIDER_REQUEST",
        )
    )
    # Historical Evidence Gap Recovery v1: independently due every cycle, with
    # NO declared dependency in either direction. It detects what Apex failed
    # to retain while it was off, recovers only daily securities bars and daily
    # crypto candles from the provider's historical record, and records
    # everything else IRRECOVERABLE. It runs after every live-capture and PAPER
    # task so it can never delay them, and nothing in the PAPER lifecycle reads
    # it. Zero provider writes; authority NONE.
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.EVIDENCE_GAP_RECOVERY,
            disposition=DueDisposition.DUE,
            reasons=("EVIDENCE_GAP_RECOVERY_MANAGED_EVERY_CYCLE",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id=cycle_id,
            network_behavior="BOUNDED_PUBLIC_HISTORICAL_READ_ONLY_GETS",
        )
    )
    brief_due = retained.latest_brief_as_of is None or retained.latest_brief_as_of < cutoff
    tasks.append(
        DueTask.create(
            task_type=OperatingTaskType.OPERATING_BRIEF_BUILD,
            disposition=DueDisposition.DUE if brief_due else DueDisposition.NOT_DUE,
            reasons=(
                ("NO_BRIEF_FOR_EXACT_AS_OF",)
                if brief_due
                else ("EXACT_AS_OF_BRIEF_ALREADY_RETAINED",)
            ),
            dependency_types=tuple(OperatingTaskType(value) for value in TASK_ORDER[:-1]),
            source_evidence_ids=_ids(
                retained.crypto_universe_id,
                retained.crypto_pulse_id,
                retained.total_market_snapshot_id,
                retained.shared_set_id,
                retained.enriched_set_id,
                retained.allocation_id,
                *retained.paper_evidence_ids,
            ),
            cycle_id=cycle_id,
            network_behavior="RETAINED_EVIDENCE_ONLY",
        )
    )
    ordered = tuple(tasks)
    values = (
        f"{cycle_id}-due-work-v1",
        plan.plan_id,
        _identity(cycle_id, "OPERATING_CYCLE_ID_INVALID", 240),
        cutoff,
        retained.canonical_hash,
        ordered,
        tuple(OperatingTaskType(value) for value in TASK_ORDER),
        "NONE",
    )
    return DueWorkPlan(*values[:-1], content_digest(values), "NONE")


@dataclass(frozen=True, slots=True)
class OperatingCycleRun:
    cycle_id: str
    plan_id: str
    as_of: datetime
    due_work_id: str
    due_work_hash: str
    prior_cycle_id: str | None
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        cycle_id: str,
        plan_id: str,
        as_of: datetime,
        due_work: DueWorkPlan,
        prior_cycle_id: str | None,
        created_at: datetime,
    ) -> OperatingCycleRun:
        values = (
            _identity(cycle_id, "OPERATING_CYCLE_ID_INVALID", 240),
            _identity(plan_id, "OPERATING_PLAN_ID_INVALID"),
            _time(as_of, "OPERATING_CYCLE_AS_OF_INVALID"),
            due_work.due_work_id,
            due_work.canonical_hash,
            prior_cycle_id,
            _time(created_at, "OPERATING_CYCLE_CREATED_AT_INVALID"),
            "NONE",
        )
        return cls(*values[:-1], content_digest((APEX_OPERATING_CYCLE_RUN_V1, *values)), "NONE")


@dataclass(frozen=True, slots=True)
class OperatingCycleEvent:
    event_id: str
    cycle_id: str
    ordinal: int
    state: OperatingCycleState
    occurred_at: datetime
    warnings: tuple[str, ...]
    failures: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        cycle_id: str,
        ordinal: int,
        state: OperatingCycleState,
        occurred_at: datetime,
        warnings: tuple[str, ...] = (),
        failures: tuple[str, ...] = (),
    ) -> OperatingCycleEvent:
        values = (
            f"{cycle_id}-event-{ordinal}",
            cycle_id,
            ordinal,
            state,
            _time(occurred_at, "OPERATING_CYCLE_EVENT_TIME_INVALID"),
            tuple(sorted(set(warnings))),
            tuple(sorted(set(failures))),
            "NONE",
        )
        return cls(*values[:-1], content_digest(values), "NONE")


@dataclass(frozen=True, slots=True)
class TaskExecution:
    status: OperatingTaskStatus
    output_identities: tuple[str, ...]
    reused_identities: tuple[str, ...]
    changed: bool
    network_requests: int
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    retryable: bool = False
    # Counterfactual Follow-Up Backlog Throughput v1: True exactly when this
    # task's own bounded per-invocation work (``COUNTERFACTUAL_FOLLOWUP_
    # REFRESH``) genuinely filled its own attempt budget -- a signal that
    # more genuinely due work likely exists right now, so
    # ``application.operating_cycle.ExecuteOperatingCycle`` should defer
    # saving a terminal ``OperatingTaskResult`` for this task this cycle_id
    # (UTC calendar day) and leave it eligible for one more bounded round on
    # a later same-day tick, exactly mirroring how
    # ``options_capture_awaits_same_day_reevaluation`` already lets
    # ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE`` stay unresolved until its own
    # capture window genuinely opens -- except here the task has already
    # done real, permanently-committed work this tick (new observations,
    # cursor advancement) and is only deferring its own *audit* row, never
    # re-attempting or duplicating anything (Part: ``SyncFollowUpObservations``'s
    # own idempotent-replay/durable-cursor contract, completely unchanged,
    # is what makes repeated same-day dispatch safe at all).
    #
    # Recurring Strategy PAPER Intraday Execution Correction v1: a second,
    # unconditional usage. ``STRATEGY_PAPER_TRIAL_EXECUTION`` reports this
    # ``True`` on every single dispatch, not only while draining a bounded
    # backlog -- unlike follow-up refresh above, a continuously managed open
    # PAPER position has no "caught up for today" terminal state: a later
    # same-day tick must always be able to observe fresh
    # ``LiveMarketObservation`` evidence and progress an open order's fill,
    # expiry, and attribution without waiting for the next UTC-day rollover
    # (see ``plan_due_work``'s own comment on why this task is
    # unconditionally DUE every cycle in the first place, and
    # ``adapters.operating_cycle_tasks._execute_strategy_paper_trial``'s own
    # docstring). Safe for the identical reason follow-up refresh's repeated
    # dispatch is safe: every mutation that executor branch performs (order
    # placement, fills, expiry, reconciliation, attribution) is
    # independently idempotent via its own deterministic ids -- this field
    # only ever controls whether the *scheduler* keeps calling the executor,
    # never whether the executor's own work is safe to repeat. A genuine
    # exception still reaches the shared ``_dispatch_task`` failure handler
    # unchanged, which freezes the task ``FAILED`` for the rest of the UTC
    # day exactly like any other task type -- this field is never set from
    # that path.
    #
    # Defaults ``False`` for every other task type, byte-for-byte unchanged.
    awaits_intraday_continuation: bool = False


@dataclass(frozen=True, slots=True)
class OperatingTaskResult:
    task_result_id: str
    cycle_id: str
    task_type: OperatingTaskType
    ordinal: int
    planned_identity: str
    input_identities: tuple[str, ...]
    output_identities: tuple[str, ...]
    reused_identities: tuple[str, ...]
    status: OperatingTaskStatus
    changed: bool
    retryable: bool
    network_requests: int
    warning_codes: tuple[str, ...]
    error_code: str | None
    started_at: datetime
    completed_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        cycle_id: str,
        task: DueTask,
        execution: TaskExecution,
        started_at: datetime,
        completed_at: datetime,
    ) -> OperatingTaskResult:
        values = (
            f"{cycle_id}-{task.ordinal}-task-result",
            cycle_id,
            task.task_type,
            task.ordinal,
            task.planned_identity,
            task.source_evidence_ids,
            tuple(sorted(set(execution.output_identities))),
            tuple(sorted(set(execution.reused_identities))),
            execution.status,
            execution.changed,
            execution.retryable,
            execution.network_requests,
            tuple(sorted(set(execution.warnings))),
            execution.error_code,
            _time(started_at, "OPERATING_TASK_START_TIME_INVALID"),
            _time(completed_at, "OPERATING_TASK_COMPLETION_TIME_INVALID"),
            "NONE",
        )
        return cls(*values[:-1], content_digest((APEX_OPERATING_TASK_RESULT_V1, *values)), "NONE")


@dataclass(frozen=True, slots=True)
class OperatingBrief:
    brief_id: str
    cycle_id: str
    as_of: datetime
    overall_system_state: str
    task_counts: tuple[tuple[str, int], ...]
    crypto_universe_id: str | None
    crypto_pulse_id: str | None
    total_market_context_id: str | None
    total_market_state: str | None
    kalshi_status: str
    shared_opportunity_set_id: str | None
    opportunity_counts: tuple[tuple[str, int], ...]
    allocation_id: str | None
    unallocated_cash: str | None
    largest_crypto_score_changes: tuple[str, ...]
    stale_or_unavailable: tuple[str, ...]
    warnings: tuple[str, ...]
    failures: tuple[str, ...]
    operator_attention: tuple[str, ...]
    source_state_hash: str
    canonical_hash: str
    authority: str = "NONE"


def build_operating_brief(
    *,
    cycle_id: str,
    as_of: datetime,
    retained: RetainedOperatingState,
    task_results: tuple[OperatingTaskResult, ...],
    total_market_state: str | None,
    opportunity_counts: tuple[tuple[str, int], ...],
    unallocated_cash: str | None,
    largest_crypto_score_changes: tuple[str, ...],
) -> OperatingBrief:
    counts = tuple(
        (status.value, sum(item.status is status for item in task_results))
        for status in OperatingTaskStatus
    )
    failures = tuple(item.error_code for item in task_results if item.error_code is not None)
    unavailable = tuple(
        item.task_type.value
        for item in task_results
        if item.status in {OperatingTaskStatus.UNAVAILABLE, OperatingTaskStatus.BLOCKED}
    )
    warnings = tuple(sorted({warning for item in task_results for warning in item.warning_codes}))
    attention = tuple(sorted(set((*failures, *unavailable))))
    overall = (
        "BLOCKED"
        if any(item.status is OperatingTaskStatus.FAILED for item in task_results)
        else "DEGRADED"
        if attention
        else "HEALTHY"
    )
    values = (
        f"{cycle_id}-operating-brief",
        cycle_id,
        _time(as_of, "OPERATING_BRIEF_TIME_INVALID"),
        overall,
        counts,
        retained.crypto_universe_id,
        retained.crypto_pulse_id,
        retained.total_market_snapshot_id,
        total_market_state,
        ("BOUNDED_RETAINED_EVIDENCE" if retained.kalshi_evaluation_ids else "UNAVAILABLE"),
        retained.shared_set_id,
        opportunity_counts,
        retained.allocation_id,
        unallocated_cash,
        largest_crypto_score_changes,
        unavailable,
        warnings,
        failures,
        attention,
        retained.canonical_hash,
        "NONE",
    )
    return OperatingBrief(*values[:-1], content_digest((APEX_OPERATING_BRIEF_V1, *values)), "NONE")


@dataclass(frozen=True, slots=True)
class OperatingHealth:
    health_id: str
    cycle_id: str
    as_of: datetime
    state: OperatingHealthState
    provider_capabilities: tuple[tuple[str, str], ...]
    latest_successful_refresh: datetime | None
    next_due_at: datetime | None
    stale_data: tuple[str, ...]
    incomplete_cycles: int
    repeated_failures: int
    blocked_dependencies: tuple[str, ...]
    schema_revision: str | None
    operator_control: OperatingControlState
    warnings: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    reservation_authority: str = "NONE"
    order_authority: str = "NONE"
    broker_authority: str = "NONE"
    financial_mutation_capability: str = "NONE"


def build_operating_health(
    *,
    cycle_id: str,
    as_of: datetime,
    retained: RetainedOperatingState,
    task_results: tuple[OperatingTaskResult, ...],
    control_state: OperatingControlState,
    paper_eligible: bool = False,
) -> OperatingHealth:
    blocked = tuple(
        item.task_type.value
        for item in task_results
        if item.status in {OperatingTaskStatus.BLOCKED, OperatingTaskStatus.FAILED}
    )
    unavailable = tuple(
        item.task_type.value
        for item in task_results
        if item.status is OperatingTaskStatus.UNAVAILABLE
    )
    state = (
        OperatingHealthState.PAUSED
        if control_state is OperatingControlState.PAUSED
        else OperatingHealthState.BLOCKED
        if blocked
        else OperatingHealthState.DEGRADED
        if unavailable or retained.incomplete_cycle_count or retained.repeated_failure_count
        else OperatingHealthState.HEALTHY
    )
    latest = retained.crypto_pulse_as_of
    next_due = None if latest is None else latest + timedelta(hours=24)
    values = (
        f"{cycle_id}-operating-health",
        cycle_id,
        _time(as_of, "OPERATING_HEALTH_TIME_INVALID"),
        state,
        (
            (
                "COINBASE_PUBLIC",
                "BOUNDED_GET_WHEN_DUE" if retained.crypto_universe_id else "UNAVAILABLE",
            ),
            (
                "KALSHI_PUBLIC",
                "MANUAL_EXACT_BOUNDED_ONLY",
            ),
            ("SECURITIES_CONTEXT", "RETAINED_LOCAL_ONLY"),
        ),
        latest,
        next_due,
        unavailable,
        retained.incomplete_cycle_count,
        retained.repeated_failure_count,
        blocked,
        retained.schema_revision,
        control_state,
        (OPERATING_WARNING,),
        "NONE",
        paper_eligible,
        False,
        "NONE",
        "NONE",
        "NONE",
        "NONE",
        "NONE",
    )
    return OperatingHealth(
        *values[:-8],
        content_digest((APEX_OPERATING_HEALTH_V1, *values)),
        *values[-8:],
    )


def terminal_cycle_state(
    results: tuple[OperatingTaskResult, ...],
) -> OperatingCycleState:
    if not results or all(item.status is OperatingTaskStatus.SKIPPED_NOT_DUE for item in results):
        return OperatingCycleState.SKIPPED_NOT_DUE
    failed = sum(item.status is OperatingTaskStatus.FAILED for item in results)
    completed = sum(
        item.status in {OperatingTaskStatus.COMPLETED, OperatingTaskStatus.REUSED}
        for item in results
    )
    warnings = any(
        item.status in {OperatingTaskStatus.BLOCKED, OperatingTaskStatus.UNAVAILABLE}
        or item.warning_codes
        for item in results
    )
    if failed and not completed:
        return OperatingCycleState.FAILED
    if failed:
        return OperatingCycleState.PARTIAL
    return (
        OperatingCycleState.COMPLETED_WITH_WARNINGS if warnings else OperatingCycleState.COMPLETED
    )


class PaperContinuationHealthState(StrEnum):
    """PAPER Continuation Health Signal v1.

    A first-class, deliberately never-persisted classification of "is this
    continuously-open-position PAPER task genuinely healthy right now",
    independent of whatever its most recent *terminal* ``OperatingTaskResult``
    happens to say.

    Exists because ``STRATEGY_PAPER_TRIAL_EXECUTION`` (and, identically,
    ``CRYPTO_PAPER_TRIAL_EXECUTION``/``KALSHI_PAPER_TRIAL_EXECUTION``) report
    ``TaskExecution.awaits_intraday_continuation=True`` on every successful
    dispatch (see that field's own docstring above), so
    ``application.operating_cycle.ExecuteOperatingCycle`` deliberately never
    persists a terminal ``OperatingTaskResult`` while the task keeps
    succeeding -- only a genuine exception ever does. "Most recent terminal
    ``OperatingTaskResult``" is therefore structurally a last-*failure*
    signal, not a last-*activity* signal (see
    ``docs/PAPER_OPERATIONS_OBSERVABILITY_V1.md``'s "Important caveat").
    ``compute_paper_continuation_health`` below resolves that ambiguity by
    combining the terminal-result stream with an independent,
    already-real continuation-evidence timestamp (e.g. the strategy PAPER
    account's own ``CryptoPaperReconciliation.evaluated_at``, unconditionally
    refreshed on every dispatch regardless of terminal-result persistence)
    into one authoritative status -- reusing existing evidence, adding no
    new persisted row, no migration, and never mutating history.
    """

    HEALTHY = "HEALTHY"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"
    IDLE = "IDLE"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class PaperContinuationHealth:
    """Computed fresh on every read -- deliberately no ``canonical_hash``/
    ``authority``/deterministic id: this is a projection over already-
    retained evidence, not new evidence of its own (mirrors
    ``FollowUpDueResult`` in ``domain.counterfactual_followup``, the
    closest existing precedent for an ephemeral, non-persisted
    classification result)."""

    state: PaperContinuationHealthState
    is_stale: bool
    recovered_since_failure: bool
    latest_continuation_at: datetime | None
    latest_terminal_status: OperatingTaskStatus | None
    latest_terminal_at: datetime | None
    freshness_window_seconds: int


def compute_paper_continuation_health(
    *,
    latest_terminal_status: OperatingTaskStatus | None,
    latest_terminal_at: datetime | None,
    latest_continuation_at: datetime | None,
    now: datetime,
    freshness_window: timedelta,
    has_active_work: bool | None = None,
) -> PaperContinuationHealth:
    """Pure, total, deterministic. Never performs I/O and never reads a
    clock itself -- ``now`` is always caller-supplied, matching this
    module's existing explicit-``as_of`` discipline (``b1554f7``).

    ``latest_terminal_status``/``latest_terminal_at`` come from the most
    recent *terminal* ``OperatingTaskResult`` for one ``OperatingTaskType``
    (immutable once persisted -- this function never asks for more than one,
    and never proposes writing, correcting, or superseding it: the frozen
    ``FAILED`` row this milestone is about stays exactly as it was).
    ``latest_continuation_at`` is an independent, already-real "this task
    genuinely ran and did not raise" timestamp the caller sources from that
    task's own unconditionally-refreshed evidence (for
    ``STRATEGY_PAPER_TRIAL_EXECUTION`` today: the strategy PAPER account's
    own ``max(CryptoPaperReconciliation.evaluated_at)`` -- the existing
    ``account_last_reconciled_at`` value; see
    ``application.operator_console_v1.build_operator_paper_strategy_registry``).
    ``freshness_window`` is caller-supplied so this function never invents
    its own cadence constant -- callers reuse
    ``application.operational_reliability.SupervisedOperatingRunner
    .EVALUATION_CADENCE_SECONDS`` (the same 5-minute cadence both production
    workers already share), not a new PAPER-only number. ``has_active_work``
    lets a caller who knows task-specific semantics (e.g. "does any strategy
    currently hold an open trial") distinguish actively-managing-something
    from genuinely-idle-right-now; omitted or ``True``, a fresh, never-failed
    (or recovered) continuation reads as plain ``HEALTHY``.

    Resolution, most-recent-signal-wins:

    - Neither a terminal result nor continuation evidence exists yet:
      ``UNKNOWN`` -- nothing retained to classify at all (distinct from
      ``STALE``: nothing was ever due to have happened).
    - The failure is the newest known signal (no continuation evidence, or
      continuation evidence is not newer than the failure): ``FAILED`` -- a
      genuinely current problem, regardless of how the freshness window
      compares, since nothing after the failure has proven recovery.
    - Continuation evidence is the newest known signal but falls outside
      ``freshness_window`` of ``now``: ``STALE`` -- expected continuation
      has not been observed recently enough to call current, whatever the
      frozen terminal status says either way.
    - Continuation evidence is the newest known signal and falls inside
      ``freshness_window``: ``RECOVERED`` if a ``FAILED`` terminal result
      exists anywhere in this history (preserved and still reported via
      ``latest_terminal_status``/``latest_terminal_at`` below, never
      hidden), else ``IDLE`` if the caller explicitly says there is no
      currently active work, else ``HEALTHY``.
    """

    is_failure = latest_terminal_status is OperatingTaskStatus.FAILED
    failure_at = latest_terminal_at if is_failure else None
    freshness_window_seconds = int(freshness_window.total_seconds())

    if failure_at is None and latest_continuation_at is None:
        return PaperContinuationHealth(
            state=PaperContinuationHealthState.UNKNOWN,
            is_stale=False,
            recovered_since_failure=False,
            latest_continuation_at=None,
            latest_terminal_status=latest_terminal_status,
            latest_terminal_at=latest_terminal_at,
            freshness_window_seconds=freshness_window_seconds,
        )

    continuation_is_newest = failure_at is None or (
        latest_continuation_at is not None and latest_continuation_at >= failure_at
    )
    if not continuation_is_newest:
        return PaperContinuationHealth(
            state=PaperContinuationHealthState.FAILED,
            is_stale=False,
            recovered_since_failure=False,
            latest_continuation_at=latest_continuation_at,
            latest_terminal_status=latest_terminal_status,
            latest_terminal_at=latest_terminal_at,
            freshness_window_seconds=freshness_window_seconds,
        )

    # continuation_is_newest guarantees latest_continuation_at is not None.
    assert latest_continuation_at is not None
    ever_failed = failure_at is not None
    stale = now - latest_continuation_at > freshness_window
    if stale:
        state = PaperContinuationHealthState.STALE
    elif ever_failed:
        state = PaperContinuationHealthState.RECOVERED
    elif has_active_work is False:
        state = PaperContinuationHealthState.IDLE
    else:
        state = PaperContinuationHealthState.HEALTHY

    return PaperContinuationHealth(
        state=state,
        is_stale=stale,
        recovered_since_failure=ever_failed,
        latest_continuation_at=latest_continuation_at,
        latest_terminal_status=latest_terminal_status,
        latest_terminal_at=latest_terminal_at,
        freshness_window_seconds=freshness_window_seconds,
    )
