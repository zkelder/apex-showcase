import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DEMO_BANNER_TEXT, DemoBanner } from "./demoBanner";

function stubFetch(routes: Record<string, unknown>) {
  const fetchMock = vi.fn(async (url: string) => {
    const key = Object.keys(routes).find((candidate) => url.endsWith(candidate));
    if (!key) throw new Error(`unexpected fetch ${url}`);
    return { ok: true, status: 200, json: async () => routes[key] };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("static demo api", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("VITE_APEX_DEMO", "1");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    cleanup();
  });

  it("answers GET requests from the retained snapshot and never touches /api", async () => {
    const fetchMock = stubFetch({
      "demo-data/manifest.json": { files: { "/api/v1/health": "h.json" } },
      "demo-data/h.json": { status: 200, body: { schema_version: "1", capability: "X", data: { database_ready: true } } },
    });
    const { api } = await import("./api");
    await expect(api("/api/v1/health?x=1")).resolves.toEqual({ database_ready: true });
    expect(fetchMock.mock.calls.every(([url]) => !String(url).startsWith("/api"))).toBe(true);
  });

  it("rejects every mutation as read-only without any network call", async () => {
    const fetchMock = stubFetch({});
    const { api, mutate } = await import("./api");
    await expect(mutate("/api/v1/campaigns", {})).rejects.toMatchObject({ code: "DEMO_READ_ONLY" });
    await expect(api("/api/v1/x", { method: "DELETE" })).rejects.toMatchObject({ code: "DEMO_READ_ONLY" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reports a view that was not captured instead of inventing data", async () => {
    stubFetch({ "demo-data/manifest.json": { files: {} } });
    const { api } = await import("./api");
    await expect(api("/api/v1/unknown")).rejects.toMatchObject({ code: "DEMO_NOT_CAPTURED", status: 404 });
  });
});

describe("demo banner", () => {
  afterEach(cleanup);
  it("is always visible, announces the data is synthetic and cannot be dismissed", () => {
    render(<DemoBanner />);
    const banner = screen.getByTestId("demo-banner");
    expect(banner.textContent).toContain(DEMO_BANNER_TEXT);
    expect(banner.querySelector("button")).toBeNull();
    expect(document.body.dataset.demo).toBe("true");
  });
});

describe("deterministic outcomes page", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    cleanup();
  });
  it("renders each retained outcome with its canonical report hash", async () => {
    const report = {
      run_outcome: "NO_PROPOSAL",
      net_trading_pnl: "0",
      final_ledger_equity: "1000",
      fees: "0",
      ledger_journals: [],
      strategy_evaluation: { outcome: "NO_PROPOSAL", reason_codes: ["BELOW_THRESHOLD"] },
    };
    stubFetch({
      "fixture-outcomes.json": {
        seed: 1,
        capital: "1000.00",
        outcomes: [{ fixture: "no-proposal", title: "No proposal", report_hash: "a".repeat(64), report }],
      },
    });
    const { FixtureOutcomesPage } = await import("./fixtureOutcomes");
    render(<FixtureOutcomesPage />);
    await waitFor(() => expect(screen.getAllByText("No proposal").length).toBeGreaterThan(0));
    expect(screen.getByText(/Decision pipeline/)).toBeTruthy();
  });
});

describe("demo shell guard", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    cleanup();
  });
  it("blocks state-changing buttons before their handler runs", async () => {
    stubFetch({ "fixture-outcomes.json": { seed: 1, capital: "1000.00", outcomes: [] } });
    const handler = vi.fn();
    const { DemoShell } = await import("./demoShell");
    const { fireEvent } = await import("@testing-library/react");
    render(<DemoShell console={<button onClick={handler}>Run Paper Cycle</button>} />);
    fireEvent.click(screen.getByText(/Operator Console/));
    fireEvent.click(screen.getByText("Run Paper Cycle"));
    expect(handler).not.toHaveBeenCalled();
    expect(screen.getByRole("status").textContent).toContain("read-only synthetic demonstration");
  });

  it("labels empty console states and defaults to the populated outcomes tab", async () => {
    stubFetch({ "fixture-outcomes.json": { seed: 1, capital: "1000.00", outcomes: [] } });
    const { DemoShell } = await import("./demoShell");
    const { fireEvent } = await import("@testing-library/react");
    render(<DemoShell console={<p>console body</p>} />);
    expect(screen.queryByTestId("demo-empty-note")).toBeNull();
    expect(screen.getByRole("button", { name: "Deterministic outcomes" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByText(/Operator Console/));
    expect(screen.getByTestId("demo-empty-note").textContent).toContain("Empty states are expected");
  });
});
