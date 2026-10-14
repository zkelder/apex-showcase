import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { OperationsPage } from "./operations";
import { mockApiOnce } from "./test/helpers";

describe("OperationsPage", () => {
  it("renders the existing task-status vocabulary verbatim", async () => {
    mockApiOnce({
      items: [
        { task_result_id: "t1", task_type: "OPTIONS_RESEARCH_EVIDENCE_CAPTURE", cycle_id: "c1", status: "SKIPPED_NOT_DUE", changed: false, output_identities: [], reused_identities: [], warning_codes: [], error_code: null, completed_at: "2026-08-13T00:00:00Z" },
        { task_result_id: "t2", task_type: "COUNTERFACTUAL_FOLLOWUP_REFRESH", cycle_id: "c1", status: "REUSED", changed: false, output_identities: [], reused_identities: [], warning_codes: [], error_code: null, completed_at: "2026-08-13T00:05:00Z" },
      ],
      total: 2,
      limit: 25,
      offset: 0,
      latest_cycle_id: "c1",
      latest_cycle_as_of: "2026-08-13T00:05:00Z",
      highlights: [
        { task_type: "OPTIONS_RESEARCH_EVIDENCE_CAPTURE", current_disposition: "NOT_DUE", current_reasons: ["ALREADY_CAPTURED_THIS_SESSION"], latest_result_status: "SKIPPED_NOT_DUE", latest_result_completed_at: "2026-08-13T00:00:00Z", latest_effective_status: "COMPLETED", latest_effective_completed_at: "2026-08-13T20:12:00Z", latest_effective_output_identities: ["checkpoint-1"] },
        { task_type: "COUNTERFACTUAL_FOLLOWUP_REFRESH", current_disposition: "DUE", current_reasons: [], latest_result_status: "REUSED", latest_result_completed_at: "2026-08-13T00:05:00Z", latest_effective_status: "REUSED", latest_effective_completed_at: "2026-08-13T00:05:00Z", latest_effective_output_identities: [] },
      ],
      empty_state: null,
      authority: "NONE",
    });
    render(<OperationsPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("SKIPPED_NOT_DUE")).toBeInTheDocument());
    expect(screen.getAllByText("REUSED").length).toBeGreaterThan(0);
    // Each task type legitimately appears three times: its own highlight
    // panel heading, the task-type filter's <option>, and a timeline row.
    expect(screen.getAllByText("OPTIONS_RESEARCH_EVIDENCE_CAPTURE").length).toBe(3);
    expect(screen.getAllByText("COUNTERFACTUAL_FOLLOWUP_REFRESH").length).toBe(3);
  });

  it("renders the honest empty state with no retained task results", async () => {
    mockApiOnce({
      items: [],
      total: 0,
      limit: 25,
      offset: 0,
      latest_cycle_id: null,
      latest_cycle_as_of: null,
      highlights: [
        { task_type: "OPTIONS_RESEARCH_EVIDENCE_CAPTURE", current_disposition: null, current_reasons: [], latest_result_status: null, latest_result_completed_at: null, latest_effective_status: null, latest_effective_completed_at: null, latest_effective_output_identities: [] },
        { task_type: "COUNTERFACTUAL_FOLLOWUP_REFRESH", current_disposition: null, current_reasons: [], latest_result_status: null, latest_result_completed_at: null, latest_effective_status: null, latest_effective_completed_at: null, latest_effective_output_identities: [] },
      ],
      empty_state: "NO_RETAINED_TASK_RESULTS",
      authority: "NONE",
    });
    render(<OperationsPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("No matching task results")).toBeInTheDocument());
  });

  it("exposes no scheduler or trigger control", async () => {
    mockApiOnce({
      items: [],
      total: 0,
      limit: 25,
      offset: 0,
      latest_cycle_id: null,
      latest_cycle_as_of: null,
      highlights: [],
      empty_state: "NO_RETAINED_TASK_RESULTS",
      authority: "NONE",
    });
    render(<OperationsPage inspect={() => {}} />);
    await waitFor(() => expect(screen.getByText("No matching task results")).toBeInTheDocument());
    for (const label of [/run now/i, /trigger/i, /pause/i, /resume/i, /^start/i]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
  });
});
