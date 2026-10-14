"""Read-only projections for newer Apex evidence: the counterfactual
decision ledger and its versioned classification, decision follow-up
observations under FOLLOWUP_POLICY_V1/V2/V3, the options-forward research
campaign, the normalized portfolio/investment-mandate boundary, and a
cross-cycle operating-task timeline.

Sibling to ``operator_console_v1``, not a replacement for it: this module
reuses that module's filter-error type and composes the same kind of
already-retained, already-JSON-safe ``dict``/``list`` structures (produced
by ``operator_api._jsonable`` from real repository reads -- see
``operator_api.py``'s new ``/api/v1/operator-console/{decisions,followups,
options-research,portfolio-mandate,operations}`` routes for the callers).

Every function here is pure: no I/O, no provider call, no financial or
scheduling judgment of its own. It calculates no eligibility, mandate,
classification, follow-up, or due/scheduling outcome -- it only shapes,
filters, sorts, paginates, and links evidence some other, already-reviewed
domain/application module already produced. The one exception is
``evaluate_options_history_readiness``, imported and called directly from
``domain.options_research_operations`` rather than re-derived here, for the
identical reason: reuse the authoritative pure function, never restate its
threshold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from apex.application.operator_console_v1 import OperatorConsoleFilterError
from apex.domain.options_research_operations import evaluate_options_history_readiness
from apex.domain.shared_opportunity import SharedVenue

type JsonMap = Mapping[str, object]

# SECURITIES Operator Console Backend Read-Model Closure v1: derived from the
# canonical SharedVenue enum, same as operator_console_v1's own _VENUES (kept
# as a separate, identically-derived constant rather than a cross-module
# import of a private name -- both are anchored to the one real enum, so they
# cannot drift relative to each other or to the actual domain vocabulary).
_VENUES = frozenset({"ALL", *(member.value for member in SharedVenue)})
_DISPOSITIONS = frozenset({"ALL", "ADVANCED", "CONSTRAINED", "NOT_ADVANCED"})
_HORIZON_KINDS = frozenset({"ALL", "SHORT_HORIZON", "STRATEGY_HORIZON"})
_BASELINE_KINDS = frozenset({"ALL", "ORIGIN_EVIDENCE", "DECISION_TIME"})
_FOLLOWUP_STATUSES = frozenset(
    {
        "ALL",
        "OBSERVED",
        "DUE",
        "NOT_YET_DUE",
        "UNSUPPORTED",
        "BLOCKED_UNAVAILABLE",
        # Counterfactual Follow-Up Starvation Fix v1: a CRYPTO baseline
        # proven (zero provider cost) to never be able to produce genuine
        # evidence -- see ``domain.counterfactual_followup.FollowUpDueState.
        # PERMANENTLY_UNAVAILABLE``. Distinct from ``BLOCKED_UNAVAILABLE``
        # (a transient, still-retryable read failure) so a filter can never
        # conflate the two.
        "PERMANENTLY_UNAVAILABLE",
    }
)
_TASK_STATUSES = frozenset(
    {
        "ALL",
        "COMPLETED",
        "REUSED",
        "SKIPPED_NOT_DUE",
        "SKIPPED_PAUSED",
        "BLOCKED",
        "UNAVAILABLE",
        "FAILED",
    }
)


def _mapping(value: object) -> JsonMap:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _state_counts_map(value: Sequence[object]) -> dict[str, int]:
    """Defensively rebuilds a ``{state: count}`` mapping from the
    already-jsonable ``[[state, count], ...]`` pair sequence
    ``FollowUpPlan.state_counts`` serializes to -- a malformed or
    non-numeric pair is simply skipped, never raised, consistent with every
    other defensive read in this module."""

    counts: dict[str, int] = {}
    for item in value:
        pair = _sequence(item)
        if len(pair) != 2:
            continue
        key = _text(pair[0])
        count = _number(pair[1])
        if key is not None and count is not None:
            counts[key] = count
    return counts


def _href(path: str, identity: object) -> str | None:
    value = _text(identity)
    return None if not value else f"{path}/{value}"


def _paginate(
    items: Sequence[JsonMap], *, limit: int, offset: int
) -> tuple[Sequence[JsonMap], int]:
    if not 1 <= limit <= 100 or offset < 0:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_PAGINATION_INVALID")
    return items[offset : offset + limit], len(items)


# ==========================================================================
# Decision Ledger (CounterfactualDecision + CounterfactualDecisionClassification)
# ==========================================================================


def _decision_row(
    decision: JsonMap,
    *,
    classifications: Sequence[JsonMap],
    has_followup: bool,
    current_classifier_version: str,
) -> dict[str, object]:
    decision_id = decision.get("decision_id")
    ordered = tuple(
        sorted(classifications, key=lambda item: _text(item.get("classified_at")) or "")
    )
    current = next(
        (item for item in ordered if item.get("classifier_version") == current_classifier_version),
        None,
    )
    return {
        "decision_id": decision_id,
        "origin": decision.get("origin"),
        "origin_decision_id": decision.get("origin_decision_id"),
        "subject_projection_id": decision.get("subject_projection_id"),
        "subject_source_record_type": decision.get("subject_source_record_type"),
        "subject_source_record_id": decision.get("subject_source_record_id"),
        "venue": decision.get("venue"),
        "opportunity_set_id": decision.get("opportunity_set_id"),
        "mandate_id": decision.get("mandate_id"),
        "mandate_version": decision.get("mandate_version"),
        "portfolio_snapshot_id": decision.get("portfolio_snapshot_id"),
        "evaluated_at": decision.get("evaluated_at"),
        "disposition": decision.get("disposition"),
        "raw_reason_codes": decision.get("raw_reason_codes", ()),
        "explanation": decision.get("explanation"),
        "classifications": tuple(
            {
                "classifier_version": item.get("classifier_version"),
                "reason_families": item.get("reason_families", ()),
                "classified_at": item.get("classified_at"),
            }
            for item in ordered
        ),
        "current_classifier_version": current_classifier_version,
        "current_reason_families": (current or {}).get("reason_families", ()),
        "has_current_classification": current is not None,
        "has_followup": has_followup,
        "authority": decision.get("authority", "NONE"),
        "canonical_hash": decision.get("canonical_hash"),
        "evidence_href": _href("/api/v1/operator-console/decisions", decision_id),
    }


def build_operator_decisions(
    decisions: Sequence[JsonMap],
    *,
    classifications_by_decision: Mapping[str, Sequence[JsonMap]],
    followup_decision_ids: Sequence[str],
    current_classifier_version: str,
    known_classifier_versions: Sequence[str],
    venue: str = "ALL",
    disposition: str = "ALL",
    query: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, object]:
    """Bounded, filterable view over the stable CounterfactualDecision
    ledger. Never reorders by anything other than ``evaluated_at`` (the
    ledger's own retained order) and never folds a classification into the
    decision's own identity, hash, or displayed disposition -- the stable
    decision event and its interpretation stay visually distinct fields on
    the same row, never merged into one value."""

    venue_filter = venue.upper()
    disposition_filter = disposition.upper()
    if venue_filter not in _VENUES:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_VENUE_FILTER_INVALID")
    if disposition_filter not in _DISPOSITIONS:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_DISPOSITION_FILTER_INVALID")
    normalized_query = (query or "").strip().casefold()
    if len(normalized_query) > 160:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_QUERY_TOO_LONG")

    followup_ids = frozenset(followup_decision_ids)
    rows: list[dict[str, object]] = []
    for item in decisions:
        decision = _mapping(item)
        decision_id = str(decision.get("decision_id"))
        rows.append(
            _decision_row(
                decision,
                classifications=classifications_by_decision.get(decision_id, ()),
                has_followup=decision_id in followup_ids,
                current_classifier_version=current_classifier_version,
            )
        )

    def included(row: JsonMap) -> bool:
        row_venue = (_text(row.get("venue")) or "").upper()
        row_disposition = (_text(row.get("disposition")) or "").upper()
        searchable = " ".join(
            str(row.get(key) or "")
            for key in (
                "decision_id",
                "subject_projection_id",
                "subject_source_record_id",
                "origin_decision_id",
            )
        ).casefold()
        return (
            (venue_filter == "ALL" or row_venue == venue_filter)
            and (disposition_filter == "ALL" or row_disposition == disposition_filter)
            and (not normalized_query or normalized_query in searchable)
        )

    filtered = tuple(row for row in rows if included(row))
    page, total = _paginate(filtered, limit=limit, offset=offset)
    return {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "current_classifier_version": current_classifier_version,
        "known_classifier_versions": tuple(known_classifier_versions),
        "empty_state": (
            "NO_RETAINED_DECISIONS"
            if not rows
            else ("NO_MATCHING_DECISIONS" if not filtered else None)
        ),
        "authority": "NONE",
    }


def build_operator_decision_detail(
    decision: JsonMap | None,
    *,
    classifications: Sequence[JsonMap],
    followups: Sequence[JsonMap],
) -> dict[str, object]:
    """The one detail bundle every decision-linked evidence_href (from the
    Decision Ledger, Opportunities, and Follow-Up views alike) resolves to:
    the stable decision event, every classifier version's independent
    interpretation of it side by side, and every retained follow-up
    observation. Computes no new judgment; only assembles already-retained
    rows that already reference this ``decision_id``."""

    if decision is None:
        return {
            "decision": None,
            "classifications": (),
            "followups": (),
            "empty_state": "DECISION_NOT_FOUND",
            "authority": "NONE",
        }
    ordered_classifications = tuple(
        sorted(classifications, key=lambda item: _text(item.get("classified_at")) or "")
    )
    ordered_followups = tuple(
        sorted(
            followups,
            key=lambda item: (
                _text(item.get("policy_version")) or "",
                _text(item.get("horizon_kind")) or "",
                _text(item.get("baseline_kind")) or "",
            ),
        )
    )
    return {
        "decision": decision,
        "classifications": ordered_classifications,
        "followups": ordered_followups,
        "empty_state": None,
        "authority": "NONE",
    }


# ==========================================================================
# Follow-Up (DecisionFollowUpObservation under FOLLOWUP_POLICY_V1/V2/V3)
# ==========================================================================


_UNASSESSED_EVIDENCE_ELIGIBILITY: dict[str, object] = {
    "status": "UNASSESSED",
    "reason_code": None,
    "evidence_defect_type": None,
    "assessment_policy_version": None,
    "assessed_at": None,
}


def _evidence_eligibility_row(assessment: JsonMap | None) -> dict[str, object]:
    """CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
    Remediation v1 -- minimal operator visibility for an observed row's
    retroactive evidence-quality verdict.  ``status`` is one of
    ``ELIGIBLE``/``INELIGIBLE_EVIDENCE`` (the two real, stored ``domain.
    counterfactual_followup.FollowUpEvidenceQualityVerdict`` members) or
    ``UNASSESSED`` -- which is never a stored verdict, only this read
    model's own description of "no assessment row exists yet for this
    observation" (Part: an absence, not a verdict).  Purely additive: an
    observed row's own already-existing fields (``reference_price_at_
    baseline``, ``provenance``, ...) are completely unchanged by this
    function -- it only ever adds one new, clearly-scoped sub-object."""

    if assessment is None:
        return dict(_UNASSESSED_EVIDENCE_ELIGIBILITY)
    return {
        "status": assessment.get("verdict"),
        "reason_code": assessment.get("reason_code"),
        "evidence_defect_type": assessment.get("evidence_defect_type"),
        "assessment_policy_version": assessment.get("assessment_policy_version"),
        "assessed_at": assessment.get("assessed_at"),
    }


def _followup_observed_row(
    observation: JsonMap, *, evidence_assessment: JsonMap | None = None
) -> dict[str, object]:
    decision_id = observation.get("decision_id")
    return {
        "row_kind": "OBSERVED",
        "status": "OBSERVED",
        "observation_id": observation.get("observation_id"),
        "decision_id": decision_id,
        "venue": observation.get("venue"),
        "disposition": observation.get("disposition"),
        "policy_version": observation.get("policy_version"),
        "horizon_kind": observation.get("horizon_kind"),
        "baseline_kind": observation.get("baseline_kind"),
        "horizon_days": observation.get("horizon_days"),
        "baseline_time": observation.get("baseline_time"),
        "target_time": observation.get("target_time"),
        "observation_type": observation.get("observation_type"),
        "subject_instrument_id": observation.get("subject_instrument_id"),
        "subject_product_or_market_id": observation.get("subject_product_or_market_id"),
        "provenance": observation.get("provenance"),
        # CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
        # Remediation v1: both sides' evidence class, when the retained row
        # itself carries them (every row built after this milestone; a
        # legacy CRYPTO row predating it reads ``baseline_evidence_class``
        # back as ``None`` -- see ``domain.counterfactual_followup.
        # DecisionFollowUpObservation.baseline_evidence_class``'s own
        # docstring for why that is never guessed).  Never a replacement
        # for ``evidence_eligibility`` below, which is the retroactive,
        # additive, versioned verdict -- these are the raw facts it was
        # computed from (when known at write time).
        "baseline_evidence_class": observation.get("baseline_evidence_class"),
        "target_evidence_class": observation.get("target_evidence_class"),
        "observed_at": observation.get("observed_at"),
        "data_limitations": observation.get("data_limitations", ()),
        "reference_price_at_baseline": observation.get("reference_price_at_baseline"),
        "reference_price_at_target": observation.get("reference_price_at_target"),
        "subsequent_return": observation.get("subsequent_return"),
        "market_status_at_target": observation.get("market_status_at_target"),
        "yes_bid_at_target": observation.get("yes_bid_at_target"),
        "yes_ask_at_target": observation.get("yes_ask_at_target"),
        "settlement_result": observation.get("settlement_result"),
        "settlement_source": observation.get("settlement_source"),
        "reasons": (),
        "evidence_eligibility": _evidence_eligibility_row(evidence_assessment),
        "evidence_href": _href("/api/v1/operator-console/decisions", decision_id),
    }


def _followup_pending_row(
    entry: JsonMap, *, plan_policy_versions: Sequence[str], decision: JsonMap | None
) -> dict[str, object]:
    """A not-yet-observed ``(decision, horizon, baseline)`` combination from
    the current policy's own read-only plan. ``FollowUpPlanEntry`` itself
    carries no subject/market fields, so market/subject/disposition context
    -- when the caller could resolve the linked decision -- is joined in
    from the same already-retained ``CounterfactualDecision`` the Decision
    Ledger shows; never invented when the decision cannot be resolved."""

    decision_id = entry.get("decision_id")
    context = decision or {}
    return {
        "row_kind": "PLANNED",
        "status": entry.get("state"),
        "observation_id": None,
        "decision_id": decision_id,
        "venue": entry.get("venue"),
        "disposition": context.get("disposition"),
        # Counterfactual Forward Outcome Temporal Integrity v1: a plan
        # entry now carries its own ``policy_version``
        # (``FollowUpPlanEntry``), which is the only honest source once the
        # current selection routes different venues to different versions
        # and their plans are shown merged.  The fallback covers a caller
        # that is still passing entries from a single-version plan produced
        # before that field existed, and only ever fires when the selection
        # has exactly one version -- never guessing which of several it was.
        "policy_version": (
            entry.get("policy_version")
            if entry.get("policy_version") is not None
            else (plan_policy_versions[0] if len(plan_policy_versions) == 1 else None)
        ),
        "horizon_kind": entry.get("horizon_kind"),
        "baseline_kind": entry.get("baseline_kind"),
        "horizon_days": None,
        "baseline_time": None,
        "target_time": entry.get("target_time"),
        "observation_type": None,
        "subject_instrument_id": context.get("subject_source_record_id"),
        "subject_product_or_market_id": context.get("subject_projection_id"),
        "provenance": None,
        "baseline_evidence_class": None,
        "target_evidence_class": None,
        "observed_at": None,
        "data_limitations": (),
        "reference_price_at_baseline": None,
        "reference_price_at_target": None,
        "subsequent_return": None,
        "market_status_at_target": None,
        "yes_bid_at_target": None,
        "yes_ask_at_target": None,
        "settlement_result": None,
        "settlement_source": None,
        "reasons": entry.get("reasons", ()),
        # No observation exists yet to assess -- deliberately ``None``, not
        # ``UNASSESSED``: the latter describes a *retained observation*
        # whose evidence has not yet been reviewed, a materially different
        # fact from "nothing has been observed at all yet" (Part: see
        # ``_evidence_eligibility_row``'s own docstring).
        "evidence_eligibility": None,
        "evidence_href": _href("/api/v1/operator-console/decisions", decision_id),
    }


# Counterfactual Follow-Up Long-Run Fairness v1 -- names of the ``KEY:<int>``
# warning-code facts ``adapters.operating_cycle_tasks`` already attaches to
# every genuinely-run ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` task result (see
# its own module for where each is produced). Listed here, once, so the
# parser below and any test asserting against it share one source of truth
# for the exact set of facts this view can ever surface.
_FOLLOWUP_CYCLE_FACT_KEYS = frozenset(
    {
        "FOLLOWUP_CONSIDERED_PAIRS",
        "FOLLOWUP_CREATED",
        "FOLLOWUP_REUSED_IDEMPOTENT",
        "FOLLOWUP_BLOCKED_UNAVAILABLE",
        "FOLLOWUP_PERMANENTLY_UNAVAILABLE",
        "FOLLOWUP_SKIPPED_NOT_YET_DUE",
        "FOLLOWUP_SKIPPED_UNSUPPORTED",
        "FOLLOWUP_SKIPPED_ALREADY_OBSERVED",
        "FOLLOWUP_PROVIDER_REQUESTS",
        "FOLLOWUP_SAMPLE_LIMIT",
        "FOLLOWUP_CANDIDATES_EXAMINED",
        "FOLLOWUP_CANDIDATE_SCAN_LIMIT",
    }
)
_FOLLOWUP_KALSHI_BUDGET_EXHAUSTED_CODE = (
    "COUNTERFACTUAL_FOLLOWUP_KALSHI_REQUEST_BUDGET_EXHAUSTED_THIS_CYCLE"
)


def _followup_cycle_facts(warning_codes: Sequence[object]) -> dict[str, int]:
    """Pure parse of one already-retained ``OperatingTaskResult.
    warning_codes`` tuple into the small int-valued mapping of known
    ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` facts it carries -- never a second
    sync, never a recomputation of what that cycle actually did. A key
    absent from ``warning_codes`` (an older retained cycle predating one of
    these facts, or a non-matching task's warnings) is simply absent from
    the result, never defaulted to zero, so 'genuinely zero this cycle' is
    never confused with 'not retained yet'."""

    facts: dict[str, int] = {}
    for code in warning_codes:
        if not isinstance(code, str):
            continue
        key, separator, raw_value = code.partition(":")
        if not separator or key not in _FOLLOWUP_CYCLE_FACT_KEYS:
            continue
        try:
            facts[key] = int(raw_value)
        except ValueError:
            continue
    return facts


def build_followup_progress(
    *,
    plan_state_counts: Sequence[object],
    latest_cycle_task: JsonMap | None,
    scan_cursors: Sequence[JsonMap],
    kalshi_request_budget: int,
    kalshi_resolution_census: JsonMap | None = None,
    kalshi_baseline_quote_census: JsonMap | None = None,
) -> dict[str, object]:
    """A calm, evidence-only answer to "is follow-up actually making
    progress, or is it stuck?" -- assembled entirely from already-retained
    facts (the current read-only plan's own state counts, the most recently
    completed-or-reused ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` cycle's warning
    codes, and the durable scan-cursor row) with no new judgment of its own:
    it computes no "stuck" verdict, only the same kind of labeled counts and
    timestamps every other view in this module already reports, so an
    operator draws that conclusion, not Apex. ``latest_cycle_task`` and
    ``scan_cursors`` are each independently optional -- a fresh database or
    one predating this fairness mechanism reports honest empty state for
    either half rather than a fabricated zero.

    ``scan_cursors`` (Counterfactual Forward Outcome Temporal Integrity v1)
    is a sequence rather than a single cursor because the current per-venue
    policy selection runs one independently-cursored bounded scan per
    distinct policy version (CRYPTO under ``FOLLOWUP_POLICY_V3``, KALSHI and
    SECURITIES under ``FOLLOWUP_POLICY_V2``).  Every cursor is reported in
    full under ``scan_cursors``; the four flat ``scan_cursor_*`` keys report
    the single *most recently updated* one, which is what the at-a-glance
    "is the scan still moving?" question they answer needs, and it is
    labelled with its own ``scan_cursor_policy_version`` so it is never
    mistaken for the only scan there is.

    ``kalshi_resolution_census`` (Kalshi Settlement & Resolution Evidence
    Maturation v1) is the third independently-optional half, for the
    identical reason: a fresh database or one predating migration `0107`
    reports honest empty state (every ``kalshi_resolution_*`` key below is
    ``None``/empty) rather than a fabricated zero. Expected shape matches
    ``application.kalshi_resolution.KalshiResolutionFactRepository.census()``
    exactly (``total_fact_rows``, ``distinct_market_tickers``,
    ``current_state_by_market``, ``latest_captured_at``).

    ``kalshi_baseline_quote_census`` (Kalshi Baseline Quote Capture &
    Economic Maturation v1) is the fourth independently-optional half, for
    the identical reason: a fresh database or one predating migration
    `0108` reports honest empty state (every ``kalshi_baseline_quote_*``
    key below is ``None``/0) rather than a fabricated zero. Expected shape
    matches ``adapters.kalshi_baseline_quote_postgres.
    PostgresKalshiBaselineQuoteRepository.census()`` exactly
    (``total_quote_rows``, ``by_quote_kind``,
    ``matched_trade_execution_rows``, ``market_quote_observation_rows``,
    ``latest_captured_at``). Answers "is Kalshi baseline capture healthy?",
    "how many baseline quotes of each kind are retained?", and "when was
    the latest successful capture?" directly from already-retained
    evidence -- the same discipline as every other field here."""

    cycle_warnings = _sequence((latest_cycle_task or {}).get("warning_codes", ()))
    facts = _followup_cycle_facts(cycle_warnings)
    state_counts = _state_counts_map(plan_state_counts)
    resolution_census = kalshi_resolution_census or {}
    raw_state_by_market = resolution_census.get("current_state_by_market")
    resolution_state_by_market = (
        raw_state_by_market if isinstance(raw_state_by_market, Mapping) else {}
    )
    resolution_unresolved_markets = sum(
        count
        for state, count in resolution_state_by_market.items()
        if state != "FINALIZED" and isinstance(count, int)
    )
    baseline_quote_census = kalshi_baseline_quote_census or {}
    # The most recently advanced scan across the selection.  Ranked by the
    # retained ``updated_at`` where present, with a deterministic
    # ``policy_version`` tie-break so two cursors saved in the same cycle
    # never resolve by argument order.  ``None`` only when no completed scan
    # has ever recorded a position for any version.
    latest_scan_cursor = max(
        scan_cursors,
        key=lambda cursor: (
            _text(cursor.get("updated_at")) or "",
            _text(cursor.get("policy_version")) or "",
        ),
        default=None,
    )
    return {
        "currently_due": state_counts.get("DUE", 0),
        "currently_not_yet_due": state_counts.get("NOT_YET_DUE", 0),
        "currently_permanently_unavailable": state_counts.get("PERMANENTLY_UNAVAILABLE", 0),
        "last_cycle_completed_at": (latest_cycle_task or {}).get("completed_at"),
        "last_cycle_status": (latest_cycle_task or {}).get("status"),
        "last_cycle_genuine_attempts": facts.get("FOLLOWUP_CONSIDERED_PAIRS"),
        "last_cycle_created": facts.get("FOLLOWUP_CREATED"),
        "last_cycle_reused_idempotent": facts.get("FOLLOWUP_REUSED_IDEMPOTENT"),
        "last_cycle_transiently_blocked": facts.get("FOLLOWUP_BLOCKED_UNAVAILABLE"),
        "last_cycle_permanently_unavailable_skipped": facts.get("FOLLOWUP_PERMANENTLY_UNAVAILABLE"),
        "last_cycle_skipped_not_yet_due": facts.get("FOLLOWUP_SKIPPED_NOT_YET_DUE"),
        "last_cycle_skipped_unsupported": facts.get("FOLLOWUP_SKIPPED_UNSUPPORTED"),
        "last_cycle_skipped_already_observed": facts.get("FOLLOWUP_SKIPPED_ALREADY_OBSERVED"),
        "last_cycle_candidates_examined": facts.get("FOLLOWUP_CANDIDATES_EXAMINED"),
        "last_cycle_candidate_scan_limit": facts.get("FOLLOWUP_CANDIDATE_SCAN_LIMIT"),
        "last_cycle_sample_limit": facts.get("FOLLOWUP_SAMPLE_LIMIT"),
        "last_cycle_provider_requests": facts.get("FOLLOWUP_PROVIDER_REQUESTS"),
        "kalshi_request_budget_per_cycle": kalshi_request_budget,
        "kalshi_budget_exhausted_last_cycle": _FOLLOWUP_KALSHI_BUDGET_EXHAUSTED_CODE
        in cycle_warnings,
        # Kalshi Settlement & Resolution Evidence Maturation v1: answers
        # "is Kalshi outcome evidence acquisition healthy?", "how many
        # tracked markets remain unresolved?", "how many authoritative
        # resolution facts have been retained?", and "when was the most
        # recent successful resolution refresh?" directly from already-
        # retained evidence -- no new judgment, same discipline as every
        # other field in this function.
        "kalshi_resolution_facts_retained": resolution_census.get("total_fact_rows", 0),
        "kalshi_resolution_distinct_markets_tracked": resolution_census.get(
            "distinct_market_tickers", 0
        ),
        "kalshi_resolution_current_state_by_market": resolution_state_by_market,
        "kalshi_resolution_markets_unresolved": resolution_unresolved_markets,
        "kalshi_resolution_latest_captured_at": resolution_census.get("latest_captured_at"),
        # Kalshi Baseline Quote Capture & Economic Maturation v1: answers
        # "is Kalshi baseline capture healthy?", "how many baseline quotes
        # (of each kind) are retained?", and "when was the latest
        # successful capture?" -- identical discipline to the resolution
        # fields immediately above.
        "kalshi_baseline_quote_rows_retained": baseline_quote_census.get("total_quote_rows", 0),
        "kalshi_baseline_quote_matched_trade_rows": baseline_quote_census.get(
            "matched_trade_execution_rows", 0
        ),
        "kalshi_baseline_quote_market_quote_rows": baseline_quote_census.get(
            "market_quote_observation_rows", 0
        ),
        "kalshi_baseline_quote_latest_captured_at": baseline_quote_census.get("latest_captured_at"),
        "scan_cursors": list(scan_cursors),
        "scan_cursor_policy_version": (latest_scan_cursor or {}).get("policy_version"),
        "scan_cursor_decision_id": (latest_scan_cursor or {}).get("decision_id"),
        "scan_cursor_horizon_kind": (latest_scan_cursor or {}).get("horizon_kind"),
        "scan_cursor_baseline_kind": (latest_scan_cursor or {}).get("baseline_kind"),
        "scan_cursor_updated_at": (latest_scan_cursor or {}).get("updated_at"),
        "scan_cursor_empty_state": (
            None if latest_scan_cursor is not None else "NO_COMPLETED_SCAN_YET"
        ),
    }


def build_operator_followups(
    observations: Sequence[JsonMap],
    plan_entries: Sequence[JsonMap],
    *,
    decisions_by_id: Mapping[str, JsonMap],
    coverage: JsonMap,
    plan_summary: JsonMap,
    plan_policy_versions: Sequence[str],
    policy_version: str = "ALL",
    baseline_kind: str = "ALL",
    horizon_kind: str = "ALL",
    venue: str = "ALL",
    status: str = "ALL",
    limit: int = 25,
    offset: int = 0,
    latest_cycle_task: JsonMap | None = None,
    scan_cursors: Sequence[JsonMap] = (),
    kalshi_request_budget: int = 0,
    evidence_assessments_by_observation_id: Mapping[str, JsonMap] | None = None,
    kalshi_resolution_census: JsonMap | None = None,
    kalshi_baseline_quote_census: JsonMap | None = None,
) -> dict[str, object]:
    """A single merged, filterable view spanning every retained V1/V2
    observation (status ``OBSERVED``) plus the current policy's own
    read-only backlog (``DUE``/``NOT_YET_DUE``/``UNSUPPORTED``) for every
    ``(decision, horizon, baseline)`` combination that has not yet been
    observed. ``plan_entries`` must already exclude entries the caller
    marked ``already_observed`` when building the plan (see
    ``application.counterfactual_followup.PlanFollowUpObservations`` --
    read-only, makes no provider request); this function additionally drops
    any stray ``OBSERVED`` plan entry defensively so a row is never shown
    twice under two different statuses.

    ``evidence_assessments_by_observation_id`` (CRYPTO Follow-Up Evidence
    Eligibility & Contaminated Observation Remediation v1) is optional and
    defaults to empty -- every existing caller (and every existing test)
    that does not pass it keeps working unchanged, and every observed row
    honestly reports ``UNASSESSED`` rather than fabricating a verdict (Part:
    "do not turn this into a GUI redesign" -- the caller is responsible for
    fetching ``adapters.counterfactual_followup_postgres.
    PostgresCounterfactualFollowUpRepository.assessments()`` and keying it
    by ``observation_id``; this function never reads a repository itself).

    ``kalshi_resolution_census`` (Kalshi Settlement & Resolution Evidence
    Maturation v1) is optional and defaults to ``None`` for the identical
    reason -- forwarded unchanged into ``build_followup_progress``'s own
    ``progress`` sub-object. The caller is responsible for fetching
    ``adapters.kalshi_resolution_postgres.PostgresKalshiResolutionFactRepository
    .census()``; this function never reads a repository itself.

    ``kalshi_baseline_quote_census`` (Kalshi Baseline Quote Capture &
    Economic Maturation v1) is optional and defaults to ``None`` for the
    identical reason -- forwarded unchanged into ``build_followup_
    progress``'s own ``progress`` sub-object. The caller is responsible for
    fetching ``adapters.kalshi_baseline_quote_postgres.
    PostgresKalshiBaselineQuoteRepository.census()``; this function never
    reads a repository itself."""

    assessments = evidence_assessments_by_observation_id or {}
    venue_filter = venue.upper()
    horizon_filter = horizon_kind.upper()
    baseline_filter = baseline_kind.upper()
    status_filter = status.upper()
    policy_filter = (policy_version or "ALL").upper()
    if venue_filter not in _VENUES:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_VENUE_FILTER_INVALID")
    if horizon_filter not in _HORIZON_KINDS:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_HORIZON_FILTER_INVALID")
    if baseline_filter not in _BASELINE_KINDS:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_BASELINE_FILTER_INVALID")
    if status_filter not in _FOLLOWUP_STATUSES:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_STATUS_FILTER_INVALID")

    def _observed_row(item: object) -> dict[str, object]:
        obs = _mapping(item)
        return _followup_observed_row(
            obs, evidence_assessment=assessments.get(str(obs.get("observation_id")))
        )

    observed_rows = [_observed_row(item) for item in observations]
    pending_rows = [
        _followup_pending_row(
            entry,
            plan_policy_versions=plan_policy_versions,
            decision=decisions_by_id.get(str(entry.get("decision_id"))),
        )
        for item in plan_entries
        if (entry := _mapping(item)).get("state") != "OBSERVED"
    ]
    rows: tuple[JsonMap, ...] = (*observed_rows, *pending_rows)

    def included(row: JsonMap) -> bool:
        row_venue = (_text(row.get("venue")) or "").upper()
        row_horizon = (_text(row.get("horizon_kind")) or "").upper()
        row_baseline = (_text(row.get("baseline_kind")) or "").upper()
        row_status = (_text(row.get("status")) or "").upper()
        row_policy = (_text(row.get("policy_version")) or "").upper()
        return (
            (venue_filter == "ALL" or row_venue == venue_filter)
            and (horizon_filter == "ALL" or row_horizon == horizon_filter)
            and (baseline_filter == "ALL" or row_baseline == baseline_filter)
            and (status_filter == "ALL" or row_status == status_filter)
            and (policy_filter == "ALL" or row_policy == policy_filter)
        )

    filtered = tuple(row for row in rows if included(row))
    ordered = tuple(
        sorted(
            filtered,
            key=lambda row: (
                _text(row.get("target_time")) or "",
                _text(row.get("decision_id")) or "",
                _text(row.get("horizon_kind")) or "",
                _text(row.get("baseline_kind")) or "",
            ),
        )
    )
    page, total = _paginate(ordered, limit=limit, offset=offset)
    return {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "coverage": coverage,
        "plan_summary": plan_summary,
        # Counterfactual Forward Outcome Temporal Integrity v1: "the
        # current policy version" is now a per-venue selection, so this
        # reports every version in it.  ``current_policy_version`` is kept
        # as the single display string the console badge already renders --
        # a joined view of the same list, never a different fact.
        "current_policy_versions": list(plan_policy_versions),
        "current_policy_version": " · ".join(plan_policy_versions),
        "progress": build_followup_progress(
            plan_state_counts=_sequence(plan_summary.get("state_counts")),
            latest_cycle_task=latest_cycle_task,
            scan_cursors=scan_cursors,
            kalshi_request_budget=kalshi_request_budget,
            kalshi_resolution_census=kalshi_resolution_census,
            kalshi_baseline_quote_census=kalshi_baseline_quote_census,
        ),
        "empty_state": (
            "NO_RETAINED_OR_PENDING_FOLLOWUP"
            if not rows
            else ("NO_MATCHING_FOLLOWUP" if not filtered else None)
        ),
        "authority": "NONE",
    }


# ==========================================================================
# Options Research (OptionsForwardEvidenceCheckpoint forward campaign)
# ==========================================================================


def _session_row(checkpoints_for_session: Sequence[JsonMap]) -> dict[str, object]:
    """One session's representative row: the latest checkpoint recorded for
    it. A same-session replay that captures further underlyings never
    rewrites the earlier checkpoint -- every checkpoint stays retained --
    so 'latest' is that session's own most-complete genuine state, never a
    summary Apex invents."""

    latest = max(checkpoints_for_session, key=lambda item: _text(item.get("created_at")) or "")
    return {
        "session_id": latest.get("session_id"),
        "checkpoint_id": latest.get("checkpoint_id"),
        "plan_id": latest.get("plan_id"),
        "window_open_at": latest.get("window_open_at"),
        "window_close_at": latest.get("window_close_at"),
        "provider": latest.get("provider"),
        "feed": latest.get("feed"),
        "feed_entitlement_state": latest.get("feed_entitlement_state"),
        "underlying_symbols_captured": latest.get("underlying_symbols_captured", ()),
        "underlyings_requested_count": latest.get("underlyings_requested_count"),
        "underlyings_captured_count": latest.get("underlyings_captured_count"),
        "stale_snapshot_count": latest.get("stale_snapshot_count"),
        "quote_quality_counts": latest.get("quote_quality_counts", ()),
        "chain_coverage_quality_counts": latest.get("chain_coverage_quality_counts", ()),
        "cap_constrained_underlyings": latest.get("cap_constrained_underlyings", ()),
        "structures_evaluated": latest.get("structures_evaluated"),
        "structures_retained": latest.get("structures_retained"),
        "fee_evidence_status": latest.get("fee_evidence_status"),
        "opra_entitlement_state": latest.get("opra_entitlement_state"),
        "open_interest_limitation": latest.get("open_interest_limitation"),
        "historical_pricing_limitation": latest.get("historical_pricing_limitation"),
        "iv_history_ready_underlying_count": latest.get("iv_history_ready_underlying_count"),
        "forward_history_underlying_count": latest.get("forward_history_underlying_count"),
        "errors": latest.get("errors", ()),
        "limitations": latest.get("limitations", ()),
        "created_at": latest.get("created_at"),
        "checkpoint_count_this_session": len(checkpoints_for_session),
        "evidence_href": _href(
            "/api/v1/operator-console/options-research/checkpoints", latest.get("checkpoint_id")
        ),
    }


def build_operator_options_research(
    checkpoints: Sequence[JsonMap],
    *,
    target_session_count: int,
    latest_underlying_snapshots: Sequence[JsonMap] = (),
    capture_task: JsonMap | None = None,
    missed_session_ids: Sequence[str] = (),
) -> dict[str, object]:
    """The forward research-campaign view: distinct-session progress toward
    ``target_session_count`` (the caller must pass
    ``domain.options_quote_intelligence.IV_RANK_MINIMUM_HISTORY`` -- never a
    value invented in this module or in the frontend), genuine per-session
    history, and the latest session's own per-underlying detail.
    ``readiness_state`` reuses ``evaluate_options_history_readiness`` rather
    than re-deriving its threshold. ``capture_task`` is passed through
    unchanged from the operating cycle's own most recently retained
    due-work evidence -- this module computes no due/scheduling decision of
    its own. ``missed_session_ids`` -- calendar sessions whose capture
    window has already closed with no retained checkpoint -- keeps a
    genuine miss (e.g. a day the unattended cycle skipped before its window
    opened) honestly visible without inventing a fake retained row for it;
    it is kept a plain list of session identities, visually distinct from
    the banked ``sessions`` above, never merged into that list."""

    by_session: dict[str, list[JsonMap]] = {}
    for item in checkpoints:
        row = _mapping(item)
        session_id = _text(row.get("session_id"))
        if session_id is None:
            continue
        by_session.setdefault(session_id, []).append(row)
    sessions = tuple(_session_row(rows) for _, rows in sorted(by_session.items()))
    distinct_session_count = len(sessions)
    latest_session = sessions[-1] if sessions else None
    campaign_identity = (
        str(latest_session.get("plan_id")) if latest_session is not None else "FORWARD_CAMPAIGN"
    )
    readiness = evaluate_options_history_readiness(
        underlying_canonical_security_id=campaign_identity,
        session_ids=tuple(by_session.keys()),
        minimum_required_sessions=max(1, target_session_count),
    )

    underlying_rows: list[dict[str, object]] = []
    cap_constrained = frozenset(
        str(item)
        for item in _sequence((latest_session or {}).get("cap_constrained_underlyings", ()))
    )
    for item in latest_underlying_snapshots:
        row = _mapping(item)
        symbol = row.get("underlying_symbol")
        underlying_rows.append(
            {
                "symbol": symbol,
                "underlying_canonical_security_id": row.get("underlying_canonical_security_id"),
                "snapshot_quality": row.get("data_quality_status"),
                "quote_count": len(_sequence(row.get("observation_ids"))),
                "contracts_considered": row.get("contracts_considered"),
                "contracts_quoted": row.get("contracts_quoted"),
                "contracts_excluded": row.get("contracts_excluded"),
                "cap_constrained": str(symbol) in cap_constrained,
                "evidence_limitations": row.get("data_quality_notes", ()),
                "historical_pricing_note": row.get("historical_options_pricing_note"),
                "captured_at": row.get("captured_at"),
            }
        )

    return {
        "campaign_sessions_captured": distinct_session_count,
        "campaign_sessions_target": target_session_count,
        "campaign_progress_label": f"{distinct_session_count}/{target_session_count}",
        "readiness_state": readiness.state.value,
        "readiness_reasons": readiness.reasons,
        "sessions": sessions,
        "latest_session": latest_session,
        "latest_underlyings": tuple(underlying_rows),
        "capture_task": capture_task,
        "missed_session_ids": tuple(sorted(dict.fromkeys(missed_session_ids))),
        "empty_state": "NO_RETAINED_FORWARD_SESSION" if not sessions else None,
        "authority": "NONE",
    }


# ==========================================================================
# Portfolio / Investment Mandate
# ==========================================================================


def build_operator_portfolio_mandate(
    *,
    portfolio_snapshot: JsonMap | None,
    account_balances: Sequence[JsonMap],
    positions: Sequence[JsonMap],
    mandate: JsonMap | None,
    mandate_lineage_versions: Sequence[JsonMap],
    latest_evaluations: Sequence[JsonMap],
) -> dict[str, object]:
    """Existing normalized ``PortfolioState`` and ``InvestmentMandate``
    truth only. Introduces no valuation of its own and no separate
    'friendly' mandate model beside the versioned backend mandate; mandate
    editing is out of scope for v1."""

    ordered_evaluations = tuple(
        sorted(
            latest_evaluations, key=lambda item: _text(item.get("evaluated_at")) or "", reverse=True
        )
    )
    return {
        "portfolio_snapshot": portfolio_snapshot,
        "account_balances": tuple(account_balances),
        "positions": tuple(positions),
        "portfolio_empty_state": (
            "NO_PORTFOLIO_SNAPSHOT_RETAINED" if portfolio_snapshot is None else None
        ),
        "mandate": mandate,
        "mandate_versions": tuple(
            sorted(mandate_lineage_versions, key=lambda item: _number(item.get("version")) or 0)
        ),
        "mandate_empty_state": "NO_MANDATE_AUTHORED" if mandate is None else None,
        "latest_evaluations": ordered_evaluations[:25],
        "latest_evaluation_count": len(ordered_evaluations),
        "authority": "NONE",
    }


# ==========================================================================
# Operations (cross-cycle operating-task timeline)
# ==========================================================================

_HIGHLIGHT_TASK_TYPES = (
    "OPTIONS_RESEARCH_EVIDENCE_CAPTURE",
    "COUNTERFACTUAL_FOLLOWUP_REFRESH",
    # PAPER Operations & Observability v1: these three PAPER-trial task
    # types already report the identical retained due-work/task-result
    # evidence every other task type does (including
    # ``awaits_intraday_continuation`` and per-strategy
    # ``STRATEGY_PAPER_REJECTED:...`` reasons folded into ``warnings`` --
    # see ``adapters.operating_cycle_tasks._execute_strategy_paper_trial``)
    # -- no separate scheduling or PAPER-specific logic is added here, only
    # a highlight of what already exists, mirroring the two entries above.
    "STRATEGY_PAPER_TRIAL_EXECUTION",
    "CRYPTO_PAPER_TRIAL_EXECUTION",
    "KALSHI_PAPER_TRIAL_EXECUTION",
)


def _task_highlight(
    task_type: str,
    *,
    latest_due_tasks: Sequence[JsonMap],
    ordered_task_results: Sequence[JsonMap],
) -> dict[str, object]:
    due = next(
        (row for item in latest_due_tasks if (row := _mapping(item)).get("task_type") == task_type),
        None,
    )
    history = tuple(
        row
        for item in ordered_task_results
        if (row := _mapping(item)).get("task_type") == task_type
    )
    latest_result = history[0] if history else None  # ordered_task_results is newest-first
    latest_effective = next(
        (row for row in history if row.get("status") in ("COMPLETED", "REUSED")), None
    )
    return {
        "task_type": task_type,
        "current_disposition": (due or {}).get("disposition"),
        "current_reasons": (due or {}).get("reasons", ()),
        "latest_result_status": (latest_result or {}).get("status"),
        "latest_result_completed_at": (latest_result or {}).get("completed_at"),
        "latest_effective_status": (latest_effective or {}).get("status"),
        "latest_effective_completed_at": (latest_effective or {}).get("completed_at"),
        "latest_effective_output_identities": (latest_effective or {}).get("output_identities", ()),
    }


def build_operator_operations(
    task_result_rows: Sequence[JsonMap],
    *,
    latest_due_tasks: Sequence[JsonMap],
    latest_cycle_id: str | None,
    latest_cycle_as_of: str | None,
    task_type: str = "ALL",
    status: str = "ALL",
    limit: int = 25,
    offset: int = 0,
) -> dict[str, object]:
    """A bounded, newest-first timeline of already-retained
    ``OperatingTaskResult`` rows across recent cycles, using the operating
    cycle's own status vocabulary verbatim (``COMPLETED``/``REUSED``/
    ``SKIPPED_NOT_DUE``/``SKIPPED_PAUSED``/``BLOCKED``/``UNAVAILABLE``/
    ``FAILED``). Highlights ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE`` and
    ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` using the identical retained
    due-work/task-result evidence every other task type gets -- no separate
    scheduling logic exists for these two."""

    task_filter = task_type.upper()
    status_filter = status.upper()
    if status_filter not in _TASK_STATUSES:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_STATUS_FILTER_INVALID")
    if not 1 <= limit <= 100 or offset < 0:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_PAGINATION_INVALID")

    ordered = tuple(
        sorted(
            (_mapping(item) for item in task_result_rows),
            key=lambda row: _text(row.get("completed_at")) or "",
            reverse=True,
        )
    )

    def included(row: JsonMap) -> bool:
        return (
            task_filter == "ALL" or (_text(row.get("task_type")) or "").upper() == task_filter
        ) and (status_filter == "ALL" or (_text(row.get("status")) or "").upper() == status_filter)

    filtered = tuple(row for row in ordered if included(row))
    page, total = _paginate(filtered, limit=limit, offset=offset)
    highlights = tuple(
        _task_highlight(name, latest_due_tasks=latest_due_tasks, ordered_task_results=ordered)
        for name in _HIGHLIGHT_TASK_TYPES
    )
    return {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "latest_cycle_id": latest_cycle_id,
        "latest_cycle_as_of": latest_cycle_as_of,
        "highlights": highlights,
        "empty_state": (
            "NO_RETAINED_TASK_RESULTS"
            if not ordered
            else ("NO_MATCHING_TASK_RESULTS" if not filtered else None)
        ),
        "authority": "NONE",
    }


# ==========================================================================
# Overview extensions (additive Home summaries) and Opportunities linkage
# ==========================================================================


def build_operator_overview_extensions(
    *,
    decision_coverage: JsonMap,
    followup_coverage: JsonMap,
    followup_plan_summary: JsonMap,
    options_summary: JsonMap,
    portfolio_summary: JsonMap,
    followup_progress: JsonMap | None = None,
) -> dict[str, object]:
    """Additive-only Home fields: never replaces an existing Home key, only
    adds new ones for the four newer subsystems that previously had no read
    surface at all. ``followup_progress`` (optional, defaults to omitted --
    an older caller not yet passing it keeps working unchanged) is the same
    ``build_followup_progress`` result the dedicated Follow-Up page shows in
    full; only a compact "is it stuck" slice of it is surfaced here so Home
    stays a summary, not a second copy of that page."""

    latest_session = _mapping(options_summary.get("latest_session"))
    portfolio_snapshot = _mapping(portfolio_summary.get("portfolio_snapshot"))
    mandate = _mapping(portfolio_summary.get("mandate"))
    progress = followup_progress or {}
    return {
        "decision_ledger_summary": decision_coverage,
        "followup_summary": {
            "coverage": followup_coverage,
            "current_policy_version": followup_plan_summary.get("policy_version"),
            "current_backlog_state_counts": followup_plan_summary.get("state_counts", ()),
            "as_of": followup_plan_summary.get("as_of"),
            "last_cycle_status": progress.get("last_cycle_status"),
            "last_cycle_completed_at": progress.get("last_cycle_completed_at"),
            "last_cycle_created": progress.get("last_cycle_created"),
            "last_cycle_permanently_unavailable_skipped": progress.get(
                "last_cycle_permanently_unavailable_skipped"
            ),
            "currently_permanently_unavailable": progress.get("currently_permanently_unavailable"),
        },
        "options_research_summary": {
            "campaign_progress_label": options_summary.get("campaign_progress_label"),
            "campaign_sessions_captured": options_summary.get("campaign_sessions_captured"),
            "campaign_sessions_target": options_summary.get("campaign_sessions_target"),
            "readiness_state": options_summary.get("readiness_state"),
            "latest_session": (
                None
                if not latest_session
                else {
                    "session_id": latest_session.get("session_id"),
                    "underlyings_captured_count": latest_session.get("underlyings_captured_count"),
                    "structures_retained": latest_session.get("structures_retained"),
                    "created_at": latest_session.get("created_at"),
                }
            ),
        },
        "portfolio_mandate_summary": {
            "portfolio_as_of": portfolio_snapshot.get("as_of"),
            "portfolio_completeness": portfolio_snapshot.get("completeness"),
            "mandate_version": mandate.get("version"),
            "mandate_effective_at": mandate.get("effective_at"),
            "latest_evaluation_count": portfolio_summary.get("latest_evaluation_count"),
        },
    }


def enrich_opportunities_with_decisions(
    opportunities_payload: JsonMap, *, decisions_by_subject: Mapping[str, JsonMap]
) -> dict[str, object]:
    """Post-processes an already-built ``build_operator_opportunities``
    payload to attach, per row, the counterfactual decision identity (if
    the ledger has synced one for that Opportunity's ``projection_id``) --
    read-only linkage only, never a second eligibility or ranking
    computation, and never a change to row order, filtering, or count."""

    items = tuple(_mapping(item) for item in _sequence(opportunities_payload.get("items")))
    enriched: list[dict[str, object]] = []
    for item in items:
        decision = decisions_by_subject.get(str(item.get("projection_id")))
        enriched.append(
            {
                **item,
                "counterfactual_decision_id": (
                    None if decision is None else decision.get("decision_id")
                ),
                "counterfactual_decision_href": (
                    None
                    if decision is None
                    else _href("/api/v1/operator-console/decisions", decision.get("decision_id"))
                ),
            }
        )
    return {**opportunities_payload, "items": tuple(enriched)}
