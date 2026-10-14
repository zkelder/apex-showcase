"""Bounded external securities-engine protocol and deterministic fixture runner."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from apex.application.securities import ExternalSecuritiesEnginePort
from apex.domain.securities import (
    ExternalSecuritiesEngineRequest,
    ExternalSecuritiesEngineRun,
    ExternalSecuritiesEngineState,
    ExternalSecuritiesSignal,
    SecuritiesResearchRun,
    SecuritiesSignal,
)
from apex.domain.values import content_digest, deterministic_id, parse_decimal


class LeanSecuritiesEngineAdapter(ExternalSecuritiesEnginePort):
    """Availability-only LEAN boundary with no process, network, or credential access.

    Stage 1 freezes the canonical protocol and reports local runtime availability.
    It deliberately does not start LEAN.  The deterministic fixture adapter below
    is the only runner used by tests and the demo.
    """

    engine_identity = "LEAN"

    def availability(self) -> tuple[bool, str]:
        location = shutil.which("lean")
        return (
            (True, "LEAN_CLI_PRESENT_UNEXECUTED")
            if location is not None
            else (False, "LEAN_RUNTIME_UNAVAILABLE")
        )

    def run(
        self,
        *,
        request: ExternalSecuritiesEngineRequest,
        apex_signals: Sequence[SecuritiesSignal],
        apex_run: SecuritiesResearchRun,
    ) -> ExternalSecuritiesEngineRun:
        available, version = self.availability()
        now = datetime.now(UTC)
        state = (
            ExternalSecuritiesEngineState.FAILED
            if available
            else ExternalSecuritiesEngineState.LEAN_RUNTIME_UNAVAILABLE
        )
        warning = (
            "LEAN_RUNTIME_PRESENT_BUT_STAGE1_EXECUTION_DISABLED"
            if available
            else "LEAN_RUNTIME_UNAVAILABLE"
        )
        return ExternalSecuritiesEngineRun(
            external_run_id=deterministic_id(
                "external_securities_run",
                {"request_hash": request.canonical_hash, "state": state},
            ),
            research_run_id=request.research_run_id,
            engine_identity=self.engine_identity,
            engine_version=version,
            state=state,
            request_hash=request.canonical_hash,
            result_hash=None,
            signals=(),
            benchmark_return=None,
            hypothetical_gross_return=None,
            hypothetical_cost=None,
            warnings=(warning,),
            started_at=now,
            completed_at=now,
        )


class DeterministicLeanFixtureAdapter(ExternalSecuritiesEnginePort):
    """Protocol-only deterministic evidence, never represented as real LEAN."""

    def __init__(
        self,
        *,
        explained_difference: bool = False,
        engine_identity: str = "LEAN_FIXTURE",
    ) -> None:
        self._explained_difference = explained_difference
        self.engine_identity = engine_identity

    def availability(self) -> tuple[bool, str]:
        return True, "LEAN_FIXTURE_PROTOCOL_V1"

    def run(
        self,
        *,
        request: ExternalSecuritiesEngineRequest,
        apex_signals: Sequence[SecuritiesSignal],
        apex_run: SecuritiesResearchRun,
    ) -> ExternalSecuritiesEngineRun:
        now = datetime.now(UTC)
        signals = tuple(
            ExternalSecuritiesSignal(
                instrument_id=signal.instrument_id,
                signal_at=signal.signal_at,
                direction=signal.direction,
                target_weight=signal.target_weight,
            )
            for signal in apex_signals
        )
        if self._explained_difference and signals:
            first = signals[0]
            changed = replace(
                first,
                target_weight=parse_decimal(
                    first.target_weight + Decimal("0.01"),
                    allow_negative=True,
                ),
            )
            signals = (changed, *signals[1:])
        result_payload = {
            "request_hash": request.canonical_hash,
            "signals": signals,
            "benchmark_return": apex_run.metrics.benchmark_return,
            "hypothetical_gross_return": apex_run.metrics.hypothetical_gross_return,
            "hypothetical_cost": apex_run.metrics.hypothetical_cost,
            "fixture_difference": self._explained_difference,
        }
        return ExternalSecuritiesEngineRun(
            external_run_id=deterministic_id(
                "external_securities_run",
                {
                    "request_hash": request.canonical_hash,
                    "result_hash": content_digest(result_payload),
                },
            ),
            research_run_id=request.research_run_id,
            engine_identity=self.engine_identity,
            engine_version=self.availability()[1],
            state=ExternalSecuritiesEngineState.COMPLETED,
            request_hash=request.canonical_hash,
            result_hash=content_digest(result_payload),
            signals=signals,
            benchmark_return=apex_run.metrics.benchmark_return,
            hypothetical_gross_return=apex_run.metrics.hypothetical_gross_return,
            hypothetical_cost=apex_run.metrics.hypothetical_cost,
            warnings=(
                ("DETERMINISTIC_FIXTURE_EXPLAINED_DIFFERENCE",)
                if self._explained_difference
                else ("DETERMINISTIC_FIXTURE_NOT_ACTUAL_LEAN",)
            ),
            started_at=now,
            completed_at=now,
        )
