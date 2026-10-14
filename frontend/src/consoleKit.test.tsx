import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AuthorityLadder, Loading, PageHead, ReasonList, StageCounts, boolText, counts, fraction, pairs, stateClass, text } from "./consoleKit";

describe("text", () => {
  it("passes strings and numbers through", () => {
    expect(text("hello")).toBe("hello");
    expect(text(42)).toBe("42");
  });
  it("falls back to an em dash for booleans, null, undefined, and objects", () => {
    expect(text(true)).toBe("—");
    expect(text(false)).toBe("—");
    expect(text(null)).toBe("—");
    expect(text(undefined)).toBe("—");
    expect(text({ value: "0.5" })).toBe("—");
  });
  it("honors a custom fallback", () => {
    expect(text(undefined, "unknown")).toBe("unknown");
  });
});

describe("boolText", () => {
  it("renders real booleans as yes/no rather than an em dash", () => {
    expect(boolText(true)).toBe("yes");
    expect(boolText(false)).toBe("no");
  });
  it("falls back to text() for non-boolean values", () => {
    expect(boolText(undefined)).toBe("—");
    expect(boolText("NONE")).toBe("NONE");
  });
});

describe("fraction", () => {
  it("unwraps a RiskFraction-shaped {value} object", () => {
    expect(fraction({ value: "0.5" })).toBe("0.5");
  });
  it("passes a bare string through unchanged", () => {
    expect(fraction("0.5")).toBe("0.5");
  });
  it("falls back honestly when nothing is retained", () => {
    expect(fraction(undefined)).toBe("—");
    expect(fraction(null)).toBe("—");
  });
});

describe("stateClass", () => {
  it("lowercases the state for the CSS modifier", () => {
    expect(stateClass("DUE")).toBe("state state-due");
    expect(stateClass(undefined)).toBe("state state-unknown");
  });
});

describe("pairs", () => {
  it("keeps only well-formed two-element tuples, stringifying both slots", () => {
    // Matches operatorConsole.tsx's pairs() exactly: it checks array shape,
    // not that the key is already a string -- so a numeric-keyed pair still
    // passes through, stringified, same as every other already-reviewed
    // call site relies on.
    expect(
      pairs([
        ["DUE", 2],
        ["NOT_YET_DUE", 10],
        "not-a-pair",
        [1, 2],
        ["only-one"],
      ]),
    ).toEqual([
      ["DUE", "2"],
      ["NOT_YET_DUE", "10"],
      ["1", "2"],
    ]);
  });
  it("returns an empty list for non-array input", () => {
    expect(pairs(undefined)).toEqual([]);
  });
  it("returns an empty list for a plain count-map object -- that is counts()'s shape, not this one's", () => {
    expect(pairs({ CRYPTO: 5, KALSHI: 3 })).toEqual([]);
  });
});

describe("counts", () => {
  it("turns a plain retained count map into stringified entries, insertion order preserved", () => {
    // Matches the real shape counterfactual_followup/counterfactual_ledger
    // coverage() breakdowns actually serialize as (a plain dict), distinct
    // from pairs()'s already-ordered-array contract.
    expect(counts({ CRYPTO: 5, KALSHI: 3 })).toEqual([
      ["CRYPTO", "5"],
      ["KALSHI", "3"],
    ]);
  });
  it("returns an empty list for a non-object, an array, null, or undefined", () => {
    expect(counts(undefined)).toEqual([]);
    expect(counts(null)).toEqual([]);
    expect(counts("not-an-object")).toEqual([]);
    expect(counts([["DUE", 2]])).toEqual([]);
  });
  it("drops entries whose value is neither a string nor a number", () => {
    expect(counts({ CRYPTO: 5, BROKEN: { nested: true }, KALSHI: "3" })).toEqual([
      ["CRYPTO", "5"],
      ["KALSHI", "3"],
    ]);
  });
});

describe("ReasonList", () => {
  it("renders a calm phrase for a known code alongside the exact raw code, never one without the other", () => {
    render(<ReasonList codes={["LIQUIDITY_INSUFFICIENT"]} />);
    expect(screen.getByText(/Not enough visible market depth/)).toBeInTheDocument();
    expect(screen.getByText("LIQUIDITY_INSUFFICIENT")).toBeInTheDocument();
  });
  it("mechanically reformats an unrecognized code rather than hiding it, and still keeps the raw code visible", () => {
    render(<ReasonList codes={["SOME_FUTURE_CODE"]} />);
    expect(screen.getByText(/Some future code/)).toBeInTheDocument();
    expect(screen.getByText("SOME_FUTURE_CODE")).toBeInTheDocument();
  });
  it("uses the default empty text when no codes are retained", () => {
    render(<ReasonList codes={[]} />);
    expect(screen.getByText("none")).toBeInTheDocument();
  });
  it("honors a call-site-specific empty text", () => {
    render(<ReasonList codes={[]} emptyText="clear" />);
    expect(screen.getByText("clear")).toBeInTheDocument();
  });
});

describe("Loading", () => {
  it("renders an honest, non-animated loading notice announced to assistive technology", () => {
    render(<Loading />);
    const node = screen.getByText("Loading retained state…");
    expect(node).toHaveAttribute("aria-live", "polite");
  });
  it("accepts a custom label", () => {
    render(<Loading label="Loading Opportunities…" />);
    expect(screen.getByText("Loading Opportunities…")).toBeInTheDocument();
  });
});

describe("AuthorityLadder", () => {
  it("states LIVE authority as NONE in every case -- it is never conditional on paperAccountCount", () => {
    render(<AuthorityLadder paperAccountCount={7} />);
    expect(screen.getByText(/NONE — not enabled in this build/)).toBeInTheDocument();
  });
  it("honestly shows zero retained PAPER accounts rather than hiding the row", () => {
    render(<AuthorityLadder paperAccountCount={0} />);
    expect(screen.getByText(/No internal PAPER account retained yet/)).toBeInTheDocument();
  });
  it("shows the real retained PAPER account count when nonzero", () => {
    render(<AuthorityLadder paperAccountCount={3} />);
    expect(screen.getByText(/3 internal simulated account\(s\) retained/)).toBeInTheDocument();
  });
  it("never renders language implying a real order was placed", () => {
    render(<AuthorityLadder paperAccountCount={3} />);
    const container = screen.getByText(/NONE — not enabled/).closest("div.key-values");
    expect(container?.textContent).not.toMatch(/order (was |is )?(placed|submitted|executed)/i);
  });
});

describe("StageCounts", () => {
  it("renders every stage's exact count as text, not a chart", () => {
    render(
      <StageCounts
        label="Current retained Opportunity set"
        stages={[
          ["considered", 12],
          ["eligible", 0],
          ["blocked", 9],
          ["rejected", 3],
        ]}
      />,
    );
    expect(screen.getByText("Current retained Opportunity set")).toBeInTheDocument();
    const eligibleStage = screen.getByText("eligible");
    expect(eligibleStage.querySelector("strong")).toHaveTextContent("0");
  });
});

describe("PageHead", () => {
  it("gives its heading a stable, programmatically focusable id for navigate-focus management", () => {
    render(<PageHead eyebrow="EYEBROW" title="Some Page" detail="detail text" />);
    const heading = screen.getByRole("heading", { level: 1, name: "Some Page" });
    expect(heading).toHaveAttribute("id", "console-page-title");
    expect(heading).toHaveAttribute("tabindex", "-1");
  });
});
