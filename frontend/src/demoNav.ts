import { createContext, useEffect, useState } from "react";

/**
 * Demo-only navigation. The URL hash is the single source of truth so every guided step is a
 * shareable deep link (for example `#path/paper/2` or `#outcomes/invariant-rejected`).
 */
export type DemoLocation =
  | { kind: "guide" }
  | { kind: "path"; path: string; step: number }
  | { kind: "outcomes"; fixture?: string }
  | { kind: "console"; view?: string };

export type StampedLocation = DemoLocation & { nonce: number };

export function parseDemoHash(hash: string): DemoLocation {
  const parts = decodeURIComponent(hash.replace(/^#\/?/, "")).split("/").filter(Boolean);
  if (parts[0] === "path" && parts[1]) {
    const step = Number.parseInt(parts[2] ?? "1", 10);
    return { kind: "path", path: parts[1], step: Number.isFinite(step) && step > 0 ? step : 1 };
  }
  if (parts[0] === "outcomes") return { kind: "outcomes", fixture: parts[1] };
  if (parts[0] === "console") return { kind: "console", view: parts[1] };
  return { kind: "guide" };
}

export function formatDemoHash(location: DemoLocation): string {
  switch (location.kind) {
    case "path":
      return `#path/${location.path}/${location.step}`;
    case "outcomes":
      return location.fixture ? `#outcomes/${location.fixture}` : "#outcomes";
    case "console":
      return location.view ? `#console/${encodeURIComponent(location.view)}` : "#console";
    default:
      return "#guide";
  }
}

const EVENT = "apex-demo-nav";
let counter = 0;

const read = (): StampedLocation => ({ ...parseDemoHash(window.location.hash), nonce: counter });

/** Navigate to a location. Always notifies listeners, even when the hash is unchanged. */
export function goDemo(location: DemoLocation): void {
  const hash = formatDemoHash(location);
  if (window.location.hash !== hash) window.history.pushState(null, "", hash);
  counter += 1;
  window.dispatchEvent(new Event(EVENT));
}

export function useDemoLocation(): StampedLocation {
  const [location, setLocation] = useState<StampedLocation>(read);
  useEffect(() => {
    const update = () => setLocation(read());
    window.addEventListener(EVENT, update);
    window.addEventListener("hashchange", update);
    window.addEventListener("popstate", update);
    return () => {
      window.removeEventListener(EVENT, update);
      window.removeEventListener("hashchange", update);
      window.removeEventListener("popstate", update);
    };
  }, []);
  return location;
}

/** The console view a guided step (or deep link) is asking for; supplied by DemoShell. */
export const DemoConsoleRequest = createContext<{ view?: string; nonce: number }>({ nonce: 0 });
