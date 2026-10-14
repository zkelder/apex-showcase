import { ReactNode, useEffect, useState } from "react";

import { ApiError, ApiRecord, api, formatTimestamp } from "./api";
import { explain } from "./reasonCodes";

// Shared presentation primitives for the consolidated Operator Console
// (operatorConsole.tsx and its sibling view modules: decisionLedger.tsx,
// followUp.tsx, optionsResearch.tsx, portfolioMandate.tsx, operations.tsx).
// Extracted unchanged from operatorConsole.tsx so every console page keeps
// one visual language -- restrained, evidence-first, no new styling rules
// introduced here. Presentation/formatting only: no eligibility, mandate,
// classification, follow-up, options-coverage, or scheduling logic lives in
// this file or any file that imports it.

export { formatTimestamp };
export type { ApiRecord };

export type InspectEvidence = (href: string, title: string) => void;

export function text(value: unknown, fallback = "—"): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
}

export function record(value: unknown): ApiRecord | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as ApiRecord) : null;
}

export function records(value: unknown): ApiRecord[] {
  return Array.isArray(value)
    ? value.filter((item): item is ApiRecord => Boolean(item) && typeof item === "object")
    : [];
}

export function list(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => text(item)) : [];
}

/** Renders a list of stable machine reason/limitation codes as a calm
 * human phrase, with the exact raw code(s) always kept visible in a
 * trailing <small> -- never one without the other. See reasonCodes.ts:
 * explain() covers a curated set of codes exactly; anything else still
 * gets a mechanical (not invented) reformatting rather than a raw,
 * unbroken token. `emptyText` lets a call site keep its own precise
 * empty-state word (e.g. "clear" for a blockers list, vs. "none"). */
export function ReasonList({ codes, emptyText = "none" }: { codes: unknown; emptyText?: string }) {
  const clean = list(codes);
  if (!clean.length) return <>{emptyText}</>;
  return (
    <>
      {clean.map((code) => explain(code)).join(" · ")}
      <small>{clean.join(" · ")}</small>
    </>
  );
}

export function pairs(value: unknown): Array<[string, string]> {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is [string, string] => Array.isArray(item) && item.length === 2)
    .map((item) => [text(item[0]), text(item[1])]);
}

/** A plain retained count map -- e.g. `{"CRYPTO": 5, "KALSHI": 3}`, the
 * shape `counterfactual_followup`/`counterfactual_ledger` repositories'
 * own `coverage()` breakdowns (`observation_count_by_venue` and siblings)
 * actually serialize as. Distinct from `pairs()`, which expects an
 * already-ordered array of `[key, value]` tuples instead (e.g.
 * `PlanFollowUpObservations.state_counts`) -- passing a plain object to
 * `pairs()`, or an array to `counts()`, both fail closed to an empty list
 * rather than a crash, so picking the one that actually matches the
 * backend field's own return type matters. */
export function counts(value: unknown): Array<[string, string]> {
  const map = record(value);
  if (!map) return [];
  return Object.entries(map)
    .filter((entry): entry is [string, string | number] => typeof entry[1] === "string" || typeof entry[1] === "number")
    .map(([key, count]) => [key, text(count)] as [string, string]);
}

export function stateClass(value: unknown): string {
  return `state state-${text(value, "unknown").toLowerCase()}`;
}

export function boolText(value: unknown): string {
  return value === true ? "yes" : value === false ? "no" : text(value);
}

/** Value objects such as RiskFraction serialize as { value: "0.5" } rather
 * than a bare string; unwrap that one known shape defensively, falling back
 * to plain text() for anything else so a format change never renders "—"
 * silently. */
export function fraction(value: unknown): string {
  const wrapped = record(value);
  return wrapped ? text(wrapped.value) : text(value);
}

export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  const known = error instanceof ApiError ? error : null;
  return (
    <div className="notice error" role="alert">
      <strong>{known?.code ?? "UNEXPECTED_FAILURE"}</strong>
      <span>{known?.message ?? "The retained view could not be loaded."}</span>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="empty">
      <h2>{title}</h2>
      <p>{children}</p>
    </div>
  );
}

/** Shown only before a page's first response resolves (or after a
 * subsequent refresh is in flight) -- honest, motion-free text, never a
 * spinner or a status implied from the browser's own clock. aria-live
 * announces its appearance and removal to assistive technology, since
 * nothing else on these pages currently does. */
export function Loading({ label = "Loading retained state…" }: { label?: string }) {
  return (
    <p className="muted" aria-live="polite">
      {label}
    </p>
  );
}

const AUTHORITY_STAGES = [
  { key: "OBSERVATION", label: "Observation / research", detail: "Always active — reads and records evidence" },
  {
    key: "RECOMMENDATION",
    label: "Recommendation",
    detail: "Always active — ranks Opportunities and drafts research proposals for operator review",
  },
] as const;

/** Answers "what execution authority does Apex currently have?" as one
 * unmistakable, honest ladder rather than the scattered ad hoc badges
 * every page already carries. Grounded only in fields the operator-console
 * read model actually returns: `paperAccountCount` is the real retained
 * count of internal PAPER accounts (zero is shown honestly, not hidden);
 * LIVE is fixed, doctrine-level copy because the read model's own
 * `live_authority`/`live_enabled` fields are hardcoded `NONE`/`false`
 * everywhere in this codebase -- there is no per-request value to fetch
 * that could ever say otherwise. Never implies an order was or could be
 * placed. */
export function AuthorityLadder({ paperAccountCount }: { paperAccountCount: unknown }) {
  const count = Number(paperAccountCount ?? 0);
  const paperDetail =
    Number.isFinite(count) && count > 0
      ? `${count} internal simulated account(s) retained — simulated capital only, never genuine earnings`
      : "No internal PAPER account retained yet — simulated capital only when one exists, never genuine earnings";
  return (
    <dl className="key-values authority-ladder">
      {AUTHORITY_STAGES.map((stage) => (
        <div key={stage.key}>
          <dt>{stage.label}</dt>
          <dd>{stage.detail}</dd>
        </div>
      ))}
      <div>
        <dt>PAPER (simulated)</dt>
        <dd>{paperDetail}</dd>
      </div>
      <div className="authority-live-off">
        <dt>LIVE (real capital)</dt>
        <dd>NONE — not enabled in this build, no provider order submission</dd>
      </div>
    </dl>
  );
}

/** A textual, non-decorative "N at this stage" strip -- deliberately not a
 * chart. Used to make Opportunity scarcity explainable (e.g. "considered
 * 12 · eligible 0 · blocked 9 · rejected 3"): a zero should read as a
 * counted, explained outcome, never a blank space that looks broken. */
export function StageCounts({ label, stages }: { label: string; stages: Array<[string, number]> }) {
  return (
    <div className="stage-counts">
      <p className="muted">{label}</p>
      <div className="stage-counts-row">
        {stages.map(([name, count]) => (
          <span className="stage-count" key={name}>
            <strong>{count}</strong> {name}
          </span>
        ))}
      </div>
    </div>
  );
}

export function Panel({ title, children, badge }: { title: string; children: ReactNode; badge?: string }) {
  return (
    <article className="panel">
      <div className="panel-title">
        <h2>{title}</h2>
        {badge && <span className="badge">{badge}</span>}
      </div>
      {children}
    </article>
  );
}

export function EvidenceButton({
  href,
  label,
  inspect,
}: {
  href: unknown;
  label: string;
  inspect: InspectEvidence;
}) {
  if (typeof href !== "string" || !href) return <span className="muted">No exact evidence route</span>;
  return (
    <button className="quiet" onClick={() => inspect(href, label)}>
      Inspect evidence
    </button>
  );
}

/** `id`/`tabIndex=-1` on the heading give OperatorConsolePage's
 * navigate-focus effect (see operatorConsole.tsx) a stable, programmatically
 * focusable target: on every job-navigation change, focus moves to the new
 * page's own heading so a keyboard/screen-reader user lands on its content
 * rather than staying wherever focus happened to be on the old page. */
export function PageHead({
  eyebrow,
  title,
  detail,
  refresh,
}: {
  eyebrow: string;
  title: string;
  detail: string;
  refresh?: () => void;
}) {
  return (
    <div className="section-head">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1 id="console-page-title" tabIndex={-1}>
          {title}
        </h1>
        <p className="muted">{detail}</p>
      </div>
      {refresh && <button onClick={refresh}>Refresh retained state</button>}
    </div>
  );
}

export function Metrics({ values }: { values: Array<[string, unknown, string?]> }) {
  return (
    <div className="metrics">
      {values.map(([label, value, note]) => (
        <article className="metric" key={label}>
          <p>{label}</p>
          <strong>{text(value)}</strong>
          {note && <small>{note}</small>}
        </article>
      ))}
    </div>
  );
}

export function EvidenceDrawer({
  selected,
  close,
}: {
  selected: { href: string; title: string } | null;
  close: () => void;
}) {
  const [value, setValue] = useState<ApiRecord | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (!selected) return;
    let active = true;
    setValue(null);
    api<ApiRecord>(selected.href)
      .then((loaded) => {
        if (active) {
          setValue(loaded);
          setError(null);
        }
      })
      .catch((reason: unknown) => {
        if (active) setError(reason);
      });
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", key);
    return () => {
      active = false;
      window.removeEventListener("keydown", key);
    };
  }, [selected, close]);
  if (!selected) return null;
  return (
    <div
      className="evidence-drawer-backdrop"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) close();
      }}
    >
      <aside className="evidence-drawer" role="dialog" aria-modal="true" aria-labelledby="evidence-title">
        <div className="panel-title">
          <div>
            <p className="eyebrow">EXACT RETAINED RECORD</p>
            <h2 id="evidence-title">{selected.title}</h2>
          </div>
          <button className="quiet" autoFocus onClick={close} aria-label="Close evidence drawer">
            Close
          </button>
        </div>
        <p className="stable-id">{selected.href}</p>
        <ErrorNotice error={error} />
        {value ? <pre>{JSON.stringify(value, null, 2)}</pre> : !error && <p className="muted">Loading retained evidence…</p>}
      </aside>
    </div>
  );
}

export function Pagination({
  offset,
  total,
  setOffset,
}: {
  offset: number;
  total: number;
  setOffset: (value: number) => void;
}) {
  return (
    <div className="pagination">
      <button className="quiet" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>
        Previous
      </button>
      <span>{total ? `${offset + 1}–${Math.min(offset + 25, total)} of ${total}` : "0 results"}</span>
      <button className="quiet" disabled={offset + 25 >= total} onClick={() => setOffset(offset + 25)}>
        Next
      </button>
    </div>
  );
}
