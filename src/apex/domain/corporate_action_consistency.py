"""Corporate-action/adjustment consistency checks (milestone section 8).

Apex's Alpaca historical-bar capture is deliberately raw/unadjusted
(``SecuritiesAdjustmentState.RAW`` / ``ALPACA_IEX_RAW_NO_ADJUSTMENT_V1`` --
see ``domain.historical_market_data``), and the per-chunk historical
canonicalizer does not fetch ``CORPORATE_ACTIONS`` (see
``adapters.alpaca_canonicalization.HistoricalAlpacaCanonicalizer``'s own
docstring). This module does not change either fact and does not build a
corporate-action engine. It only proves Apex *knows exactly what price
series it is researching*: a raw/unadjusted series must show a large
single-session discontinuity of approximately the correct ratio around each
publicly known split's effective date, for whichever declared split falls
inside a cohort's actually-retained coverage window. A missing or
wrong-ratio discontinuity would mean the retained series is not genuinely
raw, or not genuinely this instrument -- either way, a fact worth
surfacing rather than assuming.

``KNOWN_SPLIT_FACTS_V1`` is a small, versioned table of publicly documented
historical stock-split effective dates and ratios -- corporate-action
facts, not performance data. Nothing here consults a security's return.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from apex.domain.securities import SecuritiesBarEvidence

CORPORATE_ACTION_CONSISTENCY_V1 = "CORPORATE_ACTION_CONSISTENCY_V1"
DEFAULT_RATIO_TOLERANCE = Decimal("0.25")


class SplitConsistencyOutcome(StrEnum):
    CONSISTENT = "CONSISTENT"
    INCONSISTENT_RATIO = "INCONSISTENT_RATIO"
    NO_DISCONTINUITY_DETECTED = "NO_DISCONTINUITY_DETECTED"
    OUTSIDE_RETAINED_COVERAGE = "OUTSIDE_RETAINED_COVERAGE"


@dataclass(frozen=True, slots=True)
class KnownSplitFact:
    """One publicly documented historical split -- selected because it is a
    real, well-known corporate-action record, never because of how the
    security performed before or after it."""

    symbol: str
    effective_date: str
    forward_split_ratio: Decimal
    note: str


KNOWN_SPLIT_FACTS_V1: tuple[KnownSplitFact, ...] = (
    KnownSplitFact("AAPL", "2020-08-31", Decimal(4), "Apple 4-for-1 split"),
    KnownSplitFact("TSLA", "2020-08-31", Decimal(5), "Tesla 5-for-1 split"),
    KnownSplitFact("TSLA", "2022-08-25", Decimal(3), "Tesla 3-for-1 split"),
    KnownSplitFact("GOOGL", "2022-07-18", Decimal(20), "Alphabet 20-for-1 split"),
    KnownSplitFact("NVDA", "2021-07-20", Decimal(4), "NVIDIA 4-for-1 split"),
    KnownSplitFact("NVDA", "2024-06-10", Decimal(10), "NVIDIA 10-for-1 split"),
)


@dataclass(frozen=True, slots=True)
class SplitConsistencyResult:
    symbol: str
    effective_date: str
    expected_ratio: Decimal
    observed_ratio: Decimal | None
    pre_split_date: str | None
    pre_split_close: Decimal | None
    post_split_date: str | None
    post_split_close: Decimal | None
    outcome: SplitConsistencyOutcome


def check_split_consistency(
    bars: Sequence[SecuritiesBarEvidence],
    *,
    fact: KnownSplitFact,
    tolerance: Decimal = DEFAULT_RATIO_TOLERANCE,
) -> SplitConsistencyResult:
    """Compare the closest retained raw close *before* ``fact.effective_date``
    to the closest retained raw close *on or after* it. Never raises for a
    coverage gap -- an honest ``OUTSIDE_RETAINED_COVERAGE`` result is the
    correct outcome when the cohort's retained window does not reach back
    (or forward) far enough to observe both sides."""

    if tolerance <= 0:
        raise ValueError("CORPORATE_ACTION_TOLERANCE_MUST_BE_POSITIVE")
    effective = date.fromisoformat(fact.effective_date)
    ordered = sorted(bars, key=lambda item: item.period_start)
    before = [item for item in ordered if item.period_start.date() < effective]
    at_or_after = [item for item in ordered if item.period_start.date() >= effective]
    if not before or not at_or_after:
        return SplitConsistencyResult(
            symbol=fact.symbol,
            effective_date=fact.effective_date,
            expected_ratio=fact.forward_split_ratio,
            observed_ratio=None,
            pre_split_date=before[-1].period_start.date().isoformat() if before else None,
            pre_split_close=before[-1].close if before else None,
            post_split_date=(
                at_or_after[0].period_start.date().isoformat() if at_or_after else None
            ),
            post_split_close=at_or_after[0].close if at_or_after else None,
            outcome=SplitConsistencyOutcome.OUTSIDE_RETAINED_COVERAGE,
        )
    pre = before[-1]
    post = at_or_after[0]
    if pre.close <= 0 or post.close <= 0:
        raise ValueError("CORPORATE_ACTION_NONPOSITIVE_CLOSE")
    observed_ratio = pre.close / post.close
    relative_error = abs(observed_ratio - fact.forward_split_ratio) / fact.forward_split_ratio
    if observed_ratio <= Decimal("1.5"):
        # No meaningful discontinuity at all -- clearly not a several-fold
        # split, regardless of tolerance.
        outcome = SplitConsistencyOutcome.NO_DISCONTINUITY_DETECTED
    elif relative_error <= tolerance:
        outcome = SplitConsistencyOutcome.CONSISTENT
    else:
        outcome = SplitConsistencyOutcome.INCONSISTENT_RATIO
    return SplitConsistencyResult(
        symbol=fact.symbol,
        effective_date=fact.effective_date,
        expected_ratio=fact.forward_split_ratio,
        observed_ratio=observed_ratio,
        pre_split_date=pre.period_start.date().isoformat(),
        pre_split_close=pre.close,
        post_split_date=post.period_start.date().isoformat(),
        post_split_close=post.close,
        outcome=outcome,
    )


def applicable_known_splits(
    *, symbols: Sequence[str], coverage_start: str, coverage_end: str
) -> tuple[KnownSplitFact, ...]:
    """Known splits whose symbol is in the cohort and whose effective date
    falls strictly inside the declared coverage window -- the only ones a
    genuine check can possibly evaluate both sides of."""

    symbol_set = set(symbols)
    start = date.fromisoformat(coverage_start)
    end = date.fromisoformat(coverage_end)
    return tuple(
        fact
        for fact in KNOWN_SPLIT_FACTS_V1
        if fact.symbol in symbol_set and start < date.fromisoformat(fact.effective_date) < end
    )
