import { useCallback, useEffect, useState } from "react";

import { ApiRecord, api } from "./api";
import {
  Empty,
  ErrorNotice,
  InspectEvidence,
  Loading,
  PageHead,
  Pagination,
  Panel,
  list,
  records,
  text,
} from "./consoleKit";

// Cross-cycle operating-task timeline. Every status shown here is the
// operating cycle's own existing vocabulary (COMPLETED/REUSED/
// SKIPPED_NOT_DUE/SKIPPED_PAUSED/BLOCKED/UNAVAILABLE/FAILED) rendered
// verbatim -- this page computes no due/scheduling decision of its own.

const TASK_TYPES = [
  "ALL",
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
  "PORTFOLIO_SNAPSHOT_REFRESH",
  "MANDATE_EVALUATION_REBUILD",
  "PERFORMANCE_ATTRIBUTION_REBUILD",
  "RESEARCH_OPERATIONS_REFRESH",
  "COUNTERFACTUAL_FOLLOWUP_REFRESH",
  "COUNTERFACTUAL_OUTCOME_REFRESH",
  "OPERATING_BRIEF_BUILD",
] as const;

const TASK_STATUSES = [
  "ALL",
  "COMPLETED",
  "REUSED",
  "SKIPPED_NOT_DUE",
  "SKIPPED_PAUSED",
  "BLOCKED",
  "UNAVAILABLE",
  "FAILED",
] as const;

function HighlightPanel({ item }: { item: ApiRecord | undefined }) {
  if (!item) return null;
  return (
    <Panel title={text(item.task_type)} badge={text(item.current_disposition, "UNKNOWN")}>
      <div className="key-values">
        <div><dt>Current reasons</dt><dd>{list(item.current_reasons).join(" · ") || "none retained"}</dd></div>
        <div><dt>Latest result</dt><dd>{text(item.latest_result_status)}<small>{text(item.latest_result_completed_at)}</small></dd></div>
        <div><dt>Latest completed / reused</dt><dd>{text(item.latest_effective_status, "none yet")}<small>{text(item.latest_effective_completed_at)}</small></dd></div>
        <div><dt>Latest outputs</dt><dd>{list(item.latest_effective_output_identities).join(" · ") || "none"}</dd></div>
      </div>
    </Panel>
  );
}

export function OperationsPage({ inspect: _inspect }: { inspect: InspectEvidence }) {
  const [taskType, setTaskType] = useState("ALL");
  const [status, setStatus] = useState("ALL");
  const [offset, setOffset] = useState(0);
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(() => {
    const params = new URLSearchParams({ task_type: taskType, status, limit: "25", offset: String(offset) });
    api<ApiRecord>(`/api/v1/operator-console/operations?${params}`)
      .then((loaded) => {
        setValue(loaded);
        setError(null);
      })
      .catch(setError);
  }, [taskType, status, offset]);
  useEffect(refresh, [refresh]);

  const items = records(value?.items);
  const highlights = records(value?.highlights);
  const optionsHighlight = highlights.find((item) => item.task_type === "OPTIONS_RESEARCH_EVIDENCE_CAPTURE");
  const followupHighlight = highlights.find((item) => item.task_type === "COUNTERFACTUAL_FOLLOWUP_REFRESH");

  return (
    <>
      <PageHead
        eyebrow="RECURRING OPERATING CYCLE"
        title="Operations"
        detail={`Task executions across the most recently retained cycles (latest cycle ${text(value?.latest_cycle_id, "none retained")}, as of ${text(value?.latest_cycle_as_of, "unknown")}).`}
        refresh={refresh}
      />
      <ErrorNotice error={error} />
      <div className="notice neutral">RETAINED TASK RESULTS ONLY · NO SCHEDULER CONTROL · NO MANUAL TRIGGER EXISTS HERE</div>
      {!value && !error && <Loading />}

      <div className="research-summary-grid" style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
        <HighlightPanel item={optionsHighlight} />
        <HighlightPanel item={followupHighlight} />
      </div>

      <Panel title="Bounded filters">
        <form className="filters" onSubmit={(event) => event.preventDefault()}>
          <label>
            Task type
            <select value={taskType} onChange={(event) => { setTaskType(event.target.value); setOffset(0); }}>
              {TASK_TYPES.map((item) => <option key={item}>{item}</option>)}
            </select>
          </label>
          <label>
            Status
            <select value={status} onChange={(event) => { setStatus(event.target.value); setOffset(0); }}>
              {TASK_STATUSES.map((item) => <option key={item}>{item}</option>)}
            </select>
          </label>
        </form>
      </Panel>

      <Panel title="Task execution timeline" badge="NEWEST FIRST">
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Task / cycle</th>
                <th scope="col">Status</th>
                <th scope="col">Changed</th>
                <th scope="col">Outputs / reused</th>
                <th scope="col">Warnings / error</th>
                <th scope="col">Completed</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={text(item.task_result_id)}>
                  <td>{text(item.task_type)}<small>{text(item.cycle_id)}</small></td>
                  <td><span className={`state state-${text(item.status, "unknown").toLowerCase()}`}>{text(item.status)}</span></td>
                  <td>{text(item.changed)}</td>
                  <td>{list(item.output_identities).join(" · ") || "none"}<small>reused {list(item.reused_identities).join(" · ") || "none"}</small></td>
                  <td>{list(item.warning_codes).join(" · ") || text(item.error_code, "none")}</td>
                  <td>{text(item.completed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!items.length && value && (
          <Empty title="No matching task results">
            {value.empty_state === "NO_RETAINED_TASK_RESULTS"
              ? "No retained operating-cycle task result exists yet."
              : "No retained task result matches these filters."}
          </Empty>
        )}
        <Pagination offset={offset} total={Number(value?.total ?? 0)} setOffset={setOffset} />
      </Panel>
    </>
  );
}
