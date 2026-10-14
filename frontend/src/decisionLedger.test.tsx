import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DecisionLedgerPage } from "./decisionLedger";
import { mockApiOnce, mockApiPending } from "./test/helpers";

const POPULATED = {
  items: [
    {
      decision_id: "counterfactual_decision_1",
      origin: "SHARED_OPPORTUNITY_ELIGIBILITY",
      subject_projection_id: "proj-1",
      subject_source_record_type: "CryptoOpportunity",
      venue: "CRYPTO",
      evaluated_at: "2026-08-10T00:00:00Z",
      disposition: "NOT_ADVANCED",
      raw_reason_codes: ["ECONOMICS_NEGATIVE"],
      explanation: "rejected",
      classifications: [
        { classifier_version: "REASON_CLASSIFIER_V1", reason_families: ["ECONOMICS"], classified_at: "2026-08-10T01:00:00Z" },
        { classifier_version: "REASON_CLASSIFIER_V2", reason_families: ["ECONOMICS"], classified_at: "2026-08-10T01:00:00Z" },
      ],
      current_classifier_version: "REASON_CLASSIFIER_V2",
      current_reason_families: ["ECONOMICS"],
      has_current_classification: true,
      has_followup: false,
      evidence_href: "/api/v1/operator-console/decisions/counterfactual_decision_1",
    },
  ],
  total: 1,
  limit: 25,
  offset: 0,
  current_classifier_version: "REASON_CLASSIFIER_V2",
  known_classifier_versions: ["REASON_CLASSIFIER_V1", "REASON_CLASSIFIER_V2"],
  empty_state: null,
  authority: "NONE",
};

describe("DecisionLedgerPage", () => {
  it("shows a loading-safe shell before the fetch resolves", () => {
    mockApiPending();
    render(<DecisionLedgerPage inspect={() => {}} />);
    expect(screen.getByText("Decision Ledger")).toBeInTheDocument();
  });

  it("renders both classifier versions for a decision, distinctly labeled", async () => {
    mockApiOnce(POPULATED);
    render(<DecisionLedgerPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("counterfactual_decision_1")).toBeInTheDocument());
    expect(screen.getByText(/REASON_CLASSIFIER_V1/)).toBeInTheDocument();
    expect(screen.getByText(/REASON_CLASSIFIER_V2 \(current\)/)).toBeInTheDocument();
    // The stable decision's own disposition is never relabeled by
    // classification -- scoped to the row's own status badge, not the
    // filter dropdown's identically-labeled <option>.
    expect(document.querySelector(".state-not_advanced")).toHaveTextContent("NOT_ADVANCED");
  });

  it("renders the honest empty state when no decision is retained", async () => {
    mockApiOnce({ ...POPULATED, items: [], total: 0, empty_state: "NO_RETAINED_DECISIONS" });
    render(<DecisionLedgerPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("No matching decisions")).toBeInTheDocument());
  });

  it("translates a known raw reason code into a calm phrase while always keeping the exact raw code visible", async () => {
    mockApiOnce({
      ...POPULATED,
      items: [{ ...POPULATED.items[0], raw_reason_codes: ["ECONOMICS_NEGATIVE", "LIQUIDITY_INSUFFICIENT"] }],
    });
    render(<DecisionLedgerPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("counterfactual_decision_1")).toBeInTheDocument());
    // Multiple codes render as one joined phrase and one joined raw line
    // (matching the proven followUp.tsx precedent this shares its
    // component with) -- so text matching must be substring/regex, not an
    // exact match against either whole string.
    const reasonCell = screen.getByText("counterfactual_decision_1").closest("tr")!;
    // ECONOMICS_NEGATIVE has no curated dictionary entry -- it still gets a
    // mechanical (not invented) reformatting rather than showing raw.
    expect(reasonCell.textContent).toMatch(/Economics negative/);
    expect(reasonCell.textContent).toMatch(/Not enough visible market depth/);
    expect(reasonCell.textContent).toMatch(/ECONOMICS_NEGATIVE · LIQUIDITY_INSUFFICIENT/);
  });

  it("selecting SECURITIES in the venue filter actually requests venue=SECURITIES from the backend (acceptance F)", async () => {
    // Persistent (not once-only) mock: this interaction triggers a second
    // fetch after the initial mount fetch, and both must resolve.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        schema_version: "test",
        capability: "TEST",
        data: { ...POPULATED, items: [], total: 0, empty_state: "NO_RETAINED_DECISIONS" },
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<DecisionLedgerPage inspect={() => {}} />);

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

  it("exposes no mutation control", async () => {
    mockApiOnce(POPULATED);
    render(<DecisionLedgerPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("counterfactual_decision_1")).toBeInTheDocument());
    for (const label of [/approve/i, /reject/i, /^create/i, /execute/i, /cancel/i, /delete/i]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
  });
});
