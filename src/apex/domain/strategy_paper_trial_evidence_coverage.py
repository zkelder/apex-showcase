"""Trial Evidence Coverage Disclosure v1: pure, deterministic summarization
of retained ``domain.live_market_observation.LiveMarketObservation`` evidence
density over a Strategy PAPER trial's own nominal window ``[opened_at,
closed_at]``.

PURELY DESCRIPTIVE. This module never authorizes, gates, or influences a
trading decision -- it is computed only AFTER a trial has already,
genuinely, independently closed through the existing, unmodified
``domain.strategy_paper_trial_lifecycle``/``domain.crypto_paper`` machinery,
and it feeds nothing back into horizon eligibility, close-order eligibility,
contemporaneous-evidence requirements, close-price selection, realized P&L,
cooldown, or retrial eligibility. Its sole purpose is to let a closure record
honestly say how much genuine market-observation evidence was actually
retained across the trial's real elapsed window -- since the ordinary
"trial ran uninterrupted for its full nominal horizon" case and a
"the observing infrastructure was down for a large fraction of that window"
case would otherwise be indistinguishable in the closure record alone (see
``docs/`` -- this module exists precisely because a real ~16.5-day live-
observation outage during an open trial's window left no durable trace of
that gap anywhere in this codebase before this milestone).

Owns zero I/O and imports nothing from ``apex.application``/``apex.adapters``
-- matching every other pure-decision domain module in this codebase. A
caller (``adapters.operating_cycle_tasks``) is responsible for fetching the
candidate ``LiveMarketObservation`` rows (via a repository range query) and
handing them to ``evaluate_trial_evidence_coverage`` here; this module never
reaches into a repository itself.

## Evidence eligibility

Mirrors ``domain.counterfactual_followup.FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES``'s
own allowlist shape and values (``{GENUINE_PUBLIC, HISTORICAL_PUBLIC}``) --
a fail-closed allowlist, never a denylist, so a future, not-yet-invented
``EvidenceClass`` member is excluded by default rather than silently
counted. Deliberately a SEPARATE constant (not imported from that sibling
module): coverage disclosure is a structurally distinct concept from
counterfactual follow-up evidence, and this module owns no dependency on
that one. An observation must ALSO carry ``quality is PASSED`` -- a
``FAILED``-quality row (a genuine capture attempt that could not produce a
usable quote) is not retained, usable market evidence, exactly like
``domain.live_market_observation.evaluate_contemporaneous_observation``
already treats ``FAILED`` quality as equivalent to no observation at all.

## Coverage measures

Four independent, individually mechanically-precise measures, deliberately
never collapsed into one opaque score:

- ``window_duration_seconds``: the exact nominal window length -- always
  ``(window_end - window_start).total_seconds()``, independent of how much
  evidence exists inside it.
- ``nominal_days``/``covered_days``: a coarse, dead-simple, independently
  auditable (a one-line ``SELECT DISTINCT DATE(captured_at)`` query
  reproduces ``covered_days`` exactly) day-granularity measure.
  ``nominal_days`` is the count of distinct UTC calendar dates the window's
  ``[window_start, window_end]`` timestamps touch, inclusive of both ends --
  so a window whose bounds fall on the same calendar date reads
  ``nominal_days=1``, and one spanning a whole calendar month reads however
  many distinct dates that month actually covers. This is deliberately NOT
  the same figure as ``window_duration_seconds / 86400`` (a real 30.0-day
  window can span 31 distinct UTC calendar dates when its start/end
  timestamps sit just after midnight) -- the two fields measure different
  things at different granularities and are not expected to agree.
  ``covered_days`` is the count of those same distinct dates that have AT
  LEAST ONE eligible observation; by construction ``covered_days <=
  nominal_days`` always holds.
- ``qualifying_observation_count``: the total count of eligible
  observations retained anywhere inside the window -- distinguishes a
  single lonely observation on a "covered" day from a genuinely dense one.
- ``largest_gap_seconds``: the longest span, anywhere inside
  ``[window_start, window_end]``, during which no eligible observation
  exists -- computed against the exact window boundaries (never rounded to
  a day), so it also honestly captures a gap between ``window_start``
  itself and the first eligible observation, or between the last eligible
  observation and ``window_end``. Zero eligible observations anywhere in
  the window is the degenerate case where this equals
  ``window_duration_seconds`` exactly (the entire window is one gap) --
  never a fabricated smaller number.

## Materiality is derived, never persisted

Whether a gap counts as "material" is an interpretive policy judgment (a
threshold applied to a fact), not a fact itself -- unlike the five measures
above, which are either raw window arithmetic or a mechanically precise,
independently reproducible count/query. ``TrialEvidenceCoverage`` therefore
carries no ``material_gap`` field, ``StrategyPaperTrialClosureRecord``
persists no ``coverage_material_gap`` column, and ``canonical_hash`` folds
in none of this either -- baking a fixed-threshold boolean into an
otherwise-immutable canonical closure record would silently freeze today's
``MATERIAL_EVIDENCE_GAP_SECONDS`` interpretation onto historical evidence
forever, indistinguishable from a deliberately-versioned judgment if that
threshold ever needed to change. ``is_material_coverage_gap`` below is the
one, single-sourced place that threshold is applied -- a pure function of an
already-retained ``largest_gap_seconds``, meant to be called by a read model
(``application.operator_console_v1``) at render/display time, never
persisted anywhere. Because ``largest_gap_seconds`` itself is always fully
retained, materiality can be freely reinterpreted (a different threshold, a
different rule entirely) at any future time without touching a single
historical row -- the exact flexibility a persisted boolean would foreclose.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from apex.domain.live_market_observation import LiveMarketObservation, LiveObservationQuality
from apex.domain.research_evidence import EvidenceClass

TRIAL_EVIDENCE_COVERAGE_POLICY_V1 = "TRIAL_EVIDENCE_COVERAGE_POLICY_V1"

#: See module docstring "Evidence eligibility" section.
TRIAL_EVIDENCE_COVERAGE_ELIGIBLE_EVIDENCE_CLASSES: frozenset[EvidenceClass] = frozenset(
    {EvidenceClass.GENUINE_PUBLIC, EvidenceClass.HISTORICAL_PUBLIC}
)

#: A gap must exceed this many seconds for ``is_material_coverage_gap`` to
#: return ``True``. 6 hours: comfortably longer than every ordinary transient
#: operational blip this project's own history has produced (worker
#: restarts, brief Docker/WSL2 hiccups that self-heal within minutes to a
#: couple of hours -- see the 2026-08-28 ~20.5h outage's own same-day
#: recovery record for the longest genuine precedent this codebase has for
#: an outage that was NOT multi-day), while still comfortably shorter than
#: a genuine multi-day evidence outage -- so it cleanly separates "ordinary
#: hiccup, not worth flagging" from "a real, disclosure-worthy evidence
#: gap" without requiring a full lost calendar day to trigger. A fixed,
#: named, documented threshold -- never re-derived from an unrelated
#: constant (e.g. the 1800s point-in-time staleness bound
#: ``evaluate_contemporaneous_observation`` uses is a completely different
#: concept: "is THIS ONE observation fresh enough to decide from right
#: now", not "was there a retrospectively-material gap somewhere in a
#: whole closed window").
MATERIAL_EVIDENCE_GAP_SECONDS = 21_600


class TrialEvidenceCoverageError(ValueError):
    """A coverage-summarization input violated an invariant. Fails closed."""


def is_coverage_eligible_observation(observation: LiveMarketObservation) -> bool:
    """Fail-closed allowlist membership test -- never ``evidence_class is
    not FIXTURE`` or an equivalent denylist shape. The sole authority this
    module has for "may this retained observation count toward genuine
    trial evidence coverage"; every caller consults this rather than
    restating the allowlist/quality check independently."""

    return (
        observation.quality is LiveObservationQuality.PASSED
        and observation.evidence_class in TRIAL_EVIDENCE_COVERAGE_ELIGIBLE_EVIDENCE_CLASSES
    )


@dataclass(frozen=True, slots=True)
class TrialEvidenceCoverage:
    """One immutable coverage summary for one closed trial's own window.
    Never persisted on its own -- a caller flattens this onto the
    ``coverage_*`` fields of ``domain.strategy_paper_trial_lifecycle.
    StrategyPaperTrialClosureRecord`` at closure-build time (see that
    module's own ``build_strategy_paper_trial_closure_record``)."""

    instrument_id: str
    window_start: datetime
    window_end: datetime
    window_duration_seconds: int
    nominal_days: int
    covered_days: int
    qualifying_observation_count: int
    largest_gap_seconds: int
    policy_version: str = TRIAL_EVIDENCE_COVERAGE_POLICY_V1


def is_material_coverage_gap(largest_gap_seconds: int) -> bool:
    """Display-only derivation of whether an already-retained
    ``largest_gap_seconds`` value (from a ``TrialEvidenceCoverage`` or a
    persisted ``coverage_largest_gap_seconds`` column) crosses
    ``MATERIAL_EVIDENCE_GAP_SECONDS`` -- see module docstring "Materiality
    is derived, never persisted" section for why this is a function over an
    already-computed value rather than a field on ``TrialEvidenceCoverage``
    itself. A caller never needs to independently know or re-apply
    ``MATERIAL_EVIDENCE_GAP_SECONDS`` -- this is the one place that
    threshold is applied -- but the boolean this returns is never itself
    retained anywhere; only ``largest_gap_seconds`` is canonical."""

    return largest_gap_seconds > MATERIAL_EVIDENCE_GAP_SECONDS


def evaluate_trial_evidence_coverage(
    *,
    instrument_id: str,
    window_start: datetime,
    window_end: datetime,
    observations: Sequence[LiveMarketObservation],
) -> TrialEvidenceCoverage:
    """Pure, total, deterministic given fixed inputs -- the same
    ``(instrument_id, window_start, window_end, observations)`` always
    produces the byte-identical result, which is what makes closure replay
    safe (see ``build_strategy_paper_trial_closure_record``'s own
    docstring). Defensively re-filters ``observations`` itself (instrument,
    window bounds, quality, evidence class) rather than trusting a caller's
    own pre-filtering -- mirrors ``evaluate_contemporaneous_observation``'s
    identical "caller fetches candidates, this function independently
    re-validates" split. ``observations`` may safely include rows for other
    instruments or outside the window; they are silently excluded, never an
    error -- a caller is free to pass a slightly wider read-model batch
    without needing pixel-perfect query bounds.

    Never reinterprets the absence of observations as market behavior: a
    day with zero eligible observations is reported honestly as
    uncovered, never silently skipped or backfilled from an adjacent day's
    price."""

    if window_end < window_start:
        raise TrialEvidenceCoverageError("TRIAL_EVIDENCE_COVERAGE_WINDOW_INVERTED")

    qualifying = sorted(
        (
            item
            for item in observations
            if item.instrument_id == instrument_id
            and window_start <= item.captured_at <= window_end
            and is_coverage_eligible_observation(item)
        ),
        key=lambda item: (item.captured_at, item.observation_id),
    )

    window_duration_seconds = int((window_end - window_start).total_seconds())
    nominal_days = (window_end.date() - window_start.date()).days + 1
    covered_days = len({item.captured_at.date() for item in qualifying})

    if qualifying:
        boundaries = (window_start, *(item.captured_at for item in qualifying), window_end)
        largest_gap_seconds = max(
            int((later - earlier).total_seconds()) for earlier, later in pairwise(boundaries)
        )
    else:
        largest_gap_seconds = window_duration_seconds

    return TrialEvidenceCoverage(
        instrument_id=instrument_id,
        window_start=window_start,
        window_end=window_end,
        window_duration_seconds=window_duration_seconds,
        nominal_days=nominal_days,
        covered_days=covered_days,
        qualifying_observation_count=len(qualifying),
        largest_gap_seconds=largest_gap_seconds,
    )
