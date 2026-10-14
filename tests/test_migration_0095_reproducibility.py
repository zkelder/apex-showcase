"""Migration 0095 Historical Reproducibility Hardening v1 -- regression
coverage.

``tests/security/test_architecture_boundaries.py::
test_migrations_never_import_mutable_domain_or_application_modules`` is the
*structural* guard: it fails if migration ``0095_reason_classification`` (or
any future migration) ever imports from ``apex.domain``/``apex.application``
again. This file is the complementary *behavioral* proof: that migration
0095's frozen, migration-local canonicalization formulas actually compute
the same thing today's live ``apex.domain`` implementation computes, for
representative pre-0095 row shapes.

Both sides of every comparison below are independent implementations -- the
migration's own private functions, loaded directly from the migration file
on disk, are never the live ``apex.domain`` code the migration used to
import (see ``test_migration_frozen_helpers_are_not_aliases_of_live_domain_
functions``) -- so agreement here is a real cross-check, not two calls to
the same function declaring themselves equal. A hardcoded golden hash on
each case additionally pins today's actual value: if a future change made
*both* sides drift together (for instance, someone "simplifies" the frozen
copy by quietly re-deriving it from the live formula at import time), the
frozen/live comparison alone would still pass, but the golden constant would
not.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

from apex.domain.counterfactual_classification import (
    REASON_CLASSIFIER_V1,
    REASON_CLASSIFIER_V1_POLICY_HASH,
    classification_canonical_hash,
    classification_identity,
)
from apex.domain.counterfactual_ledger import decision_canonical_hash
from apex.domain.values import canonical_json_bytes, canonical_timestamp

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "migrations" / "versions" / "0095_reason_classification.py"


def _load_migration_0095() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0095_under_test", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration_0095 = _load_migration_0095()

# A representative SHARED_OPPORTUNITY_ELIGIBILITY-origin pre-0095 row: no
# mandate/portfolio pointers, matching every real eligibility-origin
# decision retained today (``origin = 'SHARED_OPPORTUNITY_ELIGIBILITY'``
# always carries ``mandate_id``/``portfolio_snapshot_id`` = NULL; see
# migration 0094's own ``ck_counterfactual_decision_origin_lineage``).
_ELIGIBILITY_CASE: dict[str, object] = {
    "decision_id": "counterfactual_decision_test_eligibility",
    "origin": "SHARED_OPPORTUNITY_ELIGIBILITY",
    "origin_decision_id": "origin-eligibility-1",
    "origin_canonical_hash": "a" * 64,
    "origin_state": "REJECTED",
    "subject_projection_id": "projection-1",
    "subject_source_record_type": "CRYPTO_MARKET_PULSE_OPPORTUNITY",
    "subject_source_record_id": "source-1",
    "venue": "CRYPTO",
    "opportunity_set_id": None,
    "mandate_id": None,
    "mandate_lineage_id": None,
    "mandate_version": None,
    "mandate_hash": None,
    "portfolio_snapshot_id": None,
    "portfolio_snapshot_hash": None,
    "evaluated_at": "2026-08-01T00:00:00Z",
    "disposition": "NOT_ADVANCED",
    "raw_reason_codes": ("SOURCE_POLICY_REJECTED", "SOURCE:CONSERVATIVE_NET_GAP_NOT_POSITIVE"),
    "explanation": "SHARED_OPPORTUNITY_ELIGIBILITY: NOT_ADVANCED (origin_state=REJECTED) test row",
}

# A representative INVESTMENT_MANDATE_EVALUATION-origin pre-0095 row: every
# mandate/portfolio pointer populated, exercising the non-None branch of
# every optional field the eligibility case above leaves ``None`` -- and a
# non-zero ``mandate_version`` int, the one non-string/non-None scalar type
# ``decision_canonical_hash`` ever encodes.
_MANDATE_CASE: dict[str, object] = {
    "decision_id": "counterfactual_decision_test_mandate",
    "origin": "INVESTMENT_MANDATE_EVALUATION",
    "origin_decision_id": "origin-mandate-1",
    "origin_canonical_hash": "b" * 64,
    "origin_state": "CONSTRAINED",
    "subject_projection_id": "projection-2",
    "subject_source_record_type": "KALSHI_BINARY_COMPLEMENT_OPPORTUNITY",
    "subject_source_record_id": "source-2",
    "venue": "KALSHI",
    "opportunity_set_id": "opportunity-set-1",
    "mandate_id": "mandate-1",
    "mandate_lineage_id": "mandate-lineage-1",
    "mandate_version": 3,
    "mandate_hash": "c" * 64,
    "portfolio_snapshot_id": "snapshot-1",
    "portfolio_snapshot_hash": "d" * 64,
    "evaluated_at": "2026-08-02T12:30:00Z",
    "disposition": "CONSTRAINED",
    "raw_reason_codes": ("MANDATE_HEADROOM_EXHAUSTED",),
    "explanation": "INVESTMENT_MANDATE_EVALUATION: CONSTRAINED test row",
}

_CASES: dict[str, dict[str, object]] = {
    "eligibility": _ELIGIBILITY_CASE,
    "mandate": _MANDATE_CASE,
}
_REASON_FAMILIES: dict[str, tuple[str, ...]] = {
    "eligibility": ("ECONOMICS", "OTHER_EXPLICIT"),
    "mandate": ("CAPACITY",),
}

# Golden values pinned at the moment this test was authored (Migration 0095
# Historical Reproducibility Hardening v1), computed once from the live
# ``apex.domain`` formulas against the cases above. If ONLY a golden
# constant fails while its matching frozen-vs-live comparison still passes,
# the live ``apex.domain`` formula legitimately changed for a new, unrelated
# reason -- expected, and safe to refresh the constant. If a frozen-vs-live
# comparison itself fails, migration 0095's frozen copy has drifted from
# ``apex.domain`` (or vice versa) -- do not "fix" this by updating the
# golden constant; that would paper over the exact defect this migration
# exists to prevent.
_GOLDEN_DECISION_HASH: dict[str, str] = {
    "eligibility": "a3ce06e7740feb71d3924029f045c24992d9ebb99eaec424c66d57ce518f0e1e",
    "mandate": "bc874eb18af45ad37d5701bf386120f6f79df976b97124564aa8cba622bed681",
}
_GOLDEN_CLASSIFICATION_HASH: dict[str, str] = {
    "eligibility": "486344bc4eefee24076a5c80a1bfb67b727f3eaed2fc5a7d3d49ab075e407a9e",
    "mandate": "8252d69954e74b0208c28a3c2abf8adb605f30308bd628ad6199c08219944096",
}


@pytest.mark.parametrize("case_name", sorted(_CASES))
def test_frozen_decision_hash_matches_live_domain_formula(case_name: str) -> None:
    case = _CASES[case_name]

    frozen = migration_0095._decision_canonical_hash(**case)
    live = decision_canonical_hash(**case)

    assert frozen == live
    assert frozen == _GOLDEN_DECISION_HASH[case_name]


@pytest.mark.parametrize("case_name", sorted(_CASES))
def test_frozen_classification_hash_matches_live_domain_formula(case_name: str) -> None:
    case = _CASES[case_name]
    reason_families = _REASON_FAMILIES[case_name]

    assert migration_0095._REASON_CLASSIFIER_V1 == REASON_CLASSIFIER_V1
    assert migration_0095._REASON_CLASSIFIER_V1_POLICY_HASH == REASON_CLASSIFIER_V1_POLICY_HASH

    decision_hash = migration_0095._decision_canonical_hash(**case)
    decision_id = case["decision_id"]
    assert isinstance(decision_id, str)

    frozen_classification_id = migration_0095._classification_identity(
        decision_id=decision_id, classifier_version=migration_0095._REASON_CLASSIFIER_V1
    )
    live_classification_id = classification_identity(
        decision_id=decision_id, classifier_version=REASON_CLASSIFIER_V1
    )
    assert frozen_classification_id == live_classification_id

    frozen = migration_0095._classification_canonical_hash(
        classification_id=frozen_classification_id,
        decision_id=decision_id,
        decision_canonical_hash=decision_hash,
        classifier_version=migration_0095._REASON_CLASSIFIER_V1,
        classifier_policy_hash=migration_0095._REASON_CLASSIFIER_V1_POLICY_HASH,
        reason_families=reason_families,
    )
    live = classification_canonical_hash(
        classification_id=live_classification_id,
        decision_id=decision_id,
        decision_canonical_hash=decision_hash,
        classifier_version=REASON_CLASSIFIER_V1,
        classifier_policy_hash=REASON_CLASSIFIER_V1_POLICY_HASH,
        reason_families=reason_families,
    )

    assert frozen == live
    assert frozen == _GOLDEN_CLASSIFICATION_HASH[case_name]


def test_frozen_canonical_timestamp_matches_live_domain_formula() -> None:
    """``classification_payload["classified_at"]`` is the one value this
    migration's backfill ever canonicalizes that is *not* read back via
    ``json.loads`` from an already-retained row: ``backfilled_at =
    datetime.now(UTC)``, a genuine ``datetime`` this migration creates
    itself. A real fresh-migration-chain run against Postgres (Migration
    0095 Historical Reproducibility Hardening v1, Part 4) caught this case
    missing from an earlier draft of the frozen ``_canonicalize`` -- it had
    been narrowed on the mistaken assumption that every value this
    migration touches was already JSON-native, which is true for every
    field sourced from a row's own payload but not for this one.  Covered
    directly here (not only indirectly through the Postgres-backed
    fresh-chain test) so this specific branch fails fast, offline, without
    needing a database."""

    naive = datetime(2026, 8, 12, 12, 8, 28, 465074)
    aware_with_micros = naive.replace(tzinfo=UTC)
    aware_whole_second = datetime(2026, 8, 12, 12, 8, 28, tzinfo=UTC)
    # A fixed, explicit non-UTC offset (never the host's own local timezone,
    # which could itself already be UTC and silently defeat this case) --
    # ``_canonical_timestamp`` must convert it to UTC before formatting.
    non_utc = datetime(2026, 8, 12, 17, 8, 28, 465074, tzinfo=timezone(timedelta(hours=5)))

    for value in (aware_with_micros, aware_whole_second, non_utc):
        assert migration_0095._canonical_timestamp(value) == canonical_timestamp(value)

    with pytest.raises(ValueError):
        migration_0095._canonical_timestamp(naive)


def test_frozen_canonical_json_bytes_matches_live_domain_serialization() -> None:
    """The migration also writes a ``payload`` TEXT column via its own
    ``_canonical_json_bytes`` -- a bug there would not show up in the hash
    comparisons above alone, since those exercise the hash-input *tuple*
    encoding, not the dict/payload encoding the migration separately uses
    for the stored row body. Includes a ``classified_at`` datetime field,
    matching the real shape of ``classification_payload`` -- the one field
    in either payload dict this migration ever builds that is not already
    JSON-native (see ``test_frozen_canonical_timestamp_matches_live_domain_
    formula``)."""

    sample_payload = {
        **_MANDATE_CASE,
        "raw_reason_codes": list(_MANDATE_CASE["raw_reason_codes"]),  # type: ignore[arg-type]
        "canonical_hash": "e" * 64,
        "classified_at": datetime(2026, 8, 12, 12, 8, 28, 465074, tzinfo=UTC),
        "authority": "NONE",
    }
    assert migration_0095._canonical_json_bytes(sample_payload) == canonical_json_bytes(
        sample_payload
    )


def test_migration_frozen_helpers_are_not_aliases_of_live_domain_functions() -> None:
    """Guards against a future "simplification" that re-imports the live
    ``apex.domain`` functions and merely renames them with a leading
    underscore -- which would make every comparison above pass while
    reintroducing the exact live-dependency this hardening removed. The
    frozen copies must be distinct function objects, not the live ones
    under a private name, and the old public names must not resurface on
    the migration module."""

    assert migration_0095._decision_canonical_hash is not decision_canonical_hash
    assert migration_0095._classification_canonical_hash is not classification_canonical_hash
    assert migration_0095._classification_identity is not classification_identity
    assert not hasattr(migration_0095, "decision_canonical_hash")
    assert not hasattr(migration_0095, "classification_canonical_hash")
    assert not hasattr(migration_0095, "classification_identity")
