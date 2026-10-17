import { MouseEvent, ReactNode, useState } from "react";
import { FixtureOutcomesPage } from "./fixtureOutcomes";

/** Labels of buttons that would change state in the live console. */
const MUTATING_LABEL =
  /^(run|pause|resume|kill|create|submit|approve|reject|authorize|activate|revoke|save|start|stop|flatten|cancel|register|record|apply|confirm|decide|promote|retire|deploy)\b/i;

/** Demo-only shell: a small switch between the deterministic outcomes and the real console. */
export function DemoShell({ console: consoleView }: { console: ReactNode }) {
  const [tab, setTab] = useState<"outcomes" | "console">("outcomes");
  const [notice, setNotice] = useState("");

  // Defence in depth: api.ts already rejects every non-GET request in demo mode; this makes the
  // refusal visible before any handler runs.
  const guard = (event: MouseEvent<HTMLDivElement>) => {
    const button = (event.target as HTMLElement).closest("button");
    if (!button || button.closest(".demo-tabs")) return;
    const label = (button.textContent ?? "").trim();
    if (MUTATING_LABEL.test(label)) {
      event.preventDefault();
      event.stopPropagation();
      setNotice(`"${label}" is disabled: this is a read-only synthetic demonstration.`);
    }
  };

  return (
    <div className="demo-shell" onClickCapture={guard}>
      {/* One skip link per view: the outcomes view owns main#main; the console view is wrapped in #demo-console. */}
      <a className="skip-link" href={tab === "outcomes" ? "#main" : "#demo-console"}>Skip to content</a>
      <nav className="demo-tabs" aria-label="Demonstration sections">
        <button aria-pressed={tab === "outcomes"} onClick={() => setTab("outcomes")}>Deterministic outcomes</button>
        <button aria-pressed={tab === "console"} onClick={() => setTab("console")}>Operator Console (retained synthetic evidence)</button>
      </nav>
      {notice && (
        <div className="demo-notice" role="status">
          {notice} <button onClick={() => setNotice("")}>Dismiss</button>
        </div>
      )}
      {tab === "console" && (
        <p className="demo-notice" role="note" data-testid="demo-empty-note">
          Empty states are expected: this snapshot was seeded only by the fixture-only governed PAPER
          demonstration. Views that say "no retained …", "not run", "never run" or show zeros (Home, Opportunities,
          Follow-Up, Options Research, Allocation, Live Readiness) were not exercised, because there is no
          scheduler, provider, or live data behind this page. The Campaign command center and its
          portfolio views are the populated part.
        </p>
      )}
      {tab === "outcomes" ? <main id="main" tabIndex={-1}><FixtureOutcomesPage /></main> : <div id="demo-console" tabIndex={-1}>{consoleView}</div>}
    </div>
  );
}
