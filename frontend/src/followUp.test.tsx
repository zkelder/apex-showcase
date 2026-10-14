import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { FollowUpPage } from "./followUp";
import { mockApiOnce } from "./test/helpers";

function payload(items: unknown[], overrides: Record<string, unknown> = {}) {
  return {
    items,
    total: items.length,
    limit: 25,
    offset: 0,
    coverage: { observation_count: items.length, decisions_with_at_least_one_observation: 0, observation_count_by_policy_version: {}, observation_count_by_baseline_kind: {} },
    plan_summary: { policy_version: "FOLLOWUP_POLICY_V2", state_counts: [] },
    progress: {},
    current_policy_version: "FOLLOWUP_POLICY_V2",
    empty_state: items.length ? null : "NO_RETAINED_OR_PENDING_FOLLOWUP",
    authority: "NONE",
    ...overrides,
  };
}

describe("FollowUpPage", () => {
  it("renders the empty state when nothing is retained or pending", async () => {
    mockApiOnce(payload([]));
    render(<FollowUpPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("No matching follow-up evidence")).toBeInTheDocument());
  });

  it("selecting SECURITIES in the venue filter actually requests venue=SECURITIES from the backend (acceptance F)", async () => {
    // Persistent (not once-only) mock: this interaction triggers a second
    // fetch after the initial mount fetch, and both must resolve.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ schema_version: "test", capability: "TEST", data: payload([]) }),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<FollowUpPage inspect={() => {}} />);

    const venueSelect = screen.getByLabelText("Venue") as HTMLSelectElement;
    // The accepted backend vocabulary is exercised end to end, not merely
    // offered: SECURITIES must be a real, selectable <option>.
    expect(Array.from(venueSelect.options).map((option) => option.value)).toContain("SECURITIES");
    fireEvent.change(venueSelect, { target: { value: "SECURITIES" } });

    await waitFor(() => {
      const requestedUrls = fetchMock.mock.calls.map((call) => String(call[0]));
      expect(requestedUrls.some((url) => url.includes("venue=SECURITIES"))).toBe(true);
    });
  });

  it("keeps V1 and V2 rows visibly distinct", async () => {
    mockApiOnce(
      payload([
        { row_kind: "OBSERVED", status: "OBSERVED", decision_id: "dec-1", venue: "CRYPTO", policy_version: "FOLLOWUP_POLICY_V1", horizon_kind: "SHORT_HORIZON", baseline_kind: "DECISION_TIME", evidence_href: "/x/dec-1" },
        { row_kind: "OBSERVED", status: "OBSERVED", decision_id: "dec-2", venue: "CRYPTO", policy_version: "FOLLOWUP_POLICY_V2", horizon_kind: "SHORT_HORIZON", baseline_kind: "DECISION_TIME", evidence_href: "/x/dec-2" },
      ]),
    );
    render(<FollowUpPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("dec-1", { exact: false })).toBeInTheDocument());
    const table = within(screen.getByRole("table"));
    expect(table.getByText("FOLLOWUP_POLICY_V1")).toBeInTheDocument();
    expect(table.getByText("FOLLOWUP_POLICY_V2")).toBeInTheDocument();
  });

  it("keeps ORIGIN_EVIDENCE and DECISION_TIME baseline kinds visibly distinct", async () => {
    mockApiOnce(
      payload([
        { row_kind: "OBSERVED", status: "OBSERVED", decision_id: "dec-1", venue: "CRYPTO", policy_version: "FOLLOWUP_POLICY_V2", horizon_kind: "SHORT_HORIZON", baseline_kind: "ORIGIN_EVIDENCE", evidence_href: "/x/dec-1" },
        { row_kind: "OBSERVED", status: "OBSERVED", decision_id: "dec-1", venue: "CRYPTO", policy_version: "FOLLOWUP_POLICY_V2", horizon_kind: "SHORT_HORIZON", baseline_kind: "DECISION_TIME", evidence_href: "/x/dec-1" },
      ]),
    );
    render(<FollowUpPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getAllByText("dec-1", { exact: false }).length).toBeGreaterThan(0));
    const table = within(screen.getByRole("table"));
    expect(table.getByText(/ORIGIN_EVIDENCE/)).toBeInTheDocument();
    expect(table.getByText(/DECISION_TIME/)).toBeInTheDocument();
  });

  it("surfaces CRYPTO vs KALSHI and horizon-kind coverage breakdowns, not only policy/baseline", async () => {
    // Shaped exactly like the real repository's own coverage() return value
    // (PostgresCounterfactualFollowUpRepository.coverage(), a plain dict
    // that FastAPI/_jsonable() serializes as a JSON object) -- NOT an
    // array of [key, value] pairs. A mock using the wrong shape here once
    // let this page's "By venue"/"By horizon"/"By policy version"/"By
    // baseline kind" panels silently render "none retained" against real
    // multi-venue data while every offline test still passed; see counts()
    // in consoleKit.tsx.
    mockApiOnce(
      payload([], {
        coverage: {
          observation_count: 71,
          decisions_with_at_least_one_observation: 58,
          observation_count_by_policy_version: { FOLLOWUP_POLICY_V1: 60, FOLLOWUP_POLICY_V2: 11 },
          observation_count_by_baseline_kind: { DECISION_TIME: 65, ORIGIN_EVIDENCE: 6 },
          observation_count_by_venue: { CRYPTO: 45, KALSHI: 26 },
          observation_count_by_horizon_kind: { SHORT_HORIZON: 50, STRATEGY_HORIZON: 21 },
        },
      }),
    );
    render(<FollowUpPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("By venue")).toBeInTheDocument());
    const venueRow = screen.getByText("By venue").closest("article");
    expect(venueRow?.textContent).toContain("CRYPTO");
    expect(venueRow?.textContent).toContain("45");
    expect(venueRow?.textContent).toContain("KALSHI");
    expect(venueRow?.textContent).toContain("26");
    const horizonRow = screen.getByText("By horizon").closest("article");
    expect(horizonRow?.textContent).toContain("SHORT_HORIZON");
    expect(horizonRow?.textContent).toContain("STRATEGY_HORIZON");
  });

  it("offers PERMANENTLY_UNAVAILABLE as its own status filter, distinct from BLOCKED_UNAVAILABLE", async () => {
    mockApiOnce(payload([]));
    render(<FollowUpPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("No matching follow-up evidence")).toBeInTheDocument());
    const options = screen.getAllByRole("option").map((option) => option.textContent);
    expect(options).toContain("PERMANENTLY_UNAVAILABLE");
    expect(options).toContain("BLOCKED_UNAVAILABLE");
  });

  it("marks a PERMANENTLY_UNAVAILABLE row distinctly and explains it in calm language without hiding the exact code", async () => {
    mockApiOnce(
      payload([
        {
          row_kind: "PLANNED",
          status: "PERMANENTLY_UNAVAILABLE",
          decision_id: "dec-perm",
          venue: "CRYPTO",
          policy_version: "FOLLOWUP_POLICY_V2",
          horizon_kind: "SHORT_HORIZON",
          baseline_kind: "DECISION_TIME",
          reasons: ["CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE"],
          evidence_href: "/x/dec-perm",
        },
      ]),
    );
    render(<FollowUpPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("dec-perm", { exact: false })).toBeInTheDocument());
    const table = within(screen.getByRole("table"));
    const badge = table.getByText("PERMANENTLY_UNAVAILABLE");
    expect(badge.className).toContain("state-permanently_unavailable");
    // Calm operator language, plus the exact raw code still available.
    expect(table.getByText(/Will never resolve/)).toBeInTheDocument();
    expect(table.getByText("CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE")).toBeInTheDocument();
  });

  it("shows follow-up progress evidence from the most recent retained cycle and scan cursor", async () => {
    mockApiOnce(
      payload([], {
        progress: {
          currently_due: 4,
          currently_not_yet_due: 12,
          currently_permanently_unavailable: 40,
          last_cycle_status: "COMPLETED",
          last_cycle_completed_at: "2026-08-16T00:49:32Z",
          last_cycle_genuine_attempts: 25,
          last_cycle_created: 11,
          last_cycle_reused_idempotent: 0,
          last_cycle_transiently_blocked: 2,
          last_cycle_permanently_unavailable_skipped: 29,
          last_cycle_candidates_examined: 64,
          last_cycle_candidate_scan_limit: 2000,
          last_cycle_sample_limit: 25,
          last_cycle_provider_requests: 9,
          kalshi_request_budget_per_cycle: 20,
          kalshi_budget_exhausted_last_cycle: false,
          scan_cursor_decision_id: "dec-9",
          scan_cursor_horizon_kind: "SHORT_HORIZON",
          scan_cursor_baseline_kind: "DECISION_TIME",
          scan_cursor_updated_at: "2026-08-16T00:49:32Z",
          scan_cursor_empty_state: null,
        },
      }),
    );
    render(<FollowUpPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("Follow-up progress")).toBeInTheDocument());
    expect(screen.getByText("COMPLETED")).toBeInTheDocument();
    const attemptsRow = screen.getByText("Genuine attempts / created").closest("div");
    expect(attemptsRow?.textContent).toContain("25");
    expect(attemptsRow?.textContent).toContain("11");
    const cursorRow = screen.getByText("Scan cursor (this policy version)").closest("div");
    expect(cursorRow?.textContent).toContain("dec-9");
    const permanentRow = screen.getByText("Permanently unavailable").closest("div");
    expect(permanentRow?.textContent).toContain("40 known right now");
    // The panel's own copy must never call permanently-unavailable work ordinary backlog.
    expect(screen.getByText(/not ordinary backlog/i)).toBeInTheDocument();
  });

  it("reports an honest empty state for follow-up progress when nothing has completed yet", async () => {
    mockApiOnce(
      payload([], {
        progress: {
          currently_due: 0,
          currently_not_yet_due: 0,
          currently_permanently_unavailable: 0,
          last_cycle_status: null,
          last_cycle_completed_at: null,
          scan_cursor_decision_id: null,
          scan_cursor_empty_state: "NO_COMPLETED_SCAN_YET",
          kalshi_request_budget_per_cycle: 20,
          kalshi_budget_exhausted_last_cycle: false,
        },
      }),
    );
    render(<FollowUpPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("Follow-up progress")).toBeInTheDocument());
    expect(screen.getByText("never completed yet")).toBeInTheDocument();
    expect(screen.getByText("no completed scan yet")).toBeInTheDocument();
  });

  it("renders a SECURITIES_STRUCTURE_TERMINAL_PAYOFF row through the existing generic (non-Kalshi) branch, translating its always-present data limitations", async () => {
    // Securities Research -> Shared Opportunity Recurring Bridge v1 (backend
    // main, 5acb8b3) added this observation type. It populates the same
    // reference_price_at_baseline/reference_price_at_target/subsequent_return
    // fields CRYPTO's own rows already use -- ReturnCell's existing generic
    // (non-Kalshi) branch needs no change to render it correctly.
    mockApiOnce(
      payload([
        {
          row_kind: "OBSERVED",
          status: "OBSERVED",
          decision_id: "dec-sec-1",
          venue: "SECURITIES",
          policy_version: "FOLLOWUP_POLICY_V2",
          horizon_kind: "STRATEGY_HORIZON",
          baseline_kind: "DECISION_TIME",
          observation_type: "SECURITIES_STRUCTURE_TERMINAL_PAYOFF",
          reference_price_at_baseline: "182.40",
          reference_price_at_target: "191.10",
          subsequent_return: "0.35",
          data_limitations: [
            "SECURITIES_TERMINAL_PRICE_IS_APEX_CAPTURED_REFERENCE_PRICE_NOT_OFFICIAL_SETTLEMENT",
            "SECURITIES_SUBSEQUENT_RETURN_IS_STRUCTURE_NET_PROFIT_LOSS_OVER_CAPITAL_REQUIRED_NOT_AN_UNDERLYING_PRICE_RETURN",
          ],
          evidence_href: "/x/dec-sec-1",
        },
      ]),
    );
    render(<FollowUpPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("dec-sec-1", { exact: false })).toBeInTheDocument());
    const table = within(screen.getByRole("table"));
    expect(table.getByText(/182.40/)).toBeInTheDocument();
    expect(table.getByText(/191.10/)).toBeInTheDocument();
    expect(table.getByText(/subsequent return 0.35/)).toBeInTheDocument();
    expect(table.getByText(/Terminal price is Apex's own captured reference price/)).toBeInTheDocument();
    expect(
      table.getByText("SECURITIES_TERMINAL_PRICE_IS_APEX_CAPTURED_REFERENCE_PRICE_NOT_OFFICIAL_SETTLEMENT", { exact: false }),
    ).toBeInTheDocument();
  });

  it("never renders a computed success/failure verdict on a row itself", async () => {
    // The page's own descriptive copy explicitly disclaims this behavior
    // (and so legitimately contains the words "successful"/"failed"
    // together); this test instead scopes to the data table -- the one
    // place a computed verdict could actually appear -- and asserts it
    // never does.
    mockApiOnce(
      payload([
        { row_kind: "OBSERVED", status: "OBSERVED", decision_id: "dec-1", venue: "CRYPTO", policy_version: "FOLLOWUP_POLICY_V2", horizon_kind: "SHORT_HORIZON", baseline_kind: "DECISION_TIME", evidence_href: "/x/dec-1" },
      ]),
    );
    render(<FollowUpPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("dec-1", { exact: false })).toBeInTheDocument());
    const table = screen.getByRole("table");
    expect(table.textContent).not.toMatch(/successful|failed|correct|wrong|good decision|bad decision/i);
  });
});
