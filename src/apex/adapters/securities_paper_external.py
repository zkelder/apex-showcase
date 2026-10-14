"""Isolated LEAN evidence boundary for Securities PAPER conformance."""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

from apex.domain.securities_paper import (
    EXTERNAL_ENGINE_WARNING,
    ExternalSecuritiesPaperRun,
    SecuritiesPaperRun,
)
from apex.domain.values import ContentHash, Money, content_digest, deterministic_id


class LeanSecuritiesPaperAdapter:
    """Availability-only boundary; it owns no Campaign, ledger, or broker access."""

    engine_identity = "LEAN"

    def __init__(self, local_root: Path | None = None) -> None:
        self._local_root = local_root or Path(".local/lean")

    def availability(self) -> tuple[bool, str]:
        pinned = self._local_root / "lean"
        location = str(pinned) if pinned.is_file() else shutil.which("lean")
        return (
            (True, "LEAN_RUNTIME_AVAILABLE")
            if location is not None
            else (False, "LEAN_RUNTIME_UNAVAILABLE")
        )

    def unavailable_run(
        self,
        *,
        apex_run: SecuritiesPaperRun,
        request_hash: ContentHash,
    ) -> ExternalSecuritiesPaperRun:
        available, state = self.availability()
        result_state = "FAILED" if available else "LEAN_RUNTIME_UNAVAILABLE"
        warning = (
            "LEAN_RUNTIME_PRESENT_BUT_BOUNDED_PAPER_RUNNER_NOT_CONFIGURED"
            if available
            else "LEAN_RUNTIME_UNAVAILABLE"
        )
        return ExternalSecuritiesPaperRun(
            external_run_id=deterministic_id(
                "external_securities_paper_run",
                {
                    "apex_run_id": apex_run.run_id,
                    "request_hash": request_hash,
                    "state": result_state,
                },
            ),
            engine_identity=self.engine_identity,
            engine_version=state,
            state=result_state,
            apex_run_id=apex_run.run_id,
            request_hash=request_hash,
            result_hash=None,
            order_count=0,
            fill_count=0,
            position_count=0,
            ending_cash=None,
            realized_pnl=None,
            maximum_drawdown=None,
            canonical_events=(),
            warnings=(EXTERNAL_ENGINE_WARNING, warning),
        )


class DeterministicLeanPaperFixtureAdapter:
    """Protocol fixture that is always labeled as non-LEAN execution."""

    def __init__(self, *, explained_difference: bool = False) -> None:
        self._explained_difference = explained_difference

    def run(
        self,
        *,
        apex_run: SecuritiesPaperRun,
        apex_events: tuple[tuple[str, str], ...],
    ) -> ExternalSecuritiesPaperRun:
        events = apex_events
        if self._explained_difference and events:
            events = (*events[:-1], ("EXPLAINED_FIXTURE_DIFFERENCE", "one"))
        result_payload = {
            "apex_run_id": apex_run.run_id,
            "events": events,
            "explained_difference": self._explained_difference,
        }
        result_hash = ContentHash(content_digest(result_payload))
        return ExternalSecuritiesPaperRun(
            external_run_id=deterministic_id(
                "external_securities_paper_run",
                {"result_hash": result_hash},
            ),
            engine_identity=(
                "LEAN_FIXTURE_EXPLAINED" if self._explained_difference else "LEAN_FIXTURE_EXACT"
            ),
            engine_version="PAPER_PROTOCOL_FIXTURE_V1",
            state="COMPLETED",
            apex_run_id=apex_run.run_id,
            request_hash=apex_run.canonical_input_hash,
            result_hash=result_hash,
            order_count=apex_run.order_count,
            fill_count=apex_run.fill_count,
            position_count=apex_run.position_count,
            ending_cash=Money(apex_run.ending_cash.amount),
            realized_pnl=(
                apex_run.net_realized_pnl
                + (Decimal("0.01") if self._explained_difference else Decimal(0))
            ),
            maximum_drawdown=apex_run.maximum_drawdown,
            canonical_events=events,
            warnings=(
                EXTERNAL_ENGINE_WARNING,
                "DETERMINISTIC_FIXTURE_NOT_ACTUAL_LEAN",
            ),
        )
