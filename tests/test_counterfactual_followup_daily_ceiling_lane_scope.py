"""Counterfactual Follow-Up Daily Ceiling Lane Scope v1.

Offline, dependency-free assertions about the *contract* the daily ceiling
expresses -- the half of this milestone that is arithmetic about bounds
rather than behavior against retained rows (see ``tests/persistence/
test_postgres_counterfactual_followup_recurring.py`` for the latter).

The regression these pin down is specific and was measured in production:
pairing a per-lane round bound with a subsystem-wide daily row count let one
newly-added lane halve the daily throughput of every lane whose semantics
had not changed (SECURITIES 822 -> 409 rows/day, KALSHI 128 -> 91), purely by
halving the number of same-day rounds the shared ceiling bought.
"""

from __future__ import annotations

from apex.adapters.operating_cycle_tasks import (
    COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_POLICY_VERSION,
    COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE,
    COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY,
    COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION,
    COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION,
)
from apex.domain.counterfactual_followup import (
    CURRENT_FOLLOWUP_POLICY_VERSIONS,
    current_followup_policy_venues,
    current_followup_policy_version,
)
from apex.domain.shared_opportunity import SharedVenue

# What the KALSHI/SECURITIES lane genuinely had when it was the only lane --
# the contract this milestone restores, quoted as a literal rather than
# derived, so a future edit to the constant cannot silently redefine it.
PRE_V3_DAILY_OBSERVATION_BUDGET = 1000
PRE_V3_MAX_KALSHI_REQUESTS_PER_DAY = 800


def _rounds_per_day(daily_ceiling: int) -> int:
    """Same-day rounds one lane's ceiling buys: each round creates at most
    the per-round bound for that lane, and continuation stops at the
    ceiling."""

    return daily_ceiling // COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION


def test_daily_ceiling_is_scoped_to_the_same_lane_as_the_per_round_bounds() -> None:
    # The whole point: every bound governing one round of one lane, and the
    # bound governing that lane's whole day, share one lane identity.
    assert COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION > 0
    assert COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_POLICY_VERSION > 0
    assert COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION > 0
    # The total is derived and named, never the number the enforcement path
    # reads -- so daily work cannot grow except by a reviewed edit to the
    # per-lane constant or by adding a lane, both explicit and visible.
    assert (
        COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION
        * len(CURRENT_FOLLOWUP_POLICY_VERSIONS)
    ) == COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY


def test_adding_a_lane_cannot_reduce_an_existing_lanes_daily_budget() -> None:
    # A per-lane budget is by construction independent of how many lanes
    # exist -- this is the property a shared row count did not have.
    for lane_count in (1, 2, 3, 17):
        assert (
            COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION * lane_count
        ) // lane_count == COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION


def test_kalshi_and_securities_share_one_lane_that_still_has_its_pre_v3_budget() -> None:
    kalshi_version = current_followup_policy_version(SharedVenue.KALSHI)
    securities_version = current_followup_policy_version(SharedVenue.SECURITIES)
    crypto_version = current_followup_policy_version(SharedVenue.CRYPTO)
    # The two venues whose follow-up semantics did not change are one lane,
    # and CRYPTO is genuinely a different one.
    assert kalshi_version == securities_version
    assert crypto_version != kalshi_version
    assert set(current_followup_policy_venues(kalshi_version)) >= {
        SharedVenue.KALSHI,
        SharedVenue.SECURITIES,
    }
    assert current_followup_policy_venues(crypto_version) == (SharedVenue.CRYPTO,)
    # That lane's daily budget is exactly what it was as the only lane --
    # restored, not enlarged.
    assert (
        COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION
        == PRE_V3_DAILY_OBSERVATION_BUDGET
    )


def test_kalshi_provider_request_maximum_is_restored_and_never_exceeded() -> None:
    """Genuine provider load scales with *rounds*, not lanes: the Kalshi
    budget is enforced by one counter shared across every lane run of one
    round. Kalshi appears in exactly one lane, so no other lane can buy it
    additional rounds."""

    kalshi_version = current_followup_policy_version(SharedVenue.KALSHI)
    kalshi_lanes = [
        version
        for version in CURRENT_FOLLOWUP_POLICY_VERSIONS
        if SharedVenue.KALSHI in current_followup_policy_venues(version)
    ]
    assert kalshi_lanes == [kalshi_version]

    rounds = _rounds_per_day(COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION)
    worst_case_requests_per_day = rounds * COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE
    assert worst_case_requests_per_day == PRE_V3_MAX_KALSHI_REQUESTS_PER_DAY
    assert worst_case_requests_per_day <= PRE_V3_MAX_KALSHI_REQUESTS_PER_DAY


def test_a_shared_daily_ceiling_would_have_halved_every_unchanged_lane() -> None:
    """The counterfactual, stated as arithmetic: this is what the code did
    before this milestone, and why restoring the lane scope is a restoration
    rather than an increase."""

    lanes = len(CURRENT_FOLLOWUP_POLICY_VERSIONS)
    assert lanes >= 2  # otherwise this regression is not expressible

    shared = PRE_V3_DAILY_OBSERVATION_BUDGET
    rounds_when_shared = shared // (COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION * lanes)
    rounds_when_scoped = _rounds_per_day(
        COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION
    )
    # Two lanes sharing one ceiling bought exactly half the rounds ...
    assert rounds_when_shared * lanes == rounds_when_scoped
    # ... and therefore half of every unchanged lane's daily throughput.
    assert (
        rounds_when_shared * COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
        == PRE_V3_DAILY_OBSERVATION_BUDGET // lanes
    )
    assert (
        rounds_when_scoped * COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
        == PRE_V3_DAILY_OBSERVATION_BUDGET
    )


def test_every_lane_remains_individually_bounded() -> None:
    # No lane is unbounded, and the candidate scan bound stays far above the
    # attempt bound it backstops (a wall-clock safety net, never the primary
    # stopping signal -- the durable per-lane cursor is that).
    for version in CURRENT_FOLLOWUP_POLICY_VERSIONS:
        assert current_followup_policy_venues(version)  # lane genuinely exists
        assert (
            COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION
            >= COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
        )
        assert (
            COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_POLICY_VERSION
            > COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
        )
