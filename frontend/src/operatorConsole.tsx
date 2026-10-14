import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { ApiRecord, api, formatTimestamp, mutate } from "./api";
import {
  AuthorityLadder,
  Empty,
  ErrorNotice,
  EvidenceButton,
  EvidenceDrawer,
  InspectEvidence,
  Loading,
  Metrics,
  PageHead,
  Pagination,
  Panel,
  ReasonList,
  StageCounts,
  boolText,
  list,
  pairs,
  record,
  records,
  stateClass,
  text,
} from "./consoleKit";
import { DecisionLedgerPage } from "./decisionLedger";
import { FollowUpPage } from "./followUp";
import { OperationsPage } from "./operations";
import { OptionsResearchPage } from "./optionsResearch";
import { PortfolioMandatePage } from "./portfolioMandate";

export const operatorNavigation = [
  "Home",
  "Attention",
  "Opportunities",
  "Decision Ledger",
  "Follow-Up",
  "Options Research",
  "Portfolio / Mandate",
  "Allocation",
  "PAPER Operations",
  "Learn",
  "Research Operations",
  "Operations",
  "Live Readiness",
  "System",
] as const;
export type OperatorView = (typeof operatorNavigation)[number];

const systemDestinations = [
  ["Operate", ["Command Center", "Overview", "Campaigns", "Portfolio", "Activity"]],
  ["Research", ["Broad Market", "Crypto Research", "Options Intelligence", "Strategies", "Strategy Lab", "Research Integrity"]],
  ["Specialists", ["Portfolio Intelligence", "Strategy Intelligence", "Ensemble and Meta-Strategy", "Securities", "Securities Operations"]],
  ["Controls and evidence", ["Crypto PAPER", "Kalshi Operating", "External PAPER", "Evidence Reviews", "Shadow Operations"]],
] as const;

function Home({ inspect }: { inspect: InspectEvidence }) {
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(() => { api<ApiRecord>("/api/v1/operator-console/home").then((loaded) => { setValue(loaded); setError(null); }).catch(setError); }, []);
  useEffect(refresh, [refresh]);
  const attention = records(value?.attention_preview);
  const changes = records(value?.latest_cycle_changes);
  const decisionSummary = record(value?.decision_ledger_summary);
  const followupSummary = record(value?.followup_summary);
  const followupCoverage = record(followupSummary?.coverage);
  const optionsSummary = record(value?.options_research_summary);
  const optionsLatestSession = record(optionsSummary?.latest_session);
  const portfolioSummary = record(value?.portfolio_mandate_summary);
  return <><PageHead eyebrow="RETAINED OPERATING BRIEF" title="Operator home" detail="One current brief for system state, due work, research Opportunity status, and operator attention. Supporting PAPER and health counts are retained read projections; the browser recalculates no financial state." refresh={refresh} /><ErrorNotice error={error} /><div className="notice neutral">SUMMARY SOURCE: RETAINED_OPERATING_BRIEF · AUTHORITY NONE · NO AUTOMATIC PAPER OPERATION · LIVE AUTHORITY NONE</div>{!value && !error && <Loading />}{value?.empty_state ? <Empty title="No retained operating brief">Run the bounded operating cycle explicitly to retain a brief. This console does not schedule or start work automatically.</Empty> : value && <><Panel title="Execution authority" badge="30-SECOND ANSWER"><AuthorityLadder paperAccountCount={value.paper_account_count} /></Panel><Metrics values={[["System / control", `${text(value.overall_system_state)} / ${text(value.operator_control)}`], ["Cycle", value.cycle_state], ["Last successful refresh", formatTimestamp(value.latest_successful_refresh)], ["Eligible Opportunities", value.eligible_opportunities], ["Blocked / rejected", `${text(value.blocked_opportunities, "0")} / ${text(value.rejected_opportunities, "0")}`], ["Unallocated research cash", value.unallocated_cash, "Hypothetical $1,000 budget"], ["Internal PAPER accounts / open orders", `${text(value.paper_account_count, "0")} / ${text(value.paper_open_order_count, "0")}`], ["Reconciliation issues", value.reconciliation_issue_count], ["Degraded / insufficient methods", `${text(value.degraded_strategy_or_method_count, "0")} / ${text(value.insufficient_sample_count, "0")}`], ["Market context", value.total_market_state], ["Kalshi refresh", value.kalshi_status], ["Health / attention", `${text(value.operating_health_state)} / ${text(value.attention_count)}`]]} /><Panel title="Operating brief" badge="AUTHORITY NONE"><div className="key-values"><div><dt>Brief / cycle</dt><dd>{text(value.brief_id)}<small>{text(value.cycle_id)}</small></dd></div><div><dt>As of</dt><dd>{formatTimestamp(value.as_of)}</dd></div><div><dt>Next due</dt><dd>{formatTimestamp(value.next_due_at)}</dd></div><div><dt>Task counts</dt><dd>{pairs(value.task_counts).map(([key, count]) => `${key} ${count}`).join(" · ") || "none"}</dd></div><div><dt>Provider limitations</dt><dd>{pairs(value.provider_capabilities).map(([key, state]) => `${key} ${state}`).join(" · ") || "none retained"}</dd></div><div><dt>Stale / unavailable</dt><dd>{list(value.stale_or_unavailable).join(" · ") || "none"}</dd></div><div><dt>Warnings</dt><dd>{list(value.warnings).join(" · ") || "none"}</dd></div></div><div className="control-row"><EvidenceButton href={value.brief_evidence_href} label="Operating brief" inspect={inspect} /></div></Panel><div className="research-summary-grid"><Panel title="Decision ledger" badge="COUNTERFACTUAL"><div className="key-values"><div><dt>Ledger decisions</dt><dd>{text(decisionSummary?.ledger_eligibility_decision_count, "0")} eligibility<small>{text(decisionSummary?.ledger_mandate_evaluation_count, "0")} mandate</small></dd></div><div><dt>Sync coverage</dt><dd>{text(decisionSummary?.eligibility_coverage_complete)} / {text(decisionSummary?.mandate_coverage_complete)}</dd></div></div></Panel><Panel title="Follow-up" badge={text(followupSummary?.current_policy_version, "—")}><div className="key-values"><div><dt>Observed</dt><dd>{text(followupCoverage?.observation_count, "0")}<small>decisions with ≥1 {text(followupCoverage?.decisions_with_at_least_one_observation, "0")}</small></dd></div><div><dt>Current backlog</dt><dd>{pairs(followupSummary?.current_backlog_state_counts).map(([key, count]) => `${key} ${count}`).join(" · ") || "none"}</dd></div><div><dt>As of</dt><dd>{formatTimestamp(followupSummary?.as_of)}</dd></div><div><dt>Last cycle</dt><dd>{text(followupSummary?.last_cycle_status, "never completed yet")}<small>created {text(followupSummary?.last_cycle_created, "—")} · permanently unavailable {text(followupSummary?.currently_permanently_unavailable, "0")} (zero-cost, not backlog)</small></dd></div></div></Panel><Panel title="Options research" badge={text(optionsSummary?.readiness_state, "—")}><div className="key-values"><div><dt>Forward campaign</dt><dd className="numeric">{text(optionsSummary?.campaign_progress_label, "0/0")}</dd></div><div><dt>Latest session</dt><dd>{text(optionsLatestSession?.session_id, "none retained")}<small>{text(optionsLatestSession?.underlyings_captured_count, "0")} underlyings · {text(optionsLatestSession?.structures_retained, "0")} structures</small></dd></div></div></Panel><Panel title="Portfolio / mandate" badge="AUTHORITY NONE"><div className="key-values"><div><dt>Portfolio as of</dt><dd>{formatTimestamp(portfolioSummary?.portfolio_as_of)}<small>{text(portfolioSummary?.portfolio_completeness, "unknown")}</small></dd></div><div><dt>Mandate version</dt><dd>{text(portfolioSummary?.mandate_version, "none authored")}<small>effective {formatTimestamp(portfolioSummary?.mandate_effective_at)}</small></dd></div><div><dt>Latest evaluations</dt><dd>{text(portfolioSummary?.latest_evaluation_count, "0")}</dd></div></div></Panel></div><Panel title="Latest operating-cycle changes" badge="RETAINED RESULTS"><div className="scroll"><table><thead><tr><th scope="col">Task</th><th scope="col">Status</th><th scope="col">Changed</th><th scope="col">Outputs / reused</th><th scope="col">Warnings</th><th scope="col">Completed</th></tr></thead><tbody>{changes.map((item) => <tr key={text(item.task_result_id)}><td>{text(item.task_type)}</td><td>{text(item.status)}</td><td>{text(item.changed)}</td><td>{list(item.output_identities).join(" · ") || "none"}<small>reused {list(item.reused_identities).join(" · ") || "none"}</small></td><td>{list(item.warning_codes).join(" · ") || text(item.error_code, "none")}</td><td>{formatTimestamp(item.completed_at)}</td></tr>)}</tbody></table></div>{!changes.length && <Empty title="No retained task results">The latest operating brief has no linked task-result projection.</Empty>}</Panel><Panel title="Attention preview"><AttentionTable items={attention} inspect={inspect} />{!attention.length && <Empty title="No retained attention items">No failed, blocked, stale, excluded, reconciliation, or insufficient-sample state is currently projected.</Empty>}</Panel></>}</>;
}

function AttentionTable({ items, inspect }: { items: ApiRecord[]; inspect: InspectEvidence }) {
  return <div className="scroll"><table><thead><tr><th scope="col">Severity</th><th scope="col">Category / status</th><th scope="col">Source / time</th><th scope="col">Reason</th><th scope="col">Explanation / action</th><th scope="col">Evidence</th></tr></thead><tbody>{items.map((item) => <tr key={text(item.attention_id)}><td><span className={stateClass(item.severity)}>{text(item.severity)}</span></td><td>{text(item.category)}<small>{text(item.status)}</small></td><td>{text(item.source_identity)}<small>{formatTimestamp(item.created_at)}</small></td><td><code>{text(item.code)}</code><small>{list(item.reason).join(" · ") || text(item.reason)}</small></td><td>{text(item.summary)}<small>{text(item.available_operator_action)}</small></td><td><EvidenceButton href={item.evidence_href} label={text(item.code, "Attention evidence")} inspect={inspect} /></td></tr>)}</tbody></table></div>;
}

function Attention({ inspect }: { inspect: InspectEvidence }) {
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(() => { api<ApiRecord>(`/api/v1/operator-console/attention?limit=25&offset=${offset}`).then((loaded) => { setValue(loaded); setError(null); }).catch(setError); }, [offset]);
  useEffect(refresh, [refresh]);
  const items = records(value?.items);
  return <><PageHead eyebrow="DETERMINISTIC REVIEW QUEUE" title="Attention" detail="Failed, blocked, stale, excluded, unresolved, and insufficient-sample retained states appear in stable severity order." refresh={refresh} /><ErrorNotice error={error} /><Panel title="Current queue" badge="READ ONLY"><AttentionTable items={items} inspect={inspect} />{!items.length && value && <Empty title="No operator attention">The retained queue is empty. This is not a scheduler or background monitor.</Empty>}<Pagination offset={offset} total={Number(value?.total ?? 0)} setOffset={setOffset} /></Panel></>;
}

function Opportunities({ inspect }: { inspect: InspectEvidence }) {
  const [venue, setVenue] = useState("ALL");
  const [eligibility, setEligibility] = useState("ALL");
  const [priority, setPriority] = useState("ALL");
  const [allocation, setAllocation] = useState("ALL");
  const [paperReadiness, setPaperReadiness] = useState("ALL");
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [library, setLibrary] = useState<ApiRecord | null>(null);
  const [homeBrief, setHomeBrief] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(() => { const params = new URLSearchParams({ venue, eligibility, priority, allocation, paper_readiness: paperReadiness, query: appliedQuery, limit: "25", offset: String(offset) }); api<ApiRecord>(`/api/v1/operator-console/opportunities?${params}`).then((loaded) => { setValue(loaded); setError(null); }).catch(setError); }, [venue, eligibility, priority, allocation, paperReadiness, appliedQuery, offset]);
  useEffect(refresh, [refresh]);
  useEffect(() => { api<ApiRecord>("/api/v1/operator-console/strategy-library").then(setLibrary).catch(setError); }, []);
  // Supplementary only: reuses the existing Home read path purely to show
  // "how many Opportunities exist in each state right now" alongside the
  // currently-applied filter, so a zero-row filtered view can explain
  // itself instead of looking broken. A failure here never touches the
  // page's own error state -- this strip simply does not render.
  useEffect(() => { api<ApiRecord>("/api/v1/operator-console/home").then(setHomeBrief).catch(() => undefined); }, []);
  const submit = (event: FormEvent) => { event.preventDefault(); setOffset(0); setAppliedQuery(query.trim()); };
  const items = records(value?.items);
  const strategies = records(library?.items);
  // SECURITIES Operator Console Backend Read-Model Closure v1:
  // application.operator_console_v1._VENUES -- the allow-list this read
  // model's own `venue` query param is validated against -- is now derived
  // from the canonical SharedVenue enum, so SECURITIES is offered below as
  // a real, accepted filter choice. Row rendering was already venue-generic
  // and needed no change.
  return <><PageHead eyebrow="CONSOLIDATED RESEARCH INBOX" title="Opportunities" detail="Crypto, Kalshi, and Securities retain distinct economics, missing values, warnings, exact source lineage, and unchanged shared-policy rank order. Securities research (defined-risk options structures) can reach research eligibility but stays research-only -- never PAPER- or LIVE-eligible under the active mandate." refresh={refresh} /><ErrorNotice error={error} /><div className="notice neutral">RESEARCH PRIORITIES ONLY · NOT ORDERS · NO PAPER AUTHORIZATION · NO LIVE AUTHORITY</div>{!value && !error && <Loading />}{homeBrief && !homeBrief.empty_state && <StageCounts label="Current retained Opportunity set, from the latest operating brief" stages={[["eligible", Number(homeBrief.eligible_opportunities ?? 0)], ["blocked", Number(homeBrief.blocked_opportunities ?? 0)], ["rejected", Number(homeBrief.rejected_opportunities ?? 0)], ["expired", Number(homeBrief.expired_opportunities ?? 0)]]} />}<Panel title="Current strategy library" badge="READ ONLY"><div className="scroll"><table><thead><tr><th scope="col">Venue / method</th><th scope="col">Current retained record</th><th scope="col">Status / results</th><th scope="col">Coverage / economics</th><th scope="col">Limitations / authority</th></tr></thead><tbody>{strategies.map((item) => <tr key={text(item.method_id)}><td>{text(item.venue)}<small>{text(item.method_id)} v{text(item.version)}</small></td><td>{text(item.current_record_id, "not run")}<small>source {text(item.source_record_id, "none")} · {formatTimestamp(item.evaluated_at)}</small></td><td>{text(item.status)}<small>{text(item.eligible_result_count, "0")} eligible / {text(item.result_count, "0")} evaluated · PAPER ready {text(item.paper_ready_count, "0")} · health {text(item.health_state)}</small></td><td>{text(item.coverage_summary, text(item.scan_breadth_summary, "coverage unavailable"))}<small>{text(item.economics)} · {list(item.required_data).join(" · ")}</small></td><td>{list(item.limitations).join(" · ")}<small>research {text(item.research_authority, "NONE")} · PAPER {text(item.paper_eligible, "false")} · LIVE {text(item.live_authority, "NONE")}</small></td></tr>)}</tbody></table></div>{!strategies.length && <Empty title="No current strategy records">No retained current method run is available.</Empty>}</Panel><Panel title="Bounded filters"><form className="filters" onSubmit={submit}><label>Venue<select value={venue} onChange={(event) => { setVenue(event.target.value); setOffset(0); }}><option>ALL</option><option>CRYPTO</option><option>KALSHI</option><option>SECURITIES</option></select></label><label>Eligibility<select value={eligibility} onChange={(event) => { setEligibility(event.target.value); setOffset(0); }}><option>ALL</option><option>ELIGIBLE</option><option>NON_ELIGIBLE</option><option>BLOCKED_MISSING_EVIDENCE</option><option>EXPIRED</option><option>REJECTED</option></select></label><label>Priority<select value={priority} onChange={(event) => { setPriority(event.target.value); setOffset(0); }}><option>ALL</option><option>RANKED</option><option>UNRANKED</option></select></label><label>Allocation<select value={allocation} onChange={(event) => { setAllocation(event.target.value); setOffset(0); }}><option>ALL</option><option>ALLOCATED</option><option>UNALLOCATED</option></select></label><label>PAPER readiness<select value={paperReadiness} onChange={(event) => { setPaperReadiness(event.target.value); setOffset(0); }}><option>ALL</option><option>READY</option><option>NOT_READY</option></select></label><label>Exact ID search<input maxLength={160} value={query} onChange={(event) => setQuery(event.target.value)} /></label><button type="submit">Apply filters</button></form></Panel><Panel title="Current retained set" badge="AUTHORITY NONE"><div className="scroll"><table><thead><tr><th scope="col">Venue / instrument</th><th scope="col">Source / validity</th><th scope="col">Signal / expected economics</th><th scope="col">Eligibility / context</th><th scope="col">Market / downside / costs</th><th scope="col">Liquidity / lockup</th><th scope="col">Priority / allocation</th><th scope="col">PAPER boundary / authority</th><th scope="col">Counterfactual</th><th scope="col">Evidence</th></tr></thead><tbody>{items.map((item) => <tr key={text(item.projection_id)}><td>{text(item.venue)} / {text(item.instrument_id)}<small>{text(item.product_or_market_id)} · {text(item.strategy_or_method_id)}</small></td><td>{formatTimestamp(item.generated_at)}<small>expires {formatTimestamp(item.valid_until)} · {text(item.evidence_class)}</small></td><td>score {text(item.signal_score, "model-free")}<small>gross/net {text(item.expected_gross_return, "not modeled")} / {text(item.expected_net_return, "not modeled")} · interval {text(item.uncertainty_lower, "missing")} to {text(item.uncertainty_upper, "missing")} · n={text(item.calibration_sample_count, "missing")}</small></td><td>{text(item.eligibility)} / {text(item.total_market_context, "missing")}<small><ReasonList codes={item.eligibility_reasons} /> · source <ReasonList codes={item.source_blockers} emptyText="clear" /></small></td><td>bid/ask {text(item.best_bid, "missing")} / {text(item.best_ask, "missing")}<small>mid {text(item.midpoint, "missing")} · spread {text(item.spread_bps, "missing")} bps · required {text(item.required_hypothetical_capital, "missing")} · max loss {text(item.maximum_loss, "missing")} · fees/slippage {text(item.modeled_fees, "missing")} / {text(item.modeled_slippage, "missing")}</small></td><td>{text(item.liquidity_state)} / {text(item.estimated_lockup_days, "missing")} days<small>depth {text(item.visible_depth, "missing")} · freshness {text(item.freshness_seconds, "missing")}/{text(item.freshness_limit_seconds, "missing")}s</small></td><td>{text(item.priority_rank, "unranked")} / {text(item.hypothetical_allocation, "not considered")} {item.priority_tier != null && <span className={stateClass(item.priority_tier)}>{text(item.priority_tier)}</span>}<small>{text(item.allocation_status)} · {list(item.allocation_reasons).length ? <ReasonList codes={item.allocation_reasons} /> : text(item.explanation)}</small></td><td>{text(item.paper_readiness)} / {text(item.authority)}<small>{list(item.paper_readiness_reasons).length ? <ReasonList codes={item.paper_readiness_reasons} /> : "separate authorization required"} · LIVE {text(item.live_authority, "NONE")}</small></td><td><EvidenceButton href={item.counterfactual_decision_href} label="Counterfactual decision" inspect={inspect} /></td><td><EvidenceButton href={item.evidence_href} label={`${text(item.venue)} Opportunity`} inspect={inspect} /><small>{list(item.evidence_lineage).length} exact lineage IDs</small></td></tr>)}</tbody></table></div>{!items.length && value && <Empty title="No matching Opportunities">{value.empty_state === "NO_RETAINED_OPPORTUNITIES" ? "No retained current Opportunity set exists." : homeBrief && !homeBrief.empty_state ? "No retained row matches these filters. See the retained Opportunity set above for what does exist right now." : "No retained row matches these filters."}</Empty>}<Pagination offset={offset} total={Number(value?.total ?? 0)} setOffset={setOffset} /></Panel></>;
}

function Allocation({ inspect }: { inspect: InspectEvidence }) {
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(() => { api<ApiRecord>("/api/v1/operator-console/allocation").then((loaded) => { setValue(loaded); setError(null); }).catch(setError); }, []);
  useEffect(refresh, [refresh]);
  const decision = record(value?.decision);
  const policy = record(value?.policy);
  const lines = records(value?.lines);
  const explanations = records(value?.explanations);
  return <><PageHead eyebrow="HYPOTHETICAL CROSS-VENUE REVIEW" title="Allocation" detail="The exact retained decision keeps at least the required cash and never creates authorization, reservation, Order, or provider request." refresh={refresh} /><ErrorNotice error={error} /><div className="notice neutral">$1,000 HYPOTHETICAL RESEARCH BUDGET · AUTHORITY NONE · ZERO EXECUTION</div>{value?.empty_state ? <Empty title="No retained allocation">No exact cross-venue allocation decision is available.</Empty> : value && decision && <><Metrics values={[["Hypothetical capital", decision.hypothetical_capital], ["Retained cash", decision.retained_cash], ["Venue totals", pairs(decision.venue_totals).map(([venueName, amount]) => `${venueName} ${amount}`).join(" · ") || "none"], ["Context / effects", `${text(decision.context_state)} / ${list(decision.context_effects).length}`], ["Modeled downside", decision.modeled_downside], ["Modeled costs", decision.modeled_costs], ["Long-lockup capital", decision.long_lockup_capital], ["Excluded Opportunities", list(decision.excluded_projection_ids).length], ["Allocation lines", lines.length], ["PAPER trial proposals", records(value.paper_trial_proposals).length], ["Orders created", value.orders_created], ["LIVE authority", value.live_authority]]} /><Panel title="Policy caps and context" badge="FIXED POLICY"><div className="key-values"><div><dt>Policy</dt><dd>{text(policy?.policy_id)}</dd></div><div><dt>Minimum retained cash</dt><dd>{text(policy?.minimum_retained_cash_fraction)}</dd></div><div><dt>Per Opportunity / venue cap</dt><dd>{text(policy?.maximum_opportunity_fraction)} / {text(policy?.maximum_venue_fraction)}</dd></div><div><dt>Strategy / instrument cap</dt><dd>{text(policy?.maximum_strategy_fraction)} / {text(policy?.maximum_instrument_fraction)}</dd></div><div><dt>Downside / long-lockup cap</dt><dd>{text(policy?.maximum_aggregate_downside_fraction)} / {text(policy?.maximum_long_lockup_fraction)}</dd></div><div><dt>Context effects</dt><dd>{list(decision.context_effects).join(" · ") || "none"}</dd></div></div></Panel><Panel title="Allocation lines" badge="NO EXECUTION"><div className="scroll"><table><thead><tr><th scope="col">Venue / instrument</th><th scope="col">Amount</th><th scope="col">Downside / costs</th><th scope="col">Lockup</th><th scope="col">Binding constraints</th></tr></thead><tbody>{lines.map((item) => <tr key={text(item.line_id)}><td>{text(item.venue)} / {text(item.instrument_id)}<small>{text(item.projection_id)}</small></td><td>{text(item.amount)}</td><td>{text(item.modeled_downside)} / {text(item.modeled_costs)}</td><td>{text(item.estimated_lockup_days, "missing")}</td><td>{list(item.binding_constraints).join(" · ") || "none"}</td></tr>)}</tbody></table></div>{!lines.length && <Empty title="Zero allocation">The retained decision leaves the entire $1,000 hypothetical budget unallocated because the listed Opportunities are excluded by exact eligibility, evidence, context, or PAPER-readiness constraints.</Empty>}<div className="control-row"><EvidenceButton href={value.evidence_href} label="Cross-venue allocation decision" inspect={inspect} /></div></Panel><Panel title="Constraint explanations">{explanations.length ? <ul>{explanations.map((item) => <li key={text(item.explanation_id)}><code>{list(item.excluded_reasons).join(" · ") || list(item.binding_constraints).join(" · ") || "CONSTRAINT"}</code> {text(item.text)}</li>)}</ul> : <p className="muted">No separate constraint explanations were retained.</p>}</Panel></>}</>;
}

function PaperLane({ lane, inspect }: { lane: ApiRecord | null; inspect: InspectEvidence }) {
  if (!lane) return null;
  const accounts = records(lane.accounts);
  const instructions = records(lane.instructions);
  const orders = accounts.flatMap((item) => records(item.orders));
  const authorizations = accounts.flatMap((item) => records(item.authorizations));
  const previews = accounts.flatMap((item) => records(item.previews));
  const positions = accounts.flatMap((item) => records(item.positions));
  const settlements = accounts.flatMap((item) => records(item.settlements));
  const reconciliations = accounts.flatMap((item) => records(item.reconciliations));
  return <Panel title={`${text(lane.venue)} · ${text(lane.state_source)}`} badge={`LIVE ${text(lane.live_authority, "NONE")}`}><p className="muted">PAPER state is venue-specific. No balance or performance shown here is genuine capital or earnings.</p>{accounts.map((item) => { const operations = record(item.operations_summary); const latestReconciliation = record(item.latest_reconciliation); const markCoverage = record(item.open_position_mark_coverage); return <div className="paper-record" key={text(item.account_id)}><div><strong>{text(item.account_id)}</strong><small>cash {text(item.cash)} · reserved {text(item.reserved_cash)} · fees {text(item.modeled_fees)} · realized PAPER P/L {text(item.realized_paper_pnl)} · unrealized PAPER P/L {text(item.unrealized_paper_pnl, "not marked")}{markCoverage && Number(markCoverage.open_count ?? 0) > 0 ? ` (${text(markCoverage.marked_count, "0")}/${text(markCoverage.open_count, "0")} open positions currently priced)` : ""} · orders/fills {records(item.orders).length}/{text(item.fills, "0")} · positions {pairs(item.position_state_counts).map(([state, count]) => `${state} ${count}`).join(" · ") || "0"} · operations {text(operations?.total, "0")} ({pairs(operations?.by_type).map(([kind, count]) => `${kind} ${count}`).join(" · ") || "none"})</small>{item.has_unresolved_reconciliation === true && <div className="notice error">UNRESOLVED RECONCILIATION as of {formatTimestamp(latestReconciliation?.evaluated_at)}<small><ReasonList codes={latestReconciliation?.issues} /></small></div>}</div><EvidenceButton href={item.evidence_href} label={`${text(lane.venue)} PAPER account`} inspect={inspect} /></div>; })}{orders.length > 0 && <div className="scroll"><table><thead><tr><th scope="col">Order / instrument</th><th scope="col">State</th><th scope="col">Quantity / filled</th><th scope="col">Expiration</th><th scope="col">Evidence</th></tr></thead><tbody>{orders.map((item) => <tr key={text(item.order_id)}><td>{text(item.order_id)}<small>{text(item.instrument_id)}</small></td><td>{text(item.state)}</td><td>{text(item.quantity)} / {text(item.filled_quantity)}</td><td>{formatTimestamp(item.expires_at)}</td><td><EvidenceButton href={item.evidence_href} label="PAPER order" inspect={inspect} /></td></tr>)}</tbody></table></div>}{authorizations.length > 0 && <div className="scroll"><table><thead><tr><th scope="col">Authorization</th><th scope="col">Strategy / discovery</th><th scope="col">Expiration</th><th scope="col">Evidence</th></tr></thead><tbody>{authorizations.map((item) => <tr key={text(item.authorization_id)}><td>{text(item.authorization_id)}<small>source {text(item.source_id)}</small></td><td>{text(item.strategy_or_discovery_id, "missing")}</td><td>{formatTimestamp(item.expires_at)}</td><td><EvidenceButton href={item.evidence_href} label="PAPER authorization" inspect={inspect} /></td></tr>)}</tbody></table></div>}{previews.length > 0 && <div className="scroll"><table><thead><tr><th scope="col">Preview</th><th scope="col">Accepted</th><th scope="col">Maximum risk / fees</th><th scope="col">Expiration</th><th scope="col">Evidence</th></tr></thead><tbody>{previews.map((item) => <tr key={text(item.preview_id)}><td>{text(item.preview_id)}</td><td>{boolText(item.accepted)}</td><td>{text(item.maximum_risk, "missing")} / {text(item.modeled_fees, "missing")}</td><td>{formatTimestamp(item.expires_at)}</td><td><EvidenceButton href={item.evidence_href} label="PAPER preview" inspect={inspect} /></td></tr>)}</tbody></table></div>}{positions.length > 0 && <div className="scroll"><table><thead><tr><th scope="col">Position / instrument</th><th scope="col">State</th><th scope="col">Quantity</th><th scope="col">Realized PAPER P/L</th><th scope="col">Unrealized PAPER P/L</th><th scope="col">Evidence</th></tr></thead><tbody>{positions.map((item) => <tr key={text(item.position_id)}><td>{text(item.position_id)}<small>{text(item.instrument_id)}</small></td><td>{text(item.state)}</td><td>{text(item.quantity)}</td><td>{text(item.realized_paper_pnl, "missing")}</td><td>{item.mark_disposition === "SATISFIED" ? <>{text(item.unrealized_paper_pnl)}<small>@ {text(item.mark_price)} ({formatTimestamp(item.marked_at)})</small></> : <span className={stateClass(item.mark_disposition)}>{text(item.mark_disposition, "NOT_EVALUATED")}</span>}</td><td><EvidenceButton href={item.evidence_href} label="PAPER position" inspect={inspect} /></td></tr>)}</tbody></table></div>}{(settlements.length > 0 || reconciliations.length > 0) && <div className="key-values"><div><dt>Settlements</dt><dd>{settlements.length}</dd></div><div><dt>Reconciliations</dt><dd>{reconciliations.length}</dd></div><div><dt>Reconciliation issues</dt><dd>{reconciliations.reduce((count, item) => count + list(item.issues).length, 0)}</dd></div></div>}{instructions.map((item) => <div className="paper-record" key={text(item.instruction_id)}><div><strong>{text(item.instruction_id)}</strong><small>{text(item.symbol)} · {text(item.side)} · {text(item.state)} · validated fills {text(item.validated_fill_count)} · broker order {text(item.broker_order_id, "missing")}</small></div><EvidenceButton href={item.evidence_href} label="External Alpaca PAPER instruction" inspect={inspect} /></div>)}{!accounts.length && !instructions.length && <Empty title="No genuine retained state">{text(lane.empty_state)}</Empty>}</Panel>;
}

/** Q1 -- "is PAPER execution enabled and healthy right now". Real, retained
 * PaperActivationEvent-folded state (`capability_gate`) -- the exact known
 * misleading value (a hardcoded `paper_eligible: false`) PAPER Operations
 * & Observability v1 replaces. Distinguishes ENABLED/DISABLED from
 * "no retained activation decision at all" rather than defaulting silently
 * to one or the other. */
function CapabilityGateBanner({ gate }: { gate: ApiRecord | null }) {
  if (!gate) return null;
  const enabled = gate.enabled === true;
  return <div className={`notice ${enabled ? "neutral" : "error"}`}>PAPER CAPABILITY GATE: {enabled ? "ENABLED" : "DISABLED"}{gate.empty_state ? " — no retained operator activation decision exists yet" : ""}<small> · reason {text(gate.reason, "none retained")} · actor {text(gate.actor, "none retained")} · last evaluated {formatTimestamp(gate.last_evaluated_at)}</small></div>;
}

function StrategyRegistryPanel({ registry, inspect }: { registry: ApiRecord | null; inspect: InspectEvidence }) {
  if (!registry) return null;
  const strategies = records(registry.strategies);
  return <Panel title="Registered strategies · STRATEGY_PAPER_TRIAL_EXECUTION" badge={text(registry.latest_task_status, "NEVER RUN")}><p className="muted">Every 5-minute operating cycle re-evaluates each registered strategy fresh against genuinely retained evidence; below is that most recent retained evaluation, never a second live recomputation. This task defers its own terminal result while it keeps succeeding (see "Account last reconciled" below), so "Last failed/completed" can predate genuinely healthy, ongoing activity -- never read one without the other.</p><Metrics values={[["Considered", registry.candidates_considered ?? "0"], ["Promoted this cycle", registry.candidates_promoted ?? "0"], ["Trials created last cycle", registry.trials_created_last_cycle ?? "0"], ["Reconciliation (as of last terminal result)", registry.reconciliation_status], ["Contemporaneous evidence (as of last terminal result)", registry.contemporaneous_evidence_disposition], ["Last failed/completed", formatTimestamp(registry.latest_task_completed_at)], ["Account last reconciled", formatTimestamp(registry.account_last_reconciled_at)]]} /><div className="scroll"><table><thead><tr><th scope="col">Strategy</th><th scope="col">Venue / instruments</th><th scope="col">Lifecycle state</th><th scope="col">Eligible this cycle</th><th scope="col">Open trial</th><th scope="col">Why</th><th scope="col">Durable evaluation history</th></tr></thead><tbody>{strategies.map((item) => { const latest = record(item.latest_durable_evaluation); const promotion = record(item.latest_promotion); return <tr key={text(item.strategy_id)}><td>{text(item.display_name)}<small>{text(item.strategy_id)} v{text(item.version)} · {text(item.source_kind)} · {text(item.rule_kind)}</small></td><td>{text(item.venue)}<small>{list(item.instrument_ids).join(" · ")}</small></td><td>{item.lifecycle_state != null ? <span className={stateClass(item.lifecycle_state)}>{text(item.lifecycle_state)}</span> : <span className="muted">not yet evaluated</span>}</td><td>{boolText(item.eligible_this_cycle)}</td><td>{item.has_open_trial === true ? <EvidenceButton href={item.open_order_evidence_href} label="Open PAPER order" inspect={inspect} /> : "no"}</td><td><ReasonList codes={item.blocking_reasons} emptyText={item.eligible_this_cycle === true || item.has_open_trial === true ? "eligible" : "not yet evaluated"} /></td><td>{item.has_evaluation_history === true ? <><span className={stateClass(item.durable_current_lifecycle_state)}>{text(item.durable_current_lifecycle_state, "RESEARCH_ONLY")}</span> operationally (durable){item.latest_promotion != null && <small> · promoted {formatTimestamp(promotion?.promoted_at)} by {text(promotion?.actor)}</small>}<small>{item.latest_promotion == null && "no explicit promotion retained · "}{text(item.evaluation_freshness)} · latest {formatTimestamp(latest?.evaluated_at)} · {text(latest?.qualification)} · sample {text(latest?.sample_size)} · net return {text(latest?.net_return, "n/a")} · {text(item.promotion_count ?? 0)} promotion(s) on record</small></> : <span className="muted">no retained durable evaluation yet (pre-lineage or never dispatched)</span>}</td></tr>; })}</tbody></table></div>{!strategies.length && <Empty title="No registered strategies">No canonical strategy catalog is currently retained.</Empty>}</Panel>;
}

/** Strategy PAPER Trial Outcome Assessment v1: retained, immutable outcome
 * evidence per closed trial. Descriptive only -- it never promotes, demotes,
 * ranks, or trades, and "expected" is the justifying evaluation's backtest
 * window return, never a horizon-matched forecast, so no delta is shown. */
function TrialOutcomePanel({ outcomes }: { outcomes: ApiRecord | null }) {
  if (!outcomes) return null;
  const recent = records(outcomes.recent);
  const counts = record(outcomes.status_counts) ?? {};
  const awaiting = records(outcomes.closed_trials_awaiting_assessment);
  return <Panel title="Strategy trial outcomes · STRATEGY_TRIAL_OUTCOME_ASSESSMENT" badge={text(outcomes.policy_version)}><p className="muted">One immutable assessment per closed trial. A legacy trial that predates strategy provenance is assessed economically but never bound to a strategy version or evaluation. No strategy-level conclusion is drawn below the minimum sample.</p><Metrics values={[["Assessments", outcomes.assessment_count ?? "0"], ["Provenance-bound sample", `${text(outcomes.provenance_bound_sample_count, "0")} / ${text(outcomes.minimum_sample_size)}`], ["Closed, awaiting assessment", String(awaiting.length)], ...Object.entries(counts).map(([status, count]) => [status, count] as [string, unknown])]} /><div className="scroll"><table><thead><tr><th scope="col">Trial</th><th scope="col">Provenance</th><th scope="col">Expected (evaluation)</th><th scope="col">Realized</th><th scope="col">Status</th></tr></thead><tbody>{recent.map((item) => { const expected = record(item.expected); const realized = record(item.realized); return <tr key={text(item.assessment_id)}><td>gen {text(item.generation)}<small>{text(item.trial_id)} · closed {formatTimestamp(item.closed_at)}</small></td><td><span className={stateClass(item.provenance_class)}>{text(item.provenance_class)}</span><small>{text(item.evaluation_id, "no justifying evaluation retained")}</small></td><td>{text(expected?.evaluation_net_return, "n/a")}<small>{text(expected?.basis)}</small></td><td>{text(realized?.realized_return, "n/a")}<small>P&amp;L {text(realized?.ledger_realized_pnl, "n/a")} · {text(realized?.economics_state)}</small></td><td><span className={stateClass(item.status)}>{text(item.status)}</span><small>sample {text(item.population_size)} / {text(item.minimum_sample_size)}</small></td></tr>; })}</tbody></table></div>{!recent.length && <Empty title="No trial outcome assessed yet">An assessment appears once a trial closes and its final attribution is retained.</Empty>}</Panel>;
}

/** External Strategy Evaluation v1: an operator-registered strategy's OWN
 * independent registration/evaluation/promotion lineage -- deliberately
 * separate from StrategyRegistryPanel above, which reflects only the LIVE
 * recurring STRATEGY_PAPER_TRIAL_EXECUTION task's fixed four-candidate
 * catalog. Reaching PAPER_ELIGIBLE here never authorizes, previews, or
 * submits a PAPER order -- see this milestone's own backend docstrings. */
function ExternalStrategyRegistryPanel({ inspect }: { inspect: InspectEvidence }) {
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(() => { api<ApiRecord>("/api/v1/operator-console/external-strategies").then((loaded) => { setValue(loaded); setError(null); }).catch(setError); }, []);
  useEffect(refresh, [refresh]);
  const strategies = records(value?.strategies);
  return <Panel title="External strategies · manually registered research" badge={`${text(value?.total, "0")} registered`}><p className="muted">Registered and evaluated independently of the live recurring STRATEGY_PAPER_TRIAL_EXECUTION catalog above. External source does not mean trusted source: every strategy here passes the same deterministic backtest, benchmark comparison, and evidence bar as an internal method.</p><ErrorNotice error={error} /><div className="scroll"><table><thead><tr><th scope="col">Strategy</th><th scope="col">Source</th><th scope="col">Lifecycle state</th><th scope="col">Latest evaluation</th><th scope="col">Why</th><th scope="col">Evidence</th></tr></thead><tbody>{strategies.map((item) => { const provenance = record(item.provenance); return <tr key={text(item.strategy_id)}><td>{text(item.display_name)}<small>{text(item.strategy_id)} v{text(item.version)} · {text(item.rule_kind)}</small></td><td>{text(item.source_kind)}<small>{text(provenance?.reference_description)}</small></td><td><span className={stateClass(item.lifecycle_state)}>{text(item.lifecycle_state)}</span></td><td>{item.latest_evaluated_at ? <>{formatTimestamp(item.latest_evaluated_at)}<small>sample {text(item.latest_sample_size)} · net return {text(item.latest_net_return, "missing")}</small></> : <span className="muted">not yet evaluated</span>}</td><td>{text(item.qualification_reason) ?? text(item.evaluation_empty_state, "-")}</td><td><EvidenceButton href={item.evidence_href} label="External strategy" inspect={inspect} /></td></tr>; })}</tbody></table></div>{!strategies.length && <Empty title="No external strategies registered">Register one with the `external-strategy-register` CLI command.</Empty>}</Panel>;
}

function PaperOperations({ navigate, inspect }: { navigate: (value: string) => void; inspect: InspectEvidence }) {
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(() => { api<ApiRecord>("/api/v1/operator-console/paper").then((loaded) => { setValue(loaded); setError(null); }).catch(setError); }, []);
  useEffect(refresh, [refresh]);
  const costs = record(value?.cost_assumptions);
  return <><PageHead eyebrow="SEPARATED PAPER OPERATIONS" title="PAPER Operations" detail="Apex-internal Crypto and Kalshi simulation remain isolated from separately governed external Alpaca PAPER evidence." refresh={refresh} /><ErrorNotice error={error} /><div className="notice neutral">PAPER ONLY · SIMULATED CAPITAL · BROKER RECORDS ARE EVIDENCE, NOT AUTOMATIC APEX TRUTH · LIVE AUTHORITY NONE</div>{value && <><CapabilityGateBanner gate={record(value.capability_gate)} /><StrategyRegistryPanel registry={record(value.strategy_registry)} inspect={inspect} /><TrialOutcomePanel outcomes={record(value.trial_outcome_assessments)} /><PaperLane lane={record(value.internal_crypto)} inspect={inspect} /><PaperLane lane={record(value.internal_kalshi)} inspect={inspect} /><PaperLane lane={record(value.external_alpaca)} inspect={inspect} />{costs && <Panel title="Transaction-cost assumptions" badge="STRATEGY_PAPER_TRIAL_EXECUTION"><div className="key-values"><div><dt>Modeled fee</dt><dd>{text(costs.fee_bps)} bps</dd></div><div><dt>Modeled spread</dt><dd>{text(costs.spread_bps)} bps</dd></div><div><dt>Modeled slippage</dt><dd>{text(costs.slippage_bps)} bps</dd></div><div><dt>Applies to</dt><dd>{text(costs.applies_to)}</dd></div></div></Panel>}<Panel title="Existing exact-ID controls" badge="NO BULK ACTIONS"><p>Mutation controls remain on their reviewed venue workspaces with explicit identifiers and confirmations. This consolidated view adds no submission path and chooses no Opportunity automatically.</p><div className="button-row"><button onClick={() => navigate("Crypto PAPER")}>Open Crypto PAPER</button><button onClick={() => navigate("Kalshi Operating")}>Open Kalshi PAPER</button><button onClick={() => navigate("External PAPER")}>Open Alpaca PAPER evidence</button></div></Panel></>}</>;
}

function Learn({ inspect }: { inspect: InspectEvidence }) {
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(() => { api<ApiRecord>("/api/v1/operator-console/learning").then((loaded) => { setValue(loaded); setError(null); }).catch(setError); }, []);
  useEffect(refresh, [refresh]);
  const attributions = records(value?.attributions);
  const health = records(value?.health);
  return <><PageHead eyebrow="DESCRIPTIVE PAPER EVIDENCE" title="Learn" detail="Expected-versus-realized attribution and strategy health preserve missing lifecycle stages, evidence class, context lineage, and reconciliation quality." refresh={refresh} /><ErrorNotice error={error} /><div className="notice neutral">PAPER PERFORMANCE IS NOT REAL EARNINGS · HEALTH CANNOT PROMOTE, AUTHORIZE, SUSPEND, RETIRE, ALLOCATE, OR SUBMIT</div>{value && <><Panel title="Strategy health" badge="AUTHORITY NONE"><div className="scroll"><table><thead><tr><th scope="col">Method / evidence</th><th scope="col">State</th><th scope="col">Trials / fills / settled</th><th scope="col">Failed thresholds</th><th scope="col">Evidence</th></tr></thead><tbody>{health.map((item) => <tr key={text(item.health_id)}><td>{text(item.strategy_or_method_id)}<small>{text(item.evidence_class)} · version {text(item.strategy_or_method_version)}</small></td><td><span className={stateClass(item.state)}>{text(item.state)}</span></td><td>{text(item.paper_trial_count, "0")} / {text(item.fill_count, "0")} / {text(item.settled_outcome_count, "0")}</td><td>{list(item.failed_thresholds).join(" · ") || "none"}<small>{text(item.operator_recommendation)}</small></td><td><EvidenceButton href={item.evidence_href} label="Strategy health" inspect={inspect} /></td></tr>)}</tbody></table></div>{!health.length && <Empty title="Insufficient sample">No current genuine PAPER health assessment is retained.</Empty>}</Panel><Panel title="Performance attribution"><div className="scroll"><table><thead><tr><th scope="col">Source / venue</th><th scope="col">Missing stages / position</th><th scope="col">Expected / realized</th><th scope="col">Reconciliation</th><th scope="col">Evidence</th></tr></thead><tbody>{attributions.map((item) => <tr key={text(item.attribution_id)}><td>{text(item.source_id)}<small>{text(item.venue)} · {text(item.evidence_class)}</small></td><td>{list(item.missing_stages).join(" · ") || "complete"}<small>position {text(item.position_state, "missing")}</small></td><td>{text(item.expected_net_return, "missing")} / {text(item.realized_net_return, "missing")}</td><td>{text(item.reconciliation_status, "missing")}<small>{list(item.reconciliation_issues).join(" · ") || "none"}</small></td><td><EvidenceButton href={item.evidence_href} label="Performance attribution" inspect={inspect} /></td></tr>)}</tbody></table></div>{!attributions.length && <Empty title="Insufficient sample">No current genuine PAPER attribution is retained.</Empty>}</Panel></>}</>;
}

function ResearchOperations({ inspect }: { inspect: InspectEvidence }) {
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [proposalId, setProposalId] = useState("");
  const [agendaId, setAgendaId] = useState("");
  const [hypothesis, setHypothesis] = useState("");
  const [mechanism, setMechanism] = useState("");
  const [experimentId, setExperimentId] = useState("");
  const refresh = useCallback(() => {
    api<ApiRecord>("/api/v1/operator-console/research-operations")
      .then((loaded) => { setValue(loaded); setError(null); })
      .catch(setError);
  }, []);
  useEffect(refresh, [refresh]);
  const agenda = records(value?.agenda_items);
  const proposals = records(value?.proposals);
  const chosenAgenda = agenda.find((item) => text(item.agenda_item_id) === agendaId);
  const generate = async () => {
    const generatorRunId = window.prompt("Exact proposal-generator run ID");
    if (!generatorRunId || !window.confirm("Generate at most three structured research proposals from retained agenda evidence? No proposal will be approved or executed.")) return;
    try {
      await mutate("/api/v1/research-operations/generator-runs", {
        generator_run_id: generatorRunId,
        maximum_proposals: 3,
        origin: "SYSTEM_HEURISTIC",
        actor: "operator:console",
      });
      refresh();
    } catch (reason) { setError(reason); }
  };
  const createProposal = async (event: FormEvent) => {
    event.preventDefault();
    if (!chosenAgenda || !window.confirm(`Create immutable DRAFT ${proposalId} for exact agenda item ${agendaId}? This grants no authority.`)) return;
    const venue = text(chosenAgenda.venue);
    const instrument = list(chosenAgenda.market_scope)[0] ?? "RETAINED_SCOPE";
    try {
      await mutate("/api/v1/research-operations/proposals", {
        proposal_id: proposalId,
        version: 1,
        agenda_item_id: agendaId,
        venue,
        hypothesis,
        expected_mechanism: mechanism,
        instruments: [instrument],
        proposed_family: `${venue}_GOVERNED_RESEARCH`,
        definition_kind: venue === "KALSHI" ? "KALSHI_RETAINED_RELATIONSHIP_METHOD" : "EXISTING_RESEARCH_REPLICATION",
        strategy_definition: [["existing_experiment_id", experimentId]],
        exact_change_from_prior: "OPERATOR_AUTHORED_STRUCTURED_RESEARCH_PROPOSAL",
        prior_research_ids: list(chosenAgenda.prior_related_research),
        required_data_ids: [text(chosenAgenda.source_fact_id), `LIQUIDITY:${text(chosenAgenda.source_fact_hash)}`],
        experiment_configuration: [["existing_experiment_id", experimentId]],
        validation_windows: [["retained_scope", "EXACT_EXISTING_EXPERIMENT_SCOPE"]],
        cost_assumptions: [["source", "EXISTING_RETAINED_EXPERIMENT_POLICY"]],
        success_criteria: ["POSITIVE_NET_ECONOMICS", "UNCERTAINTY_SUFFICIENT", "DOWNSIDE_WITHIN_LIMIT"],
        failure_criteria: ["NON_POSITIVE_NET_ECONOMICS", "INSUFFICIENT_EVIDENCE"],
        maximum_experiment_runs: 1,
        origin: "OPERATOR",
        actor: "operator:console",
        warnings: ["RESEARCH_ONLY", "NO_PAPER_OR_LIVE_AUTHORITY"],
      });
      setProposalId(""); setHypothesis(""); setMechanism(""); setExperimentId(""); refresh();
    } catch (reason) { setError(reason); }
  };
  const review = async (item: ApiRecord, target: string) => {
    const identity = text(item.proposal_id);
    const decisionHash = text(item.duplicate_decision_hash);
    const budget = record(item.budget);
    const approvalSummary = target === "APPROVED_FOR_EXPERIMENT" ? `\nData: ${list(item.required_data_ids).join(" · ")}\nPublic requests: ${text(item.estimated_public_requests, "0")}\nRuntime bound: ${text(item.estimated_runtime_seconds, "0")} seconds\nDuplicate: ${text(item.duplicate_classification)}\nBudget: ${text(budget?.remaining_runs, "0")} run(s) remaining\nAuthority: EXPERIMENT_ONLY; PAPER NONE; LIVE NONE` : "";
    if (!window.confirm(`${target} exact proposal ${identity}?${approvalSummary}\nThis does not grant PAPER or LIVE authority.`)) return;
    try {
      await mutate(`/api/v1/research-operations/proposals/${identity}/review`, {
        target_state: target,
        actor: "operator:console",
        reason: `OPERATOR_CONSOLE_${target}`,
        confirmation: `${target}:${identity}:${decisionHash}`,
      });
      refresh();
    } catch (reason) { setError(reason); }
  };
  const enqueue = async (item: ApiRecord) => {
    const identity = text(item.proposal_id);
    const queueId = window.prompt("Exact experiment queue ID");
    const runId = window.prompt("Exact bounded run ID");
    if (!queueId || !runId || !window.confirm(`Queue approved proposal ${identity} as ${queueId}? This grants experiment authority only.`)) return;
    try {
      await mutate("/api/v1/research-operations/experiment-queue", {
        queue_id: queueId,
        proposal_id: identity,
        run_id: runId,
        actor: "operator:console",
      });
      refresh();
    } catch (reason) { setError(reason); }
  };
  const executeQueue = async (item: ApiRecord) => {
    const queueId = text(item.queue_id);
    if (!queueId || !window.confirm(`Run exact approved queue item ${queueId}? No PAPER or LIVE authority is granted.`)) return;
    try {
      await mutate(`/api/v1/research-operations/experiment-queue/${queueId}/execute`, { actor: "operator:console", reason: "EXACT_OPERATOR_QUEUE_EXECUTION" });
      refresh();
    } catch (reason) { setError(reason); }
  };
  const cancelQueue = async (item: ApiRecord) => {
    const queueId = text(item.queue_id);
    if (!queueId || !window.confirm(`Cancel queued experiment ${queueId}?`)) return;
    try {
      await mutate(`/api/v1/research-operations/experiment-queue/${queueId}/cancel`, { actor: "operator:console", reason: "OPERATOR_CANCELED_BEFORE_EXECUTION" });
      refresh();
    } catch (reason) { setError(reason); }
  };
  return <><PageHead eyebrow="GOVERNED RESEARCH CONTROL PLANE" title="Research Operations" detail="Retained blockers become structured proposals, pass deterministic negative-knowledge and feasibility checks, enter an exact review queue, and only run after operator approval." refresh={refresh} /><ErrorNotice error={error} /><div className="notice neutral">AUTHORITY NONE · NO AUTOMATIC APPROVAL · PAPER CANDIDATE MEANS REVIEW ONLY · LIVE AUTHORITY NONE</div><Metrics values={[["Agenda items", value?.agenda_count], ["Generated proposals", value?.generated_proposal_count], ["Awaiting review", value?.awaiting_review_count], ["Queued / running", `${text(value?.queued_experiment_count)} / ${text(value?.running_experiment_count)}`], ["PAPER candidates", value?.paper_candidate_count], ["External AI calls", value?.external_ai_model_calls_enabled], ["Provider / financial mutation", `${text(value?.provider_write_capability)} / ${text(value?.financial_mutation_capability)}`]]} /><Panel title="Research agenda" badge="FACT-BOUND"><div className="control-row"><button onClick={generate}>Generate bounded proposals</button></div><div className="scroll"><table><thead><tr><th scope="col">Priority / venue</th><th scope="col">Problem</th><th scope="col">Prior research / duplicate risk</th><th scope="col">Suggested next step</th><th scope="col">Evidence</th></tr></thead><tbody>{agenda.map((item) => <tr key={text(item.agenda_item_id)}><td>{text(item.priority)} / {text(item.venue)}<small>{text(item.status)}</small></td><td>{text(item.problem_statement)}<small>{text(item.why_it_matters)}</small></td><td>{list(item.prior_related_research).join(" · ") || "none"}<small>{text(item.duplicate_risk_status)}</small></td><td><code>{text(item.suggested_next_step)}</code></td><td><EvidenceButton href={item.evidence_href} label="Research agenda item" inspect={inspect} /></td></tr>)}</tbody></table></div>{!agenda.length && <Empty title="No retained agenda blockers">No current fact-bound blocker has been materialized.</Empty>}</Panel><Panel title="Create structured operator proposal" badge="DRAFT ONLY"><form className="filters" onSubmit={createProposal}><label>Agenda<select required value={agendaId} onChange={(event) => setAgendaId(event.target.value)}><option value="">Select exact agenda item</option>{agenda.map((item) => <option key={text(item.agenda_item_id)} value={text(item.agenda_item_id)}>{text(item.agenda_item_id)}</option>)}</select></label><label>Proposal ID<input required maxLength={300} value={proposalId} onChange={(event) => setProposalId(event.target.value)} /></label><label>Existing experiment ID<input required maxLength={300} value={experimentId} onChange={(event) => setExperimentId(event.target.value)} /></label><label>Hypothesis<input required maxLength={2000} value={hypothesis} onChange={(event) => setHypothesis(event.target.value)} /></label><label>Mechanism<input required maxLength={2000} value={mechanism} onChange={(event) => setMechanism(event.target.value)} /></label><button type="submit">Create immutable draft</button></form></Panel><Panel title="Generated and operator proposals" badge="EXACT-ID ACTIONS"><div className="scroll"><table><thead><tr><th scope="col">Proposal / class / origin</th><th scope="col">Review / feasibility / duplicate</th><th scope="col">Mechanism / required evidence</th><th scope="col">Priority / budget</th><th scope="col">Queue / recommendation</th><th scope="col">Exact actions</th><th scope="col">Evidence</th></tr></thead><tbody>{proposals.map((item) => { const budget = record(item.budget); const priority = record(item.priority); return <tr key={text(item.proposal_id)}><td>{text(item.proposal_id)} v{text(item.version)}<small>{text(item.proposal_class, "operator-authored")} · {text(item.venue)} · {text(item.origin)}</small></td><td>{text(item.state)} / {text(item.feasibility_status, "manual review")}<small>{text(item.duplicate_classification)} · {list(item.feasibility_reasons).join(" · ") || list(item.duplicate_reasons).join(" · ")}</small></td><td>{text(item.expected_mechanism)}<small>{list(item.required_data_ids).join(" · ")}</small></td><td>{text(priority?.total_score, "manual")}<small>{text(budget?.consumed_runs, "0")} / {text(budget?.maximum_runs, "0")} · remaining {text(budget?.remaining_runs, "0")} · replay {text(budget?.replays, "0")}</small></td><td>{text(item.queue_status, "not queued")}<small>{text(item.recommendation, "not completed")} · {list(item.recommendation_reasons).join(" · ")}</small></td><td><div className="button-row">{item.state === "DRAFT" && <button onClick={() => review(item, "READY_FOR_REVIEW")}>Submit for review</button>}{item.state === "READY_FOR_REVIEW" && <><button disabled={item.approval_permitted !== true || (item.feasibility_status !== undefined && item.feasibility_status !== "READY_FOR_REVIEW")} onClick={() => review(item, "APPROVED_FOR_EXPERIMENT")}>Approve experiment</button><button className="quiet" onClick={() => review(item, "REJECTED")}>Reject</button><button className="quiet" onClick={() => review(item, "DEFERRED")}>Defer</button></>}{item.state === "APPROVED_FOR_EXPERIMENT" && !item.queue_id && <button onClick={() => enqueue(item)}>Enqueue exact experiment</button>}{item.queue_status === "QUEUED" && <><button onClick={() => executeQueue(item)}>Run exact queue item</button><button className="quiet" onClick={() => cancelQueue(item)}>Cancel queued experiment</button></>}</div></td><td><EvidenceButton href={item.evidence_href} label="Research proposal" inspect={inspect} /></td></tr>; })}</tbody></table></div>{!proposals.length && <Empty title="No governed proposals">Generate from retained agenda evidence or create one structured operator draft.</Empty>}</Panel><ExternalStrategyRegistryPanel inspect={inspect} /></>;
}

function LiveReadiness({ inspect }: { inspect: InspectEvidence }) {
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(() => { api<ApiRecord>("/api/v1/operator-console/live-readiness").then((loaded) => { setValue(loaded); setError(null); }).catch(setError); }, []);
  useEffect(refresh, [refresh]);
  const retainedCapabilities = records(value?.provider_capabilities);
  const assessment = record(value?.assessment);
  const control = record(value?.execution_control);
  const backup = record(value?.backup_readiness);
  const policy = record(value?.risk_policy);
  const deployment = record(value?.deployment_profile);
  const runner = record(value?.operating_runner);
  const restore = record(value?.restore_verification);
  const operationalHealth = record(value?.operational_health);
  const killSwitchDrill = record(value?.kill_switch_drill);
  const alerts = records(value?.alert_deliveries);
  const coinbase = record(value?.coinbase_private_read_only);
  const coinbaseApproval = record(coinbase?.latest_approval);
  const coinbaseAttempt = record(coinbase?.latest_attempt);
  const coinbaseCapture = record(coinbase?.latest_capture);
  const coinbaseBalances = records(coinbaseCapture?.balances);
  const coinbaseProducts = records(coinbaseCapture?.products);
  const kalshi = record(value?.kalshi_private_read_only);
  const kalshiApproval = record(kalshi?.latest_approval);
  const kalshiAttempt = record(kalshi?.latest_attempt);
  const kalshiCapture = record(kalshi?.latest_capture);
  const kalshiBalances = records(kalshiCapture?.balances);
  const livePreflight = record(value?.live_preflight);
  const latestIntent = record(livePreflight?.latest_intent);
  const latestInternalPreflight = record(livePreflight?.latest_internal_preflight);
  const latestProviderPreview = record(livePreflight?.latest_provider_preview);
  const latestPreflightAssessment = record(livePreflight?.latest_assessment);
  const capabilities = [
    ...retainedCapabilities.filter((item) => item.provider !== "KALSHI"),
    ...(kalshi && Object.keys(kalshi).length
      ? [{
          snapshot_id: text(kalshiCapture?.capture_id, "kalshi-private-read-empty"),
          provider: "KALSHI",
          account_environment: "LIVE",
          account_scope_id: text(kalshiCapture?.credential_reference_id, "credential reference absent"),
          authenticated_read_status: text(kalshiAttempt?.status, "NOT_ATTEMPTED"),
          account_status: kalshiCapture?.account_status,
          permissions: kalshiCapture?.capability_classes,
          fee_evidence: [],
          minimum_size_constraints: [],
          limitations: [
            `approval ${text(kalshiApproval?.approval_id, "absent")}`,
            `account tier ${text(kalshiCapture?.account_tier, "unavailable")}`,
            ...kalshiBalances.map((item) => `${text(item.currency)} balance ${text(item.available_amount)} (evidence only)`),
            ...list(kalshiCapture?.account_restrictions),
            ...list(kalshiCapture?.warnings),
          ],
          canonical_hash: kalshiCapture?.canonical_hash,
          preview_available: false,
          trade_available: list(kalshiCapture?.capability_classes).includes("LIVE_TRADE_PERMISSION_AVAILABLE"),
          transfer_available: list(kalshiCapture?.capability_classes).includes("TRANSFER_PERMISSION_AVAILABLE"),
          withdrawal_available: list(kalshiCapture?.capability_classes).includes("WITHDRAWAL_PERMISSION_AVAILABLE"),
        }]
      : []),
  ];
  return <><PageHead eyebrow="OPERATIONAL RELIABILITY AND READ-ONLY PROVIDER FOUNDATION" title="Live Readiness" detail="Supervised local services, runner health, backup/restore proof, redacted alerts, credential references, restrictive risk limits, and non-executing intent preflight remain independent evidence gates." refresh={refresh} /><ErrorNotice error={error} /><div className="notice neutral">DEFAULT LIVE_NOT_READY · LIVE AUTHORITY NONE · NO GENUINE PROVIDER WRITE · SECRET VALUES NEVER RETAINED</div><Metrics values={[["Recommendation", value?.readiness_recommendation], ["Execution control", control?.state], ["Deployment / health", `${text(deployment?.profile_id, "missing")} / ${text(operationalHealth?.state, "missing")}`], ["Runner / next due", `${text(runner?.status, "missing")} / ${formatTimestamp(runner?.next_due_at)}`], ["Backup / restore", `${text(backup?.status, "missing")} / ${text(restore?.status, "missing")}`], ["Alerts delivered", alerts.filter((item) => item.status === "DELIVERED").length], ["Coinbase private reads / writes", `${text(coinbase?.provider_request_count, "0")} / ${text(coinbase?.provider_write_count, "0")}`], ["Live intents / preview writes", `${text(livePreflight?.intent_count, "0")} / ${text(livePreflight?.provider_write_count, "0")}`], ["Kill-switch drill", killSwitchDrill?.final_state], ["Risk policy", policy?.policy_id], ["Unresolved blockers", list(value?.unresolved_blockers).length], ["Provider write / LIVE", `${text(value?.provider_write_capability)} / ${text(value?.live_authority)}`]]} /><Panel title="Operational reliability" badge="LOCAL · BOUNDED · READ ONLY"><div className="key-values"><div><dt>Deployment profile</dt><dd>{text(deployment?.profile_id, "not retained")}</dd></div><div><dt>Runner heartbeat</dt><dd>{text(runner?.heartbeat_id, "not retained")} · {text(runner?.status, "missing")}</dd></div><div><dt>Latest / next cycle</dt><dd>{text(runner?.last_cycle_id, "none")} · {formatTimestamp(runner?.next_due_at)}</dd></div><div><dt>Restore verification</dt><dd>{text(restore?.verification_id, "not retained")} · {text(restore?.status, "missing")}</dd></div><div><dt>Recovery attempts</dt><dd>{records(value?.recovery_attempts).length}</dd></div><div><dt>Alert deliveries</dt><dd>{alerts.length}</dd></div><div><dt>Kill switch</dt><dd>{text(killSwitchDrill?.starting_state, "missing")} → {text(killSwitchDrill?.emergency_state, "missing")} → {text(killSwitchDrill?.final_state, "missing")}</dd></div><div><dt>Evidence lineage</dt><dd>{text(operationalHealth?.canonical_hash, "not retained")}</dd></div></div></Panel><Panel title="Coinbase read-only account capability" badge="PRIVATE VIEW · AUTHORITY NONE"><div className="key-values"><div><dt>Credential reference</dt><dd>{text(coinbaseCapture?.credential_reference_id, "absent or not inspected")}</dd></div><div><dt>Exact approval</dt><dd>{text(coinbaseApproval?.approval_id, "absent")} · expires {formatTimestamp(coinbaseApproval?.expires_at)}</dd></div><div><dt>Last inspection</dt><dd>{text(coinbaseAttempt?.attempt_id, "not run")} · {text(coinbaseAttempt?.status, "not run")}</dd></div><div><dt>Redacted account</dt><dd>{text(coinbaseCapture?.redacted_account_fingerprint, "no account evidence")} · {text(coinbaseCapture?.account_status)}</dd></div><div><dt>Permissions</dt><dd>{list(coinbaseCapture?.capability_classes).join(" · ") || "unknown"}</dd></div><div><dt>Fee evidence</dt><dd>{text(coinbaseCapture?.fee_tier, "tier missing")} · maker {text(coinbaseCapture?.maker_fee_rate, "missing")} · taker {text(coinbaseCapture?.taker_fee_rate, "missing")}</dd></div><div><dt>Balances</dt><dd>{coinbaseBalances.map((item) => `${text(item.currency)} available ${text(item.available_amount)} held ${text(item.held_amount, "unknown")}`).join(" · ") || "no balance evidence"}<small>READ-ONLY EVIDENCE — NOT APEX CASH OR TRADING CAPITAL</small></dd></div><div><dt>Product constraints</dt><dd>{coinbaseProducts.map((item) => `${text(item.product_id)} min base ${text(item.minimum_base_size)} min quote ${text(item.minimum_quote_size)}`).join(" · ") || "public evidence remains authoritative"}</dd></div><div><dt>Warnings / lineage</dt><dd>{list(coinbaseCapture?.warnings).join(" · ") || list(coinbaseAttempt?.warnings).join(" · ") || "none retained"}<small>{text(coinbaseCapture?.canonical_hash, "no capture lineage")}</small></dd></div></div><div className="control-row"><EvidenceButton href={coinbaseApproval ? `/api/v1/live-readiness/coinbase/read-approvals/${text(coinbaseApproval.approval_id)}` : undefined} label="Read-only approval" inspect={inspect} /><EvidenceButton href={coinbaseCapture ? `/api/v1/live-readiness/coinbase/captures/${text(coinbaseCapture.capture_id)}` : undefined} label="Redacted Coinbase evidence" inspect={inspect} /></div><small>Safe actions use exact API confirmations: register an opaque reference, approve or inspect read-only scope, run an approved inspection, reassess readiness, and export redacted evidence.</small></Panel><Panel title="Private-provider capabilities" badge="READ ONLY"><div className="scroll"><table><thead><tr><th scope="col">Provider / environment</th><th scope="col">Authenticated read</th><th scope="col">Permissions</th><th scope="col">Fees / minimums</th><th scope="col">Preview / trade / dangerous</th><th scope="col">Limitations</th><th scope="col">Evidence</th></tr></thead><tbody>{capabilities.map((item) => <tr key={text(item.snapshot_id)}><td>{text(item.provider)} / {text(item.account_environment)}<small>{text(item.account_scope_id)}</small></td><td>{text(item.authenticated_read_status)}<small>{text(item.account_status)}</small></td><td>{list(item.permissions).join(" · ") || "unproven"}</td><td>{pairs(item.fee_evidence).map(([key, amount]) => `${key} ${amount}`).join(" · ") || "missing"}<small>{pairs(item.minimum_size_constraints).map(([key, amount]) => `${key} ${amount}`).join(" · ") || "minimums missing"}</small></td><td>{text(item.preview_available)} / {text(item.trade_available)} / {text(Boolean(item.transfer_available || item.withdrawal_available))}</td><td>{list(item.limitations).join(" · ")}</td><td><EvidenceButton href={`/api/v1/live-readiness/capabilities/${text(item.snapshot_id)}`} label="Provider capability" inspect={inspect} /></td></tr>)}</tbody></table></div>{!capabilities.length && <Empty title="Credentials absent">No private capability snapshot is retained. The operating cycle makes no private request in this state.</Empty>}</Panel><Panel title="Live Preflight" badge="AUTHORITY NONE · ZERO WRITES">{livePreflight?.empty_state ? <Empty title="No eligible live intent">Current genuine retained state has no eligible Shared Opportunity or nonzero allocation, so Apex did not invent an intent.</Empty> : <div className="key-values"><div><dt>Eligible source lineage</dt><dd>{text(latestIntent?.source_opportunity_id)} → {text(latestIntent?.shared_projection_id)} → {text(latestIntent?.allocation_line_id)}<small>PAPER proposal {text(latestIntent?.paper_trial_proposal_id)}</small></dd></div><div><dt>Provider / account / product</dt><dd>{text(latestIntent?.provider)} / {text(latestIntent?.account_scope_id)} / {text(latestIntent?.product_or_market_id)}</dd></div><div><dt>Quantity / notional / price</dt><dd>{text(latestIntent?.requested_quantity, "n/a")} / {text(latestIntent?.requested_notional, "n/a")} / {text(latestIntent?.limit_price, "market")}</dd></div><div><dt>Fees / maximum loss</dt><dd>{text(latestInternalPreflight?.estimated_fees, "not run")} / {text(latestInternalPreflight?.maximum_loss, text(latestIntent?.maximum_risk))}</dd></div><div><dt>Account / product / risk checks</dt><dd>{pairs(latestInternalPreflight?.constraint_checks).map(([key, state]) => `${key} ${state}`).join(" · ") || "not run"}</dd></div><div><dt>Internal / provider preview</dt><dd>{text(latestInternalPreflight?.status, "not run")} / {text(latestProviderPreview?.status, "not run")}</dd></div><div><dt>Blockers / readiness effect</dt><dd>{list(latestPreflightAssessment?.blockers).join(" · ") || "none retained"}<small>{text(latestPreflightAssessment?.recommendation, "BLOCKED")}</small></dd></div><div><dt>Authority</dt><dd>{text(latestIntent?.authority, "NONE")} · LIVE {text(livePreflight?.live_authority, "NONE")}</dd></div></div>}<div className="control-row"><EvidenceButton href={latestIntent ? `/api/v1/live-preflight/intents/${text(latestIntent.intent_id)}` : undefined} label="Live intent" inspect={inspect} /><EvidenceButton href={latestInternalPreflight ? `/api/v1/live-preflight/internal/${text(latestInternalPreflight.preflight_id)}` : undefined} label="Internal preflight" inspect={inspect} /><EvidenceButton href={latestProviderPreview ? `/api/v1/live-preflight/provider-previews/${text(latestProviderPreview.capture_id)}` : undefined} label="Provider preview" inspect={inspect} /><EvidenceButton href={latestPreflightAssessment ? `/api/v1/live-preflight/assessments/${text(latestPreflightAssessment.assessment_id)}` : undefined} label="Preflight assessment" inspect={inspect} /></div><small>Safe exact-ID actions: {list(livePreflight?.safe_actions).join(" · ") || "inspect retained evidence"}. Prohibited: {list(livePreflight?.prohibited_actions).join(" · ") || "submit · cancel · replace · transfer · withdraw · live arming"}.</small></Panel><Panel title="Readiness blockers" badge="FAIL CLOSED"><ul>{list(value?.unresolved_blockers).map((item) => <li key={item}><code>{item}</code></li>)}</ul>{!list(value?.unresolved_blockers).length && <p className="muted">No assessment is retained; this does not imply readiness.</p>}<div className="control-row"><EvidenceButton href={assessment ? `/api/v1/live-readiness/assessments/${text(assessment.assessment_id)}` : undefined} label="Live-readiness assessment" inspect={inspect} /></div></Panel><Panel title="Supported safe controls" badge="EXACT CONFIRMATION"><p>{list(value?.safe_controls).join(" · ") || "No safe controls loaded."}</p><small>Prohibited: {list(value?.prohibited_controls).join(" · ") || "LIVE_PILOT_ARMED · provider writes"}</small></Panel></>;
}

function System({ navigate }: { navigate: (value: string) => void }) {
  return <><PageHead eyebrow="COMPATIBILITY AND SPECIALIST SURFACES" title="System" detail="Specialist tools remain available without crowding the default operator job navigation." /><div className="notice neutral">NO NEW PROVIDER, BROKER, FINANCIAL, PAPER, OR LIVE AUTHORITY</div><div className="system-grid">{systemDestinations.map(([group, destinations]) => <Panel title={group} key={group}>{destinations.map((destination) => <button className="system-link" key={destination} onClick={() => navigate(destination)}>{destination}</button>)}</Panel>)}</div></>;
}

export function OperatorConsolePage({ view, navigate }: { view: OperatorView; navigate: (value: string) => void }) {
  const [selected, setSelected] = useState<{ href: string; title: string } | null>(null);
  const close = useCallback(() => setSelected(null), []);
  const inspect = useCallback((href: string, title: string) => setSelected({ href, title }), []);
  // Move focus to the new page's own heading on every job-navigation
  // change (never on first mount, so initial page load never steals focus
  // from wherever the browser itself put it) -- otherwise a keyboard or
  // screen-reader user stays wherever focus happened to sit on the
  // previous page after choosing a new nav item. PageHead gives every page
  // the same stable #console-page-title target.
  const mountedOnce = useRef(false);
  useEffect(() => {
    if (!mountedOnce.current) {
      mountedOnce.current = true;
      return;
    }
    document.getElementById("console-page-title")?.focus();
  }, [view]);
  return <>{view === "Home" && <Home inspect={inspect} />}{view === "Attention" && <Attention inspect={inspect} />}{view === "Opportunities" && <Opportunities inspect={inspect} />}{view === "Decision Ledger" && <DecisionLedgerPage inspect={inspect} />}{view === "Follow-Up" && <FollowUpPage inspect={inspect} />}{view === "Options Research" && <OptionsResearchPage inspect={inspect} />}{view === "Portfolio / Mandate" && <PortfolioMandatePage inspect={inspect} />}{view === "Allocation" && <Allocation inspect={inspect} />}{view === "PAPER Operations" && <PaperOperations navigate={navigate} inspect={inspect} />}{view === "Learn" && <Learn inspect={inspect} />}{view === "Research Operations" && <ResearchOperations inspect={inspect} />}{view === "Operations" && <OperationsPage inspect={inspect} />}{view === "Live Readiness" && <LiveReadiness inspect={inspect} />}{view === "System" && <System navigate={navigate} />}<EvidenceDrawer selected={selected} close={close} /></>;
}
