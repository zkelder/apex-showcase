"""Idempotent synchronization of Counterfactual Decision Classification from
already-retained ``CounterfactualDecision`` rows.

Reads every already-retained decision through a narrow read-only source
protocol, classifies each one's already-immutable ``raw_reason_codes`` under
one named classifier version via the pure builder in
``domain.counterfactual_classification``, and persists the result
idempotently.  Performs no provider I/O, creates no new investment
judgment, and never writes to ``counterfactual_decision`` -- it only ever
reads decisions and adds classification rows that reference them.

Like ``application.counterfactual_ledger``'s sync services, this is
deliberately CLI-triggered rather than auto-wired into the recurring
operating cycle -- re-running it is always safe (unchanged decision +
classifier version replays without writing anything new) but that is an
operator/researcher decision, made deliberately to avoid adding any new
moving part to the cycle that currently protects the options-forward
capture window.  There is deliberately no "last synced" cursor, for the
same reason ``application.counterfactual_ledger`` has none: at today's
evidence volume a full idempotent rescan is simpler and strictly safer than
cursor state that could itself drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.counterfactual_classification import (
    CURRENT_CLASSIFIER_VERSION,
    CounterfactualDecisionClassification,
    build_classification,
)
from apex.domain.counterfactual_ledger import CounterfactualDecision


class CounterfactualClassificationRepository(Protocol):
    def save_classification(
        self, *, classification: CounterfactualDecisionClassification
    ) -> bool: ...


class CounterfactualDecisionSource(Protocol):
    def all_decisions(self) -> tuple[CounterfactualDecision, ...]: ...


@dataclass(frozen=True, slots=True)
class ClassificationSyncResult:
    classifier_version: str
    considered_count: int
    newly_written_count: int
    idempotent_replay_count: int
    classification_ids: tuple[str, ...]
    classified_at: datetime


class SyncCounterfactualClassification:
    """Classify every retained ``CounterfactualDecision`` -- across every
    integrated origin, every disposition -- under one named classifier
    version.  ``execute`` is safe to call repeatedly with the same version
    (idempotent replay) and safe to call with a new version at any time
    (adds new, independent rows; never touches an earlier version's rows or
    the decisions themselves)."""

    def __init__(
        self,
        repository: CounterfactualClassificationRepository,
        source: CounterfactualDecisionSource,
    ) -> None:
        self._repository = repository
        self._source = source

    def execute(
        self, *, classified_at: datetime, classifier_version: str = CURRENT_CLASSIFIER_VERSION
    ) -> ClassificationSyncResult:
        classification_ids: list[str] = []
        replay_count = 0
        for decision in self._source.all_decisions():
            built = build_classification(
                decision=decision,
                classified_at=classified_at,
                classifier_version=classifier_version,
            )
            replay = self._repository.save_classification(classification=built)
            classification_ids.append(built.classification_id)
            replay_count += 1 if replay else 0
        return ClassificationSyncResult(
            classifier_version=classifier_version,
            considered_count=len(classification_ids),
            newly_written_count=len(classification_ids) - replay_count,
            idempotent_replay_count=replay_count,
            classification_ids=tuple(classification_ids),
            classified_at=classified_at,
        )
