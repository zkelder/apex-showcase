import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PortfolioMandatePage } from "./portfolioMandate";
import { mockApiOnce } from "./test/helpers";

const POPULATED = {
  portfolio_snapshot: { snapshot_id: "snap-1", as_of: "2026-08-14T00:00:00Z", completeness: "COMPLETE", total_known_cash: "1000", total_known_position_value: "0", total_known_portfolio_value: "1000", exposure_by_venue: [], exposure_by_market_family: [], warnings: [], limitations: [], unavailable_sources: [] },
  account_balances: [],
  positions: [],
  portfolio_empty_state: null,
  mandate: {
    mandate_id: "m1",
    version: 1,
    effective_at: "2026-08-13T00:00:00Z",
    total_capital_allocated: "1000",
    autonomy_requirement: "OPERATOR_APPROVAL_REQUIRED",
    leverage_permitted: false,
    short_exposure_permitted: false,
    options_permitted: false,
    maximum_capital_lockup_days: 30,
    permitted_venues: ["COINBASE"],
    permitted_market_families: ["CRYPTO_SPOT"],
    disallowed_market_families: [],
    maximum_aggregate_exposure_fraction: { value: "0.5" },
    maximum_per_instrument_fraction: { value: "0.2" },
    maximum_venue_concentration_fraction: { value: "0.5" },
    maximum_market_family_concentration_fraction: { value: "0.5" },
    notes: "",
  },
  mandate_versions: [],
  mandate_empty_state: null,
  latest_evaluations: [],
  latest_evaluation_count: 0,
  authority: "NONE",
};

describe("PortfolioMandatePage", () => {
  it("shows the authority-none banner and no mandate-editing control", async () => {
    mockApiOnce(POPULATED);
    render(<PortfolioMandatePage inspect={() => {}} />);
    await waitFor(() => expect(screen.getAllByText(/AUTHORITY NONE/).length).toBeGreaterThan(0));
    expect(screen.queryByRole("button", { name: /save mandate/i })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("renders real booleans as yes/no rather than an em dash", async () => {
    mockApiOnce(POPULATED);
    render(<PortfolioMandatePage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("no / no / no")).toBeInTheDocument());
  });

  it("unwraps RiskFraction-shaped values instead of showing an em dash", async () => {
    mockApiOnce(POPULATED);
    render(<PortfolioMandatePage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("0.5 / 0.2")).toBeInTheDocument());
  });

  it("translates a mandate evaluation's reason codes into calm phrases while always keeping the exact raw codes", async () => {
    mockApiOnce({
      ...POPULATED,
      latest_evaluations: [
        {
          evaluation_id: "eval-1",
          evaluated_at: "2026-08-14T00:00:00Z",
          opportunity_id: "opp-1",
          venue: "CRYPTO",
          market_family: "CRYPTO_SPOT",
          state: "REJECTED",
          reason_codes: ["MANDATE", "SOME_FUTURE_MANDATE_CODE"],
          constraints_evaluated: [],
          permitted_allocation_amount: "0",
        },
      ],
      latest_evaluation_count: 1,
    });
    render(<PortfolioMandatePage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("opp-1", { exact: false })).toBeInTheDocument());
    const row = screen.getByText("opp-1", { exact: false }).closest("tr")!;
    expect(row.textContent).toMatch(/Blocked by the current investment mandate/);
    // No curated entry for this one -- still a mechanical reformatting, not raw.
    expect(row.textContent).toMatch(/Some future mandate code/);
    expect(row.textContent).toMatch(/MANDATE · SOME_FUTURE_MANDATE_CODE/);
  });

  it("renders the honest empty state when no mandate has been authored", async () => {
    mockApiOnce({ ...POPULATED, mandate: null, mandate_empty_state: "NO_MANDATE_AUTHORED" });
    render(<PortfolioMandatePage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("No mandate authored")).toBeInTheDocument());
  });
});
