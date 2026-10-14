"""Rejected-Opportunity / Counterfactual Decision Ledger v1.

This module owns no provider client, credential, network, execution, or
capital-mutation capability, and it introduces no new investment judgment of
its own.  It normalizes decisions Apex has *already made and already
retained* durably elsewhere -- today, Shared Opportunity eligibility
(``domain.shared_opportunity.EligibilityDecision``) and Investment Mandate
evaluation (``domain.investment_mandate.MandateEvaluation``) -- into one
provider-neutral, strategy-neutral record answering: "at time T, Apex
considered candidate/opportunity X and did or did not advance it, for
reason(s) R, based on evidence E."

Deliberate design choices, so a later milestone can extend this without
redesigning it:

* **Reference, don't duplicate.**  A ``CounterfactualDecision`` never copies
  the economic/evidentiary payload already retained on the origin decision
  (``EligibilityDecision``/``MandateEvaluation``) or on the
  ``SharedOpportunityProjection`` it is about.  It stores the origin's own
  id + content hash (an unbreakable pointer -- the origin rows are
  immutable and append-only, so the pointer can never silently drift) plus
  a handful of denormalized columns (``venue``, the subject's precursor
  identity) that exist purely to make bounded filtering and cohort queries
  possible without a join, mirroring how ``MandateEvaluation`` itself
  already denormalizes ``venue``/``market_family`` onto its own row instead
  of forcing every reader back through ``SharedOpportunityProjection``.
* **Raw reasons are authoritative and immutable; analytical interpretation
  of them is not.**  ``raw_reason_codes`` always preserves the origin's
  exact reason strings verbatim and participates in ``canonical_hash`` --
  it is part of what this module attests actually happened.  A *derived*,
  evolvable classification of those same strings into normalized families
  (``ReasonFamily``, ``classify_reason``/``classify_all``, and the
  versioned ``CounterfactualDecisionClassification`` record that persists
  them) deliberately lives in the sibling module
  ``domain.counterfactual_classification`` instead of here, and never
  participates in this module's identity or ``canonical_hash``.  Counterfactual
  Decision Classification & Interpretation Versioning v1 moved that
  classification layer out of this module for exactly this reason: an
  earlier revision computed ``reason_families`` inline and folded it into
  ``canonical_hash``, which meant improving the classifier's mapping (fixing
  a previously-unmapped raw reason code, for example) would change the
  ``canonical_hash`` of an already-retained, otherwise-unchanged decision --
  a stable ``decision_id`` would then appear to carry conflicting canonical
  content on the next idempotent resync
  (``COUNTERFACTUAL_DECISION_IDENTITY_CONFLICT``) purely because analytical
  code evolved, never because the underlying decision did.  This module now
  only ever attests to what Apex's origin subsystems actually decided and
  said, in their own words; see ``domain.counterfactual_classification`` for
  how those words get interpreted, and why that interpretation is versioned
  separately.
* **Disposition is coarser than, and always derived from, origin state.**
  ``DecisionDisposition`` (``ADVANCED``/``CONSTRAINED``/``NOT_ADVANCED``) is
  the one vocabulary every integrated origin maps onto, so accepted and
  rejected cohorts can be compared across origins without the reader needing
  to know each origin's own state vocabulary.
* **Identity is a pure function of the origin decision.**  ``decision_id``
  is deterministically derived from ``(origin, origin_decision_id,
  origin_canonical_hash)``.  Re-syncing the identical already-retained
  origin decision therefore always reproduces the identical ledger row
  (idempotent replay); a changed origin decision (new economics, new
  mandate version, new portfolio snapshot, ... anything that changes the
  origin's own ``canonical_hash``) always produces a *new* ledger row,
  never a mutation of the old one -- because the origin decisions this
  module reads are themselves already immutable and already
  idempotently-versioned, this module inherits that property for free
  rather than re-implementing it.  ``canonical_hash`` (see
  ``decision_canonical_hash``) is a pure function of only the fields listed
  there -- every one of them either copied verbatim from an already-immutable
  origin record or a structural fact about *this* module's own normalization
  (never a value that depends on classifier/analytical code elsewhere).
* **The hook for future outcome attribution is the stable ``decision_id``
  itself**, not a speculative column on this table.  A later milestone can
  add a ``counterfactual_outcome`` (or similarly named) table with a
  foreign key to ``counterfactual_decision.decision_id`` without altering
  this module at all -- see the module-level ``FUTURE_OUTCOME_HOOK`` note.
  That FK target is stable under classifier evolution precisely because
  classification was moved out of this module's identity/hash: a future
  outcome can be analyzed under classifier v1 or v2 (or later) without ever
  rewriting the outcome, the decision, or this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from apex.domain.investment_mandate import MandateDecisionState, MandateEvaluation
from apex.domain.shared_opportunity import (
    EligibilityDecision,
    EligibilityState,
    SharedOpportunityProjection,
    SharedVenue,
)
from apex.domain.values import content_digest, deterministic_id

COUNTERFACTUAL_DECISION_V1 = "COUNTERFACTUAL_DECISION_V1"
COUNTERFACTUAL_LEDGER_WARNING = (
    "COUNTERFACTUAL DECISION LEDGER — HISTORICAL EVIDENCE ONLY — "
    "NO ALLOCATION OR EXECUTION AUTHORITY — NOT A HINDSIGHT STRATEGY-TUNING SIGNAL"
)
# A future milestone may add a table (e.g. ``counterfactual_outcome``) whose
# primary link back to this one is a foreign key on ``decision_id``.  Kept
# here as documentation of intent, not as a schema commitment.
FUTURE_OUTCOME_HOOK = "counterfactual_decision.decision_id"


class CounterfactualLedgerError(ValueError):
    """Raised when retained counterfactual-ledger evidence is invalid or conflicts."""


class DecisionOrigin(StrEnum):
    """Which already-existing, already-trustworthy Apex decision this ledger
    row normalizes.  New members can be added later (e.g. a bounded
    candidate-precursor origin, or a risk-kernel origin) without changing
    the meaning of any existing row."""

    SHARED_OPPORTUNITY_ELIGIBILITY = "SHARED_OPPORTUNITY_ELIGIBILITY"
    INVESTMENT_MANDATE_EVALUATION = "INVESTMENT_MANDATE_EVALUATION"


class DecisionDisposition(StrEnum):
    """The ledger's own normalized outcome, coarser than and always derived
    from the origin's own state."""

    ADVANCED = "ADVANCED"
    CONSTRAINED = "CONSTRAINED"
    NOT_ADVANCED = "NOT_ADVANCED"


_ELIGIBILITY_DISPOSITION: dict[EligibilityState, DecisionDisposition] = {
    EligibilityState.ELIGIBLE: DecisionDisposition.ADVANCED,
    EligibilityState.REJECTED: DecisionDisposition.NOT_ADVANCED,
    EligibilityState.EXPIRED: DecisionDisposition.NOT_ADVANCED,
    EligibilityState.BLOCKED_MISSING_EVIDENCE: DecisionDisposition.NOT_ADVANCED,
}
_MANDATE_DISPOSITION: dict[MandateDecisionState, DecisionDisposition] = {
    MandateDecisionState.ALLOWED: DecisionDisposition.ADVANCED,
    MandateDecisionState.CONSTRAINED: DecisionDisposition.CONSTRAINED,
    MandateDecisionState.REJECTED: DecisionDisposition.NOT_ADVANCED,
}


@dataclass(frozen=True, slots=True)
class CounterfactualDecision:
    decision_id: str
    origin: DecisionOrigin
    origin_decision_id: str
    origin_canonical_hash: str
    origin_state: str
    subject_projection_id: str
    subject_source_record_type: str
    subject_source_record_id: str
    venue: SharedVenue
    opportunity_set_id: str | None
    mandate_id: str | None
    mandate_lineage_id: str | None
    mandate_version: int | None
    mandate_hash: str | None
    portfolio_snapshot_id: str | None
    portfolio_snapshot_hash: str | None
    evaluated_at: datetime
    disposition: DecisionDisposition
    raw_reason_codes: tuple[str, ...]
    explanation: str
    canonical_hash: str
    authority: str = "NONE"


def _decision_id(
    origin: DecisionOrigin, origin_decision_id: str, origin_canonical_hash: str
) -> str:
    return deterministic_id(
        "counterfactual_decision",
        (COUNTERFACTUAL_DECISION_V1, origin.value, origin_decision_id, origin_canonical_hash),
    )


def decision_canonical_hash(
    *,
    decision_id: str,
    origin: DecisionOrigin | str,
    origin_decision_id: str,
    origin_canonical_hash: str,
    origin_state: str,
    subject_projection_id: str,
    subject_source_record_type: str,
    subject_source_record_id: str,
    venue: SharedVenue | str,
    opportunity_set_id: str | None,
    mandate_id: str | None,
    mandate_lineage_id: str | None,
    mandate_version: int | None,
    mandate_hash: str | None,
    portfolio_snapshot_id: str | None,
    portfolio_snapshot_hash: str | None,
    evaluated_at: datetime | str,
    disposition: DecisionDisposition | str,
    raw_reason_codes: tuple[str, ...],
    explanation: str,
) -> str:
    """The ``CounterfactualDecision.canonical_hash`` formula, factored out
    of the two builders below so it has exactly one implementation.

    Every argument is either copied verbatim from an already-immutable
    origin record (``origin_canonical_hash``, ``mandate_hash``,
    ``portfolio_snapshot_hash``, ``raw_reason_codes``, ...) or a structural
    fact this module itself derives once and never revises (``disposition``,
    ``explanation``).  Nothing here may ever depend on analytical/classifier
    code -- see the module docstring for why that matters.

    Exposed (not underscore-prefixed) specifically so migration
    ``0095_reason_classification`` can recompute this exact formula
    against already-retained rows -- deterministically, from each row's own
    already-persisted fields -- when it corrects the ``canonical_hash`` of
    the pre-v1.1 rows that were hashed with ``reason_families`` still
    folded in.  Do not call this to fabricate a decision that did not come
    from a real builder below; it performs no validation of its own.
    """

    payload = (
        COUNTERFACTUAL_DECISION_V1,
        decision_id,
        origin,
        origin_decision_id,
        origin_canonical_hash,
        origin_state,
        subject_projection_id,
        subject_source_record_type,
        subject_source_record_id,
        venue,
        opportunity_set_id,
        mandate_id,
        mandate_lineage_id,
        mandate_version,
        mandate_hash,
        portfolio_snapshot_id,
        portfolio_snapshot_hash,
        evaluated_at,
        disposition,
        raw_reason_codes,
        explanation,
        "NONE",
    )
    return content_digest(payload)


def build_decision_from_eligibility(
    *,
    decision: EligibilityDecision,
    projection: SharedOpportunityProjection,
    opportunity_set_id: str,
) -> CounterfactualDecision:
    """Normalize one already-retained ``EligibilityDecision`` (and the
    ``SharedOpportunityProjection`` it is about) into one
    ``CounterfactualDecision``.  Pure; performs no I/O and mutates nothing."""

    if decision.projection_id != projection.projection_id:
        raise CounterfactualLedgerError("COUNTERFACTUAL_LEDGER_ELIGIBILITY_PROJECTION_MISMATCH")
    origin = DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY
    disposition = _ELIGIBILITY_DISPOSITION[decision.state]
    explanation = (
        f"{origin.value}: {disposition.value} (origin_state={decision.state.value}) for "
        f"subject={projection.projection_id} venue={projection.venue.value} at "
        f"{decision.evaluated_at.isoformat()}; reasons=[{','.join(decision.reasons) or 'none'}]"
    )
    decision_id = _decision_id(origin, decision.decision_id, decision.canonical_hash)
    canonical_hash = decision_canonical_hash(
        decision_id=decision_id,
        origin=origin,
        origin_decision_id=decision.decision_id,
        origin_canonical_hash=decision.canonical_hash,
        origin_state=decision.state.value,
        subject_projection_id=projection.projection_id,
        subject_source_record_type=projection.source_record_type,
        subject_source_record_id=projection.source_record_id,
        venue=projection.venue,
        opportunity_set_id=opportunity_set_id,
        mandate_id=None,
        mandate_lineage_id=None,
        mandate_version=None,
        mandate_hash=None,
        portfolio_snapshot_id=None,
        portfolio_snapshot_hash=None,
        evaluated_at=decision.evaluated_at,
        disposition=disposition,
        raw_reason_codes=decision.reasons,
        explanation=explanation,
    )
    return CounterfactualDecision(
        decision_id=decision_id,
        origin=origin,
        origin_decision_id=decision.decision_id,
        origin_canonical_hash=decision.canonical_hash,
        origin_state=decision.state.value,
        subject_projection_id=projection.projection_id,
        subject_source_record_type=projection.source_record_type,
        subject_source_record_id=projection.source_record_id,
        venue=projection.venue,
        opportunity_set_id=opportunity_set_id,
        mandate_id=None,
        mandate_lineage_id=None,
        mandate_version=None,
        mandate_hash=None,
        portfolio_snapshot_id=None,
        portfolio_snapshot_hash=None,
        evaluated_at=decision.evaluated_at,
        disposition=disposition,
        raw_reason_codes=decision.reasons,
        explanation=explanation,
        canonical_hash=canonical_hash,
        authority="NONE",
    )


def build_decision_from_mandate_evaluation(
    *,
    evaluation: MandateEvaluation,
    opportunity: SharedOpportunityProjection,
) -> CounterfactualDecision:
    """Normalize one already-retained ``MandateEvaluation`` (and the
    ``SharedOpportunityProjection`` it evaluated) into one
    ``CounterfactualDecision``.  Pure; performs no I/O and mutates nothing."""

    if evaluation.opportunity_id != opportunity.projection_id:
        raise CounterfactualLedgerError("COUNTERFACTUAL_LEDGER_MANDATE_OPPORTUNITY_MISMATCH")
    origin = DecisionOrigin.INVESTMENT_MANDATE_EVALUATION
    disposition = _MANDATE_DISPOSITION[evaluation.state]
    explanation = (
        f"{origin.value}: {disposition.value} (origin_state={evaluation.state.value}) for "
        f"subject={opportunity.projection_id} venue={opportunity.venue.value} under mandate "
        f"{evaluation.mandate_lineage_id} v{evaluation.mandate_version} at "
        f"{evaluation.evaluated_at.isoformat()}; "
        f"reasons=[{','.join(evaluation.reason_codes) or 'none'}]"
    )
    decision_id = _decision_id(origin, evaluation.evaluation_id, evaluation.canonical_hash)
    canonical_hash = decision_canonical_hash(
        decision_id=decision_id,
        origin=origin,
        origin_decision_id=evaluation.evaluation_id,
        origin_canonical_hash=evaluation.canonical_hash,
        origin_state=evaluation.state.value,
        subject_projection_id=opportunity.projection_id,
        subject_source_record_type=opportunity.source_record_type,
        subject_source_record_id=opportunity.source_record_id,
        venue=opportunity.venue,
        opportunity_set_id=None,
        mandate_id=evaluation.mandate_id,
        mandate_lineage_id=evaluation.mandate_lineage_id,
        mandate_version=evaluation.mandate_version,
        mandate_hash=evaluation.mandate_hash,
        portfolio_snapshot_id=evaluation.portfolio_snapshot_id,
        portfolio_snapshot_hash=evaluation.portfolio_snapshot_hash,
        evaluated_at=evaluation.evaluated_at,
        disposition=disposition,
        raw_reason_codes=evaluation.reason_codes,
        explanation=explanation,
    )
    return CounterfactualDecision(
        decision_id=decision_id,
        origin=origin,
        origin_decision_id=evaluation.evaluation_id,
        origin_canonical_hash=evaluation.canonical_hash,
        origin_state=evaluation.state.value,
        subject_projection_id=opportunity.projection_id,
        subject_source_record_type=opportunity.source_record_type,
        subject_source_record_id=opportunity.source_record_id,
        venue=opportunity.venue,
        opportunity_set_id=None,
        mandate_id=evaluation.mandate_id,
        mandate_lineage_id=evaluation.mandate_lineage_id,
        mandate_version=evaluation.mandate_version,
        mandate_hash=evaluation.mandate_hash,
        portfolio_snapshot_id=evaluation.portfolio_snapshot_id,
        portfolio_snapshot_hash=evaluation.portfolio_snapshot_hash,
        evaluated_at=evaluation.evaluated_at,
        disposition=disposition,
        raw_reason_codes=evaluation.reason_codes,
        explanation=explanation,
        canonical_hash=canonical_hash,
        authority="NONE",
    )
