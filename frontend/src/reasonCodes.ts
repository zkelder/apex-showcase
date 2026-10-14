// Shared, presentation-only translation of Apex's stable machine reason
// codes into calmer operator language.
//
// This is a lookup table over a small, curated set of codes this console's
// own read models are known to emit (ReasonFamily, DecisionDisposition, a
// handful of named raw_reason_codes, and Research Operations recommendation
// states -- each entry below is sourced from an exact backend enum/constant,
// not guessed). It computes no new judgment and invents no meaning: an
// unrecognized code still gets a mechanically reformatted (never
// reinterpreted) fallback via humanize(), and every call site that uses
// explain() is expected to keep the exact raw code visible alongside it
// (see ReasonList in consoleKit.tsx) -- never as a replacement for it.

const REASON_TEXT: Record<string, string> = {
  // Follow-up backlog reasons (DecisionFollowUpObservation planning state) --
  // ported verbatim from the phrasing followUp.tsx already shipped and
  // tested; unchanged here, just shared.
  CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE:
    "Will never resolve — predates this product's own retained daily price history",
  CRYPTO_BASELINE_BUCKET_NOT_RETAINED: "Not yet captured — retryable once daily history catches up",
  CRYPTO_TARGET_BUCKET_NOT_YET_RETAINED: "Target day not yet captured — retryable later",
  SUBJECT_PROJECTION_NOT_RETAINED: "Source research record is no longer retained",
  COUNTERFACTUAL_FOLLOWUP_KALSHI_REQUEST_BUDGET_EXHAUSTED_THIS_CYCLE:
    "Kalshi request budget was used up this cycle — retried automatically next cycle",

  // Securities Research -> Shared Opportunity Recurring Bridge v1
  // (domain.counterfactual_followup.SECURITIES_STRUCTURE_LIMITATIONS) --
  // always present on every genuine SECURITIES_STRUCTURE_TERMINAL_PAYOFF
  // observation's data_limitations, never only on a failure path.
  SECURITIES_TERMINAL_PRICE_IS_APEX_CAPTURED_REFERENCE_PRICE_NOT_OFFICIAL_SETTLEMENT:
    "Terminal price is Apex's own captured reference price, not an official settlement price",
  SECURITIES_SUBSEQUENT_RETURN_IS_STRUCTURE_NET_PROFIT_LOSS_OVER_CAPITAL_REQUIRED_NOT_AN_UNDERLYING_PRICE_RETURN:
    "Return is the structure's own net profit/loss over capital required, not the underlying's price return",

  // ReasonFamily (domain.counterfactual_classification) -- the versioned,
  // normalized interpretation of a CounterfactualDecision's raw reasons.
  EVIDENCE_INCOMPLETE: "Missing evidence — required data was not retained",
  STALE: "Evidence was too old to use safely",
  LIQUIDITY: "Not enough visible market depth",
  ECONOMICS: "Did not clear the economic bar (expected return, costs, or downside)",
  RESEARCH_CONFIDENCE: "Sample or calibration was not yet sufficient",
  MANDATE: "Blocked by the current investment mandate",
  RISK: "Blocked by a risk check",
  CAPACITY: "Blocked by a capacity or sizing constraint",
  COMPETITIVE_SELECTION: "Ranked below another Opportunity competing for the same allocation",
  PROVIDER_UNAVAILABLE: "The venue or provider was unavailable at evaluation time",
  OPERATIONAL_FAILURE: "An operational task failed while evaluating this",
  OTHER_EXPLICIT: "An explicit reason outside the standard families — see the raw code",

  // DecisionDisposition (domain.counterfactual_ledger)
  ADVANCED: "Advanced — passed this stage",
  CONSTRAINED: "Advanced with a binding constraint applied",
  NOT_ADVANCED: "Did not advance",

  // Named raw_reason_codes this console's read models are known to emit.
  SOURCE_IMMUTABLE_HASH_MISSING: "Source evidence is missing its required integrity hash",
  EVIDENCE_CLASS_NOT_PERMITTED: "This evidence class is not permitted for this decision",
  LIQUIDITY_INSUFFICIENT: "Not enough visible market depth",
  OPPORTUNITY_EXPIRED: "The Opportunity's validity window has passed",
  EVIDENCE_STALE: "Evidence was too old to use safely",

  // Research Operations proposal recommendation states.
  REJECT: "Recommended: reject",
  REVISE: "Recommended: revise and resubmit",
  REPLICATE: "Recommended: replicate an existing result first",
  INSUFFICIENT_EVIDENCE: "Recommended: not enough evidence yet",
  PAPER_CANDIDATE: "Recommended: candidate for PAPER review",
};

/** Mechanically reformats an unrecognized SCREAMING_SNAKE_CASE code into a
 * calmer sentence-case phrase -- e.g. "SOME_NEW_CODE" becomes
 * "Some new code". This is pure text formatting, never a reinterpretation:
 * an unknown code's actual meaning is never guessed at. */
export function humanize(code: string): string {
  if (!code) return code;
  const words = code.toLowerCase().split(/[_\s]+/).filter(Boolean);
  if (!words.length) return code;
  const [first, ...rest] = words;
  return first.charAt(0).toUpperCase() + first.slice(1) + (rest.length ? " " + rest.join(" ") : "");
}

/** The calm phrase for a stable code: the curated dictionary entry when
 * known, otherwise a mechanical (never invented) reformatting. Callers
 * must keep the exact raw code available alongside this -- see ReasonList,
 * which never renders explain() alone. */
export function explain(code: unknown): string {
  if (typeof code !== "string" || !code) return "—";
  return REASON_TEXT[code] ?? humanize(code);
}

export function knownReasonCode(code: string): boolean {
  return code in REASON_TEXT;
}
