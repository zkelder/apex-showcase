import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiRecord, api, formatTimestamp } from "./api";
import {
  Empty,
  ErrorNotice,
  EvidenceButton,
  InspectEvidence,
  Loading,
  PageHead,
  Pagination,
  Panel,
  ReasonList,
  boolText,
  counts,
  list,
  pairs,
  record,
  records,
  text,
} from "./consoleKit";

// Reason/limitation codes are always shown verbatim (see ReasonList in
// consoleKit.tsx, which this page shares with the rest of the console) --
// a presentation-only lookup for a calmer phrase alongside the exact code,
// never a replacement for it, and never a new judgment about what the code
// means. This page's own known-code phrasing now lives in reasonCodes.ts
// so Opportunities, Decision Ledger, and Portfolio/Mandate read the exact
// same dictionary instead of each keeping a separate, driftable copy.

// Read-only view over DecisionFollowUpObservation evidence. V1 and V2 rows,
// and ORIGIN_EVIDENCE vs DECISION_TIME baseline-kind rows, are always shown
// as distinct, separately labeled facts -- never merged, never averaged,
// and never used to label a decision a success or a failure.

function StatusBadge({ status }: { status: unknown }) {
  return <span className={`state state-${text(status, "unknown").toLowerCase()}`}>{text(status)}</span>;
}

function ReturnCell({ item }: { item: ApiRecord }) {
  if (item.observation_type === "KALSHI_MARKET_STATUS_AND_PRICE") {
    return (
      <>
        {text(item.market_status_at_target, "unknown status")}
        <small>
          YES bid/ask {text(item.yes_bid_at_target, "missing")} / {text(item.yes_ask_at_target, "missing")}
          {item.settlement_result ? ` · settled ${text(item.settlement_result)}` : ""}
        </small>
      </>
    );
  }
  if (item.status !== "OBSERVED") {
    return <span className="muted">not yet observed</span>;
  }
  return (
    <>
      {text(item.reference_price_at_baseline, "missing")} → {text(item.reference_price_at_target, "missing")}
      <small>subsequent return {text(item.subsequent_return, "missing")}</small>
    </>
  );
}

type ComparisonRow = { decisionId: string; horizon: string; origin: ApiRecord | null; decisionTime: ApiRecord | null };

function buildComparisons(items: ApiRecord[]): ComparisonRow[] {
  const byKey = new Map<string, ComparisonRow>();
  for (const item of items) {
    if (item.status !== "OBSERVED") continue;
    const decisionId = text(item.decision_id);
    const horizon = text(item.horizon_kind);
    const key = `${decisionId}::${horizon}`;
    const existing = byKey.get(key) ?? { decisionId, horizon, origin: null, decisionTime: null };
    if (item.baseline_kind === "ORIGIN_EVIDENCE") existing.origin = item;
    if (item.baseline_kind === "DECISION_TIME") existing.decisionTime = item;
    byKey.set(key, existing);
  }
  return Array.from(byKey.values()).filter((row) => row.origin && row.decisionTime);
}

export function FollowUpPage({ inspect }: { inspect: InspectEvidence }) {
  const [policyVersion, setPolicyVersion] = useState("ALL");
  const [baselineKind, setBaselineKind] = useState("ALL");
  const [horizonKind, setHorizonKind] = useState("ALL");
  const [venue, setVenue] = useState("ALL");
  const [status, setStatus] = useState("ALL");
  const [offset, setOffset] = useState(0);
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(() => {
    const params = new URLSearchParams({
      policy_version: policyVersion,
      baseline_kind: baselineKind,
      horizon_kind: horizonKind,
      venue,
      status,
      limit: "25",
      offset: String(offset),
    });
    api<ApiRecord>(`/api/v1/operator-console/followups?${params}`)
      .then((loaded) => {
        setValue(loaded);
        setError(null);
      })
      .catch(setError);
  }, [policyVersion, baselineKind, horizonKind, venue, status, offset]);
  useEffect(refresh, [refresh]);

  const items = records(value?.items);
  const coverage = record(value?.coverage);
  const planSummary = record(value?.plan_summary);
  const progress = record(value?.progress);
  const comparisons = useMemo(() => buildComparisons(items), [items]);

  return (
    <>
      <PageHead
        eyebrow="FOLLOW-UP OBSERVATIONS"
        title="Follow-Up"
        detail={`Objective future facts about retained decisions under ${text(value?.current_policy_version)}. Evidence, not judgment: no observation here labels a decision a successful or failed rejection.`}
        refresh={refresh}
      />
      <ErrorNotice error={error} />
      <div className="notice neutral">
        V1 HAD NO ORIGIN-EVIDENCE/DECISION-TIME BASELINE AXIS · V2 ADDS IT FOR CRYPTO ONLY · KALSHI AND SECURITIES ARE ALWAYS DECISION_TIME
      </div>
      {!value && !error && <Loading />}
      <div className="research-summary-grid">
        <Panel title="Retained coverage" badge="ALL POLICY VERSIONS">
          <div className="key-values">
            <div><dt>Observations</dt><dd>{text(coverage?.observation_count, "0")}</dd></div>
            <div><dt>Decisions with ≥1</dt><dd>{text(coverage?.decisions_with_at_least_one_observation, "0")}</dd></div>
            <div><dt>Most recent capture</dt><dd>{text(coverage?.most_recent_observed_at)}</dd></div>
          </div>
        </Panel>
        <Panel title="By policy version">
          <div className="key-values">
            {counts(coverage?.observation_count_by_policy_version).map(([key, count]) => (
              <div key={key}><dt>{key}</dt><dd>{count}</dd></div>
            ))}
            {!counts(coverage?.observation_count_by_policy_version).length && <div><dt>none retained</dt><dd>0</dd></div>}
          </div>
        </Panel>
        <Panel title="By venue">
          <div className="key-values">
            {counts(coverage?.observation_count_by_venue).map(([key, count]) => (
              <div key={key}><dt>{key}</dt><dd>{count}</dd></div>
            ))}
            {!counts(coverage?.observation_count_by_venue).length && <div><dt>none retained</dt><dd>0</dd></div>}
          </div>
        </Panel>
        <Panel title="By horizon">
          <div className="key-values">
            {counts(coverage?.observation_count_by_horizon_kind).map(([key, count]) => (
              <div key={key}><dt>{key}</dt><dd>{count}</dd></div>
            ))}
            {!counts(coverage?.observation_count_by_horizon_kind).length && <div><dt>none retained</dt><dd>0</dd></div>}
          </div>
        </Panel>
        <Panel title="By baseline kind">
          <div className="key-values">
            {counts(coverage?.observation_count_by_baseline_kind).map(([key, count]) => (
              <div key={key}><dt>{key}</dt><dd>{count}</dd></div>
            ))}
            {!counts(coverage?.observation_count_by_baseline_kind).length && <div><dt>none retained</dt><dd>0</dd></div>}
          </div>
        </Panel>
        <Panel title="Current backlog" badge={text(planSummary?.policy_version)}>
          <div className="key-values">
            {pairs(planSummary?.state_counts).map(([key, count]) => (
              <div key={key}><dt>{key}</dt><dd>{count}</dd></div>
            ))}
            {!pairs(planSummary?.state_counts).length && <div><dt>none pending</dt><dd>0</dd></div>}
          </div>
        </Panel>
      </div>

      <Panel title="Follow-up progress" badge="IS IT MOVING?">
        <p className="muted">
          Evidence from the most recently completed recurring cycle and the durable scan position it left behind
          -- read only, never a live sync. Permanently-unavailable pairs cost nothing to skip and never consume
          the budgets below; they are not ordinary backlog.
        </p>
        <div className="key-values">
          <div>
            <dt>Last recurring cycle</dt>
            <dd>
              {text(progress?.last_cycle_status, "never completed yet")}
              <small>{progress?.last_cycle_completed_at ? formatTimestamp(progress.last_cycle_completed_at) : "no retained cycle"}</small>
            </dd>
          </div>
          <div>
            <dt>Genuine attempts / created</dt>
            <dd>
              {text(progress?.last_cycle_genuine_attempts, "—")} / {text(progress?.last_cycle_created, "—")}
              <small>reused (idempotent replay) {text(progress?.last_cycle_reused_idempotent, "—")}</small>
            </dd>
          </div>
          <div>
            <dt>Permanently unavailable</dt>
            <dd>
              {text(progress?.currently_permanently_unavailable, "0")} known right now
              <small>{text(progress?.last_cycle_permanently_unavailable_skipped, "0")} skipped last cycle at zero cost</small>
            </dd>
          </div>
          <div>
            <dt>Transiently blocked</dt>
            <dd>
              {text(progress?.last_cycle_transiently_blocked, "0")} last cycle
              <small>genuine attempt, may still resolve later</small>
            </dd>
          </div>
          <div>
            <dt>Candidates examined / scan bound</dt>
            <dd>
              {text(progress?.last_cycle_candidates_examined, "—")} / {text(progress?.last_cycle_candidate_scan_limit, "—")}
              <small>sample limit {text(progress?.last_cycle_sample_limit, "—")}</small>
            </dd>
          </div>
          <div>
            <dt>Kalshi request budget</dt>
            <dd>
              {text(progress?.last_cycle_provider_requests, "0")} / {text(progress?.kalshi_request_budget_per_cycle, "—")} used last cycle
              <small>
                exhausted last cycle: {boolText(progress?.kalshi_budget_exhausted_last_cycle)} · CRYPTO reads retained
                daily closes only -- zero live provider requests
              </small>
            </dd>
          </div>
          <div>
            <dt>Kalshi resolution evidence</dt>
            <dd>
              {text(progress?.kalshi_resolution_facts_retained, "0")} facts / {text(progress?.kalshi_resolution_distinct_markets_tracked, "0")} markets tracked
              <small>
                {text(progress?.kalshi_resolution_markets_unresolved, "0")} unresolved · last capture {formatTimestamp(progress?.kalshi_resolution_latest_captured_at)}
              </small>
            </dd>
          </div>
          <div>
            <dt>Kalshi baseline-quote evidence</dt>
            <dd>
              {text(progress?.kalshi_baseline_quote_rows_retained, "0")} retained
              <small>
                {text(progress?.kalshi_baseline_quote_matched_trade_rows, "0")} matched-trade · {text(progress?.kalshi_baseline_quote_market_quote_rows, "0")} market-quote · last capture{" "}
                {formatTimestamp(progress?.kalshi_baseline_quote_latest_captured_at)}
              </small>
            </dd>
          </div>
          <div>
            <dt>Scan cursor (this policy version)</dt>
            <dd>
              {text(progress?.scan_cursor_decision_id, "no completed scan yet")}
              <small>
                {progress?.scan_cursor_decision_id
                  ? `${text(progress?.scan_cursor_horizon_kind)} · ${text(progress?.scan_cursor_baseline_kind)} · advanced ${formatTimestamp(progress?.scan_cursor_updated_at)}`
                  : "the next scan starts at the oldest decision"}
              </small>
            </dd>
          </div>
          <div>
            <dt>Remaining backlog</dt>
            <dd>
              {text(progress?.currently_due, "0")} due
              <small>{text(progress?.currently_not_yet_due, "0")} not yet due</small>
            </dd>
          </div>
        </div>
      </Panel>

      {comparisons.length > 0 && (
        <Panel title="Origin-evidence vs decision-time (current page)" badge="V2 ONLY">
          <p className="muted">
            Same decision, same horizon, two independent baselines. Shown side by side for comparison only
            -- neither is computed or labeled as more correct.
          </p>
          {comparisons.map((row) => (
            <div key={`${row.decisionId}-${row.horizon}`} className="compare-grid">
              <div>
                <dt>Decision / horizon</dt>
                <dd>{row.decisionId}<small>{row.horizon}</small></dd>
              </div>
              <div>
                <dt>Baseline times</dt>
                <dd>
                  origin {text(row.origin?.baseline_time)}
                  <small>decision {text(row.decisionTime?.baseline_time)}</small>
                </dd>
              </div>
              <div>
                <dt>Origin-evidence baseline</dt>
                <dd>
                  {text(row.origin?.reference_price_at_baseline)} → {text(row.origin?.reference_price_at_target)}
                  <small>return {text(row.origin?.subsequent_return)}</small>
                </dd>
              </div>
              <div>
                <dt>Decision-time baseline</dt>
                <dd>
                  {text(row.decisionTime?.reference_price_at_baseline)} → {text(row.decisionTime?.reference_price_at_target)}
                  <small>return {text(row.decisionTime?.subsequent_return)}</small>
                </dd>
              </div>
            </div>
          ))}
        </Panel>
      )}

      <Panel title="Bounded filters">
        <form className="filters" onSubmit={(event) => event.preventDefault()}>
          <label>
            Policy version
            <select value={policyVersion} onChange={(event) => { setPolicyVersion(event.target.value); setOffset(0); }}>
              <option>ALL</option>
              <option>FOLLOWUP_POLICY_V1</option>
              <option>FOLLOWUP_POLICY_V2</option>
              <option>FOLLOWUP_POLICY_V3</option>
            </select>
          </label>
          <label>
            Baseline kind
            <select value={baselineKind} onChange={(event) => { setBaselineKind(event.target.value); setOffset(0); }}>
              <option>ALL</option>
              <option>ORIGIN_EVIDENCE</option>
              <option>DECISION_TIME</option>
            </select>
          </label>
          <label>
            Horizon kind
            <select value={horizonKind} onChange={(event) => { setHorizonKind(event.target.value); setOffset(0); }}>
              <option>ALL</option>
              <option>SHORT_HORIZON</option>
              <option>STRATEGY_HORIZON</option>
            </select>
          </label>
          {/* SECURITIES Operator Console Backend Read-Model Closure v1: this
              endpoint's own venue allow-list (application.
              operator_console_evidence._VENUES) is now derived from the
              canonical SharedVenue enum, so real SECURITIES_STRUCTURE_TERMINAL_PAYOFF
              rows are filterable directly -- ReturnCell's existing generic,
              non-Kalshi branch already renders them correctly, unchanged. */}
          <label>
            Venue
            <select value={venue} onChange={(event) => { setVenue(event.target.value); setOffset(0); }}>
              <option>ALL</option>
              <option>CRYPTO</option>
              <option>KALSHI</option>
              <option>SECURITIES</option>
            </select>
          </label>
          <label>
            Status
            <select value={status} onChange={(event) => { setStatus(event.target.value); setOffset(0); }}>
              <option>ALL</option>
              <option>OBSERVED</option>
              <option>DUE</option>
              <option>NOT_YET_DUE</option>
              <option>UNSUPPORTED</option>
              <option>BLOCKED_UNAVAILABLE</option>
              <option>PERMANENTLY_UNAVAILABLE</option>
            </select>
          </label>
        </form>
      </Panel>

      <Panel title="Follow-up rows" badge="AUTHORITY NONE">
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Decision / venue</th>
                <th scope="col">Policy / horizon / baseline</th>
                <th scope="col">Status</th>
                <th scope="col">Baseline → target time</th>
                <th scope="col">Reference / result</th>
                <th scope="col">Limitations / reasons</th>
                <th scope="col">Evidence</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => (
                <tr key={`${text(item.decision_id)}-${text(item.horizon_kind)}-${text(item.baseline_kind)}-${index}`}>
                  <td>{text(item.decision_id)}<small>{text(item.venue)} · {text(item.disposition)}</small></td>
                  <td>
                    <span className="badge compact">{text(item.policy_version)}</span>
                    <small>{text(item.horizon_kind)} · {text(item.baseline_kind)}</small>
                  </td>
                  <td><StatusBadge status={item.status} /></td>
                  <td>{text(item.baseline_time, "—")}<small>→ {text(item.target_time, "—")}</small></td>
                  <td><ReturnCell item={item} /></td>
                  <td>
                    <ReasonList codes={[...list(item.data_limitations), ...list(item.reasons)]} />
                  </td>
                  <td><EvidenceButton href={item.evidence_href} label="Decision + follow-up evidence" inspect={inspect} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!items.length && value && (
          <Empty title="No matching follow-up evidence">
            {value.empty_state === "NO_RETAINED_OR_PENDING_FOLLOWUP"
              ? "No retained observation and no pending backlog entry exists yet."
              : "No retained or pending row matches these filters."}
          </Empty>
        )}
        <Pagination offset={offset} total={Number(value?.total ?? 0)} setOffset={setOffset} />
      </Panel>
    </>
  );
}
