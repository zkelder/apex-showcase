import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OperatorConsolePage, OperatorView, operatorNavigation } from "./operatorConsole";
import { mockApiOnce, mockApiPending } from "./test/helpers";

/** Opportunities fetches three independent read paths on mount (its own
 * filtered set, the strategy library, and -- new in this milestone -- the
 * Home brief, reused only for its stage counts). Routing each mocked
 * response by URL, rather than relying on call order, keeps this test
 * honest about what it exercises regardless of effect-declaration order.
 * Returns the underlying fetch mock so a test can also assert on the exact
 * request URLs a later interaction (e.g. changing a filter) produced. */
function stubByUrl(responses: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((input: unknown) => {
    const url = String(input);
    const match = Object.entries(responses).find(([key]) => url.includes(key));
    const data = match ? match[1] : null;
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({ schema_version: "test", capability: "TEST", data }),
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("operatorNavigation", () => {
  it("includes exactly one entry each for the five new views, with no duplicates", () => {
    const newViews = ["Decision Ledger", "Follow-Up", "Options Research", "Portfolio / Mandate", "Operations"];
    for (const view of newViews) {
      expect(operatorNavigation.filter((item) => item === view)).toHaveLength(1);
    }
    expect(new Set(operatorNavigation).size).toBe(operatorNavigation.length);
  });
});

describe("OperatorConsolePage dispatch", () => {
  const cases: Array<[OperatorView, string]> = [
    ["Decision Ledger", "Decision Ledger"],
    ["Follow-Up", "Follow-Up"],
    ["Options Research", "Options Research"],
    ["Portfolio / Mandate", "Portfolio / Mandate"],
    ["Operations", "Operations"],
  ];

  it.each(cases)("renders the %s page for view=%s, and only that page", (view, heading) => {
    mockApiPending();
    render(<OperatorConsolePage view={view} navigate={() => {}} />);
    expect(screen.getByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
  });

  it("never renders the PAPER Operations page for view='Operations' (the exact substring-match hazard the two names share)", () => {
    mockApiPending();
    render(<OperatorConsolePage view="Operations" navigate={() => {}} />);
    expect(screen.getByRole("heading", { level: 1, name: "Operations" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1, name: "PAPER Operations" })).toBeNull();
  });
});

describe("Home", () => {
  it("shows a loading-safe shell with an honest, announced loading notice before the fetch resolves", () => {
    mockApiPending();
    render(<OperatorConsolePage view="Home" navigate={() => {}} />);
    expect(screen.getByRole("heading", { level: 1, name: "Operator home" })).toBeInTheDocument();
    expect(screen.getByText("Loading retained state…")).toBeInTheDocument();
  });

  it("answers execution authority plainly, grounded in the real retained PAPER account count, once a brief exists", async () => {
    mockApiOnce({
      empty_state: null,
      overall_system_state: "NORMAL",
      operator_control: "AUTOMATIC",
      cycle_state: "IDLE",
      eligible_opportunities: 0,
      blocked_opportunities: 2,
      rejected_opportunities: 1,
      paper_account_count: 2,
      paper_open_order_count: 0,
      attention_preview: [],
      latest_cycle_changes: [],
    });
    render(<OperatorConsolePage view="Home" navigate={() => {}} />);
    await waitFor(() => expect(screen.getByText(/2 internal simulated account\(s\) retained/)).toBeInTheDocument());
    expect(screen.getByText(/NONE — not enabled in this build/)).toBeInTheDocument();
  });

  it("shows the honest empty state -- with no execution-authority panel -- when no brief is retained yet", async () => {
    mockApiOnce({ empty_state: "NO_RETAINED_BRIEF" });
    render(<OperatorConsolePage view="Home" navigate={() => {}} />);
    await waitFor(() => expect(screen.getByText("No retained operating brief")).toBeInTheDocument());
    expect(screen.queryByText(/NONE — not enabled in this build/)).toBeNull();
  });
});

describe("Opportunities", () => {
  it("shows a loading-safe shell with an honest loading notice before the fetch resolves", () => {
    mockApiPending();
    render(<OperatorConsolePage view="Opportunities" navigate={() => {}} />);
    expect(screen.getByRole("heading", { level: 1, name: "Opportunities" })).toBeInTheDocument();
    expect(screen.getByText("Loading retained state…")).toBeInTheDocument();
  });

  it("translates a known eligibility reason code into a calm phrase while always keeping the exact raw code visible", async () => {
    stubByUrl({
      "/opportunities?": {
        items: [{ projection_id: "proj-1", venue: "CRYPTO", eligibility: "REJECTED", eligibility_reasons: ["LIQUIDITY_INSUFFICIENT"], source_blockers: [] }],
        total: 1,
        empty_state: null,
      },
      "/strategy-library": { items: [], total: 0 },
      "/home": { empty_state: null, eligible_opportunities: 0, blocked_opportunities: 0, rejected_opportunities: 1, expired_opportunities: 0 },
    });
    render(<OperatorConsolePage view="Opportunities" navigate={() => {}} />);
    // projection_id is a React key only, never rendered as visible text --
    // wait on the translated reason itself, which is the point of the test.
    await waitFor(() => expect(screen.getByText(/Not enough visible market depth/)).toBeInTheDocument());
    expect(screen.getByText("LIQUIDITY_INSUFFICIENT")).toBeInTheDocument();
  });

  it("shows a stage-count strip sourced from the latest operating brief, distinct from the current filter's own total", async () => {
    stubByUrl({
      "/opportunities?": { items: [], total: 0, empty_state: "NO_RETAINED_OPPORTUNITIES" },
      "/strategy-library": { items: [], total: 0 },
      "/home": { empty_state: null, eligible_opportunities: 0, blocked_opportunities: 9, rejected_opportunities: 3, expired_opportunities: 0 },
    });
    render(<OperatorConsolePage view="Opportunities" navigate={() => {}} />);
    await waitFor(() => expect(screen.getByText("Current retained Opportunity set, from the latest operating brief")).toBeInTheDocument());
    const blockedStage = screen.getByText("blocked");
    expect(blockedStage.querySelector("strong")).toHaveTextContent("9");
  });

  it("never shows the stage-count strip when the operating brief itself has no retained state", async () => {
    stubByUrl({
      "/opportunities?": { items: [], total: 0, empty_state: "NO_RETAINED_OPPORTUNITIES" },
      "/strategy-library": { items: [], total: 0 },
      "/home": { empty_state: "NO_RETAINED_BRIEF" },
    });
    render(<OperatorConsolePage view="Opportunities" navigate={() => {}} />);
    await waitFor(() => expect(screen.getByText("No matching Opportunities")).toBeInTheDocument());
    expect(screen.queryByText("Current retained Opportunity set, from the latest operating brief")).toBeNull();
  });

  it("renders a SECURITIES Opportunity row safely, distinct from Crypto and Kalshi, without implying execution capability", async () => {
    // Securities Research -> Shared Opportunity Recurring Bridge v1 (backend
    // main, 5acb8b3) widened SharedVenue to include SECURITIES; rows flow
    // through the unfiltered "ALL" set this test exercises. Row rendering
    // is venue-generic and needs no change -- this proves it stays that way.
    //
    // Also SECURITIES Operator Console Backend Read-Model Closure v1
    // (acceptance C/D): the same row simultaneously carries
    // eligibility=ELIGIBLE, priority_tier=RESEARCH_ONLY,
    // paper_readiness=NOT_READY, and authority=NONE -- proving the propagated
    // tier is rendered without implying PAPER readiness or execution
    // authority, and without reinterpreting RESEARCH_ONLY as rejected.
    stubByUrl({
      "/opportunities?": {
        items: [
          {
            projection_id: "sec-proj-1",
            venue: "SECURITIES",
            instrument_id: "AAPL 2026-09-19 200/210 CALL SPREAD",
            eligibility: "ELIGIBLE",
            eligibility_reasons: [],
            source_blockers: [],
            priority_rank: 3,
            priority_tier: "RESEARCH_ONLY",
            priority_tier_reasons: ["BELOW_OPPORTUNITY_COST_FLOOR"],
            paper_eligible: false,
            live_eligible: false,
            authority: "NONE",
            paper_readiness: "NOT_READY",
            paper_readiness_reasons: ["VENUE_NOT_PERMITTED_BY_MANDATE"],
          },
        ],
        total: 1,
        empty_state: null,
      },
      "/strategy-library": { items: [], total: 0 },
      "/home": { empty_state: null, eligible_opportunities: 1, blocked_opportunities: 0, rejected_opportunities: 0, expired_opportunities: 0 },
    });
    render(<OperatorConsolePage view="Opportunities" navigate={() => {}} />);
    await waitFor(() => expect(screen.getByText(/SECURITIES \/ AAPL/)).toBeInTheDocument());
    // The page's own copy names Securities without claiming PAPER/LIVE eligibility.
    expect(screen.getByText(/Securities research \(defined-risk options structures\)/)).toBeInTheDocument();
    // Scope every remaining assertion to this Opportunity's own <tr> --
    // several of these exact words (e.g. "NOT_READY") are also filter
    // <option>s elsewhere on the page, so a bare screen.getByText would
    // ambiguously match those instead.
    const row = screen.getByText(/SECURITIES \/ AAPL/).closest("tr")!;
    expect(row.textContent).toMatch(/Venue not permitted by mandate/);
    // eligibility=ELIGIBLE, tier=RESEARCH_ONLY, paper_readiness=NOT_READY,
    // and authority=NONE are all simultaneously visible on the identical
    // row -- none is inferred from, or overwrites, another.
    expect(row.textContent).toContain("ELIGIBLE");
    const tierBadge = row.querySelector(".state-research_only");
    expect(tierBadge).toHaveTextContent("RESEARCH_ONLY");
    expect(row.textContent).toContain("NOT_READY");
    expect(row.textContent).toContain("NONE");
  });

  it("selecting SECURITIES in the venue filter actually requests venue=SECURITIES from the backend (acceptance F)", async () => {
    const fetchMock = stubByUrl({
      "/opportunities?": { items: [], total: 0, empty_state: "NO_RETAINED_OPPORTUNITIES" },
      "/strategy-library": { items: [], total: 0 },
      "/home": { empty_state: "NO_RETAINED_BRIEF" },
    });
    render(<OperatorConsolePage view="Opportunities" navigate={() => {}} />);
    await waitFor(() => expect(screen.getByText("No matching Opportunities")).toBeInTheDocument());

    const venueSelect = screen.getByLabelText("Venue") as HTMLSelectElement;
    // The accepted backend vocabulary is exercised end to end, not merely
    // offered: SECURITIES must be a real, selectable <option>.
    expect(Array.from(venueSelect.options).map((option) => option.value)).toContain("SECURITIES");
    // Venue (unlike the free-text query) refetches immediately on change --
    // no form submission needed, matching the page's own onChange handler.
    fireEvent.change(venueSelect, { target: { value: "SECURITIES" } });

    await waitFor(() => {
      const requestedUrls = fetchMock.mock.calls.map((call) => String(call[0]));
      expect(requestedUrls.some((url) => url.includes("/opportunities?") && url.includes("venue=SECURITIES"))).toBe(
        true,
      );
    });
  });
});

describe("Allocation", () => {
  it("shows every retained venue's total, not just Crypto and Kalshi", async () => {
    // cross_venue_allocation.py's venue_totals always reports every known
    // SharedVenue member (CRYPTO/KALSHI/SECURITIES), zero-filled when a
    // venue contributed nothing -- a fixed two-venue lookup here would
    // silently drop the third the moment one is genuinely retained.
    mockApiOnce({
      empty_state: null,
      decision: {
        hypothetical_capital: "1000.00000000",
        retained_cash: "500.00000000",
        venue_totals: [
          ["CRYPTO", "400.00000000"],
          ["KALSHI", "100.00000000"],
          ["SECURITIES", "0E-8"],
        ],
        context_state: "NORMAL",
        context_effects: [],
        modeled_downside: "100.00000000",
        modeled_costs: "0",
        long_lockup_capital: "0",
        excluded_projection_ids: [],
        live_authority: "NONE",
      },
      policy: { policy_id: "policy-1" },
      lines: [],
      explanations: [],
      paper_trial_proposals: [],
      orders_created: 0,
    });
    render(<OperatorConsolePage view="Allocation" navigate={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText("CRYPTO 400.00000000 · KALSHI 100.00000000 · SECURITIES 0E-8")).toBeInTheDocument(),
    );
  });
});

describe("navigate-focus management", () => {
  it("moves focus to the new page's own heading on a job-navigation change, but never on first mount", async () => {
    mockApiPending();
    const { rerender } = render(<OperatorConsolePage view="Decision Ledger" navigate={() => {}} />);
    expect(document.activeElement?.id).not.toBe("console-page-title");
    rerender(<OperatorConsolePage view="Follow-Up" navigate={() => {}} />);
    await waitFor(() => expect(document.activeElement?.id).toBe("console-page-title"));
    expect(document.activeElement).toHaveTextContent("Follow-Up");
  });
});

describe("PaperOperations", () => {
  // PAPER Operations & Observability v1: the exact known-misleading state
  // this milestone fixes is a hardcoded PAPER capability, and the exact
  // new capability is the STRATEGY_PAPER_TRIAL_EXECUTION strategy registry
  // -- neither had any prior test coverage on this page at all.
  it("shows the real capability gate, strategy registry, lineage, and unrealized mark for a populated cycle", async () => {
    mockApiOnce({
      capability_gate: {
        state: "ENABLED",
        enabled: true,
        reason: "operator enabled crypto strategy PAPER",
        actor: "operator:console",
        last_evaluated_at: "2026-08-27T00:00:00Z",
        empty_state: null,
      },
      strategy_registry: {
        task_type: "STRATEGY_PAPER_TRIAL_EXECUTION",
        latest_task_status: "COMPLETED",
        latest_task_completed_at: "2026-08-30T00:05:00Z",
        candidates_considered: "4",
        candidates_promoted: "1",
        trials_created_last_cycle: "0",
        reconciliation_status: "CLEAN",
        contemporaneous_evidence_disposition: "SATISFIED",
        strategies: [
          {
            strategy_id: "strategy-buy-and-hold",
            display_name: "Buy and Hold BTC-USD",
            version: 1,
            source_kind: "EXTERNAL",
            venue: "CRYPTO",
            instrument_ids: ["BTC-USD"],
            rule_kind: "BUY_AND_HOLD",
            lifecycle_state: "PAPER_ELIGIBLE",
            eligible_this_cycle: true,
            has_open_trial: true,
            open_order_id: "order-1",
            open_order_evidence_href: "/api/v1/crypto-paper/accounts/genuine-strategy-crypto-paper-account/orders/order-1",
            blocking_reasons: [],
          },
          {
            strategy_id: "strategy-mean-reversion",
            display_name: "20-Day Mean Reversion",
            version: 1,
            source_kind: "EXTERNAL",
            venue: "CRYPTO",
            instrument_ids: ["BTC-USD"],
            rule_kind: "MEAN_REVERSION",
            lifecycle_state: "RESEARCH_ONLY",
            eligible_this_cycle: false,
            has_open_trial: false,
            open_order_id: null,
            open_order_evidence_href: null,
            blocking_reasons: ["INSUFFICIENT_RETAINED_CANDLE_HISTORY"],
          },
        ],
        empty_state: null,
      },
      internal_crypto: {
        venue: "CRYPTO",
        state_source: "APEX_INTERNAL_PAPER",
        live_authority: "NONE",
        empty_state: null,
        accounts: [
          {
            account_id: "genuine-strategy-crypto-paper-account",
            cash: "875.00",
            reserved_cash: "0",
            modeled_fees: "0.30",
            realized_paper_pnl: "0",
            unrealized_paper_pnl: "12.50",
            open_position_mark_coverage: { open_count: 1, marked_count: 1 },
            authorizations: [
              {
                authorization_id: "auth-1",
                source_id: "projection-1",
                strategy_or_discovery_id: "strategy-buy-and-hold",
                expires_at: "2026-08-31T00:00:00Z",
                evidence_href: "/api/v1/crypto-paper/accounts/genuine-strategy-crypto-paper-account/authorizations/auth-1",
              },
            ],
            previews: [],
            orders: [],
            order_state_counts: [["OPEN", 1]],
            fills: 1,
            positions: [
              {
                position_id: "position-1",
                instrument_id: "BTC-USD",
                state: "OPEN",
                quantity: "0.01",
                realized_paper_pnl: "0",
                unrealized_paper_pnl: "12.50",
                mark_price: "56250",
                marked_at: "2026-08-30T00:05:00Z",
                mark_disposition: "SATISFIED",
                evidence_href: "/api/v1/crypto-paper/accounts/genuine-strategy-crypto-paper-account/positions/position-1",
              },
            ],
            position_state_counts: [["OPEN", 1]],
            settlements: [],
            reconciliations: [],
            latest_reconciliation: null,
            has_unresolved_reconciliation: false,
            operations_summary: { total: 2, by_type: [["PLACE_ORDER", 1], ["APPLY_EVIDENCE", 1]] },
            evidence_href: "/api/v1/crypto-paper/accounts/genuine-strategy-crypto-paper-account",
          },
        ],
      },
      internal_kalshi: { venue: "KALSHI", state_source: "APEX_INTERNAL_PAPER", live_authority: "NONE", empty_state: "NO_GENUINE_INTERNAL_PAPER_ACCOUNT", accounts: [] },
      external_alpaca: { venue: "ALPACA", state_source: "EXTERNAL_PAPER_EVIDENCE", live_authority: "NONE", empty_state: "NO_EXTERNAL_PAPER_INSTRUCTIONS", instructions: [] },
      cost_assumptions: { fee_bps: 60, spread_bps: 10, slippage_bps: 10, applies_to: "STRATEGY_PAPER_TRIAL_EXECUTION (genuine-strategy-crypto-paper-account)" },
      summary: { internal_account_count: 1, open_order_count: 0, partially_filled_order_count: 0, unresolved_reconciliation_count: 0 },
      genuine_provider_writes_created: 0,
      live_authority: "NONE",
    });
    render(<OperatorConsolePage view="PAPER Operations" navigate={() => {}} />);

    await waitFor(() => expect(screen.getByText(/PAPER CAPABILITY GATE: ENABLED/)).toBeInTheDocument());
    expect(screen.getByText(/operator enabled crypto strategy PAPER/)).toBeInTheDocument();

    // Strategy registry: an eligible strategy with an open trial and a
    // genuinely ineligible one, side by side, never conflated.
    const eligibleRow = screen.getByText("Buy and Hold BTC-USD").closest("tr")!;
    expect(eligibleRow.querySelector(".state-paper_eligible")).toHaveTextContent("PAPER_ELIGIBLE");
    expect(eligibleRow.textContent).toContain("yes"); // eligible_this_cycle
    expect(eligibleRow.textContent).toMatch(/Inspect evidence/);
    const ineligibleRow = screen.getByText("20-Day Mean Reversion").closest("tr")!;
    expect(ineligibleRow.querySelector(".state-research_only")).toHaveTextContent("RESEARCH_ONLY");
    expect(ineligibleRow.textContent).toMatch(/INSUFFICIENT_RETAINED_CANDLE_HISTORY/);

    // Lineage: which strategy caused this PAPER account's activity (Q11).
    expect(screen.getByText("strategy-buy-and-hold")).toBeInTheDocument();

    // Positions: real, precomputed, point-in-time-bounded unrealized P/L,
    // never a UI-side recomputation (Q9).
    expect(screen.getByText("12.50")).toBeInTheDocument();
    expect(screen.getByText(/1\/1 open positions currently priced/)).toBeInTheDocument();

    // Real transaction-cost assumptions, not invented numbers (Q10).
    expect(screen.getByText("60 bps")).toBeInTheDocument();
  });

  it("shows honest disabled/never-evaluated states without looking broken when the task has never run", async () => {
    // canonical_strategy_benchmarks() always returns the full registered
    // catalog (Q2) even when STRATEGY_PAPER_TRIAL_EXECUTION has never once
    // executed -- "which strategies are registered" and "has this ever
    // been evaluated" are two distinct honest answers, never conflated
    // into a blank table.
    mockApiOnce({
      capability_gate: { state: null, enabled: false, reason: null, actor: null, last_evaluated_at: null, empty_state: "NO_RETAINED_PAPER_ACTIVATION_DECISION" },
      strategy_registry: {
        task_type: "STRATEGY_PAPER_TRIAL_EXECUTION",
        latest_task_status: null,
        latest_task_completed_at: null,
        candidates_considered: null,
        candidates_promoted: null,
        trials_created_last_cycle: null,
        reconciliation_status: null,
        contemporaneous_evidence_disposition: null,
        strategies: [
          {
            strategy_id: "strategy-buy-and-hold",
            display_name: "Buy and Hold BTC-USD",
            version: 1,
            source_kind: "EXTERNAL",
            venue: "CRYPTO",
            instrument_ids: ["BTC-USD"],
            rule_kind: "BUY_AND_HOLD",
            lifecycle_state: null,
            eligible_this_cycle: false,
            has_open_trial: false,
            open_order_id: null,
            open_order_evidence_href: null,
            blocking_reasons: [],
            evaluation_empty_state: "NOT_YET_EVALUATED_THIS_TASK_HISTORY",
          },
        ],
        empty_state: "NO_RETAINED_STRATEGY_PAPER_CYCLE_YET",
      },
      internal_crypto: { venue: "CRYPTO", state_source: "APEX_INTERNAL_PAPER", live_authority: "NONE", empty_state: "NO_GENUINE_INTERNAL_PAPER_ACCOUNT", accounts: [] },
      internal_kalshi: { venue: "KALSHI", state_source: "APEX_INTERNAL_PAPER", live_authority: "NONE", empty_state: "NO_GENUINE_INTERNAL_PAPER_ACCOUNT", accounts: [] },
      external_alpaca: { venue: "ALPACA", state_source: "EXTERNAL_PAPER_EVIDENCE", live_authority: "NONE", empty_state: "NO_EXTERNAL_PAPER_INSTRUCTIONS", instructions: [] },
      cost_assumptions: { fee_bps: 60, spread_bps: 10, slippage_bps: 10, applies_to: "STRATEGY_PAPER_TRIAL_EXECUTION (genuine-strategy-crypto-paper-account)" },
      summary: { internal_account_count: 0, open_order_count: 0, partially_filled_order_count: 0, unresolved_reconciliation_count: 0 },
      genuine_provider_writes_created: 0,
      live_authority: "NONE",
    });
    render(<OperatorConsolePage view="PAPER Operations" navigate={() => {}} />);

    await waitFor(() => expect(screen.getByText(/PAPER CAPABILITY GATE: DISABLED/)).toBeInTheDocument());
    expect(screen.getByText(/no retained operator activation decision exists yet/)).toBeInTheDocument();
    // The strategy is still shown as registered -- never dropped -- with an
    // honest "not yet evaluated" state instead of a fabricated lifecycle.
    const row = screen.getByText("Buy and Hold BTC-USD").closest("tr")!;
    expect(row.textContent).toMatch(/not yet evaluated/i);
    expect(row.querySelector(".state-research_only, .state-paper_eligible")).toBeNull();
    expect(screen.getAllByText("No genuine retained state").length).toBeGreaterThan(0);
  });

  it("shows the honest empty state, not a crash, when zero strategies are ever registered", async () => {
    mockApiOnce({
      capability_gate: { state: "DISABLED", enabled: false, reason: "never enabled", actor: "system", last_evaluated_at: null, empty_state: null },
      strategy_registry: {
        task_type: "STRATEGY_PAPER_TRIAL_EXECUTION",
        latest_task_status: null,
        latest_task_completed_at: null,
        candidates_considered: null,
        candidates_promoted: null,
        trials_created_last_cycle: null,
        reconciliation_status: null,
        contemporaneous_evidence_disposition: null,
        strategies: [],
        empty_state: "NO_RETAINED_STRATEGY_PAPER_CYCLE_YET",
      },
      internal_crypto: { venue: "CRYPTO", state_source: "APEX_INTERNAL_PAPER", live_authority: "NONE", empty_state: "NO_GENUINE_INTERNAL_PAPER_ACCOUNT", accounts: [] },
      internal_kalshi: { venue: "KALSHI", state_source: "APEX_INTERNAL_PAPER", live_authority: "NONE", empty_state: "NO_GENUINE_INTERNAL_PAPER_ACCOUNT", accounts: [] },
      external_alpaca: { venue: "ALPACA", state_source: "EXTERNAL_PAPER_EVIDENCE", live_authority: "NONE", empty_state: "NO_EXTERNAL_PAPER_INSTRUCTIONS", instructions: [] },
      cost_assumptions: { fee_bps: 60, spread_bps: 10, slippage_bps: 10, applies_to: "STRATEGY_PAPER_TRIAL_EXECUTION (genuine-strategy-crypto-paper-account)" },
      summary: { internal_account_count: 0, open_order_count: 0, partially_filled_order_count: 0, unresolved_reconciliation_count: 0 },
      genuine_provider_writes_created: 0,
      live_authority: "NONE",
    });
    render(<OperatorConsolePage view="PAPER Operations" navigate={() => {}} />);

    await waitFor(() => expect(screen.getByText("No registered strategies")).toBeInTheDocument());
    expect(screen.getAllByText("No genuine retained state").length).toBeGreaterThan(0);
  });
});

describe("TrialOutcomePanel (Strategy PAPER Trial Outcome Assessment v1)", () => {
  it("shows a legacy PREDATES assessment and the sample floor without implying any verdict", async () => {
    mockApiOnce({
      capability_gate: { state: "ENABLED", enabled: true, reason: "operator", actor: "operator:console", last_evaluated_at: null, empty_state: null },
      strategy_registry: { task_type: "STRATEGY_PAPER_TRIAL_EXECUTION", latest_task_status: null, strategies: [] },
      internal_crypto: { venue: "CRYPTO", state_source: "APEX_INTERNAL_PAPER", live_authority: "NONE", empty_state: "NO_GENUINE_INTERNAL_PAPER_ACCOUNT", accounts: [] },
      internal_kalshi: { venue: "KALSHI", state_source: "APEX_INTERNAL_PAPER", live_authority: "NONE", empty_state: "NO_GENUINE_INTERNAL_PAPER_ACCOUNT", accounts: [] },
      external_alpaca: { venue: "ALPACA", state_source: "EXTERNAL_PAPER_EVIDENCE", live_authority: "NONE", empty_state: "NO_EXTERNAL_PAPER_INSTRUCTIONS", instructions: [] },
      trial_outcome_assessments: {
        policy_version: "STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1",
        minimum_sample_size: 30,
        assessment_count: 1,
        status_counts: { PREDATES_STRATEGY_PROVENANCE: 1 },
        provenance_bound_sample_count: 0,
        closed_trials_awaiting_assessment: [],
        recent: [
          {
            assessment_id: "strategy_trial_outcome_assessment_1",
            trial_id: "strategy_paper_trial_generation_5f0024234415d261eb976b31",
            generation: 1,
            provenance_class: "PREDATES_STRATEGY_PROVENANCE",
            evaluation_id: null,
            closed_at: "2026-09-30T00:05:31Z",
            expected: { basis: "NO_JUSTIFYING_EVALUATION_RETAINED", evaluation_net_return: null },
            realized: { economics_state: "RECONCILED", ledger_realized_pnl: "3.89", realized_return: "0.38977956" },
            status: "PREDATES_STRATEGY_PROVENANCE",
            population_size: 0,
            minimum_sample_size: 30,
          },
        ],
        authority: "NONE",
        live_authority: "NONE",
      },
      live_authority: "NONE",
    });
    render(<OperatorConsolePage view="PAPER Operations" navigate={() => {}} />);

    await waitFor(() => expect(screen.getByText(/Strategy trial outcomes/)).toBeInTheDocument());
    expect(screen.getByText("0 / 30")).toBeInTheDocument();
    expect(screen.getByText("0.38977956")).toBeInTheDocument();
    expect(screen.getByText("no justifying evaluation retained")).toBeInTheDocument();
    expect(screen.getAllByText("PREDATES_STRATEGY_PROVENANCE").length).toBeGreaterThan(0);
  });
});

describe("ExternalStrategyRegistryPanel (Research Operations)", () => {
  // External Strategy Evaluation v1: a registered external strategy's own
  // registration/evaluation/promotion lineage, deliberately a SEPARATE
  // surface from PaperOperations' StrategyRegistryPanel above -- neither
  // had any prior test coverage on the Research Operations page at all.
  const emptyResearchOperations = {
    agenda_count: 0,
    generated_proposal_count: 0,
    awaiting_review_count: 0,
    queued_experiment_count: 0,
    running_experiment_count: 0,
    paper_candidate_count: 0,
    external_ai_model_calls_enabled: false,
    provider_write_capability: "NONE",
    financial_mutation_capability: "NONE",
    agenda_items: [],
    proposals: [],
  };

  it("shows a registered strategy's lifecycle state, latest evaluation, and qualification reason", async () => {
    stubByUrl({
      "/research-operations": emptyResearchOperations,
      "/external-strategies": {
        strategies: [
          {
            strategy_id: "strategy-ma-cross",
            evidence_href: "/api/v1/operator-console/external-strategies/strategy-ma-cross",
            version: 1,
            display_name: "20/50-Day Moving Average Crossover (Manual Demo)",
            source_kind: "EXTERNAL",
            venue: "CRYPTO",
            instrument_ids: ["BTC-USD"],
            rule_kind: "MOVING_AVERAGE_TREND",
            lifecycle_state: "RESEARCH_ONLY",
            provenance: { reference_description: "A well-known moving-average crossover rule." },
            created_at: "2026-09-01T00:00:00Z",
            latest_evaluation_id: "external_strategy_evaluation_abc",
            latest_evaluated_at: "2026-09-02T00:00:00Z",
            latest_as_of: "2026-09-02T00:00:00Z",
            latest_capture_id: "btc-usd-genuine-2024-07-01-2026-07-01-v3",
            latest_sample_size: 40,
            latest_net_return: "-0.0120",
            qualification: "RESEARCH_ONLY_NONPOSITIVE_NET_RETURN",
            qualification_reason: "net return (after the standard cost model) is zero or negative",
            evaluation_empty_state: null,
          },
        ],
        total: 1,
        empty_state: null,
      },
    });
    render(<OperatorConsolePage view="Research Operations" navigate={() => {}} />);

    await waitFor(() =>
      expect(
        screen.getByText("20/50-Day Moving Average Crossover (Manual Demo)")
      ).toBeInTheDocument()
    );
    expect(screen.getByText("RESEARCH_ONLY")).toBeInTheDocument();
    expect(
      screen.getByText("net return (after the standard cost model) is zero or negative")
    ).toBeInTheDocument();
  });

  it("shows the honest empty state when no external strategy is registered yet", async () => {
    stubByUrl({
      "/research-operations": emptyResearchOperations,
      "/external-strategies": { strategies: [], total: 0, empty_state: "NO_EXTERNAL_STRATEGIES_REGISTERED_YET" },
    });
    render(<OperatorConsolePage view="Research Operations" navigate={() => {}} />);

    await waitFor(() =>
      expect(screen.getByText("No external strategies registered")).toBeInTheDocument()
    );
  });
});
