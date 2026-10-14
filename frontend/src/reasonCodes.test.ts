import { describe, expect, it } from "vitest";

import { explain, humanize, knownReasonCode } from "./reasonCodes";

describe("humanize", () => {
  it("reformats SCREAMING_SNAKE_CASE into a sentence-case phrase", () => {
    expect(humanize("SOME_NEW_CODE")).toBe("Some new code");
  });
  it("capitalizes only the first word, leaving the rest lowercase", () => {
    expect(humanize("ALREADY_LOWER_WORDS")).toBe("Already lower words");
  });
  it("handles a single-word code", () => {
    expect(humanize("SOLO")).toBe("Solo");
  });
  it("passes through an empty string unchanged", () => {
    expect(humanize("")).toBe("");
  });
});

describe("explain", () => {
  it("returns the curated phrase for a known ReasonFamily code", () => {
    expect(explain("ECONOMICS")).toBe("Did not clear the economic bar (expected return, costs, or downside)");
  });
  it("returns the curated phrase for a known DecisionDisposition code", () => {
    expect(explain("NOT_ADVANCED")).toBe("Did not advance");
  });
  it("returns the curated phrase for a known named raw_reason_code", () => {
    expect(explain("LIQUIDITY_INSUFFICIENT")).toBe("Not enough visible market depth");
  });
  it("returns the curated phrase for both always-present SECURITIES follow-up data-limitation codes", () => {
    expect(explain("SECURITIES_TERMINAL_PRICE_IS_APEX_CAPTURED_REFERENCE_PRICE_NOT_OFFICIAL_SETTLEMENT")).toBe(
      "Terminal price is Apex's own captured reference price, not an official settlement price",
    );
    expect(
      explain(
        "SECURITIES_SUBSEQUENT_RETURN_IS_STRUCTURE_NET_PROFIT_LOSS_OVER_CAPITAL_REQUIRED_NOT_AN_UNDERLYING_PRICE_RETURN",
      ),
    ).toBe("Return is the structure's own net profit/loss over capital required, not the underlying's price return");
  });
  it("falls back to a mechanical reformatting for an unrecognized code, never inventing meaning", () => {
    expect(explain("SOME_FUTURE_CODE_NOT_YET_CURATED")).toBe("Some future code not yet curated");
  });
  it("returns an em dash for non-string or empty input rather than throwing", () => {
    expect(explain(undefined)).toBe("—");
    expect(explain(null)).toBe("—");
    expect(explain("")).toBe("—");
    expect(explain(42)).toBe("—");
  });
});

describe("knownReasonCode", () => {
  it("is true only for codes in the curated dictionary", () => {
    expect(knownReasonCode("ECONOMICS")).toBe(true);
    expect(knownReasonCode("SOME_FUTURE_CODE_NOT_YET_CURATED")).toBe(false);
  });
});
