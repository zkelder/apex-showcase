import { MouseEvent, ReactNode, useEffect, useState } from "react";
import { CASE_STUDY_URL, DemoGuide, GUIDE_PATHS } from "./demoGuide";
import { DemoConsoleRequest, goDemo, useDemoLocation } from "./demoNav";
import { FixtureOutcomesPage } from "./fixtureOutcomes";

/** Labels of buttons that would change state in the live console. */
const MUTATING_LABEL =
  /^(run|pause|resume|kill|create|submit|approve|reject|authorize|activate|revoke|save|start|stop|flatten|cancel|register|record|apply|confirm|decide|promote|retire|deploy)\b/i;

/** Scroll a heading containing `text` into view, waiting for asynchronously rendered content. */
function scrollToHeading(text: string): () => void {
  let frame = 0;
  let settle = 0;
  let attempts = 0;
  const wanted = text.toLowerCase();
  const align = (heading: Element) => {
    heading.scrollIntoView({ block: "start" });
    const banner = document.querySelector(".demo-banner")?.getBoundingClientRect().height ?? 0;
    const rail = document.querySelector(".demo-rail")?.getBoundingClientRect().height ?? 0;
    window.scrollBy(0, -(banner + rail + 24));
  };
  const find = () => {
    const heading = Array.from(document.querySelectorAll("h1, h2, h3, h4")).find((node) =>
      (node.textContent ?? "").toLowerCase().includes(wanted),
    );
    if (heading) {
      align(heading);
      // Content above the target may still be loading; align once more when it has settled.
      settle = window.setTimeout(() => {
        const again = Array.from(document.querySelectorAll("h1, h2, h3, h4")).find((node) =>
          (node.textContent ?? "").toLowerCase().includes(wanted),
        );
        if (again) align(again);
      }, 350);
      return;
    }
    attempts += 1;
    if (attempts < 40) frame = window.requestAnimationFrame(find);
  };
  frame = window.requestAnimationFrame(find);
  return () => {
    window.cancelAnimationFrame(frame);
    window.clearTimeout(settle);
  };
}

/**
 * Make horizontally scrollable table regions keyboard-reachable and named. The shared console markup wraps
 * wide tables in plain scroll containers; keyboard users need to be able to focus them to scroll.
 */
function useScrollRegionAccess(deps: unknown[]) {
  useEffect(() => {
    const apply = () => {
      document.querySelectorAll<HTMLElement>(".scroll, .panel").forEach((element) => {
        const overflows = element.scrollWidth > element.clientWidth + 1;
        const scrolls = ["auto", "scroll"].includes(getComputedStyle(element).overflowX);
        if (!overflows || !scrolls) return;
        if (!element.hasAttribute("tabindex")) element.setAttribute("tabindex", "0");
        if (!element.hasAttribute("role")) element.setAttribute("role", "region");
        if (!element.hasAttribute("aria-label")) {
          const title = element.closest(".panel")?.querySelector("h2, h3")?.textContent?.trim();
          element.setAttribute("aria-label", title ? `${title} (scrollable table)` : "Scrollable table");
        }
      });
    };
    apply();
    const observer = new MutationObserver(apply);
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", apply);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", apply);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

/** Demo-only shell: a guided entry page, the deterministic outcomes, and the real console. */
export function DemoShell({ console: consoleView }: { console: ReactNode }) {
  const location = useDemoLocation();
  const [notice, setNotice] = useState("");
  useScrollRegionAccess([]);

  const path = location.kind === "path" ? GUIDE_PATHS.find((item) => item.id === location.path) : undefined;
  const stepIndex = path && location.kind === "path" ? Math.min(Math.max(location.step, 1), path.steps.length) - 1 : 0;
  const step = path?.steps[stepIndex];
  const effective = step ? step.to : location;
  const section = effective.kind === "outcomes" ? "outcomes" : effective.kind === "console" ? "console" : "guide";
  const fixture = effective.kind === "outcomes" ? effective.fixture : undefined;

  useEffect(() => {
    if (step?.anchor) return scrollToHeading(step.anchor);
    if (step) window.scrollTo(0, 0);
    return undefined;
  }, [location.nonce, step]);

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

  const tabs = [
    { id: "guide", label: "Start here", to: { kind: "guide" } as const },
    { id: "outcomes", label: "Deterministic outcomes", to: { kind: "outcomes" } as const },
    { id: "console", label: "Operator Console", to: { kind: "console" } as const },
  ];

  return (
    <div className="demo-shell" onClickCapture={guard}>
      <nav className="demo-tabs" aria-label="Demonstration sections">
        {tabs.map((tab) => (
          <button key={tab.id} aria-pressed={section === tab.id} onClick={() => goDemo(tab.to)}>{tab.label}</button>
        ))}
        <a className="demo-back" href={CASE_STUDY_URL}>← Case study</a>
      </nav>
      {path && step && (
        <div className="demo-rail" role="navigation" aria-label={`Guided path: ${path.title}`}>
          <p>
            <strong>{path.title}</strong>
            <span>Step {stepIndex + 1} of {path.steps.length}: {step.label}</span>
            <small>{step.note}</small>
          </p>
          <div>
            <button disabled={stepIndex === 0} onClick={() => goDemo({ kind: "path", path: path.id, step: stepIndex })}>← Previous</button>
            <button disabled={stepIndex === path.steps.length - 1} onClick={() => goDemo({ kind: "path", path: path.id, step: stepIndex + 2 })}>Next →</button>
            <button onClick={() => goDemo({ kind: "guide" })}>All paths</button>
          </div>
        </div>
      )}
      {notice && (
        <div className="demo-notice" role="status">
          {notice} <button onClick={() => setNotice("")}>Dismiss</button>
        </div>
      )}
      {section === "guide" && <DemoGuide />}
      {section === "outcomes" && <main><FixtureOutcomesPage selectedFixture={fixture} /></main>}
      {section === "console" && (
        <DemoConsoleRequest.Provider value={{ view: effective.kind === "console" ? effective.view : undefined, nonce: location.nonce }}>
          {consoleView}
        </DemoConsoleRequest.Provider>
      )}
    </div>
  );
}
