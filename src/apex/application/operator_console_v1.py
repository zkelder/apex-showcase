"""Read-only projections for the consolidated Apex Operator Console.

The console deliberately composes retained records.  It does not calculate
financial truth, persist summaries, or add an execution authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from apex.application.strategy_paper_trial import (
    STRATEGY_TRIAL_FEE_BPS,
    STRATEGY_TRIAL_SLIPPAGE_BPS,
    STRATEGY_TRIAL_SPREAD_BPS,
)
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_paper_trial_evidence_coverage import (
    MATERIAL_EVIDENCE_GAP_SECONDS,
    is_material_coverage_gap,
)

type JsonMap = Mapping[str, object]

# SECURITIES Operator Console Backend Read-Model Closure v1: derived from the
# canonical SharedVenue enum (the same identity every Opportunity/decision/
# follow-up row's own `venue` field already uses) rather than a hand-maintained
# literal set -- a real new SharedVenue member can never again require a second,
# separately-drifting edit here to stay filterable.
_VENUES = frozenset({"ALL", *(member.value for member in SharedVenue)})
_ELIGIBILITY = frozenset(
    {"ALL", "ELIGIBLE", "NON_ELIGIBLE", "BLOCKED_MISSING_EVIDENCE", "EXPIRED", "REJECTED"}
)
_PRIORITY = frozenset({"ALL", "RANKED", "UNRANKED"})
_ALLOCATION = frozenset({"ALL", "ALLOCATED", "UNALLOCATED"})
_PAPER_READINESS = frozenset({"ALL", "READY", "NOT_READY"})
_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3}


class OperatorConsoleFilterError(ValueError):
    """A bounded console filter is outside the published vocabulary."""


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


def _href(path: str, identity: object) -> str | None:
    value = _text(identity)
    return None if not value else f"{path}/{value}"


def _pairs(value: object) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in _sequence(value):
        pair = _sequence(item)
        if len(pair) == 2 and isinstance(pair[0], str):
            result[pair[0]] = pair[1]
    return result


def _closure_coverage(closure: object) -> JsonMap | None:
    """Trial Evidence Coverage Disclosure v1: the ``coverage_*`` fields off
    an already-jsonified ``StrategyPaperTrialClosureRecord`` mapping,
    regrouped under one ``evidence_coverage`` sub-object -- see
    ``domain.strategy_paper_trial_evidence_coverage`` for what each one
    means. ``None`` (never a partially-populated dict) for a closure built
    before this milestone shipped or otherwise missing the fields, matching
    every other empty-state convention in this module.

    ``material_gap`` is NOT a persisted field -- ``StrategyPaperTrialClosure
    Record`` retains no such column (see that domain module's "Materiality
    is derived, never persisted" section for why). It is computed here, at
    read time, by applying ``is_material_coverage_gap`` to the persisted,
    authoritative ``largest_gap_seconds``; ``material_gap_threshold_seconds``
    is surfaced alongside it so a consumer never has to separately know or
    hard-code the cutoff that boolean means."""

    row = _mapping(closure)
    window_duration = _number(row.get("coverage_window_duration_seconds"))
    largest_gap_seconds = _number(row.get("coverage_largest_gap_seconds"))
    if window_duration is None or largest_gap_seconds is None:
        return None
    return {
        "window_duration_seconds": window_duration,
        "nominal_days": _number(row.get("coverage_nominal_days")),
        "covered_days": _number(row.get("coverage_covered_days")),
        "qualifying_observation_count": _number(row.get("coverage_qualifying_observation_count")),
        "largest_gap_seconds": largest_gap_seconds,
        "material_gap": is_material_coverage_gap(largest_gap_seconds),
        "material_gap_threshold_seconds": MATERIAL_EVIDENCE_GAP_SECONDS,
        "policy_version": row.get("coverage_policy_version"),
    }


def build_operator_live_readiness(
    workspace: JsonMap,
    *,
    operational: JsonMap | None = None,
    coinbase_read_only: JsonMap | None = None,
    kalshi_read_only: JsonMap | None = None,
    live_preflight: JsonMap | None = None,
) -> dict[str, object]:
    """Expose retained safe readiness evidence without credential or execution data."""

    references = tuple(
        {
            "reference_id": row.get("reference_id"),
            "version": row.get("version"),
            "provider": row.get("provider"),
            "account_environment": row.get("account_environment"),
            "account_purpose": row.get("account_purpose"),
            "required_permissions": row.get("required_permissions", ()),
            "secret_backend": row.get("secret_backend"),
            "validation_status": row.get("validation_status"),
            "last_successful_read_at": row.get("last_successful_read_at"),
            "warnings": row.get("warnings", ()),
            "canonical_hash": row.get("canonical_hash"),
            "secret_values_retained": False,
        }
        for value in _sequence(workspace.get("credential_references"))
        if (row := _mapping(value))
    )
    capabilities = tuple(
        {
            "snapshot_id": row.get("snapshot_id"),
            "provider": row.get("provider"),
            "account_scope_id": row.get("account_scope_id"),
            "account_environment": row.get("account_environment"),
            "authenticated_read_status": row.get("authenticated_read_status"),
            "permissions": row.get("permissions", ()),
            "fee_evidence": row.get("fee_evidence", ()),
            "minimum_size_constraints": row.get("minimum_size_constraints", ()),
            "preview_available": row.get("preview_available", False),
            "trade_available": row.get("trade_available", False),
            "transfer_available": row.get("transfer_available", False),
            "withdrawal_available": row.get("withdrawal_available", False),
            "account_status": row.get("account_status"),
            "observed_at": row.get("observed_at"),
            "limitations": row.get("limitations", ()),
            "canonical_hash": row.get("canonical_hash"),
            "provider_write_capability": "NONE",
            "live_authority": "NONE",
        }
        for value in _sequence(workspace.get("capabilities"))
        if (row := _mapping(value))
    )
    assessment = _mapping(workspace.get("current_assessment"))
    control = _mapping(workspace.get("current_execution_control"))
    backup = _mapping(workspace.get("backup_readiness"))
    policy = _mapping(workspace.get("risk_policy"))
    operations = _mapping(operational)
    coinbase = _mapping(coinbase_read_only)
    kalshi = _mapping(kalshi_read_only)
    preflight = build_operator_live_preflight(_mapping(live_preflight))
    return {
        "provider_configuration_status": (
            "CREDENTIAL_REFERENCES_CONFIGURED" if references else "CREDENTIALS_ABSENT"
        ),
        "credential_references": references,
        "provider_capabilities": capabilities,
        "risk_policy": policy,
        "execution_control": control,
        "backup_readiness": backup,
        "deployment_profile": operations.get("deployment_profile"),
        "operating_runner": operations.get("runner"),
        "backup_manifest": operations.get("backup_manifest"),
        "restore_verification": operations.get("restore_verification"),
        "recovery_attempts": operations.get("recovery_attempts", ()),
        "alert_deliveries": operations.get("alert_deliveries", ()),
        "operational_health": operations.get("operational_health"),
        "kill_switch_drill": operations.get("kill_switch_drill"),
        "coinbase_private_read_only": coinbase,
        "kalshi_private_read_only": kalshi,
        "live_preflight": preflight,
        "assessment": assessment,
        "readiness_recommendation": assessment.get("recommendation", "LIVE_NOT_READY"),
        "unresolved_blockers": assessment.get(
            "unresolved_blockers", ("READINESS_ASSESSMENT_MISSING",)
        ),
        "safe_controls": tuple(_sequence(workspace.get("safe_controls")))
        + tuple(_sequence(operations.get("safe_controls"))),
        "prohibited_controls": workspace.get("prohibited_controls", ()),
        "empty_state": "LIVE_READINESS_EVIDENCE_NOT_MATERIALIZED" if not assessment else None,
        "warning": "LIVE READINESS IS EVIDENCE ONLY — NO LIVE AUTHORITY",
        "authority": "NONE",
        "provider_write_capability": "NONE",
        "live_authority": "NONE",
    }


def build_operator_live_preflight(workspace: JsonMap) -> dict[str, object]:
    """Compact exact-lineage LIVE-preflight evidence with no submission controls."""

    intents = tuple(_mapping(value) for value in _sequence(workspace.get("intents")))
    preflights = tuple(_mapping(value) for value in _sequence(workspace.get("preflights")))
    approvals = tuple(
        _mapping(value) for value in _sequence(workspace.get("provider_preview_approvals"))
    )
    captures = tuple(
        _mapping(value) for value in _sequence(workspace.get("provider_preview_captures"))
    )
    assessments = tuple(_mapping(value) for value in _sequence(workspace.get("assessments")))
    latest_intent = None if not intents else intents[-1]
    latest_preflight = None if not preflights else preflights[-1]
    latest_preview = None if not captures else captures[-1]
    latest_assessment = None if not assessments else assessments[-1]
    return {
        "latest_intent": latest_intent,
        "latest_internal_preflight": latest_preflight,
        "latest_provider_preview": latest_preview,
        "latest_assessment": latest_assessment,
        "intent_count": len(intents),
        "approval_count": len(approvals),
        "provider_preview_support": workspace.get("provider_preview_support", {}),
        "operating_cycle_tasks": workspace.get("operating_cycle_tasks", ()),
        "safe_actions": workspace.get("safe_actions", ()),
        "prohibited_actions": workspace.get("prohibited_actions", ()),
        "provider_request_count": workspace.get("provider_request_count", 0),
        "provider_write_count": 0,
        "empty_state": "NO_ELIGIBLE_LIVE_INTENT" if latest_intent is None else None,
        "warning": "LIVE PREFLIGHT IS EVIDENCE ONLY — NO LIVE AUTHORITY",
        "authority": "NONE",
        "live_authority": "NONE",
    }


def _opportunity_row(value: object) -> dict[str, object]:
    row = _mapping(value)
    projection = _mapping(row.get("projection"))
    eligibility = _mapping(row.get("eligibility"))
    priority = _mapping(row.get("priority"))
    explanation = _mapping(row.get("explanation"))
    source_details = _pairs(projection.get("source_details"))
    projection_id = projection.get("projection_id")
    return {
        "projection_id": projection_id,
        "venue": projection.get("venue"),
        "instrument_id": projection.get("instrument_id"),
        "product_or_market_id": projection.get("product_or_market_id"),
        "strategy_or_method_id": projection.get("strategy_or_method_id"),
        "source_record_type": projection.get("source_record_type"),
        "source_evidence_id": projection.get("source_record_id"),
        "source_canonical_hash": projection.get("source_canonical_hash"),
        "generated_at": projection.get("generated_at"),
        "valid_until": projection.get("valid_until"),
        "evidence_class": projection.get("evidence_class"),
        "freshness_seconds": projection.get("freshness_seconds"),
        "freshness_limit_seconds": projection.get("freshness_limit_seconds"),
        "expected_gross_return": projection.get("expected_gross_return"),
        "expected_net_return": projection.get("expected_net_return"),
        "uncertainty": projection.get("uncertainty"),
        "uncertainty_lower": source_details.get("uncertainty_lower"),
        "uncertainty_upper": source_details.get("uncertainty_upper"),
        "calibration_sample_count": source_details.get("calibration_sample_count"),
        "signal_score": source_details.get("score"),
        "return_7d": source_details.get("return_7d"),
        "return_90d": source_details.get("return_90d"),
        "downside_probability": source_details.get("downside_probability"),
        "downside_p05": source_details.get("downside_p05"),
        "maximum_observed_loss": source_details.get("maximum_observed_loss"),
        "event_set_market_count": source_details.get("event_set_market_count"),
        "matched_quantity": source_details.get("matched_quantity"),
        "guaranteed_settlement_payout": source_details.get("guaranteed_settlement_payout"),
        "conservative_net_gap": source_details.get("conservative_net_gap"),
        "package_legs": tuple(
            pair for pair in source_details.items() if pair[0].startswith("yes_leg_")
        ),
        "maximum_loss": projection.get("maximum_loss"),
        "required_hypothetical_capital": projection.get("required_hypothetical_capital"),
        "modeled_fees": projection.get("modeled_fees"),
        "modeled_slippage": projection.get("modeled_slippage"),
        "liquidity_state": projection.get("liquidity_state"),
        "best_bid": source_details.get("best_bid", source_details.get("current_yes_bid")),
        "best_ask": source_details.get("best_ask", source_details.get("current_yes_ask")),
        "midpoint": source_details.get("midpoint"),
        "spread_bps": source_details.get("spread_bps"),
        "visible_depth": source_details.get(
            "visible_ask_quote_depth", source_details.get("visible_contract_count")
        ),
        "estimated_lockup_days": projection.get("estimated_lockup_days"),
        "eligibility": eligibility.get("state"),
        "eligibility_reasons": eligibility.get("reasons", ()),
        "source_blockers": projection.get("source_rejection_reasons", ()),
        "priority_rank": priority.get("rank"),
        "priority_score": priority.get("score"),
        "priority_components": priority.get("components", ()),
        # SECURITIES Operator Console Backend Read-Model Closure v1: the one
        # existing field this read model dropped. PriorityResult.tier
        # (ACTIONABLE / RESEARCH_ONLY) is orthogonal to rank/score -- it never
        # reorders or hides a row -- and is distinct from `eligibility`
        # (research eligibility), `paper_readiness` (below), and `authority`
        # (always "NONE" here): a row with eligibility=ELIGIBLE can still
        # faithfully show tier=RESEARCH_ONLY. `None` (not "RESEARCH_ONLY")
        # when no PriorityResult exists for this row at all -- e.g. a
        # rejected/unranked Opportunity never had one computed -- matching
        # the existing honest-missing convention `priority_rank`/
        # `priority_score` already use just above.
        "priority_tier": priority.get("tier"),
        "priority_tier_reasons": priority.get("tier_reasons", ()),
        "explanation": explanation.get("text"),
        "warnings": projection.get("source_warnings", ()),
        "evidence_lineage": projection.get("supporting_evidence_ids", ()),
        "source_details": projection.get("source_details", ()),
        "authority": projection.get("authority", "NONE"),
        "paper_eligible": projection.get("paper_eligible", False),
        "paper_eligibility_reasons": projection.get("paper_eligibility_reasons", ()),
        "live_eligible": projection.get("live_eligible", False),
        "live_authority": "NONE",
        "evidence_href": _href("/api/v1/shared-opportunities/projections", projection_id),
    }


def build_operator_opportunities(
    shared: JsonMap,
    *,
    cross_venue: JsonMap | None = None,
    venue: str = "ALL",
    eligibility: str = "ALL",
    priority: str = "ALL",
    allocation: str = "ALL",
    paper_readiness: str = "ALL",
    query: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, object]:
    """Build a bounded, filterable inbox without changing retained rank order."""

    venue_filter = venue.upper()
    eligibility_filter = eligibility.upper()
    priority_filter = priority.upper()
    allocation_filter = allocation.upper()
    paper_filter = paper_readiness.upper()
    if venue_filter not in _VENUES:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_VENUE_FILTER_INVALID")
    if eligibility_filter not in _ELIGIBILITY:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_ELIGIBILITY_FILTER_INVALID")
    if priority_filter not in _PRIORITY:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_PRIORITY_FILTER_INVALID")
    if allocation_filter not in _ALLOCATION:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_ALLOCATION_FILTER_INVALID")
    if paper_filter not in _PAPER_READINESS:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_PAPER_READINESS_FILTER_INVALID")
    if not 1 <= limit <= 100 or offset < 0:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_PAGINATION_INVALID")
    normalized_query = (query or "").strip().casefold()
    if len(normalized_query) > 160:
        raise OperatorConsoleFilterError("OPERATOR_CONSOLE_QUERY_TOO_LONG")

    retained = (
        *_sequence(shared.get("ranked_opportunities")),
        *_sequence(shared.get("rejected_opportunities")),
    )
    cross = _mapping(cross_venue)
    decision = _mapping(cross.get("decision"))
    considered = frozenset(
        str(value) for value in _sequence(decision.get("considered_projection_ids"))
    )
    excluded = frozenset(str(value) for value in _sequence(decision.get("excluded_projection_ids")))
    lines = {
        str(line_value.get("projection_id")): line_value
        for value in _sequence(cross.get("lines"))
        if (line_value := _mapping(value)).get("projection_id") is not None
    }
    proposals = {
        str(proposal_value.get("source_projection_id")): proposal_value
        for value in _sequence(cross.get("paper_trial_proposals"))
        if (proposal_value := _mapping(value)).get("source_projection_id") is not None
    }
    allocation_explanations = {
        str(explanation.get("projection_id")): explanation
        for value in _sequence(cross.get("explanations"))
        if (explanation := _mapping(value)).get("projection_id") is not None
    }
    rows: tuple[dict[str, object], ...] = ()
    projected: list[dict[str, object]] = []
    for item in retained:
        row = _opportunity_row(item)
        projection_id = str(row.get("projection_id"))
        current_line = lines.get(projection_id)
        current_proposal = proposals.get(projection_id)
        explanation = allocation_explanations.get(projection_id, {})
        row.update(
            {
                "total_market_context": (
                    decision.get("context_state") if projection_id in considered else None
                ),
                "context_effects": (
                    decision.get("context_effects", ()) if projection_id in considered else ()
                ),
                "hypothetical_allocation": (
                    current_line.get("amount")
                    if current_line is not None
                    else "0"
                    if projection_id in considered
                    else None
                ),
                "allocation_status": (
                    "ALLOCATED"
                    if current_line is not None
                    else "EXCLUDED"
                    if projection_id in excluded
                    else "NOT_CONSIDERED"
                ),
                "allocation_reasons": (
                    explanation.get("excluded_reasons", ())
                    or explanation.get("binding_constraints", ())
                ),
                "paper_readiness": "READY" if current_proposal is not None else "NOT_READY",
                "paper_readiness_reasons": (
                    ()
                    if current_proposal is not None
                    else explanation.get("excluded_reasons", ("NO_PAPER_TRIAL_PROPOSAL",))
                ),
                "paper_trial_proposal_id": (
                    current_proposal.get("proposal_id") if current_proposal is not None else None
                ),
            }
        )
        projected.append(row)
    rows = tuple(projected)

    def included(row: JsonMap) -> bool:
        row_venue = (_text(row.get("venue")) or "").upper()
        row_eligibility = (_text(row.get("eligibility")) or "").upper()
        is_ranked = row.get("priority_rank") is not None
        is_allocated = row.get("allocation_status") == "ALLOCATED"
        is_paper_ready = row.get("paper_readiness") == "READY"
        searchable = " ".join(
            str(row.get(key) or "")
            for key in (
                "projection_id",
                "venue",
                "instrument_id",
                "product_or_market_id",
                "strategy_or_method_id",
            )
        ).casefold()
        return (
            (venue_filter == "ALL" or row_venue == venue_filter)
            and (
                eligibility_filter == "ALL"
                or row_eligibility == eligibility_filter
                or (eligibility_filter == "NON_ELIGIBLE" and row_eligibility != "ELIGIBLE")
            )
            and (
                priority_filter == "ALL"
                or (priority_filter == "RANKED" and is_ranked)
                or (priority_filter == "UNRANKED" and not is_ranked)
            )
            and (
                allocation_filter == "ALL"
                or (allocation_filter == "ALLOCATED" and is_allocated)
                or (allocation_filter == "UNALLOCATED" and not is_allocated)
            )
            and (
                paper_filter == "ALL"
                or (paper_filter == "READY" and is_paper_ready)
                or (paper_filter == "NOT_READY" and not is_paper_ready)
            )
            and (not normalized_query or normalized_query in searchable)
        )

    filtered = tuple(row for row in rows if included(row))
    opportunity_set = _mapping(shared.get("opportunity_set"))
    return {
        "opportunity_set_id": opportunity_set.get("opportunity_set_id"),
        "source_manifest_hash": opportunity_set.get("manifest_hash"),
        "items": filtered[offset : offset + limit],
        "total": len(filtered),
        "limit": limit,
        "offset": offset,
        "filters": {
            "venue": venue_filter,
            "eligibility": eligibility_filter,
            "priority": priority_filter,
            "allocation": allocation_filter,
            "paper_readiness": paper_filter,
            "query": normalized_query,
        },
        "empty_state": (
            "NO_RETAINED_OPPORTUNITIES"
            if not rows
            else "NO_OPPORTUNITIES_MATCH_FILTERS"
            if not filtered
            else None
        ),
        "authority": "NONE",
        "paper_authority": "NONE",
        "live_authority": "NONE",
    }


def build_operator_allocation(cross_venue: JsonMap) -> dict[str, object]:
    """Expose the exact retained allocation decision and its explanation records."""

    decision = _mapping(cross_venue.get("decision"))
    decision_id = decision.get("decision_id")
    return {
        "policy": cross_venue.get("policy"),
        "decision": cross_venue.get("decision"),
        "lines": cross_venue.get("lines", ()),
        "explanations": cross_venue.get("explanations", ()),
        "paper_trial_proposals": cross_venue.get("paper_trial_proposals", ()),
        "evidence_href": _href("/api/v1/cross-venue-allocation/decisions", decision_id),
        "empty_state": ("NO_CROSS_VENUE_ALLOCATION_RETAINED" if not decision else None),
        "authority": "NONE",
        "paper_authorization_created": False,
        "orders_created": 0,
        "live_authority": "NONE",
    }


def build_operator_paper_operations(
    *,
    crypto_accounts: Sequence[object],
    kalshi_accounts: Sequence[object],
    external_instructions: Sequence[object],
    crypto_capability: object,
    kalshi_capability: object,
    paper_activation: object | None = None,
    position_marks: Mapping[str, object] | None = None,
    unrealized_totals: Mapping[str, object] | None = None,
    continuation_health: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Preserve internal venue state and external broker evidence as separate lanes.

    PAPER Operations & Observability v1 additive fields:

    - ``paper_activation``: the identical retained ``PaperActivationEvent``
      folding ``build_operator_home`` already reads
      (``operating.workspace()["paper_activation"]``) -- the real per-cycle
      PAPER capability gate, replacing any hardcoded value. ``None`` means
      the caller genuinely has no retained activation history, not that the
      gate is closed.
    - ``position_marks``: optional ``position_id -> mark`` mapping,
      precomputed by the caller (``operator_api.py``) via
      ``ObtainContemporaneousMarketObservation`` (Live Observation
      Point-in-Time Race Fix v1 semantics -- ``as_of``-bounded, never an
      unbounded "latest" query) plus ``domain.crypto_paper.
      estimate_open_position_mark``. Only OPEN CRYPTO positions may have an
      entry; a position absent from this mapping shows an honest
      "not evaluated" disposition rather than a fabricated number. Kalshi
      contracts have no equivalent point-in-time mark substrate today and
      are never looked up here.
    - ``unrealized_totals``: optional ``account_id -> total`` mapping, the
      caller's own real Decimal sum of ``position_marks`` for that
      account's OPEN positions, computed before jsonification -- never
      re-derived from the already-stringified per-position values here.

    PAPER Continuation Health Signal v2 (cross-venue extension) additive
    field:

    - ``continuation_health``: optional ``account_id -> health`` mapping,
      each value an already-computed, already-jsonified
      ``domain.operating_cycle.PaperContinuationHealth`` (via
      ``compute_paper_continuation_health``) -- identical contract to
      ``build_operator_paper_strategy_registry``'s own ``continuation_health``
      parameter (see that function's docstring): this function never
      computes health itself, only surfaces the caller's precomputed value
      verbatim alongside the raw ``latest_reconciliation``/task-status
      fields it supersedes for "is it healthy now". An account absent from
      this mapping (or the whole mapping being ``None``) surfaces ``None``,
      never a fabricated status.
    """

    activation = _mapping(paper_activation)
    marks = position_marks or {}
    health_by_account = continuation_health or {}

    def internal_lane(
        venue: str, values: Sequence[object], capability: object
    ) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        for value in values:
            state = _mapping(value)
            account = _mapping(state.get("account"))
            account_id = account.get("account_id")
            route = (
                "/api/v1/crypto-paper/accounts"
                if venue == "CRYPTO"
                else "/api/v1/kalshi-paper/accounts"
            )

            def evidence_href(
                entity_type: str,
                identity: object,
                account_identity: object = account_id,
                route_path: str = route,
            ) -> str | None:
                identity_text = _text(identity)
                account_text = _text(account_identity)
                if identity_text is None or account_text is None:
                    return None
                return f"{route_path}/{account_text}/{entity_type}/{identity_text}"

            orders = tuple(
                {
                    "order_id": order.get("order_id"),
                    "instrument_id": order.get("product_id", order.get("market_ticker")),
                    "state": order.get("state"),
                    "quantity": order.get("target_base_quantity", order.get("contracts")),
                    "filled_quantity": order.get(
                        "filled_base_quantity", order.get("filled_contracts")
                    ),
                    "expires_at": order.get("valid_until", order.get("expires_at")),
                    "evidence_href": evidence_href("orders", order.get("order_id")),
                }
                for value in _sequence(state.get("orders"))
                if (order := _mapping(value))
            )
            authorizations = tuple(
                {
                    "authorization_id": authorization.get("authorization_id"),
                    "source_id": authorization.get(
                        "projection_id", authorization.get("opportunity_id")
                    ),
                    # PAPER Operations & Observability v1: an already-real,
                    # already-retained field (CryptoPaperAuthorization.
                    # strategy_or_discovery_id) this row previously dropped --
                    # the direct lineage answer to "which strategy caused
                    # this PAPER action", not just the opaque projection id.
                    "strategy_or_discovery_id": authorization.get("strategy_or_discovery_id"),
                    "expires_at": authorization.get("expires_at"),
                    "evidence_href": evidence_href(
                        "authorizations", authorization.get("authorization_id")
                    ),
                }
                for value in _sequence(state.get("authorizations"))
                if (authorization := _mapping(value))
            )
            previews = tuple(
                {
                    "preview_id": preview.get("preview_id"),
                    "expires_at": preview.get("expires_at"),
                    "accepted": not bool(_sequence(preview.get("rejection_reasons"))),
                    "maximum_risk": preview.get("maximum_loss", preview.get("estimated_notional")),
                    "modeled_fees": preview.get("modeled_fees"),
                    "evidence_href": evidence_href("previews", preview.get("preview_id")),
                }
                for value in _sequence(state.get("previews"))
                if (preview := _mapping(value))
            )

            def _mark(position_id: object) -> dict[str, object]:
                mark = _mapping(marks.get(str(position_id)))
                if mark.get("unrealized_paper_pnl") is not None:
                    return {
                        "unrealized_paper_pnl": mark.get("unrealized_paper_pnl"),
                        "mark_price": mark.get("mark_price"),
                        "marked_at": mark.get("marked_at"),
                        "mark_disposition": "SATISFIED",
                    }
                return {
                    "unrealized_paper_pnl": None,
                    "mark_price": None,
                    "marked_at": None,
                    "mark_disposition": mark.get("disposition", "NOT_EVALUATED"),
                }

            positions = tuple(
                {
                    "position_id": position.get("position_id"),
                    "instrument_id": position.get("product_id", position.get("market_ticker")),
                    "state": position.get("state"),
                    "quantity": position.get("base_quantity", position.get("contracts")),
                    "realized_paper_pnl": position.get("realized_paper_pnl"),
                    **(
                        _mark(position.get("position_id"))
                        if position.get("state") == "OPEN"
                        else {
                            "unrealized_paper_pnl": None,
                            "mark_price": None,
                            "marked_at": None,
                            "mark_disposition": "CLOSED_POSITION_NOT_MARKED",
                        }
                    ),
                    "evidence_href": evidence_href("positions", position.get("position_id")),
                }
                for value in _sequence(state.get("positions"))
                if (position := _mapping(value))
            )
            position_state_counts: dict[str, int] = {}
            for position in positions:
                position_state = str(position.get("state") or "UNKNOWN")
                position_state_counts[position_state] = (
                    position_state_counts.get(position_state, 0) + 1
                )
            settlements = tuple(
                {
                    "settlement_id": settlement.get("settlement_id"),
                    "position_id": settlement.get("position_id"),
                    "settled_at": settlement.get("settled_at"),
                    "realized_paper_pnl": settlement.get("realized_paper_pnl"),
                    "evidence_href": evidence_href("settlements", settlement.get("settlement_id")),
                }
                for value in _sequence(state.get("settlements"))
                if (settlement := _mapping(value))
            )
            reconciliations = tuple(
                sorted(
                    (
                        {
                            "reconciliation_id": reconciliation.get("reconciliation_id"),
                            "status": reconciliation.get("status"),
                            "issues": reconciliation.get(
                                "issues", reconciliation.get("discrepancies", ())
                            ),
                            "evaluated_at": reconciliation.get(
                                "evaluated_at", reconciliation.get("reconciled_at")
                            ),
                            "evidence_href": evidence_href(
                                "reconciliations", reconciliation.get("reconciliation_id")
                            ),
                        }
                        for value in _sequence(state.get("reconciliations"))
                        if (reconciliation := _mapping(value))
                    ),
                    key=lambda item: _text(item.get("evaluated_at")) or "",
                )
            )
            latest_reconciliation = reconciliations[-1] if reconciliations else None
            operations = tuple(
                operation
                for value in _sequence(state.get("operations"))
                if (operation := _mapping(value))
            )
            operations_by_type: dict[str, int] = {}
            for operation in operations:
                operation_type = str(operation.get("operation_type") or "UNKNOWN")
                operations_by_type[operation_type] = operations_by_type.get(operation_type, 0) + 1
            order_state_counts: dict[str, int] = {}
            for order in orders:
                order_state = str(order.get("state") or "UNKNOWN")
                order_state_counts[order_state] = order_state_counts.get(order_state, 0) + 1
            rows.append(
                {
                    "account_id": account_id,
                    "cash": state.get("cash", _mapping(state.get("account_state")).get("cash")),
                    "reserved_cash": state.get(
                        "reserved_cash", _mapping(state.get("account_state")).get("reserved_cash")
                    ),
                    "modeled_fees": state.get(
                        "modeled_fees", _mapping(state.get("account_state")).get("modeled_fees")
                    ),
                    "realized_paper_pnl": state.get(
                        "realized_paper_pnl",
                        _mapping(state.get("account_state")).get("realized_paper_pnl"),
                    ),
                    # Deliberately NOT summed here from the per-position
                    # values below: every value already reaching this
                    # function is pre-jsonified (Decimal -> str) by the
                    # caller, exactly like realized_paper_pnl above -- this
                    # read model must not re-derive financial totals from
                    # stringified evidence (see this function's own module
                    # docstring: "does not calculate financial truth"). This
                    # is real Decimal arithmetic performed BEFORE
                    # jsonification in operator_api.py and threaded through
                    # already computed, the same way account_state's own
                    # realized_paper_pnl already is -- ``None`` when the
                    # caller has no open, markable position for this account.
                    "unrealized_paper_pnl": _mapping(unrealized_totals).get(str(account_id)),
                    "open_position_mark_coverage": {
                        "open_count": position_state_counts.get("OPEN", 0),
                        "marked_count": sum(
                            item.get("mark_disposition") == "SATISFIED" for item in positions
                        ),
                    },
                    "authorizations": authorizations,
                    "previews": previews,
                    "orders": orders,
                    "order_state_counts": tuple(sorted(order_state_counts.items())),
                    "fills": len(_sequence(state.get("fills"))),
                    "positions": positions,
                    "position_state_counts": tuple(sorted(position_state_counts.items())),
                    "settlements": settlements,
                    "reconciliations": reconciliations,
                    "latest_reconciliation": latest_reconciliation,
                    "has_unresolved_reconciliation": bool(
                        latest_reconciliation
                        and list(_sequence(latest_reconciliation.get("issues")))
                    ),
                    # PAPER Continuation Health Signal v2: the one
                    # authoritative "is CRYPTO_PAPER_TRIAL_EXECUTION /
                    # KALSHI_PAPER_TRIAL_EXECUTION healthy right now" answer
                    # for this account, precomputed by the caller exactly
                    # like strategy_registry's own field -- see this
                    # function's own docstring. ``None`` when the caller
                    # supplied no mapping or no entry for this account_id.
                    "continuation_health": health_by_account.get(str(account_id)),
                    "operations_summary": {
                        "total": len(operations),
                        "by_type": tuple(sorted(operations_by_type.items())),
                    },
                    "evidence_href": (
                        _href("/api/v1/crypto-paper/accounts", account_id)
                        if venue == "CRYPTO"
                        else _href("/api/v1/kalshi-paper/accounts", account_id)
                    ),
                }
            )
        return {
            "venue": venue,
            "state_source": "APEX_INTERNAL_PAPER",
            "accounts": tuple(rows),
            "account_count": len(rows),
            "capability": capability,
            "empty_state": "NO_GENUINE_INTERNAL_PAPER_ACCOUNT" if not rows else None,
            "provider_submission_capability": "NONE",
            "paper_authority": "PAPER_ONLY",
            "live_authority": "NONE",
        }

    external_rows: list[dict[str, object]] = []
    for value in external_instructions:
        row = _mapping(value)
        instruction = _mapping(row.get("instruction"))
        instruction_id = instruction.get("instruction_id")
        status_base_href = _href("/api/v1/external-paper/alpaca/instructions", instruction_id)
        external_rows.append(
            {
                "instruction_id": instruction_id,
                "campaign_id": instruction.get("campaign_id"),
                "symbol": instruction.get("symbol"),
                "side": instruction.get("side"),
                "quantity": instruction.get("quantity"),
                "state": instruction.get("state"),
                "broker_order_id": instruction.get("broker_order_id"),
                "submission_attempt": row.get("submission_attempt"),
                "validated_fill_count": len(_sequence(row.get("validated_fills"))),
                "reconciliations": row.get("reconciliations", ()),
                "evidence_href": (
                    f"{status_base_href}/status" if status_base_href is not None else None
                ),
            }
        )
    internal_crypto = internal_lane("CRYPTO", crypto_accounts, crypto_capability)
    internal_kalshi = internal_lane("KALSHI", kalshi_accounts, kalshi_capability)
    internal_accounts = (
        *_sequence(internal_crypto.get("accounts")),
        *_sequence(internal_kalshi.get("accounts")),
    )
    open_states = {"SUBMITTED", "OPEN", "PARTIALLY_FILLED", "CANCEL_REQUESTED"}
    paper_summary = {
        "internal_account_count": len(internal_accounts),
        "open_order_count": sum(
            _number(count) or 0
            for account in internal_accounts
            for state_name, count in _pairs(_mapping(account).get("order_state_counts")).items()
            if state_name in open_states
        ),
        "partially_filled_order_count": sum(
            _number(_pairs(_mapping(account).get("order_state_counts")).get("PARTIALLY_FILLED"))
            or 0
            for account in internal_accounts
        ),
        "unresolved_reconciliation_count": sum(
            any(_sequence(_mapping(item).get("issues")))
            for account in internal_accounts
            for item in _sequence(_mapping(account).get("reconciliations"))
        ),
    }
    return {
        "internal_crypto": internal_crypto,
        "internal_kalshi": internal_kalshi,
        "external_alpaca": {
            "venue": "ALPACA",
            "state_source": "EXTERNAL_PAPER_EVIDENCE",
            "instructions": tuple(external_rows),
            "instruction_count": len(external_rows),
            "validated_fill_count": sum(
                _number(row["validated_fill_count"]) or 0 for row in external_rows
            ),
            "empty_state": "NO_EXTERNAL_PAPER_INSTRUCTIONS" if not external_rows else None,
            "broker_records_are_financial_truth": False,
            "paper_authority": "SEPARATELY_GOVERNED_EXTERNAL_PAPER",
            "live_authority": "NONE",
        },
        "warnings": (
            "PAPER PERFORMANCE IS NOT REAL EARNINGS",
            "INTERNAL_AND_EXTERNAL_PAPER_STATE_MUST_NOT_BE_COMBINED",
        ),
        "summary": paper_summary,
        "genuine_provider_writes_created": 0,
        "live_authority": "NONE",
        # PAPER Operations & Observability v1: the real per-cycle capability
        # gate (operator question 1 -- "is PAPER execution enabled and
        # healthy right now"), not a hardcoded value. Mirrors
        # ``build_operator_home``'s identical fold of the same retained
        # ``PaperActivationEvent`` history one level up.
        "capability_gate": {
            "state": activation.get("state"),
            "enabled": activation.get("state") == "ENABLED",
            "reason": activation.get("reason"),
            "actor": activation.get("actor"),
            "last_evaluated_at": activation.get("occurred_at"),
            "empty_state": "NO_RETAINED_PAPER_ACTIVATION_DECISION" if not activation else None,
        },
        # Real governing constants (application.strategy_paper_trial), not
        # invented -- the STRATEGY_PAPER_TRIAL_EXECUTION path's actual fee/
        # spread/slippage model. The older cross-venue PAPER path
        # (adapters.operating_cycle_tasks) uses separately-defined
        # constants of the identical value by deliberate design (see that
        # module's own comment); this surfaces the one path this milestone
        # can name without importing an adapter into this application module.
        "cost_assumptions": {
            "fee_bps": STRATEGY_TRIAL_FEE_BPS,
            "spread_bps": STRATEGY_TRIAL_SPREAD_BPS,
            "slippage_bps": STRATEGY_TRIAL_SLIPPAGE_BPS,
            "applies_to": "STRATEGY_PAPER_TRIAL_EXECUTION (genuine-strategy-crypto-paper-account)",
        },
    }


_STRATEGY_PAPER_COUNTER_PREFIXES = (
    "STRATEGY_PAPER_CANDIDATES_CONSIDERED",
    "STRATEGY_PAPER_CANDIDATES_PROMOTED",
    "STRATEGY_PAPER_TRIALS_CREATED",
    "STRATEGY_PAPER_FILLS_APPLIED",
    "STRATEGY_PAPER_ORDERS_EXPIRED",
    "STRATEGY_PAPER_RECONCILIATION",
    "STRATEGY_PAPER_ATTRIBUTIONS",
    "STRATEGY_PAPER_HEALTH_ASSESSMENTS",
    "STRATEGY_PAPER_CONTEMPORANEOUS_EVIDENCE",
    "STRATEGY_PAPER_TOTAL_MARKET_CONTEXT",
)

# A candidate rejected for one of these three reasons never reached the
# evidence-gated RESEARCH_ONLY -> PAPER_ELIGIBLE transition
# (``domain.strategy_evaluation.promote_to_paper_eligible``) this cycle --
# every other STRATEGY_PAPER_REJECTED reason (capacity/ranking/evidence/
# placement) is reached only AFTER a candidate was already promoted this
# cycle. Prefixes copied verbatim from
# ``adapters.operating_cycle_tasks._execute_strategy_paper_trial``'s own
# ``rejected.append(...)`` call sites -- never guessed, and unchanged by
# this milestone.
_STRATEGY_PAPER_PRE_PROMOTION_REJECTION_PREFIXES = (
    "INSUFFICIENT_RETAINED_CANDLE_HISTORY",
    "BACKTEST_FAILED",
    "PROMOTION_REJECTED",
)


def _parse_strategy_paper_task_warnings(
    warning_codes: Sequence[object],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Pure parser over a retained ``STRATEGY_PAPER_TRIAL_EXECUTION``
    ``OperatingTaskResult.warning_codes`` tuple -- the sorted, de-duplicated
    persisted copy of ``TaskExecution.warnings`` (``adapters.
    operating_cycle_tasks._execute_strategy_paper_trial``'s own return
    statement -- read, never changed, by this milestone; see
    ``domain.operating_cycle.OperatingTaskResult.create``). That task is
    unconditionally due every 5-minute operating cycle and already folds a
    structured, colon-delimited summary of exactly what it decided into
    this one retained field every time it runs; this recovers that existing
    structure rather than re-deriving the same facts a second way. Sorting/
    de-duplication never loses information here: every ``STRATEGY_PAPER_
    REJECTED:`` entry already embeds its own strategy_id, so two genuinely
    different candidates can never collide, and one candidate is rejected
    at most once per cycle."""

    counters: dict[str, str] = {}
    rejections: dict[str, list[str]] = {}
    for value in warning_codes:
        if not isinstance(value, str):
            continue
        if value.startswith("STRATEGY_PAPER_REJECTED:"):
            remainder = value[len("STRATEGY_PAPER_REJECTED:") :]
            strategy_id, _, reason = remainder.partition(":")
            if strategy_id:
                rejections.setdefault(strategy_id, []).append(reason or remainder)
            continue
        for prefix in _STRATEGY_PAPER_COUNTER_PREFIXES:
            if value.startswith(f"{prefix}:"):
                counters[prefix] = value[len(prefix) + 1 :]
                break
    return counters, rejections


def build_operator_paper_strategy_registry(
    *,
    candidates: Sequence[object],
    latest_task_result: object | None,
    open_trial_order_by_strategy_id: Mapping[str, object] | None = None,
    account_last_reconciled_at: object | None = None,
    continuation_health: object | None = None,
    internal_evaluation_history: Mapping[str, object] | None = None,
    trial_lineage_by_strategy_id: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """The registered STRATEGY_PAPER_TRIAL_EXECUTION strategy catalog
    (``domain.strategy_benchmark.canonical_strategy_benchmarks``) crossed
    with the most recent already-retained per-cycle evaluation.

    Never a second, independently-recomputed eligibility decision:
    ``_execute_strategy_paper_trial`` already re-evaluates every candidate
    fresh against genuinely retained evidence on every 5-minute cycle (it
    is unconditionally due, no dependency gate), so its own last retained
    result already answers "is this strategy currently eligible, and why"
    at real operating cadence. A strategy absent from this cycle's
    rejection list and not currently holding an open trial genuinely has
    no retained cycle evaluation yet (task never ran, or ran before this
    strategy existed in the canonical catalog) -- shown as such, never
    guessed.

    ``latest_task_status``/``latest_task_completed_at`` reflect the most
    recent *terminal* ``OperatingTaskResult`` only -- this task type always
    reports ``awaits_intraday_continuation=True`` on success, so
    ``ExecuteOperatingCycle`` deliberately does not persist a terminal
    result while it keeps succeeding; only a genuine exception forces one.
    A task that failed once and has since recovered and been succeeding
    silently every cycle will still show that old failure here -- by
    design, that failure is real retained history, not fabricated, and
    these two raw fields are kept exactly as before (never removed, never
    reinterpreted) for that reason.

    PAPER Continuation Health Signal v1: never read the two raw fields
    above alone to answer "is it healthy *right now*" -- that is what
    ``continuation_health`` (the caller's already-computed
    ``domain.operating_cycle.PaperContinuationHealth``, via
    ``compute_paper_continuation_health``) is *for*, and it is the
    authoritative field for that question. It combines the same terminal
    result with ``account_last_reconciled_at`` (the account's own real
    Position/Order/Reconciliation evidence, refreshed on every dispatch
    regardless of terminal-result persistence) into one status --
    ``HEALTHY``/``RECOVERED``/``FAILED``/``IDLE``/``STALE``/``UNKNOWN`` --
    so an operator no longer has to manually cross-reference two fields and
    a mental staleness threshold to avoid a false "broken" read. Both
    ``account_last_reconciled_at`` and the raw terminal fields stay
    surfaced alongside it, unchanged, so the historical failure record
    (when ``continuation_health`` reports ``RECOVERED``) remains fully
    visible, never hidden.

    Strategy Evaluation History & Promotion Lineage v1: ``internal_
    evaluation_history`` (already-jsonified, keyed by ``strategy_id`` --
    see ``application.strategy_evaluation_history.
    internal_strategy_evaluation_registry``) adds a SECOND, independent,
    durably-retained perspective per strategy row, deliberately never
    merged into or replacing the fields above:

    - ``lifecycle_state``/``eligible_this_cycle`` above answer "what did
      THIS CYCLE'S live dispatch decide" (parsed from the most recent
      terminal ``OperatingTaskResult``, unchanged by this milestone).
    - ``latest_durable_evaluation``/``evaluation_freshness`` answer "what
      does the durably retained evidence trail show" -- immutable,
      queryable independent of task-result retention/masking.
    - ``durable_current_lifecycle_state`` answers "what lifecycle state has
      an OPERATOR explicitly, durably promoted this strategy to" (Part 14):
      ``RESEARCH_ONLY`` unless at least one explicit promotion event is
      retained. This is deliberately NOT the same question as ``lifecycle_
      state`` above (which reflects whether the automatic per-cycle gate
      allowed a trade THIS cycle) -- a strategy can show ``lifecycle_
      state="PAPER_ELIGIBLE"`` (this cycle's automatic gate passed) while
      ``durable_current_lifecycle_state="RESEARCH_ONLY"`` (no operator has
      ever explicitly promoted it) simultaneously and honestly; see
      ``domain.strategy_evaluation_history``'s own module docstring.

    A strategy_id absent from ``internal_evaluation_history`` (or given a
    ``None``/empty mapping entirely) shows ``has_evaluation_history=False``
    and every durable field as ``None`` -- never guessed, matching every
    other empty-state convention in this module.

    Repeatable Strategy PAPER Evidence v1: ``trial_lineage_by_strategy_id``
    (already-jsonified, keyed by ``strategy_id`` -- see ``application.
    strategy_paper_trial_lifecycle.strategy_paper_trial_lineage``) adds a
    THIRD independent perspective, answering a question neither of the two
    above can: "has this strategy's slot produced ONE trial ever, or
    several bounded, repeated ones -- and which one is active right now?"
    ``has_open_trial``/``open_order_id`` above answer "is SOME order for
    this strategy currently open" from the raw crypto_paper ledger alone;
    they collapse multiple historical trial generations into a single
    current fact and cannot distinguish generation 1 from generation 3.
    ``active_trial``/``latest_terminal_trial``/``recent_trials`` here are
    read from the durable, generation-aware ``internal_strategy_paper_
    trial``/``internal_strategy_paper_trial_closure`` evidence instead, so
    an operator can see the SAME slot's own repeated history: each
    generation's own identity, its opened/closed timestamps, its terminal
    reason (``TRIAL_HORIZON_EXIT``/``OPENING_ORDER_EXPIRED_UNFILLED`` --
    see ``domain.strategy_paper_trial_lifecycle``'s own module docstring
    for why a horizon exit is never presented as a strategy-generated
    signal), and its realized PAPER outcome where one exists. A strategy_id
    absent from ``trial_lineage_by_strategy_id`` (or with zero retained
    trials) shows ``trial_count=0`` and both trial fields ``None`` -- never
    fabricated, matching every other empty-state convention here.
    """

    task = _mapping(latest_task_result)
    internal_history_by_strategy_id = internal_evaluation_history or {}
    trial_lineage_by_strategy = trial_lineage_by_strategy_id or {}
    # OperatingTaskResult's persisted field is "warning_codes" (renamed,
    # sorted, and de-duplicated from TaskExecution.warnings at task-result
    # construction time -- see domain.operating_cycle.OperatingTaskResult
    # .create) -- the same key application.operator_console_evidence's
    # _followup_cycle_facts already reads for an identical purpose.
    counters, rejections = _parse_strategy_paper_task_warnings(_sequence(task.get("warning_codes")))
    open_trials = open_trial_order_by_strategy_id or {}

    rows: list[dict[str, object]] = []
    for value in candidates:
        definition = _mapping(value)
        strategy_id = _text(definition.get("strategy_id")) or ""
        reasons = rejections.get(strategy_id, [])
        open_order = _mapping(open_trials.get(strategy_id))
        has_open_trial = bool(open_order)
        if has_open_trial:
            lifecycle_state: str | None = "PAPER_ELIGIBLE"
            eligible_this_cycle = True
        elif reasons:
            pre_promotion = any(
                reason.startswith(_STRATEGY_PAPER_PRE_PROMOTION_REJECTION_PREFIXES)
                for reason in reasons
            )
            lifecycle_state = "RESEARCH_ONLY" if pre_promotion else "PAPER_ELIGIBLE"
            eligible_this_cycle = False
        else:
            lifecycle_state = None
            eligible_this_cycle = False
        history = _mapping(internal_history_by_strategy_id.get(strategy_id))
        latest_durable_evaluation = _mapping(history.get("latest_evaluation")) if history else {}
        latest_promotion = _mapping(history.get("latest_promotion")) if history else {}
        recent_evaluations = _sequence(history.get("evaluations")) if history else ()
        # Repeatable Strategy PAPER Evidence v1 -- see this function's own
        # docstring for why this is a third, deliberately separate
        # perspective from has_open_trial/open_order_id above.
        lineage = _mapping(trial_lineage_by_strategy.get(strategy_id))
        active_trial_raw = _mapping(lineage.get("active_trial")) if lineage else {}
        latest_closure_raw = _mapping(lineage.get("latest_closure")) if lineage else {}
        recent_trials: list[dict[str, object]] = []
        for entry in (_sequence(lineage.get("generations")) if lineage else ())[-3:]:
            item = _mapping(entry)
            entry_trial = _mapping(item.get("trial"))
            entry_closure = _mapping(item.get("closure"))
            recent_trials.append(
                {
                    "trial_id": entry_trial.get("trial_id"),
                    "generation": entry_trial.get("generation"),
                    "opened_at": entry_trial.get("opened_at"),
                    "legacy_adopted": entry_trial.get("legacy_adopted"),
                    "closed_at": entry_closure.get("closed_at") if entry_closure else None,
                    "terminal_reason": (
                        entry_closure.get("terminal_reason") if entry_closure else None
                    ),
                    "realized_paper_pnl": (
                        entry_closure.get("realized_paper_pnl") if entry_closure else None
                    ),
                    # Trial Evidence Coverage Disclosure v1 -- see
                    # domain.strategy_paper_trial_evidence_coverage.
                    "evidence_coverage": (
                        _closure_coverage(entry_closure) if entry_closure else None
                    ),
                }
            )
        rows.append(
            {
                "strategy_id": strategy_id or None,
                "display_name": definition.get("display_name"),
                "version": definition.get("version"),
                "source_kind": definition.get("source_kind"),
                "venue": definition.get("venue"),
                "instrument_ids": definition.get("instrument_ids"),
                "rule_kind": definition.get("rule_kind"),
                "horizon_kind": definition.get("horizon_kind"),
                "lifecycle_state": lifecycle_state,
                "eligible_this_cycle": eligible_this_cycle,
                "has_open_trial": has_open_trial,
                "open_order_id": open_order.get("order_id"),
                "open_order_evidence_href": _href(
                    f"/api/v1/crypto-paper/accounts/{_text(open_order.get('account_id')) or ''}"
                    "/orders",
                    open_order.get("order_id"),
                )
                if has_open_trial
                else None,
                "blocking_reasons": tuple(reasons),
                "evaluation_empty_state": (
                    "NOT_YET_EVALUATED_THIS_TASK_HISTORY" if lifecycle_state is None else None
                ),
                # Strategy Evaluation History & Promotion Lineage v1 -- see
                # this function's own docstring for why these are a
                # deliberately separate perspective from the fields above.
                "has_evaluation_history": bool(latest_durable_evaluation),
                "durable_current_lifecycle_state": (
                    history.get("current_lifecycle_state") if history else None
                ),
                "latest_durable_evaluation": latest_durable_evaluation or None,
                "evaluation_freshness": history.get("freshness") if history else None,
                "recent_evaluations": tuple(recent_evaluations),
                "promotion_count": len(_sequence(history.get("promotions"))) if history else 0,
                "latest_promotion": latest_promotion or None,
                # Repeatable Strategy PAPER Evidence v1 -- see this
                # function's own docstring. Distinct from has_open_trial/
                # open_order_id above: this is the durable, generation-
                # aware trial-lifecycle perspective, never collapsed to a
                # single current fact.
                "trial_count": lineage.get("trial_count", 0) if lineage else 0,
                "active_trial": (
                    {
                        "trial_id": active_trial_raw.get("trial_id"),
                        "generation": active_trial_raw.get("generation"),
                        "opened_at": active_trial_raw.get("opened_at"),
                        "horizon_target_at": active_trial_raw.get("horizon_target_at"),
                        "legacy_adopted": active_trial_raw.get("legacy_adopted"),
                    }
                    if active_trial_raw
                    else None
                ),
                "latest_terminal_trial": (
                    {
                        "trial_id": latest_closure_raw.get("trial_id"),
                        "closed_at": latest_closure_raw.get("closed_at"),
                        "terminal_reason": latest_closure_raw.get("terminal_reason"),
                        "realized_paper_pnl": latest_closure_raw.get("realized_paper_pnl"),
                        "next_eligible_at": latest_closure_raw.get("next_eligible_at"),
                        # Trial Evidence Coverage Disclosure v1 -- see
                        # domain.strategy_paper_trial_evidence_coverage.
                        "evidence_coverage": _closure_coverage(latest_closure_raw),
                    }
                    if latest_closure_raw
                    else None
                ),
                "recent_trials": tuple(recent_trials),
            }
        )

    return {
        "task_type": "STRATEGY_PAPER_TRIAL_EXECUTION",
        "strategies": tuple(rows),
        "latest_task_status": task.get("status"),
        "latest_task_completed_at": task.get("completed_at"),
        "candidates_considered": counters.get("STRATEGY_PAPER_CANDIDATES_CONSIDERED"),
        "candidates_promoted": counters.get("STRATEGY_PAPER_CANDIDATES_PROMOTED"),
        "trials_created_last_cycle": counters.get("STRATEGY_PAPER_TRIALS_CREATED"),
        "reconciliation_status": counters.get("STRATEGY_PAPER_RECONCILIATION"),
        "contemporaneous_evidence_disposition": counters.get(
            "STRATEGY_PAPER_CONTEMPORANEOUS_EVIDENCE"
        ),
        "account_last_reconciled_at": account_last_reconciled_at,
        "continuation_health": continuation_health,
        "empty_state": "NO_RETAINED_STRATEGY_PAPER_CYCLE_YET" if not task else None,
        "authority": "NONE",
    }


_EXTERNAL_STRATEGY_QUALIFICATION_REASONS = {
    "RESEARCH_ONLY_INSUFFICIENT_SAMPLE": (
        "fewer evaluated observations than the required minimum sample size"
    ),
    "RESEARCH_ONLY_NONPOSITIVE_NET_RETURN": (
        "net return (after the standard cost model) is zero or negative"
    ),
    "WOULD_QUALIFY_FOR_PAPER_ELIGIBLE": (
        "meets the same sample-size and positive-net-return bar used for internal strategies"
    ),
}


def build_operator_external_strategy_registry(snapshot: JsonMap) -> dict[str, object]:
    """External Strategy Evaluation v1's own operator-facing registry --
    deliberately a SEPARATE surface from ``build_operator_paper_strategy_
    registry`` immediately above (Part 19 of the milestone this function
    belongs to). That other panel's ``lifecycle_state``/``eligible_this_
    cycle`` answer "is this one of the four fixed canonical benchmarks the
    LIVE, recurring ``STRATEGY_PAPER_TRIAL_EXECUTION`` task would place a
    genuine PAPER order for today." An operator-registered external
    strategy is never a member of that fixed catalog and is never read by
    that task (see ``domain.external_strategy``'s own module docstring for
    the full rationale) -- conflating the two surfaces here would
    misleadingly suggest a registered strategy could reach a genuine PAPER
    trial on its own. This panel only ever reports this milestone's own,
    independent registration/evaluation/promotion lineage: reaching
    ``lifecycle_state=PAPER_ELIGIBLE`` here means only that an operator
    explicitly ran ``external-strategy-promote`` -- never that a PAPER
    order was authorized, previewed, or submitted.

    ``snapshot`` is the already-jsonified output of ``application.
    external_strategy_research.external_strategy_registry_snapshot`` --
    this function only reshapes it into the console's presentation idiom
    (adding a plain-language ``qualification_reason``), matching every
    other ``build_operator_*`` function in this module."""

    rows: list[dict[str, object]] = []
    for value in _sequence(snapshot.get("strategies")):
        item = _mapping(value)
        evaluation = _mapping(item.get("latest_evaluation"))
        qualification = _text(evaluation.get("qualification")) if evaluation else None
        rows.append(
            {
                "strategy_id": item.get("strategy_id"),
                "evidence_href": _href(
                    "/api/v1/operator-console/external-strategies", item.get("strategy_id")
                ),
                "version": item.get("version"),
                "display_name": item.get("display_name"),
                "source_kind": item.get("source_kind"),
                "venue": item.get("venue"),
                "instrument_ids": item.get("instrument_ids"),
                "rule_kind": item.get("rule_kind"),
                "lifecycle_state": item.get("lifecycle_state"),
                "provenance": item.get("provenance"),
                "created_at": item.get("created_at"),
                "latest_evaluation_id": evaluation.get("evaluation_id") if evaluation else None,
                "latest_evaluated_at": evaluation.get("evaluated_at") if evaluation else None,
                "latest_as_of": evaluation.get("as_of") if evaluation else None,
                "latest_capture_id": evaluation.get("capture_id") if evaluation else None,
                "latest_sample_size": evaluation.get("sample_size") if evaluation else None,
                "latest_net_return": evaluation.get("net_return") if evaluation else None,
                "latest_gross_return": evaluation.get("gross_return") if evaluation else None,
                "latest_maximum_drawdown": evaluation.get("maximum_drawdown")
                if evaluation
                else None,
                "qualification": qualification,
                "qualification_reason": _EXTERNAL_STRATEGY_QUALIFICATION_REASONS.get(
                    qualification or ""
                ),
                "benchmark_comparisons": evaluation.get("benchmark_comparisons")
                if evaluation
                else (),
                "evaluation_empty_state": (
                    None if evaluation else "NOT_YET_EVALUATED_SINCE_REGISTRATION"
                ),
            }
        )

    return {
        "strategies": tuple(rows),
        "total": _number(snapshot.get("total")) or len(rows),
        "empty_state": "NO_EXTERNAL_STRATEGIES_REGISTERED_YET" if not rows else None,
        "authority": "NONE",
        "paper_authority": "NONE",
        "live_authority": "NONE",
    }


def build_operator_learning(performance: JsonMap) -> dict[str, object]:
    """Expose retained attribution and health without creating recommendations."""

    return {
        "policy": performance.get("policy"),
        "attributions": tuple(
            {
                **dict(_mapping(item)),
                "evidence_href": _href(
                    "/api/v1/performance-attribution/attributions",
                    _mapping(item).get("attribution_id"),
                ),
            }
            for item in _sequence(performance.get("attributions"))
        ),
        "health": tuple(
            {
                **dict(_mapping(item)),
                "evidence_href": _href(
                    "/api/v1/performance-attribution/health",
                    _mapping(item).get("health_id"),
                ),
            }
            for item in _sequence(performance.get("health"))
        ),
        "method_summaries": performance.get("method_summaries", ()),
        "venue_summaries": performance.get("venue_summaries", ()),
        "portfolio_summary": performance.get("portfolio_summary"),
        "paper_evidence_counts": performance.get("paper_evidence_counts", {}),
        "empty_state": (
            "INSUFFICIENT_SAMPLE" if not _sequence(performance.get("attributions")) else None
        ),
        "warning": "PAPER PERFORMANCE IS NOT REAL EARNINGS",
        "authority": "NONE",
        "live_authority": "NONE",
    }


def build_operator_research_operations(research: JsonMap) -> dict[str, object]:
    """Compact governed-research projection with explicit zero authority."""

    priorities = {
        _mapping(value).get("proposal_id"): _mapping(value)
        for value in _sequence(research.get("proposal_priorities"))
    }
    feasibilities = {
        _mapping(value).get("proposal_id"): _mapping(value)
        for value in _sequence(research.get("proposal_feasibilities"))
    }
    queued = {
        _mapping(_mapping(value).get("item")).get("proposal_id"): _mapping(value)
        for value in _sequence(research.get("experiment_queue"))
    }
    review_queue = {
        _mapping(_mapping(value).get("proposal")).get("proposal_id"): _mapping(value)
        for value in _sequence(research.get("review_queue"))
    }
    proposals: list[dict[str, object]] = []
    for value in _sequence(research.get("proposals")):
        retained = _mapping(value)
        proposal = _mapping(retained.get("proposal"))
        duplicate = _mapping(retained.get("duplicate_decision"))
        budget = _mapping(retained.get("budget"))
        recommendations = _sequence(retained.get("recommendations"))
        latest_recommendation = _mapping(recommendations[-1]) if recommendations else {}
        proposal_id = proposal.get("proposal_id")
        priority = priorities.get(proposal_id, {})
        feasibility = feasibilities.get(proposal_id, {})
        queue = queued.get(proposal_id, {})
        review_projection = review_queue.get(proposal_id, {})
        queue_item = _mapping(queue.get("item"))
        proposals.append(
            {
                "proposal_id": proposal_id,
                "proposal_hash": proposal.get("canonical_hash"),
                "version": proposal.get("version"),
                "agenda_item_id": proposal.get("agenda_item_id"),
                "venue": proposal.get("venue"),
                "hypothesis": proposal.get("hypothesis"),
                "expected_mechanism": proposal.get("expected_mechanism"),
                "proposed_family": proposal.get("proposed_family"),
                "definition_kind": proposal.get("definition_kind"),
                "exact_change_from_prior": proposal.get("exact_change_from_prior"),
                "required_data_ids": proposal.get("required_data_ids", ()),
                "validation_windows": proposal.get("validation_windows", ()),
                "origin": proposal.get("origin"),
                "proposal_class": priority.get("proposal_class"),
                "priority": priority,
                "feasibility_status": feasibility.get("status"),
                "feasibility_reasons": feasibility.get("reasons", ()),
                "state": retained.get("state"),
                "duplicate_classification": duplicate.get("classification"),
                "duplicate_decision_hash": duplicate.get("canonical_hash"),
                "duplicate_reasons": duplicate.get("reasons", ()),
                "approval_permitted": duplicate.get("approval_permitted", False),
                "budget": budget,
                "recommendation": latest_recommendation.get("state"),
                "recommendation_reasons": latest_recommendation.get("reasons", ()),
                "queue_id": queue_item.get("queue_id"),
                "queue_status": queue.get("status"),
                "queue_attempt_count": queue.get("attempt_count", 0),
                "estimated_public_requests": review_projection.get("estimated_public_requests", 0),
                "estimated_runtime_seconds": review_projection.get("estimated_runtime_seconds", 0),
                "evidence_href": _href("/api/v1/research-operations/proposals", proposal_id),
                "authority": "NONE",
                "paper_authorization_created": False,
                "live_authority": "NONE",
            }
        )
    agenda = tuple(
        {
            **dict(item),
            "evidence_href": _href(
                "/api/v1/research-operations/agenda",
                item.get("agenda_item_id"),
            ),
            "authority": "NONE",
        }
        for value in _sequence(research.get("agenda_items"))
        if (item := _mapping(value))
    )
    return {
        "agenda_items": agenda,
        "proposals": tuple(proposals),
        "generator_runs": research.get("generator_runs", ()),
        "review_queue": research.get("review_queue", ()),
        "experiment_queue": research.get("experiment_queue", ()),
        "follow_up_attention": research.get("follow_up_attention", ()),
        "recommendations": research.get("recommendations", ()),
        "first_generated_method_selection": research.get("first_generated_method_selection"),
        "first_generated_method_implementation": research.get(
            "first_generated_method_implementation"
        ),
        "first_generated_method_runs": research.get("first_generated_method_runs", ()),
        "generated_crypto_challenger": research.get("generated_crypto_challenger", {}),
        "agenda_count": len(agenda),
        "awaiting_review_count": sum(item.get("state") == "READY_FOR_REVIEW" for item in proposals),
        "approved_not_run_count": sum(
            item.get("state") == "APPROVED_FOR_EXPERIMENT" for item in proposals
        ),
        "paper_candidate_count": sum(
            item.get("recommendation") == "PAPER_CANDIDATE" for item in proposals
        ),
        "generated_proposal_count": sum(item.get("origin") != "OPERATOR" for item in proposals),
        "queued_experiment_count": sum(item.get("queue_status") == "QUEUED" for item in proposals),
        "running_experiment_count": sum(
            item.get("queue_status") == "RUNNING" for item in proposals
        ),
        "external_ai_model_calls_enabled": False,
        "automatic_approval": False,
        "automatic_experiment_execution": False,
        "provider_write_capability": "NONE",
        "financial_mutation_capability": "NONE",
        "authority": "NONE",
        "paper_authority": "NONE",
        "live_authority": "NONE",
    }


def build_operator_attention(
    *,
    operating: JsonMap,
    shared: JsonMap,
    cross_venue: JsonMap | None = None,
    performance: JsonMap,
    paper: JsonMap,
    research_operations: JsonMap | None = None,
) -> tuple[dict[str, object], ...]:
    """Derive a deterministic queue of retained warnings and review states."""

    values: list[dict[str, object]] = []
    research = build_operator_research_operations(_mapping(research_operations))
    for proposal_value in _sequence(research.get("proposals")):
        proposal = _mapping(proposal_value)
        state = proposal.get("state")
        if state not in {"READY_FOR_REVIEW", "APPROVED_FOR_EXPERIMENT"}:
            continue
        values.append(
            {
                "attention_id": f"RESEARCH_PROPOSAL:{proposal.get('proposal_id')}",
                "severity": "MEDIUM" if state == "APPROVED_FOR_EXPERIMENT" else "INFO",
                "category": "RESEARCH_OPERATIONS",
                "status": state,
                "code": (
                    "APPROVED_EXPERIMENT_REQUIRES_EXACT_OPERATOR_RUN"
                    if state == "APPROVED_FOR_EXPERIMENT"
                    else "RESEARCH_PROPOSAL_AWAITING_REVIEW"
                ),
                "summary": "A governed research proposal requires an exact operator action.",
                "evidence_href": proposal.get("evidence_href"),
                "source_identity": proposal.get("proposal_id"),
            }
        )
    brief = _mapping(operating.get("operating_brief"))
    brief_id = brief.get("brief_id")
    brief_href = _href("/api/v1/operating-cycle/briefs", brief_id)
    for value in _sequence(operating.get("task_results")):
        task = _mapping(value)
        status = task.get("status")
        if status not in {"FAILED", "BLOCKED", "UNAVAILABLE"}:
            continue
        values.append(
            {
                "attention_id": f"OPERATING_TASK:{task.get('task_result_id')}",
                "severity": "HIGH" if status in {"FAILED", "BLOCKED"} else "MEDIUM",
                "category": "OPERATING_TASK",
                "status": "OPEN",
                "code": task.get("error_code") or status,
                "summary": "A retained operating-cycle task did not complete normally.",
                "evidence_href": brief_href,
                "source_identity": task.get("task_result_id"),
                "created_at": task.get("completed_at"),
            }
        )
    for code in _sequence(brief.get("operator_attention")):
        values.append(
            {
                "attention_id": f"OPERATING:{code}",
                "severity": "HIGH",
                "category": "OPERATING_CYCLE",
                "status": "OPEN",
                "code": code,
                "summary": "The retained operating brief requires operator review.",
                "evidence_href": brief_href,
            }
        )
    health = _mapping(operating.get("operating_health"))
    health_href = _href("/api/v1/operating-cycle/health", health.get("health_id"))
    health_codes = (
        *_sequence(health.get("stale_data")),
        *_sequence(health.get("blocked_dependencies")),
    )
    for code in health_codes:
        values.append(
            {
                "attention_id": f"HEALTH:{code}",
                "severity": "HIGH",
                "category": "DATA_HEALTH",
                "status": "OPEN",
                "code": code,
                "summary": "Retained data is stale, unavailable, or dependency-blocked.",
                "evidence_href": health_href,
            }
        )
    for row in _sequence(shared.get("rejected_opportunities")):
        opportunity = _opportunity_row(row)
        projection_id = opportunity.get("projection_id")
        values.append(
            {
                "attention_id": f"OPPORTUNITY:{projection_id}",
                "severity": "INFO",
                "category": "OPPORTUNITY",
                "status": "EXCLUDED",
                "code": "OPPORTUNITY_NOT_ELIGIBLE",
                "summary": "A retained Opportunity is excluded by the shared eligibility policy.",
                "evidence_href": opportunity.get("evidence_href"),
            }
        )
    for value in _sequence(_mapping(cross_venue).get("explanations")):
        explanation = _mapping(value)
        if explanation.get("allocated") is not False:
            continue
        values.append(
            {
                "attention_id": f"ALLOCATION:{explanation.get('explanation_id')}",
                "severity": "INFO",
                "category": "ALLOCATION",
                "status": "EXCLUDED",
                "code": "ALLOCATION_CONSTRAINT_EXCLUDED",
                "summary": explanation.get("text"),
                "reason": explanation.get("excluded_reasons", ()),
                "evidence_href": _href(
                    "/api/v1/cross-venue-allocation/decisions",
                    _mapping(_mapping(cross_venue).get("decision")).get("decision_id"),
                ),
                "source_identity": explanation.get("projection_id"),
                "created_at": _mapping(_mapping(cross_venue).get("decision")).get("decided_at"),
            }
        )
    for item in _sequence(performance.get("health")):
        retained = _mapping(item)
        state = retained.get("state")
        if state != "HEALTHY":
            values.append(
                {
                    "attention_id": f"LEARNING:{retained.get('health_id')}",
                    "severity": "INFO",
                    "category": "LEARNING",
                    "status": "WAITING" if state == "INSUFFICIENT_SAMPLE" else "REVIEW",
                    "code": state,
                    "summary": (
                        "Strategy health is descriptive evidence and needs more "
                        "PAPER lifecycle data."
                    ),
                    "evidence_href": _href(
                        "/api/v1/performance-attribution/health", retained.get("health_id")
                    ),
                }
            )
    external = _mapping(paper.get("external_alpaca"))
    for item in _sequence(external.get("instructions")):
        retained = _mapping(item)
        reconciliations = _sequence(retained.get("reconciliations"))
        unresolved = any(
            _mapping(value).get("classification") not in {"MATCHED", "RECONCILED"}
            for value in reconciliations
        )
        if unresolved:
            values.append(
                {
                    "attention_id": f"RECONCILIATION:{retained.get('instruction_id')}",
                    "severity": "CRITICAL",
                    "category": "PAPER_RECONCILIATION",
                    "status": "OPEN",
                    "code": "EXTERNAL_PAPER_RECONCILIATION_REVIEW",
                    "summary": (
                        "External PAPER evidence has an unresolved reconciliation classification."
                    ),
                    "evidence_href": retained.get("evidence_href"),
                }
            )
    for lane_name in ("internal_crypto", "internal_kalshi"):
        lane = _mapping(paper.get(lane_name))
        for account_value in _sequence(lane.get("accounts")):
            account = _mapping(account_value)
            for order_value in _sequence(account.get("orders")):
                order = _mapping(order_value)
                state = order.get("state")
                if state not in {
                    "OPEN",
                    "PARTIALLY_FILLED",
                    "CANCEL_REQUESTED",
                    "RECONCILIATION_REQUIRED",
                }:
                    continue
                values.append(
                    {
                        "attention_id": f"PAPER_ORDER:{order.get('order_id')}",
                        "severity": ("HIGH" if state == "RECONCILIATION_REQUIRED" else "MEDIUM"),
                        "category": "PAPER_ORDER",
                        "status": "OPEN",
                        "code": state,
                        "summary": "An internal PAPER order requires lifecycle review.",
                        "evidence_href": order.get("evidence_href"),
                        "source_identity": order.get("order_id"),
                        "created_at": order.get("expires_at"),
                    }
                )
            for reconciliation_value in _sequence(account.get("reconciliations")):
                reconciliation = _mapping(reconciliation_value)
                if not _sequence(reconciliation.get("issues")):
                    continue
                values.append(
                    {
                        "attention_id": (
                            f"PAPER_RECONCILIATION:{reconciliation.get('reconciliation_id')}"
                        ),
                        "severity": "CRITICAL",
                        "category": "PAPER_RECONCILIATION",
                        "status": "OPEN",
                        "code": "RECONCILIATION_BLOCKED",
                        "summary": "Internal PAPER reconciliation retained discrepancies.",
                        "reason": reconciliation.get("issues", ()),
                        "evidence_href": reconciliation.get("evidence_href"),
                        "source_identity": reconciliation.get("reconciliation_id"),
                        "created_at": reconciliation.get("evaluated_at"),
                    }
                )
    normalized = tuple(
        {
            **item,
            "source_identity": item.get("source_identity")
            or str(item.get("attention_id", "")).partition(":")[2],
            "reason": item.get("reason", item.get("code")),
            "created_at": item.get("created_at"),
            "available_operator_action": (
                "INSPECT_EXACT_EVIDENCE" if item.get("evidence_href") else "NONE"
            ),
            "authority": "NONE",
        }
        for item in values
    )
    deduplicated = {str(item["attention_id"]): item for item in normalized}
    return tuple(
        sorted(
            deduplicated.values(),
            key=lambda item: (
                _SEVERITY_ORDER.get(str(item["severity"]), 99),
                str(item["category"]),
                str(item["attention_id"]),
            ),
        )
    )


def build_operator_home(
    *,
    operating: JsonMap,
    attention: Sequence[object],
    paper: JsonMap | None = None,
    performance: JsonMap | None = None,
    allocation: JsonMap | None = None,
) -> dict[str, object]:
    """Make the retained operating brief the sole source of the home summary."""

    brief = _mapping(operating.get("operating_brief"))
    cycle = _mapping(operating.get("latest_cycle"))
    health = _mapping(operating.get("operating_health"))
    paper_activation = _mapping(operating.get("paper_activation"))
    paper_activation_enabled = paper_activation.get("state") == "ENABLED"
    paper_summary = _mapping(_mapping(paper).get("summary"))
    performance_value = _mapping(performance)
    allocation_decision = _mapping(_mapping(allocation).get("decision"))
    health_rows = _sequence(performance_value.get("health"))
    task_results = tuple(
        {
            "task_result_id": task.get("task_result_id"),
            "task_type": task.get("task_type"),
            "status": task.get("status"),
            "changed": task.get("changed"),
            "output_identities": task.get("output_identities", ()),
            "reused_identities": task.get("reused_identities", ()),
            "warning_codes": task.get("warning_codes", ()),
            "error_code": task.get("error_code"),
            "completed_at": task.get("completed_at"),
        }
        for value in _sequence(operating.get("task_results"))
        if (task := _mapping(value))
    )
    supporting = {
        "latest_successful_refresh": health.get("latest_successful_refresh"),
        "provider_capabilities": health.get("provider_capabilities", ()),
        "paper_account_count": paper_summary.get("internal_account_count", 0),
        "paper_open_order_count": paper_summary.get("open_order_count", 0),
        "paper_partially_filled_order_count": paper_summary.get("partially_filled_order_count", 0),
        "reconciliation_issue_count": (
            _number(paper_summary.get("unresolved_reconciliation_count")) or 0
        )
        + (
            _number(
                _mapping(performance_value.get("paper_evidence_counts")).get(
                    "reconciliation_issues"
                )
            )
            or 0
        ),
        "degraded_strategy_or_method_count": sum(
            _mapping(value).get("state")
            in {"WATCH", "DEGRADED", "DATA_QUALITY_BLOCKED", "RECONCILIATION_BLOCKED"}
            for value in health_rows
        ),
        "insufficient_sample_count": sum(
            _mapping(value).get("state") == "INSUFFICIENT_SAMPLE" for value in health_rows
        ),
        "current_allocation_decision_id": allocation_decision.get("decision_id"),
        "current_retained_cash": allocation_decision.get("retained_cash"),
        "latest_cycle_changes": task_results,
    }
    if not brief:
        return {
            "summary_source": "RETAINED_OPERATING_BRIEF",
            "operating_brief": None,
            "latest_cycle": operating.get("latest_cycle"),
            "operating_health": operating.get("operating_health"),
            "attention_preview": tuple(attention[:10]),
            "empty_state": "NO_RETAINED_OPERATING_BRIEF",
            **supporting,
            "authority": "NONE",
            "paper_eligible": paper_activation_enabled,
            "paper_activation": operating.get("paper_activation"),
            "live_authority": "NONE",
        }
    opportunity_counts = _pairs(brief.get("opportunity_counts"))
    return {
        "summary_source": "RETAINED_OPERATING_BRIEF",
        "brief_id": brief.get("brief_id"),
        "brief_evidence_href": _href("/api/v1/operating-cycle/briefs", brief.get("brief_id")),
        "as_of": brief.get("as_of"),
        "overall_system_state": brief.get("overall_system_state"),
        "cycle_id": brief.get("cycle_id"),
        "cycle_state": operating.get("latest_cycle_state", cycle.get("state")),
        "task_counts": brief.get("task_counts", ()),
        "total_market_state": brief.get("total_market_state"),
        "kalshi_status": brief.get("kalshi_status"),
        "shared_opportunity_set_id": brief.get("shared_opportunity_set_id"),
        "eligible_opportunities": opportunity_counts.get("ELIGIBLE", 0),
        "blocked_opportunities": opportunity_counts.get("BLOCKED", 0),
        "rejected_opportunities": opportunity_counts.get("REJECTED", 0),
        "expired_opportunities": opportunity_counts.get("EXPIRED", 0),
        "allocation_id": brief.get("allocation_id"),
        "unallocated_cash": brief.get("unallocated_cash"),
        "stale_or_unavailable": brief.get("stale_or_unavailable", ()),
        "warnings": brief.get("warnings", ()),
        "failures": brief.get("failures", ()),
        "operating_health_state": health.get("state"),
        "operator_control": health.get("operator_control"),
        "next_due_at": health.get("next_due_at"),
        **supporting,
        "attention_count": len(attention),
        "attention_preview": tuple(attention[:10]),
        "empty_state": None,
        "authority": "NONE",
        "paper_eligible": paper_activation_enabled,
        "paper_activation": operating.get("paper_activation"),
        "live_authority": "NONE",
    }


def build_operator_broad_market(summary: JsonMap) -> dict[str, object]:
    """Read-only Console projection of the bounded broad-market reference summary.

    Mirrors ``BROAD_MARKET_REFERENCE_SUMMARY_V1`` exactly; it adds hrefs and a
    display label only.  It cannot change Shared Opportunity eligibility,
    priority, allocation, or source explanation, and grants no Campaign,
    Order, Position, reservation, journal, PAPER, or LIVE authority.
    Research-universe eligibility and provider execution capability are
    surfaced as separate fields; neither is derived from the other.
    """

    universe_id = summary.get("universe_id")
    total_considered = _number(summary.get("total_considered")) or 0
    eligible_count = _number(summary.get("research_eligible_count")) or 0
    review_count = _number(summary.get("identities_requiring_review")) or 0
    underlyings_considered = _number(summary.get("underlyings_considered")) or 0
    optionable_count = _number(summary.get("optionable_underlying_count")) or 0
    execution_known = _number(summary.get("execution_capability_known_count")) or 0
    execution_unknown = _number(summary.get("execution_capability_unknown_count")) or 0
    return {
        "reference_contract": summary.get("reference_contract"),
        "universe_id": universe_id,
        "universe_href": _href("/api/v1/broad-equity/universe", universe_id),
        "as_of_date": summary.get("as_of_date"),
        "total_considered": total_considered,
        "research_eligible_count": eligible_count,
        "excluded_count": max(0, total_considered - eligible_count),
        "identities_requiring_review": review_count,
        "underlyings_considered": underlyings_considered,
        "optionable_underlying_count": optionable_count,
        "contract_count": summary.get("contract_count"),
        "execution_capability_counts": summary.get("execution_capability_counts", ()),
        "execution_capability_known_count": execution_known,
        "execution_capability_unknown_count": execution_unknown,
        # A retained universe snapshot may match the current schema, a known
        # pre-research/execution-separation legacy schema, or (retained,
        # never dropped) neither -- these counts and the latest snapshot's
        # own compatibility never silently shrink relative to what the
        # repository actually retains. See RetainedUniverseSnapshotEvidence.
        "latest_universe_schema_compatibility": summary.get("latest_universe_schema_compatibility"),
        "universe_snapshot_count": summary.get("universe_snapshot_count", 0),
        "universe_snapshot_current_count": summary.get("universe_snapshot_current_count", 0),
        "universe_snapshot_legacy_compatible_count": summary.get(
            "universe_snapshot_legacy_compatible_count", 0
        ),
        "universe_snapshot_incompatible_count": summary.get(
            "universe_snapshot_incompatible_count", 0
        ),
        "incompatible_universe_snapshots": summary.get("incompatible_universe_snapshots", ()),
        # Whether U.S. listing coverage is Nasdaq-only, other-listed-only,
        # complete under the current official-source policy, or not yet
        # captured at all -- see _broad_market_reference_summary.
        "nasdaq_listed_source": summary.get("nasdaq_listed_source"),
        "nasdaq_other_listed_source": summary.get("nasdaq_other_listed_source"),
        "sec_company_tickers_source": summary.get("sec_company_tickers_source"),
        "listing_source_coverage": summary.get("listing_source_coverage"),
        "alpaca_credentials_available": summary.get("alpaca_credentials_available"),
        "options_source_schema_level": summary.get("options_source_schema_level"),
        "options_capture_bound_bytes": summary.get("options_capture_bound_bytes"),
        "options_capture_actual_bytes": summary.get("options_capture_actual_bytes"),
        "options_capture_raw_sha256": summary.get("options_capture_raw_sha256"),
        "options_capture_status": summary.get("options_capture_status"),
        # Stable-identity/options-relink evidence -- see docs/ARCHITECTURE.md's
        # "Stable Security Identity and Options Relink v1" section. Present
        # (as zero/None) even before that milestone's commands have ever
        # been run, so Console rendering never needs a special empty case.
        "stable_identity_count": summary.get("stable_identity_count", 0),
        "identity_resolution_count": summary.get("identity_resolution_count", 0),
        "identity_resolution_state_counts": summary.get("identity_resolution_state_counts", ()),
        "identity_ambiguous_href": "/api/v1/security-identity/ambiguous",
        "latest_option_linkage_run_id": summary.get("latest_option_linkage_run_id"),
        "option_linkage_distinct_raw_symbols": summary.get(
            "option_linkage_distinct_raw_symbols", 0
        ),
        "option_linkage_linked_count": summary.get("option_linkage_linked_count", 0),
        "option_linkage_unlinked_count": summary.get("option_linkage_unlinked_count", 0),
        "option_linkage_ambiguous_count": summary.get("option_linkage_ambiguous_count", 0),
        "legacy_vs_stable_optionable_underlying_comparison": summary.get(
            "legacy_vs_stable_optionable_underlying_comparison"
        ),
        # Historical Market Data Foundation v1 -- see docs/ARCHITECTURE.md's
        # "Historical Market Data Foundation v1" section. Present (as
        # zero/None) even before any capture plan has ever been run, so
        # Console rendering never needs a special empty case. The
        # point-in-time-universe/survivorship states are always surfaced
        # alongside the count fields -- never just a number on its own -- so
        # the "today's identities applied retroactively" limitation stays
        # visible next to whatever the latest snapshot otherwise looks like.
        "historical_capture_plan_count": summary.get("historical_capture_plan_count", 0),
        "historical_dataset_snapshot_count": summary.get("historical_dataset_snapshot_count", 0),
        "historical_quality_report_count": summary.get("historical_quality_report_count", 0),
        "historical_latest_snapshot_id": summary.get("historical_latest_snapshot_id"),
        "historical_latest_snapshot_href": _href(
            "/api/v1/securities/historical-data/snapshots",
            summary.get("historical_latest_snapshot_id"),
        ),
        "historical_latest_snapshot_completeness_state": summary.get(
            "historical_latest_snapshot_completeness_state"
        ),
        "historical_latest_snapshot_point_in_time_universe_state": summary.get(
            "historical_latest_snapshot_point_in_time_universe_state"
        ),
        "historical_latest_snapshot_survivorship_state": summary.get(
            "historical_latest_snapshot_survivorship_state"
        ),
        "historical_latest_snapshot_observation_count": summary.get(
            "historical_latest_snapshot_observation_count"
        ),
        "historical_data_warnings": summary.get("historical_data_warnings", ()),
        # Corporate Fundamental, Filing, and Event Intelligence v1 -- present
        # (as zero/None) even before any plan/capture has ever run, so
        # Console rendering never needs a special empty case. See
        # docs/ARCHITECTURE.md's "Corporate Fundamental, Filing, and Event
        # Intelligence v1" section.
        "corporate_evidence_plan_id": summary.get("corporate_evidence_plan_id"),
        "corporate_evidence_workspace_href": "/api/v1/corporate-fundamentals/workspace",
        "corporate_evidence_member_count": summary.get("corporate_evidence_member_count"),
        "corporate_evidence_excluded_missing_cik_count": summary.get(
            "corporate_evidence_excluded_missing_cik_count"
        ),
        "corporate_evidence_latest_checkpoint_id": summary.get(
            "corporate_evidence_latest_checkpoint_id"
        ),
        "corporate_evidence_latest_checkpoint_created_at": summary.get(
            "corporate_evidence_latest_checkpoint_created_at"
        ),
        "corporate_evidence_processed_issuer_count": summary.get(
            "corporate_evidence_processed_issuer_count"
        ),
        "corporate_evidence_remaining_issuer_count": summary.get(
            "corporate_evidence_remaining_issuer_count"
        ),
        "corporate_evidence_capture_complete": summary.get("corporate_evidence_capture_complete"),
        "corporate_evidence_latest_quality_report_id": summary.get(
            "corporate_evidence_latest_quality_report_id"
        ),
        "corporate_evidence_issuers_captured": summary.get("corporate_evidence_issuers_captured"),
        "corporate_evidence_point_in_time_completeness": summary.get(
            "corporate_evidence_point_in_time_completeness"
        ),
        "corporate_evidence_latest_dataset_id": summary.get("corporate_evidence_latest_dataset_id"),
        "corporate_evidence_concept_vocabulary_version": summary.get(
            "corporate_evidence_concept_vocabulary_version"
        ),
        "empty_state": "NO_RETAINED_BROAD_MARKET_UNIVERSE" if universe_id is None else None,
        "warnings": (summary.get("warning"),) if summary.get("warning") else (),
        "authority": "NONE",
    }
