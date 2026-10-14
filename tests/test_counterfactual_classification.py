"""Domain proofs for Counterfactual Decision Classification & Interpretation
Versioning v1: deterministic reason classification under each named
classifier version, the versioned classification record itself, and the
invariants that make classifier evolution safe -- stable identity, no
double-counting, idempotent replay, no mutation of the decision it
classifies. No database, no network -- mirrors the existing offline
``tests/test_counterfactual_ledger.py`` pattern.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apex.domain.counterfactual_classification import (
    CURRENT_CLASSIFIER_VERSION,
    KNOWN_CLASSIFIER_VERSIONS,
    REASON_CLASSIFIER_V1,
    REASON_CLASSIFIER_V1_POLICY_HASH,
    REASON_CLASSIFIER_V2,
    REASON_CLASSIFIER_V2_POLICY_HASH,
    CounterfactualClassificationError,
    ReasonFamily,
    build_classification,
    classification_canonical_hash,
    classification_identity,
    classifier_policy_hash,
    classify_all,
    classify_all_v1,
    classify_all_v2,
    classify_reason_v1,
    classify_reason_v2,
    is_reason_code_explicitly_mapped,
    unmapped_raw_reason_codes,
)
from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    DecisionDisposition,
    DecisionOrigin,
)
from apex.domain.shared_opportunity import SharedVenue

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _decision(
    *, decision_id: str = "cf-decision-1", raw_reason_codes: tuple[str, ...]
) -> CounterfactualDecision:
    return CounterfactualDecision(
        decision_id=decision_id,
        origin=DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY,
        origin_decision_id="proj-1-eligibility-v1",
        origin_canonical_hash="a" * 64,
        origin_state="REJECTED",
        subject_projection_id="proj-1",
        subject_source_record_type="CryptoResearchOpportunity",
        subject_source_record_id="crypto-opp-1",
        venue=SharedVenue.CRYPTO,
        opportunity_set_id="set-1",
        mandate_id=None,
        mandate_lineage_id=None,
        mandate_version=None,
        mandate_hash=None,
        portfolio_snapshot_id=None,
        portfolio_snapshot_hash=None,
        evaluated_at=NOW,
        disposition=DecisionDisposition.NOT_ADVANCED,
        raw_reason_codes=raw_reason_codes,
        explanation="test fixture",
        canonical_hash="b" * 64,
        authority="NONE",
    )


# --------------------------------------------------------------------------
# classify_reason_v1 / classify_all_v1 -- frozen, must remain byte-identical
# to what Counterfactual Decision Ledger v1 originally shipped.
# --------------------------------------------------------------------------


def test_classify_reason_v1_exact_match_shared_opportunity_layer() -> None:
    assert classify_reason_v1("EVIDENCE_STALE") is ReasonFamily.STALE
    assert classify_reason_v1("LIQUIDITY_INSUFFICIENT") is ReasonFamily.LIQUIDITY
    assert classify_reason_v1("REQUIRED_CAPITAL_MISSING") is ReasonFamily.EVIDENCE_INCOMPLETE


def test_classify_reason_v1_exact_match_mandate_layer() -> None:
    assert classify_reason_v1("MANDATE_HEADROOM_EXHAUSTED") is ReasonFamily.CAPACITY
    assert classify_reason_v1("VENUE_NOT_PERMITTED_BY_MANDATE") is ReasonFamily.MANDATE
    assert classify_reason_v1("PORTFOLIO_SNAPSHOT_STALE") is ReasonFamily.STALE


def test_classify_reason_v1_strips_source_prefix_and_recurses() -> None:
    assert classify_reason_v1("SOURCE:CONSERVATIVE_NET_GAP_NOT_POSITIVE") is ReasonFamily.ECONOMICS
    assert (
        classify_reason_v1("SOURCE:CALIBRATION_SAMPLE_INSUFFICIENT")
        is ReasonFamily.RESEARCH_CONFIDENCE
    )


def test_classify_reason_v1_strips_disqualifying_warning_prefix() -> None:
    assert (
        classify_reason_v1("DISQUALIFYING_WARNING:LIQUIDITY_INSUFFICIENT") is ReasonFamily.LIQUIDITY
    )
    assert (
        classify_reason_v1("DISQUALIFYING_WARNING:UNKNOWN_SOURCE_AUTHORITY")
        is ReasonFamily.OPERATIONAL_FAILURE
    )


def test_classify_reason_v1_keyword_fallback_for_unseen_code() -> None:
    # never enumerated verbatim, but shares a recognizable keyword
    assert classify_reason_v1("NEW_SUBSYSTEM_QUOTE_IS_STALE_BEYOND_LIMIT") is ReasonFamily.STALE


def test_classify_reason_v1_totally_novel_code_is_other_explicit() -> None:
    assert (
        classify_reason_v1("SOMETHING_APEX_HAS_NEVER_EMITTED_BEFORE_XYZ")
        is ReasonFamily.OTHER_EXPLICIT
    )


def test_classify_reason_v1_empty_string_is_other_explicit() -> None:
    assert classify_reason_v1("") is ReasonFamily.OTHER_EXPLICIT
    assert classify_reason_v1("   ") is ReasonFamily.OTHER_EXPLICIT


def test_classify_all_v1_drops_positive_marker_and_dedupes() -> None:
    families = classify_all_v1(("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY",))
    assert families == ()


def test_classify_all_v1_is_order_independent_and_deterministic() -> None:
    first = classify_all_v1(("EVIDENCE_STALE", "LIQUIDITY_INSUFFICIENT", "EVIDENCE_STALE"))
    second = classify_all_v1(("LIQUIDITY_INSUFFICIENT", "EVIDENCE_STALE"))
    assert first == second
    assert first == (ReasonFamily.LIQUIDITY, ReasonFamily.STALE)


def test_classify_all_v1_operational_and_investment_families_are_disjoint_for_a_mixed_case() -> (
    None
):
    """A single decision can carry both an operational-integrity reason and
    an investment-economics reason; both must be independently visible
    rather than one masking the other."""

    families = classify_all_v1(
        ("UNKNOWN_SOURCE_AUTHORITY", "SOURCE:CONSERVATIVE_NET_GAP_NOT_POSITIVE")
    )
    assert ReasonFamily.OPERATIONAL_FAILURE in families
    assert ReasonFamily.ECONOMICS in families


def test_classify_reason_v1_known_v1_gaps_remain_unmapped() -> None:
    """v1 is frozen: it must keep reproducing exactly the gaps it always
    had, so historical v1 classification stays exactly reproducible."""

    assert classify_reason_v1("NON_POSITIVE_NET_RESEARCH_SCORE") is ReasonFamily.OTHER_EXPLICIT
    assert classify_reason_v1("OUTSIDE_TOP_RANKED_BOUND") is ReasonFamily.OTHER_EXPLICIT
    assert classify_reason_v1("SOURCE_POLICY_REJECTED") is ReasonFamily.OTHER_EXPLICIT


# --------------------------------------------------------------------------
# classify_reason_v2 / classify_all_v2 -- the three known real corrections
# (Part 7), traced to their exact origin subsystem/trigger.
# --------------------------------------------------------------------------


def test_classify_reason_v2_maps_non_positive_net_research_score_to_economics() -> None:
    """domain.crypto_universe.build_opportunities emits this when
    ``result.gross_research_score <= 0`` -- a score already net of modeled
    round-trip cost and a liquidity/volatility penalty, so a non-positive
    value is a real negative-modeled-economics signal."""

    assert classify_reason_v2("NON_POSITIVE_NET_RESEARCH_SCORE") is ReasonFamily.ECONOMICS
    assert classify_reason_v2("SOURCE:NON_POSITIVE_NET_RESEARCH_SCORE") is ReasonFamily.ECONOMICS


def test_classify_reason_v2_maps_outside_top_ranked_bound_to_competitive_selection() -> None:
    """domain.crypto_universe.build_opportunities emits this when
    ``result.rank > MAX_OPPORTUNITIES`` (a fixed top-3 slate) -- the
    candidate lost to higher-ranked peers, not to its own defect."""

    assert classify_reason_v2("OUTSIDE_TOP_RANKED_BOUND") is ReasonFamily.COMPETITIVE_SELECTION
    assert (
        classify_reason_v2("SOURCE:OUTSIDE_TOP_RANKED_BOUND") is ReasonFamily.COMPETITIVE_SELECTION
    )


def test_classify_reason_v2_excludes_source_policy_rejected_as_non_substantive() -> None:
    """``SOURCE_POLICY_REJECTED`` always co-occurs with a ``SOURCE:<code>``
    sibling that is already, independently classified once its prefix is
    stripped -- so in isolation it now contributes no family (matches the
    existing ``ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY`` treatment), rather
    than the misleading standalone ``OTHER_EXPLICIT`` v1 gave it."""

    assert classify_all_v2(("SOURCE_POLICY_REJECTED",)) == ()


def test_classify_all_v2_does_not_double_count_source_policy_rejected() -> None:
    """The real shape ``evaluate_eligibility`` always produces: the meta
    flag plus its one substantive wrapped sibling.  v1 counted this as two
    independent families; v2 must count it as exactly one (Part 11: avoid
    double-counting one source reason as independent evidence merely
    because it maps to multiple analytical families)."""

    raw = ("SOURCE_POLICY_REJECTED", "SOURCE:CONSERVATIVE_NET_GAP_NOT_POSITIVE")
    v1_families = classify_all_v1(raw)
    v2_families = classify_all_v2(raw)
    assert v1_families == (ReasonFamily.ECONOMICS, ReasonFamily.OTHER_EXPLICIT)
    assert v2_families == (ReasonFamily.ECONOMICS,)


def test_classify_all_v2_still_classifies_every_other_v1_mapping_identically() -> None:
    """Regression safety: v2 differs from v1 in exactly the three documented
    corrections and nowhere else."""

    import apex.domain.counterfactual_classification as module

    changed = {
        "SOURCE_POLICY_REJECTED",
        "NON_POSITIVE_NET_RESEARCH_SCORE",
        "OUTSIDE_TOP_RANKED_BOUND",
    }
    for code, family in module._V1_EXACT_REASON_FAMILY.items():
        if code in changed:
            continue
        assert classify_reason_v2(code) is family, code
    assert set(module._V2_EXACT_REASON_FAMILY) - set(module._V1_EXACT_REASON_FAMILY) == {
        "NON_POSITIVE_NET_RESEARCH_SCORE",
        "OUTSIDE_TOP_RANKED_BOUND",
    }


# --------------------------------------------------------------------------
# classify_all dispatch, classifier_policy_hash, coverage helpers
# --------------------------------------------------------------------------


def test_classify_all_dispatches_by_version() -> None:
    assert classify_all(("EVIDENCE_STALE",), classifier_version=REASON_CLASSIFIER_V1) == (
        ReasonFamily.STALE,
    )
    assert classify_all(("OUTSIDE_TOP_RANKED_BOUND",), classifier_version=REASON_CLASSIFIER_V1) == (
        ReasonFamily.OTHER_EXPLICIT,
    )
    assert classify_all(("OUTSIDE_TOP_RANKED_BOUND",), classifier_version=REASON_CLASSIFIER_V2) == (
        ReasonFamily.COMPETITIVE_SELECTION,
    )


def test_classify_all_unknown_version_raises() -> None:
    with pytest.raises(CounterfactualClassificationError):
        classify_all(("EVIDENCE_STALE",), classifier_version="REASON_CLASSIFIER_V99")


def test_known_classifier_versions_and_current() -> None:
    assert KNOWN_CLASSIFIER_VERSIONS == (REASON_CLASSIFIER_V1, REASON_CLASSIFIER_V2)
    assert CURRENT_CLASSIFIER_VERSION == REASON_CLASSIFIER_V2


def test_classifier_policy_hash_differs_between_versions_and_matches_constants() -> None:
    assert REASON_CLASSIFIER_V1_POLICY_HASH != REASON_CLASSIFIER_V2_POLICY_HASH
    assert classifier_policy_hash(REASON_CLASSIFIER_V1) == REASON_CLASSIFIER_V1_POLICY_HASH
    assert classifier_policy_hash(REASON_CLASSIFIER_V2) == REASON_CLASSIFIER_V2_POLICY_HASH
    assert len(REASON_CLASSIFIER_V1_POLICY_HASH) == 64
    assert len(REASON_CLASSIFIER_V2_POLICY_HASH) == 64


def test_is_reason_code_explicitly_mapped_distinguishes_versions() -> None:
    assert (
        is_reason_code_explicitly_mapped(
            "NON_POSITIVE_NET_RESEARCH_SCORE", classifier_version=REASON_CLASSIFIER_V1
        )
        is False
    )
    assert (
        is_reason_code_explicitly_mapped(
            "NON_POSITIVE_NET_RESEARCH_SCORE", classifier_version=REASON_CLASSIFIER_V2
        )
        is True
    )
    # keyword-fallback-only codes are not "explicitly" mapped under either version
    assert (
        is_reason_code_explicitly_mapped(
            "NEW_SUBSYSTEM_QUOTE_IS_STALE_BEYOND_LIMIT", classifier_version=REASON_CLASSIFIER_V1
        )
        is False
    )


def test_unmapped_raw_reason_codes_is_deterministic_and_deduplicated() -> None:
    codes = (
        "NON_POSITIVE_NET_RESEARCH_SCORE",
        "EVIDENCE_STALE",
        "OUTSIDE_TOP_RANKED_BOUND",
        "NON_POSITIVE_NET_RESEARCH_SCORE",
    )
    unmapped_v1 = unmapped_raw_reason_codes(codes, classifier_version=REASON_CLASSIFIER_V1)
    unmapped_v2 = unmapped_raw_reason_codes(codes, classifier_version=REASON_CLASSIFIER_V2)
    assert unmapped_v1 == ("NON_POSITIVE_NET_RESEARCH_SCORE", "OUTSIDE_TOP_RANKED_BOUND")
    assert unmapped_v2 == ()
    # deterministic on replay
    assert unmapped_raw_reason_codes(codes, classifier_version=REASON_CLASSIFIER_V1) == unmapped_v1


# --------------------------------------------------------------------------
# build_classification / classification_identity / classification_canonical_hash
# --------------------------------------------------------------------------


def test_build_classification_is_deterministic() -> None:
    decision = _decision(raw_reason_codes=("EVIDENCE_STALE",))
    first = build_classification(
        decision=decision, classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1
    )
    second = build_classification(
        decision=decision, classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1
    )
    assert first.classification_id == second.classification_id
    assert first.canonical_hash == second.canonical_hash
    assert first.reason_families == second.reason_families == (ReasonFamily.STALE,)


def test_build_classification_identity_and_hash_are_independent_of_classified_at() -> None:
    """Idempotent replay must work no matter how much wall-clock time
    separates two runs of the same classifier version over the same
    decision."""

    decision = _decision(raw_reason_codes=("EVIDENCE_STALE",))
    first = build_classification(
        decision=decision, classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1
    )
    later = build_classification(
        decision=decision,
        classified_at=NOW + timedelta(days=30),
        classifier_version=REASON_CLASSIFIER_V1,
    )
    assert first.classification_id == later.classification_id
    assert first.canonical_hash == later.canonical_hash
    assert first.classified_at != later.classified_at


def test_build_classification_v1_and_v2_disagree_without_identity_conflict() -> None:
    decision = _decision(raw_reason_codes=("OUTSIDE_TOP_RANKED_BOUND",))
    v1 = build_classification(
        decision=decision, classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1
    )
    v2 = build_classification(
        decision=decision, classified_at=NOW, classifier_version=REASON_CLASSIFIER_V2
    )
    assert v1.reason_families == (ReasonFamily.OTHER_EXPLICIT,)
    assert v2.reason_families == (ReasonFamily.COMPETITIVE_SELECTION,)
    assert v1.classification_id != v2.classification_id
    assert v1.canonical_hash != v2.canonical_hash
    # both point at the identical underlying decision -- one decision, two
    # independent interpretations, never a conflict
    assert v1.decision_id == v2.decision_id == decision.decision_id
    assert v1.decision_canonical_hash == v2.decision_canonical_hash == decision.canonical_hash


def test_build_classification_never_touches_the_decision_object() -> None:
    decision = _decision(raw_reason_codes=("EVIDENCE_STALE",))
    before = (decision.canonical_hash, decision.decision_id, decision.raw_reason_codes)
    build_classification(
        decision=decision, classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1
    )
    build_classification(
        decision=decision, classified_at=NOW, classifier_version=REASON_CLASSIFIER_V2
    )
    after = (decision.canonical_hash, decision.decision_id, decision.raw_reason_codes)
    assert before == after  # frozen dataclass; this would fail to even construct if mutated


def test_build_classification_defaults_to_current_classifier_version() -> None:
    decision = _decision(raw_reason_codes=("OUTSIDE_TOP_RANKED_BOUND",))
    built = build_classification(decision=decision, classified_at=NOW)
    assert built.classifier_version == CURRENT_CLASSIFIER_VERSION
    assert built.reason_families == (ReasonFamily.COMPETITIVE_SELECTION,)


def test_classification_identity_depends_only_on_decision_id_and_version() -> None:
    same_a = classification_identity(decision_id="d-1", classifier_version=REASON_CLASSIFIER_V1)
    same_b = classification_identity(decision_id="d-1", classifier_version=REASON_CLASSIFIER_V1)
    different_version = classification_identity(
        decision_id="d-1", classifier_version=REASON_CLASSIFIER_V2
    )
    different_decision = classification_identity(
        decision_id="d-2", classifier_version=REASON_CLASSIFIER_V1
    )
    assert same_a == same_b
    assert different_version != same_a
    assert different_decision != same_a


def test_classification_canonical_hash_excludes_classified_at_by_construction() -> None:
    """``classification_canonical_hash`` takes no ``classified_at``
    parameter at all -- structurally impossible for it to affect the hash,
    not merely conventionally excluded."""

    import inspect

    parameters = inspect.signature(classification_canonical_hash).parameters
    assert "classified_at" not in parameters


def test_classification_canonical_hash_changes_when_decision_canonical_hash_changes() -> None:
    """A future outcome-attribution-relevant tamper-evidence property: if
    the underlying decision's own canonical content ever legitimately
    changed (a new decision_id from a changed origin, never a mutation of
    an existing one), a classification bound to the old hash must not
    silently appear to still describe the new one."""

    base = dict(
        classification_id="cid-1",
        decision_id="d-1",
        classifier_version=REASON_CLASSIFIER_V1,
        classifier_policy_hash=REASON_CLASSIFIER_V1_POLICY_HASH,
        reason_families=(ReasonFamily.STALE,),
    )
    first = classification_canonical_hash(decision_canonical_hash="h" * 64, **base)
    second = classification_canonical_hash(decision_canonical_hash="i" * 64, **base)
    assert first != second
