"""Counterfactual V2 Research Evidence Quarantine v1 -- domain proofs.

The quarantine is a *forward sourcing* restriction declared in one place
(``domain.counterfactual_followup.DEFECTIVE_FOLLOWUP_POLICY_VERSIONS``).
These tests pin what it does, what it deliberately does not do, and the
structural properties that keep it from becoming a second, competing
notion of evidence validity.
"""

from __future__ import annotations

import pytest

from apex.domain.counterfactual_followup import (
    CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE,
    DEFECTIVE_FOLLOWUP_POLICY_VERSIONS,
    FOLLOWUP_POLICY_V1,
    FOLLOWUP_POLICY_V2,
    FOLLOWUP_POLICY_V3,
    KNOWN_FOLLOWUP_POLICY_VERSIONS,
    followup_policy_version_defect,
    followup_policy_version_may_source_new_research,
)
from apex.domain.shared_opportunity import SharedVenue


def test_crypto_v2_is_quarantined_from_sourcing_new_research() -> None:
    assert not followup_policy_version_may_source_new_research(
        venue=SharedVenue.CRYPTO, policy_version=FOLLOWUP_POLICY_V2
    )
    defect = followup_policy_version_defect(
        venue=SharedVenue.CRYPTO, policy_version=FOLLOWUP_POLICY_V2
    )
    assert defect is not None
    assert defect.defect_code == "FORWARD_RETURN_TEMPORAL_INTEGRITY_DEFECT"
    assert defect.corrected_by_policy_version == FOLLOWUP_POLICY_V3


def test_crypto_v1_is_quarantined_by_its_own_already_documented_defect() -> None:
    """V1's hindsight-leaking DECISION_TIME baseline is a defect of the same
    class, recorded in ``domain.counterfactual_outcome``'s module docstring
    long before this milestone. Stating it makes the table a general rule
    rather than a one-off carve-out for V2."""

    defect = followup_policy_version_defect(
        venue=SharedVenue.CRYPTO, policy_version=FOLLOWUP_POLICY_V1
    )
    assert defect is not None
    assert defect.corrected_by_policy_version == FOLLOWUP_POLICY_V2


@pytest.mark.parametrize("venue", [SharedVenue.KALSHI, SharedVenue.SECURITIES])
def test_kalshi_and_securities_v2_are_completely_unaffected(venue: SharedVenue) -> None:
    """The defect lives in the CRYPTO builder alone. KALSHI and SECURITIES
    share only the version *label*, and both remain current and sound."""

    assert followup_policy_version_may_source_new_research(
        venue=venue, policy_version=FOLLOWUP_POLICY_V2
    )
    assert followup_policy_version_defect(venue=venue, policy_version=FOLLOWUP_POLICY_V2) is None


def test_crypto_current_policy_version_may_always_source_new_research() -> None:
    """The structural invariant that stops the table from ever quarantining
    a venue into having no usable evidence at all: whatever a venue is
    *currently* observed under must never itself be quarantined."""

    for venue, version in CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE.items():
        assert followup_policy_version_may_source_new_research(
            venue=venue, policy_version=version
        ), f"{venue.value}'s current policy version {version} is quarantined"


def test_every_declared_defect_names_a_known_corrective_version() -> None:
    for defect in DEFECTIVE_FOLLOWUP_POLICY_VERSIONS:
        assert defect.policy_version in KNOWN_FOLLOWUP_POLICY_VERSIONS
        assert defect.corrected_by_policy_version in KNOWN_FOLLOWUP_POLICY_VERSIONS
        assert defect.corrected_by_policy_version != defect.policy_version
        assert defect.defect_code and defect.summary


def test_declared_defects_are_unique_per_venue_and_policy_version() -> None:
    keys = [(item.venue, item.policy_version) for item in DEFECTIVE_FOLLOWUP_POLICY_VERSIONS]
    assert len(keys) == len(set(keys))


def test_an_unrecognized_policy_version_is_not_fabricated_into_a_defect() -> None:
    """This gate answers "is a reviewed defect recorded against this pair",
    not "is this pair proven clean" -- inventing a defect for a version
    nobody assessed would be the opposite of the honesty it protects."""

    assert followup_policy_version_may_source_new_research(
        venue=SharedVenue.CRYPTO, policy_version="FOLLOWUP_POLICY_V99"
    )


def test_quarantine_says_nothing_about_validity_of_retained_rows() -> None:
    """A quarantined version stays fully known, fully valid, and fully
    re-runnable -- the restriction is exclusively about originating new
    research conclusions."""

    assert FOLLOWUP_POLICY_V2 in KNOWN_FOLLOWUP_POLICY_VERSIONS
    assert FOLLOWUP_POLICY_V1 in KNOWN_FOLLOWUP_POLICY_VERSIONS
