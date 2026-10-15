import { DemoLocation, goDemo } from "./demoNav";

const SOURCE = "https://github.com/zkelder/apex-showcase/blob/main";
export const CASE_STUDY_URL = "/apex/";
export const REPO_URL = "https://github.com/zkelder/apex-showcase";

export type GuideStep = { label: string; note: string; to: DemoLocation; anchor?: string };
export type GuidePath = {
  id: string;
  title: string;
  tagline: string;
  demonstrates: string;
  steps: GuideStep[];
  code: { label: string; href: string }[];
};

export const GUIDE_PATHS: GuidePath[] = [
  {
    id: "outcomes",
    title: "Deterministic outcomes",
    tagline: "Eight decisions, including the refusals, reproduced byte for byte.",
    demonstrates: "Determinism and exact accounting",
    steps: [
      { label: "A settled trade", note: "Every ledger posting is a balanced debit and credit.", to: { kind: "outcomes", fixture: "golden" }, anchor: "Double-entry ledger" },
      { label: "Rejected by the risk kernel", note: "A refusal is retained and inspectable, and creates no order.", to: { kind: "outcomes", fixture: "invariant-rejected" }, anchor: "Decision pipeline" },
      { label: "Partial fill, settled VOID", note: "Only the filled share settles; the canceled order is untouched.", to: { kind: "outcomes", fixture: "partial-fill-canceled-settled-void" }, anchor: "Double-entry ledger" },
    ],
    code: [
      { label: "Fixtures", href: `${SOURCE}/fixtures` },
      { label: "Determinism tests", href: `${SOURCE}/tests/security/test_no_live_path.py` },
      { label: "Ledger", href: `${SOURCE}/src/apex/domain/ledger.py` },
    ],
  },
  {
    id: "paper",
    title: "Governed PAPER campaign",
    tagline: "A simulated campaign from authorization to accounting.",
    demonstrates: "Auditability and exact state",
    steps: [
      { label: "Campaign command center", note: "Funded capital, loss limit, and lifecycle state.", to: { kind: "console", view: "Command" } },
      { label: "Opportunity queue", note: "Every retained opportunity stays visible, with its reason codes.", to: { kind: "console", view: "Command" }, anchor: "Opportunity queue" },
      { label: "Portfolio summary", note: "Orders, positions, and risk derived from retained state.", to: { kind: "console", view: "Command" }, anchor: "Portfolio summary" },
      { label: "Activity timeline", note: "Each operator action and cycle is recorded with its result.", to: { kind: "console", view: "Command" }, anchor: "Activity timeline" },
    ],
    code: [
      { label: "Operator API", href: `${SOURCE}/src/apex/operator_api.py` },
      { label: "Ledger accounting tests", href: `${SOURCE}/tests/test_crypto_paper_ledger_close_accounting.py` },
    ],
  },
  {
    id: "gates",
    title: "Authority and safety gates",
    tagline: "How the console refuses to act, and says why.",
    demonstrates: "Fail-closed authority",
    steps: [
      { label: "PAPER Operations", note: "The PAPER capability gate is disabled until an operator activates it.", to: { kind: "console", view: "PAPER Operations" } },
      { label: "Live Readiness", note: "LIVE_NOT_READY, with the prohibited actions listed explicitly.", to: { kind: "console", view: "Live Readiness" } },
      { label: "Try a control", note: "Run, Pause and Resume are disabled (the campaign has expired on the fixture clock). Press Kill: the demo rejects it and says why.", to: { kind: "console", view: "Command" }, anchor: "Campaign controls" },
    ],
    code: [
      { label: "Safety model", href: `${SOURCE}/docs/SAFETY_MODEL.md` },
      { label: "Boundary tests", href: `${SOURCE}/tests/security/test_architecture_boundaries.py` },
    ],
  },
];

export function DemoGuide() {
  return (
    <main className="demo-guide" aria-labelledby="demo-guide-title">
      <p className="guide-eyebrow">Interactive demonstration</p>
      <h1 id="demo-guide-title">Explore the Apex Operator Console on synthetic data</h1>
      <p className="guide-lead">
        Apex is a local-first research platform: it observes markets, evaluates strategies against retained evidence, and
        runs governed paper simulations. This page is a frozen snapshot of its real React console and fixture services.
        Pick a path below, or open the console directly.
      </p>
      <dl className="guide-facts">
        <div><dt>What is real</dt><dd>The React console, the API response shapes, and the deterministic outcomes produced by Apex's own domain, risk and ledger code.</dd></div>
        <div><dt>What is synthetic</dt><dd>Every instrument, price and balance. A throwaway database was seeded only by Apex's fixture services; it is not production data.</dd></div>
        <div><dt>What is disabled</dt><dd>Every write. There is no backend, account, credential or market connection, and the page makes no API calls.</dd></div>
      </dl>
      <h2 className="guide-heading">Choose a path</h2>
      <ol className="guide-paths">
        {GUIDE_PATHS.map((path, index) => (
          <li key={path.id} className="guide-path">
            <span className="guide-number" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
            <h3>{path.title}</h3>
            <p>{path.tagline}</p>
            <p className="guide-demonstrates"><span>Demonstrates</span> {path.demonstrates}</p>
            <ol className="guide-steps">
              {path.steps.map((step, stepIndex) => (
                <li key={step.label}>
                  <a
                    href={`#path/${path.id}/${stepIndex + 1}`}
                    onClick={(event) => {
                      event.preventDefault();
                      goDemo({ kind: "path", path: path.id, step: stepIndex + 1 });
                    }}
                  >
                    {step.label}
                  </a>
                  <small>{step.note}</small>
                </li>
              ))}
            </ol>
            <p className="guide-code">
              {path.code.map((link) => (
                <a key={link.href} href={link.href} rel="noopener">{link.label} ↗</a>
              ))}
            </p>
          </li>
        ))}
      </ol>
      <details className="guide-empty" data-testid="demo-empty-note">
        <summary>Why are some console views empty?</summary>
        <p>
          The snapshot was seeded only by the fixture governed-PAPER demonstration. Home, Opportunities, Follow-Up, Options
          Research, Allocation and Operations show empty states because no scheduler, provider or recurring-cycle history
          exists behind this page. They are labelled honestly rather than filled with invented data. Runner leases, crash
          recovery and the schema guard are explained in the case study and covered by the persistence tests.
        </p>
      </details>
      <p className="guide-links">
        <a href={CASE_STUDY_URL}>← Back to the case study</a>
        <a href={REPO_URL} rel="noopener">Source on GitHub ↗</a>
        <a href="#console" onClick={(event) => { event.preventDefault(); goDemo({ kind: "console" }); }}>Open the full console →</a>
      </p>
    </main>
  );
}
