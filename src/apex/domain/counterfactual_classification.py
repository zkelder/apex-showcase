"""Counterfactual Decision Classification & Interpretation Versioning v1.

This module owns no provider client, credential, network, execution, or
capital-mutation capability, and it introduces no new investment judgment.
It is the *analytical interpretation* layer that sits beside (never inside)
``domain.counterfactual_ledger``: given an already-immutable
``CounterfactualDecision`` -- itself a normalization of an already-retained
origin decision, see that module -- this module classifies its
``raw_reason_codes`` into a small set of normalized ``ReasonFamily`` values,
under one explicitly named, explicitly versioned classifier.

Why this is a separate module rather than a field on ``CounterfactualDecision``
itself: classification is *interpretation*, and interpretation improves over
time as Apex notices a previously-unmapped raw reason code or corrects a
misleading one.  ``CounterfactualDecision`` must never change shape or
content because someone improved a classifier -- its ``decision_id`` and
``canonical_hash`` are the stable anchor a future counterfactual-outcome
table will foreign-key against, and that anchor must stay valid independent
of which (or how many) classifier versions have since been run.  So:

* **``CounterfactualDecision`` is never mutated, never re-hashed, and never
  re-created by anything in this module.**  This module only ever *reads*
  already-retained decisions and *adds* new, independent
  ``CounterfactualDecisionClassification`` rows that reference them by
  ``decision_id`` (+ ``decision_canonical_hash``, for tamper-evidence --
  the same "reference by id and hash" idiom ``CounterfactualDecision``
  itself already uses for its own origin pointers).
* **A classifier version is a named, code-defined constant** --
  ``REASON_CLASSIFIER_V1``, ``REASON_CLASSIFIER_V2``, ... -- exactly the
  ``<NAME>_POLICY_V1``/``_V2`` convention used throughout Apex
  (``domain.crypto_universe.CRYPTO_UNIVERSE_POLICY_V1``/``_V2``,
  ``domain.total_market_context.TOTAL_MARKET_CONTEXT_POLICY_V1``, ...), not
  a free-text or operator-authored value: a new classifier version is a
  deliberate, reviewed code change, never something a mapping edit can
  silently reuse.  Each version's exact mapping table is also fingerprinted
  into its own ``*_POLICY_HASH`` (mirroring
  ``TOTAL_MARKET_CONTEXT_POLICY_HASH``) so a future mapping edit that
  forgets to mint a new version string is still detectable: the stored
  ``classifier_policy_hash`` on old rows would stop matching what the
  unchanged version string recomputes.
* **Every classification is its own immutable, append-only row** (see
  ``CounterfactualDecisionClassification``), never an update to a prior
  one.  Re-classifying the identical decision under the identical version
  is idempotent replay (matches the existing ``save_decision`` idiom in
  ``adapters.counterfactual_ledger_postgres``); classifying it under a
  *different* version always adds a new, independently-retained row,
  side by side with every earlier version's row for the same decision --
  "what did classifier v1 say?" and "what does classifier v2 say?" both
  remain answerable forever, from the same underlying decision, without
  rewriting anything.
* **One decision, classified under N versions, is still one decision.**
  ``CounterfactualDecisionClassification`` rows are *interpretations*, not
  additional considered decisions -- counting them is never a substitute
  for counting ``CounterfactualDecision`` rows, and callers must not add
  the two together.

``REASON_CLASSIFIER_V1`` reproduces, unchanged, exactly the mapping that
shipped with the original Counterfactual Decision Ledger v1 (when
``reason_families`` was still computed inline and folded into
``CounterfactualDecision.canonical_hash``); it is frozen so that classifier
v1's historical output for the 521 decisions retained before this milestone
remains exactly reproducible.  ``REASON_CLASSIFIER_V2`` is additive on top
of it, correcting three known real gaps traced to their exact origin
subsystem (see the entries below) -- never a threshold or judgment change
to anything ``REASON_CLASSIFIER_V1`` already mapped correctly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from apex.domain.counterfactual_ledger import CounterfactualDecision
from apex.domain.values import content_digest, deterministic_id

COUNTERFACTUAL_DECISION_CLASSIFICATION_V1 = "COUNTERFACTUAL_DECISION_CLASSIFICATION_V1"

REASON_CLASSIFIER_V1 = "REASON_CLASSIFIER_V1"
REASON_CLASSIFIER_V2 = "REASON_CLASSIFIER_V2"


class CounterfactualClassificationError(ValueError):
    """Raised when retained classification evidence is invalid, conflicts,
    or references an unknown classifier version."""


class ReasonFamily(StrEnum):
    """A small, bounded normalization layer over many subsystems' raw reason
    vocabularies -- introduced only because no generic taxonomy already
    existed.  Never a replacement for ``CounterfactualDecision.
    raw_reason_codes``, which always retains each origin's exact original
    strings; this is a derived, versioned interpretation of them (see the
    module docstring)."""

    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    STALE = "STALE"
    LIQUIDITY = "LIQUIDITY"
    ECONOMICS = "ECONOMICS"
    RESEARCH_CONFIDENCE = "RESEARCH_CONFIDENCE"
    MANDATE = "MANDATE"
    RISK = "RISK"
    CAPACITY = "CAPACITY"
    COMPETITIVE_SELECTION = "COMPETITIVE_SELECTION"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    OPERATIONAL_FAILURE = "OPERATIONAL_FAILURE"
    OTHER_EXPLICIT = "OTHER_EXPLICIT"


# Composite reason codes some origins wrap a nested raw code inside of;
# strip the prefix(es) and classify the remainder.  Structural/wire-format
# concern shared by every classifier version -- no known gap touches it.
_STRIPPED_PREFIXES = ("SOURCE:", "DISQUALIFYING_WARNING:")

# Best-effort fallback for reason codes no version's exact-match table
# enumerates (e.g. a future subsystem's raw vocabulary no version has seen
# yet).  Order matters: first matching keyword wins, so more specific
# keywords are listed first.  Never used when an exact match exists.
# Shared by every classifier version -- no known gap touches it.
_KEYWORD_FAMILY: tuple[tuple[str, ReasonFamily], ...] = (
    ("MATCHED_SIZE", ReasonFamily.LIQUIDITY),
    ("LIQUID", ReasonFamily.LIQUIDITY),
    ("DEPTH", ReasonFamily.LIQUIDITY),
    ("SPREAD", ReasonFamily.LIQUIDITY),
    ("NET_GAP", ReasonFamily.ECONOMICS),
    ("LOWER_BOUND", ReasonFamily.ECONOMICS),
    ("ROUNDING", ReasonFamily.ECONOMICS),
    ("FEE", ReasonFamily.ECONOMICS),
    ("COST", ReasonFamily.ECONOMICS),
    ("CALIBRATION", ReasonFamily.RESEARCH_CONFIDENCE),
    ("CONFIDENCE", ReasonFamily.RESEARCH_CONFIDENCE),
    ("SAMPLE", ReasonFamily.RESEARCH_CONFIDENCE),
    ("UNCERTAIN", ReasonFamily.RESEARCH_CONFIDENCE),
    ("MANDATE", ReasonFamily.MANDATE),
    ("CONCENTRATION", ReasonFamily.CAPACITY),
    ("HEADROOM", ReasonFamily.CAPACITY),
    ("CAPPED", ReasonFamily.CAPACITY),
    ("PROVIDER", ReasonFamily.PROVIDER_UNAVAILABLE),
    ("CAPABILITY", ReasonFamily.PROVIDER_UNAVAILABLE),
    ("UNAVAILABLE", ReasonFamily.PROVIDER_UNAVAILABLE),
    ("STALE", ReasonFamily.STALE),
    ("FRESH", ReasonFamily.STALE),
    ("EXPIRED", ReasonFamily.STALE),
    ("SKEW", ReasonFamily.STALE),
    ("MISSING", ReasonFamily.EVIDENCE_INCOMPLETE),
    ("INCOMPLETE", ReasonFamily.EVIDENCE_INCOMPLETE),
    ("INVALID", ReasonFamily.EVIDENCE_INCOMPLETE),
    ("INSUFFICIENT", ReasonFamily.EVIDENCE_INCOMPLETE),
    ("UNVERIFIED", ReasonFamily.EVIDENCE_INCOMPLETE),
    ("MISMATCH", ReasonFamily.EVIDENCE_INCOMPLETE),
)

# --------------------------------------------------------------------------
# REASON_CLASSIFIER_V1 -- frozen, byte-identical to the original Counterfactual
# Decision Ledger v1 mapping.  Never edit this table; a correction belongs in
# a new version (see V2 immediately below).
# --------------------------------------------------------------------------

# Reason strings that mean "advanced, nothing blocked this" rather than a
# rejection cause; excluded from classification (still preserved verbatim in
# ``CounterfactualDecision.raw_reason_codes``).
_V1_EXCLUDED_MARKERS: frozenset[str] = frozenset({"ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY"})

# Exact-match table for reason codes Apex is already known to emit.  Grouped
# by originating module so it stays auditable against that module's own
# source.  Anything not listed here falls through to the keyword heuristic,
# then to ``OTHER_EXPLICIT`` -- new/unrecognized codes are never silently
# mis-classified into a false-precision family.
_V1_EXACT_REASON_FAMILY: dict[str, ReasonFamily] = {
    # -- domain.shared_opportunity.evaluate_eligibility / DISQUALIFYING_WARNINGS --
    "SOURCE_IMMUTABLE_HASH_MISSING": ReasonFamily.EVIDENCE_INCOMPLETE,
    "EVIDENCE_CLASS_NOT_PERMITTED": ReasonFamily.EVIDENCE_INCOMPLETE,
    "SOURCE_POLICY_REJECTED": ReasonFamily.OTHER_EXPLICIT,
    "OPPORTUNITY_EXPIRED": ReasonFamily.STALE,
    "EVIDENCE_STALE": ReasonFamily.STALE,
    "EXPECTED_COSTS_MISSING": ReasonFamily.EVIDENCE_INCOMPLETE,
    "MAXIMUM_LOSS_MISSING": ReasonFamily.EVIDENCE_INCOMPLETE,
    "REQUIRED_CAPITAL_MISSING": ReasonFamily.EVIDENCE_INCOMPLETE,
    "LIQUIDITY_INSUFFICIENT": ReasonFamily.LIQUIDITY,
    "SOURCE_AUTHORITY_NOT_RESEARCH_ONLY": ReasonFamily.OPERATIONAL_FAILURE,
    "SOURCE_DATA_INVALID": ReasonFamily.EVIDENCE_INCOMPLETE,
    "SOURCE_LINEAGE_INVALID": ReasonFamily.EVIDENCE_INCOMPLETE,
    "UNKNOWN_SOURCE_AUTHORITY": ReasonFamily.OPERATIONAL_FAILURE,
    # -- domain.investment_mandate.evaluate_mandate --
    "MANDATE_NOT_YET_EFFECTIVE": ReasonFamily.MANDATE,
    "PORTFOLIO_SNAPSHOT_FROM_THE_FUTURE": ReasonFamily.EVIDENCE_INCOMPLETE,
    "PORTFOLIO_SNAPSHOT_STALE": ReasonFamily.STALE,
    "OPPORTUNITY_EVIDENCE_FROM_THE_FUTURE": ReasonFamily.EVIDENCE_INCOMPLETE,
    "VENUE_NOT_PERMITTED_BY_MANDATE": ReasonFamily.MANDATE,
    "MARKET_FAMILY_EXPLICITLY_DISALLOWED_BY_MANDATE": ReasonFamily.MANDATE,
    "MARKET_FAMILY_NOT_PERMITTED_BY_MANDATE": ReasonFamily.MANDATE,
    "OPTIONS_NOT_PERMITTED_BY_MANDATE": ReasonFamily.MANDATE,
    "SHORT_EXPOSURE_NOT_PERMITTED_BY_MANDATE": ReasonFamily.MANDATE,
    "LIQUIDITY_BELOW_MANDATE_MINIMUM": ReasonFamily.LIQUIDITY,
    "CAPITAL_LOCKUP_EXCEEDS_MANDATE_MAXIMUM": ReasonFamily.MANDATE,
    "OPPORTUNITY_ECONOMICS_INCOMPLETE_FOR_MANDATE_EVALUATION": ReasonFamily.EVIDENCE_INCOMPLETE,
    "EXISTING_INSTRUMENT_EXPOSURE_UNKNOWN": ReasonFamily.EVIDENCE_INCOMPLETE,
    "EXISTING_VENUE_EXPOSURE_UNKNOWN": ReasonFamily.EVIDENCE_INCOMPLETE,
    "EXISTING_MARKET_FAMILY_EXPOSURE_UNKNOWN": ReasonFamily.EVIDENCE_INCOMPLETE,
    "AGGREGATE_POSITION_EXPOSURE_UNKNOWN": ReasonFamily.EVIDENCE_INCOMPLETE,
    "AVAILABLE_CASH_UNKNOWN": ReasonFamily.EVIDENCE_INCOMPLETE,
    "MANDATE_HEADROOM_EXHAUSTED": ReasonFamily.CAPACITY,
    "MANDATE_CAPPED_BELOW_REQUIRED_CAPITAL": ReasonFamily.CAPACITY,
    # -- crypto precursor layer (domain.crypto_universe), reached today only
    # wrapped as SOURCE:<code> inside a SharedOpportunityProjection's own
    # ``source_rejection_reasons`` --
    "CALIBRATION_SAMPLE_INSUFFICIENT": ReasonFamily.RESEARCH_CONFIDENCE,
    "CRYPTO_CATALOG_INCOMPLETE_FOR_PRODUCTION_UNIVERSE": ReasonFamily.EVIDENCE_INCOMPLETE,
    "CRYPTO_DISCOVERY_HISTORY_INSUFFICIENT": ReasonFamily.EVIDENCE_INCOMPLETE,
    "CRYPTO_RELATIVE_STRENGTH_CALIBRATION_HISTORY_INSUFFICIENT": ReasonFamily.RESEARCH_CONFIDENCE,
    "CRYPTO_RELATIVE_STRENGTH_HISTORY_INSUFFICIENT": ReasonFamily.EVIDENCE_INCOMPLETE,
    "CRYPTO_RELATIVE_STRENGTH_INPUT_LINEAGE_INCOMPLETE": ReasonFamily.EVIDENCE_INCOMPLETE,
    "CRYPTO_RELATIVE_STRENGTH_VOLATILITY_INSUFFICIENT": ReasonFamily.EVIDENCE_INCOMPLETE,
    "CRYPTO_REVERSAL_REGIME_CALIBRATION_HISTORY_INSUFFICIENT": ReasonFamily.RESEARCH_CONFIDENCE,
    "CRYPTO_REVERSAL_REGIME_HISTORY_INSUFFICIENT": ReasonFamily.EVIDENCE_INCOMPLETE,
    "CRYPTO_REVERSAL_REGIME_INPUT_LINEAGE_INCOMPLETE": ReasonFamily.EVIDENCE_INCOMPLETE,
    "CURRENT_MARKET_EVIDENCE_INCOMPLETE": ReasonFamily.EVIDENCE_INCOMPLETE,
    "CURRENT_MARKET_EVIDENCE_STALE": ReasonFamily.STALE,
    "CURRENT_VISIBLE_DEPTH_INSUFFICIENT": ReasonFamily.LIQUIDITY,
    "HISTORICAL_COVERAGE_INSUFFICIENT": ReasonFamily.EVIDENCE_INCOMPLETE,
    "INSUFFICIENT_HISTORY": ReasonFamily.EVIDENCE_INCOMPLETE,
    "LIQUIDITY_BELOW_POLICY_MINIMUM": ReasonFamily.LIQUIDITY,
    "MISSING_DATA_QUALITY_FAILED": ReasonFamily.EVIDENCE_INCOMPLETE,
    "PUBLIC_VOLUME_IS_NOT_EXECUTABLE_LIQUIDITY": ReasonFamily.LIQUIDITY,
    # -- Kalshi exhaustive-event-set layer (domain.public_exhaustive) --
    "AMBIGUOUS_VOID_CANCELLATION_CONDITIONAL_OR_OTHER_RULE": ReasonFamily.EVIDENCE_INCOMPLETE,
    "CLOSE_OR_SETTLEMENT_TIMING_INCOMPATIBLE": ReasonFamily.EVIDENCE_INCOMPLETE,
    "COLLECTIVE_EXHAUSTIVENESS_NOT_VERIFIED": ReasonFamily.EVIDENCE_INCOMPLETE,
    "CONSERVATIVE_NET_GAP_NOT_POSITIVE": ReasonFamily.ECONOMICS,
    "EVENT_IDENTITY_MISMATCH": ReasonFamily.EVIDENCE_INCOMPLETE,
    "EVENT_NESTED_MARKET_COUNT_CONFLICT": ReasonFamily.EVIDENCE_INCOMPLETE,
    "EVENT_SET_COVERAGE_INCOMPLETE": ReasonFamily.EVIDENCE_INCOMPLETE,
    "EVENT_SET_EXPIRED": ReasonFamily.STALE,
    "FEE_EVIDENCE_MISSING": ReasonFamily.EVIDENCE_INCOMPLETE,
    "FEE_EVIDENCE_STALE": ReasonFamily.STALE,
    "FEE_MODEL_UNSUPPORTED": ReasonFamily.EVIDENCE_INCOMPLETE,
    "MARKET_NOT_OPEN": ReasonFamily.OTHER_EXPLICIT,
    "MUTUAL_EXCLUSIVITY_NOT_VERIFIED": ReasonFamily.EVIDENCE_INCOMPLETE,
    "NO_MATCHED_VISIBLE_PACKAGE_QUANTITY": ReasonFamily.LIQUIDITY,
    "ORDERBOOK_EVIDENCE_STALE": ReasonFamily.STALE,
    "ORDERBOOK_YES_SIDE_MISSING_OR_INVALID": ReasonFamily.EVIDENCE_INCOMPLETE,
    "RULE_EVIDENCE_MISSING": ReasonFamily.EVIDENCE_INCOMPLETE,
    "SETTLEMENT_SOURCE_INCOMPATIBLE_OR_MISSING": ReasonFamily.EVIDENCE_INCOMPLETE,
    "YES_ASK_OUTSIDE_BINARY_RANGE": ReasonFamily.EVIDENCE_INCOMPLETE,
    # -- Kalshi related-market/threshold layer (domain.public_threshold) --
    "EVENT_MEMBERSHIP_MISSING": ReasonFamily.EVIDENCE_INCOMPLETE,
    "FEES_ELIMINATE_LOWER_BOUND": ReasonFamily.ECONOMICS,
    "FEE_ROUNDING_UNSUPPORTED": ReasonFamily.EVIDENCE_INCOMPLETE,
    "GROSS_LOWER_BOUND_NOT_POSITIVE": ReasonFamily.ECONOMICS,
    "INSUFFICIENT_MATCHED_SIZE": ReasonFamily.LIQUIDITY,
    "MARKET_NOT_ACTIVE": ReasonFamily.OTHER_EXPLICIT,
    "MARKET_TYPE_UNSUPPORTED": ReasonFamily.OTHER_EXPLICIT,
    "OBSERVATION_SKEW_EXCEEDED": ReasonFamily.STALE,
    "ORDERBOOK_LEVEL_INVALID": ReasonFamily.EVIDENCE_INCOMPLETE,
    "ORDERBOOK_MISSING_SIDE": ReasonFamily.EVIDENCE_INCOMPLETE,
    "RELATIONSHIP_INVALID": ReasonFamily.EVIDENCE_INCOMPLETE,
    "RELATIONSHIP_UNSUPPORTED": ReasonFamily.OTHER_EXPLICIT,
    "ROUNDING_ELIMINATES_LOWER_BOUND": ReasonFamily.ECONOMICS,
    "RULE_FAMILY_MISMATCH": ReasonFamily.OTHER_EXPLICIT,
    "SERIES_IDENTITY_MISMATCH": ReasonFamily.EVIDENCE_INCOMPLETE,
    "SETTLEMENT_SOURCE_MISMATCH": ReasonFamily.EVIDENCE_INCOMPLETE,
    "SETTLEMENT_SOURCE_MISSING": ReasonFamily.EVIDENCE_INCOMPLETE,
    "STRIKE_METADATA_UNSUPPORTED": ReasonFamily.OTHER_EXPLICIT,
    "STRIKE_TYPE_UNSUPPORTED": ReasonFamily.OTHER_EXPLICIT,
    "STRIKE_VALUE_MISSING": ReasonFamily.EVIDENCE_INCOMPLETE,
    "THRESHOLD_DIRECTION_MISMATCH": ReasonFamily.OTHER_EXPLICIT,
    "THRESHOLD_ORDER_INVALID": ReasonFamily.OTHER_EXPLICIT,
}

# --------------------------------------------------------------------------
# REASON_CLASSIFIER_V2 -- additive on top of V1.  Exactly three changes,
# each traced to its exact origin subsystem/trigger condition (Counterfactual
# Decision Classification & Interpretation Versioning v1, Part 7):
#
# * ``NON_POSITIVE_NET_RESEARCH_SCORE`` (domain.crypto_universe.
#   build_opportunities, emitted when ``result.gross_research_score <= 0``)
#   -> ``ECONOMICS``.  That score is already net of modeled round-trip cost
#   and a liquidity/volatility penalty (``score = metric(trailing - 0.25 *
#   volatility - round_trip_cost - penalty)``) -- despite the "gross" field
#   name, a non-positive value is a real negative-modeled-economics signal,
#   not missing evidence or low research confidence.  V1 left this
#   unmapped (falls through to ``OTHER_EXPLICIT``): it matches no exact
#   entry and no keyword (it does not contain "COST", "FEE", "NET_GAP", ...).
# * ``OUTSIDE_TOP_RANKED_BOUND`` (same origin, emitted when ``result.rank >
#   MAX_OPPORTUNITIES`` -- a fixed top-3 slate) -> ``COMPETITIVE_SELECTION``.
#   This candidate did not fail on its own merits; it lost a fixed number of
#   slots to higher-ranked peers in the same run.  ``COMPETITIVE_SELECTION``
#   already existed in ``ReasonFamily`` for exactly this shape of rejection
#   but had never been reachable by any mapping until now.  V1 also left
#   this unmapped (no exact entry; "RANKED_BOUND" is not "LOWER_BOUND" and
#   matches no keyword).
# * ``SOURCE_POLICY_REJECTED`` (domain.shared_opportunity._crypto_paper_
#   readiness / evaluate_eligibility, appended whenever ``projection.
#   source_rejection_reasons`` is non-empty) moves from an
#   ``OTHER_EXPLICIT`` exact entry to an excluded structural marker (see
#   ``_V2_EXCLUDED_MARKERS``).  ``evaluate_eligibility`` always appends this
#   flag *together with* one ``SOURCE:<code>`` entry per underlying source
#   reason (never alone) -- those wrapped codes are already independently,
#   correctly classified once their ``SOURCE:`` prefix is stripped.  Keeping
#   ``SOURCE_POLICY_REJECTED`` itself as its own ``OTHER_EXPLICIT`` entry
#   double-counted one source signal as two independent reasons/families
#   (Part 11: "avoid double-counting one source reason as independent
#   evidence merely because it maps to multiple analytical families").  It
#   carries no independent meaning of its own beyond what its siblings
#   already say, so V2 excludes it the same way V1 already excludes
#   ``ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY``: a marker, not a reason.
# --------------------------------------------------------------------------

_V2_EXCLUDED_MARKERS: frozenset[str] = _V1_EXCLUDED_MARKERS | frozenset({"SOURCE_POLICY_REJECTED"})

_V2_EXACT_REASON_FAMILY: dict[str, ReasonFamily] = {
    **{
        key: value
        for key, value in _V1_EXACT_REASON_FAMILY.items()
        if key != "SOURCE_POLICY_REJECTED"
    },
    "NON_POSITIVE_NET_RESEARCH_SCORE": ReasonFamily.ECONOMICS,
    "OUTSIDE_TOP_RANKED_BOUND": ReasonFamily.COMPETITIVE_SELECTION,
}


def _strip_all_prefixes(text: str) -> str:
    for prefix in _STRIPPED_PREFIXES:
        if text.startswith(prefix):
            return _strip_all_prefixes(text[len(prefix) :])
    return text


def _classify_reason(raw: str, *, exact_table: Mapping[str, ReasonFamily]) -> ReasonFamily:
    text = _strip_all_prefixes(raw.strip())
    if not text:
        return ReasonFamily.OTHER_EXPLICIT
    exact = exact_table.get(text)
    if exact is not None:
        return exact
    upper = text.upper()
    for keyword, family in _KEYWORD_FAMILY:
        if keyword in upper:
            return family
    return ReasonFamily.OTHER_EXPLICIT


def _classify_all(
    reasons: Iterable[str],
    *,
    exact_table: Mapping[str, ReasonFamily],
    excluded_markers: frozenset[str],
) -> tuple[ReasonFamily, ...]:
    families = {
        _classify_reason(reason, exact_table=exact_table)
        for reason in reasons
        if reason not in excluded_markers
    }
    return tuple(sorted(families, key=lambda item: item.value))


def classify_reason_v1(raw: str) -> ReasonFamily:
    """Deterministic, total (never raises) -- frozen exactly as it shipped
    in Counterfactual Decision Ledger v1.  Kept reproducible forever; a
    correction belongs in v2, never here."""

    return _classify_reason(raw, exact_table=_V1_EXACT_REASON_FAMILY)


def classify_all_v1(reasons: tuple[str, ...]) -> tuple[ReasonFamily, ...]:
    """Deduplicated, deterministic alphabetical order regardless of input
    order.  Frozen exactly as it shipped in Counterfactual Decision Ledger
    v1."""

    return _classify_all(
        reasons, exact_table=_V1_EXACT_REASON_FAMILY, excluded_markers=_V1_EXCLUDED_MARKERS
    )


def classify_reason_v2(raw: str) -> ReasonFamily:
    """Deterministic, total (never raises).  Identical to
    ``classify_reason_v1`` except for the three corrections documented
    above ``_V2_EXACT_REASON_FAMILY``."""

    return _classify_reason(raw, exact_table=_V2_EXACT_REASON_FAMILY)


def classify_all_v2(reasons: tuple[str, ...]) -> tuple[ReasonFamily, ...]:
    """Deduplicated, deterministic alphabetical order regardless of input
    order.  Identical to ``classify_all_v1`` except for the three
    corrections documented above ``_V2_EXACT_REASON_FAMILY``."""

    return _classify_all(
        reasons, exact_table=_V2_EXACT_REASON_FAMILY, excluded_markers=_V2_EXCLUDED_MARKERS
    )


def _policy_hash(
    *, version: str, exact_table: Mapping[str, ReasonFamily], excluded_markers: frozenset[str]
) -> str:
    return content_digest(
        (
            version,
            tuple(sorted(exact_table.items())),
            _KEYWORD_FAMILY,
            _STRIPPED_PREFIXES,
            tuple(sorted(excluded_markers)),
        )
    )


REASON_CLASSIFIER_V1_POLICY_HASH = _policy_hash(
    version=REASON_CLASSIFIER_V1,
    exact_table=_V1_EXACT_REASON_FAMILY,
    excluded_markers=_V1_EXCLUDED_MARKERS,
)
REASON_CLASSIFIER_V2_POLICY_HASH = _policy_hash(
    version=REASON_CLASSIFIER_V2,
    exact_table=_V2_EXACT_REASON_FAMILY,
    excluded_markers=_V2_EXCLUDED_MARKERS,
)


@dataclass(frozen=True, slots=True)
class _ClassifierTables:
    exact: Mapping[str, ReasonFamily]
    excluded_markers: frozenset[str]
    policy_hash: str


_CLASSIFIER_TABLES: dict[str, _ClassifierTables] = {
    REASON_CLASSIFIER_V1: _ClassifierTables(
        _V1_EXACT_REASON_FAMILY, _V1_EXCLUDED_MARKERS, REASON_CLASSIFIER_V1_POLICY_HASH
    ),
    REASON_CLASSIFIER_V2: _ClassifierTables(
        _V2_EXACT_REASON_FAMILY, _V2_EXCLUDED_MARKERS, REASON_CLASSIFIER_V2_POLICY_HASH
    ),
}

# Enumerated explicitly (not inferred from a free-text column) so a new
# classifier version is always a deliberate, reviewed code change -- see the
# module docstring and the matching ``ck_counterfactual_decision_classification_
# classifier_version`` CHECK constraint in migration
# ``0095_reason_classification``.
KNOWN_CLASSIFIER_VERSIONS: tuple[str, ...] = tuple(_CLASSIFIER_TABLES)

# The version new syncs use by default.  Every earlier version stays fully
# supported (queryable, re-runnable, reproducible) -- this constant only
# selects what's "current," never what's "valid."
CURRENT_CLASSIFIER_VERSION = REASON_CLASSIFIER_V2


def _tables(classifier_version: str) -> _ClassifierTables:
    tables = _CLASSIFIER_TABLES.get(classifier_version)
    if tables is None:
        raise CounterfactualClassificationError("COUNTERFACTUAL_CLASSIFIER_VERSION_UNKNOWN")
    return tables


def classifier_policy_hash(classifier_version: str) -> str:
    return _tables(classifier_version).policy_hash


def classify_all(
    raw_reason_codes: tuple[str, ...], *, classifier_version: str
) -> tuple[ReasonFamily, ...]:
    """Dispatch to the named classifier version.  Raises
    ``CounterfactualClassificationError`` for an unknown version rather
    than silently falling back to one -- an unrecognized version string is
    always a caller bug, never a legitimate "unversioned" classification."""

    tables = _tables(classifier_version)
    return _classify_all(
        raw_reason_codes, exact_table=tables.exact, excluded_markers=tables.excluded_markers
    )


def is_reason_code_explicitly_mapped(raw: str, *, classifier_version: str) -> bool:
    """Coverage-diagnostic helper only -- never used by ``classify_all``
    itself, and never affects a decision's classified ``reason_families``.
    True when this classifier version has a considered, exact answer for
    ``raw`` (an entry in its exact-match table, or one of its
    explicitly-excluded non-substantive markers); False when ``raw`` is
    only resolved via the generic keyword fallback, or falls all the way
    through to ``OTHER_EXPLICIT`` -- i.e. nobody has looked at this exact
    code yet under this classifier version."""

    tables = _tables(classifier_version)
    if raw in tables.excluded_markers:
        return True
    text = _strip_all_prefixes(raw.strip())
    return bool(text) and text in tables.exact


def unmapped_raw_reason_codes(
    raw_reason_codes: Iterable[str], *, classifier_version: str
) -> tuple[str, ...]:
    """Every distinct code in ``raw_reason_codes`` this classifier version
    does not explicitly map, deduplicated, in first-seen order -- the
    direct answer to "which real reason codes are falling through to
    OTHER_EXPLICIT or unmapped?" for the given version (Part 10)."""

    seen: dict[str, None] = {}
    for raw in raw_reason_codes:
        if not is_reason_code_explicitly_mapped(raw, classifier_version=classifier_version):
            seen.setdefault(raw, None)
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class CounterfactualDecisionClassification:
    classification_id: str
    decision_id: str
    decision_canonical_hash: str
    classifier_version: str
    classifier_policy_hash: str
    reason_families: tuple[ReasonFamily, ...]
    classified_at: datetime
    canonical_hash: str
    authority: str = "NONE"


def classification_identity(*, decision_id: str, classifier_version: str) -> str:
    """Stable per (decision, classifier version); independent of
    ``classified_at`` so re-running the same classifier version against an
    unchanged decision always reproduces the identical
    ``classification_id`` (idempotent replay -- mirrors ``counterfactual_
    ledger._decision_id`` excluding sync-time metadata for the same
    reason).  Exposed (not underscore-prefixed) so migration
    ``0095_reason_classification``'s backfill can reuse it exactly
    when it explicitly identifies the 521 decisions' existing inline
    ``reason_families`` as classifier v1's own retained output."""

    return deterministic_id(
        "counterfactual_decision_classification",
        (COUNTERFACTUAL_DECISION_CLASSIFICATION_V1, decision_id, classifier_version),
    )


def classification_canonical_hash(
    *,
    classification_id: str,
    decision_id: str,
    decision_canonical_hash: str,
    classifier_version: str,
    classifier_policy_hash: str,
    reason_families: tuple[ReasonFamily, ...],
) -> str:
    """Deliberately excludes ``classified_at``: two classification runs of
    the identical decision under the identical classifier version must
    always be recognized as the identical fact, no matter how much
    wall-clock time separates them.  Exposed for migration
    ``0095_reason_classification``'s backfill to reuse exactly."""

    payload = (
        COUNTERFACTUAL_DECISION_CLASSIFICATION_V1,
        classification_id,
        decision_id,
        decision_canonical_hash,
        classifier_version,
        classifier_policy_hash,
        tuple(reason_families),
    )
    return content_digest(payload)


def build_classification(
    *,
    decision: CounterfactualDecision,
    classified_at: datetime,
    classifier_version: str = CURRENT_CLASSIFIER_VERSION,
) -> CounterfactualDecisionClassification:
    """Classify one already-retained ``CounterfactualDecision`` under one
    named classifier version.  Pure; performs no I/O and never touches
    ``decision`` itself -- ``CounterfactualDecision`` has no classification
    field left to touch (see the module docstring).  Re-running with the
    identical ``decision``/``classifier_version`` always reproduces the
    identical ``classification_id``/``canonical_hash`` regardless of
    ``classified_at`` (see ``classification_canonical_hash``); a different
    ``classifier_version`` always produces a new, independent
    ``classification_id`` that coexists with every earlier version's row
    for the same decision, never replacing one."""

    policy_hash = classifier_policy_hash(classifier_version)
    reason_families = classify_all(decision.raw_reason_codes, classifier_version=classifier_version)
    classification_id = classification_identity(
        decision_id=decision.decision_id, classifier_version=classifier_version
    )
    canonical_hash = classification_canonical_hash(
        classification_id=classification_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        classifier_version=classifier_version,
        classifier_policy_hash=policy_hash,
        reason_families=reason_families,
    )
    return CounterfactualDecisionClassification(
        classification_id=classification_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        classifier_version=classifier_version,
        classifier_policy_hash=policy_hash,
        reason_families=reason_families,
        classified_at=classified_at,
        canonical_hash=canonical_hash,
        authority="NONE",
    )
