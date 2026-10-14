import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { OptionsResearchPage } from "./optionsResearch";
import { mockApiOnce } from "./test/helpers";

describe("OptionsResearchPage", () => {
  it("renders the campaign progress label from fetched state, not a hardcoded constant", async () => {
    mockApiOnce({
      campaign_sessions_captured: 1,
      campaign_sessions_target: 20,
      campaign_progress_label: "1/20",
      readiness_state: "CURRENT_SNAPSHOT_AVAILABLE",
      readiness_reasons: [],
      sessions: [],
      latest_session: null,
      latest_underlyings: [],
      capture_task: null,
      missed_session_ids: [],
      empty_state: null,
      authority: "NONE",
    });
    render(<OptionsResearchPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("1/20")).toBeInTheDocument());
  });

  it("renders a different target faithfully -- proving the '20' is not baked into the UI", async () => {
    mockApiOnce({
      campaign_sessions_captured: 3,
      campaign_sessions_target: 5,
      campaign_progress_label: "3/5",
      readiness_state: "FORWARD_HISTORY_ACCUMULATING",
      readiness_reasons: [],
      sessions: [],
      latest_session: null,
      latest_underlyings: [],
      capture_task: null,
      missed_session_ids: [],
      empty_state: null,
      authority: "NONE",
    });
    render(<OptionsResearchPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("3/5")).toBeInTheDocument());
    expect(screen.queryByText("1/20")).toBeNull();
  });

  it("renders the honest zero-session empty state", async () => {
    mockApiOnce({
      campaign_sessions_captured: 0,
      campaign_sessions_target: 20,
      campaign_progress_label: "0/20",
      readiness_state: "INSUFFICIENT_HISTORY",
      readiness_reasons: [],
      sessions: [],
      latest_session: null,
      latest_underlyings: [],
      capture_task: null,
      missed_session_ids: [],
      empty_state: "NO_RETAINED_FORWARD_SESSION",
      authority: "NONE",
    });
    render(<OptionsResearchPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("No retained forward session")).toBeInTheDocument());
  });

  it("shows genuine missed capture windows separately from banked sessions, without alarm styling text", async () => {
    mockApiOnce({
      campaign_sessions_captured: 1,
      campaign_sessions_target: 20,
      campaign_progress_label: "1/20",
      readiness_state: "CURRENT_SNAPSHOT_AVAILABLE",
      readiness_reasons: [],
      sessions: [{ session_id: "2026-08-13", checkpoint_id: "c1", underlyings_captured_count: 19, underlyings_requested_count: 20, evidence_href: "/x/c1", limitations: [], quote_quality_counts: [], chain_coverage_quality_counts: [] }],
      latest_session: { session_id: "2026-08-13", underlyings_captured_count: 19, structures_retained: 306 },
      latest_underlyings: [],
      capture_task: null,
      missed_session_ids: ["2026-08-10", "2026-08-11", "2026-08-12"],
      empty_state: null,
      authority: "NONE",
    });
    render(<OptionsResearchPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("2026-08-10")).toBeInTheDocument());
    expect(screen.getByText("Genuine missed capture windows")).toBeInTheDocument();
    expect(screen.getByText("2026-08-11")).toBeInTheDocument();
    expect(screen.getByText("2026-08-12")).toBeInTheDocument();
    expect(screen.getByText(/not an active incident/i)).toBeInTheDocument();
  });
});
