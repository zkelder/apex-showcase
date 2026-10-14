import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ApiError,
  ApiRecord,
  DEMO_MODE,
  Campaign,
  Health,
  api,
  asItems,
  canonicalDecimal,
  formatTimestamp,
  mutate,
} from "./api";
import { DemoBanner } from "./demoBanner";
import { DemoShell } from "./demoShell";
import { OperatorConsolePage, OperatorView, operatorNavigation } from "./operatorConsole";
import "./styles.css";

const legacyNavigation = ["Command Center", "Overview", "Campaigns", "Portfolio", "Portfolio Intelligence", "Broad Market", "Crypto Research", "Crypto PAPER", "Kalshi Operating", "Options Intelligence", "Strategies", "Strategy Lab", "Strategy Intelligence", "Ensemble and Meta-Strategy", "Research Integrity", "Securities", "Securities Operations", "External PAPER", "Evidence Reviews", "Shadow Operations", "Activity"] as const;
type View = OperatorView | (typeof legacyNavigation)[number] | "Command" | "Opportunity" | "Strategy";
const navigation: readonly View[] = operatorNavigation;
type PortfolioTab = "Reservations" | "Orders" | "Open Positions" | "Settled Positions" | "Marks" | "Ledger";

type Overview = ApiRecord & { campaigns?: Campaign[] };
type CampaignDetail = ApiRecord & { portfolio?: ApiRecord; autopilot?: ApiRecord; multi_strategy?: ApiRecord; actions?: ApiRecord[] };
type CampaignData = {
  detail: CampaignDetail;
  cycles: ApiRecord[];
  opportunities: ApiRecord[];
  portfolio: ApiRecord;
  reservations: ApiRecord[];
  orders: ApiRecord[];
  positions: ApiRecord[];
  ledger: ApiRecord[];
  activity: ApiRecord[];
  scorecards: ApiRecord[];
  markEvidence: ApiRecord[];
};

function text(value: unknown, fallback = "—"): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
}

function list(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => text(item)) : [];
}

function record(value: unknown): ApiRecord | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as ApiRecord) : null;
}

function records(value: unknown): ApiRecord[] {
  return Array.isArray(value)
    ? value.filter((item): item is ApiRecord => Boolean(item) && typeof item === "object")
    : [];
}

function pairValue(value: unknown, key: string): unknown {
  if (!Array.isArray(value)) return undefined;
  const pair = value.find((item) => Array.isArray(item) && text(item[0]) === key);
  return Array.isArray(pair) ? pair[1] : undefined;
}

function decimal(value: unknown): ReactNode {
  return <span className="numeric">{text(value)}</span>;
}

function stateClass(value: unknown): string {
  return `state state-${text(value, "unknown").toLowerCase()}`;
}

function dateInputValue(date: Date): string {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.valueOf() - offset).toISOString().slice(0, 16);
}

function decimalCompare(left: string, right: string): number {
  const [leftWhole, leftFraction = ""] = left.split(".");
  const [rightWhole, rightFraction = ""] = right.split(".");
  const scale = Math.max(leftFraction.length, rightFraction.length);
  const leftValue = BigInt(`${leftWhole}${leftFraction.padEnd(scale, "0")}`);
  const rightValue = BigInt(`${rightWhole}${rightFraction.padEnd(scale, "0")}`);
  return leftValue === rightValue ? 0 : leftValue > rightValue ? 1 : -1;
}

function machineItems(value: unknown): ReactNode {
  const values = list(value);
  return values.length ? (
    <span className="codes">{values.map((item) => <code key={item}>{item}</code>)}</span>
  ) : <span className="muted">None retained</span>;
}

function fieldLabel(key: string): string {
  return key.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  const apiError = error instanceof ApiError ? error : null;
  return <div className="notice error"><strong>{apiError?.code ?? "UNEXPECTED_FAILURE"}</strong><span>{apiError?.message ?? "The local console could not complete that request."}</span></div>;
}

function Notice({ value }: { value: string }) {
  return value ? <div className="notice success">{value}</div> : null;
}

function Empty({ title, text: detail }: { title: string; text: string }) {
  return <div className="empty"><h2>{title}</h2><p>{detail}</p></div>;
}

function Panel({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return <article className="panel"><div className="panel-title"><h2>{title}</h2>{action}</div>{children}</article>;
}

function Metric({ label, value, note }: { label: string; value: ReactNode; note?: string }) {
  return <article className="metric"><p>{label}</p><strong>{value}</strong>{note && <small>{note}</small>}</article>;
}

function KeyValueGrid({ entries }: { entries: Array<[string, ReactNode]> }) {
  return <dl className="key-values">{entries.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}

function CampaignTable({ campaigns, openCampaign }: { campaigns: Campaign[]; openCampaign: (id: string) => void }) {
  if (!campaigns.length) return <Empty title="No Campaigns" text="Create a bounded Moonshot Campaign to begin local SHADOW or PAPER operations." />;
  return <div className="scroll"><table><thead><tr><th>Campaign</th><th>Mode</th><th>State</th><th>Funded</th><th>Loss limit</th><th>Expiration</th></tr></thead><tbody>{campaigns.map((campaign) => <tr key={campaign.campaign_id} className="clickable" onClick={() => openCampaign(campaign.campaign_id)}><td><strong>{campaign.name}</strong><small>{campaign.campaign_id}</small></td><td><span className="badge compact">{campaign.operating_mode}</span></td><td><span className={stateClass(campaign.state)}>{campaign.state}</span></td><td>{decimal(campaign.contribution)}</td><td>{decimal(campaign.authorized_loss)}</td><td>{formatTimestamp(campaign.expires_at)}</td></tr>)}</tbody></table></div>;
}

function CreateCampaign({ onCreated }: { onCreated: (campaignId: string, replay: boolean) => void }) {
  const now = useMemo(() => new Date(), []);
  const [open, setOpen] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [values, setValues] = useState<Record<string, string>>({
    name: "Moonshot local campaign",
    operating_mode: "SHADOW",
    initial_capital: "1000",
    maximum_authorized_loss: "500",
    starts_at: dateInputValue(now),
    expires_at: dateInputValue(new Date(now.valueOf() + 86_400_000)),
    maximum_active_risk_fraction: "0.5",
    maximum_per_opportunity_risk_fraction: "0.1",
    maximum_per_strategy_risk_fraction: "0.25",
    maximum_per_correlation_bucket_risk_fraction: "0.25",
    maximum_simultaneous_reservations: "4",
    maximum_simultaneous_positions: "4",
    maximum_capital_lockup_seconds: "3600",
    minimum_conservative_expected_value: "0",
    minimum_evidence_tier: "RESEARCH",
    allow_profit_compounding: "false",
    operator_note: "",
  });
  const update = (event: FormEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => {
    const target = event.currentTarget;
    setValues((current) => ({ ...current, [target.name]: target instanceof HTMLInputElement && target.type === "checkbox" ? String(target.checked) : target.value }));
  };
  const validation = (): string | null => {
    if (!values.name.trim()) return "Campaign name is required.";
    const decimals = ["initial_capital", "maximum_authorized_loss", "maximum_active_risk_fraction", "maximum_per_opportunity_risk_fraction", "maximum_per_strategy_risk_fraction", "maximum_per_correlation_bucket_risk_fraction", "minimum_conservative_expected_value"];
    if (decimals.some((name) => !canonicalDecimal(values[name]))) return "Financial and risk limits must use ordinary canonical decimal strings; exponent notation and floats are not accepted.";
    if (decimalCompare(values.maximum_authorized_loss, values.initial_capital) > 0) return "Maximum authorized loss cannot exceed initial capital.";
    if (new Date(values.expires_at).valueOf() <= new Date(values.starts_at).valueOf()) return "Expiration must follow the start time.";
    if (["maximum_simultaneous_reservations", "maximum_simultaneous_positions", "maximum_capital_lockup_seconds"].some((name) => !/^\d+$/.test(values[name]))) return "Reservation, Position, and lockup limits must be whole bounded values.";
    return null;
  };
  const review = (event: FormEvent) => {
    event.preventDefault();
    const message = validation();
    setError(message ? new ApiError(message, "FORM_VALIDATION") : null);
    setReviewing(!message);
  };
  const submit = async () => {
    setSubmitting(true); setError(null);
    try {
      const result = await mutate<{ campaign_id: string; idempotent_replay: boolean }>("/api/v1/campaigns", {
        ...values,
        starts_at: new Date(values.starts_at).toISOString(),
        expires_at: new Date(values.expires_at).toISOString(),
        maximum_simultaneous_reservations: Number(values.maximum_simultaneous_reservations),
        maximum_simultaneous_positions: Number(values.maximum_simultaneous_positions),
        maximum_capital_lockup_seconds: Number(values.maximum_capital_lockup_seconds),
        allow_profit_compounding: values.allow_profit_compounding === "true",
      });
      setOpen(false); setReviewing(false); onCreated(result.campaign_id, result.idempotent_replay);
    } catch (reason) { setError(reason); } finally { setSubmitting(false); }
  };
  const close = () => { setOpen(false); setReviewing(false); setError(null); };
  return <><button className="primary" onClick={() => setOpen(true)}>Create Moonshot Campaign</button>{open && <div className="modal-backdrop" role="presentation"><section className="dialog" role="dialog" aria-modal="true" aria-label="Create Moonshot Campaign"><div className="panel-title"><div><p className="eyebrow">LOCAL / BOUNDED / NO LIVE AUTHORITY</p><h2>Create Moonshot Campaign</h2></div><button className="quiet" onClick={close}>Close</button></div>{!reviewing ? <form className="form-grid" onSubmit={review}><label className="wide">Campaign name<input name="name" value={values.name} onChange={update} maxLength={160} required /></label><label>Operating mode<select name="operating_mode" value={values.operating_mode} onChange={update}><option value="SHADOW">SHADOW</option><option value="PAPER">PAPER</option></select></label><label>Initial capital<input name="initial_capital" value={values.initial_capital} onChange={update} inputMode="decimal" required /></label><label>Maximum authorized loss<input name="maximum_authorized_loss" value={values.maximum_authorized_loss} onChange={update} inputMode="decimal" required /></label><label>Start<input name="starts_at" type="datetime-local" value={values.starts_at} onChange={update} required /></label><label>Expiration<input name="expires_at" type="datetime-local" value={values.expires_at} onChange={update} required /></label><label>Maximum active-risk fraction<input name="maximum_active_risk_fraction" value={values.maximum_active_risk_fraction} onChange={update} inputMode="decimal" required /></label><label>Maximum per-opportunity fraction<input name="maximum_per_opportunity_risk_fraction" value={values.maximum_per_opportunity_risk_fraction} onChange={update} inputMode="decimal" required /></label><label>Maximum per-strategy fraction<input name="maximum_per_strategy_risk_fraction" value={values.maximum_per_strategy_risk_fraction} onChange={update} inputMode="decimal" required /></label><label>Maximum per-correlation-bucket fraction<input name="maximum_per_correlation_bucket_risk_fraction" value={values.maximum_per_correlation_bucket_risk_fraction} onChange={update} inputMode="decimal" required /></label><label>Maximum reservations<input name="maximum_simultaneous_reservations" value={values.maximum_simultaneous_reservations} onChange={update} inputMode="numeric" required /></label><label>Maximum Positions<input name="maximum_simultaneous_positions" value={values.maximum_simultaneous_positions} onChange={update} inputMode="numeric" required /></label><label>Maximum lockup (seconds)<input name="maximum_capital_lockup_seconds" value={values.maximum_capital_lockup_seconds} onChange={update} inputMode="numeric" required /></label><label>Minimum conservative expected value<input name="minimum_conservative_expected_value" value={values.minimum_conservative_expected_value} onChange={update} inputMode="decimal" required /></label><label>Minimum evidence tier<select name="minimum_evidence_tier" value={values.minimum_evidence_tier} onChange={update}><option>RESEARCH</option><option>VERIFIED</option><option>STRONG</option></select></label><label className="check"><input name="allow_profit_compounding" type="checkbox" checked={values.allow_profit_compounding === "true"} onChange={update} /> Allow profit compounding</label><label className="wide">Operator note<textarea name="operator_note" value={values.operator_note} onChange={update} maxLength={500} placeholder="Why this bounded local Campaign exists" /></label><ErrorNotice error={error} /><div className="actions wide"><button type="button" onClick={close}>Cancel</button><button className="primary">Review Campaign</button></div></form> : <div className="review"><p>Review the retained policy terms below. Financial values remain canonical decimal strings until the local API validates and persists them.</p><KeyValueGrid entries={Object.entries(values).map(([key, value]) => [fieldLabel(key), key.includes("capital") || key.includes("loss") || key.includes("fraction") || key.includes("expected") ? decimal(value) : value])} /><ErrorNotice error={error} /><div className="actions"><button onClick={() => setReviewing(false)}>Back to edit</button><button className="primary" disabled={submitting} onClick={() => void submit()}>{submitting ? "Creating…" : "Create bounded Campaign"}</button></div></div>}</section></div>}</>;
}

function Controls({ campaign, onRefresh, setNotice }: { campaign: CampaignDetail; onRefresh: () => Promise<void>; setNotice: (value: string) => void }) {
  const [selected, setSelected] = useState<"cycle" | "pause" | "resume" | "kill" | null>(null);
  const [reason, setReason] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const state = text(campaign.state);
  const campaignId = text(campaign.campaign_id);
  const mode = text(campaign.operating_mode);
  const expired = new Date(text(campaign.expires_at)).valueOf() <= Date.now();
  const canRun = state === "RUNNING" && !expired;
  const clear = () => { setSelected(null); setReason(""); setConfirmation(""); setError(null); };
  const open = (action: "cycle" | "pause" | "resume" | "kill") => { setSelected(action); setError(null); };
  const perform = async () => {
    if (!reason.trim()) { setError(new ApiError("A reason is required for every control.", "CONTROL_REASON_REQUIRED")); return; }
    if (selected === "kill" && confirmation !== campaignId) { setError(new ApiError("Type the exact Campaign ID to confirm kill.", "KILL_CONFIRMATION_REQUIRED")); return; }
    if (!selected) return;
    setPending(true); setError(null);
    try {
      let result: ApiRecord;
      if (selected === "cycle") {
        result = await mutate<ApiRecord>(`/api/v1/campaigns/${campaignId}/cycles`, { mode, maximum_strategies: 25, maximum_proposals: 100, operator_note: reason });
      } else {
        result = await mutate<ApiRecord>(`/api/v1/campaigns/${campaignId}/${selected}`, { operator_reason: reason, confirmation: selected === "kill" ? confirmation : null });
      }
      await onRefresh();
      const replay = Boolean(result.idempotent_replay);
      setNotice(`${selected === "cycle" ? `Run ${mode} cycle` : `${selected} Campaign`} completed${replay ? " as an idempotent replay" : ""}. Authoritative state was refreshed.`);
      clear();
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  const actionTitle = selected === "cycle" ? `Run ${mode} Cycle` : selected ? `${selected[0].toUpperCase()}${selected.slice(1)} Campaign` : "";
  return <Panel title="Campaign controls"><p className="muted">Every action is local, idempotent, and recorded. The console never exposes LIVE controls or provider submission.</p><div className="control-row"><button className="primary" disabled={!canRun} onClick={() => open("cycle")}>Run {mode === "PAPER" ? "Paper" : "Shadow"} Cycle</button><button disabled={state !== "RUNNING" || expired} onClick={() => open("pause")}>Pause</button>{state !== "KILLED" && <button disabled={state !== "PAUSED" || expired} onClick={() => open("resume")}>Resume</button>}<button className="danger" disabled={state === "KILLED"} onClick={() => open("kill")}>Kill</button></div>{!canRun && <p className="disabled-explanation">{expired ? "Campaign expiration disables further cycles and controls." : state === "PAUSED" ? "Pause is active; resume is required before a cycle can run." : state === "KILLED" ? "Killed Campaigns are terminal and cannot resume." : "The retained Campaign state does not permit another cycle."}</p>}{selected && <div className="control-form"><h3>{actionTitle}</h3><label>Required operator reason<textarea value={reason} onChange={(event) => setReason(event.currentTarget.value)} maxLength={500} autoFocus /></label>{selected === "kill" && <label>Type exact Campaign ID to kill<input value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} placeholder={campaignId} /></label>}<ErrorNotice error={error} /><div className="actions"><button onClick={clear}>Cancel</button><button className={selected === "kill" ? "danger" : "primary"} onClick={() => void perform()} disabled={pending}>{pending ? "Submitting…" : actionTitle}</button></div></div>}</Panel>;
}

function OpportunityTable({ opportunities, openOpportunity }: { opportunities: ApiRecord[]; openOpportunity: (id: string) => void }) {
  if (!opportunities.length) return <Empty title="Zero opportunities" text="No ranked proposals were retained for this Campaign yet. A run can still retain explicit no-runnable-strategy evidence." />;
  return <div className="scroll"><table><thead><tr><th>Rank</th><th>Strategy</th><th>Instrument</th><th>Evidence</th><th>Point EV</th><th>Conservative EV</th><th>Maximum loss</th><th>Portfolio</th><th>Allocation / risk</th><th>Reasons</th></tr></thead><tbody>{opportunities.map((item) => <tr className="clickable" key={text(item.proposal_id)} onClick={() => openOpportunity(text(item.proposal_id))}><td>{text(item.ranking_ordinal)}</td><td>{text(item.strategy_id)}<small>{text(item.strategy_version)}</small></td><td>{text(item.instrument_id)}</td><td>{text(item.evidence_tier)}</td><td>{decimal(item.point_net_expected_value)}</td><td>{decimal(item.conservative_net_expected_value)}</td><td>{decimal(item.maximum_loss)}</td><td>{text(item.validity)}</td><td>{text(item.allocation_accepted, "—")} / {text(item.risk_accepted, "—")}</td><td>{machineItems(item.reason_codes)}{machineItems(item.risk_reason_codes)}</td></tr>)}</tbody></table></div>;
}

function PortfolioSummary({ portfolio }: { portfolio: ApiRecord }) {
  return <div className="metrics compact-metrics"><Metric label="Reservation risk" value={decimal(portfolio.active_order_reservation_risk)} /><Metric label="Open Position risk" value={decimal(portfolio.open_position_risk)} /><Metric label="Remaining loss capacity" value={decimal(portfolio.remaining_campaign_loss_capacity)} /><Metric label="Open / settled Positions" value={`${text(portfolio.open_positions)} / ${text(portfolio.settled_positions)}`} /><Metric label="Realized result" value={decimal(portfolio.net_realized_paper_pnl)} /><Metric label="Conservative marked result" value={decimal(portfolio.conservative_unrealized_paper_pnl)} /><Metric label="Informational midpoint" value={decimal(portfolio.informational_midpoint_value)} /><Metric label="Orders by state" value={Object.entries(record(portfolio.orders_by_state) ?? {}).map(([key, value]) => `${key}: ${text(value)}`).join(" · ") || "None"} /></div>;
}

function ActivityTable({ activity, compact = false }: { activity: ApiRecord[]; compact?: boolean }) {
  if (!activity.length) return <Empty title="No Activity" text="No linked operator actions or retained Campaign evidence match this bounded view." />;
  const rows = compact ? activity.slice(0, 8) : activity;
  return <div className="scroll"><table><thead><tr><th>Timestamp</th><th>Actor</th><th>Action</th><th>Campaign</th><th>Target</th><th>Result</th><th>Reason codes</th></tr></thead><tbody>{rows.map((item, index) => <tr key={`${text(item.target_id)}-${index}`}><td>{formatTimestamp(item.timestamp)}</td><td>{text(item.actor)}</td><td>{text(item.action)}</td><td><small>{text(item.campaign_id)}</small></td><td><small>{text(item.target_id)}</small></td><td><span className={stateClass(item.result_state)}>{text(item.result_state)}</span></td><td>{machineItems(item.reason_codes)}</td></tr>)}</tbody></table></div>;
}

function ThesisComposer({ campaignId, onSubmitted }: { campaignId: string; onSubmitted: () => Promise<void> }) {
  const [marketId, setMarketId] = useState("");
  const [market, setMarket] = useState<ApiRecord | null>(null);
  const [marketProbability, setMarketProbability] = useState("—");
  const [values, setValues] = useState({ pointYes: "0.5", conservativeYes: "0.5", resolutionAt: dateInputValue(new Date(Date.now() + 86_400_000)), riskFraction: "0.05", thesis: "", evidence: "", correlations: "", note: "" });
  const [result, setResult] = useState<ApiRecord | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const update = (event: FormEvent<HTMLInputElement | HTMLTextAreaElement>) => setValues((current) => ({ ...current, [event.currentTarget.name]: event.currentTarget.value }));
  const loadMarket = async () => {
    if (!marketId.trim()) { setError(new ApiError("An exact retained market snapshot ID is required.", "RETAINED_MARKET_REQUIRED")); return; }
    setPending(true); setError(null); setResult(null);
    try {
      const loaded = await api<{ market: ApiRecord; market_probability: string }>(`/api/v1/retained-markets/${encodeURIComponent(marketId.trim())}`);
      setMarket(loaded.market); setMarketProbability(loaded.market_probability);
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!market || !values.thesis.trim() || !values.evidence.trim() || !values.correlations.trim()) { setError(new ApiError("Load exact retained market evidence and complete every thesis field.", "THESIS_FIELDS_REQUIRED")); return; }
    const pointYes = canonicalDecimal(values.pointYes) ? values.pointYes : "";
    const conservativeYes = canonicalDecimal(values.conservativeYes) ? values.conservativeYes : "";
    if (!pointYes || !conservativeYes || !canonicalDecimal(values.riskFraction)) { setError(new ApiError("Probabilities and requested risk must be canonical decimal strings.", "THESIS_DECIMAL_INVALID")); return; }
    const complement = (value: string) => {
      const [whole, fraction = ""] = value.split("."); const scale = Math.max(12, fraction.length); const unit = 10n ** BigInt(scale); const numerator = BigInt(`${whole}${fraction.padEnd(scale, "0")}`); return `${unit - numerator}`.replace(new RegExp(`(\\d{${scale}})$`), ".$1").replace(/^0+(?=\d)/, "").replace(/^\./, "0.").replace(/\.?0+$/, "") || "0";
    };
    setPending(true); setError(null);
    try {
      const response = await mutate<ApiRecord>(`/api/v1/campaigns/${campaignId}/theses`, {
        market_snapshot_id: marketId.trim(),
        point_probabilities: { NO: complement(pointYes), YES: pointYes },
        conservative_probabilities: { NO: complement(conservativeYes), YES: conservativeYes },
        resolution_at: new Date(values.resolutionAt).toISOString(), requested_risk_fraction: values.riskFraction,
        thesis_summary: values.thesis, evidence_references: values.evidence.split(",").map((item) => item.trim()).filter(Boolean),
        correlation_buckets: values.correlations.split(",").map((item) => item.trim()).filter(Boolean).sort(), operator_note: values.note,
      });
      setResult(response); await onSubmitted();
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  return <Panel title="Operator Thesis / SHADOW only" action={<span className="badge">NO FINANCIAL STATE</span>}><p className="muted">Load an exact retained public market first. The operator supplies only forward probabilities and untrusted research notes; market terms and all decision mathematics come from retained evidence.</p><div className="inline-action"><input value={marketId} onChange={(event) => setMarketId(event.currentTarget.value)} placeholder="Retained market snapshot ID" aria-label="Retained market snapshot ID" /><button onClick={() => void loadMarket()} disabled={pending}>Load retained market</button></div>{market && <KeyValueGrid entries={[["Market", text(market.provider_market_ticker)], ["Event", text(market.provider_event_ticker)], ["Retained YES probability", decimal(marketProbability)], ["YES ask / NO ask", `${text(market.yes_ask)} / ${text(market.no_ask)}`], ["Rule", text(market.rule_text)], ["Observed", formatTimestamp(market.observed_at)], ["Closes", formatTimestamp(market.close_at)], ["Fee model / expected fee", `${text(market.fee_model_version)} / ${text(market.expected_fee)}`]]} />}{market && <form className="form-grid" onSubmit={submit}><label>YES point probability<input name="pointYes" value={values.pointYes} onChange={update} inputMode="decimal" required /></label><label>YES conservative probability<input name="conservativeYes" value={values.conservativeYes} onChange={update} inputMode="decimal" required /></label><label>Expected resolution<input name="resolutionAt" type="datetime-local" value={values.resolutionAt} onChange={update} required /></label><label>Requested risk fraction<input name="riskFraction" value={values.riskFraction} onChange={update} inputMode="decimal" required /></label><label className="wide">Thesis summary<textarea name="thesis" value={values.thesis} onChange={update} maxLength={4000} required /></label><label>Evidence references (comma-separated)<input name="evidence" value={values.evidence} onChange={update} required /></label><label>Correlation buckets (comma-separated)<input name="correlations" value={values.correlations} onChange={update} required /></label><label className="wide">Operator note (untrusted text)<textarea name="note" value={values.note} onChange={update} maxLength={500} /></label><div className="wide notice neutral">Preview terms: YES point {decimal(values.pointYes)}, YES conservative {decimal(values.conservativeYes)}, retained total acquisition term {decimal(`${text(market.yes_ask)} + ${text(market.expected_fee)}`)}. The local API returns the authoritative point/conservative EV, maximum loss/upside, and capital efficiency in the retained SHADOW decision chain.</div><div className="actions wide"><button className="primary" disabled={pending}>{pending ? "Submitting…" : "Submit to SHADOW"}</button></div></form>}{result && <KeyValueGrid entries={[["Prediction", text(record(result.prediction)?.prediction_id)], ["Cycle", text(record(result.cycle)?.cycle_id)], ["Assessment", text(record(result.assessment)?.validity)], ["Replay", text(result.idempotent_replay)]]} />}{!market && <p className="disabled-explanation">No Trade button exists. This workflow cannot create reservations, Orders, Positions, or ledger entries.</p>}<ErrorNotice error={error} /></Panel>;
}

function PredictionTable({ campaignId }: { campaignId: string }) {
  const [items, setItems] = useState<ApiRecord[]>([]);
  const [strategy, setStrategy] = useState("");
  const [state, setState] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    const query = new URLSearchParams(); if (strategy) query.set("strategy_id", strategy); if (state) query.set("resolved", state); const value = await api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/predictions?${query.toString()}`); setItems(asItems(value));
  }, [campaignId, strategy, state]);
  useEffect(() => { void refresh().catch(setError); }, [refresh]);
  const filtered = items.filter((item) => (!start || new Date(text(item.decision_at)).valueOf() >= new Date(start).valueOf()) && (!end || new Date(text(item.decision_at)).valueOf() <= new Date(end).valueOf()));
  return <Panel title="Forward predictions and outcomes"><div className="filters"><label>Strategy<select value={strategy} onChange={(event) => setStrategy(event.currentTarget.value)}><option value="">All SHADOW strategies</option><option value="operator-thesis-v1">Operator Thesis</option><option value="market-consensus-baseline-v1">Market Consensus Baseline</option></select></label><label>Evaluation state<select value={state} onChange={(event) => setState(event.currentTarget.value)}><option value="">All</option><option value="false">Unresolved</option><option value="true">Resolved</option></select></label><label>Decision from<input type="datetime-local" value={start} onChange={(event) => setStart(event.currentTarget.value)} /></label><label>Decision to<input type="datetime-local" value={end} onChange={(event) => setEnd(event.currentTarget.value)} /></label></div>{filtered.length ? <div className="scroll"><table><thead><tr><th>Strategy</th><th>Instrument / event</th><th>Decision / resolution</th><th>Forecast</th><th>Market baseline</th><th>Assessment</th><th>State</th><th>Evidence</th></tr></thead><tbody>{filtered.map((item) => <tr key={text(item.prediction_id)}><td>{text(item.strategy_id)}<small>{text(item.strategy_version)}</small></td><td>{text(item.instrument_id)}<small>{text(item.event_id)}</small></td><td>{formatTimestamp(item.decision_at)}<small>{formatTimestamp(item.resolution_at)}</small></td><td><small>{text(item.point_probabilities_payload)}<br />conservative {text(item.conservative_probabilities_payload)}</small></td><td><small>{text(item.market_probabilities_payload)}</small></td><td>{text(item.proposal_id) === "—" ? "Baseline only" : <small>{text(item.proposal_id)}</small>}</td><td><span className={stateClass(item.state)}>{text(item.state)}</span></td><td>{machineItems(item.evidence_references)}</td></tr>)}</tbody></table></div> : <Empty title="No forward predictions" text="A retained SHADOW thesis or baseline session will appear here. Forecasts, market baselines, realized outcomes, and Paper financial state remain distinct." />}<ErrorNotice error={error} /></Panel>;
}

function CommandCenter({ campaignId, openOpportunity, openPortfolio, onBack, setNotice }: { campaignId: string; openOpportunity: (id: string) => void; openPortfolio: (tab?: PortfolioTab) => void; onBack: () => void; setNotice: (value: string) => void }) {
  const [data, setData] = useState<CampaignData | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [detail, cycles, opportunities, portfolio, reservations, orders, positions, ledger, activity, scorecards, markEvidence] = await Promise.all([
        api<CampaignDetail>(`/api/v1/campaigns/${campaignId}`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/cycles`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/opportunities`), api<ApiRecord>(`/api/v1/campaigns/${campaignId}/portfolio`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/reservations`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/orders`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/positions`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/ledger`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/activity`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/strategies/scorecards`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/mark-evidence`),
      ]);
      setData({ detail, cycles: asItems(cycles), opportunities: asItems(opportunities), portfolio, reservations: asItems(reservations), orders: asItems(orders), positions: asItems(positions), ledger: asItems(ledger), activity: asItems(activity), scorecards: asItems(scorecards), markEvidence: asItems(markEvidence) }); setError(null);
    } catch (reason) { setError(reason); } finally { setLoading(false); }
  }, [campaignId]);
  useEffect(() => { void refresh(); }, [refresh]);
  if (loading && !data) return <Empty title="Loading Campaign command center" text="Loading retained Campaign, risk, portfolio, and activity evidence from the local database." />;
  if (!data) return <><ErrorNotice error={error} /><button onClick={onBack}>Back to Campaigns</button></>;
  const campaign = data.detail;
  const autopilot = record(campaign.autopilot) ?? {};
  const portfolio = data.portfolio;
  return <><div className="section-head"><div><p className="eyebrow">LOCAL CAMPAIGN COMMAND CENTER</p><h1>{text(campaign.name)}</h1><p className="stable-id">{campaignId}</p></div><div className="header-actions"><span className="badge">{text(campaign.operating_mode)}</span><span className={stateClass(campaign.state)}>{text(campaign.state)}</span><button onClick={onBack}>All Campaigns</button></div></div><ErrorNotice error={error} /><div className="metrics"><Metric label="Funded capital" value={decimal(campaign.contribution)} /><Metric label="Maximum authorized loss" value={decimal(campaign.authorized_loss)} /><Metric label="Remaining loss capacity" value={decimal(portfolio.remaining_campaign_loss_capacity)} /><Metric label="Active risk" value={decimal(portfolio.total_active_risk)} /><Metric label="Start" value={formatTimestamp(campaign.starts_at)} /><Metric label="Expiration" value={formatTimestamp(campaign.expires_at)} /></div><Controls campaign={campaign} onRefresh={refresh} setNotice={setNotice} /><MultiStrategyEvaluation campaignId={campaignId} initialCycles={records(record(campaign.multi_strategy)?.cycles)} onRefresh={refresh} /><ThesisComposer campaignId={campaignId} onSubmitted={refresh} /><PredictionTable campaignId={campaignId} /><Panel title="Opportunity queue"><p className="muted">All retained opportunities remain visible, including rejected portfolio and risk decisions. Model estimates are not financial facts or execution authority.</p><OpportunityTable opportunities={data.opportunities} openOpportunity={openOpportunity} /></Panel><Panel title="Portfolio summary" action={<button onClick={() => openPortfolio()}>Open portfolio views</button>}><PortfolioSummary portfolio={portfolio} /></Panel><Panel title="Strategy allocation"><StrategyRows scorecards={data.scorecards} opportunities={data.opportunities} invocations={asItems(autopilot.strategy_invocations)} /></Panel><Panel title="Activity timeline"><ActivityTable activity={data.activity} compact /></Panel><Panel title="Recent cycles"><CycleTable cycles={data.cycles} /></Panel></>;
}

function MultiStrategyEvaluation({ campaignId, initialCycles, onRefresh }: { campaignId: string; initialCycles: ApiRecord[]; onRefresh: () => Promise<void> }) {
  const [cycles, setCycles] = useState<ApiRecord[]>(initialCycles);
  const [detail, setDetail] = useState<ApiRecord | null>(null);
  const [iterations, setIterations] = useState("4");
  const [interval, setInterval] = useState("30");
  const [marketLimit, setMarketLimit] = useState("20");
  const [pages, setPages] = useState("3");
  const [enrichments, setEnrichments] = useState("40");
  const [depth, setDepth] = useState("5");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    const value = await api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/multi-strategy-cycles`);
    setCycles(asItems(value));
  }, [campaignId]);
  useEffect(() => { void refresh().catch(setError); }, [refresh]);
  const run = async () => {
    setSubmitting(true); setError(null);
    try {
      await mutate<ApiRecord>(`/api/v1/campaigns/${campaignId}/multi-strategy-shadow-session`, {
        iterations: Number(iterations), interval_seconds: Number(interval), market_limit: Number(marketLimit), maximum_pages: Number(pages), maximum_enrichments: Number(enrichments), orderbook_depth: Number(depth),
      });
      await Promise.all([refresh(), onRefresh()]);
    } catch (failure) { setError(failure); } finally { setSubmitting(false); }
  };
  const inspect = async (cycleId: string) => {
    try { setDetail(await api<ApiRecord>(`/api/v1/multi-strategy-cycles/${cycleId}`)); setError(null); } catch (failure) { setError(failure); }
  };
  const cycle = record(detail?.cycle) ?? {};
  return <Panel title="Multi-Strategy Evaluation" action={<span className="badge">SHADOW / HYPOTHETICAL</span>}><p className="disabled-explanation">SHADOW / HYPOTHETICAL — NO CAPITAL RESERVED. This foreground public-data session uses one bounded shared cohort, does not average forecasts, and cannot create an Order, reservation, Position, settlement, or ledger entry.</p><div className="form-grid"><label>Iterations<input value={iterations} onChange={(event) => setIterations(event.currentTarget.value)} disabled={submitting} /></label><label>Interval seconds<input value={interval} onChange={(event) => setInterval(event.currentTarget.value)} disabled={submitting} /></label><label>Market limit<input value={marketLimit} onChange={(event) => setMarketLimit(event.currentTarget.value)} disabled={submitting} /></label><label>Pages<input value={pages} onChange={(event) => setPages(event.currentTarget.value)} disabled={submitting} /></label><label>Enrichments<input value={enrichments} onChange={(event) => setEnrichments(event.currentTarget.value)} disabled={submitting} /></label><label>Orderbook depth<input value={depth} onChange={(event) => setDepth(event.currentTarget.value)} disabled={submitting} /></label></div><button className="primary" onClick={() => void run()} disabled={submitting}>{submitting ? "Running bounded foreground session…" : "Run Multi-Strategy Shadow Session"}</button><ErrorNotice error={error} />{cycles.length ? <div className="scroll"><table><thead><tr><th>Started / state</th><th>Cohort</th><th>Strategies / predictions</th><th>Opportunities / conflicts</th><th>Requested / allocated risk</th><th>Capacity</th><th /></tr></thead><tbody>{cycles.map((item) => <tr key={text(item.cycle_id)}><td>{formatTimestamp(item.started_at)}<small>{text(item.state)}</small></td><td>{text(item.selected_market_count)}</td><td>{text(item.invocation_count)} / {text(item.prediction_count)}</td><td>{text(item.opportunity_count)} / {text(item.conflict_count)}</td><td>{decimal(item.requested_risk_total)} / {decimal(item.allocated_risk_total)}</td><td>{decimal(item.remaining_shadow_capacity)}</td><td><button onClick={() => void inspect(text(item.cycle_id))}>Inspect</button></td></tr>)}</tbody></table></div> : <Empty title="No retained multi-strategy cycle" text="Run one bounded foreground session to retain independently scored prediction comparison and hypothetical capital-plan evidence." />}{detail && <div className="stack"><Panel title={`Cycle detail · ${text(cycle.cycle_id)}`}><KeyValueGrid entries={[["State", text(cycle.state)], ["Configuration", text(cycle.cohort_configuration_hash)], ["Reason", text(cycle.last_error_code)], ["Remaining shadow capacity", decimal(cycle.remaining_shadow_capacity)]]} /><p className="muted">Predictions remain independent; the comparison below never combines or averages their probabilities.</p></Panel><Panel title="Shared cohort and strategy invocations"><div className="scroll"><table><thead><tr><th>Market / event / series</th><th>Rule lineage</th><th>Strategy</th><th>Output / reasons</th></tr></thead><tbody>{records(detail.cohort).map((item, index) => <tr key={text(item.cohort_member_id)}><td>{text(item.market_ticker)}<small>{text(item.event_ticker)} / {text(item.series_ticker)}</small></td><td><small>{text(item.rule_content_hash)}</small></td><td>{index === 0 ? records(detail.invocations).map((invocation) => <small key={text(invocation.invocation_id)}>{text(invocation.strategy_id)} · {text(invocation.state)}<br /></small>) : ""}</td><td>{index === 0 ? records(detail.invocations).map((invocation) => <small key={`${text(invocation.invocation_id)}-results`}>{text(invocation.prediction_ids).replace(/^$/, "NO_PREDICTION")} · {machineItems(invocation.reason_codes)}<br /></small>) : ""}</td></tr>)}</tbody></table></div></Panel><Panel title="Independent prediction comparison"><div className="scroll"><table><thead><tr><th>Market</th><th>State</th><th>Consensus YES</th><th>Microstructure YES / conservative</th><th>Dynamics YES / conservative</th><th>Agreement / reasons</th></tr></thead><tbody>{records(detail.comparisons).map((item) => <tr key={text(item.comparison_id)}><td>{text(item.market_ticker)}</td><td>{text(item.state)}</td><td>{decimal(item.consensus_yes_probability)}</td><td>{decimal(item.microstructure_yes_probability)} / {decimal(item.microstructure_conservative_yes_probability)}</td><td>{decimal(item.dynamics_yes_probability)} / {decimal(item.dynamics_conservative_yes_probability)}</td><td>{text(item.directional_agreement)}<small>{machineItems(item.reason_codes)}</small></td></tr>)}</tbody></table></div></Panel><Panel title="Conflicts and hypothetical capital plan"><div className="scroll"><table><thead><tr><th>Instrument / strategy</th><th>Conflict / resolution</th><th>Rank</th><th>Requested / permitted / allocated</th><th>Constraint / reason</th></tr></thead><tbody>{records(detail.conflicts).map((item) => <tr key={text(item.conflict_id)}><td>{text(item.instrument_id)}<small>{machineItems(item.strategy_ids)}</small></td><td>{text(item.conflict_type)} / {text(item.resolution)}</td><td>—</td><td>—</td><td>{machineItems(item.reason_codes)}</td></tr>)}{records(detail.allocations).map((item) => <tr key={text(item.allocation_id)}><td>{text(item.instrument_group)}<small>{text(item.strategy_id)}</small></td><td>—</td><td>{text(item.ranking_ordinal)}</td><td>{decimal(item.requested_risk)} / {decimal(item.policy_permitted_risk)} / {decimal(item.hypothetical_allocated_risk)}</td><td>{machineItems(item.binding_constraints)}<small>{machineItems(item.reason_codes)}</small></td></tr>)}</tbody></table></div></Panel></div>}</Panel>;
}

function CycleTable({ cycles }: { cycles: ApiRecord[] }) {
  if (!cycles.length) return <Empty title="Campaign with no cycles" text="No cycle has run for this Campaign. Running a matching local mode records bounded evidence only." />;
  return <div className="scroll"><table><thead><tr><th>Started</th><th>State</th><th>Invocations</th><th>Assessments</th><th>Approved</th><th>Rejected</th><th>Reason</th></tr></thead><tbody>{cycles.map((cycle) => <tr key={text(cycle.cycle_id)}><td>{formatTimestamp(cycle.started_at)}</td><td><span className={stateClass(cycle.state)}>{text(cycle.state)}</span></td><td>{text(cycle.invocation_count)}</td><td>{text(cycle.assessment_count)}</td><td>{text(cycle.approved_count)}</td><td>{text(cycle.rejected_count)}</td><td>{text(cycle.last_error_code)}</td></tr>)}</tbody></table></div>;
}

function StrategyRows({ scorecards, opportunities, invocations }: { scorecards: ApiRecord[]; opportunities: ApiRecord[]; invocations: ApiRecord[] }) {
  if (!scorecards.length && !invocations.length) return <Empty title="No runnable strategies" text="This Campaign has no retained strategy invocation. Research, deprioritized, or disabled strategies remain non-runnable." />;
  const combined = new Map<string, ApiRecord>();
  scorecards.forEach((item) => combined.set(`${text(item.strategy_id)}:${text(item.strategy_version)}`, item));
  invocations.forEach((item) => { const key = `${text(item.strategy_id)}:${text(item.strategy_version)}`; if (!combined.has(key)) combined.set(key, item); });
  return <div className="scroll"><table><thead><tr><th>Strategy</th><th>Invocations</th><th>Proposals</th><th>Paper Orders</th><th>Open / settled</th><th>Active risk</th><th>Result / health</th></tr></thead><tbody>{[...combined.values()].map((item) => { const id = text(item.strategy_id); const matching = opportunities.filter((opportunity) => text(opportunity.strategy_id) === id); return <tr key={`${id}-${text(item.strategy_version)}`}><td><strong>{id}</strong><small>{text(item.strategy_version)}</small></td><td>{invocations.filter((invocation) => text(invocation.strategy_id) === id).length}</td><td>{matching.length}</td><td>{text(item.paper_orders, "0")}</td><td>{text(item.open_positions, "0")} / {text(item.settled_positions, "0")}</td><td>{decimal(item.maximum_active_risk)}</td><td>{machineItems(item.health_reason_codes)}</td></tr>; })}</tbody></table></div>;
}

function OpportunityDetail({ opportunityId, onBack, openPortfolio }: { opportunityId: string; onBack: () => void; openPortfolio: () => void }) {
  const [opportunity, setOpportunity] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { void api<ApiRecord>(`/api/v1/opportunities/${opportunityId}`).then(setOpportunity).catch(setError); }, [opportunityId]);
  if (!opportunity) return <><button onClick={onBack}>Back to Campaign</button><ErrorNotice error={error} /><Empty title="Loading Opportunity decision chain" text="Loading the immutable proposal, economics, allocation, and risk evidence." /></>;
  const outcomes = opportunity.outcomes;
  const ranking = record(opportunity.ranking_inputs);
  const allocation = [["Allocation ID", text(opportunity.allocation_id)], ["Allocation accepted", text(opportunity.allocation_accepted)], ["Allocation quantity", decimal(opportunity.allocation_quantity)], ["Allocation maximum loss", decimal(opportunity.allocation_maximum_loss)]] as Array<[string, ReactNode]>;
  const risk = [["Risk decision ID", text(opportunity.risk_decision_id)], ["Risk accepted", text(opportunity.risk_accepted)], ["Risk reason codes", machineItems(opportunity.risk_reason_codes)]] as Array<[string, ReactNode]>;
  return <><div className="section-head"><div><p className="eyebrow">OPPORTUNITY / IMMUTABLE DECISION CHAIN</p><h1>{text(opportunity.instrument_id)}</h1><p className="stable-id">{opportunityId}</p></div><button onClick={onBack}>Back to Campaign</button></div><div className="notice neutral">Point and conservative probabilities, payout values, and EV are strategy model estimates. The only financial facts appear after a separate retained PAPER execution and ledger posting.</div><Panel title="Lineage and ranking"><KeyValueGrid entries={[["Strategy", `${text(opportunity.strategy_id)} / ${text(opportunity.strategy_version)}`], ["Cycle", text(opportunity.cycle_id)], ["Invocation", text(opportunity.invocation_id)], ["Decision timestamp", formatTimestamp(opportunity.decision_at)], ["Capital release / resolution timestamp", formatTimestamp(opportunity.capital_release_at)], ["Evidence tier", text(opportunity.evidence_tier)], ["Evidence references", machineItems(opportunity.evidence_references)], ["Correlation buckets", machineItems(opportunity.correlation_buckets)], ["Ranking ordinal", text(opportunity.ranking_ordinal)], ["Ranking inputs", ranking ? <pre>{JSON.stringify(ranking, null, 2)}</pre> : "None retained"]]} /></Panel><Panel title="Outcome probabilities and payouts"><OutcomeTable outcomes={outcomes} /></Panel><Panel title="Mathematical decision chain"><KeyValueGrid entries={[["Acquisition cost", decimal(opportunity.acquisition_cost)], ["Expected fee", decimal(opportunity.expected_fee)], ["Slippage reserve", decimal(opportunity.slippage_reserve)], ["Execution-uncertainty reserve", decimal(opportunity.execution_uncertainty_reserve)], ["Total modeled acquisition cost", decimal(opportunity.total_modeled_acquisition_cost)], ["Point expected payout", decimal(opportunity.point_expected_payout)], ["Conservative expected payout", decimal(opportunity.conservative_expected_payout)], ["Point net EV", decimal(opportunity.point_net_expected_value)], ["Conservative net EV", decimal(opportunity.conservative_net_expected_value)], ["Minimum payout", decimal(opportunity.minimum_payout)], ["Maximum payout", decimal(opportunity.maximum_payout)], ["Maximum loss", decimal(opportunity.maximum_loss)], ["Maximum upside", decimal(opportunity.maximum_upside)], ["Capital efficiency", decimal(opportunity.capital_efficiency)], ["Visible quantity", decimal(opportunity.visible_quantity)], ["Requested risk fraction", decimal(opportunity.requested_risk_fraction)]]} /></Panel><Panel title="Portfolio, allocation, and risk decisions"><KeyValueGrid entries={[["Portfolio assessment", text(opportunity.validity)], ["Portfolio reason codes", machineItems(opportunity.reason_codes)], ...allocation, ...risk]} /></Panel><Panel title="Downstream lineage">{opportunity.order ? <KeyValueGrid entries={[["Order", text(record(opportunity.order)?.order_id)], ["Order state", text(record(opportunity.order)?.state)], ["Position", text(record(opportunity.position)?.position_id)], ["Position settlement state", text(record(opportunity.position)?.settlement_state)]]} /> : <Empty title="No downstream Order" text="This opportunity stopped before a retained Order or Position was authorized." />}<button onClick={openPortfolio}>Open portfolio lineage</button></Panel></>;
}

function OutcomeTable({ outcomes }: { outcomes: unknown }) {
  const rows: ApiRecord[] = Array.isArray(outcomes)
    ? records(outcomes)
    : Object.entries(record(outcomes) ?? {}).map<ApiRecord>(([outcomeId, value]) => ({
        outcome_id: outcomeId,
        ...(record(value) ?? {}),
      }));
  if (!rows.length) return <Empty title="No outcome payload" text="The retained opportunity has no outcome payload to display." />;
  return <div className="scroll"><table><thead><tr><th>Outcome</th><th>Point probability</th><th>Conservative probability</th><th>Payout</th></tr></thead><tbody>{rows.map((item, index) => <tr key={`${text(item.outcome_id)}-${index}`}><td>{text(item.outcome_id)}</td><td>{decimal(item.point_probability ?? item.point_estimate)}</td><td>{decimal(item.conservative_probability ?? item.conservative_estimate)}</td><td>{decimal(item.payout ?? item.payout_value)}</td></tr>)}</tbody></table></div>;
}

function PortfolioWorkspace({ campaignId, initialTab, onBack, setNotice }: { campaignId: string; initialTab: PortfolioTab; onBack: () => void; setNotice: (value: string) => void }) {
  const [tab, setTab] = useState<PortfolioTab>(initialTab);
  const [data, setData] = useState<Pick<CampaignData, "portfolio" | "reservations" | "orders" | "positions" | "ledger" | "markEvidence"> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => { try { const [portfolio, reservations, orders, positions, ledger, markEvidence] = await Promise.all([api<ApiRecord>(`/api/v1/campaigns/${campaignId}/portfolio`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/reservations`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/orders`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/positions`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/ledger`), api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/mark-evidence`)]); setData({ portfolio, reservations: asItems(reservations), orders: asItems(orders), positions: asItems(positions), ledger: asItems(ledger), markEvidence: asItems(markEvidence) }); setError(null); } catch (reason) { setError(reason); } }, [campaignId]);
  useEffect(() => { void refresh(); }, [refresh]);
  if (!data) return <><button onClick={onBack}>Back to Campaign</button><ErrorNotice error={error} /><Empty title="Loading portfolio evidence" text="Loading local ledgers, retained reservations, PAPER Orders, Positions, and marks." /></>;
  const open = data.positions.filter((item) => text(item.settlement_state) === "OPEN");
  const settled = data.positions.filter((item) => text(item.settlement_state) === "SETTLED");
  return <><div className="section-head"><div><p className="eyebrow">PAPER PORTFOLIO / FINANCIAL FACTS</p><h1>Portfolio</h1><p className="stable-id">{campaignId}</p></div><button onClick={onBack}>Back to Campaign</button></div><PortfolioSummary portfolio={data.portfolio} /><div className="tabs">{(["Reservations", "Orders", "Open Positions", "Settled Positions", "Marks", "Ledger"] as PortfolioTab[]).map((item) => <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{item}</button>)}</div>{tab === "Reservations" && <ReservationsTable reservations={data.reservations} />}{tab === "Orders" && <OrdersTable orders={data.orders} />}{tab === "Open Positions" && <PositionsTable positions={open} markEvidence={data.markEvidence} campaignId={campaignId} refresh={refresh} setNotice={setNotice} />}{tab === "Settled Positions" && <PositionsTable positions={settled} markEvidence={[]} campaignId={campaignId} refresh={refresh} setNotice={setNotice} />}{tab === "Marks" && <MarksTable positions={data.positions} evidence={data.markEvidence} />}{tab === "Ledger" && <LedgerTable rows={data.ledger} portfolio={data.portfolio} />}<ErrorNotice error={error} /></>;
}

function ReservationsTable({ reservations }: { reservations: ApiRecord[] }) {
  if (!reservations.length) return <Empty title="No reservations" text="No authorization reservation has been retained for this Campaign." />;
  return <Panel title="Reservations"><div className="scroll"><table><thead><tr><th>Reservation</th><th>Source proposal</th><th>Authorized</th><th>Current</th><th>State</th><th>Created</th><th>Released</th><th>Order / Position</th></tr></thead><tbody>{reservations.map((item) => <tr key={text(item.reservation_id)}><td><small>{text(item.reservation_id)}</small></td><td><small>{text(item.proposal_id)}</small></td><td>{decimal(item.authorized_amount)}</td><td>{decimal(item.current_amount)}</td><td><span className={stateClass(item.state)}>{text(item.state)}</span></td><td>{formatTimestamp(item.created_at)}</td><td>{formatTimestamp(item.released_at)}</td><td><small>{text(item.order_id)}<br />{text(item.position_id)}</small></td></tr>)}</tbody></table></div></Panel>;
}

function OrdersTable({ orders }: { orders: ApiRecord[] }) {
  if (!orders.length) return <Empty title="No paper Orders" text="No retained PAPER Order exists. A rejected opportunity is deliberately not transformed into an Order." />;
  return <Panel title="Orders"><div className="scroll"><table><thead><tr><th>Order</th><th>Side</th><th>Requested</th><th>Filled / unfilled</th><th>Model</th><th>Modeled price</th><th>Fee</th><th>State</th><th>Reservation</th><th>Strategy / cycle</th></tr></thead><tbody>{orders.map((item) => <tr key={text(item.order_id)}><td><small>{text(item.order_id)}</small><span className="badge compact">PAPER</span></td><td>{text(item.side)}</td><td>{decimal(item.quantity)}</td><td>{decimal(item.paper_filled_quantity ?? item.filled_quantity)} / {decimal(item.paper_unfilled_quantity)}</td><td>{text(item.execution_policy_version)}</td><td>{decimal(item.modeled_execution_price)}</td><td>{decimal(item.paper_execution_fee)}</td><td><span className={stateClass(item.state)}>{text(item.state)}</span></td><td><small>{text(item.reservation_id)}<br />{decimal(item.reservation_current_amount)}</small></td><td>{text(item.strategy_id)}<small>{text(item.cycle_id)}</small></td></tr>)}</tbody></table></div></Panel>;
}

function PositionsTable({ positions, markEvidence, campaignId, refresh, setNotice }: { positions: ApiRecord[]; markEvidence: ApiRecord[]; campaignId: string; refresh: () => Promise<void>; setNotice: (value: string) => void }) {
  const [markId, setMarkId] = useState(markEvidence.length ? text(markEvidence[0].evidence_id) : "");
  const [pending, setPending] = useState<string>("");
  const [error, setError] = useState<unknown>(null);
  const mark = async () => { if (!markId) return; setPending("mark"); try { await mutate(`/api/v1/campaigns/${campaignId}/marks`, { orderbook_run_id: markId, operator_note: "operator-selected retained evidence" }); await refresh(); setNotice("Retained-evidence mark completed and authoritative portfolio values were refreshed."); } catch (reason) { setError(reason); } finally { setPending(""); } };
  const settle = async (position: ApiRecord, factId: string) => { setPending(text(position.position_id)); try { await mutate(`/api/v1/positions/${text(position.position_id)}/settle`, { settlement_fact_id: factId, operator_note: "operator-selected compatible retained SettlementFact" }); await refresh(); setNotice("Position settlement completed from the selected existing SettlementFact."); } catch (reason) { setError(reason); } finally { setPending(""); } };
  if (!positions.length) return <Empty title="No Positions" text="There are no retained Positions in this state. This is an explicit portfolio state, not a blank financial panel." />;
  return <Panel title={text(positions[0]?.settlement_state) === "OPEN" ? "Open Positions" : "Settled Positions"} action={markEvidence.length && text(positions[0]?.settlement_state) === "OPEN" ? <div className="inline-action"><select value={markId} onChange={(event) => setMarkId(event.currentTarget.value)}>{markEvidence.map((item) => <option key={text(item.evidence_id)} value={text(item.evidence_id)}>{text(item.evidence_id)} · {text(item.provider_market_ticker)}</option>)}</select><button className="primary" onClick={() => void mark()} disabled={pending === "mark"}>Mark with retained evidence</button></div> : undefined}><div className="scroll"><table><thead><tr><th>Position</th><th>Held side</th><th>Quantity</th><th>Consideration cost</th><th>Execution fee</th><th>Maximum remaining loss</th><th>Conservative mark</th><th>Unsupported quantity</th><th>Midpoint</th><th>State / lineage</th><th>Settlement</th></tr></thead><tbody>{positions.map((item) => { const latest = record(item.latest_mark); const settlement = record(item.settlement); const facts = records(item.available_settlement_facts); return <tr key={text(item.position_id)}><td><small>{text(item.position_id)}</small></td><td>{text(item.side)}</td><td>{decimal(item.open_quantity)}</td><td>{decimal(item.cost_basis)}</td><td>{decimal(record(item.lineage)?.execution_fee)}</td><td>{decimal(item.maximum_remaining_loss)}</td><td>{decimal(latest?.conservative_liquidation_value)}</td><td>{decimal(latest?.unsupported_quantity)}</td><td>{decimal(latest?.informational_midpoint_value)}</td><td><span className={stateClass(item.settlement_state)}>{text(item.settlement_state)}</span><small>{text(record(item.lineage)?.paper_execution_id)}</small></td><td>{settlement ? <span>{text(settlement.classification)} · {decimal(settlement.realized_contract_pnl)}</span> : facts.length ? <FactSettle facts={facts} pending={pending === text(item.position_id)} settle={(factId) => void settle(item, factId)} /> : <span className="muted">No compatible retained SettlementFact</span>}</td></tr>; })}</tbody></table></div><ErrorNotice error={error} /></Panel>;
}

function FactSettle({ facts, settle, pending }: { facts: ApiRecord[]; settle: (id: string) => void; pending: boolean }) {
  const [fact, setFact] = useState(text(facts[0]?.settlement_fact_id));
  return <div className="inline-action"><select value={fact} onChange={(event) => setFact(event.currentTarget.value)}>{facts.map((item) => <option key={text(item.settlement_fact_id)} value={text(item.settlement_fact_id)}>{text(item.settlement_fact_id)} · {text(item.classification)}</option>)}</select><button onClick={() => settle(fact)} disabled={pending}>Settle from fact</button></div>;
}

function MarksTable({ positions, evidence }: { positions: ApiRecord[]; evidence: ApiRecord[] }) {
  const marks = positions.map((position) => ({ position, mark: record(position.latest_mark) })).filter((item) => item.mark);
  return <Panel title="Retained conservative marks">{marks.length ? <div className="scroll"><table><thead><tr><th>Position</th><th>Observed</th><th>Supported</th><th>Unsupported</th><th>Liquidation value</th><th>Close fee</th><th>Slippage reserve</th><th>Midpoint</th><th>Reason codes</th></tr></thead><tbody>{marks.map(({ position, mark }) => <tr key={text(position.position_id)}><td><small>{text(position.position_id)}</small></td><td>{formatTimestamp(mark?.observed_at)}</td><td>{decimal(mark?.supported_quantity)}</td><td>{decimal(mark?.unsupported_quantity)}</td><td>{decimal(mark?.conservative_liquidation_value)}</td><td>{decimal(mark?.estimated_close_fee)}</td><td>{decimal(mark?.slippage_reserve)}</td><td>{decimal(mark?.informational_midpoint_value)}</td><td>{machineItems(mark?.reason_codes)}</td></tr>)}</tbody></table></div> : <Empty title="No retained marks" text={evidence.length ? "Retained evidence exists and can be selected from Open Positions; no mark has been posted yet." : "No retained evidence is currently valid for a Position mark."} />}</Panel>;
}

function LedgerTable({ rows, portfolio }: { rows: ApiRecord[]; portfolio: ApiRecord }) {
  if (!rows.length) return <Empty title="No ledger entries" text="The double-entry ledger has no posted records for this Campaign." />;
  return <Panel title="Posted ledger entries"><KeyValueGrid entries={[["Contributions", decimal(portfolio.contributions)], ["Cash", decimal(portfolio.settled_cash)], ["Position cost / ledger equity", decimal(portfolio.ledger_equity_at_cost)], ["Fees", decimal(portfolio.execution_fees)], ["Realized contract P&L", decimal(portfolio.realized_contract_pnl)], ["Net realized PAPER P&L", decimal(portfolio.net_realized_paper_pnl)]]} /><div className="scroll"><table><thead><tr><th>Timestamp</th><th>Journal type</th><th>Source identity</th><th>Account</th><th>Debit</th><th>Credit</th><th>State</th></tr></thead><tbody>{rows.map((item, index) => <tr key={`${text(item.journal_id)}-${index}`}><td>{formatTimestamp(item.occurred_at)}</td><td>{text(item.journal_type)}</td><td><small>{text(item.source_kind)} / {text(item.source_id)}</small></td><td>{text(item.account_code)}</td><td>{text(item.side) === "DEBIT" ? decimal(item.amount) : ""}</td><td>{text(item.side) === "CREDIT" ? decimal(item.amount) : ""}</td><td><span className={stateClass(item.status)}>{text(item.status)}</span></td></tr>)}</tbody></table></div></Panel>;
}

function Strategies({ campaignId, openStrategy }: { campaignId: string | null; openStrategy: (id: string) => void }) {
  const [strategies, setStrategies] = useState<ApiRecord[]>([]);
  const [scorecards, setScorecards] = useState<ApiRecord[]>([]);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { void api<{ items: ApiRecord[] }>("/api/v1/strategies").then((value) => setStrategies(asItems(value))).catch(setError); }, []);
  useEffect(() => { if (!campaignId) { setScorecards([]); return; } void api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/strategies/scorecards`).then((value) => setScorecards(asItems(value))).catch(setError); }, [campaignId]);
  if (!strategies.length && !error) return <Empty title="Loading governed strategy catalog" text="Loading immutable strategy governance; this never registers or promotes a strategy." />;
  return <><div className="section-head"><div><p className="eyebrow">GOVERNED STRATEGY CATALOG</p><h1>Strategies</h1></div></div><ErrorNotice error={error} />{!campaignId && <div className="notice neutral">Choose a Campaign command center to see its local scorecards. The catalog itself never grants strategy authority.</div>}<Panel title="Strategy governance"><div className="scroll"><table><thead><tr><th>Strategy / version</th><th>Family</th><th>Authority</th><th>Implementation</th><th>Evidence</th><th>Runnable</th><th>Invocations / failures</th><th>Proposals</th><th>Approvals / rejections</th><th>Paper Orders</th><th>Positions</th><th>Result / active risk</th><th>Health</th></tr></thead><tbody>{strategies.map((strategy) => { const card = scorecards.find((item) => text(item.strategy_id) === text(strategy.strategy_id) && text(item.strategy_version) === text(strategy.version)); return <tr className="clickable" key={text(strategy.strategy_id)} onClick={() => openStrategy(text(strategy.strategy_id))}><td><strong>{text(strategy.strategy_id)}</strong><small>{text(strategy.version)}</small></td><td>{text(strategy.family)}</td><td><span className={stateClass(strategy.authority)}>{text(strategy.authority)}</span></td><td>{text(strategy.implementation_state)}</td><td>{text(strategy.evidence_state)}<small>{text(strategy.last_evidence_decision)}</small></td><td>{text(strategy.runnable)}</td><td>{text(card?.invocation_count, "0")} / {text(card?.failures, "0")}</td><td>{text(card?.proposals, "0")}</td><td>{text(card?.approvals, "0")} / {text(card?.rejections, "0")}</td><td>{text(card?.paper_orders, "0")}</td><td>{text(card?.open_positions, "0")} / {text(card?.settled_positions, "0")}</td><td>{decimal(card?.net_realized_paper_pnl)} / {decimal(card?.maximum_active_risk)}</td><td>{machineItems(card?.health_reason_codes)}</td></tr>; })}</tbody></table></div></Panel></>;
}

function MarketDynamicsStrategyDetail({ strategy, campaignId, onBack }: { strategy: ApiRecord; campaignId: string | null; onBack: () => void }) {
  const [features, setFeatures] = useState<ApiRecord[]>([]);
  const [comparisons, setComparisons] = useState<ApiRecord[]>([]);
  const [iterations, setIterations] = useState("3");
  const [interval, setInterval] = useState("30");
  const [marketLimit, setMarketLimit] = useState("20");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const load = async () => {
    const query = campaignId ? `?campaign_id=${encodeURIComponent(campaignId)}` : "";
    const [featureValue, comparisonValue] = await Promise.all([
      api<{ items: ApiRecord[] }>(`/api/v1/strategies/kalshi-market-dynamics-v1/market-dynamics-features${query}`),
      api<{ items: ApiRecord[] }>(`/api/v1/strategy-comparisons${query}`),
    ]);
    setFeatures(asItems(featureValue)); setComparisons(asItems(comparisonValue));
  };
  useEffect(() => { void load().catch(setError); }, [campaignId]);
  const run = async () => {
    if (!campaignId) { setError(new ApiError("Choose a RUNNING Campaign before starting a bounded foreground session.", "CAMPAIGN_REQUIRED")); return; }
    setSubmitting(true); setError(null);
    try {
      await mutate<ApiRecord>(`/api/v1/campaigns/${campaignId}/strategies/kalshi-market-dynamics-v1/shadow-observations`, {
        iterations: Number(iterations), interval_seconds: Number(interval), market_limit: Number(marketLimit), maximum_pages: 1, maximum_enrichments: Number(marketLimit), observations_per_market: 3, orderbook_depth: 1,
      });
      await load();
    } catch (failure) { setError(failure); } finally { setSubmitting(false); }
  };
  return <><div className="section-head"><div><p className="eyebrow">STRATEGY GOVERNANCE / FORWARD SHADOWING ONLY</p><h1>Kalshi Market Dynamics</h1></div><button onClick={onBack}>Back to Strategies</button></div><ErrorNotice error={error} /><Panel title="Authority and evidence"><KeyValueGrid entries={[["Version", text(strategy.version)], ["Family", text(strategy.family)], ["Authority", text(strategy.authority)], ["Evidence state", text(strategy.evidence_state)], ["Configuration", text(strategy.configuration_hash)], ["PAPER promotion", "Disabled"]]} /><p className="disabled-explanation">This strategy derives only retained temporal YES midpoint and spread history. It has no orderbook-pressure inputs, provider submission, or financial authority.</p></Panel><Panel title="Market dynamics features" action={<span className="badge">PUBLIC DATA / SHADOW ONLY</span>}><p className="muted">Window and recent momentum plus signed directional persistence are fixed, exact Decimal features. Widening latest spread rejects evidence; it is not treated as directional alpha.</p><div className="form-grid"><label>Iterations<input value={iterations} onChange={(event) => setIterations(event.currentTarget.value)} disabled={!campaignId || submitting} /></label><label>Interval seconds<input value={interval} onChange={(event) => setInterval(event.currentTarget.value)} disabled={!campaignId || submitting} /></label><label>Market limit<input value={marketLimit} onChange={(event) => setMarketLimit(event.currentTarget.value)} disabled={!campaignId || submitting} /></label></div><button className="primary" disabled={!campaignId || submitting} onClick={() => void run()}>{submitting ? "Running foreground session…" : "Run Market Dynamics Shadow Session"}</button>{!campaignId && <p className="disabled-explanation">Choose a RUNNING Campaign first. This never submits an order or allocates capital.</p>}{features.length ? <div className="scroll"><table><thead><tr><th>Market</th><th>State / reasons</th><th>Cadence</th><th>Momentum / persistence</th><th>Adjustment</th><th>Prediction / proposal</th></tr></thead><tbody>{features.map((item) => <tr key={text(item.feature_snapshot_id)}><td>{text(item.provider_market_ticker)}<small>{text(item.provider_event_ticker)} / {text(item.series_ticker)}</small></td><td>{text(item.feature_state)}<small>{machineItems(item.reason_codes)}</small></td><td>{text(item.observation_count)}<small>{text(item.history_duration_seconds)} seconds</small></td><td>{decimal(item.window_momentum)} / {decimal(item.recent_momentum)} / {decimal(item.signed_directional_persistence)}</td><td>{decimal(item.raw_adjustment)} / {decimal(item.clamped_adjustment)}</td><td>{text(item.prediction_id, "non-prediction evidence")}<small>{text(item.proposal_id, "NO_PROPOSAL")}</small></td></tr>)}</tbody></table></div> : <Empty title="No retained dynamics features" text="Complete and rejected temporal evidence appears here after a bounded foreground session." />}</Panel><Panel title="Read-only cross-strategy comparison"><p className="muted">Apex compares compatible market-consensus, microstructure, and dynamics predictions without combining or averaging them.</p>{comparisons.length ? <div className="scroll"><table><thead><tr><th>Market</th><th>Consensus YES</th><th>Microstructure conservative YES</th><th>Dynamics conservative YES</th><th>Agreement / difference</th></tr></thead><tbody>{comparisons.map((item) => <tr key={`${text(item.microstructure_prediction_id)}-${text(item.market_dynamics_prediction_id)}`}><td>{text(item.instrument_id)}<small>{text(item.event_id)}</small></td><td>{decimal(item.market_consensus_yes_probability)}</td><td>{decimal(item.microstructure_conservative_yes_probability)}</td><td>{decimal(item.market_dynamics_conservative_yes_probability)}</td><td>{text(item.directional_agreement)} / {decimal(item.disagreement_magnitude)}</td></tr>)}</tbody></table></div> : <Empty title="No compatible comparison" text="Rows appear only after both independent strategies retain compatible predictions for the same exact market." />}</Panel></>;
}

function StrategyDetail({ strategyId, campaignId, onBack }: { strategyId: string; campaignId: string | null; onBack: () => void }) {
  const [strategy, setStrategy] = useState<ApiRecord | null>(null);
  const [scorecards, setScorecards] = useState<ApiRecord[]>([]);
  const [shadowCard, setShadowCard] = useState<ApiRecord | null>(null);
  const [readiness, setReadiness] = useState<ApiRecord | null>(null);
  const [history, setHistory] = useState<ApiRecord[]>([]);
  const [microstructureFeatures, setMicrostructureFeatures] = useState<ApiRecord[]>([]);
  const [marketDynamicsFeatures, setMarketDynamicsFeatures] = useState<ApiRecord[]>([]);
  const [strategyComparisons, setStrategyComparisons] = useState<ApiRecord[]>([]);
  const [liveIterations, setLiveIterations] = useState("3");
  const [liveInterval, setLiveInterval] = useState("30");
  const [liveMarketLimit, setLiveMarketLimit] = useState("20");
  const [liveDepth, setLiveDepth] = useState("5");
  const [reason, setReason] = useState("");
  const [reviewId, setReviewId] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { void api<ApiRecord>(`/api/v1/strategies/${strategyId}`).then(setStrategy).catch(setError); }, [strategyId]);
  useEffect(() => {
    const query = campaignId ? `?campaign_id=${encodeURIComponent(campaignId)}` : "";
    void Promise.all([
      api<ApiRecord>(`/api/v1/strategies/${strategyId}/scorecard${query}`),
      api<ApiRecord>(`/api/v1/strategies/${strategyId}/promotion-readiness${query}`),
      api<{ items: ApiRecord[] }>(`/api/v1/strategies/${strategyId}/authority-history`),
      api<{ items: ApiRecord[] }>(`/api/v1/strategies/${strategyId}/microstructure-features${query}`),
      api<{ items: ApiRecord[] }>(`/api/v1/strategies/${strategyId}/market-dynamics-features${query}`),
      api<{ items: ApiRecord[] }>(`/api/v1/strategy-comparisons${query}`),
    ]).then(([scorecard, readinessValue, historyValue, featureValue, dynamicsValue, comparisonValue]) => {
      setScorecards(Array.isArray(scorecard.scorecards) ? scorecard.scorecards as ApiRecord[] : []);
      setShadowCard(record(scorecard.shadow_scorecard));
      setReadiness(readinessValue);
      setHistory(asItems(historyValue));
      setMicrostructureFeatures(asItems(featureValue));
      setMarketDynamicsFeatures(asItems(dynamicsValue));
      setStrategyComparisons(asItems(comparisonValue));
    }).catch(setError);
  }, [campaignId, strategyId]);
  if (!strategy) return <><button onClick={onBack}>Back to Strategies</button><ErrorNotice error={error} /><Empty title="Loading Strategy detail" text="Loading local governance and retained scorecards." /></>;
  if (strategyId === "kalshi-market-dynamics-v1") return <MarketDynamicsStrategyDetail strategy={strategy} campaignId={campaignId} onBack={onBack} />;
  const card = scorecards[0];
  const status = text(strategy.authority);
  const canPromoteShadow = status === "RESEARCH" && text(strategy.evidence_state) !== "INCOMPLETE";
  const promote = async () => {
    if (!reason.trim() || !reviewId.trim() || confirmation !== "PROMOTE TO SHADOW") { setError(new ApiError("Evidence review, reason, and exact confirmation are required.", "PROMOTION_CONFIRMATION_REQUIRED")); return; }
    setSubmitting(true); setError(null);
    try {
      await mutate<ApiRecord>(`/api/v1/strategies/${strategyId}/authority`, { requested_authority: "SHADOW", operator_reason: reason, evidence_review_id: reviewId, confirmation_text: confirmation });
      setStrategy(await api<ApiRecord>(`/api/v1/strategies/${strategyId}`));
      setReason(""); setReviewId(""); setConfirmation("");
    } catch (failure) { setError(failure); } finally { setSubmitting(false); }
  };
  const runMicrostructureSession = async () => {
    if (!campaignId) { setError(new ApiError("Choose a RUNNING Campaign before starting a bounded foreground session.", "CAMPAIGN_REQUIRED")); return; }
    setSubmitting(true); setError(null);
    try {
      await mutate<ApiRecord>(`/api/v1/campaigns/${campaignId}/strategies/kalshi-orderbook-microstructure-v1/shadow-observations`, {
        iterations: Number(liveIterations), interval_seconds: Number(liveInterval), market_limit: Number(liveMarketLimit), orderbook_depth: Number(liveDepth), observations_per_market: 3, maximum_pages: 1, maximum_enrichments: Number(liveMarketLimit),
      });
      const query = `?campaign_id=${encodeURIComponent(campaignId)}`;
      setMicrostructureFeatures(asItems(await api<{ items: ApiRecord[] }>(`/api/v1/strategies/${strategyId}/microstructure-features${query}`)));
    } catch (failure) { setError(failure); } finally { setSubmitting(false); }
  };
  const runMarketDynamicsSession = async () => {
    if (!campaignId) { setError(new ApiError("Choose a RUNNING Campaign before starting a bounded foreground session.", "CAMPAIGN_REQUIRED")); return; }
    setSubmitting(true); setError(null);
    try {
      await mutate<ApiRecord>(`/api/v1/campaigns/${campaignId}/strategies/kalshi-market-dynamics-v1/shadow-observations`, {
        iterations: Number(liveIterations), interval_seconds: Number(liveInterval), market_limit: Number(liveMarketLimit), orderbook_depth: Number(liveDepth), observations_per_market: 3, maximum_pages: 1, maximum_enrichments: Number(liveMarketLimit),
      });
      const query = `?campaign_id=${encodeURIComponent(campaignId)}`;
      setMarketDynamicsFeatures(asItems(await api<{ items: ApiRecord[] }>(`/api/v1/strategies/${strategyId}/market-dynamics-features${query}`)));
      setStrategyComparisons(asItems(await api<{ items: ApiRecord[] }>(`/api/v1/strategy-comparisons${query}`)));
    } catch (failure) { setError(failure); } finally { setSubmitting(false); }
  };
  return <><div className="section-head"><div><p className="eyebrow">STRATEGY GOVERNANCE / FORWARD SHADOWING ONLY</p><h1>{strategyId}</h1></div><button onClick={onBack}>Back to Strategies</button></div><ErrorNotice error={error} /><Panel title="Authority and evidence"><KeyValueGrid entries={[["Version", text(strategy.version)], ["Family", text(strategy.family)], ["Authority", <span className={stateClass(status)}>{status}</span>], ["Implementation state", text(strategy.implementation_state)], ["Evidence state", text(strategy.evidence_state)], ["Evidence decision", text(strategy.last_evidence_decision)], ["Runnable", text(strategy.runnable)], ["Why it can or cannot run", text(strategy.runnable) === "true" ? "Only inside an explicit bounded SHADOW evaluation. This creates no reservation, Order, Position, or ledger state." : "Governance and retained evidence do not authorize this strategy to run."]]} /></Panel><Panel title="Promotion readiness"><KeyValueGrid entries={[["Resolved predictions", text(shadowCard?.resolved_predictions, "0")], ["Distinct events", text(shadowCard?.distinct_events, "0")], ["Brier score", decimal(shadowCard?.brier_score)], ["Log loss", decimal(shadowCard?.log_loss)], ["Market baseline Brier", decimal(shadowCard?.market_baseline_brier_score)], ["Market baseline log loss", decimal(shadowCard?.market_baseline_log_loss)], ["Probability coverage", decimal(shadowCard?.probability_coverage)], ["Integrity warnings", machineItems(shadowCard?.integrity_warnings)], ["Explicit promotion reasons", machineItems(readiness?.reasons)]]} /><p className="disabled-explanation">PAPER promotion is an intentionally disabled future gate. No automatic strategy or PAPER promotion exists.</p></Panel>{strategyId === "kalshi-orderbook-microstructure-v1" && <Panel title="Orderbook microstructure evidence" action={<span className="badge">PUBLIC DATA / SHADOW ONLY</span>}><p className="muted">Fixed retained features: midpoint, spread, visible depth, imbalance, microprice displacement, and short-window momentum. A bounded foreground session is public-data-only, sequential, SHADOW-only, and creates no order or capital state.</p><div className="form-grid"><label>Iterations<input value={liveIterations} onChange={(event) => setLiveIterations(event.currentTarget.value)} disabled={!campaignId || submitting} /></label><label>Interval seconds<input value={liveInterval} onChange={(event) => setLiveInterval(event.currentTarget.value)} disabled={!campaignId || submitting} /></label><label>Market limit<input value={liveMarketLimit} onChange={(event) => setLiveMarketLimit(event.currentTarget.value)} disabled={!campaignId || submitting} /></label><label>Orderbook depth<input value={liveDepth} onChange={(event) => setLiveDepth(event.currentTarget.value)} disabled={!campaignId || submitting} /></label></div><button className="primary" disabled={!campaignId || submitting} onClick={() => void runMicrostructureSession()}>{submitting ? "Running foreground session…" : "Run Live Shadow Session"}</button>{!campaignId && <p className="disabled-explanation">Choose a RUNNING Campaign first. This never submits an order or allocates capital.</p>}{microstructureFeatures.length ? <div className="scroll"><table><thead><tr><th>Market</th><th>Feature state / reasons</th><th>Midpoint / spread</th><th>Depth / imbalance</th><th>Point lineage</th><th>Proposal</th></tr></thead><tbody>{microstructureFeatures.map((item) => <tr key={text(item.feature_snapshot_id)}><td>{text(item.provider_market_ticker)}<small>{text(item.provider_event_ticker)}</small></td><td>{text(item.feature_state)}<small>{machineItems(item.reason_codes)}</small></td><td>{decimal(item.midpoint)} / {decimal(item.spread)}</td><td>{decimal(item.visible_bid_quantity)} / {decimal(item.visible_ask_quantity)} / {decimal(item.depth_imbalance)}</td><td>{text(item.prediction_id, "non-prediction evidence")}</td><td>{text(item.proposal_id, "NO_PROPOSAL")}</td></tr>)}</tbody></table></div> : <Empty title="No retained microstructure features" text="A bounded foreground session retains complete, rejected, and non-prediction evidence here." />}</Panel>}<Panel title="Authority history">{history.length ? <ActivityTable activity={history.map((item) => ({ ...item, timestamp: item.completed_at, actor: "OPERATOR", action: "STRATEGY_AUTHORITY", campaign_id: "—", target_id: item.decision_id, result_state: item.state }))} /> : <Empty title="No authority changes" text="The static catalog authority is currently in force." />}</Panel>{status === "RESEARCH" && <Panel title="Promote to Shadow" action={<span className="badge">LOCAL ONLY</span>}><p className="muted">This transition requires retained evidence review and remains non-financial. It does not enable PAPER or LIVE execution.</p><label>Evidence review ID<input value={reviewId} onChange={(event) => setReviewId(event.currentTarget.value)} disabled={!canPromoteShadow} /></label><label>Operator reason<textarea value={reason} onChange={(event) => setReason(event.currentTarget.value)} disabled={!canPromoteShadow} /></label><label>Type PROMOTE TO SHADOW<input value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} disabled={!canPromoteShadow} /></label><button className="primary" disabled={!canPromoteShadow || submitting} onClick={() => void promote()}>{submitting ? "Recording…" : "Promote to Shadow"}</button>{!canPromoteShadow && <p className="disabled-explanation">This research strategy has not met its retained evidence-review requirements.</p>}</Panel>}<Panel title="Campaign scorecard">{card ? <KeyValueGrid entries={[["Invocations / failures", `${text(card.invocation_count)} / ${text(card.failures)}`], ["Proposals", text(card.proposals)], ["Approvals / rejections", `${text(card.approvals)} / ${text(card.rejections)}`], ["PAPER Orders", text(card.paper_orders)], ["Open Positions", text(card.open_positions)], ["Settled Positions", text(card.settled_positions)], ["Fees", decimal(card.fees)], ["Active risk", decimal(card.maximum_active_risk)], ["Health reasons", machineItems(card.health_reason_codes)]]} /> : <Empty title="No PAPER scorecard" text="Paper evidence is separate from forward SHADOW prediction scoring." />}</Panel><div className="notice neutral">Weather remains RESEARCH and non-runnable. Same-market complement and threshold-ladder strategies remain rejected. There is no Trade button, LIVE mode, credential input, or provider submission path.</div></>;
}

function Activity({ campaigns }: { campaigns: Campaign[] }) {
  const [items, setItems] = useState<ApiRecord[]>([]);
  const [filters, setFilters] = useState({ campaign: "", action: "", entity: "", start: "", end: "" });
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { void api<{ items: ApiRecord[] }>("/api/v1/activity").then((value) => setItems(asItems(value))).catch(setError); }, []);
  const filtered = items.filter((item) => (!filters.campaign || text(item.campaign_id) === filters.campaign) && (!filters.action || text(item.action).includes(filters.action)) && (!filters.entity || text(item.entity_type) === filters.entity) && (!filters.start || new Date(text(item.timestamp)).valueOf() >= new Date(filters.start).valueOf()) && (!filters.end || new Date(text(item.timestamp)).valueOf() <= new Date(filters.end).valueOf()));
  return <><div className="section-head"><div><p className="eyebrow">IMMUTABLE LOCAL ACTIVITY</p><h1>Activity</h1></div></div><ErrorNotice error={error} /><Panel title="Bounded activity filters"><div className="filters"><label>Campaign<select value={filters.campaign} onChange={(event) => setFilters((current) => ({ ...current, campaign: event.currentTarget.value }))}><option value="">All Campaigns</option>{campaigns.map((campaign) => <option key={campaign.campaign_id} value={campaign.campaign_id}>{campaign.name}</option>)}</select></label><label>Action type<input value={filters.action} onChange={(event) => setFilters((current) => ({ ...current, action: event.currentTarget.value }))} placeholder="e.g. CYCLE" /></label><label>Entity type<select value={filters.entity} onChange={(event) => setFilters((current) => ({ ...current, entity: event.currentTarget.value }))}><option value="">All entities</option>{["OPERATOR_ACTION", "CYCLE", "OPPORTUNITY", "ORDER", "MARK", "SETTLEMENT"].map((item) => <option key={item}>{item}</option>)}</select></label><label>From<input type="datetime-local" value={filters.start} onChange={(event) => setFilters((current) => ({ ...current, start: event.currentTarget.value }))} /></label><label>To<input type="datetime-local" value={filters.end} onChange={(event) => setFilters((current) => ({ ...current, end: event.currentTarget.value }))} /></label></div><ActivityTable activity={filtered} /></Panel></>;
}

function ShadowOperations({ campaigns }: { campaigns: Campaign[] }) {
  const [campaignId, setCampaignId] = useState("");
  const [plans, setPlans] = useState<ApiRecord[]>([]);
  const [selected, setSelected] = useState<ApiRecord | null>(null);
  const [cohort, setCohort] = useState<ApiRecord[]>([]);
  const [iterations, setIterations] = useState<ApiRecord[]>([]);
  const [predictions, setPredictions] = useState<ApiRecord[]>([]);
  const [performance, setPerformance] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [values, setValues] = useState({ name: "Forward Shadow Evidence", iterations: "6", interval_seconds: "30", market_count: "6", maximum_pages: "3", maximum_enrichments: "20", orderbook_depth: "5" });
  const loadPlans = async (id = campaignId) => {
    if (!id) return;
    try { setPlans(asItems(await api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${id}/shadow-plans`))); setError(null); } catch (failure) { setError(failure); }
  };
  const inspect = async (planId: string) => {
    try {
      const [plan, members, iterationRows, predictionRows, report] = await Promise.all([
        api<ApiRecord>(`/api/v1/shadow-plans/${planId}`),
        api<{ items: ApiRecord[] }>(`/api/v1/shadow-plans/${planId}/cohort`),
        api<{ items: ApiRecord[] }>(`/api/v1/shadow-plans/${planId}/iterations`),
        api<{ items: ApiRecord[] }>(`/api/v1/shadow-plans/${planId}/predictions`),
        api<ApiRecord>(`/api/v1/campaigns/${campaignId}/strategy-performance?plan_id=${encodeURIComponent(planId)}`),
      ]);
      setSelected(plan); setCohort(asItems(members)); setIterations(asItems(iterationRows)); setPredictions(asItems(predictionRows)); setPerformance(record(report.metrics)); setError(null);
    } catch (failure) { setError(failure); }
  };
  const create = async () => {
    if (!campaignId) { setError(new ApiError("Choose a Campaign before creating a finite evidence plan.", "CAMPAIGN_REQUIRED")); return; }
    setPending(true); setError(null);
    try {
      const result = await mutate<ApiRecord>(`/api/v1/campaigns/${campaignId}/shadow-plans`, { name: values.name, iterations: Number(values.iterations), interval_seconds: Number(values.interval_seconds), market_count: Number(values.market_count), maximum_pages: Number(values.maximum_pages), maximum_enrichments: Number(values.maximum_enrichments), orderbook_depth: Number(values.orderbook_depth) });
      await loadPlans();
      const plan = record(result.plan);
      if (plan) await inspect(text(plan.plan_id));
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  const control = async (action: "pause" | "resume" | "cancel" | "run-due") => {
    if (!selected) return;
    setPending(true); setError(null);
    try {
      const id = text(selected.plan_id);
      await mutate<ApiRecord>(`/api/v1/shadow-plans/${id}/${action}`, action === "run-due" ? {} : { operator_reason: `Operator requested ${action} for finite local evidence plan.` });
      await loadPlans(); await inspect(id);
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  return <><div className="section-head"><div><p className="eyebrow">FINITE / FROZEN / FORWARD EVIDENCE ONLY</p><h1>Shadow Operations</h1><p className="muted">Plans use public GET-only evidence, a local worker lease, and non-financial forward scoring. They cannot submit orders, reserve capital, settle Positions, or post journals.</p></div></div><ErrorNotice error={error} /><Panel title="Create finite evidence plan" action={<span className="badge">NO EXECUTION AUTHORITY</span>}><div className="form-grid"><label>Campaign<select value={campaignId} onChange={(event) => { setCampaignId(event.currentTarget.value); void loadPlans(event.currentTarget.value); }}><option value="">Choose Campaign</option>{campaigns.map((campaign) => <option key={campaign.campaign_id} value={campaign.campaign_id}>{campaign.name}</option>)}</select></label><label>Name<input value={values.name} onChange={(event) => setValues((current) => ({ ...current, name: event.currentTarget.value }))} /></label><label>Iterations<input value={values.iterations} onChange={(event) => setValues((current) => ({ ...current, iterations: event.currentTarget.value }))} /></label><label>Interval seconds<input value={values.interval_seconds} onChange={(event) => setValues((current) => ({ ...current, interval_seconds: event.currentTarget.value }))} /></label><label>Frozen market count<input value={values.market_count} onChange={(event) => setValues((current) => ({ ...current, market_count: event.currentTarget.value }))} /></label><label>Pages / enrichments / depth<input value={`${values.maximum_pages} / ${values.maximum_enrichments} / ${values.orderbook_depth}`} readOnly /></label></div><button className="primary" disabled={pending || !campaignId} onClick={() => void create()}>{pending ? "Recording…" : "Create finite Shadow Plan"}</button></Panel><Panel title="Shadow evidence plans"><div className="scroll"><table><thead><tr><th>Name / state</th><th>Configured</th><th>Completed / partial / failed</th><th>Next due</th><th /></tr></thead><tbody>{plans.map((plan) => <tr key={text(plan.plan_id)}><td><strong>{text(plan.name)}</strong><small>{text(plan.plan_id)}</small><span className={stateClass(plan.state)}>{text(plan.state)}</span></td><td>{text(plan.iterations_requested)} iterations · {text(plan.market_count)} frozen markets</td><td>{text(plan.completed_iterations)} / {text(plan.partial_iterations)} / {text(plan.failed_iterations)}</td><td>{formatTimestamp(plan.next_due_at)}</td><td><button onClick={() => void inspect(text(plan.plan_id))}>Inspect</button></td></tr>)}</tbody></table></div>{!plans.length && <Empty title="No finite plans" text="Select a Campaign and create a bounded forward-evidence plan. Its cohort becomes immutable after first successful enrichment." />}</Panel>{selected && <div className="stack"><Panel title={`Plan detail · ${text(selected.name)}`} action={<span className={stateClass(selected.state)}>{text(selected.state)}</span>}><KeyValueGrid entries={[["Policy", text(selected.policy_identity)], ["Configuration hash", text(selected.canonical_request_hash)], ["Frozen cohort", `${cohort.length} exact members`], ["Last stage", text(selected.last_completed_stage)], ["Last error", text(selected.last_error_code)]]} /><div className="actions"><button disabled={pending || text(selected.state) !== "RUNNING"} onClick={() => void control("pause")}>Pause</button><button disabled={pending || text(selected.state) !== "PAUSED"} onClick={() => void control("resume")}>Resume</button><button className="primary" disabled={pending || !["CREATED", "RUNNING"].includes(text(selected.state))} onClick={() => void control("run-due")}>Run one due iteration</button><button className="danger" disabled={pending || ["COMPLETED", "PARTIAL", "FAILED", "CANCELED", "INTERRUPTED"].includes(text(selected.state))} onClick={() => void control("cancel")}>Cancel plan</button></div></Panel><Panel title="Frozen cohort"><div className="scroll"><table><thead><tr><th>Market / event / series</th><th>Rule / lineage</th><th>Observed / closes</th><th>State</th></tr></thead><tbody>{cohort.map((member) => <tr key={text(member.member_id)}><td>{text(member.market_ticker)}<small>{text(member.event_ticker)} / {text(member.series_ticker)}</small></td><td><small>{text(member.rule_content_hash)}</small></td><td>{formatTimestamp(member.observed_at)}<small>{formatTimestamp(member.close_at)}</small></td><td>{text(member.state)}</td></tr>)}</tbody></table></div></Panel><Panel title="Iteration history"><div className="scroll"><table><thead><tr><th>Ordinal / state</th><th>Capture</th><th>Predictions / comparisons</th><th>Hypothetical allocation</th><th>Reasons</th></tr></thead><tbody>{iterations.map((item) => <tr key={text(item.iteration_id)}><td>{text(item.ordinal)} · {text(item.state)}<small>{formatTimestamp(item.completed_at)}</small></td><td>{text(item.market_refresh_count)} refresh / {text(item.orderbook_success_count)} successful / {text(item.orderbook_failure_count)} failed</td><td>{text(item.prediction_count)} / {text(item.comparison_count)}</td><td>{text(item.hypothetical_admission_count)} admitted / {text(item.hypothetical_rejection_count)} rejected</td><td>{machineItems(item.reason_codes)}</td></tr>)}</tbody></table></div></Panel><Panel title="Forward performance"><KeyValueGrid entries={[["Resolved / unresolved", `${text(performance?.resolved_prediction_count, "0")} / ${text(performance?.unresolved_prediction_count, "0")}`], ["Point Brier / log loss", `${text(performance?.point_brier_score)} / ${text(performance?.point_log_loss)}`], ["Conservative Brier / log loss", `${text(performance?.conservative_brier_score)} / ${text(performance?.conservative_log_loss)}`], ["Consensus Brier / log loss", `${text(performance?.consensus_brier_score)} / ${text(performance?.consensus_log_loss)}`], ["Promotion", text(performance?.readiness_or_promotion)]]} /><p className="disabled-explanation">Public outcomes are scoring facts, never financial settlement. Insufficient outcomes remain visible and nonfatal; no opaque performance score or automatic promotion exists.</p></Panel><Panel title="Predictions"><div className="scroll"><table><thead><tr><th>Strategy</th><th>Market / event</th><th>Decision / resolution</th><th>State</th></tr></thead><tbody>{predictions.map((prediction) => <tr key={text(prediction.prediction_id)}><td>{text(prediction.strategy_id)}</td><td>{text(prediction.instrument_id)}<small>{text(prediction.event_id)}</small></td><td>{formatTimestamp(prediction.decision_at)}<small>{formatTimestamp(prediction.resolution_at)}</small></td><td>{text(prediction.state)}</td></tr>)}</tbody></table></div></Panel></div>}</>;
}

function OverviewView({ overview, campaigns, openCampaign }: { overview: Overview | null; campaigns: Campaign[]; openCampaign: (id: string) => void }) {
  if (!overview) return <Empty title="Loading Overview" text="Loading local Campaign and financial summaries from the database." />;
  const activity = records(overview.recent_activity);
  return <><div className="section-head"><div><p className="eyebrow">LOCAL CAPITAL OPERATIONS</p><h1>Overview</h1></div></div><div className="metrics"><Metric label="Campaigns by state" value={Object.entries(record(overview.campaigns_by_state) ?? {}).map(([key, value]) => `${key}: ${text(value)}`).join(" · ") || "0"} /><Metric label="SHADOW / PAPER" value={`${text(record(overview.campaigns_by_mode)?.SHADOW, "0")} / ${text(record(overview.campaigns_by_mode)?.PAPER, "0")}`} /><Metric label="Funded capital" value={decimal(overview.funded_capital)} /><Metric label="Authorized loss" value={decimal(overview.authorized_loss)} /><Metric label="Remaining loss capacity" value={decimal(overview.remaining_loss_capacity)} /><Metric label="Active risk" value={decimal(overview.active_risk)} /><Metric label="Realized PAPER result" value={decimal(overview.realized_paper_result)} /><Metric label="Conservative marked result" value={decimal(overview.conservative_marked_result)} /><Metric label="Open Positions" value={text(overview.open_positions)} /></div><Panel title="Campaigns"><CampaignTable campaigns={campaigns} openCampaign={openCampaign} /></Panel><Panel title="Opportunities needing attention"><p className="muted">Open a Campaign command center to inspect every ranked, rejected, and approved opportunity with its exact decision reasons.</p></Panel><Panel title="Strategy health warnings"><p className="muted">Research and deprioritized strategies remain visible but do not grant runnable or promotion authority.</p></Panel><Panel title="Recent operator and system activity"><ActivityTable activity={activity} compact /></Panel></>;
}

function EvidenceReviews({ campaignId }: { campaignId: string | null }) {
  const [policies, setPolicies] = useState<ApiRecord[]>([]);
  const [reviews, setReviews] = useState<ApiRecord[]>([]);
  const [snapshots, setSnapshots] = useState<ApiRecord[]>([]);
  const [trials, setTrials] = useState<ApiRecord[]>([]);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    setError(null);
    try {
      const strategyIds = ["kalshi-orderbook-microstructure-v1", "kalshi-market-dynamics-v1"];
      const [policyValue, ...strategyValues] = await Promise.all([
        api<{ items: ApiRecord[] }>("/api/v1/strategy-review-policies"),
        ...strategyIds.flatMap((id) => [
          api<{ items: ApiRecord[] }>(`/api/v1/strategies/${id}/reviews`),
          api<{ items: ApiRecord[] }>(`/api/v1/strategies/${id}/evidence-snapshots`),
        ]),
      ]);
      setPolicies(asItems(policyValue));
      setReviews(strategyValues.filter((_, index) => index % 2 === 0).flatMap((value) => asItems(value)));
      setSnapshots(strategyValues.filter((_, index) => index % 2 === 1).flatMap((value) => asItems(value)));
      setTrials(campaignId ? asItems(await api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/paper-trials`)) : []);
    } catch (failure) { setError(failure); }
  }, [campaignId]);
  useEffect(() => { void refresh(); }, [refresh]);
  return <><div className="section-head"><div><p className="eyebrow">FORWARD SHADOW EVIDENCE — NOT FINANCIAL PERFORMANCE</p><h1>Evidence Reviews</h1><p className="muted">PAPER requires every frozen hard gate to pass and an active campaign-specific trial. There is no LIVE promotion control.</p></div><button onClick={() => void refresh()}>Refresh</button></div><ErrorNotice error={error} /><Panel title="Versioned review policies"><div className="scroll"><table><thead><tr><th>Policy</th><th>Strategy scope</th><th>Resolved / events / markets</th><th>Current</th></tr></thead><tbody>{policies.map((policy) => <tr key={`${text(policy.policy_id)}-${text(policy.policy_version)}`}><td><strong>{text(policy.policy_id)}</strong><small>{text(policy.policy_version)}</small></td><td>{text(policy.supported_strategy_family, text(policy.supported_strategy_id))}</td><td>{text(policy.minimum_resolved_predictions)} / {text(policy.minimum_distinct_events)} / {text(policy.minimum_distinct_markets)}</td><td>{text(policy.superseded_at, "CURRENT")}</td></tr>)}</tbody></table></div></Panel><Panel title="Review queue"><div className="scroll"><table><thead><tr><th>Strategy</th><th>State</th><th>Requested campaign</th><th>Snapshot</th><th>Reason</th></tr></thead><tbody>{reviews.map((review) => <tr key={text(review.review_id)}><td>{text(review.strategy_id)}<small>{text(review.strategy_version)}</small></td><td><span className={stateClass(review.state)}>{text(review.state)}</span></td><td>{text(review.requested_campaign_id)}</td><td><small>{text(review.snapshot_id)}</small></td><td>{machineItems(review.reason_codes)}</td></tr>)}</tbody></table></div>{!reviews.length && <p className="disabled-explanation">No formal review has been requested. Microstructure and Dynamics remain blocked until enough resolved forward evidence exists.</p>}</Panel><Panel title="Immutable evidence snapshots"><div className="scroll"><table><thead><tr><th>Strategy</th><th>Cutoff</th><th>Resolved / events / markets</th><th>Brier / log loss versus consensus</th><th>Calibration</th><th>Warnings</th></tr></thead><tbody>{snapshots.map((snapshot) => <tr key={text(snapshot.snapshot_id)}><td>{text(snapshot.strategy_id)}</td><td>{formatTimestamp(snapshot.cutoff_at)}</td><td>{text(snapshot.resolved_predictions)} / {text(snapshot.distinct_events)} / {text(snapshot.distinct_markets)}</td><td>{text(snapshot.point_brier_difference)} / {text(snapshot.point_log_loss_difference)}</td><td>{decimal(snapshot.calibration_coverage)}</td><td>{machineItems(snapshot.integrity_warnings)}</td></tr>)}</tbody></table></div>{!snapshots.length && <p className="disabled-explanation">Snapshots freeze forward evidence and every independently visible criterion. They do not grant authority on their own.</p>}</Panel><Panel title="Campaign PAPER trials"><div className="scroll"><table><thead><tr><th>Strategy / review</th><th>State</th><th>Cycles / Orders / Positions</th><th>Reason</th></tr></thead><tbody>{trials.map((trial) => <tr key={text(trial.authorization_id)}><td>{text(trial.strategy_id)}<small>{text(trial.review_id)}</small></td><td><span className={stateClass(trial.state)}>{text(trial.state)}</span></td><td>{text(record(trial.usage)?.completed_cycles)} / {text(record(trial.usage)?.paper_orders)} / {text(record(trial.usage)?.open_positions)}</td><td>{text(trial.suspension_reason, text(trial.revocation_reason, "—"))}</td></tr>)}</tbody></table></div>{!campaignId && <p className="muted">Choose a Campaign to inspect its trials.</p>}{campaignId && !trials.length && <p className="disabled-explanation">Global PAPER authority alone cannot run a PAPER cycle.</p>}</Panel></>;
}

function StrategyLabControls({ candidates, reviews, deployments, champions, refresh }: { candidates: ApiRecord[]; reviews: ApiRecord[]; deployments: ApiRecord[]; champions: ApiRecord[]; refresh: () => Promise<void> }) {
  const [candidateId, setCandidateId] = useState("");
  const [reviewId, setReviewId] = useState("");
  const [deploymentId, setDeploymentId] = useState("");
  const [assignmentId, setAssignmentId] = useState("");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [candidateConfirmation, setCandidateConfirmation] = useState("");
  const [datasetConfirmation, setDatasetConfirmation] = useState("");
  const [artifactConfirmation, setArtifactConfirmation] = useState("");
  const [evaluationConfirmation, setEvaluationConfirmation] = useState("");
  const [cutoff, setCutoff] = useState(() => new Date().toISOString());
  const [maximumRows, setMaximumRows] = useState("100");
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  useEffect(() => { setCandidateId((value) => value || text(candidates.find((item) => text(item.state) === "ELIGIBLE_FOR_SHADOW_REVIEW")?.candidate_id, "")); setReviewId((value) => value || text(reviews[0]?.review_id, "")); setDeploymentId((value) => value || text(deployments[0]?.deployment_id, "")); setAssignmentId((value) => value || text(champions.find((item) => text(item.state) === "ACTIVE")?.assignment_id, "")); }, [candidates, reviews, deployments, champions]);
  const write = async (path: string, body: ApiRecord, needsReason = true) => { if (needsReason && !reason.trim()) { setError(new ApiError("An operator reason is required.", "OPERATOR_REASON_REQUIRED")); return; } setPending(true); try { await mutate(path, body); await refresh(); setError(null); } catch (failure) { setError(failure); } finally { setPending(false); } };
  const reviewState = text(reviews.find((item) => text(item.review_id) === reviewId)?.state);
  const deploymentState = text(deployments.find((item) => text(item.deployment_id) === deploymentId)?.state);
  const request = () => { if (candidateId) void write(`/api/v1/strategy-lab/candidates/${candidateId}/shadow-reviews`, { reason }); };
  const decide = (action: "start" | "reject" | "defer" | "cancel") => { if (reviewId) void write(`/api/v1/strategy-lab/shadow-reviews/${reviewId}/${action}`, { reason, note }); };
  const approve = () => { if (reviewId) void write(`/api/v1/strategy-lab/shadow-reviews/${reviewId}/approve`, { role: "CHALLENGER", reason, confirm_candidate: candidateConfirmation, confirm_dataset: datasetConfirmation, confirm_artifact_sha256: artifactConfirmation, confirm_evaluation: evaluationConfirmation }); };
  const deploymentAction = (action: "pause" | "resume" | "revoke" | "retire") => { if (deploymentId) void write(`/api/v1/strategy-lab/deployments/${deploymentId}/${action}`, { reason }); };
  const run = () => { if (deploymentId) void write(`/api/v1/strategy-lab/deployments/${deploymentId}/inference-batches`, { adapter: "event-market-retained-v1", cutoff_at: cutoff, maximum_rows: Number(maximumRows) }, false); };
  const resolve = () => { if (deploymentId) void write(`/api/v1/strategy-lab/deployments/${deploymentId}/resolve`, { cutoff_at: cutoff }, false); };
  const evaluate = () => { if (deploymentId) void write(`/api/v1/strategy-lab/deployments/${deploymentId}/evaluate`, { cutoff_at: cutoff }, false); };
  const assign = () => { if (deploymentId) void write("/api/v1/strategy-lab/champions", { reason, confirm_deployment: deploymentId }); };
  const revokeChampion = () => { if (assignmentId) void write(`/api/v1/strategy-lab/champions/${assignmentId}/revoke`, { reason }); };
  return <><Panel title="SHADOW review controls" action={<button onClick={() => void refresh()} disabled={pending}>Refresh authoritative state</button>}><p className="muted">All writes use generated idempotency keys, require a reason where a decision is made, and refresh authoritative local state after completion. They have no strategy, PAPER, or trading authority.</p><div className="form-grid"><label>Eligible candidate<select value={candidateId} onChange={(event) => setCandidateId(event.currentTarget.value)} disabled={pending}>{candidates.map((item) => <option key={text(item.candidate_id)} value={text(item.candidate_id)}>{text(item.state)} · {text(item.candidate_id)}</option>)}</select></label><label>Review<select value={reviewId} onChange={(event) => setReviewId(event.currentTarget.value)} disabled={pending}>{reviews.map((item) => <option key={text(item.review_id)} value={text(item.review_id)}>{text(item.state)} · {text(item.review_id)}</option>)}</select></label><label>Operator reason<textarea value={reason} onChange={(event) => setReason(event.currentTarget.value)} disabled={pending} /></label><label>Bounded note<textarea value={note} maxLength={500} onChange={(event) => setNote(event.currentTarget.value)} disabled={pending} /></label></div><p className="disabled-explanation">Review state: {reviewState || "none"}. Approval is disabled by the local API unless offline eligibility and artifact verification both hold.</p><div className="inline-action"><button onClick={request} disabled={pending || !candidateId}>Request SHADOW Review</button><button onClick={() => decide("start")} disabled={pending || reviewState !== "REQUESTED"}>Start Review</button><button onClick={() => decide("reject")} disabled={pending || reviewState !== "IN_REVIEW"}>Reject</button><button onClick={() => decide("defer")} disabled={pending || reviewState !== "IN_REVIEW"}>Defer</button><button onClick={() => decide("cancel")} disabled={pending || !["REQUESTED", "IN_REVIEW"].includes(reviewState)}>Cancel</button></div><div className="form-grid"><label>Confirm candidate ID<input value={candidateConfirmation} onChange={(event) => setCandidateConfirmation(event.currentTarget.value)} disabled={pending} /></label><label>Confirm dataset snapshot ID<input value={datasetConfirmation} onChange={(event) => setDatasetConfirmation(event.currentTarget.value)} disabled={pending} /></label><label>Confirm artifact SHA-256<input value={artifactConfirmation} onChange={(event) => setArtifactConfirmation(event.currentTarget.value)} disabled={pending} /></label><label>Confirm evaluation ID<input value={evaluationConfirmation} onChange={(event) => setEvaluationConfirmation(event.currentTarget.value)} disabled={pending} /></label></div><button className="primary" onClick={approve} disabled={pending || reviewState !== "IN_REVIEW" || !candidateConfirmation || !datasetConfirmation || !artifactConfirmation || !evaluationConfirmation}>Approve as Challenger</button></Panel><Panel title="Retained-evidence deployment controls"><p className="muted">Batches consume only retained local evidence. No artifact path, provider, financial, or normal strategy control is exposed.</p><div className="form-grid"><label>Deployment<select value={deploymentId} onChange={(event) => setDeploymentId(event.currentTarget.value)} disabled={pending}>{deployments.map((item) => <option key={text(item.deployment_id)} value={text(item.deployment_id)}>{text(item.state)} · {text(item.deployment_id)}</option>)}</select></label><label>Cutoff (UTC)<input value={cutoff} onChange={(event) => setCutoff(event.currentTarget.value)} disabled={pending} /></label><label>Maximum retained rows<input type="number" min="1" max="10000" value={maximumRows} onChange={(event) => setMaximumRows(event.currentTarget.value)} disabled={pending} /></label></div><p className="disabled-explanation">Deployment state: {deploymentState || "none"}. Integrity failures invalidate the deployment and block Resume.</p><div className="inline-action"><button onClick={run} disabled={pending || deploymentState !== "ACTIVE"}>Run Retained-Evidence SHADOW Batch</button><button onClick={resolve} disabled={pending || !deploymentId}>Resolve Retained Outcomes</button><button onClick={evaluate} disabled={pending || !deploymentId}>Build Challenger Evaluation</button><button onClick={() => deploymentAction("pause")} disabled={pending || deploymentState !== "ACTIVE"}>Pause</button><button onClick={() => deploymentAction("resume")} disabled={pending || deploymentState !== "PAUSED"}>Resume</button><button onClick={() => deploymentAction("revoke")} disabled={pending || deploymentState !== "ACTIVE"}>Revoke</button><button onClick={() => deploymentAction("retire")} disabled={pending || deploymentState !== "PAUSED"}>Retire</button></div></Panel><Panel title="Research champion controls" action={<span className="badge">COMPARISON ONLY</span>}><p className="muted">CHAMPION STATUS IS RESEARCH COMPARISON, NOT TRADING AUTHORITY. Assignment requires a completed eligible challenger evaluation and exact deployment confirmation; recommendations never replace a champion automatically.</p><div className="form-grid"><label>Champion assignment<select value={assignmentId} onChange={(event) => setAssignmentId(event.currentTarget.value)} disabled={pending}>{champions.map((item) => <option key={text(item.assignment_id)} value={text(item.assignment_id)}>{text(item.state)} · {text(item.assignment_id)}</option>)}</select></label></div><div className="inline-action"><button onClick={assign} disabled={pending || !deploymentId}>Assign as Research Champion</button><button onClick={revokeChampion} disabled={pending || !assignmentId}>Revoke Champion Assignment</button></div></Panel><ErrorNotice error={error} /></>;
}

function BatchLearningConsole({ datasets, deployments }: { datasets: ApiRecord[]; deployments: ApiRecord[] }) {
  const [items, setItems] = useState<ApiRecord[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [reason, setReason] = useState("Bounded retained-evidence learning with no deployment authority.");
  const [schemaId, setSchemaId] = useState("");
  const [confirmId, setConfirmId] = useState("");
  const [confirmHash, setConfirmHash] = useState("");
  const [deploymentId, setDeploymentId] = useState("");
  const [cutoff, setCutoff] = useState(() => new Date().toISOString());
  const refresh = useCallback(async () => {
    try {
      setItems(asItems(await api<{ items: ApiRecord[] }>("/api/v1/strategy-lab/learning-plans")));
      setError(null);
    } catch (failure) { setError(failure); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    setSchemaId((value) => value || text(record(datasets[0]?.schema)?.schema_id, ""));
    setDeploymentId((value) => value || text(deployments.find((item) => text(item.state) === "ACTIVE")?.deployment_id, ""));
  }, [datasets, deployments]);
  const write = async (path: string, body: ApiRecord = {}) => {
    setPending(true); setError(null);
    try { await mutate(path, body); await refresh(); } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  const create = () => void write("/api/v1/strategy-lab/learning-plans", {
    name: "Bounded retained-evidence learning",
    source_scope: "strategy-lab-forward-evaluations-v1",
    feature_schema_id: schemaId,
    search_template: "strategy-lab-initial-bounded-v1",
    minimum_new_groups: 30,
    minimum_new_observations: 120,
    maximum_trials_per_cycle: 20,
    maximum_cycles: 5,
    reason,
  });
  const wrappers = items.map((item) => ({ plan: record(item.plan), cycles: records(item.cycles), windows: records(item.evidence_windows), details: records(item.cycle_details) }));
  return <><Panel title="Automated batch learning" action={<span className="badge">RESEARCH / AUTHORITY NONE</span>}>
    <p className="muted">AUTOMATED LEARNING DOES NOT AUTHORIZE MODEL DEPLOYMENT</p>
    <p className="muted">CHAMPION REPLACEMENT REQUIRES EXPLICIT OPERATOR ACTION</p>
    <div className="form-grid"><label>Exact feature schema<select value={schemaId} onChange={(event) => setSchemaId(event.currentTarget.value)}>{datasets.map((item) => <option key={text(record(item.schema)?.schema_id)} value={text(record(item.schema)?.schema_id)}>{text(item.name)} · {text(record(item.schema)?.schema_id)}</option>)}</select></label><label>Operator reason<textarea value={reason} onChange={(event) => setReason(event.currentTarget.value)} /></label></div>
    <button className="primary" disabled={pending || !schemaId || !reason} onClick={create}>Create Draft Learning Plan</button>
    <p className="disabled-explanation">Plans freeze source scope, evidence thresholds, model families, per-cycle search budget, cooldown, drift policy, and forward comparison policy. Activation requires the exact plan ID and hash.</p>
  </Panel>
  <Panel title="Learning plans, cycles, and forward windows"><div className="scroll"><table><thead><tr><th>Plan / state</th><th>Watermark / cycle budget</th><th>Latest cycle / evidence</th><th>Forward comparison</th><th>Explicit controls</th></tr></thead><tbody>{wrappers.map(({ plan, cycles, windows, details }) => { const latest = cycles[cycles.length - 1]; const evidence = windows.find((item) => text(item.window_id) === text(latest?.evidence_window_id)); const detail = details.find((item) => text(record(item.cycle)?.cycle_id) === text(latest?.cycle_id)); const binding = record(detail?.binding); const forward = record(detail?.forward_window); const comparison = record(detail?.challenger_evaluation); const decisions = records(detail?.decisions); const decision = decisions[decisions.length - 1]; const state = text(plan?.state); return <tr key={text(plan?.plan_id)}><td>{text(plan?.name)}<small>{text(plan?.plan_id)} · {state}<br />scope {text(plan?.source_scope_hash)}</small></td><td>{text(plan?.minimum_new_resolved_observations)} observations / {text(plan?.minimum_new_independent_groups)} groups<small>{cycles.length} / {text(plan?.maximum_cycles)} cycles · cooldown {text(plan?.minimum_cooldown_seconds)}s<br />watermark {text(evidence?.new_watermark, "none")}</small></td><td>{text(latest?.state, "no cycle")}<small>new {text(evidence?.included_observation_count, "0")} / groups {text(evidence?.included_independent_group_count, "0")} / excluded {Number(evidence?.excluded_unresolved_count ?? 0) + Number(evidence?.excluded_duplicate_count ?? 0) + Number(evidence?.excluded_unavailable_count ?? 0) + Number(evidence?.excluded_integrity_count ?? 0)}<br />dataset {text(latest?.dataset_id, "none")} · family {text(latest?.experiment_id, "none")}<br />search {text(latest?.search_run_id, "none")} · candidate {text(latest?.eligible_candidate_id, "none")}<br />trials {list(binding?.research_trial_ids).length} · mirrors {list(binding?.mlflow_record_ids).length}</small></td><td>{text(forward?.state, "not opened")}<small>predictions {text(forward?.champion_prediction_count, "0")} / {text(forward?.challenger_prediction_count, "0")}<br />resolved {text(forward?.champion_resolved_count, "0")} / {text(forward?.challenger_resolved_count, "0")} · unmatched {text(forward?.unmatched_count, "0")}<br />Brier {text(comparison?.champion_brier, "—")} / {text(comparison?.calibrated_brier, "—")} · log loss {text(comparison?.champion_log_loss, "—")} / {text(comparison?.calibrated_log_loss, "—")}<br />drift {text(record(comparison?.drift_metrics)?.state, "—")} · {text(decision?.recommendation, "no recommendation")}</small></td><td><div className="inline-action">
    <button disabled={pending || state !== "DRAFT"} onClick={() => { setConfirmId(text(plan?.plan_id)); setConfirmHash(text(plan?.canonical_input_hash)); }}>Load Exact Confirmation</button>
    <button disabled={pending || state !== "DRAFT" || confirmId !== text(plan?.plan_id) || confirmHash !== text(plan?.canonical_input_hash)} onClick={() => void write(`/api/v1/strategy-lab/learning-plans/${text(plan?.plan_id)}/activate`, { confirm_plan_id: confirmId, confirm_plan_hash: confirmHash, reason })}>Activate</button>
    <button disabled={pending || state !== "ACTIVE"} onClick={() => void write(`/api/v1/strategy-lab/learning-plans/${text(plan?.plan_id)}/trigger`)}>Evaluate Trigger</button>
    <button disabled={pending || text(latest?.state) !== "REQUESTED"} onClick={() => void write(`/api/v1/strategy-lab/learning-cycles/${text(latest?.cycle_id)}/run`)}>Run Eligible Cycle</button>
    <button disabled={pending || !latest} onClick={() => void write(`/api/v1/strategy-lab/learning-cycles/${text(latest?.cycle_id)}/recover`)}>Recover</button>
    <button disabled={pending || !latest || ["COMPLETED", "CANCELED"].includes(text(latest?.state))} onClick={() => void write(`/api/v1/strategy-lab/learning-cycles/${text(latest?.cycle_id)}/cancel`, { reason })}>Cancel</button>
    <button disabled={pending || state !== "ACTIVE"} onClick={() => void write(`/api/v1/strategy-lab/learning-plans/${text(plan?.plan_id)}/pause`, { reason })}>Pause</button>
    <button disabled={pending || state !== "PAUSED"} onClick={() => void write(`/api/v1/strategy-lab/learning-plans/${text(plan?.plan_id)}/resume`, { reason })}>Resume</button>
    <button className="danger" disabled={pending || state !== "ACTIVE"} onClick={() => void write(`/api/v1/strategy-lab/learning-plans/${text(plan?.plan_id)}/revoke`, { reason })}>Revoke</button>
    <button disabled={pending || text(latest?.state) !== "ELIGIBLE_FOR_SHADOW_REVIEW" || !deploymentId} onClick={() => void write(`/api/v1/strategy-lab/learning-cycles/${text(latest?.cycle_id)}/attach-challenger`, { deployment_id: deploymentId })}>Attach Existing Approved Challenger</button>
    <button disabled={pending || !latest?.forward_window_id} onClick={() => void write(`/api/v1/strategy-lab/forward-windows/${text(latest?.forward_window_id)}/infer`, { cutoff_at: cutoff })}>Collect Forward Predictions</button>
    <button disabled={pending || !latest?.forward_window_id} onClick={() => void write(`/api/v1/strategy-lab/forward-windows/${text(latest?.forward_window_id)}/evaluate`, { cutoff_at: cutoff })}>Resolve / Evaluate</button>
  </div></td></tr>; })}</tbody></table></div>
  <div className="form-grid"><label>Exact plan ID confirmation<input value={confirmId} onChange={(event) => setConfirmId(event.currentTarget.value)} /></label><label>Exact plan hash confirmation<input value={confirmHash} onChange={(event) => setConfirmHash(event.currentTarget.value)} /></label><label>Existing ACTIVE SHADOW deployment<select value={deploymentId} onChange={(event) => setDeploymentId(event.currentTarget.value)}>{deployments.map((item) => <option key={text(item.deployment_id)} value={text(item.deployment_id)}>{text(item.state)} · {text(item.deployment_id)}</option>)}</select></label><label>Forward cutoff (UTC)<input value={cutoff} onChange={(event) => setCutoff(event.currentTarget.value)} /></label></div>
  <p className="disabled-explanation">An eligible cycle only links to the existing exact-confirmed SHADOW review workflow. There is no automatic Deploy or Replace Champion control. Every evidence window, trial, decision, attachment, prediction, evaluation, and watermark remains visible and undeletable.</p>
  </Panel><ErrorNotice error={error} /></>;
}

function StrategyLab() {
  const [datasets, setDatasets] = useState<ApiRecord[]>([]);
  const [runs, setRuns] = useState<ApiRecord[]>([]);
  const [candidates, setCandidates] = useState<ApiRecord[]>([]);
  const [shadowReviews, setShadowReviews] = useState<ApiRecord[]>([]);
  const [deployments, setDeployments] = useState<ApiRecord[]>([]);
  const [champions, setChampions] = useState<ApiRecord[]>([]);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    try {
      const [datasetValue, runValue, candidateValue, reviewValue, deploymentValue, championValue] = await Promise.all([
        api<{ items: ApiRecord[] }>("/api/v1/strategy-lab/datasets"),
        api<{ items: ApiRecord[] }>("/api/v1/strategy-lab/training-runs"),
        api<{ items: ApiRecord[] }>("/api/v1/strategy-lab/candidates"),
        api<{ items: ApiRecord[] }>("/api/v1/strategy-lab/shadow-reviews"),
        api<{ items: ApiRecord[] }>("/api/v1/strategy-lab/deployments"),
        api<{ items: ApiRecord[] }>("/api/v1/strategy-lab/champions"),
      ]);
      setDatasets(asItems(datasetValue)); setRuns(asItems(runValue)); setCandidates(asItems(candidateValue)); setShadowReviews(asItems(reviewValue)); setDeployments(asItems(deploymentValue)); setChampions(asItems(championValue)); setError(null);
    } catch (failure) { setError(failure); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  return <><div className="section-head"><div><p className="eyebrow">SHADOW MODEL — NO CAPITAL AUTHORITY</p><h1>Strategy Lab</h1><p className="muted">SHADOW deployments emit retained research probabilities only. Champion status is comparison, not strategy, PAPER, or trading authority.</p></div><button onClick={() => void refresh()}>Refresh</button></div><ErrorNotice error={error} /><BatchLearningConsole datasets={datasets} deployments={deployments} /><Panel title="Immutable datasets"><div className="scroll"><table><thead><tr><th>Name</th><th>Vertical / adapter</th><th>Cutoff</th><th>Rows / groups</th><th>State</th><th>Artifact</th><th>Warnings</th></tr></thead><tbody>{datasets.map((item) => <tr key={text(item.dataset_id)}><td>{text(item.name)}<small>{text(item.dataset_id)}</small></td><td>{text(record(item.schema)?.vertical)} / {text(item.adapter_id)}</td><td>{formatTimestamp(item.cutoff_at)}</td><td>{text(item.row_count)} / {text(item.grouping_count)}</td><td><span className={stateClass(item.state)}>{text(item.state)}</span></td><td><small>{text(record(item.artifact)?.sha256)}</small></td><td>{machineItems(item.integrity_warnings)}</td></tr>)}</tbody></table></div>{!datasets.length && <p className="disabled-explanation">No immutable research dataset has been built from retained evidence.</p>}</Panel><Panel title="Candidate evaluation"><div className="scroll"><table><thead><tr><th>Candidate</th><th>Model</th><th>State</th><th>Dataset / model artifact</th><th>Automatic reasons</th></tr></thead><tbody>{candidates.map((item) => <tr key={text(item.candidate_id)}><td><small>{text(item.candidate_id)}</small></td><td>{text(item.model_family)}</td><td><span className={stateClass(item.state)}>{text(item.state)}</span></td><td><small>{text(item.dataset_hash)} / {text(item.model_artifact_hash)}</small></td><td>{machineItems(item.rejection_reasons)}</td></tr>)}</tbody></table></div></Panel><Panel title="SHADOW reviews"><div className="scroll"><table><thead><tr><th>Candidate</th><th>Family / vertical</th><th>State</th><th>Reason</th></tr></thead><tbody>{shadowReviews.map((item) => <tr key={text(item.review_id)}><td><small>{text(item.candidate_id)}</small></td><td>{text(item.requested_deployment_family)} / {text(item.requested_vertical)}</td><td><span className={stateClass(item.state)}>{text(item.state)}</span></td><td>{text(item.operator_reason)}</td></tr>)}</tbody></table></div><p className="disabled-explanation">Requests, decisions, and exact approval confirmations use the local API. No automatic deployment or promotion exists.</p></Panel><StrategyLabControls candidates={candidates} reviews={shadowReviews} deployments={deployments} champions={champions} refresh={refresh} /><Panel title="Research deployments"><div className="scroll"><table><thead><tr><th>Role / state</th><th>Candidate</th><th>Artifact / schema</th><th>Batches</th><th>Integrity</th></tr></thead><tbody>{deployments.map((item) => <tr key={text(item.deployment_id)}><td>{text(item.role)}<small>{text(item.state)}</small></td><td><small>{text(item.candidate_id)}</small></td><td><small>{text(record(item.artifact)?.sha256)} / {text(item.feature_schema_hash)}</small></td><td>{text(item.batch_count, "retained batches")}</td><td>{text(item.last_error_code, "verified on activation/inference")}</td></tr>)}</tbody></table></div></Panel><Panel title="Champion–challenger comparison" action={<span className="badge">COMPARISON ONLY</span>}><p className="muted">CHAMPION STATUS IS RESEARCH COMPARISON, NOT TRADING AUTHORITY. Recommendation records never replace a champion automatically.</p><div className="scroll"><table><thead><tr><th>Vertical / family</th><th>Deployment</th><th>State</th><th>Reason</th></tr></thead><tbody>{champions.map((item) => <tr key={text(item.assignment_id)}><td>{text(item.vertical)} / {text(item.research_family)}</td><td><small>{text(item.deployment_id)}</small></td><td><span className={stateClass(item.state)}>{text(item.state)}</span></td><td>{text(item.reason)}</td></tr>)}</tbody></table></div></Panel></>;
}

function AutomatedCandidateSearches() {
  const [items, setItems] = useState<ApiRecord[]>([]);
  const [mlflow, setMlflow] = useState<ApiRecord>({});
  const [detail, setDetail] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [values, setValues] = useState({ experimentId: "", datasetId: "", featureSchemaId: "", name: "Bounded Strategy Lab candidate search", maximumTrials: "30", seed: "20260727", reason: "Explore only the immutable managed Strategy Lab search space." });
  const refresh = useCallback(async () => {
    try {
      const value = await api<{ items: ApiRecord[]; mlflow: ApiRecord }>("/api/v1/strategy-lab/automated-searches");
      setItems(asItems(value));
      setMlflow(record(value.mlflow) ?? {});
      setError(null);
    } catch (failure) { setError(failure); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const create = async () => {
    setPending(true); setError(null);
    try {
      await mutate("/api/v1/strategy-lab/automated-searches", { experiment_id: values.experimentId, name: values.name, dataset_id: values.datasetId, feature_schema_id: values.featureSchemaId, specification: "strategy-lab-initial-bounded-v1", objective: "BRIER", maximum_trials: Number(values.maximumTrials), seed: Number(values.seed), operator_reason: values.reason });
      await refresh();
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  const run = async (planId: string, trials: number) => {
    setPending(true); setError(null);
    try {
      const result = await mutate<ApiRecord>(`/api/v1/strategy-lab/automated-searches/${planId}/runs`, { requested_trials: trials });
      const runId = text(record(result.run)?.search_run_id);
      if (runId) setDetail(await api(`/api/v1/strategy-lab/automated-search-runs/${runId}`));
      await refresh();
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  const act = async (runId: string, action: "recover" | "cancel") => {
    setPending(true); setError(null);
    try {
      await mutate(`/api/v1/strategy-lab/automated-search-runs/${runId}/${action}`, action === "cancel" ? { operator_reason: "Explicit operator cancellation of bounded research.", confirmation: null } : {});
      setDetail(await api(`/api/v1/strategy-lab/automated-search-runs/${runId}`));
      await refresh();
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  const reconcile = async (experimentId: string) => {
    setPending(true); setError(null);
    try { await mutate(`/api/v1/strategy-lab/mlflow-mirror/${experimentId}/reconcile`, {}); } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  const bindings = records(detail?.bindings);
  const decision = record(detail?.decision);
  return <><Panel title="Bounded automated candidate generation" action={<span className="badge">RESEARCH / AUTHORITY NONE</span>}><p className="muted">AUTOMATED SEARCH EXPLORES ONLY THE PREDECLARED BOUNDED SPACE</p><p className="muted">OPTUNA PROPOSES PARAMETERS — APEX OWNS THE TRIAL LEDGER AND DECISION</p><p className="muted">MLFLOW IS A LOCAL RESEARCH MIRROR — IT GRANTS NO AUTHORITY</p><div className="form-grid"><label>OPEN experiment family ID<input value={values.experimentId} onChange={(event) => setValues({ ...values, experimentId: event.currentTarget.value })} /></label><label>READY dataset ID<input value={values.datasetId} onChange={(event) => setValues({ ...values, datasetId: event.currentTarget.value })} /></label><label>Exact feature schema ID<input value={values.featureSchemaId} onChange={(event) => setValues({ ...values, featureSchemaId: event.currentTarget.value })} /></label><label>Plan name<input value={values.name} onChange={(event) => setValues({ ...values, name: event.currentTarget.value })} /></label><label>Maximum trials<input type="number" min="1" max="500" value={values.maximumTrials} onChange={(event) => setValues({ ...values, maximumTrials: event.currentTarget.value })} /></label><label>Seed<input type="number" min="0" value={values.seed} onChange={(event) => setValues({ ...values, seed: event.currentTarget.value })} /></label><label className="wide">Operator reason<textarea value={values.reason} onChange={(event) => setValues({ ...values, reason: event.currentTarget.value })} /></label></div><button className="primary" disabled={pending || !values.experimentId || !values.datasetId || !values.featureSchemaId || !values.reason} onClick={() => void create()}>Create Search Plan</button><p className="disabled-explanation">The managed specification permits only Logistic Regression and Histogram Gradient Boosting, fixed calibration choices, seeded TPE, NopPruner, and concurrency one. No arbitrary code or open-ended range is accepted.</p></Panel><Panel title="Automated searches"><div className="scroll"><table><thead><tr><th>Family / plan</th><th>Objective / optimizer</th><th>Budgets / state</th><th>Raw best / Apex eligible best</th><th>Controls</th></tr></thead><tbody>{items.map((wrapper) => { const plan = record(wrapper.plan); const runs = records(wrapper.runs); const latest = runs[runs.length - 1]; return <tr key={text(plan?.search_plan_id)}><td>{text(plan?.display_name)}<small>{text(plan?.experiment_id)} · {text(plan?.search_plan_id)}</small></td><td>{text(plan?.objective_identity)}<small>{text(plan?.sampler_identity)} {text(plan?.sampler_version)} · seed {text(plan?.random_seed)}</small></td><td>{text(latest?.reserved_trial_count, "0")} / {text(plan?.maximum_trials)}<small>{text(latest?.state, text(plan?.state))} · artifact bytes {text(latest?.artifact_bytes_written, "0")}</small></td><td><small>raw {text(latest?.best_raw_trial_id, "none")}<br />eligible {text(latest?.best_integrity_eligible_trial_id, "none")}<br />{text(latest?.final_recommendation, "not evaluated")}</small></td><td><div className="inline-action"><button disabled={pending || text(plan?.state) !== "READY"} onClick={() => void run(text(plan?.search_plan_id), Number(plan?.maximum_trials))}>Start Search</button><button disabled={pending || !latest} onClick={() => { const id = text(latest?.search_run_id); if (id) void api<ApiRecord>(`/api/v1/strategy-lab/automated-search-runs/${id}`).then(setDetail).catch(setError); }}>Inspect</button><button disabled={pending || !["REQUESTED", "RUNNING"].includes(text(latest?.state))} onClick={() => void act(text(latest?.search_run_id), "recover")}>Resume / Recover</button><button className="danger" disabled={pending || !["REQUESTED", "RUNNING"].includes(text(latest?.state))} onClick={() => void act(text(latest?.search_run_id), "cancel")}>Cancel Search</button><button disabled={pending} onClick={() => void reconcile(text(plan?.experiment_id))}>Reconcile MLflow</button></div></td></tr>; })}</tbody></table></div>{!items.length && <p className="disabled-explanation">No bounded automated search plan has been created.</p>}</Panel><Panel title="MLflow local research mirror"><KeyValueGrid entries={[["Availability", text(mlflow.tracking_mode, "LOCAL_SQLITE_TRACKING")], ["Exact version", text(mlflow.version)], ["Loopback UI", text(mlflow.loopback_url)], ["Bind host", text(mlflow.bind_host)], ["Server required for search", text(mlflow.server_required_for_search)], ["Authority", text(mlflow.authority, "NONE")]]} /><p className="disabled-explanation">Apex artifact identities and hashes remain authoritative. There is no MLflow serving, stage, alias, remote tracking, or deployment control.</p></Panel>{detail && <Panel title="Complete automated trial ledger" action={<span className="badge">FAILED AND LOSING TRIALS VISIBLE</span>}><div className="scroll"><table><thead><tr><th>Apex / Optuna trial</th><th>Parameters</th><th>Training / objective</th><th>Artifacts</th><th>MLflow / reasons</th></tr></thead><tbody>{bindings.map((item) => <tr key={text(item.binding_id)}><td>{text(item.research_trial_id)}<small>Optuna #{text(item.optuna_trial_number)}</small></td><td><small>{JSON.stringify(record(item.parameter_payload) ?? {})}</small></td><td><span className={stateClass(item.objective_state)}>{text(item.objective_state)}</span><small>{text(item.objective_value, "unavailable")}</small></td><td><small>{list(item.artifact_hashes).join(" · ") || "none"}</small></td><td><small>{text(item.mlflow_mirror_record_id, "not mirrored")}<br />{text(item.failure_reason, "no failure")}</small></td></tr>)}</tbody></table></div><KeyValueGrid entries={[["Raw best", text(decision?.raw_best_trial_id, "none")], ["Eligible candidate set", list(decision?.integrity_eligible_trial_ids).join(", ") || "none"], ["Selected candidate", text(decision?.selected_candidate_trial_id, "none")], ["Integrity state", text(decision?.integrity_policy_state, "not evaluated")], ["Recommendation", text(decision?.recommendation, "not evaluated")], ["Hard gates", machineItems(decision?.hard_gate_reason_codes)]]} /><p className="disabled-explanation">CANDIDATE ELIGIBILITY DOES NOT GRANT DEPLOYMENT OR CAPITAL AUTHORITY. This workflow intentionally has no Deploy, Champion, PAPER, or LIVE control.</p></Panel>}<ErrorNotice error={error} /></>;
}

function ResearchIntegrity() {
  const [families, setFamilies] = useState<ApiRecord[]>([]);
  const [trials, setTrials] = useState<ApiRecord[]>([]);
  const [evaluations, setEvaluations] = useState<ApiRecord[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [reason, setReason] = useState("Predeclare a bounded canonical Securities comparison before any trial runs.");
  const [values, setValues] = useState({
    name: "Canonical Securities research family",
    question: "Which canonical daily Securities baseline remains stable after costs and research-selection controls?",
    dataBatchId: "",
    universeId: "managed-securities-universe-v1",
    start: "2025-01-01",
    end: "2025-12-31",
    maximumTrials: "64",
  });
  const loadDetail = useCallback(async (experimentId: string) => {
    if (!experimentId) { setTrials([]); setEvaluations([]); return; }
    const [trialValue, evaluationValue] = await Promise.all([
      api<{ items: ApiRecord[] }>(`/api/v1/research-integrity/experiment-families/${experimentId}/trials`),
      api<{ items: ApiRecord[] }>(`/api/v1/research-integrity/experiment-families/${experimentId}/evaluations`),
    ]);
    setTrials(asItems(trialValue));
    setEvaluations(asItems(evaluationValue));
  }, []);
  const refresh = useCallback(async () => {
    try {
      const value = await api<{ items: ApiRecord[] }>("/api/v1/research-integrity/experiment-families");
      const loaded = asItems(value);
      setFamilies(loaded);
      const next = selectedId || text(record(loaded[0]?.family)?.experiment_id, "");
      setSelectedId(next);
      await loadDetail(next);
      setError(null);
    } catch (failure) { setError(failure); }
  }, [loadDetail, selectedId]);
  useEffect(() => { void refresh(); }, [refresh]);
  const create = async () => {
    setPending(true); setError(null);
    try {
      const result = await mutate<ApiRecord>("/api/v1/research-integrity/experiment-families", {
        name: values.name,
        vertical: "SECURITIES",
        research_question: values.question,
        target_label_identity: "daily-total-return-v1",
        benchmark_identities: ["CASH", "BUY_AND_HOLD"],
        data_batch_id: values.dataBatchId,
        universe_id: values.universeId,
        start: values.start,
        end: values.end,
        permitted_families: ["CASH", "BUY_AND_HOLD", "EQUAL_WEIGHT_REBALANCE", "TIME_SERIES_MOMENTUM", "CROSS_SECTIONAL_MOMENTUM", "SHORT_HORIZON_MEAN_REVERSION", "PAIRS_DISTANCE_MEAN_REVERSION", "VOLATILITY_TARGETED_LONG_ONLY"],
        managed_specification: "canonical-securities-baselines-v1",
        maximum_trials: Number(values.maximumTrials),
        maximum_feature_variants: 1,
        maximum_model_variants: 8,
        maximum_simulator_variants: 3,
        operator_reason: reason,
      });
      setSelectedId(text(result.experiment_id));
      await refresh();
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  const transition = async (action: "close" | "cancel") => {
    if (!selectedId) return;
    setPending(true); setError(null);
    try {
      await mutate(`/api/v1/research-integrity/experiment-families/${selectedId}/${action}`, { operator_reason: reason });
      await refresh();
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  const evaluate = async () => {
    if (!selectedId) return;
    setPending(true); setError(null);
    try {
      await mutate(`/api/v1/research-integrity/experiment-families/${selectedId}/evaluations`, { cutoff_at: new Date().toISOString() });
      await loadDetail(selectedId);
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  const selectedWrapper = families.find((item) => text(record(item.family)?.experiment_id) === selectedId);
  const selected = record(selectedWrapper?.family);
  return <><div className="section-head"><div><p className="eyebrow">IMMUTABLE ALL-ATTEMPT RESEARCH LEDGER</p><h1>Research Integrity</h1><p className="muted">TESTING MORE VARIANTS INCREASES THE CHANCE OF A FALSE WINNER</p><p className="muted">RESEARCH INTEGRITY ELIGIBILITY DOES NOT GRANT TRADING AUTHORITY</p></div><button onClick={() => void refresh()}>Refresh</button></div><ErrorNotice error={error} /><Panel title="Create and open a frozen experiment family" action={<span className="badge">RESEARCH ONLY</span>}><div className="form-grid"><label>Name<input value={values.name} onChange={(event) => setValues({ ...values, name: event.currentTarget.value })} /></label><label>Research question<textarea value={values.question} onChange={(event) => setValues({ ...values, question: event.currentTarget.value })} /></label><label>Managed data batch ID<input value={values.dataBatchId} onChange={(event) => setValues({ ...values, dataBatchId: event.currentTarget.value })} /></label><label>Managed universe ID<input value={values.universeId} onChange={(event) => setValues({ ...values, universeId: event.currentTarget.value })} /></label><label>Start<input type="date" value={values.start} onChange={(event) => setValues({ ...values, start: event.currentTarget.value })} /></label><label>End<input type="date" value={values.end} onChange={(event) => setValues({ ...values, end: event.currentTarget.value })} /></label><label>Maximum trials<input value={values.maximumTrials} onChange={(event) => setValues({ ...values, maximumTrials: event.currentTarget.value })} /></label><label>Operator reason<textarea value={reason} onChange={(event) => setReason(event.currentTarget.value)} /></label></div><button className="primary" disabled={pending || !values.dataBatchId || !reason} onClick={() => void create()}>Create / Open Frozen Family</button><p className="disabled-explanation">Scope, search space, budgets, data, universe, period, costs, and evaluation policy become immutable at OPEN. Expansion requires a new family.</p></Panel><Panel title="Experiment families"><div className="scroll"><table><thead><tr><th>Family / question</th><th>Data / period</th><th>Budget used</th><th>State</th><th>Configuration hash</th></tr></thead><tbody>{families.map((item) => { const family = record(item.family); return <tr className="clickable" key={text(family?.experiment_id)} onClick={() => { const id = text(family?.experiment_id); setSelectedId(id); void loadDetail(id); }}><td><strong>{text(family?.name)}</strong><small>{text(family?.research_question)}</small></td><td>{list(family?.data_scope_identities).join(", ")}<small>{text(family?.period_start)} — {text(family?.period_end)}</small></td><td>{text(item.trial_budget_used)} / {text(family?.maximum_trials)}</td><td><span className={stateClass(family?.state)}>{text(family?.state)}</span></td><td><small>{text(family?.canonical_input_hash)}</small></td></tr>; })}</tbody></table></div>{!families.length && <p className="disabled-explanation">No governed experiment family has been opened.</p>}</Panel>{selected && <><Panel title={`Family controls · ${text(selected.name)}`}><KeyValueGrid entries={[["Question", text(selected.research_question)], ["Permitted families", list(selected.permitted_families).join(", ")], ["Declared parameter space", JSON.stringify(record(selected.parameter_space) ?? {})], ["Trial budget", `${text(selectedWrapper?.trial_budget_used)} / ${text(selected.maximum_trials)}`], ["Scope hash", text(selected.canonical_input_hash)]]} /><div className="inline-action"><button disabled={pending || text(selected.state) !== "OPEN"} onClick={() => void evaluate()}>Evaluate Selection Integrity</button><button disabled={pending || text(selected.state) !== "OPEN"} onClick={() => void transition("close")}>Close Family</button><button className="danger" disabled={pending || !["DRAFT", "OPEN"].includes(text(selected.state))} onClick={() => void transition("cancel")}>Cancel Family</button></div></Panel><Panel title="Complete trial ledger" action={<span className="badge">FAILED TRIALS VISIBLE</span>}><div className="scroll"><table><thead><tr><th>Ordinal</th><th>Source / family</th><th>Simulator</th><th>State / result</th><th>Metric / artifact lineage</th></tr></thead><tbody>{trials.map((item) => { const trial = record(item.trial); const metric = record(item.metric); return <tr key={text(trial?.trial_id)}><td>{text(trial?.ordinal)}</td><td>{text(trial?.source_type)}<small>{text(trial?.strategy_model_family)} · {text(trial?.configuration_hash)}</small></td><td>{text(trial?.simulator_identity)}<small>{text(trial?.simulator_version)}</small></td><td><span className={stateClass(trial?.state)}>{text(trial?.state)}</span><small>{text(trial?.failure_reason, text(trial?.result_identity))}</small></td><td>Sharpe {text(metric?.sharpe_ratio)}<small>{records(item.artifacts).map((artifact) => text(artifact.sha256)).join(" · ") || "No aligned return artifact"}</small></td></tr>; })}</tbody></table></div><p className="disabled-explanation">Every attempted, failed, unavailable, losing, or integrity-rejected trial remains visible. There is no deletion control or default failed-run filter.</p></Panel><Panel title="Multiple-testing report"><div className="scroll"><table><thead><tr><th>Raw / effective trials</th><th>Best / expected null</th><th>PSR / DSR / PBO</th><th>Stability</th><th>Recommendation</th><th>Hard-gate reasons</th></tr></thead><tbody>{evaluations.map((item) => <tr key={text(item.evaluation_id)}><td>{text(item.raw_comparable_trial_count)} / {text(item.effective_independent_trials)}</td><td>{text(item.best_observed_metric)} / {text(item.expected_maximum_sharpe)}</td><td>{text(item.probabilistic_sharpe_ratio)} / {text(item.deflated_sharpe_ratio)} / {text(item.probability_backtest_overfitting)}</td><td>{JSON.stringify(record(item.rank_stability) ?? {})}</td><td><span className={stateClass(item.state)}>{text(item.recommendation)}</span></td><td>{machineItems(item.reason_codes)}</td></tr>)}</tbody></table></div><p className="disabled-explanation">BEST OBSERVED RESULT MAY REFLECT RESEARCH SELECTION. Eligibility is only evidence for a later governed forward-SHADOW review; it creates no deployment, PAPER, capital, order, or LIVE authority.</p></Panel></>}</>;
}

function StrategyIntelligence() {
  const [data, setData] = useState<ApiRecord>({});
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    try { setData(await api<ApiRecord>("/api/v1/strategy-intelligence/runs")); setError(null); }
    catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const runs = asItems(data.items);
  const snapshot = record(data.latest_snapshot);
  const regime = record(snapshot?.regime);
  const opportunities = records(snapshot?.opportunities);
  const evaluation = records(data.latest_evaluation);
  return <><div className="section-head"><div><p className="eyebrow">HISTORICAL RESEARCH — AUTHORITY NONE</p><h1>Strategy Intelligence</h1><p className="muted">STRATEGY INTELLIGENCE IS RESEARCH ONLY</p><p className="muted">ABSTENTION IS A VALID DECISION</p><p className="muted">GENUINE HISTORICAL PERFORMANCE DOES NOT ESTABLISH A TRADING EDGE</p></div><button onClick={() => void refresh()}>Refresh</button></div><ErrorNotice error={error} /><Panel title="Latest historical research snapshot" action={<span className="badge">NOT A CURRENT MARKET RECOMMENDATION</span>}>{snapshot ? <><KeyValueGrid entries={[["Decision session", text(snapshot.session_id)], ["Regime", text(regime?.regime)], ["Regime confidence", decimal(regime?.confidence)], ["Regime reasons", machineItems(regime?.reason_codes)]]} /><div className="scroll"><table><thead><tr><th>Rank</th><th>Instrument</th><th>Expected net</th><th>Action</th><th>Desired exposure</th><th>Constraints</th></tr></thead><tbody>{opportunities.map((item) => { const decision = record(item.decision); const components = record(item.components); return <tr key={`${text(item.decision_session_id)}-${text(item.instrument_id)}`}><td>{text(item.rank)}</td><td>{text(item.instrument_id)}</td><td>{decimal(components?.expected_net_return)}</td><td><span className={stateClass(decision?.action)}>{text(decision?.action)}</span></td><td>{decimal(decision?.desired_exposure)}</td><td>{machineItems(decision?.binding_constraints)}</td></tr>; })}</tbody></table></div></> : <Empty title="No retained intelligence run" text="Run the local strategy-intelligence demo against an existing genuine dataset. It cannot fetch providers or create financial state." />}</Panel><Panel title="Fold and untouched-holdout evaluation"><div className="scroll"><table><thead><tr><th>Partition</th><th>Coverage</th><th>Abstention</th><th>Gross / net</th><th>Max drawdown</th><th>Cost burden</th><th>False positives</th></tr></thead><tbody>{evaluation.map((item) => <tr key={text(item.partition)}><td>{decimal(item.action_coverage)}</td><td>{decimal(item.abstention_rate)}</td><td>{decimal(item.realized_gross_return)} / {decimal(item.realized_net_return)}</td><td>{decimal(item.maximum_drawdown)}</td><td>{decimal(item.cost_burden)}</td><td>{decimal(item.false_positive_rate)}</td></tr>)}</tbody></table></div></Panel><Panel title="Immutable research runs"><div className="scroll"><table><thead><tr><th>Run</th><th>Dataset</th><th>Integrity / conformance</th><th>Blockers</th><th>Lineage</th></tr></thead><tbody>{runs.map((item) => <tr key={text(item.run_id)}><td><small>{text(item.run_id)}</small></td><td><small>{text(item.dataset_id)}</small></td><td>{text(item.model_integrity_state)} / {text(item.simulator_conformance_state)}</td><td>{machineItems(item.blocking_reasons)}</td><td><small>{text(item.canonical_result_hash)}</small></td></tr>)}</tbody></table></div></Panel></>;
}

function EnsembleMetaStrategy() {
  const [data, setData] = useState<ApiRecord>({}); const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => { try { setData(await api<ApiRecord>("/api/v1/ensemble-meta-strategy/runs")); setError(null); } catch (reason) { setError(reason); } }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const snapshot = record(data.latest_snapshot); const ensembles = records(snapshot?.ensembles); const decisions = records(snapshot?.decisions); const portfolios = records(snapshot?.portfolios);
  return <><div className="section-head"><div><p className="eyebrow">RESEARCH ONLY — AUTHORITY NONE</p><h1>Ensemble and Meta-Strategy</h1><p className="muted">META-STRATEGY OUTPUT IS RESEARCH ONLY</p><p className="muted">BLOCKED MODELS CANNOT GRANT ACTION AUTHORITY</p><p className="muted">CASH AND ABSTENTION ARE VALID OUTCOMES</p></div><button onClick={() => void refresh()}>Refresh</button></div><ErrorNotice error={error} /><Panel title="Latest historical meta-strategy snapshot" action={<span className="badge">NOT A CURRENT RECOMMENDATION</span>}>{snapshot ? <><KeyValueGrid entries={[["Session", text(snapshot.session_id)], ["Blockers", machineItems(snapshot.blockers)]]} /><div className="scroll"><table><thead><tr><th>Instrument</th><th>Expected net</th><th>Uncertainty</th><th>Cash</th><th>Action</th><th>Exposure</th></tr></thead><tbody>{ensembles.map((item, index) => { const decision = decisions[index]; const portfolio = portfolios[index]; return <tr key={text(item.instrument_id)}><td>{text(item.instrument_id)}</td><td>{decimal(item.expected_net_return)}</td><td>{decimal(item.uncertainty)}</td><td>{decimal(item.cash_weight)}</td><td>{text(decision?.action)}</td><td>{decimal(decision?.desired_exposure)}</td></tr>; })}</tbody></table></div></> : <Empty title="No retained ensemble run" text="The local demo builds immutable evidence from retained Strategy Intelligence artifacts only." />}</Panel></>;
}

function Securities() {
  const [instruments, setInstruments] = useState<ApiRecord[]>([]);
  const [batches, setBatches] = useState<ApiRecord[]>([]);
  const [actions, setActions] = useState<ApiRecord[]>([]);
  const [specifications, setSpecifications] = useState<ApiRecord[]>([]);
  const [runs, setRuns] = useState<ApiRecord[]>([]);
  const [signals, setSignals] = useState<ApiRecord[]>([]);
  const [reports, setReports] = useState<ApiRecord[]>([]);
  const [canonicalBaselines, setCanonicalBaselines] = useState<ApiRecord[]>([]);
  const [baselineRuns, setBaselineRuns] = useState<ApiRecord[]>([]);
  const [baselineScorecards, setBaselineScorecards] = useState<ApiRecord[]>([]);
  const [simulatorReports, setSimulatorReports] = useState<ApiRecord[]>([]);
  const [simulatorStatus, setSimulatorStatus] = useState<ApiRecord | null>(null);
  const [realProvider, setRealProvider] = useState<ApiRecord | null>(null);
  const [realUniverses, setRealUniverses] = useState<ApiRecord[]>([]);
  const [realOperations, setRealOperations] = useState<ApiRecord[]>([]);
  const [realQuality, setRealQuality] = useState<ApiRecord[]>([]);
  const [realValidations, setRealValidations] = useState<ApiRecord[]>([]);
  const [realForwardPlans, setRealForwardPlans] = useState<ApiRecord[]>([]);
  const [genuineDatasets, setGenuineDatasets] = useState<ApiRecord[]>([]);
  const [simulatorDetail, setSimulatorDetail] = useState<ApiRecord | null>(null);
  const [deployments, setDeployments] = useState<ApiRecord[]>([]);
  const [paper, setPaper] = useState<ApiRecord | null>(null);
  const [managedSource, setManagedSource] = useState("incoming/securities.jsonl");
  const [datasetName, setDatasetName] = useState("Managed securities evidence");
  const [campaignId, setCampaignId] = useState("");
  const [strategyId, setStrategyId] = useState("");
  const [batchId, setBatchId] = useState("");
  const [deploymentId, setDeploymentId] = useState("");
  const [researchRunId, setResearchRunId] = useState("");
  const [trialId, setTrialId] = useState("");
  const [paperRunId, setPaperRunId] = useState("");
  const [positionId, setPositionId] = useState("");
  const [exitReason, setExitReason] = useState("OPERATOR_EXIT");
  const [asOf, setAsOf] = useState(dateInputValue(new Date()));
  const [start, setStart] = useState("2024-01-01");
  const [end, setEnd] = useState("2026-12-31");
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [
        instrumentValues,
        batchValues,
        specificationValues,
        runValues,
        reportValues,
        deploymentValues,
        canonicalBaselineValues,
        baselineRunValues,
        baselineScorecardValues,
        simulatorReportValues,
        simulatorStatusValue,
        realProviderValue,
        realUniverseValues,
        realOperationValues,
        realQualityValues,
        realValidationValues,
        realForwardPlanValues,
        genuineDatasetValues,
      ] = await Promise.all([
        api<{ items: ApiRecord[] }>("/api/v1/securities/instruments"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/data-batches"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/specifications"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/research-runs"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/conformance"),
        api<{ items: ApiRecord[] }>("/api/v1/strategy-lab/deployments"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/baselines"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/baseline-runs"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/baseline-scorecards"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/simulator-conformance"),
        api<ApiRecord>("/api/v1/securities/simulator-status"),
        api<ApiRecord>("/api/v1/securities/real-data/provider"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/real-data/universes"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/real-data/fetch-operations"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/real-data/quality"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/real-data/historical-validations"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/real-data/forward-plans"),
        api<{ items: ApiRecord[] }>("/api/v1/securities/genuine-strategy-datasets"),
      ]);
      const nextBatches = asItems(batchValues);
      const nextRuns = asItems(runValues);
      const nextActions = (
        await Promise.all(
          nextBatches.map((item) =>
            api<{ items: ApiRecord[] }>(
              `/api/v1/securities/data-batches/${text(item.batch_id)}/corporate-actions`,
            ),
          ),
        )
      ).flatMap((item) => asItems(item));
      const nextSignals = (
        await Promise.all(
          nextRuns.map((item) =>
            api<{ items: ApiRecord[] }>(
              `/api/v1/securities/research-runs/${text(item.research_run_id)}/signals`,
            ),
          ),
        )
      ).flatMap((item) => asItems(item));
      const nextSpecifications = asItems(specificationValues);
      const nextDeployments = asItems(deploymentValues).filter(
        (item) => text(item.state) === "ACTIVE",
      );
      const nextPaper = campaignId
        ? await api<ApiRecord>(
            `/api/v1/securities/paper?campaign_id=${encodeURIComponent(campaignId)}`,
          )
        : null;
      setInstruments(asItems(instrumentValues));
      setBatches(nextBatches);
      setActions(nextActions);
      setSpecifications(nextSpecifications);
      setRuns(nextRuns);
      setSignals(nextSignals);
      setReports(asItems(reportValues));
      setCanonicalBaselines(asItems(canonicalBaselineValues));
      setBaselineRuns(asItems(baselineRunValues));
      setBaselineScorecards(asItems(baselineScorecardValues));
      setSimulatorReports(asItems(simulatorReportValues));
      setSimulatorStatus(simulatorStatusValue);
      setRealProvider(realProviderValue);
      setRealUniverses(asItems(realUniverseValues));
      setRealOperations(asItems(realOperationValues));
      setRealQuality(asItems(realQualityValues));
      setRealValidations(asItems(realValidationValues));
      setRealForwardPlans(asItems(realForwardPlanValues));
      setGenuineDatasets(asItems(genuineDatasetValues));
      setDeployments(nextDeployments);
      setPaper(nextPaper);
      setBatchId((current) => current || text(nextBatches[0]?.batch_id, ""));
      setStrategyId(
        (current) => current || text(nextSpecifications[0]?.strategy_id, ""),
      );
      setDeploymentId(
        (current) => current || text(nextDeployments[0]?.deployment_id, ""),
      );
      setResearchRunId(
        (current) => current || text(nextRuns[0]?.research_run_id, ""),
      );
      const nextPaperRuns = records(nextPaper?.runs);
      const nextPositions = records(nextPaper?.positions).filter(
        (item) => text(item.state) !== "CLOSED",
      );
      setPaperRunId(
        (current) => current || text(nextPaperRuns[0]?.run_id, ""),
      );
      setPositionId(
        (current) => current || text(nextPositions[0]?.position_id, ""),
      );
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }, [campaignId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const execute = async (path: string, body: ApiRecord) => {
    setPending(true);
    try {
      await mutate(path, body);
      await refresh();
    } catch (failure) {
      setError(failure);
    } finally {
      setPending(false);
    }
  };

  const inspectSimulatorReport = async (reportId: string) => {
    try {
      setSimulatorDetail(
        await api<ApiRecord>(
          `/api/v1/securities/simulator-conformance/${encodeURIComponent(reportId)}`,
        ),
      );
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  };

  const universe = instruments.map((item) => text(item.instrument_id));
  const benchmark = universe[0] ?? "";
  const researchBody = {
    campaign_id: campaignId,
    strategy_id: strategyId,
    data_batch_id: batchId,
    universe,
    start,
    end,
    decision_cutoff: new Date().toISOString(),
    benchmark_instrument_id: benchmark,
  };
  const paperStatus = record(paper?.status);
  const paperRuns = records(paper?.runs);
  const trials = records(paper?.trials);
  const policies = records(paper?.policies);
  const orders = records(paper?.orders);
  const fills = records(paper?.fills);
  const positions = records(paper?.positions);
  const marks = records(paper?.marks);
  const actionApplications = records(paper?.corporate_actions);
  const journals = records(paper?.journals);
  const paperConformance = records(paper?.conformance);
  const selectedSimulatorReport = record(simulatorDetail?.report);
  const selectedReference = record(simulatorDetail?.reference);
  const selectedComparison = record(simulatorDetail?.comparison);
  const selectedRequest = record(selectedReference?.request);
  const selectedReferenceResult = record(selectedReference?.result);
  const selectedComparisonResult = record(selectedComparison?.result);
  const fillsByOrder = new Map(
    fills.map((item) => [text(item.order_id), item]),
  );
  const moment = new Date(asOf).toISOString();

  return (
    <>
      <div className="section-head">
        <div>
          <p className="eyebrow">SECURITIES RESEARCH AND GOVERNED PAPER</p>
          <h1>Securities</h1>
          <p className="muted">
            Point-in-time research and bounded next-bar simulation over managed
            continuous-market evidence.
          </p>
        </div>
        <button onClick={() => void refresh()} disabled={pending}>
          Refresh
        </button>
      </div>
      <div className="notice neutral">
        <strong>SECURITIES PAPER — SIMULATED CAPITAL ONLY</strong>
        <span>NO BROKER OR LIVE MARKET AUTHORITY</span>
      </div>
      <div className="notice neutral">
        <strong>EXTERNAL ENGINE OUTPUT IS EVIDENCE, NOT APEX AUTHORITY</strong>
      </div>
      <div className="notice neutral">
        <strong>CANONICAL BASELINE — TRANSPARENT RESEARCH SPECIFICATION</strong>
        <span>BASELINES ARE RESEARCH BENCHMARKS, NOT PERFORMANCE PROMISES</span>
      </div>
      <div className="notice neutral">
        <strong>REAL MARKET DATA DOES NOT IMPLY A REAL TRADING EDGE</strong>
        <span>IEX DATA IS SINGLE-VENUE COVERAGE, NOT THE FULL CONSOLIDATED MARKET</span>
      </div>
      <ErrorNotice error={error} />
      <section className="metrics">
        <Metric label="Instruments" value={instruments.length} />
        <Metric label="Research runs" value={runs.length} />
        <Metric label="Canonical baselines" value={canonicalBaselines.length} />
        <Metric label="Simulator trials" value={baselineRuns.length} />
        <Metric label="PAPER runs" value={paperRuns.length} />
        <Metric
          label="Open positions"
          value={text(paperStatus?.open_position_count, "0")}
        />
        <Metric label="Simulated cash" value={decimal(paperStatus?.cash ?? "0")} />
        <Metric label="Net P&L" value={decimal(paperStatus?.net_pnl ?? "0")} />
        <Metric label="Real-data universes" value={realUniverses.length} />
        <Metric label="Provider fetches" value={realOperations.length} />
        <Metric label="Quality reports" value={realQuality.length} />
        <Metric label="Historical validations" value={realValidations.length} />
        <Metric label="Forward plans" value={realForwardPlans.length} />
        <Metric label="Genuine direction datasets" value={genuineDatasets.length} />
      </section>

      <Panel title="Real-data provider boundary">
        <KeyValueGrid entries={[
          ["Provider", text(record(realProvider?.configuration)?.provider, "Not configured")],
          ["Feed", text(record(realProvider?.configuration)?.feed, "IEX only")],
          ["Credentials available", realProvider?.credential_availability_only === null || realProvider?.credential_availability_only === undefined ? "Not checked" : realProvider.credential_availability_only === true ? "Available" : "Not available"],
          ["Latest capability", text(record(realProvider?.latest_capability)?.state, "Not checked")],
          ["Financial authority", "None"],
        ]} />
        <p className="muted">Provider credentials are never shown here. Historical fetches remain raw evidence until a retained exchange calendar, frozen universe, and quality evaluation make canonical research eligibility explicit.</p>
      </Panel>

      <Panel title="Historical validation evidence">
        <div className="scroll"><table><thead><tr><th>Scope</th><th>Claim</th><th>State</th><th>Recommendation</th><th>Reasons</th></tr></thead><tbody>{realValidations.map((item) => <tr key={text(item.validation_run_id)}><td>{text(item.market_data_batch_id)}<small>{text(item.start_at)} — {text(item.end_at)}</small></td><td>{text(item.historical_claim_classification)}</td><td><span className={stateClass(item.state)}>{text(item.state)}</span></td><td>{text(item.recommendation)}</td><td>{machineItems(item.reason_codes)}</td></tr>)}</tbody></table></div>
        {!realValidations.length && <p className="disabled-explanation">No real-data historical validation has been retained. Raw evidence, calendar coverage, frozen universe, and quality must be explicit before baseline or model research is considered.</p>}
      </Panel>

      <Panel title="Forward validation plans">
        <div className="scroll"><table><thead><tr><th>Plan</th><th>Universe</th><th>Horizon</th><th>State</th><th>Watermark</th></tr></thead><tbody>{realForwardPlans.map((item) => <tr key={text(item.plan_id)}><td>{text(item.feature_schema_id)}</td><td>{text(item.universe_id)}</td><td>{text(item.prediction_horizon_sessions)} sessions</td><td><span className={stateClass(item.state)}>{text(item.state)}</span></td><td>{text(item.current_watermark, "None")}</td></tr>)}</tbody></table></div>
        {!realForwardPlans.length && <p className="disabled-explanation">Forward plans are DRAFT by default. There is no credential field, automatic activation, prediction, PAPER, or LIVE control in this console.</p>}
      </Panel>

      <Panel title="Genuine Strategy Lab daily-direction datasets">
        <div className="scroll"><table><thead><tr><th>Scope</th><th>Rows / groups / instruments</th><th>Label balance</th><th>Holdout</th><th>Integrity lineage</th></tr></thead><tbody>{genuineDatasets.map((item) => <tr key={text(item.dataset_id)}><td>{text(item.raw_batch_id)}<small>RAW → {text(item.derived_batch_id)}</small></td><td>{text(item.row_count)} / {text(item.group_count)} / {text(item.instrument_count)}</td><td>YES {text(item.positive_label_count)} · NO {text(item.negative_label_count)}</td><td>{list(item.holdout_group_ids).length} complete sessions</td><td>{list(item.baseline_scorecard_ids).length} scorecards · {list(item.simulator_conformance_ids).length} reports</td></tr>)}</tbody></table></div>
        {!genuineDatasets.length && <p className="disabled-explanation">No immutable genuine daily-direction dataset is retained. Building one is a local research action only; it never opens an experiment, candidate, deployment, PAPER run, or financial record.</p>}
      </Panel>

      <Panel title="Managed data and SHADOW controls">
        <div className="form-grid">
          <label>
            Managed source
            <input
              value={managedSource}
              onChange={(event) => setManagedSource(event.currentTarget.value)}
              disabled={pending}
            />
          </label>
          <label>
            Dataset name
            <input
              value={datasetName}
              onChange={(event) => setDatasetName(event.currentTarget.value)}
              disabled={pending}
            />
          </label>
          <label>
            Campaign ID
            <input
              value={campaignId}
              onChange={(event) => setCampaignId(event.currentTarget.value)}
              disabled={pending}
            />
          </label>
          <label>
            Data batch
            <select
              value={batchId}
              onChange={(event) => setBatchId(event.currentTarget.value)}
              disabled={pending}
            >
              {batches.map((item) => (
                <option key={text(item.batch_id)} value={text(item.batch_id)}>
                  {text(item.dataset_name)} · {text(item.batch_id)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Baseline specification
            <select
              value={strategyId}
              onChange={(event) => setStrategyId(event.currentTarget.value)}
              disabled={pending}
            >
              {specifications
                .filter((item) => text(item.kind) !== "STRATEGY_LAB_MODEL")
                .map((item) => (
                  <option
                    key={text(item.specification_id)}
                    value={text(item.strategy_id)}
                  >
                    {text(item.kind)} · {text(item.strategy_id)}
                  </option>
                ))}
            </select>
          </label>
          <label>
            ACTIVE Strategy Lab deployment
            <select
              value={deploymentId}
              onChange={(event) => setDeploymentId(event.currentTarget.value)}
              disabled={pending}
            >
              {deployments.map((item) => (
                <option
                  key={text(item.deployment_id)}
                  value={text(item.deployment_id)}
                >
                  {text(item.role)} · {text(item.deployment_id)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Start
            <input
              type="date"
              value={start}
              onChange={(event) => setStart(event.currentTarget.value)}
              disabled={pending}
            />
          </label>
          <label>
            End
            <input
              type="date"
              value={end}
              onChange={(event) => setEnd(event.currentTarget.value)}
              disabled={pending}
            />
          </label>
        </div>
        <div className="inline-action">
          <button
            onClick={() =>
              void execute("/api/v1/securities/data-batches", {
                managed_source: managedSource,
                dataset_name: datasetName,
              })
            }
            disabled={pending || !managedSource || !datasetName}
          >
            Import Managed Dataset
          </button>
          <button
            className="primary"
            onClick={() =>
              void execute("/api/v1/securities/research-runs", researchBody)
            }
            disabled={
              pending ||
              !campaignId ||
              !batchId ||
              !strategyId ||
              !universe.length
            }
          >
            Run SHADOW
          </button>
          <button
            onClick={() =>
              void execute("/api/v1/securities/strategy-lab-research-runs", {
                ...researchBody,
                strategy_id: undefined,
                deployment_id: deploymentId,
              })
            }
            disabled={
              pending ||
              !campaignId ||
              !batchId ||
              !deploymentId ||
              !universe.length
            }
          >
            Run Strategy Lab
          </button>
          <button
            onClick={() =>
              void execute("/api/v1/securities/conformance", {
                research_run_id: researchRunId,
                external_engine: "LEAN",
              })
            }
            disabled={pending || !researchRunId}
          >
            Run Research Conformance
          </button>
          <button
            onClick={() =>
              void execute("/api/v1/securities/baseline-suites", {
                campaign_id: campaignId,
                data_batch_id: batchId,
                universe,
                start,
                end,
                decision_cutoff: new Date().toISOString(),
                simulators: ["APEX_RESEARCH", "VECTORBT", "LEAN"],
              })
            }
            disabled={
              pending || !campaignId || !batchId || !universe.length
            }
          >
            Run Bounded Baseline Suite
          </button>
        </div>
      </Panel>

      <Panel
        title="Canonical baseline catalog"
        action={<span className="badge">RESEARCH BENCHMARKS</span>}
      >
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Family / version</th>
                <th>Feature and decision rules</th>
                <th>Timing</th>
                <th>Price state</th>
                <th>PAPER compatibility</th>
                <th>Benchmark / hash</th>
              </tr>
            </thead>
            <tbody>
              {canonicalBaselines.map((item) => (
                <tr key={text(item.baseline_id)}>
                  <td>{text(item.family)}<small>{text(item.specification_version)}</small></td>
                  <td>{machineItems(item.exact_feature_rules)}{machineItems(item.exact_decision_rules)}</td>
                  <td>{text(item.rebalance_policy)}<small>{text(item.holding_policy)}</small></td>
                  <td>{text(item.required_adjustment_state)}</td>
                  <td>{text(item.paper_compatibility_state)}</td>
                  <td>{text(item.benchmark_identity)}<small>{text(item.canonical_hash)}</small></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Baseline scorecards and simulator integrity">
        <p className="muted">
          Side-by-side results use one exact dataset, universe, period, cost
          policy, and canonical request. Material mismatches are integrity
          blockers, never hidden by wider tolerances.
        </p>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Baseline / simulator</th>
                <th>Gross / net return</th>
                <th>Drawdown</th>
                <th>Turnover / exposure</th>
                <th>Hit rate / cost</th>
                <th>Corporate actions</th>
                <th>Conformance</th>
              </tr>
            </thead>
            <tbody>
              {baselineScorecards.map((item) => (
                <tr key={text(item.scorecard_id)}>
                  <td>{text(item.baseline_id)}<small>{text(item.simulator_identity)}</small></td>
                  <td>{decimal(item.gross_return)} / {decimal(item.net_return)}</td>
                  <td>{decimal(item.maximum_drawdown)}</td>
                  <td>{decimal(item.turnover)} / {decimal(item.exposure)}</td>
                  <td>{decimal(item.hit_rate)} / {decimal(item.cost_total)}</td>
                  <td>{text(item.corporate_action_count)}</td>
                  <td><span className={stateClass(item.conformance_state)}>{text(item.conformance_state)}</span>{machineItems(item.integrity_warnings)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Reference / comparison</th>
                <th>Signal / transaction disagreements</th>
                <th>Return / drawdown / turnover difference</th>
                <th>Classification / reasons</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {simulatorReports.map((item) => (
                <tr key={text(item.report_id)}>
                  <td>{text(item.reference_run_id)}<small>{text(item.comparison_run_id)}</small></td>
                  <td>{text(item.direction_disagreements)} / {text(item.transaction_disagreements)}</td>
                  <td>{decimal(item.return_difference)} / {decimal(item.drawdown_difference)} / {decimal(item.turnover_difference)}</td>
                  <td><span className={stateClass(item.classification)}>{text(item.classification)}</span>{machineItems(item.reason_codes)}</td>
                  <td><button onClick={() => void inspectSimulatorReport(text(item.report_id))}>Inspect evidence</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {selectedSimulatorReport && (
          <div className="stack">
            <KeyValueGrid entries={[
              ["Canonical request hash", text(selectedRequest?.canonical_hash)],
              ["Baseline / data", `${text(selectedRequest?.baseline_id)} / ${text(selectedRequest?.data_batch_id)}`],
              ["Universe hash", text(selectedRequest?.universe_hash)],
              ["Period / cutoff", `${text(selectedRequest?.start_date)} → ${text(selectedRequest?.end_date)} / ${formatTimestamp(selectedRequest?.cutoff)}`],
              ["Timing / costs", `${text(selectedRequest?.signal_timing)} / ${decimal(selectedRequest?.spread_bps)} + ${decimal(selectedRequest?.slippage_bps)} + ${decimal(selectedRequest?.commission_bps)} bps`],
              ["Materiality policy", `${text(selectedSimulatorReport.materiality_policy_id)} / ${text(selectedSimulatorReport.materiality_policy_hash)}`],
              ["Classification", text(selectedSimulatorReport.classification)],
              ["Reason codes", machineItems(selectedSimulatorReport.reason_codes)],
            ]} />
            <p className="muted">
              Reference: {text(selectedReferenceResult?.simulator_identity)} · Comparison: {text(selectedComparisonResult?.simulator_identity)}.
              The tables are bounded views of immutable retained output.
            </p>
            <div className="scroll">
              <table>
                <thead><tr><th>Decision / instrument</th><th>Direction / score</th><th>Target weight</th><th>Source lineage</th></tr></thead>
                <tbody>{records(selectedReferenceResult?.signals).slice(0, 20).map((item) => <tr key={text(item.canonical_hash)}><td>{formatTimestamp(item.decision_at)}<small>{text(item.instrument_id)}</small></td><td>{text(item.direction)} / {decimal(item.score)}</td><td>{decimal(item.target_weight)}</td><td>{machineItems(item.source_bar_ids)}</td></tr>)}</tbody>
              </table>
            </div>
            <div className="scroll">
              <table>
                <thead><tr><th>Transaction / instrument</th><th>Prior / target weight</th><th>Modeled cost</th><th>Execution price</th></tr></thead>
                <tbody>{records(selectedReferenceResult?.transactions).slice(0, 20).map((item) => <tr key={text(item.canonical_hash)}><td>{formatTimestamp(item.transaction_at)}<small>{text(item.instrument_id)}</small></td><td>{decimal(item.prior_weight)} / {decimal(item.target_weight)}</td><td>{decimal(item.modeled_cost)}</td><td>{decimal(item.execution_price)}</td></tr>)}</tbody>
              </table>
            </div>
            <div className="scroll">
              <table>
                <thead><tr><th>Equity checkpoint</th><th>Gross / net equity</th><th>Cash / exposure</th><th>Drawdown</th></tr></thead>
                <tbody>{records(selectedReferenceResult?.equity_curve).slice(-5).map((item) => <tr key={text(item.canonical_hash)}><td>{formatTimestamp(item.session_at)}</td><td>{decimal(item.gross_equity)} / {decimal(item.net_equity)}</td><td>{decimal(item.cash_balance)} / {decimal(item.gross_exposure)}</td><td>{decimal(item.drawdown)}</td></tr>)}</tbody>
              </table>
            </div>
          </div>
        )}
        <p className="disabled-explanation">
          {baselineRuns.length} immutable simulator attempts · {simulatorReports.length} three-layer comparisons · Runtime status: {machineItems(simulatorStatus?.items)}
        </p>
      </Panel>

      <Panel
        title="Governed Securities PAPER"
        action={<span className="badge">SIMULATED CAPITAL ONLY</span>}
      >
        <div className="form-grid">
          <label>
            Active trial authorization
            <input
              value={trialId}
              onChange={(event) => setTrialId(event.currentTarget.value)}
              placeholder="Exact approved trial ID"
              disabled={pending}
            />
          </label>
          <label>
            As-of time
            <input
              type="datetime-local"
              value={asOf}
              onChange={(event) => setAsOf(event.currentTarget.value)}
              disabled={pending}
            />
          </label>
          <label>
            Open Position
            <select
              value={positionId}
              onChange={(event) => setPositionId(event.currentTarget.value)}
              disabled={pending}
            >
              {positions
                .filter((item) => text(item.state) !== "CLOSED")
                .map((item) => (
                  <option
                    key={text(item.position_id)}
                    value={text(item.position_id)}
                  >
                    {text(item.current_symbol)} · {text(item.open_quantity)}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Exit reason
            <select
              value={exitReason}
              onChange={(event) => setExitReason(event.currentTarget.value)}
              disabled={pending}
            >
              {[
                "OPERATOR_EXIT",
                "RISK_REDUCTION",
                "FIXED_HORIZON",
                "STRATEGY_FLAT",
                "TERMINAL_RUN_DATE",
                "CAMPAIGN_KILL",
                "DELISTING",
              ].map((reason) => (
                <option key={reason}>{reason}</option>
              ))}
            </select>
          </label>
          <label>
            PAPER run for conformance
            <select
              value={paperRunId}
              onChange={(event) => setPaperRunId(event.currentTarget.value)}
              disabled={pending}
            >
              {paperRuns.map((item) => (
                <option key={text(item.run_id)} value={text(item.run_id)}>
                  {text(item.state)} · {text(item.run_id)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="inline-action">
          <button
            className="primary"
            onClick={() =>
              void execute("/api/v1/securities/paper/runs", {
                campaign_id: campaignId,
                trial_id: trialId,
                strategy_id: strategyId,
                data_batch_id: batchId,
                start,
                end,
              })
            }
            disabled={
              pending ||
              !campaignId ||
              !trialId ||
              !strategyId ||
              !batchId
            }
          >
            Run Approved PAPER Trial
          </button>
          <button
            onClick={() =>
              void execute(
                `/api/v1/securities/paper/campaigns/${campaignId}/marks`,
                { as_of: moment },
              )
            }
            disabled={pending || !campaignId}
          >
            Mark Positions
          </button>
          <button
            onClick={() =>
              void execute(
                `/api/v1/securities/paper/campaigns/${campaignId}/corporate-actions`,
                { as_of: moment },
              )
            }
            disabled={pending || !campaignId}
          >
            Apply Corporate Actions
          </button>
          <button
            className="danger"
            onClick={() =>
              void execute(
                `/api/v1/securities/paper/positions/${positionId}/exit`,
                { as_of: moment, reason: exitReason },
              )
            }
            disabled={pending || !positionId}
          >
            Exit Position
          </button>
          <button
            onClick={() =>
              void execute("/api/v1/securities/paper/conformance", {
                paper_run_id: paperRunId,
                external_engine: "LEAN",
              })
            }
            disabled={pending || !paperRunId}
          >
            Run PAPER Conformance
          </button>
        </div>
        <p className="disabled-explanation">
          Trial pause, resume, revoke, and completion remain governed by the
          existing Evidence Reviews controls. New openings require global PAPER
          authority and an exact ACTIVE Campaign trial; marks, dividends, and
          risk-reducing exits remain available after suspension.
        </p>
      </Panel>

      <Panel title="PAPER trials and execution policy">
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Trial / state</th>
                <th>Strategy</th>
                <th>Limits and usage</th>
                <th>Execution policy</th>
              </tr>
            </thead>
            <tbody>
              {trials.map((item) => {
                const run = paperRuns.find(
                  (value) =>
                    text(value.trial_authorization_id) ===
                    text(item.authorization_id),
                );
                const policy = policies.find(
                  (value) =>
                    text(value.policy_id) === text(run?.execution_policy_id),
                );
                return (
                  <tr key={text(item.authorization_id)}>
                    <td>
                      <span className={stateClass(item.state)}>
                        {text(item.state)}
                      </span>
                      <small>{text(item.authorization_id)}</small>
                    </td>
                    <td>
                      {text(item.strategy_id)}
                      <small>{text(item.strategy_configuration_hash)}</small>
                    </td>
                    <td>
                      {text(item.maximum_paper_orders)} orders ·{" "}
                      {text(item.maximum_simultaneous_open_positions)} positions
                      <small>
                        cumulative risk {text(item.maximum_cumulative_authorized_risk)}
                      </small>
                    </td>
                    <td>
                      {text(policy?.execution_model)}
                      <small>{text(policy?.canonical_hash)}</small>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="PAPER Orders and fills">
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Order / lineage</th>
                <th>Timing</th>
                <th>Quantity</th>
                <th>Fill economics</th>
                <th>Reasons</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((item) => {
                const fill = fillsByOrder.get(text(item.order_id));
                return (
                  <tr key={text(item.order_id)}>
                    <td>
                      {text(item.action)} ·{" "}
                      <span className={stateClass(item.state)}>
                        {text(item.state)}
                      </span>
                      <small>{text(item.order_id)}</small>
                    </td>
                    <td>
                      {formatTimestamp(item.decision_at)}
                      <small>
                        eligible {formatTimestamp(item.earliest_fill_at)}
                      </small>
                    </td>
                    <td>
                      {text(item.requested_quantity)} requested /{" "}
                      {text(item.filled_quantity)} filled
                    </td>
                    <td>
                      {decimal(fill?.fill_price)} · spread{" "}
                      {decimal(fill?.modeled_spread)} · slippage{" "}
                      {decimal(fill?.modeled_slippage)} · fee{" "}
                      {decimal(fill?.modeled_commission)}
                    </td>
                    <td>{machineItems(fill?.reason_codes)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Positions, marks, and corporate actions">
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Instrument</th>
                <th>State / quantity</th>
                <th>Cost and mark</th>
                <th>P&L</th>
                <th>Retained evidence</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((item) => (
                <tr key={text(item.position_id)}>
                  <td>
                    {text(item.current_symbol)}
                    <small>{text(item.instrument_id)}</small>
                  </td>
                  <td>
                    <span className={stateClass(item.state)}>
                      {text(item.state)}
                    </span>
                    <small>{text(item.open_quantity)} open</small>
                  </td>
                  <td>
                    average {decimal(item.average_cost)} · last{" "}
                    {decimal(item.last_mark_price)}
                  </td>
                  <td>
                    unrealized {decimal(item.unrealized_pnl)} · realized{" "}
                    {decimal(item.realized_pnl)}
                  </td>
                  <td>
                    {
                      marks.filter(
                        (value) =>
                          text(value.position_id) === text(item.position_id),
                      ).length
                    }{" "}
                    marks ·{" "}
                    {
                      actionApplications.filter(
                        (value) =>
                          text(value.position_id) === text(item.position_id),
                      ).length
                    }{" "}
                    actions
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Ledger and exact reconciliation">
        <KeyValueGrid
          entries={[
            ["Starting/ending cash", `${text(paperRuns[0]?.starting_cash)} / ${text(paperStatus?.cash, "0")}`],
            ["Position cost", decimal(paperStatus?.position_cost ?? "0")],
            ["Sale gain/loss", decimal(paperStatus?.realized_pnl ?? "0")],
            ["Dividend income", decimal(paperStatus?.dividend_income ?? "0")],
            ["Fees", decimal(paperStatus?.fees ?? "0")],
            ["Net P&L", decimal(paperStatus?.net_pnl ?? "0")],
          ]}
        />
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Journal</th>
                <th>Type</th>
                <th>Source</th>
                <th>Time</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {journals.map((item) => (
                <tr key={text(item.journal_id)}>
                  <td>
                    <small>{text(item.journal_id)}</small>
                  </td>
                  <td>{text(item.journal_type)}</td>
                  <td>
                    {text(item.source_kind)} / {text(item.source_id)}
                  </td>
                  <td>{formatTimestamp(item.occurred_at)}</td>
                  <td>
                    <span className={stateClass(item.status)}>
                      {text(item.status)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Research and PAPER conformance">
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Report / scope</th>
                <th>Classification</th>
                <th>Compared</th>
                <th>Differences</th>
                <th>Reasons</th>
              </tr>
            </thead>
            <tbody>
              {[...reports, ...paperConformance].map((item) => (
                <tr key={text(item.report_id)}>
                  <td>
                    <small>{text(item.report_id)}</small>
                    {item.paper_run_id ? "PAPER" : "RESEARCH"}
                  </td>
                  <td>
                    <span className={stateClass(item.classification)}>
                      {text(item.classification)}
                    </span>
                  </td>
                  <td>
                    {text(
                      item.compared_signal_count ??
                        item.matching_event_count,
                    )}
                  </td>
                  <td>
                    {text(
                      item.differing_signal_count ??
                        item.differing_event_count,
                    )}
                  </td>
                  <td>{machineItems(item.reason_codes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}

function EvidenceReviewControls({ campaignId }: { campaignId: string | null }) {
  const [reviews, setReviews] = useState<ApiRecord[]>([]);
  const [snapshots, setSnapshots] = useState<ApiRecord[]>([]);
  const [trials, setTrials] = useState<ApiRecord[]>([]);
  const [criteria, setCriteria] = useState<Record<string, ApiRecord[]>>({});
  const [snapshotId, setSnapshotId] = useState("");
  const [reviewId, setReviewId] = useState("");
  const [trialId, setTrialId] = useState("");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [limits, setLimits] = useState({ policy: "", hash: "", starts: "", expires: "", cycles: "1", opportunities: "1", orders: "1", positions: "1", opportunityRisk: "0.01", strategyRisk: "0.01", cumulativeRisk: "1", loss: "1", drawdown: "1", lockup: "60", instrument: "BOUNDED_PREDICTION_MARKET", provider: "KALSHI" });
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const strategyIds = ["kalshi-orderbook-microstructure-v1", "kalshi-market-dynamics-v1"];
  const refresh = useCallback(async () => {
    try {
      const values = await Promise.all(strategyIds.flatMap((id) => [api<{ items: ApiRecord[] }>(`/api/v1/strategies/${id}/reviews`), api<{ items: ApiRecord[] }>(`/api/v1/strategies/${id}/evidence-snapshots`)]));
      const nextReviews = values.filter((_, index) => index % 2 === 0).flatMap((value) => asItems(value));
      const nextSnapshots = values.filter((_, index) => index % 2 === 1).flatMap((value) => asItems(value));
      const criteriaPairs = await Promise.all(nextSnapshots.map(async (snapshot) => [text(snapshot.snapshot_id), asItems(await api<{ items: ApiRecord[] }>(`/api/v1/evidence-snapshots/${text(snapshot.snapshot_id)}/criteria`))] as const));
      const nextTrials = campaignId ? asItems(await api<{ items: ApiRecord[] }>(`/api/v1/campaigns/${campaignId}/paper-trials`)) : [];
      setReviews(nextReviews); setSnapshots(nextSnapshots); setCriteria(Object.fromEntries(criteriaPairs)); setTrials(nextTrials);
      setSnapshotId((current) => current || text(nextSnapshots[0]?.snapshot_id));
      setReviewId((current) => current || text(nextReviews[0]?.review_id));
      setTrialId((current) => current || text(nextTrials[0]?.authorization_id));
      setError(null);
    } catch (failure) { setError(failure); }
  }, [campaignId]);
  useEffect(() => { void refresh(); }, [refresh]);
  const selectedSnapshot = snapshots.find((item) => text(item.snapshot_id) === snapshotId);
  const selectedReview = reviews.find((item) => text(item.review_id) === reviewId);
  const hardPass = selectedSnapshot ? (criteria[snapshotId] ?? []).filter((item) => item.hard_gate === true).every((item) => text(item.state) === "PASS") : false;
  const execute = async (path: string, body: ApiRecord) => { if (!reason.trim()) { setError(new ApiError("An operator reason is required.", "OPERATOR_REASON_REQUIRED")); return; } setPending(true); try { await mutate(path, body); await refresh(); setReason(""); setNote(""); } catch (failure) { setError(failure); } finally { setPending(false); } };
  const requestReview = () => { if (!campaignId || !selectedSnapshot) { setError(new ApiError("Choose a Campaign and frozen snapshot first.", "CAMPAIGN_AND_SNAPSHOT_REQUIRED")); return; } void execute(`/api/v1/strategies/${text(selectedSnapshot.strategy_id)}/reviews`, { snapshot_id: snapshotId, campaign_id: campaignId, expires_at: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(), operator_reason: reason, operator_note: note }); };
  const transitionReview = (action: "start" | "reject" | "defer" | "cancel") => { if (!reviewId) return; void execute(`/api/v1/strategy-reviews/${reviewId}/${action}`, { operator_reason: reason, operator_note: note }); };
  const approve = () => { if (!selectedReview || !hardPass) return; void execute(`/api/v1/strategy-reviews/${reviewId}/approve`, { operator_reason: reason, operator_note: note, confirmation_text: confirmation, paper_execution_policy_id: limits.policy, paper_execution_policy_hash: limits.hash, starts_at: limits.starts, expires_at: limits.expires, maximum_paper_cycles: Number(limits.cycles), maximum_approved_opportunities: Number(limits.opportunities), maximum_paper_orders: Number(limits.orders), maximum_simultaneous_open_positions: Number(limits.positions), maximum_per_opportunity_risk_fraction: limits.opportunityRisk, maximum_strategy_active_risk_fraction: limits.strategyRisk, maximum_cumulative_authorized_risk: limits.cumulativeRisk, maximum_cumulative_realized_paper_loss: limits.loss, maximum_paper_drawdown: limits.drawdown, maximum_capital_lockup_seconds: Number(limits.lockup), allowed_instrument_class: limits.instrument, allowed_provider: limits.provider }); };
  const transitionTrial = (action: "pause" | "resume" | "revoke" | "complete") => { if (!trialId) return; void execute(`/api/v1/paper-trials/${trialId}/${action}`, { operator_reason: reason, operator_note: note }); };
  const limitInput = (label: string, key: keyof typeof limits) => <label>{label}<input value={limits[key]} onChange={(event) => setLimits({ ...limits, [key]: event.currentTarget.value })} disabled={pending} /></label>;
  return <><Panel title="Evidence review decisions" action={<button onClick={() => void refresh()} disabled={pending}>Refresh authoritative state</button>}><p className="muted">Every local decision requires a reason and receives a fresh idempotency key. The displayed state is reloaded after completion; this form never changes authority optimistically.</p><div className="form-grid"><label>Frozen snapshot<select value={snapshotId} onChange={(event) => setSnapshotId(event.currentTarget.value)}>{snapshots.map((snapshot) => <option key={text(snapshot.snapshot_id)} value={text(snapshot.snapshot_id)}>{text(snapshot.strategy_id)} · {text(snapshot.snapshot_id)}</option>)}</select></label><label>Review<select value={reviewId} onChange={(event) => setReviewId(event.currentTarget.value)}>{reviews.map((review) => <option key={text(review.review_id)} value={text(review.review_id)}>{text(review.state)} · {text(review.review_id)}</option>)}</select></label><label>Operator reason<textarea value={reason} onChange={(event) => setReason(event.currentTarget.value)} disabled={pending} /></label><label>Operator note<textarea value={note} onChange={(event) => setNote(event.currentTarget.value)} disabled={pending} /></label></div>{selectedSnapshot && <p className={hardPass ? "notice success" : "disabled-explanation"}>Hard criteria: {(criteria[snapshotId] ?? []).filter((item) => item.hard_gate === true).map((item) => `${text(item.criterion_code)} ${text(item.state)}`).join(" · ") || "not loaded"}</p>}<div className="inline-action"><button onClick={requestReview} disabled={pending || !campaignId || !selectedSnapshot}>Request Review</button><button onClick={() => transitionReview("start")} disabled={pending || text(selectedReview?.state) !== "REQUESTED"}>Start Review</button><button onClick={() => transitionReview("reject")} disabled={pending || text(selectedReview?.state) !== "IN_REVIEW"}>Reject</button><button onClick={() => transitionReview("defer")} disabled={pending || text(selectedReview?.state) !== "IN_REVIEW"}>Defer</button><button onClick={() => transitionReview("cancel")} disabled={pending || !["REQUESTED", "IN_REVIEW"].includes(text(selectedReview?.state))}>Cancel</button></div></Panel><Panel title="Approve bounded PAPER trial"><p className="disabled-explanation">Approval remains disabled unless every hard criterion is PASS. Exact typed confirmation must name the strategy, Campaign, and snapshot.</p><div className="form-grid"><label>Exact confirmation<input value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} placeholder={selectedReview ? `APPROVE_PAPER_TRIAL:${text(selectedReview.strategy_id)}:${text(selectedReview.requested_campaign_id)}:${text(selectedReview.snapshot_id)}` : "APPROVE_PAPER_TRIAL:strategy:campaign:snapshot"} /></label>{limitInput("PaperExecutionPolicy ID", "policy")}{limitInput("PaperExecutionPolicy hash", "hash")}{limitInput("Start (ISO-8601 UTC)", "starts")}{limitInput("Expiration (ISO-8601 UTC)", "expires")}{limitInput("Maximum cycles", "cycles")}{limitInput("Maximum approved opportunities", "opportunities")}{limitInput("Maximum Orders", "orders")}{limitInput("Maximum open Positions", "positions")}{limitInput("Per-opportunity risk", "opportunityRisk")}{limitInput("Strategy active-risk", "strategyRisk")}{limitInput("Cumulative authorized risk", "cumulativeRisk")}{limitInput("Realized-loss limit", "loss")}{limitInput("Drawdown limit", "drawdown")}{limitInput("Lockup seconds", "lockup")}{limitInput("Instrument class", "instrument")}{limitInput("Provider", "provider")}</div><button className="primary" onClick={approve} disabled={pending || !hardPass || text(selectedReview?.state) !== "IN_REVIEW"}>Approve Bounded PAPER Trial</button></Panel><Panel title="Paper trial controls"><div className="form-grid"><label>Trial<select value={trialId} onChange={(event) => setTrialId(event.currentTarget.value)}>{trials.map((trial) => <option key={text(trial.authorization_id)} value={text(trial.authorization_id)}>{text(trial.state)} · {text(trial.authorization_id)}</option>)}</select></label></div>{trials.map((trial) => <KeyValueGrid key={text(trial.authorization_id)} entries={[["Evidence review", text(trial.review_id)], ["Authority", text(trial.strategy_id)], ["Usage", `${text(record(trial.usage)?.completed_cycles)} cycles · ${text(record(trial.usage)?.approved_opportunities)} opportunities · ${text(record(trial.usage)?.paper_orders)} Orders · ${text(record(trial.usage)?.open_positions)} Positions`], ["Result / drawdown", `${decimal(record(trial.usage)?.cumulative_realized_result)} / ${decimal(record(trial.usage)?.paper_drawdown)}`], ["Binding reason", text(trial.suspension_reason, text(trial.revocation_reason, "—"))]]} />)}<div className="inline-action"><button onClick={() => transitionTrial("pause")} disabled={pending || text(trials.find((trial) => text(trial.authorization_id) === trialId)?.state) !== "ACTIVE"}>Pause</button><button onClick={() => transitionTrial("resume")} disabled={pending || text(trials.find((trial) => text(trial.authorization_id) === trialId)?.state) !== "PAUSED"}>Resume</button><button onClick={() => transitionTrial("revoke")} disabled={pending || !trialId}>Revoke</button><button onClick={() => transitionTrial("complete")} disabled={pending || !trialId}>Complete</button></div>{!campaignId && <p className="disabled-explanation">Select a Campaign to inspect or operate only its campaign-specific trial authority.</p>}</Panel><ErrorNotice error={error} /></>;
}

function allocationSummary(value: unknown, names: Map<string, string>): string {
  if (!Array.isArray(value)) return "—";
  const allocations = value.flatMap((item) => Array.isArray(item) && item.length === 2 ? [[text(item[0]), text(item[1])] as const] : []);
  return allocations.length
    ? allocations.map(([id, weight]) => `${names.get(id) ?? id}: ${weight}`).join(" · ")
    : "No allocation retained";
}

function SecuritiesOperationsView({ embedded = false }: { embedded?: boolean }) {
  const [items, setItems] = useState<ApiRecord[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [runtime, setRuntime] = useState<ApiRecord>({});
  const [error, setError] = useState<unknown>(null);
  const [selectedPlanId, setSelectedPlanId] = useState("");
  const [reason, setReason] = useState("Observation-only plan lifecycle control.");
  const [confirmation, setConfirmation] = useState("");
  const [pending, setPending] = useState(false);
  const refresh = useCallback(async () => {
    try {
      const value = await api<{ items: ApiRecord[]; warnings?: string[]; runtime?: ApiRecord }>("/api/v1/securities/sustained-shadow-operations");
      setItems(asItems(value)); setWarnings(list(value.warnings)); setRuntime(record(value.runtime) ?? {}); setError(null);
    } catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { setSelectedPlanId((value) => value || text(record(items[0]?.plan)?.plan_id, "")); }, [items]);
  const control = async (action: "activate" | "pause" | "resume" | "revoke") => {
    if (!selectedPlanId) return;
    setPending(true); setError(null);
    try {
      await mutate(`/api/v1/securities/sustained-shadow-plans/${selectedPlanId}/${action}`, action === "pause" ? { operator_reason: reason } : { confirm_plan_id: confirmation });
      await refresh();
    } catch (failure) { setError(failure); } finally { setPending(false); }
  };
  return <><div className="section-head"><div><p className="eyebrow">SECURITIES SHADOW — NO CAPITAL RESERVED</p><h1>{embedded ? "Securities SHADOW operations" : "Real SHADOW Operations"}</h1><p className="muted">Retained operational state only. The local scheduler and daily worker create research observations and cannot create Orders, Positions, reservations, or broker requests.</p></div><button onClick={() => void refresh()}>Refresh</button></div><ErrorNotice error={error} /><div className="notice neutral">{warnings.join(" · ") || "OBSERVATION_ONLY · NO PAPER, BROKER, OR LIVE AUTHORITY"}</div><Panel title="Local runtime state" action={<span className="badge">LOOPBACK ONLY</span>}><KeyValueGrid entries={[["Scheduler PID", text(runtime.scheduler_pid, "not running")], ["Bound plan", text(runtime.plan_id, "none")], ["Started", formatTimestamp(runtime.started_at)], ["Database owned by runtime", text(runtime.database_owned, "false")], ["Financial authority", "NONE"]]} /></Panel><Panel title="Immutable plan lineage" action={<span className="badge">READ ONLY</span>}><div className="scroll"><table><thead><tr><th>Plan / state</th><th>Parent / authority</th><th>Watermark</th><th>Predictions / outcomes</th><th>Last operation</th><th>Frozen blockers</th></tr></thead><tbody>{items.map((item) => { const plan = record(item.plan); const report = record(item.report); const latestJob = records(item.operation_jobs)[0]; return <tr key={text(plan?.plan_id)}><td><small>{text(plan?.plan_id)}</small><br /><span className={stateClass(plan?.state)}>{text(plan?.state)}</span></td><td><small>{text(item.parent_plan_id, "root plan")}</small><br />{text(plan?.authority)}</td><td><small>{text(report?.watermark, text(plan?.watermark, "none"))}</small></td><td>{text(report?.prediction_count, "0")} predictions<br /><small>{text(report?.resolved_outcome_count, "0")} resolved / {text(report?.unresolved_outcome_count, "0")} unresolved</small></td><td>{text(latestJob?.state, "no job")}<small>attempt {text(latestJob?.attempt, "0")} · {text(latestJob?.result_code, "—")}</small></td><td>{machineItems(plan?.frozen_blockers ?? report?.frozen_blockers)}</td></tr>; })}</tbody></table></div>{!items.length && <Empty title="No retained Securities SHADOW plans" text="The console has no plan evidence to display. It cannot create or activate a plan from this screen." />}</Panel><Panel title="Observation-plan controls" action={<span className="badge">NO FINANCIAL AUTHORITY</span>}><div className="form-grid"><label>Plan<select value={selectedPlanId} onChange={(event) => setSelectedPlanId(event.currentTarget.value)} disabled={pending}>{items.map((item) => { const plan = record(item.plan); return <option key={text(plan?.plan_id)} value={text(plan?.plan_id)}>{text(plan?.state)} · {text(plan?.plan_id)}</option>; })}</select></label><label>Pause reason<textarea value={reason} onChange={(event) => setReason(event.currentTarget.value)} disabled={pending} /></label><label>Exact plan ID confirmation<input value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} disabled={pending} /></label></div><div className="inline-action"><button onClick={() => void control("activate")} disabled={pending || !selectedPlanId || confirmation !== selectedPlanId}>Activate Draft</button><button onClick={() => void control("pause")} disabled={pending || !selectedPlanId || !reason}>Pause</button><button onClick={() => void control("resume")} disabled={pending || !selectedPlanId || confirmation !== selectedPlanId}>Resume</button><button className="danger" onClick={() => void control("revoke")} disabled={pending || !selectedPlanId || confirmation !== selectedPlanId}>Revoke</button></div><p className="disabled-explanation">These controls only change observation-plan lifecycle state. They cannot start a broker, create a PAPER trial, reserve capital, submit an Order, or create a Position.</p></Panel><Panel title="Operations boundary"><KeyValueGrid entries={[["Capture", "One bounded retained-evidence pass per eligible XNYS session"], ["Outcomes", "Unresolved until their actual five-session horizon passes"], ["Financial authority", "NONE"]]} /></Panel></>;
}

function PortfolioIntelligenceView({ embedded = false }: { embedded?: boolean }) {
  const [items, setItems] = useState<ApiRecord[]>([]);
  const [warning, setWarning] = useState("");
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    try {
      const value = await api<{ items: ApiRecord[]; warning?: string; financial_authority?: string }>("/api/v1/portfolio-intelligence");
      setItems(asItems(value)); setWarning(`${text(value.warning, "PORTFOLIO INTELLIGENCE IS RESEARCH ONLY")} · financial authority ${text(value.financial_authority, "NONE")}`); setError(null);
    } catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const current = items[items.length - 1];
  const run = record(current?.run);
  const center = record(current?.command_center);
  const opportunities = records(run?.opportunities);
  const names = new Map(opportunities.map((item) => [text(item.opportunity_id), `${text(item.instrument_id)} (${text(item.source)})`]));
  const ranked = records(center?.ranked_opportunities);
  const scenarios = record(center?.scenarios);
  const dependencies = records(center?.dependency_matrix);
  const instructions = records(center?.non_executable_instructions);
  return <><div className="section-head"><div><p className="eyebrow">HISTORICAL/FORWARD-EARLY RESEARCH CAPITAL PLAN</p><h1>{embedded ? "Portfolio Intelligence" : "Portfolio Intelligence"}</h1><p className="muted">Research attractiveness is shown separately from authority. Cash and abstention are valid retained decisions.</p></div><button onClick={() => void refresh()}>Refresh</button></div><ErrorNotice error={error} /><div className="notice neutral">{warning || "PORTFOLIO INTELLIGENCE IS RESEARCH ONLY · RESEARCH ATTRACTIVENESS IS NOT CAPITAL AUTHORITY"}</div>{run && center ? <><Panel title="Research plan versus permitted plan" action={<span className="badge">NOT EXECUTABLE</span>}><KeyValueGrid entries={[["Run", <small>{text(run.run_id)}</small>], ["Research-desired allocation", allocationSummary(center.research_desired_allocations, names)], ["Authority-permitted allocation", allocationSummary(center.authority_permitted_allocations, names)], ["Canonical hash", <small>{text(run.canonical_hash)}</small>], ["Financial authority", text(center.financial_authority, "NONE")]]} /></Panel><Panel title="Global opportunity ranking"><div className="scroll"><table><thead><tr><th>Instrument / source</th><th>Research utility</th><th>Authority-adjusted utility</th><th>Maturity / ceiling</th><th>Blockers</th></tr></thead><tbody>{ranked.map((item) => <tr key={text(item.opportunity_id)}><td>{text(item.instrument)}<small>{text(item.source)}</small></td><td>{decimal(item.research_utility)}</td><td>{decimal(item.authority_adjusted_utility)}</td><td>{text(item.maturity)}<small>{text(item.authority_ceiling)}</small></td><td>{machineItems(item.blockers)}</td></tr>)}</tbody></table></div></Panel><Panel title="Dependency evidence"><div className="scroll"><table><thead><tr><th>Relationship</th><th>Conservative penalty</th><th>Evidence</th></tr></thead><tbody>{dependencies.map((item) => <tr key={text(item.dependency_id)}><td><small>{text(item.left_opportunity_id)} → {text(item.right_opportunity_id)}</small></td><td>{decimal(item.dependency_penalty)}</td><td>{text(item.reason_code)}</td></tr>)}</tbody></table></div><p className="disabled-explanation">Unknown cross-opportunity relationships receive a conservative retained penalty; this is not a fabricated correlation claim.</p></Panel><Panel title="Conservative scenario allocations"><div className="scroll"><table><thead><tr><th>Scenario</th><th>Authority-permitted allocation</th></tr></thead><tbody>{Object.entries(scenarios ?? {}).map(([scenario, allocation]) => <tr key={scenario}><td>{scenario}</td><td>{allocationSummary(allocation, names)}</td></tr>)}</tbody></table></div><p className="disabled-explanation">Stress scenarios are retained research outputs only. They neither reserve capital nor instruct a broker.</p></Panel><Panel title="Future PAPER handoff evidence" action={<span className="badge">RESEARCH ONLY</span>}><div className="scroll"><table><thead><tr><th>Opportunity</th><th>Desired / permitted</th><th>Maturity / ceiling</th><th>State</th></tr></thead><tbody>{instructions.map((item) => <tr key={text(item.instruction_id)}><td><small>{text(item.opportunity_id)}</small></td><td>{decimal(item.desired_allocation)} / {decimal(item.permitted_allocation)}</td><td>{text(item.maturity)} / {text(item.authority_ceiling)}</td><td>{text(item.state)} · executable {text(item.executable)}</td></tr>)}</tbody></table></div></Panel></> : <Empty title="No retained portfolio-intelligence run" text="No current portfolio report is available. This screen does not initiate model evaluation, provider collection, or financial allocation." />}</>;
}

function GenuineAlpacaPaperPanel() {
  const [instructionId, setInstructionId] = useState("");
  const [status, setStatus] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const load = async () => {
    if (!instructionId) return;
    try {
      setStatus(await api<ApiRecord>(`/api/v1/external-paper/alpaca/instructions/${encodeURIComponent(instructionId)}/status`));
      setError(null);
    } catch (failure) { setError(failure); }
  };
  return <Panel title="Genuine Alpaca Paper operations"><p className="muted">Engineering acceptance evidence only — excluded from strategy performance. LIVE authority remains NONE.</p><div className="form-grid"><label>Exact instruction ID<input value={instructionId} onChange={(event) => setInstructionId(event.target.value)} placeholder="external_paper_instruction_…" /></label></div><div className="button-row"><button disabled={!instructionId} onClick={() => void load()}>Status</button><button disabled>Sync uses the External PAPER workflow</button><button disabled>Validate retained fill</button><button disabled>Apply validated fill</button><button disabled>Reconcile</button><button disabled>Cancel requires exact confirmation</button><button disabled>Close requires risk-reducing instruction</button></div><ErrorNotice error={error} />{status && <KeyValueGrid entries={[["Broker Order", text(status.broker_order_id)], ["Broker state", text(status.latest_broker_state)], ["Kill state", text(status.kill_state)], ["Validated fills", text(records(status.validated_fills).length)], ["Reconciliation", text(records(status.reconciliations).length)], ["LIVE authority", text(status.live_authority)]]} />}</Panel>;
}

function ApexCommandCenter() {
  return <><div className="notice neutral">PORTFOLIO INTELLIGENCE IS RESEARCH ONLY · RESEARCH ATTRACTIVENESS IS NOT CAPITAL AUTHORITY · CASH AND ABSTENTION ARE VALID DECISIONS</div><BroadMarketWorkspace embedded /><CryptoResearchWorkspace embedded /><GenericCampaignCandidateWorkspace embedded /><OptionsIntelligenceWorkspace embedded /><PortfolioIntelligenceView embedded /><SecuritiesOperationsView embedded /><GenuineAlpacaPaperPanel /><ExternalPaperView /></>;
}

function BroadMarketWorkspace({ embedded = false }: { embedded?: boolean }) {
  const [workspace, setWorkspace] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    try { setWorkspace(await api<ApiRecord>("/api/v1/broad-equity/workspace")); setError(null); }
    catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const sources = records(workspace?.reference_sources);
  const retained = record(workspace?.retained_research);
  const hypotheses = records(retained?.hypotheses);
  const captures = records(retained?.captures);
  const snapshots = records(retained?.snapshots);
  const runs = records(retained?.runs);
  const outcomes = records(retained?.outcomes);
  return <><div className="section-head"><div><p className="eyebrow">CURRENT-LISTED RESEARCH UNIVERSE</p><h1>{embedded ? "Broad Market health" : "Broad Market"}</h1><p className="muted">Dynamic U.S. common-stock discovery remains separate from the fixed 16-ETF research universe and from the SPY engineering-acceptance Order.</p></div>{!embedded && <button onClick={() => void refresh()}>Refresh</button>}</div><ErrorNotice error={error} /><div className="notice neutral">{list(workspace?.warnings).join(" · ") || "CURRENT-LISTED UNIVERSE — NOT HISTORICAL POINT-IN-TIME MEMBERSHIP"}</div><Panel title="Reference and universe health" action={<span className="badge">RESEARCH ONLY</span>}><KeyValueGrid entries={[["Security master", text(workspace?.security_master_count, "0")], ["Eligible universe", text(record(workspace?.universe)?.eligible_count, "inspect exact snapshot")], ["Regime", text(workspace?.regime)], ["Broad SHADOW", text(workspace?.shadow_state)], ["Authority", text(workspace?.authority, "RESEARCH_ONLY")]]} /><div className="scroll"><table><thead><tr><th>Source</th><th>Capability</th><th>State</th></tr></thead><tbody>{sources.map((item) => <tr key={text(item.provider)}><td>{text(item.provider)}</td><td>{text(item.capability)}</td><td>{text(item.state)}</td></tr>)}</tbody></table></div></Panel><Panel title="Durable hypothesis-to-outcome evidence"><KeyValueGrid entries={[["Hypothesis versions", hypotheses.length], ["Captures", captures.length], ["Observations", text(retained?.observation_count, "0")], ["Dataset snapshots", snapshots.length], ["Experiment runs", runs.length], ["Outcomes", outcomes.length]]} /><div className="scroll"><table><thead><tr><th>Kind</th><th>Exact identity</th><th>State / evidence</th></tr></thead><tbody>{hypotheses.map((item) => <tr key={text(item.hypothesis_version_id)}><td>Hypothesis</td><td><small>{text(item.hypothesis_version_id)}</small></td><td>{text(item.status)}</td></tr>)}{captures.map((item) => <tr key={text(item.capture_id)}><td>Capture</td><td><small>{text(item.capture_id)}</small></td><td>{text(item.status)} · {text(item.evidence_class)}</td></tr>)}{runs.map((item) => <tr key={text(item.run_id)}><td>Experiment</td><td><small>{text(item.run_id)}</small></td><td>{text(item.state)} · {text(item.reproducibility_status)}</td></tr>)}{outcomes.map((item) => <tr key={text(item.outcome_id)}><td>Outcome</td><td><small>{text(item.outcome_id)}</small></td><td>{text(item.decision)} · authority {text(item.authority, "NONE")}</td></tr>)}</tbody></table></div><p className="disabled-explanation">Exact-ID read routes expose each retained hypothesis, capture, snapshot, run, and outcome. No “latest” record is selected for mutation or capital authority.</p></Panel><Panel title="Eligibility, data quality, and ranked opportunities"><KeyValueGrid entries={[["Provider disagreements", list(workspace?.provider_disagreements).length], ["Eligibility exclusions", list(workspace?.eligibility_waterfall).length], ["Data quality records", list(workspace?.data_quality).length], ["Ranked securities", list(workspace?.ranked_securities).length], ["Hypothetical allocations", list(workspace?.hypothetical_portfolio).length], ["Financial authority", text(workspace?.financial_authority, "NONE")]]} /><p className="disabled-explanation">No mutable “latest” universe is inferred. Exact retained universe and capture IDs are required before a discovery or SHADOW run can be displayed.</p></Panel></>;
}

function HistoricalReplayWorkspace({ embedded = false }: { embedded?: boolean }) {
  const [response, setResponse] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    try { setResponse(await api<ApiRecord>("/api/v1/securities/historical-replay/runs")); setError(null); }
    catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const items = records(response?.items);
  return <><div className="section-head"><div><p className="eyebrow">POINT-IN-TIME HISTORICAL REPLAY — AUTHORITY NONE</p><h2>Historical Replay</h2><p className="muted">Replays research/market-intelligence logic across historical as-of dates only; never a live projection, never a claimed strategy.</p></div>{!embedded && <button onClick={() => void refresh()}>Refresh</button>}</div><ErrorNotice error={error} /><div className="notice neutral">{list(response?.warnings).join(" · ") || "POINT_IN_TIME_UNIVERSE_INCOMPLETE · SURVIVORSHIP_LIMITED"}</div><Panel title="Replay runs" action={<span className="badge">AUTHORITY NONE</span>}><div className="scroll"><table><thead><tr><th>Run</th><th>Dataset snapshot</th><th>Declared range</th><th>Cadence</th><th>Calendar</th><th>State</th><th>Cursor</th><th>Checkpoints</th></tr></thead><tbody>{items.map((entry) => { const run = record(entry.run); return <tr key={text(run?.replay_run_id)}><td><small>{text(run?.replay_run_id)}</small></td><td><small>{text(run?.dataset_snapshot_id)}</small></td><td>{text(run?.start_date)} → {text(run?.end_date)}</td><td>{text(run?.cadence)}</td><td>{text(run?.calendar_coverage_state, "CALENDAR_EVIDENCE_UNAVAILABLE")}</td><td><span className={stateClass(run?.completion_state)}>{text(run?.completion_state)}</span></td><td>{text(run?.cursor_session_date, "not started")} · {text(run?.completed_step_count, "0")} steps</td><td>{text(entry.checkpoint_count, "0")}</td></tr>; })}</tbody></table></div>{!items.length && <p className="disabled-explanation">No replay run has been created yet. Use replay-plan-create / replay-run-start / replay-run-resume against a genuine or fixture historical dataset snapshot; results remain research evidence only and never grant PAPER or LIVE authority.</p>}</Panel></>;
}

function ResearchMethodologyWorkspace({ embedded = false }: { embedded?: boolean }) {
  const [hypothesisInput, setHypothesisInput] = useState<string>("");
  const [hypothesisId, setHypothesisId] = useState<string>("");
  const [conclusionsResponse, setConclusionsResponse] = useState<ApiRecord | null>(null);
  const [auditResponse, setAuditResponse] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async (id: string) => {
    if (!id) { setConclusionsResponse(null); setAuditResponse(null); return; }
    try {
      const [conclusions, audit] = await Promise.all([
        api<ApiRecord>(`/api/v1/securities/research-methodology/conclusions/by-hypothesis/${encodeURIComponent(id)}`),
        api<ApiRecord>(`/api/v1/securities/research-methodology/audit/${encodeURIComponent(id)}`),
      ]);
      setConclusionsResponse(conclusions); setAuditResponse(audit); setError(null);
    } catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(hypothesisId); }, [hypothesisId, refresh]);
  const conclusions = records(conclusionsResponse?.conclusions);
  const auditTrail = record(auditResponse?.audit_trail);
  const entries = records(auditTrail?.entries);
  const submit = (event: FormEvent) => { event.preventDefault(); setHypothesisId(hypothesisInput.trim()); };
  return <><div className="section-head"><div><p className="eyebrow">QUANT RESEARCH METHODOLOGY — AUTHORITY NONE</p><h2>Research Methodology</h2><p className="muted">Governed research phases, holdout-by-default governance, walk-forward validation, multiple-testing-aware trial families, robustness checks, and fail-closed promotion evaluation. Research evidence only — never a strategy, PAPER, or LIVE grant.</p></div>{!embedded && <button onClick={() => void refresh(hypothesisId)}>Refresh</button>}</div><ErrorNotice error={error} /><div className="notice neutral">RESEARCH METHODOLOGY EVIDENCE ONLY — NO STRATEGY, PAPER, OR LIVE AUTHORITY</div><Panel title="Inspect by hypothesis" action={<span className="badge">EXACT ID ONLY</span>}><form className="form-grid" onSubmit={submit}><label className="wide">Hypothesis version ID<input value={hypothesisInput} onChange={(event) => setHypothesisInput(event.target.value)} placeholder="hv-…" /></label><button type="submit" className="primary">Load</button></form>{!hypothesisId && <p className="disabled-explanation">Enter a hypothesis version ID to load its retained conclusions and audit trail. No "latest" hypothesis is inferred.</p>}</Panel><Panel title="Research conclusions" action={<span className="badge">AUTHORITY NONE</span>}><div className="scroll"><table><thead><tr><th>Conclusion</th><th>Phase</th><th>State</th><th>Sample quality</th><th>Benchmark</th><th>Beats benchmark</th><th>Blockers</th></tr></thead><tbody>{conclusions.map((item) => <tr key={text(item.conclusion_id)}><td><small>{text(item.conclusion_id)}</small></td><td>{text(item.phase)}</td><td><span className={stateClass(item.conclusion_state)}>{text(item.conclusion_state)}</span></td><td>{text(item.sample_quality_state)}</td><td>{text(item.benchmark_kind, "not declared")}</td><td>{text(item.beats_benchmark, "n/a")}</td><td>{list(item.blockers).join(" · ") || "none"}</td></tr>)}</tbody></table></div>{hypothesisId && !conclusions.length && <p className="disabled-explanation">No retained conclusion for this hypothesis yet.</p>}</Panel><Panel title="Research audit trail" action={<span className="badge">ORDERED · BOUNDED</span>}><div className="scroll"><table><thead><tr><th>Occurred</th><th>Kind</th><th>Entity</th><th>Summary</th></tr></thead><tbody>{entries.map((item, index) => <tr key={`${text(item.entity_id)}-${index}`}><td><small>{text(item.occurred_at)}</small></td><td>{text(item.kind)}</td><td><small>{text(item.entity_id)}</small></td><td>{text(item.summary)}</td></tr>)}</tbody></table></div>{hypothesisId && !entries.length && <p className="disabled-explanation">No trial family, conclusion, or promotion evaluation retained for this hypothesis yet.</p>}</Panel></>;
}

function CrossMarketResearchWorkspace({ embedded = false }: { embedded?: boolean }) {
  const [agendasResponse, setAgendasResponse] = useState<ApiRecord | null>(null);
  const [agendaInput, setAgendaInput] = useState<string>("");
  const [agendaId, setAgendaId] = useState<string>("");
  const [agendaDetail, setAgendaDetail] = useState<ApiRecord | null>(null);
  const [hypothesesResponse, setHypothesesResponse] = useState<ApiRecord | null>(null);
  const [hypothesisInput, setHypothesisInput] = useState<string>("");
  const [hypothesisId, setHypothesisId] = useState<string>("");
  const [knowledgeResponse, setKnowledgeResponse] = useState<ApiRecord | null>(null);
  const [contradictionsResponse, setContradictionsResponse] = useState<ApiRecord | null>(null);
  const [followUpsResponse, setFollowUpsResponse] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(async () => {
    try {
      const [agendas, contradictions, followUps] = await Promise.all([
        api<ApiRecord>("/api/v1/securities/cross-market-research/agendas"),
        api<ApiRecord>("/api/v1/securities/cross-market-research/contradictions"),
        api<ApiRecord>("/api/v1/securities/cross-market-research/follow-up-proposals"),
      ]);
      setAgendasResponse(agendas);
      setContradictionsResponse(contradictions);
      setFollowUpsResponse(followUps);
      setError(null);
    } catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    if (!agendaId) { setAgendaDetail(null); setHypothesesResponse(null); return; }
    (async () => {
      try {
        const [detail, hypotheses] = await Promise.all([
          api<ApiRecord>(`/api/v1/securities/cross-market-research/agendas/${encodeURIComponent(agendaId)}`),
          api<ApiRecord>(`/api/v1/securities/cross-market-research/agendas/${encodeURIComponent(agendaId)}/hypotheses`),
        ]);
        setAgendaDetail(detail); setHypothesesResponse(hypotheses); setError(null);
      } catch (reason) { setError(reason); }
    })();
  }, [agendaId]);

  useEffect(() => {
    if (!hypothesisId) { setKnowledgeResponse(null); return; }
    api<ApiRecord>(`/api/v1/securities/cross-market-research/hypotheses/${encodeURIComponent(hypothesisId)}/knowledge`)
      .then((loaded) => { setKnowledgeResponse(loaded); setError(null); })
      .catch(setError);
  }, [hypothesisId]);

  const agendas = records(agendasResponse?.items);
  const agenda = record(agendaDetail?.agenda);
  const agendaRun = record(agendaDetail?.agenda_run);
  const qualityReport = record(agendaDetail?.quality_report);
  const hypotheses = records(hypothesesResponse?.items);
  const knowledgeHistory = records(knowledgeResponse?.knowledge_history);
  const contradictions = records(contradictionsResponse?.items);
  const followUps = records(followUpsResponse?.items);

  const submitAgenda = (event: FormEvent) => { event.preventDefault(); setAgendaId(agendaInput.trim()); };
  const submitHypothesis = (event: FormEvent) => { event.preventDefault(); setHypothesisId(hypothesisInput.trim()); };

  return (
    <>
      <div className="section-head">
        <div>
          <p className="eyebrow">CONTROLLED CROSS-MARKET HYPOTHESIS RESEARCH — AUTHORITY NONE</p>
          <h2>Cross-Market Research</h2>
          <p className="muted">
            A bounded, versioned research agenda evaluates a fixed set of predeclared equity/BTC/rates-proxy/
            commodity-proxy hypotheses and negative controls through the same governed methodology stack (holdout,
            walk-forward, multiple-testing correction, robustness). Research evidence only — never a Strategy,
            Shared Opportunity, PAPER order, or LIVE grant.
          </p>
        </div>
        {!embedded && <button onClick={() => void refresh()}>Refresh</button>}
      </div>
      <ErrorNotice error={error} />
      <div className="notice neutral">
        CONTROLLED HYPOTHESIS RESEARCH EVIDENCE ONLY — NO STRATEGY, PAPER, OR LIVE AUTHORITY
      </div>
      <Panel title="Research agendas" action={<span className="badge">AUTHORITY NONE</span>}>
        <div className="scroll">
          <table>
            <thead>
              <tr><th>Agenda</th><th>Version</th><th>Hypothesis budget</th><th>Trial budget</th><th>Registered</th></tr>
            </thead>
            <tbody>
              {agendas.map((item) => (
                <tr key={text(item.agenda_id)}>
                  <td><small>{text(item.agenda_id)}</small></td>
                  <td>{text(item.version)}</td>
                  <td>{text(item.maximum_hypothesis_count)}</td>
                  <td>{text(item.maximum_total_trials)}</td>
                  <td>{formatTimestamp(item.registered_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!agendas.length && <p className="disabled-explanation">No research agenda has been registered yet.</p>}
      </Panel>
      <Panel title="Inspect agenda" action={<span className="badge">EXACT ID ONLY</span>}>
        <form className="form-grid" onSubmit={submitAgenda}>
          <label className="wide">Agenda ID<input value={agendaInput} onChange={(event) => setAgendaInput(event.target.value)} placeholder="cross_market_research_agenda_…" /></label>
          <button type="submit" className="primary">Load</button>
        </form>
        {agenda && (
          <>
            <KeyValueGrid entries={[
              ["Objective", text(agenda.objective)],
              ["Agenda run", `${text(agendaRun?.status, "not started")} · cursor ${text(agendaRun?.cursor_ordinal, "0")} · trials ${text(agendaRun?.trials_executed, "0")}`],
              ["Hypotheses generated", hypotheses.length],
              ["Negative controls", text(hypothesesResponse?.negative_control_count, "0")],
              ["Quality report", text(qualityReport?.report_id, "not built yet")],
            ]} />
            <div className="scroll">
              <table>
                <thead><tr><th>Hypothesis</th><th>Template</th><th>Family</th><th>Negative control</th><th>Horizon</th></tr></thead>
                <tbody>
                  {hypotheses.map((item) => (
                    <tr key={text(item.hypothesis_id)}>
                      <td><small>{text(item.hypothesis_id)}</small></td>
                      <td>{text(item.template_id)}</td>
                      <td>{text(item.family)}</td>
                      <td>{text(item.is_negative_control)}{item.negative_control_kind ? ` (${text(item.negative_control_kind)})` : ""}</td>
                      <td>{text(item.horizon_sessions)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
        {agendaId && !agenda && <p className="disabled-explanation">No retained agenda for this exact ID.</p>}
      </Panel>
      <Panel title="Inspect hypothesis knowledge" action={<span className="badge">EXACT ID ONLY</span>}>
        <form className="form-grid" onSubmit={submitHypothesis}>
          <label className="wide">Hypothesis ID<input value={hypothesisInput} onChange={(event) => setHypothesisInput(event.target.value)} placeholder="cross_market_hypothesis_…" /></label>
          <button type="submit" className="primary">Load</button>
        </form>
        <div className="scroll">
          <table>
            <thead><tr><th>Revision</th><th>Status</th><th>Evidence strength</th><th>Holdout</th><th>Robustness</th><th>Regime dependence</th></tr></thead>
            <tbody>
              {knowledgeHistory.map((item, index) => (
                <tr key={`${text(item.entry_id)}-${index}`}>
                  <td>{text(item.revision)}</td>
                  <td><span className={stateClass(item.status)}>{text(item.status)}</span></td>
                  <td><small>{text(item.evidence_strength)}</small></td>
                  <td>{text(item.holdout_result)}</td>
                  <td>{text(item.robustness_state)}</td>
                  <td>{text(item.regime_dependence)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {hypothesisId && !knowledgeHistory.length && <p className="disabled-explanation">No retained knowledge-base entry for this hypothesis yet.</p>}
      </Panel>
      <Panel title="Contradictions" action={<span className="badge">NEVER SILENTLY AVERAGED</span>}>
        <div className="scroll">
          <table>
            <thead><tr><th>First / second entry</th><th>Holds when</th><th>Description</th></tr></thead>
            <tbody>
              {contradictions.map((item) => (
                <tr key={text(item.contradiction_id)}>
                  <td><small>{text(item.first_entry_id)}<br />{text(item.second_entry_id)}</small></td>
                  <td><small>{text(item.first_holds_when)} / {text(item.second_holds_when)}</small></td>
                  <td>{text(item.description)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!contradictions.length && <p className="disabled-explanation">No retained contradiction between knowledge-base entries.</p>}
      </Panel>
      <Panel title="Follow-up proposals" action={<span className="badge">NEVER AUTO-EXECUTED</span>}>
        <div className="scroll">
          <table>
            <thead><tr><th>Parent hypothesis</th><th>Kind</th><th>Description</th></tr></thead>
            <tbody>
              {followUps.map((item) => (
                <tr key={text(item.follow_up_id)}>
                  <td><small>{text(item.parent_hypothesis_id)}</small></td>
                  <td>{text(item.kind)}</td>
                  <td>{text(item.description)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!followUps.length && <p className="disabled-explanation">No retained follow-up proposal.</p>}
      </Panel>
    </>
  );
}

function CryptoResearchWorkspace({ embedded = false }: { embedded?: boolean }) {
  const [workspace, setWorkspace] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    try { setWorkspace(await api<ApiRecord>("/api/v1/crypto-research/workspace")); setError(null); }
    catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const capability = record(workspace?.provider_capability);
  const captures = records(workspace?.captures);
  const attempts = records(workspace?.attempts);
  const snapshots = records(workspace?.snapshots);
  const runs = records(workspace?.runs);
  const outcomes = records(workspace?.outcomes);
  const strategies = records(workspace?.strategy_specifications);
  const replications = records(workspace?.replication_runs);
  const replicationOutcomes = records(workspace?.replication_outcomes);
  const dossiers = records(workspace?.dossiers);
  const conclusions = records(workspace?.dossier_conclusions);
  const challengerStrategies = records(workspace?.challenger_strategies);
  const challengerProtocols = records(workspace?.challenger_protocols);
  const challengerRuns = records(workspace?.challenger_runs);
  const challengerOutcomes = records(workspace?.challenger_outcomes);
  const challengerDossiers = records(workspace?.challenger_dossiers);
  const challengerConclusions = records(workspace?.challenger_conclusions);
  const campaignCandidates = records(workspace?.campaign_candidates);
  const candidateProtocols = records(workspace?.candidate_1_protocols);
  const candidateRuns = records(workspace?.candidate_1_runs);
  const candidateOutcomes = records(workspace?.candidate_1_outcomes);
  const candidateDossiers = records(workspace?.candidate_1_dossiers);
  const candidateNominations = records(workspace?.candidate_1_nomination_evaluations);
  const candidate2Protocols = records(workspace?.candidate_2_protocols);
  const candidate2Runs = records(workspace?.candidate_2_runs);
  const candidate2Outcomes = records(workspace?.candidate_2_outcomes);
  const candidate2Dossiers = records(workspace?.candidate_2_dossiers);
  const candidate2Nominations = records(workspace?.candidate_2_nomination_evaluations);
  const campaign = record(workspace?.research_campaign);
  const campaignBudget = record(workspace?.budget);
  const holdout = record(workspace?.holdout);
  const exposures = records(workspace?.dataset_exposures);
  const currentCapture = captures[captures.length - 1];
  const currentSnapshot = snapshots[snapshots.length - 1];
  const currentRun = runs[runs.length - 1];
  const currentOutcome = outcomes[outcomes.length - 1];
  const currentReplication = replications[replications.length - 1];
  const currentReplicationOutcome = replicationOutcomes[replicationOutcomes.length - 1];
  const currentDossier = dossiers[dossiers.length - 1];
  const currentConclusion = conclusions[conclusions.length - 1];
  const challengerStrategy = challengerStrategies[challengerStrategies.length - 1];
  const challengerProtocol = challengerProtocols[challengerProtocols.length - 1];
  const challengerDossier = challengerDossiers[challengerDossiers.length - 1];
  const challengerConclusion = challengerConclusions[challengerConclusions.length - 1];
  const campaignCandidate = campaignCandidates.find((item) => text(item.strategy_family) === "BTC_USD_MONTHLY_200_OBSERVATION_PRICE_TREND");
  const campaignCandidate2 = campaignCandidates.find((item) => text(item.strategy_family) === "BTC_USD_MONTHLY_STATEFUL_90_45_CLOSE_BREAKOUT");
  const candidateProtocol = candidateProtocols[candidateProtocols.length - 1];
  const candidateDossier = candidateDossiers[candidateDossiers.length - 1];
  const candidateNomination = candidateNominations[candidateNominations.length - 1];
  const candidate2Protocol = candidate2Protocols[candidate2Protocols.length - 1];
  const candidate2Dossier = candidate2Dossiers[candidate2Dossiers.length - 1];
  const candidate2Nomination = candidate2Nominations[candidate2Nominations.length - 1];
  return <><div className="section-head"><div><p className="eyebrow">RETROSPECTIVE HISTORICAL PUBLIC DATA</p><h1>{embedded ? "BTC-USD validation evidence" : "Crypto Research"}</h1><p className="muted">Retained Coinbase public daily candles support rejected strategies and bounded campaign candidates. Development windows are exposed evidence; the separate holdout remains application-sealed.</p></div>{!embedded && <button onClick={() => void refresh()}>Refresh</button>}</div><ErrorNotice error={error} /><div className="notice neutral">{list(workspace?.warnings).join(" · ") || "NO RETAINED CRYPTO RESEARCH YET · NO PAPER OR LIVE AUTHORITY"}</div><Panel title="Research campaign and dataset exposure" action={<span className="badge">GOVERNANCE ONLY</span>}><KeyValueGrid entries={[["Campaign / state", `${text(campaign?.campaign_id, "not registered")} / ${text(workspace?.campaign_state, "NOT_REGISTERED")}`], ["Question", text(campaign?.question, "No bounded campaign registered.")], ["Development datasets", list(campaign?.allowed_development_snapshot_ids).join(", ") || "none"], ["Exposure records", exposures.length], ["Candidate budget", `${text(campaignBudget?.consumed_candidates, "0")} consumed / ${text(campaignBudget?.candidate_budget, "3")}`], ["Experiment budget", `${text(campaignBudget?.consumed_experiments, "0")} consumed / ${text(campaignBudget?.experiment_budget, "6")}`], ["Nominated candidate", text(workspace?.nominated_candidate, "none")]]} /><p className="disabled-explanation">Previously inspected strategy windows cannot regain untouched-validation status. Replay and retrospective comparison remain available; independent-validation claims do not.</p></Panel><Panel title="Operationally sealed historical holdout" action={<span className="badge">VALUES REDACTED</span>}><KeyValueGrid entries={[["Holdout / state", `${text(holdout?.holdout_id, "none")} / ${text(holdout?.sealing_state, "not captured")}`], ["Interval", `${formatTimestamp(holdout?.requested_start)} to ${formatTimestamp(holdout?.requested_end)}`], ["Evidence / quality", `${text(holdout?.evidence_class, "none")} / ${text(holdout?.quality, "none")}`], ["Coverage", `${text(holdout?.accepted_buckets, "0")} accepted / ${text(holdout?.expected_buckets, "0")} expected / ${text(holdout?.missing_buckets, "0")} missing`], ["Snapshot / manifest", `${text(holdout?.snapshot_id, "none")} / ${text(holdout?.manifest_hash, "none")}`], ["Release policy", list(holdout?.release_policy).join(" · ") || "not registered"], ["Values / raw responses", `${text(holdout?.values_redacted, "true")} / ${text(holdout?.raw_responses_redacted, "true")}`]]} /><p className="disabled-explanation">This is application-enforced sealing, not cryptographic denial to database administrators. No holdout return, signal, candle, or raw response is shown.</p></Panel><Panel title="Public provider and retained evidence" action={<span className="badge">READ ONLY</span>}><KeyValueGrid entries={[["Provider", text(capability?.provider, "COINBASE")], ["Network behavior", text(workspace?.network_behavior, "RETAINED EVIDENCE ONLY")], ["Product / granularity", `${text(capability?.product, "BTC-USD")} / ${text(capability?.granularity, "ONE_DAY")}`], ["Development capture", text(currentCapture?.capture_id, "none")], ["Capture status / class", `${text(currentCapture?.status, "empty")} / ${text(currentCapture?.evidence_class, "none")}`], ["Checkpoints / requests", `${records(workspace?.checkpoints).length} / ${attempts.length}`], ["Development snapshot / manifest", `${text(currentSnapshot?.snapshot_id, "none")} / ${text(currentSnapshot?.manifest_hash, "none")}`]]} /></Panel><Panel title="Original out-of-sample validation"><KeyValueGrid entries={[["Executor", text(currentRun?.executor_version, "BTC_USD_DAILY_TREND_VALIDATION_V1")], ["Validation", "2025-07-01 through 2026-06-30"], ["Feature timing", "prior completed daily candles only"], ["Cost policy", "diagnostic 0/0; conservative 60 bps fee + 10 bps slippage per side"], ["Reproducibility", text(currentRun?.reproducibility_status, "not run")], ["Result hash", text(currentRun?.result_hash, "none")], ["Outcome", text(currentOutcome?.decision, "none")], ["Outcome reason", text(currentOutcome?.reason, "No retained outcome.")], ["PAPER / LIVE authority", "NONE / NONE"]]} /><p className="disabled-explanation">The frozen result remains retained negative knowledge.</p></Panel><Panel title="Frozen strategy and disjoint replication" action={<span className="badge">REJECTED RESEARCH</span>}><KeyValueGrid entries={[["Frozen strategy versions", strategies.length], ["Replication interval", "2022-07-01 through 2024-06-30; validation begins 2023-07-01"], ["Replication run / outcome", `${text(currentReplication?.run_id, "none")} / ${text(currentReplicationOutcome?.decision, "none")}`], ["Dossier / consistency", `${text(currentDossier?.dossier_id, "none")} / ${text(currentDossier?.consistency_status, "none")}`], ["Conclusion", text(currentConclusion?.conclusion, "none")]]} /></Panel><Panel title="Rejected monthly 252 challenger" action={<span className="badge">REJECTED</span>}><KeyValueGrid entries={[["Identity / family", `${text(challengerStrategy?.specification_id, "none")} / ${text(challengerStrategy?.strategy_family, "none")}`], ["Parent / semantics", `${text(challengerStrategy?.parent_specification_id, "none")} / ${text(challengerStrategy?.semantics_hash, "none")}`], ["Protocol", text(challengerProtocol?.protocol_id, "none")], ["Exact window runs / outcomes", `${challengerRuns.length} / ${challengerOutcomes.length}`], ["Dossier / consistency", `${text(challengerDossier?.dossier_id, "none")} / ${text(challengerDossier?.consistency_status, "none")}`], ["Conclusion", text(challengerConclusion?.conclusion, "none")]]} /></Panel><Panel title="Campaign Candidate 1 · monthly 200-observation price trend" action={<span className="badge">DEVELOPMENT EVIDENCE</span>}><KeyValueGrid entries={[["Candidate / family", `${text(campaignCandidate?.candidate_id, "none")} / ${text(campaignCandidate?.strategy_family, "none")}`], ["Semantics", text(campaignCandidate?.strategy_semantics_hash, "none")], ["Expected mechanism", text(campaignCandidate?.expected_mechanism, "none")], ["Protocol", text(candidateProtocol?.protocol_id, "none")], ["Runs / outcomes", `${candidateRuns.length} / ${candidateOutcomes.length}`], ["Dossier / consistency", `${text(candidateDossier?.dossier_id, "none")} / ${text(candidateDossier?.consistency_status, "none")}`], ["Nomination eligible / failures", `${text(candidateNomination?.eligible, "false")} / ${list(candidateNomination?.failed_conditions).join(", ") || "none retained"}`]]} /></Panel><Panel title="Campaign Candidate 2 · monthly stateful 90/45 breakout" action={<span className="badge">EXPOSED DEVELOPMENT EVIDENCE</span>}><KeyValueGrid entries={[["Candidate / family", `${text(campaignCandidate2?.candidate_id, "none")} / ${text(campaignCandidate2?.strategy_family, "none")}`], ["Semantics", text(campaignCandidate2?.strategy_semantics_hash, "none")], ["Expected mechanism", text(campaignCandidate2?.expected_mechanism, "none")], ["Protocol / initial state", `${text(candidate2Protocol?.protocol_id, "none")} / ${text(candidate2Protocol?.initial_state, "CASH")}`], ["Runs / outcomes", `${candidate2Runs.length} / ${candidate2Outcomes.length}`], ["Dossier / consistency", `${text(candidate2Dossier?.dossier_id, "none")} / ${text(candidate2Dossier?.consistency_status, "none")}`], ["State transitions / protective exits", `${text(candidate2Dossier?.total_strategy_triggered_transitions, "0")} / ${text(candidate2Dossier?.total_protective_data_quality_transitions, "0")}`], ["Nomination eligible / failures", `${text(candidate2Nomination?.eligible, "false")} / ${list(candidate2Nomination?.failed_conditions).join(", ") || "none retained"}`], ["Leading challenger", text(workspace?.leading_challenger_status, "NO_LEADING_CHALLENGER")], ["Approved champion", text(workspace?.approved_champion_status, "NO_APPROVED_CHAMPION")], ["Capital / opportunity / order authority", `${text(workspace?.capital_authority, "NONE")} / ${text(workspace?.opportunity_authority, "NONE")} / ${text(workspace?.order_authority, "NONE")}`]]} /><p className="disabled-explanation">Each window starts independently in CASH. Entry and exit use only preceding completed closes and the next eligible daily open. Ending notional is hypothetical research evidence, never account value or earned profit. No holdout value, account, Order, Position, reservation, broker, journal, PAPER, or LIVE control exists here.</p></Panel></>;
}

function MultiAssetCryptoWorkspace() {
  const [workspace, setWorkspace] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    try {
      const value = await api<ApiRecord>("/api/v1/crypto-research/workspace");
      setWorkspace({
        ...record(value.multi_asset_discovery),
        market_pulse: value.market_pulse,
      });
      setError(null);
    } catch (reason) {
      setError(reason);
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const policy = record(workspace?.policy);
  const capture = record(workspace?.product_capture);
  const universe = record(workspace?.universe_snapshot);
  const products = records(workspace?.products);
  const observations = records(workspace?.observations);
  const rankings = records(workspace?.rankings);
  const opportunities = records(workspace?.opportunities);
  const pages = records(capture?.pages);
  const routeGroups = records(universe?.asset_groups);
  const pulseWorkspace = record(workspace?.market_pulse);
  const pulse = record(pulseWorkspace?.pulse_snapshot);
  const pulseSummary = record(pulseWorkspace?.market_change_summary);
  const pulseLineages = records(pulseWorkspace?.opportunity_lineages);
  return <><div className="section-head"><div><p className="eyebrow">BOUNDED PUBLIC MARKET RESEARCH</p><h2>Multi-Asset Crypto Discovery</h2><p className="muted">A deterministic Coinbase spot universe and cross-sectional ranking. These records are research evidence, not executable signals.</p></div><button onClick={() => void refresh()}>Refresh</button></div><ErrorNotice error={error} /><div className="notice neutral">CRYPTO RESEARCH ONLY · NOT AN ORDER · NOT PAPER-ELIGIBLE · NOT LIVE-ELIGIBLE · NO CAPITAL OR BROKER AUTHORITY</div><Panel title="Universe policy and evidence" action={<span className="badge">READ ONLY</span>}><KeyValueGrid entries={[["Policy / version", `${text(policy?.policy_id, "CRYPTO_UNIVERSE_SELECTION_QUALITY_POLICY_V1")} / ${text(policy?.version, "1")}`], ["Product capture", text(capture?.capture_id, "none retained")], ["Catalog pagination", `${text(capture?.pagination_status, "none")} · ${pages.length} pages · continuation ${text(capture?.continuation_cursor, "none")}`], ["Provider routes / base assets", `${text(workspace?.product_route_count, String(products.length))} / ${text(workspace?.canonical_base_asset_count, "0")}`], ["History shortlist / observations", `${list(workspace?.history_shortlist).length} / ${observations.length}`], ["Universe snapshot", text(universe?.snapshot_id, "none retained")], ["Eligible assets / excluded routes", `${list(universe?.eligible_asset_ids).length} / ${list(universe?.excluded_product_ids).length}`], ["Manifest", text(universe?.manifest_hash, "none")]]} /></Panel><Panel title="Canonical assets and representative routes"><div className="scroll"><table><thead><tr><th>Asset</th><th>Grouped routes</th><th>Selected route</th><th>History shortlist</th><th>Alternate-route evidence</th></tr></thead><tbody>{routeGroups.map((item) => <tr key={text(item.canonical_asset_id)}><td>{text(item.canonical_asset_id)}</td><td>{list(item.product_route_ids).join(", ")}</td><td>{text(item.selected_product_id, "excluded")}</td><td>{text(item.history_shortlist_rank, "not selected")}<small>{list(item.history_shortlist_reasons).join(", ")}</small></td><td>{records(item.route_decisions).filter((route) => !route.selected).map((route) => `${text(route.product_id)}: ${list(route.reasons).join("/")}`).join(" · ") || "none"}</td></tr>)}</tbody></table></div></Panel><Panel title="Cross-sectional discovery ranking"><div className="scroll"><table><thead><tr><th>Rank</th><th>Canonical asset / route</th><th>Trailing return</th><th>Realized volatility</th><th>Research score</th></tr></thead><tbody>{rankings.map((item) => <tr key={text(item.canonical_asset_id, text(item.product_id))}><td>{text(item.rank)}</td><td>{text(item.canonical_asset_id)}<small>{text(item.product_id)} · alternates {list(item.alternate_product_route_ids).join(", ") || "none"}</small></td><td>{text(item.trailing_return)}</td><td>{text(item.realized_volatility)}</td><td>{text(item.gross_research_score)}</td></tr>)}</tbody></table></div><p className="disabled-explanation">{rankings.length ? "One ranking result is retained per canonical base asset with deterministic route and asset tie-breaking." : text(workspace?.empty_state, "No retained discovery run.")}</p></Panel><Panel title="Daily market pulse" action={<span className="badge">AUTHORITY NONE</span>}><KeyValueGrid entries={[["Refresh / state", `${text(pulseWorkspace?.refresh_id, "none")} / ${text(pulseWorkspace?.refresh_state, "EMPTY")}`], ["Completed UTC day", formatTimestamp(pulse?.as_of)], ["Pulse / manifest", `${text(pulse?.pulse_id, "none")} / ${text(pulse?.manifest_hash, "none")}`], ["Positive / persistent / reversed", `${text(pulseSummary?.positive_score_count, "0")} / ${text(pulseSummary?.persistent_positive_count, "0")} / ${text(pulseSummary?.reversed_count, "0")}`], ["Added / removed routes", `${list(pulseSummary?.assets_added).length} / ${list(pulseSummary?.assets_removed).length}`], ["Quote limitation", text(pulse?.quote_limitation, "PUBLIC_TOP_OF_BOOK_NOT_CAPTURED_BY_SUPPORTED_BOUNDARY")], ["Authority", text(pulseWorkspace?.authority, "NONE")]]} /><div className="scroll"><table><thead><tr><th>Asset / route</th><th>Transition</th><th>Persistence</th><th>Consecutive positive</th><th>Score / rank change</th></tr></thead><tbody>{pulseLineages.map((item) => <tr key={text(item.lineage_id)}><td>{text(item.canonical_asset_id)}<small>{text(item.product_id)}</small></td><td>{text(item.transition)}</td><td>{text(item.persistence_classification)}</td><td>{text(item.consecutive_positive_refreshes)}</td><td>{text(item.score_delta)} / {text(item.rank_delta)}</td></tr>)}</tbody></table></div><p className="disabled-explanation">{text(pulseWorkspace?.empty_state, "Daily refresh evidence is shown only after a complete bounded observation. Missing quote and depth data remain explicitly unavailable.")}</p></Panel><Panel title="Research Opportunities and rejections"><div className="scroll"><table><thead><tr><th>Asset / route</th><th>Status</th><th>Valid until</th><th>Rejection reasons</th></tr></thead><tbody>{opportunities.map((item) => <tr key={text(item.opportunity_id)}><td>{text(item.canonical_asset_id)}<small>{text(item.product_id)}</small></td><td>{text(item.status)}</td><td>{formatTimestamp(item.valid_until)}</td><td>{list(item.rejection_reasons).join(", ") || "none"}</td></tr>)}</tbody></table></div><p className="disabled-explanation">Maximum research allocation is hypothetical only. No Opportunity displayed here can create an Order, Position, reservation, fill, journal, PAPER trial, or LIVE authority.</p></Panel></>;
}

function SharedOpportunityInbox() {
  const [workspace, setWorkspace] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    try { setWorkspace(await api<ApiRecord>("/api/v1/shared-opportunities/workspace")); setError(null); }
    catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const opportunitySet = record(workspace?.opportunity_set);
  const allocation = record(workspace?.allocation);
  const ranked = records(workspace?.ranked_opportunities);
  const rejected = records(workspace?.rejected_opportunities);
  const rows = [...ranked, ...rejected];
  const marketContext = record(workspace?.total_market_context);
  const contextSnapshot = record(marketContext?.context_snapshot);
  const enrichedSet = record(marketContext?.enriched_opportunity_set);
  const contextComponents = records(marketContext?.components);
  const contextAttachments = records(marketContext?.attachments);
  const unavailableComponents = records(marketContext?.unavailable_components);
  const crossVenue = record(workspace?.cross_venue_allocation);
  const crossPolicy = record(crossVenue?.policy);
  const crossDecision = record(crossVenue?.decision);
  const crossLines = records(crossVenue?.lines);
  const crossExplanations = records(crossVenue?.explanations);
  const trialProposals = records(crossVenue?.paper_trial_proposals);
  const performance = record(workspace?.performance_attribution);
  const performanceCounts = record(performance?.paper_evidence_counts);
  const attributions = records(performance?.attributions);
  const strategyHealth = records(performance?.health);
  const venueSummaries = records(performance?.venue_summaries);
  const portfolioSummary = record(performance?.portfolio_summary);
  const venueTotals = {
    CRYPTO: pairValue(crossDecision?.venue_totals, "CRYPTO"),
    KALSHI: pairValue(crossDecision?.venue_totals, "KALSHI"),
  };
  return <><div className="section-head"><div><p className="eyebrow">SHARED OPPORTUNITY OPERATING LAYER</p><h2>Current research Opportunity inbox</h2><p className="muted">Retained Crypto and Kalshi evidence is compared without erasing venue semantics. Missing values remain missing.</p></div><button onClick={() => void refresh()}>Refresh</button></div><ErrorNotice error={error} /><div className="notice neutral">HYPOTHETICAL RESEARCH ONLY · NOT ACCOUNT CAPITAL · NOT A RESERVATION OR ORDER · NO PAPER OR LIVE AUTHORITY</div><Panel title="Opportunity set" action={<span className="badge">AUTHORITY NONE</span>}><KeyValueGrid entries={[["Set / manifest", `${text(opportunitySet?.opportunity_set_id, "none")} / ${text(opportunitySet?.manifest_hash, "none")}`], ["Crypto / Kalshi", `${text(workspace?.crypto_count, "0")} / ${text(workspace?.kalshi_count, "0")}`], ["Eligible / rejected", `${text(workspace?.eligible_count, "0")} / ${text(workspace?.rejected_count, "0")}`], ["Allocation proposal", text(allocation?.proposal_id, "none")], ["Unallocated cash", text(allocation?.unallocated_cash, "1000")], ["Total Market context", text(contextSnapshot?.snapshot_id, "not attached")]]} /><div className="scroll"><table><thead><tr><th>Venue / instrument</th><th>Eligibility</th><th>Value / downside / costs</th><th>Liquidity / lockup</th><th>Rank / components</th><th>Explanation</th></tr></thead><tbody>{rows.map((row) => { const projection = record(row.projection); const eligibility = record(row.eligibility); const priority = record(row.priority); const explanation = record(row.explanation); const components = records(priority?.components); return <tr key={text(projection?.projection_id)}><td>{text(projection?.venue)} / {text(projection?.instrument_id)}<small>{text(projection?.product_or_market_id)} · {text(projection?.strategy_or_method_id)}</small></td><td>{text(eligibility?.state)}<small>{list(eligibility?.reasons).join(", ") || "none"}</small></td><td>{text(projection?.expected_net_return, "missing")} / {text(projection?.maximum_loss, "missing")} / {text(projection?.modeled_fees, "missing")} + {text(projection?.modeled_slippage, "missing")}</td><td>{text(projection?.liquidity_state)} / {text(projection?.estimated_lockup_days, "missing")} days<small>freshness {text(projection?.freshness_seconds)}s</small></td><td>{text(priority?.rank, "—")} / {text(priority?.score, "—")}<small>{components.map((item) => `${text(item.name)}=${text(item.weighted_value)}`).join(", ") || "not ranked"}</small></td><td>{text(explanation?.text)}<small>{list(projection?.source_warnings).join(", ")}</small></td></tr>; })}</tbody></table></div><p className="disabled-explanation">There are no trading, approval, reservation, account-balance, or execution controls in this inbox.</p></Panel><Panel title="Total Market context" action={<span className="badge">EXPLANATORY ONLY</span>}><KeyValueGrid entries={[["Snapshot / state", `${text(contextSnapshot?.snapshot_id, "none")} / ${text(contextSnapshot?.overall_state, "INSUFFICIENT_CONTEXT")}`], ["Policy", text(contextSnapshot?.policy_id, "TOTAL_MARKET_CONTEXT_POLICY_V1")], ["Enriched set", text(enrichedSet?.enriched_set_id, "none")], ["Components / unavailable", `${contextComponents.length} / ${unavailableComponents.length}`], ["Opportunity attachments", contextAttachments.length], ["Eligibility changed", text(marketContext?.eligibility_changed, "false")], ["Ranking changed", text(marketContext?.ranking_changed, "false")], ["Allocation changed", text(marketContext?.allocation_changed, "false")]]} /><div className="scroll"><table><thead><tr><th>Component</th><th>Subject</th><th>State</th><th>Change</th><th>Freshness / quality</th><th>Limitations</th></tr></thead><tbody>{contextComponents.map((item) => <tr key={text(item.component_id)}><td>{text(item.component_type)}</td><td>{text(item.subject)}</td><td>{text(item.state)}</td><td>{text(item.normalized_change)}</td><td>{text(item.freshness_seconds)}s / {text(item.quality_status)}</td><td>{list(item.limitations).join(", ")}</td></tr>)}</tbody></table></div><p className="disabled-explanation">Context is retained beside each Opportunity for explanation and future attribution lineage. It does not reject, promote, rank, allocate, reserve capital, or predict returns.</p></Panel><Panel title="Cross-Venue Allocation" action={<span className="badge">AUTHORITY NONE · NO EXECUTION</span>}><KeyValueGrid entries={[["Policy", text(crossPolicy?.policy_id, "CROSS_VENUE_ALLOCATION_POLICY_V1")], ["Available hypothetical capital", text(crossDecision?.hypothetical_capital, "1000")], ["Retained cash", text(crossDecision?.retained_cash, "1000")], ["Crypto allocation", text(venueTotals.CRYPTO, "0")], ["Kalshi allocation", text(venueTotals.KALSHI, "0")], ["Modeled downside / costs", `${text(crossDecision?.modeled_downside, "0")} / ${text(crossDecision?.modeled_costs, "0")}`], ["Long-lockup capital", text(crossDecision?.long_lockup_capital, "0")], ["PAPER trial proposals", trialProposals.length], ["Authority / LIVE", `${text(crossVenue?.authority, "NONE")} / ${text(crossVenue?.live_authority, "NONE")}`]]} /><div className="scroll"><table><thead><tr><th>Opportunity / venue</th><th>Allocation</th><th>Downside / costs</th><th>Lockup</th><th>Binding constraints</th></tr></thead><tbody>{crossLines.map((item) => <tr key={text(item.line_id)}><td>{text(item.projection_id)}<small>{text(item.venue)} · {text(item.instrument_id)}</small></td><td>{decimal(item.amount)}</td><td>{text(item.modeled_downside)} / {text(item.modeled_costs)}</td><td>{text(item.estimated_lockup_days, "unknown")} days</td><td>{list(item.binding_constraints).join(", ")}</td></tr>)}</tbody></table></div>{!crossLines.length && <Empty title="Zero allocation" text="No retained Opportunity currently passes every source, context, constraint, and venue PAPER-readiness gate." />}<p className="muted">Context effects: {list(crossDecision?.context_effects).join(" · ") || "none retained"}</p><p className="muted">Excluded: {crossExplanations.filter((item) => !item.allocated).map((item) => `${text(item.projection_id)}: ${list(item.excluded_reasons).join("/")}`).join(" · ") || "none"}</p><p className="disabled-explanation">Trial proposals are immutable planning evidence only. They create no operator authorization, reservation, order, fill, journal, provider request, broker write, or LIVE authority. This area intentionally has no execution buttons.</p></Panel><Panel title="Attribution and Strategy Health" action={<span className="badge">READ ONLY · AUTHORITY NONE</span>}><div className="notice neutral">PAPER PERFORMANCE IS NOT REAL EARNINGS · INTERNAL AND EXTERNAL PAPER REMAIN SEPARATE</div><KeyValueGrid entries={[["Attributions / fills / settled", `${text(performanceCounts?.attributions, "0")} / ${text(performanceCounts?.fills, "0")} / ${text(performanceCounts?.settled, "0")}`], ["Reconciliation issues", text(performanceCounts?.reconciliation_issues, "0")], ["Combined filled capital", text(portfolioSummary?.filled_capital, "0")], ["Realized / unrealized PAPER P&L", `${text(portfolioSummary?.realized_paper_pnl, "unavailable")} / ${text(portfolioSummary?.unrealized_paper_pnl, "unavailable")}`], ["Fees / slippage", `${text(portfolioSummary?.fees, "0")} / ${text(portfolioSummary?.slippage, "0")}`], ["LIVE authority", text(performance?.live_authority, "NONE")]]} /><div className="scroll"><table><thead><tr><th>Method</th><th>Health</th><th>Trials / fills / settled</th><th>Expected / realized</th><th>Fill / reconciliation</th><th>Operator recommendation</th></tr></thead><tbody>{strategyHealth.map((item) => <tr key={text(item.health_id)}><td>{text(item.strategy_or_method_id)}<small>{text(item.strategy_or_method_version)}</small></td><td>{text(item.state)}</td><td>{text(item.paper_trial_count)} / {text(item.fill_count)} / {text(item.settled_outcome_count)}</td><td>{text(item.expected_net_return, "unavailable")} / {text(item.realized_net_return, "unavailable")}</td><td>{text(item.fill_rate, "unavailable")} / {text(item.reconciliation_quality, "unavailable")}</td><td>{text(item.operator_recommendation)}</td></tr>)}</tbody></table></div><p className="muted">Venue summaries: {venueSummaries.map((item) => `${text(item.scope_id)} filled=${text(item.filled_capital)} realized=${text(item.realized_paper_pnl)} issues=${text(item.reconciliation_issue_count)}`).join(" · ") || "none retained"}</p><p className="muted">Context lineage: {attributions.map((item) => `${text(item.attribution_id)} decision=${text(item.context_at_consideration_id, "unavailable")} execution=${text(item.context_at_execution_id, "unavailable")}`).join(" · ") || "no attribution records"}</p><p className="disabled-explanation">Health is descriptive operator evidence only. It cannot promote, authorize, suspend, retire, allocate, submit, cancel, or contact a provider.</p></Panel></>;
}

function OperatingCycleWorkspace() {
  const [workspace, setWorkspace] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    try { setWorkspace(await api<ApiRecord>("/api/v1/operating-cycle/workspace")); setError(null); }
    catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const plan = record(workspace?.plan);
  const control = record(workspace?.operator_control);
  const cycle = record(workspace?.latest_cycle);
  const health = record(workspace?.operating_health);
  const brief = record(workspace?.operating_brief);
  return <><ErrorNotice error={error} /><Panel title="Recurring operating cycle" action={<button onClick={() => void refresh()}>Refresh</button>}><div className="notice neutral">RESEARCH COORDINATION ONLY · AUTHORITY NONE</div><KeyValueGrid entries={[["Plan / enabled", `${text(plan?.plan_id, "none")} / ${text(plan?.enabled, "false")}`], ["Operator control", text(control?.state, plan ? "ENABLED" : "UNAVAILABLE")], ["Latest cycle / as-of", `${text(cycle?.cycle_id, "none")} / ${formatTimestamp(cycle?.as_of)}`], ["Cycle state", text(workspace?.latest_cycle_state, "NO RETAINED CYCLE")], ["Health", text(health?.state, "UNAVAILABLE")], ["Current brief", text(brief?.brief_id, "none")], ["Operator attention", list(brief?.operator_attention).join(", ") || "none"], ["Capital / orders / broker", `${text(workspace?.capital_authority, "NONE")} / ${text(workspace?.order_authority, "NONE")} / ${text(workspace?.broker_authority, "NONE")}`]]} /><p className="disabled-explanation">This bounded external cycle coordinates retained research evidence only. Pause and resume are exact-ID CLI controls; no trading control is exposed here.</p></Panel></>;
}

function GenericCampaignCandidateWorkspace({ embedded = false }: { embedded?: boolean }) {
  const [workspace, setWorkspace] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    try { setWorkspace(await api<ApiRecord>("/api/v1/crypto-research/workspace")); setError(null); }
    catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const candidates = records(workspace?.generic_candidate_views);
  const operability = record(workspace?.generic_operability);
  const primitives = record(operability?.supported_primitives);
  return <><div className="section-head"><div><p className="eyebrow">GENERIC CAMPAIGN OPERABILITY</p><h2>Structured candidate definitions</h2><p className="muted">Retained numbered candidates are compatibility projections through one bounded definition, compiler, execution, dossier, and nomination view model.</p></div>{!embedded && <button onClick={() => void refresh()}>Refresh</button>}</div><ErrorNotice error={error} /><Panel title="Bounded compiler and executor" action={<span className="badge">AUTHORITY NONE</span>}><KeyValueGrid entries={[["Definition schema", text(operability?.definition_schema_version)], ["Compiler", text(operability?.compiler)], ["Executor", text(operability?.executor)], ["Features", list(primitives?.features).join(", ")], ["Rule modes", list(primitives?.rule_modes).join(", ")], ["Unsupported", list(operability?.unsupported).join(", ")], ["Financial authority", text(operability?.financial_authority, "NONE")]]} /></Panel>{candidates.map((item) => { const candidate = record(item.candidate); const definition = record(item.definition); const protocol = record(item.protocol); const dossier = record(item.dossier); const nomination = record(item.nomination); const features = records(definition?.features); const rules = records(definition?.rules); return <Panel key={text(candidate?.candidate_id)} title={`Campaign candidate · ${text(candidate?.candidate_id)}`} action={<span className="badge">COMPATIBILITY VIEW</span>}><KeyValueGrid entries={[["Family / semantics", `${text(candidate?.strategy_family)} / ${text(candidate?.strategy_semantics_hash)}`], ["Hypothesis", text(candidate?.hypothesis_version_id)], ["Expected mechanism", text(candidate?.expected_mechanism)], ["Features / rules", `${features.length} / ${rules.length}`], ["Mode / cadence", `${text(definition?.rule_mode)} / ${text(definition?.cadence)}`], ["Execution / costs", `${text(definition?.execution_timing)} / ${text(definition?.fee_bps_per_side)} + ${text(definition?.slippage_bps_per_side)} bps`], ["Protocol", text(protocol?.protocol_id)], ["Runs / outcomes", `${records(item.runs).length} / ${records(item.outcomes).length}`], ["Dossier", text(dossier?.dossier_id)], ["Nomination / failures", `${text(nomination?.eligible, "false")} / ${list(nomination?.failed_conditions).join(", ") || "none"}`], ["PAPER / LIVE / financial", `${text(item.paper_eligible, "false")} / ${text(item.live_eligible, "false")} / ${text(item.financial_authority, "NONE")}`]]} /><p className="disabled-explanation">This structured projection does not replace historical hashes or grant candidate, budget, holdout, capital, broker, Order, Position, or financial authority.</p></Panel>; })}</>;
}

function OptionsIntelligenceWorkspace({ embedded = false }: { embedded?: boolean }) {
  const [workspace, setWorkspace] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    try { setWorkspace(await api<ApiRecord>("/api/v1/options-intelligence/workspace")); setError(null); }
    catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const capabilities = records(workspace?.capabilities);
  const forwardOps = record(workspace?.forward_evidence_operations);
  const captureDue = record(forwardOps?.capture_due);
  const latestCheckpoint = record(forwardOps?.latest_checkpoint);
  const readinessRows = records(forwardOps?.per_underlying_history_readiness);
  const operatorCommands = record(forwardOps?.operator_commands);
  return <><div className="section-head"><div><p className="eyebrow">DEFINED-RISK / OBSERVATION ONLY</p><h1>{embedded ? "Options-data capability" : "Options Intelligence"}</h1><p className="muted">Only long calls, long puts, call debit spreads, and put debit spreads are eligible research structures.</p></div>{!embedded && <button onClick={() => void refresh()}>Refresh</button>}</div><ErrorNotice error={error} /><div className="notice neutral">{list(workspace?.warnings).join(" · ") || "OPTIONS INTELLIGENCE IS RESEARCH ONLY"}</div><Panel title="Chain and candidate state" action={<span className="badge">NO ORDER AUTHORITY</span>}><KeyValueGrid entries={[["Selected underlying", text(workspace?.selected_underlying, "none")], ["Chain snapshot", text(record(workspace?.chain_snapshot)?.chain_id, "none")], ["Expirations", list(workspace?.expirations).length], ["Contracts", list(workspace?.contracts).length], ["Defined-risk candidates", list(workspace?.defined_risk_candidates).length], ["Stock-option comparisons", list(workspace?.stock_option_comparisons).length], ["SHADOW proposal", text(workspace?.shadow_proposal_state)], ["Financial authority", text(workspace?.financial_authority, "NONE")]]} /></Panel><Panel title="Provider capability evidence"><div className="scroll"><table><thead><tr><th>Provider</th><th>Contract discovery</th><th>Quotes / Greeks</th><th>Order authority</th></tr></thead><tbody>{capabilities.map((item) => <tr key={text(item.provider)}><td>{text(item.provider)}</td><td>{text(item.contract_discovery, text(item.complete_series))}</td><td>{text(item.quotes, "—")} / {text(item.greeks, text(item.open_interest))}</td><td>{text(item.option_order_authority, "NONE")}</td></tr>)}</tbody></table></div><p className="disabled-explanation">Missing IV, Greeks, open interest, or licensed OCC coverage remains explicitly unavailable. No value is fabricated.</p></Panel><Panel title="Forward options evidence operations" action={<span className="badge">AUTHORITY NONE</span>}><KeyValueGrid entries={[["Capture due state", text(captureDue?.status, "unknown")], ["Due session", text(captureDue?.session_id, "none")], ["Window opens", text(captureDue?.window_open_at, "—")], ["Window closes", text(captureDue?.window_close_at, "—")], ["Due reasons", list(captureDue?.reasons).join(", ") || "—"], ["Latest checkpoint session", text(latestCheckpoint?.session_id, "none retained")], ["Underlyings captured", latestCheckpoint ? `${text(latestCheckpoint.underlyings_captured_count)} / ${text(latestCheckpoint.underlyings_requested_count)}` : "—"], ["Fresh snapshots", list(latestCheckpoint?.fresh_snapshot_ids).length], ["Stale snapshots (excluded from current)", text(latestCheckpoint?.stale_snapshot_count, "0")], ["Structures retained / evaluated", latestCheckpoint ? `${text(latestCheckpoint.structures_retained)} / ${text(latestCheckpoint.structures_evaluated)}` : "—"], ["Fee evidence", text(latestCheckpoint?.fee_evidence_status, "—")], ["OPRA entitlement", text(latestCheckpoint?.opra_entitlement_state, "UNCONFIRMED (no checkpoint yet)")], ["Open interest", text(latestCheckpoint?.open_interest_limitation, "OPEN_INTEREST_UNAVAILABLE")], ["Longitudinal-ready underlyings", `${text(forwardOps?.longitudinal_research_ready_underlying_count, "0")} / ${readinessRows.length} (min ${text(forwardOps?.minimum_distinct_sessions_for_longitudinal_research, "20")} sessions)`]]} /><p className="disabled-explanation">Operator commands: {machineItems(operatorCommands ? Object.values(operatorCommands) : [])}</p><p className="disabled-explanation">{list(forwardOps?.limitations).join(" · ") || "No retained limitations"}</p></Panel></>;
}

function ExternalPaperView() {
  const [readiness, setReadiness] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [campaignId, setCampaignId] = useState("");
  const [campaignStatus, setCampaignStatus] = useState<ApiRecord | null>(null);
  const [instructionId, setInstructionId] = useState("");
  const [bindingId, setBindingId] = useState("");
  const [evidenceId, setEvidenceId] = useState("");
  const [positionId, setPositionId] = useState("");
  const [reason, setReason] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [notice, setNotice] = useState("");
  const refresh = useCallback(async () => {
    try { setReadiness(await api<ApiRecord>("/api/v1/external-paper/readiness")); setError(null); }
    catch (reason) { setError(reason); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const kill = async () => {
    try {
      await api(`/api/v1/external-paper/campaigns/${encodeURIComponent(campaignId)}/kill`, { method: "POST", headers: { "Idempotency-Key": `console-kill-${campaignId}`, "Content-Type": "application/json" }, body: JSON.stringify({ reason, confirmation }) });
      setNotice("External PAPER openings are killed for this Campaign. Synchronization and risk-reducing exits remain available."); setError(null);
    } catch (failure) { setError(failure); }
  };
  const loadCampaign = async () => {
    if (!campaignId) return;
    try { setCampaignStatus(await api<ApiRecord>(`/api/v1/external-paper/campaigns/${encodeURIComponent(campaignId)}/status`)); setError(null); }
    catch (failure) { setError(failure); }
  };
  const applyFills = async () => {
    if (!instructionId) return;
    try {
      const result = await api<ApiRecord>(`/api/v1/external-paper/instructions/${encodeURIComponent(instructionId)}/apply-fills`, { method: "POST", headers: { "Idempotency-Key": `console-apply-${instructionId}`, "Content-Type": "application/json" }, body: JSON.stringify({}) });
      setNotice(`Retained fills applied: ${text(result.applied_fill_ids, "none")}. No broker operation occurred.`); await loadCampaign(); setError(null);
    } catch (failure) { setError(failure); }
  };
  const instructionAction = async (action: "prepare" | "submit" | "synchronize" | "cancel" | "validate-fills" | "apply-fills" | "reconciliation") => {
    if (!instructionId) return;
    try {
      const path = `/api/v1/external-paper/instructions/${encodeURIComponent(instructionId)}/${action}`;
      const options = action === "reconciliation"
        ? undefined
        : { method: "POST", headers: { "Idempotency-Key": `console-${action}-${instructionId}`, "Content-Type": "application/json" }, body: JSON.stringify(action === "prepare" ? { binding_id: bindingId } : action === "validate-fills" ? { evidence_id: evidenceId } : {}) };
      const result = await api<ApiRecord>(path, options);
      setNotice(`${action}: ${text(result.state, "retained")}. ${text(result.next_action, "Review retained state.")}`); await loadCampaign(); setError(null);
    } catch (failure) { setError(failure); }
  };
  const reduce = async () => {
    if (!instructionId || !positionId) return;
    const decision = new Date(); const earliest = new Date(decision.getTime() + 60_000); const expires = new Date(earliest.getTime() + 3_600_000);
    try {
      const result = await api<ApiRecord>(`/api/v1/external-paper/instructions/${encodeURIComponent(instructionId)}/reduce`, { method: "POST", headers: { "Idempotency-Key": `console-reduce-${instructionId}`, "Content-Type": "application/json" }, body: JSON.stringify({ position_id: positionId, decision_at: decision.toISOString(), earliest_fill_at: earliest.toISOString(), expires_at: expires.toISOString() }) });
      setNotice(`reduce: ${text(result.state, "retained")}. ${text(result.next_action, "Review retained state.")}`); await loadCampaign(); setError(null);
    } catch (failure) { setError(failure); }
  };
  const flatten = async () => {
    if (!campaignId) return;
    try {
      const result = await api<ApiRecord>(`/api/v1/external-paper/campaigns/${encodeURIComponent(campaignId)}/flatten`, { method: "POST", headers: { "Idempotency-Key": `console-flatten-${campaignId}`, "Content-Type": "application/json" }, body: JSON.stringify({ reason, confirmation }) });
      setNotice(`flatten: ${text(result.completed, "blocked")}. ${text(result.blockers, "No blockers retained.")}`); await loadCampaign(); setError(null);
    } catch (failure) { setError(failure); }
  };
  const instructions = records(campaignStatus?.instructions);
  return <><div className="section-head"><div><p className="eyebrow">EXTERNAL BROKER BOUNDARY</p><h1>External PAPER</h1><p className="muted">Readiness is retained separately from authority. This workspace does not enable LIVE trading.</p></div><button onClick={() => void refresh()}>Refresh</button></div><ErrorNotice error={error} />{notice && <div className="notice neutral">{notice}</div>}<div className="notice neutral">PAPER EXECUTION IS SIMULATED · FIXTURE BROKER STATE IS EVIDENCE, NOT AUTOMATIC APEX TRUTH · ONLY VALIDATED FILLS MAY CREATE PAPER FINANCIAL STATE · LIVE TRADING IS NOT ENABLED</div><Panel title="Paper-only readiness" action={<span className="badge">{text(readiness?.readiness, "CHECKING")}</span>}><KeyValueGrid entries={[["Environment", text(readiness?.environment, "FIXTURE_PAPER")], ["Adapter", text(readiness?.adapter)], ["Credentials", text(readiness?.credentials_available)], ["Authority", text(readiness?.authority, "NONE")], ["Kill state", text(readiness?.kill_state, "NOT AUTHORIZED")], ["Order submit", text(readiness?.order_submit, "false")], ["Order read", text(readiness?.order_read, "false")], ["Cancel", text(readiness?.cancel, "false")]]} /><p className="disabled-explanation">This acceptance workspace uses only the deterministic no-network fixture. It has no credential, provider, or LIVE control.</p></Panel><Panel title="Campaign lifecycle"><div className="form-grid"><label>Campaign ID<input value={campaignId} onChange={(event) => setCampaignId(event.target.value)} /></label></div><button disabled={!campaignId} onClick={() => void loadCampaign()}>Load retained state</button>{campaignStatus && <><KeyValueGrid entries={[["Kill state", text(campaignStatus.kill_state)], ["Instructions", text(campaignStatus.instruction_count)], ["Unknown activity", text(campaignStatus.unknown_broker_activity_block)], ["LIVE authority", text(campaignStatus.live_authority)]]} /><div className="scroll"><table><thead><tr><th>Instruction</th><th>Internal execution</th><th>Broker evidence</th><th>Position / financial effects</th><th>Reconciliation</th></tr></thead><tbody>{instructions.map((item) => { const binding = record(item.binding); const order = record(item.internal_order); const position = record(item.position); const positionQuantity = record(position?.open_quantity); const attempt = record(item.submission_attempt); const observations = records(item.broker_observations); const fills = records(item.validated_fills); const reconciliations = records(item.reconciliations); return <tr key={text(item.instruction_id)}><td><small>{text(item.instruction_id)}</small><br />{text(item.symbol)} / {text(item.side)} · {text(item.quantity)}<small>{text(item.state)} · broker {text(item.broker_order_id, "—")}</small></td><td><small>binding {text(binding?.binding_id, "not prepared")}</small><br />{text(binding?.state, "—")}<small>order {text(order?.order_id, "—")} · {text(order?.state, "—")}</small><small>reservation/reference {text(binding?.risk_reservation_id, "—")}</small></td><td><small>attempt {text(attempt?.state, "—")}</small><br />{observations.length} retained observation(s)<small>{fills.length} validated fill(s) / {fills.filter((fill) => record(fill.application)).length} applied</small></td><td><small>position {text(position?.position_id, "—")}</small><br />qty {text(positionQuantity?.value, "0")} · average {text(position?.average_cost, "—")}<small>realized P&L {text(position?.realized_pnl, "0")} · journals are retained with each applied increment</small></td><td>{reconciliations.length ? reconciliations.map((report) => <small key={text(report.reconciliation_id)}>{text(report.classification)} · {text(report.required_action)}</small>) : <small>not yet reconciled</small>}</td></tr>; })}</tbody></table></div></>}</Panel><Panel title="Exact-ID fixture lifecycle controls"><p className="muted">Each control calls the production application service through the deterministic fixture. State and retained evidence decide the next action.</p><div className="form-grid"><label>Instruction ID<input value={instructionId} onChange={(event) => setInstructionId(event.target.value)} placeholder="external_paper_instruction_…" /></label><label>Binding ID (prepare)<input value={bindingId} onChange={(event) => setBindingId(event.target.value)} /></label><label>Evidence ID (validate)<input value={evidenceId} onChange={(event) => setEvidenceId(event.target.value)} /></label><label>Position ID (reduce)<input value={positionId} onChange={(event) => setPositionId(event.target.value)} /></label></div><div className="button-row"><button disabled={!instructionId || !bindingId} onClick={() => void instructionAction("prepare")}>Prepare</button><button disabled={!instructionId} onClick={() => void instructionAction("submit")}>Submit fixture</button><button disabled={!instructionId} onClick={() => void instructionAction("synchronize")}>Sync fixture</button><button disabled={!instructionId || !evidenceId} onClick={() => void instructionAction("validate-fills")}>Validate</button><button disabled={!instructionId} onClick={() => void instructionAction("apply-fills")}>Apply</button><button disabled={!instructionId} onClick={() => void instructionAction("reconciliation")}>Reconcile</button><button disabled={!instructionId} onClick={() => void instructionAction("cancel")}>Cancel</button><button disabled={!instructionId || !positionId} onClick={() => void reduce()}>Reduce</button></div></Panel><Panel title="Kill and flatten"><p className="muted">Kill blocks openings; flatten then cancels pending fixture opens and reports retained blockers until every position is closed.</p><div className="form-grid"><label>Reason<input value={reason} onChange={(event) => setReason(event.target.value)} /></label><label>Exact confirmation<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder={campaignId ? `KILL_EXTERNAL_PAPER:${campaignId} or FLATTEN_EXTERNAL_PAPER:${campaignId}` : "KILL_EXTERNAL_PAPER:<campaign-id>"} /></label></div><div className="button-row"><button disabled={!campaignId || !reason || confirmation !== `KILL_EXTERNAL_PAPER:${campaignId}`} onClick={() => void kill()}>Kill new openings</button><button disabled={!campaignId || !reason || confirmation !== `FLATTEN_EXTERNAL_PAPER:${campaignId}`} onClick={() => void flatten()}>Flatten fixture Campaign</button></div></Panel></>;
}

function CryptoPaperWorkspace() {
  const [accountId, setAccountId] = useState("apex-crypto-paper-fixture-v1");
  const [workspace, setWorkspace] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const inspect = async () => {
    if (!accountId.trim()) {
      setError(new ApiError("Enter one exact PAPER account ID.", "CRYPTO_PAPER_ACCOUNT_ID_REQUIRED"));
      return;
    }
    try {
      setWorkspace(await api<ApiRecord>(`/api/v1/crypto-paper/workspace/${encodeURIComponent(accountId.trim())}`));
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  };
  const account = record(workspace?.account);
  const accountState = record(workspace?.account_state);
  const orders = records(workspace?.orders);
  const fills = records(workspace?.fills);
  const positions = records(workspace?.positions);
  const authorizations = records(workspace?.authorizations);
  const previews = records(workspace?.previews);
  const reconciliations = records(workspace?.reconciliations);
  return <><div className="section-head"><div><p className="eyebrow">APEX INTERNAL VENUE / ZERO PROVIDER SUBMISSION</p><h1>Crypto Spot PAPER</h1><p className="muted">Balances, fills, positions, fees, and profit or loss shown here are simulated Apex PAPER evidence. They are not Coinbase balances or genuine earnings.</p></div><span className="badge">PAPER ONLY · LIVE NONE</span></div><ErrorNotice error={error} /><Panel title="Exact PAPER account"><div className="control-row"><label>Account ID<input value={accountId} onChange={(event) => setAccountId(event.currentTarget.value)} /></label><button className="primary" onClick={() => void inspect()}>Inspect retained state</button></div></Panel>{workspace && !account && <Empty title="No retained PAPER account" text="No state exists for this exact identity. Account creation and all mutations remain exact-ID CLI operations." />}{account && accountState && <><div className="metrics"><Metric label="PAPER cash" value={decimal(accountState.cash)} note="Hypothetical USD" /><Metric label="Reserved PAPER cash" value={decimal(accountState.reserved_cash)} /><Metric label="Modeled fees" value={decimal(accountState.modeled_fees)} /><Metric label="Realized PAPER P/L" value={decimal(accountState.realized_paper_pnl)} note="Not genuine earnings" /><Metric label="Open orders" value={records(workspace?.open_orders).length} /><Metric label="Provider requests" value={text(workspace?.provider_requests, "0")} /></div><Panel title="Authority boundary"><KeyValueGrid entries={[["Venue", text(account.venue)], ["Account type", text(account.account_type)], ["PAPER authority", text(workspace?.paper_authority)], ["LIVE authority", text(workspace?.live_authority)], ["Genuine financial mutation", String(Boolean(workspace?.genuine_financial_mutation))], ["Warnings", machineItems(workspace?.warnings)]]} /></Panel><Panel title="Balances"><div className="scroll"><table><thead><tr><th>Asset</th><th>Total</th><th>Reserved</th></tr></thead><tbody>{records(accountState.balances).map((item) => <tr key={text(item.asset)}><td>{text(item.asset)}</td><td>{decimal(item.total)}</td><td>{decimal(item.reserved)}</td></tr>)}</tbody></table></div>{!records(accountState.balances).length && <Empty title="No PAPER assets" text="The account currently holds hypothetical cash only." />}</Panel><Panel title="Authorizations and previews"><KeyValueGrid entries={[["Exact authorizations", authorizations.length], ["Immutable previews", previews.length], ["Operating-cycle seam", text(record(workspace?.operating_cycle_seam)?.reason)]]} /></Panel><Panel title="PAPER orders"><div className="scroll"><table><thead><tr><th>Order / product</th><th>Side / type</th><th>State</th><th>Filled base</th><th>Filled quote</th><th>Fees</th></tr></thead><tbody>{orders.map((item) => <tr key={text(item.order_id)}><td>{text(item.order_id)}<small>{text(item.product_id)}</small></td><td>{text(item.side)} / {text(item.order_type)}</td><td><span className={stateClass(item.state)}>{text(item.state)}</span></td><td>{decimal(item.filled_base_quantity)}</td><td>{decimal(item.filled_quote_quantity)}</td><td>{decimal(item.cumulative_fees)}</td></tr>)}</tbody></table></div>{!orders.length && <Empty title="No PAPER orders" text="The exact account has retained no internal submissions." />}</Panel><Panel title="Simulated fills and positions"><KeyValueGrid entries={[["Fills", fills.length], ["Positions", positions.length], ["Latest reconciliation", text(reconciliations.at(-1)?.status, "None retained")]]} /></Panel></>}</>;
}

function KalshiOperatingWorkspace() {
  const [accountId, setAccountId] = useState("apex-kalshi-paper-fixture-v1");
  const [workspace, setWorkspace] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  const inspect = async () => {
    if (!accountId.trim()) {
      setError(new ApiError("Enter one exact Kalshi PAPER account ID.", "KALSHI_PAPER_ACCOUNT_ID_REQUIRED"));
      return;
    }
    try {
      setWorkspace(await api<ApiRecord>(`/api/v1/kalshi-operating/workspace/${encodeURIComponent(accountId.trim())}`));
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  };
  const capability = record(workspace?.provider_capability);
  const refresh = record(workspace?.latest_refresh);
  const state = record(workspace?.paper_state);
  const account = record(workspace?.paper_account);
  const opportunities = records(workspace?.current_opportunities);
  const orders = records(state?.orders);
  const fills = records(state?.fills);
  const positions = records(state?.positions);
  const settlements = records(state?.settlements);
  const reconciliations = records(state?.reconciliations);
  return <><div className="section-head"><div><p className="eyebrow">BOUNDED PUBLIC GET / INTERNAL SIMULATION</p><h1>Kalshi Operating</h1><p className="muted">Current public evidence and simulated PAPER state remain separate. PAPER profit or loss is not real earnings, and LIVE authority is unavailable.</p></div><span className="badge">PAPER ONLY · LIVE NONE</span></div><ErrorNotice error={error} /><Panel title="Exact retained workspace"><div className="control-row"><label>Internal PAPER account ID<input value={accountId} onChange={(event) => setAccountId(event.currentTarget.value)} /></label><button className="primary" onClick={() => void inspect()}>Inspect retained state</button></div></Panel>{workspace && <><div className="metrics"><Metric label="Capability" value={text(capability?.primary_state, "UNAVAILABLE")} /><Metric label="Markets evaluated" value={text(refresh?.markets_evaluated, "0")} /><Metric label="Current Opportunities" value={opportunities.length} /><Metric label="PAPER cash" value={decimal(state?.cash)} note="Hypothetical USD" /><Metric label="Reserved PAPER cash" value={decimal(state?.reserved_cash)} /><Metric label="Realized PAPER P/L" value={decimal(state?.realized_paper_pnl)} note="Not genuine earnings" /></div><Panel title="Provider and refresh boundary"><KeyValueGrid entries={[["Capability states", machineItems(capability?.states)], ["Credentials requested", text(capability?.credentials_requested, "false")], ["Refresh ID", text(refresh?.refresh_id, "none")], ["Evidence class", text(refresh?.evidence_class, "none")], ["Provider requests", text(refresh?.provider_requests, "0")], ["Provider writes", text(refresh?.provider_writes, "0")], ["Warnings", machineItems(refresh?.warnings)]]} /></Panel><Panel title="Current Kalshi Opportunities"><div className="scroll"><table><thead><tr><th>Market / side</th><th>Price / maximum loss</th><th>Liquidity</th><th>Eligibility</th><th>Expiration</th></tr></thead><tbody>{opportunities.map((item) => <tr key={text(item.opportunity_id)}><td>{text(item.market_ticker)} / {text(item.side)}<small>{text(item.evaluation_id)}</small></td><td>{decimal(item.price)} / {decimal(item.maximum_loss)}<small>fee {text(item.modeled_fee)}</small></td><td>{text(item.visible_liquidity)} contracts</td><td>{text(item.eligibility)}<small>{list(item.rejection_reasons).join(", ") || "none"}</small></td><td>{formatTimestamp(item.expires_at)}</td></tr>)}</tbody></table></div>{!opportunities.length && <Empty title="No current Kalshi Opportunities" text="No exact retained refresh is available for this workspace." />}</Panel><Panel title="Internal Kalshi PAPER"><KeyValueGrid entries={[["Account", text(account?.account_id, "none")], ["Authorizations / previews", `${records(state?.authorizations).length} / ${records(state?.previews).length}`], ["Orders / fills", `${orders.length} / ${fills.length}`], ["Positions / settlements", `${positions.length} / ${settlements.length}`], ["Modeled fees", text(state?.modeled_fees, "0")], ["Latest reconciliation", text(reconciliations.at(-1)?.status, "none")], ["PAPER authority", text(workspace?.paper_live_separation && record(workspace.paper_live_separation)?.paper_authority, "PAPER_ONLY")], ["LIVE authority", "NONE"]]}/><p className="disabled-explanation">All mutations require exact-ID CLI commands and explicit operator PAPER authorization. This read-only workspace exposes no provider or LIVE control.</p></Panel></>}</>;
}

function App() {
  const [view, setView] = useState<View>("Home");
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [selectedCampaign, setSelectedCampaign] = useState<string | null>(null);
  const [selectedOpportunity, setSelectedOpportunity] = useState<string | null>(null);
  const [selectedStrategy, setSelectedStrategy] = useState<string | null>(null);
  const [portfolioTab, setPortfolioTab] = useState<PortfolioTab>("Reservations");
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const refresh = useCallback(async () => { try { const [summary, service] = await Promise.all([api<Overview>("/api/v1/overview"), api<Health>("/api/v1/health")]); const loaded = Array.isArray(summary.campaigns) ? summary.campaigns : []; setOverview(summary); setCampaigns(loaded); setHealth(service); setError(null); } catch (reason) { setError(reason); } }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  // Static demo only: open on the populated campaign command center instead of an empty Home brief.
  const demoOpened = useRef(false);
  useEffect(() => {
    if (!DEMO_MODE || demoOpened.current || campaigns.length === 0) return;
    demoOpened.current = true;
    const preferred = campaigns.find((item) => item.name.includes("evidence-review")) ?? campaigns[0];
    setSelectedCampaign(preferred.campaign_id);
    setView("Command");
  }, [campaigns]);
  const openCampaign = (id: string) => { setSelectedCampaign(id); setView("Command"); };
  const openPortfolio = (tab: PortfolioTab = "Reservations") => { setPortfolioTab(tab); setView("Portfolio"); };
  const created = async (campaignId: string, replay: boolean) => { await refresh(); setNotice(`Campaign ${replay ? "loaded from its idempotent replay" : "created"}. Authoritative local state is now displayed.`); openCampaign(campaignId); };
  if (operatorNavigation.includes(String(view) as OperatorView)) return <main><a className="skip-link" href="#main-content">Skip to content</a><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">RESEARCH / PAPER ONLY · LIVE NONE</div><div className="status">DB {health?.database_ready ? "READY" : health ? "UNAVAILABLE" : "CHECKING"}</div></header><div className="layout"><nav aria-label="Operator Console job navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content" id="main-content"><OperatorConsolePage view={view as OperatorView} navigate={(destination) => setView(destination as View)} /></section></div></main>;
  if (view === "Strategy Lab") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">RESEARCH ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><StrategyLab /></section></div></main>;
  if (view === "Research Integrity") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">RESEARCH EVIDENCE ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><ResearchIntegrity /><AutomatedCandidateSearches /></section></div></main>;
  if (view === "Strategy Intelligence") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">RESEARCH ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><StrategyIntelligence /></section></div></main>;
  if (view === "Ensemble and Meta-Strategy") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">RESEARCH ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><EnsembleMetaStrategy /></section></div></main>;
  if (view === "Securities") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">SECURITIES PAPER — SIMULATED CAPITAL ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><Securities /></section></div></main>;
  if (view === "Evidence Reviews") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">SIMULATION / PAPER ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><EvidenceReviews campaignId={selectedCampaign} /><EvidenceReviewControls campaignId={selectedCampaign} /></section></div></main>;
  if (view === "Command Center") return <main><header><div className="brand">APEX <span>COMMAND CENTER</span></div><div className="badge">RESEARCH / OBSERVATION ONLY</div><div className="status">DB {health?.database_ready ? "READY" : health ? "UNAVAILABLE" : "CHECKING"}</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><ApexCommandCenter /></section></div></main>;
  if (view === "Broad Market") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">RESEARCH ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><BroadMarketWorkspace /><HistoricalReplayWorkspace /><ResearchMethodologyWorkspace /><CrossMarketResearchWorkspace /></section></div></main>;
  if (view === "Crypto Research") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">HISTORICAL RESEARCH ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><CryptoResearchWorkspace /><MultiAssetCryptoWorkspace /><SharedOpportunityInbox /><OperatingCycleWorkspace /><GenericCampaignCandidateWorkspace /></section></div></main>;
  if (view === "Crypto PAPER") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">CRYPTO SPOT PAPER — SIMULATED CAPITAL ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><CryptoPaperWorkspace /></section></div></main>;
  if (view === "Kalshi Operating") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">KALSHI PAPER — SIMULATED CAPITAL ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><KalshiOperatingWorkspace /></section></div></main>;
  if (view === "Options Intelligence") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">DEFINED-RISK RESEARCH ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><OptionsIntelligenceWorkspace /></section></div></main>;
  if (view === "Portfolio Intelligence") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">RESEARCH ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><PortfolioIntelligenceView /></section></div></main>;
  if (view === "Securities Operations") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">OBSERVATION ONLY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><SecuritiesOperationsView /></section></div></main>;
  if (view === "External PAPER") return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">PAPER ONLY — NO LIVE AUTHORITY</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}</nav><section className="content"><ExternalPaperView /></section></div></main>;
  return <main><header><div className="brand">APEX <span>OPERATOR CONSOLE</span></div><div className="badge">SIMULATION / PAPER ONLY</div><div className="status">DB {health?.database_ready ? "READY" : health ? "UNAVAILABLE" : "CHECKING"} · {campaigns.filter((item) => item.state === "RUNNING").length} RUNNING</div></header><div className="layout"><nav aria-label="Operator Console navigation">{navigation.map((item) => <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>)}{selectedCampaign && <button className={view === "Command" ? "active" : ""} onClick={() => setView("Command")}>Command Center</button>}</nav><section className="content"><Notice value={notice} /><ErrorNotice error={error} />{view === "Overview" && <OverviewView overview={overview} campaigns={campaigns} openCampaign={openCampaign} />}{view === "Campaigns" && <><div className="section-head"><div><p className="eyebrow">LOCAL CAPITAL OPERATIONS</p><h1>Campaigns</h1></div><CreateCampaign onCreated={(id, replay) => void created(id, replay)} /></div><CampaignTable campaigns={campaigns} openCampaign={openCampaign} /></>}{view === "Command" && selectedCampaign && <CommandCenter campaignId={selectedCampaign} openOpportunity={(id) => { setSelectedOpportunity(id); setView("Opportunity"); }} openPortfolio={openPortfolio} onBack={() => setView("Campaigns")} setNotice={setNotice} />}{view === "Command" && !selectedCampaign && <Empty title="No Campaign selected" text="Choose a Campaign to open its local command center." />}{view === "Opportunity" && selectedOpportunity && <OpportunityDetail opportunityId={selectedOpportunity} onBack={() => setView("Command")} openPortfolio={() => openPortfolio("Orders")} />}{view === "Portfolio" && selectedCampaign && <PortfolioWorkspace campaignId={selectedCampaign} initialTab={portfolioTab} onBack={() => setView("Command")} setNotice={setNotice} />}{view === "Portfolio" && !selectedCampaign && <Empty title="No portfolio selected" text="Choose a Campaign command center before opening its reservations, Orders, Positions, marks, or ledger." />}{view === "Strategies" && <Strategies campaignId={selectedCampaign} openStrategy={(id) => { setSelectedStrategy(id); setView("Strategy"); }} />}{view === "Strategy" && selectedStrategy && <StrategyDetail strategyId={selectedStrategy} campaignId={selectedCampaign} onBack={() => setView("Strategies")} />}{view === "Shadow Operations" && <ShadowOperations campaigns={campaigns} />}{view === "Activity" && <Activity campaigns={campaigns} />}</section></div></main>;
}

createRoot(document.getElementById("root")!).render(DEMO_MODE ? <><DemoBanner /><DemoShell console={<App />} /></> : <App />);
