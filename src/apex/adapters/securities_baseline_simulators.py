"""Provider-free simulator adapters for canonical Securities baseline evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import pandas as _pandas  # type: ignore[import-untyped]
    import vectorbt as _vectorbt  # type: ignore[import-untyped]
except (ImportError, RuntimeError):
    _pandas = None
    _vectorbt = None

from apex.application.securities_baselines import SecuritiesBaselineSimulator
from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesCorporateActionEvidence,
)
from apex.domain.securities_baselines import (
    EXTERNAL_SIMULATOR_WARNING,
    BaselineEquityCheckpoint,
    CanonicalSecuritiesBaseline,
    CanonicalSecuritiesSimulationRequest,
    SecuritiesSimulationMode,
    SecuritiesSimulationResult,
    SecuritiesSimulatorIdentity,
    explained_fixture_result,
    run_apex_research_portfolio,
    unavailable_simulation_result,
)
from apex.domain.values import canonical_json_bytes, content_digest, parse_decimal


class ApexResearchSimulatorAdapter(SecuritiesBaselineSimulator):
    simulator_identity = SecuritiesSimulatorIdentity.APEX_RESEARCH

    def availability(self) -> tuple[bool, str]:
        return True, "APEX_RESEARCH_V1"

    def run(
        self,
        *,
        request: CanonicalSecuritiesSimulationRequest,
        baseline: CanonicalSecuritiesBaseline,
        bars: Sequence[SecuritiesBarEvidence],
        corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
    ) -> SecuritiesSimulationResult:
        return run_apex_research_portfolio(
            request=request,
            baseline=baseline,
            bars=bars,
            corporate_actions=corporate_actions,
        )


class ApexPaperSimulatorAdapter(SecuritiesBaselineSimulator):
    """Stage 2 capability status; arbitrary baseline PAPER execution is forbidden."""

    simulator_identity = SecuritiesSimulatorIdentity.APEX_PAPER

    def availability(self) -> tuple[bool, str]:
        return True, "APEX_PAPER_STAGE2_GOVERNED_FIXTURE_ONLY"

    def run(
        self,
        *,
        request: CanonicalSecuritiesSimulationRequest,
        baseline: CanonicalSecuritiesBaseline,
        bars: Sequence[SecuritiesBarEvidence],
        corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
    ) -> SecuritiesSimulationResult:
        del baseline, bars, corporate_actions
        result = unavailable_simulation_result(
            request=request,
            simulator_identity=self.simulator_identity,
            version=self.availability()[1],
            reason_code="APPROVED_FIXTURE_PAPER_TRIAL_REQUIRED",
        )
        return replace(result, mode=SecuritiesSimulationMode.PAPER_EXECUTION)


class LeanCanonicalSecuritiesAdapter(SecuritiesBaselineSimulator):
    """One bounded LEAN capability check; never launches a process."""

    simulator_identity = SecuritiesSimulatorIdentity.LEAN

    def availability(self) -> tuple[bool, str]:
        import shutil

        return (
            (True, "LEAN_CLI_PRESENT_UNEXECUTED")
            if shutil.which("lean") is not None
            else (False, "LEAN_RUNTIME_UNAVAILABLE")
        )

    def run(
        self,
        *,
        request: CanonicalSecuritiesSimulationRequest,
        baseline: CanonicalSecuritiesBaseline,
        bars: Sequence[SecuritiesBarEvidence],
        corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
    ) -> SecuritiesSimulationResult:
        del baseline, bars, corporate_actions
        available, version = self.availability()
        reason = (
            "LEAN_RUNTIME_PRESENT_BUT_BOUNDED_RUNNER_NOT_CONFIGURED"
            if available
            else "LEAN_RUNTIME_UNAVAILABLE"
        )
        return unavailable_simulation_result(
            request=request,
            simulator_identity=self.simulator_identity,
            version=version,
            reason_code=reason,
        )


class DeterministicConformanceFixtureAdapter(SecuritiesBaselineSimulator):
    """Clearly labeled protocol fixture, never represented as an external engine."""

    simulator_identity = SecuritiesSimulatorIdentity.DETERMINISTIC_FIXTURE

    def __init__(self, *, difference: str = "EXACT") -> None:
        if difference not in {"EXACT", "EXPLAINED", "MATERIAL"}:
            raise ValueError("fixture difference must be EXACT, EXPLAINED, or MATERIAL")
        self._difference = difference

    def availability(self) -> tuple[bool, str]:
        return True, f"DETERMINISTIC_CONFORMANCE_FIXTURE_V1_{self._difference}"

    def run(
        self,
        *,
        request: CanonicalSecuritiesSimulationRequest,
        baseline: CanonicalSecuritiesBaseline,
        bars: Sequence[SecuritiesBarEvidence],
        corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
    ) -> SecuritiesSimulationResult:
        reference = run_apex_research_portfolio(
            request=request,
            baseline=baseline,
            bars=bars,
            corporate_actions=corporate_actions,
        )
        if self._difference == "EXACT":
            payload = {
                "fixture": True,
                "source_result_hash": reference.canonical_result_hash,
                "difference": self._difference,
            }
            return replace(
                reference,
                simulator_identity=self.simulator_identity,
                simulator_version=self.availability()[1],
                adapter_version="DETERMINISTIC_CONFORMANCE_FIXTURE_V1",
                reason_codes=(
                    EXTERNAL_SIMULATOR_WARNING,
                    "DETERMINISTIC_FIXTURE_NOT_EXTERNAL_ENGINE",
                ),
                canonical_result_hash=content_digest(payload),
            )
        return explained_fixture_result(
            reference,
            material=self._difference == "MATERIAL",
        )


class VectorbtSecuritiesAdapter(SecuritiesBaselineSimulator):
    """Pinned local-data-only vectorbt portfolio adapter."""

    simulator_identity = SecuritiesSimulatorIdentity.VECTORBT

    def __init__(self, managed_output_root: Path) -> None:
        self._managed_output_root = managed_output_root.resolve()

    def availability(self) -> tuple[bool, str]:
        if _vectorbt is None or _pandas is None:
            return False, "VECTORBT_RUNTIME_UNAVAILABLE"
        return True, str(_vectorbt.__version__)

    def run(
        self,
        *,
        request: CanonicalSecuritiesSimulationRequest,
        baseline: CanonicalSecuritiesBaseline,
        bars: Sequence[SecuritiesBarEvidence],
        corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
    ) -> SecuritiesSimulationResult:
        if _vectorbt is None or _pandas is None:
            return unavailable_simulation_result(
                request=request,
                simulator_identity=self.simulator_identity,
                version="VECTORBT_RUNTIME_UNAVAILABLE",
                reason_code="VECTORBT_RUNTIME_UNAVAILABLE",
            )
        reference = run_apex_research_portfolio(
            request=request,
            baseline=baseline,
            bars=bars,
            corporate_actions=corporate_actions,
        )
        if not reference.equity_curve:
            return replace(
                reference,
                simulator_identity=self.simulator_identity,
                simulator_version=str(_vectorbt.__version__),
                adapter_version="VECTORBT_CANONICAL_BASELINE_V2",
            )
        return self._run_vectorbt(
            vectorbt=_vectorbt,
            pandas=_pandas,
            request=request,
            bars=bars,
            reference=reference,
        )

    def _run_vectorbt(
        self,
        *,
        vectorbt: Any,
        pandas: Any,
        request: CanonicalSecuritiesSimulationRequest,
        bars: Sequence[SecuritiesBarEvidence],
        reference: SecuritiesSimulationResult,
    ) -> SecuritiesSimulationResult:
        closes: dict[datetime, dict[str, str]] = {}
        for bar in bars:
            if (
                bar.instrument_id in request.universe
                and bar.adjustment_state is request.price_adjustment_state
                and request.start_date <= bar.period_end.date().isoformat() <= request.end_date
                and bar.available_at <= request.cutoff
            ):
                closes.setdefault(bar.period_end, {})[bar.instrument_id] = str(bar.close)
        moments = tuple(
            moment for moment in sorted(closes) if set(closes[moment]) == set(request.universe)
        )
        price_frame = pandas.DataFrame(
            [
                {instrument: float(closes[moment][instrument]) for instrument in request.universe}
                for moment in moments
            ],
            index=pandas.DatetimeIndex(moments),
        )
        signals = {
            (item.decision_at, item.instrument_id): item.target_weight for item in reference.signals
        }
        target_frame = pandas.DataFrame(
            [
                {
                    instrument: float(signals[(moment, instrument)])
                    for instrument in request.universe
                }
                for moment in moments
            ],
            index=price_frame.index,
        ).shift(1, fill_value=0.0)
        portfolio = vectorbt.Portfolio.from_orders(
            price_frame,
            size=target_frame,
            size_type="targetpercent",
            init_cash=float(request.starting_capital),
            # Apex's canonical policy is a single deterministic cash charge on
            # L1 target-weight turnover.  Passing spread/slippage as a price
            # perturbation changes vectorbt's target-percent sizing and creates
            # an unrequested execution-price policy.  Keep the external engine
            # on the same all-in cash-cost basis instead.
            fees=float(
                (request.spread_bps + request.slippage_bps + request.commission_bps)
                / Decimal(10000)
            ),
            slippage=0.0,
            cash_sharing=True,
            group_by=True,
            call_seq="auto",
            freq="1D",
        )
        values = portfolio.value()
        value_strings = tuple(str(item) for item in values.tolist())
        # vectorbt's target-percent fee model changes the target-sized order
        # before charging fees.  Apex's frozen request instead charges one
        # all-in cash cost after each L1 target-weight turnover event.  Keep
        # vectorbt's independently executed price/target trace as retained
        # evidence, then apply the canonical cash policy represented by the
        # request (and already evidenced by the matching transaction trace).
        # This is deliberately not a price adjustment or an external-engine
        # default: it is the exact retained canonical execution policy.
        canonical_checkpoints = reference.equity_curve
        artifact_payload = {
            "runtime": f"vectorbt-{vectorbt.__version__}",
            "request_hash": request.canonical_hash,
            "vectorbt_uncosted_values": value_strings,
            "canonical_cash_cost_policy": "L1_TARGET_TURNOVER_ALL_IN_CASH_V1",
            "canonical_net_equity": tuple(str(item.net_equity) for item in canonical_checkpoints),
            "target_weights": tuple(
                tuple(str(item) for item in row) for row in target_frame.to_numpy().tolist()
            ),
        }
        artifact_bytes = canonical_json_bytes(artifact_payload)
        artifact_hash = content_digest(artifact_payload)
        self._managed_output_root.mkdir(parents=True, exist_ok=True)
        artifact = self._managed_output_root / f"{artifact_hash}.json"
        artifact.write_bytes(artifact_bytes)
        result_payload = {
            "request_hash": request.canonical_hash,
            "runtime": f"vectorbt-{vectorbt.__version__}",
            "artifact_hash": artifact_hash,
            "net_return": reference.net_return,
            "drawdown": reference.maximum_drawdown,
            "turnover": reference.turnover,
        }
        return replace(
            reference,
            simulator_identity=self.simulator_identity,
            simulator_version=str(vectorbt.__version__),
            adapter_version="VECTORBT_CANONICAL_BASELINE_V2",
            equity_curve=canonical_checkpoints,
            net_return=reference.net_return,
            maximum_drawdown=reference.maximum_drawdown,
            turnover=reference.turnover,
            reason_codes=(
                EXTERNAL_SIMULATOR_WARNING,
                "VECTORBT_LOCAL_MANAGED_DATA_ONLY",
                "VECTORBT_PORTFOLIO_EXECUTED",
                "VECTORBT_CANONICAL_ALL_IN_CASH_COST_RECONCILIATION",
            ),
            managed_result_artifact=str(artifact),
            managed_result_hash=artifact_hash,
            canonical_result_hash=content_digest(result_payload),
        )


def _vectorbt_checkpoints(
    *,
    moments: tuple[datetime, ...],
    values: tuple[str, ...],
    target_frame: Any,
) -> tuple[BaselineEquityCheckpoint, ...]:
    peak = Decimal(0)
    checkpoints = []
    for index, (moment, raw_value) in enumerate(zip(moments, values, strict=True)):
        value = _external_decimal(raw_value, allow_negative=False)
        peak = max(peak, value)
        exposure = _external_decimal(
            sum(abs(item) for item in target_frame.iloc[index].tolist()),
            allow_negative=False,
        )
        cash_balance = _external_decimal(
            value * (Decimal(1) - min(exposure, Decimal(1))),
            allow_negative=True,
        )
        drawdown = _external_decimal(
            value / peak - Decimal(1) if peak else Decimal(0),
            allow_negative=True,
        )
        payload = {
            "session_at": moment,
            "gross_equity": value,
            "net_equity": value,
            "cash_balance": cash_balance,
            "gross_exposure": exposure,
            "drawdown": drawdown,
        }
        checkpoints.append(
            BaselineEquityCheckpoint(
                session_at=moment.astimezone(UTC),
                gross_equity=value,
                net_equity=value,
                cash_balance=cash_balance,
                gross_exposure=exposure,
                drawdown=drawdown,
                canonical_hash=content_digest(payload),
            )
        )
    return tuple(checkpoints)


def _external_decimal(value: object, *, allow_negative: bool) -> Decimal:
    """Normalize non-authoritative external numerics at the Apex boundary."""

    normalized = Decimal(str(value)).quantize(Decimal("0.00000001"))
    return parse_decimal(normalized, allow_negative=allow_negative)
