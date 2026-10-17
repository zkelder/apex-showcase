import { KeyboardEvent, useEffect, useState } from "react";

type Json = Record<string, unknown>;
type Outcome = { fixture: string; title: string; report_hash: string; report: Json };
type Document = { seed: number; capital: string; outcomes: Outcome[] };

const money = (value: unknown): string => {
  if (value && typeof value === "object" && "amount" in value) return String((value as { amount: unknown }).amount);
  if (value && typeof value === "object" && "value" in value) return String((value as { value: unknown }).value);
  return value === undefined || value === null ? "—" : String(value);
};
const asArray = (value: unknown): Json[] => (Array.isArray(value) ? (value as Json[]) : []);
const text = (value: unknown): string => (value === undefined || value === null ? "—" : String(value));
const short = (value: string): string => `${value.slice(0, 12)}…${value.slice(-6)}`;

const OUTCOME_TONE: Record<string, string> = {
  SETTLED_TRADE: "ok",
  NO_PROPOSAL: "neutral",
  POLICY_REJECTED: "warn",
  INVARIANT_REJECTED: "warn",
  UNFILLED_EXPIRED: "neutral",
  PARTIAL_FILL_CANCELED: "neutral",
  PARTIAL_FILL_CANCELED_SETTLED_NO: "ok",
  PARTIAL_FILL_CANCELED_SETTLED_VOID: "ok",
};

function stages(report: Json): { stage: string; detail: string }[] {
  const out: { stage: string; detail: string }[] = [];
  const evaluation = report.strategy_evaluation as Json | undefined;
  out.push({
    stage: "Strategy",
    detail: evaluation ? `${text(evaluation.outcome)} (${asArray(evaluation.reason_codes).join(", ") || text((evaluation.reason_codes as unknown[] | undefined)?.join(", "))})` : "—",
  });
  const allocation = report.allocation as Json | undefined;
  if (allocation) out.push({ stage: "Allocation", detail: `${allocation.accepted ? "accepted" : "rejected"}; quantity ${money((allocation.quantity as Json | undefined)?.value ?? allocation.quantity)}; max loss ${money(allocation.maximum_loss)}` });
  if (report.policy_reason_codes) out.push({ stage: "Campaign policy", detail: `${text((report.policy_reason_codes as unknown[]).join(", "))} — ${text(report.policy_explanation)}` });
  const risk = report.risk_decision as Json | undefined;
  if (risk) out.push({ stage: "Risk kernel", detail: `${risk.accepted ? "accepted" : "rejected"}: ${(risk.reason_codes as unknown[] | undefined)?.join(", ") ?? "—"}` });
  if (report.invariant_explanation) out.push({ stage: "Invariant", detail: text(report.invariant_explanation) });
  const order = report.order as Json | undefined;
  if (order) out.push({ stage: "Order", detail: `${text(order.state)}; filled ${money(order.filled_quantity)} of ${money(order.quantity)}` });
  const events = asArray(report.execution_events);
  if (events.length) out.push({ stage: "Execution", detail: events.map((event) => text(event.event_type)).join(" → ") });
  if (report.final_reservation) out.push({ stage: "Reservation", detail: text(((report.final_reservation as Json).state as unknown) ?? report.final_reservation) });
  if (report.settlement_result) out.push({ stage: "Settlement", detail: `${text(report.settlement_result)}; cash ${money(report.settlement_cash)}` });
  else if (report.settlement) out.push({ stage: "Settlement", detail: `realized ${text((report.settlement as Json).realized_pnl)}; cash ${money((report.settlement as Json).settlement_cash)}` });
  return out;
}

function Journals({ report }: { report: Json }) {
  const journals = asArray(report.ledger_journals);
  if (!journals.length) return <p className="muted">No ledger journals beyond the opening contribution.</p>;
  return (
    <div className="scroll" tabIndex={0} role="region" aria-label="Double-entry ledger journals (scrolls horizontally on small screens)">
    <table>
      <thead>
        <tr><th>Journal</th><th>Posting</th><th>Side</th><th>Amount (USD)</th></tr>
      </thead>
      <tbody>
        {journals.flatMap((journal) =>
          asArray(journal.postings).map((posting, index) => (
            <tr key={`${text(journal.journal_id)}-${index}`}>
              <td>{index === 0 ? text(journal.journal_type) : ""}</td>
              <td>{text(posting.account_code)}</td>
              <td>{text(posting.side)}</td>
              <td>{money(posting.amount)}</td>
            </tr>
          )),
        )}
      </tbody>
    </table>
    </div>
  );
}

export function FixtureOutcomesPage() {
  const [document, setDocument] = useState<Document | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState("golden");
  useEffect(() => {
    fetch(`${import.meta.env.BASE_URL}demo-data/fixture-outcomes.json`)
      .then((response) => response.json() as Promise<Document>)
      .then(setDocument)
      .catch(() => setError("Fixture outcomes could not be loaded."));
  }, []);
  if (error) return <p role="alert">{error}</p>;
  if (!document) return <p>Loading deterministic outcomes…</p>;
  const current = document.outcomes.find((item) => item.fixture === selected) ?? document.outcomes[0];
  const report = current.report;
  const onTabKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const fixtures = document.outcomes.map((item) => item.fixture);
    const index = fixtures.indexOf(current.fixture);
    let next: number | null = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % fixtures.length;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index - 1 + fixtures.length) % fixtures.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = fixtures.length - 1;
    if (next === null) return;
    event.preventDefault();
    setSelected(fixtures[next]);
    window.document.getElementById(`outcome-tab-${fixtures[next]}`)?.focus();
  };
  return (
    <section aria-label="Deterministic fixture outcomes" className="fixture-outcomes">
      <h1 className="fixture-title">Apex in 60 seconds</h1>
      <p>
        Apex is a research and <strong>simulation-only</strong> decision platform for prediction markets and other instruments.
        Every decision passes a fixed pipeline: point-in-time evidence → strategy proposal → allocation → a non-bypassable
        risk kernel → order → execution events → a double-entry ledger. It never trades real money, and each outcome below
        is reproducible to the byte.
      </p>
      <h2>Eight deterministic outcomes</h2>
      <p className="muted">
        Each card is the real output of <code>scripts/dev.py demo-core --fixture … --capital {document.capital} --seed {document.seed}</code>,
        produced by Apex's domain, risk and ledger code with no database and no network. The report hash is pinned by tests; any change in behaviour changes it.
      </p>
      <div className="outcome-grid" role="tablist" aria-label="Fixture outcomes" onKeyDown={onTabKeyDown}>
        {document.outcomes.map((item) => (
          <button
            key={item.fixture}
            id={`outcome-tab-${item.fixture}`}
            role="tab"
            aria-selected={item.fixture === current.fixture}
            aria-controls="outcome-detail"
            tabIndex={item.fixture === current.fixture ? 0 : -1}
            className={`outcome-card tone-${OUTCOME_TONE[text(item.report.run_outcome)] ?? "neutral"} ${item.fixture === current.fixture ? "active" : ""}`}
            onClick={() => setSelected(item.fixture)}
          >
            <strong>{item.title}</strong>
            <span>{text(item.report.run_outcome)}</span>
            <small>net P&L {text(item.report.net_trading_pnl)} · equity {text(item.report.final_ledger_equity)}</small>
          </button>
        ))}
      </div>
      <div className="outcome-detail" id="outcome-detail" role="tabpanel" aria-labelledby={`outcome-tab-${current.fixture}`}>
        <h3>{current.title}</h3>
        <dl className="facts">
          <div><dt>Run outcome</dt><dd>{text(report.run_outcome)}</dd></div>
          <div><dt>Instrument</dt><dd>{text(report.selected_instrument)}</dd></div>
          <div><dt>Fees</dt><dd>{text(report.fees)}</dd></div>
          <div><dt>Net trading P&L</dt><dd>{text(report.net_trading_pnl)}</dd></div>
          <div><dt>Final ledger equity</dt><dd>{text(report.final_ledger_equity)}</dd></div>
          <div><dt>Execution evidence</dt><dd>{text(report.execution_evidence)}</dd></div>
          <div><dt>Edge evidence</dt><dd>{text(report.edge_evidence)}</dd></div>
          <div><dt>Canonical report hash</dt><dd title={current.report_hash}><code>{short(current.report_hash)}</code></dd></div>
        </dl>
        <h4>Decision pipeline</h4>
        <ol className="pipeline">
          {stages(report).map((step) => (
            <li key={step.stage}><strong>{step.stage}</strong><span>{step.detail}</span></li>
          ))}
        </ol>
        <h4>Double-entry ledger</h4>
        <Journals report={report} />
        <details>
          <summary>Full canonical report (JSON)</summary>
          <pre tabIndex={0} aria-label="Full canonical report, JSON">{JSON.stringify(report, null, 2)}</pre>
        </details>
      </div>
    </section>
  );
}
