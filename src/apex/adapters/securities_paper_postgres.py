"""Atomic PostgreSQL adapter for governed Securities PAPER evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from apex.adapters.postgres import (
    PostgresCampaignRepository,
    PostgresLedgerRepository,
    campaign_operating_state,
    external_securities_paper_run,
    ledger_account,
    ledger_journal,
    ledger_posting,
    paper_trial_authorization,
    securities_corporate_action_application,
    securities_execution_lineage,
    securities_execution_policy,
    securities_exit_decision,
    securities_fill_evidence,
    securities_mark_evidence,
    securities_paper_authorization,
    securities_paper_conformance_report,
    securities_paper_opportunity,
    securities_paper_order,
    securities_paper_run,
    securities_position_state,
)
from apex.application.securities_paper import (
    SecuritiesPaperApplicationError,
    SecuritiesPaperIdempotencyConflictError,
)
from apex.domain.campaign import CampaignSnapshot
from apex.domain.ledger import AccountCode, Journal, PostingSide
from apex.domain.securities_paper import (
    AppliedCorporateAction,
    AppliedSecuritiesExit,
    AppliedSecuritiesMark,
    ExternalSecuritiesPaperRun,
    SecuritiesActionApplicationState,
    SecuritiesCorporateActionApplication,
    SecuritiesExecutionLineage,
    SecuritiesExecutionPolicy,
    SecuritiesExecutionSource,
    SecuritiesExitDecision,
    SecuritiesFillEvidence,
    SecuritiesFillState,
    SecuritiesMarkEvidence,
    SecuritiesOrderAction,
    SecuritiesOrderState,
    SecuritiesPaperAuthorization,
    SecuritiesPaperConformanceReport,
    SecuritiesPaperOpportunity,
    SecuritiesPaperOrder,
    SecuritiesPaperRun,
    SecuritiesPaperRunState,
    SecuritiesPositionState,
    SecuritiesPositionStatus,
)
from apex.domain.values import (
    ContentHash,
    Money,
    Quantity,
    canonical_json_bytes,
)


def _database_decimal(value: Decimal | int | str) -> Decimal:
    restored = Decimal(value)
    return Decimal(0) if restored == 0 else restored.normalize()


class PostgresSecuritiesPaperRepository:
    """Retain each bounded Securities financial stage in one transaction."""

    def __init__(self, engine: Engine) -> None:
        self._session_factory = sessionmaker(
            bind=engine,
            expire_on_commit=False,
            future=True,
        )

    @staticmethod
    def _insert_immutable(
        session: Session,
        *,
        table: sa.Table,
        identity_column: sa.Column[object],
        identity: str,
        values: dict[str, object],
        hash_column: sa.Column[object],
        expected_hash: str,
    ) -> bool:
        existing = session.execute(
            select(hash_column).where(identity_column == identity)
        ).scalar_one_or_none()
        if existing is None:
            session.execute(table.insert().values(**values))
            return True
        if str(existing) != expected_hash:
            raise SecuritiesPaperIdempotencyConflictError(
                f"{table.name} identity has conflicting content"
            )
        return False

    @staticmethod
    def _run_from_row(row: sa.RowMapping) -> SecuritiesPaperRun:
        return SecuritiesPaperRun(
            run_id=str(row["run_id"]),
            campaign_id=str(row["campaign_id"]),
            trial_authorization_id=str(row["trial_authorization_id"]),
            strategy_id=str(row["strategy_id"]),
            strategy_version=str(row["strategy_version"]),
            strategy_configuration_hash=ContentHash(str(row["strategy_configuration_hash"])),
            data_batch_id=str(row["data_batch_id"]),
            data_batch_hash=ContentHash(str(row["data_batch_hash"])),
            execution_policy_id=str(row["execution_policy_id"]),
            execution_policy_hash=ContentHash(str(row["execution_policy_hash"])),
            start_date=row["start_date"].isoformat(),
            end_date=row["end_date"].isoformat(),
            state=SecuritiesPaperRunState(str(row["state"])),
            signal_count=int(row["signal_count"]),
            opportunity_count=int(row["opportunity_count"]),
            accepted_opportunity_count=int(row["accepted_opportunity_count"]),
            rejected_opportunity_count=int(row["rejected_opportunity_count"]),
            order_count=int(row["order_count"]),
            fill_count=int(row["fill_count"]),
            position_count=int(row["position_count"]),
            closed_position_count=int(row["closed_position_count"]),
            mark_count=int(row["mark_count"]),
            corporate_action_application_count=int(row["corporate_action_application_count"]),
            starting_cash=Money(_database_decimal(row["starting_cash"])),
            ending_cash=Money(_database_decimal(row["ending_cash"])),
            gross_realized_pnl=_database_decimal(row["gross_realized_pnl"]),
            net_realized_pnl=_database_decimal(row["net_realized_pnl"]),
            maximum_drawdown=_database_decimal(row["maximum_drawdown"]),
            idempotency_key=str(row["idempotency_key"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            failure_code=(str(row["failure_code"]) if row["failure_code"] is not None else None),
            canonical_input_hash=ContentHash(str(row["canonical_input_hash"])),
            canonical_result_hash=(
                ContentHash(str(row["canonical_result_hash"]))
                if row["canonical_result_hash"] is not None
                else None
            ),
        )

    @staticmethod
    def _order_from_row(row: sa.RowMapping) -> SecuritiesPaperOrder:
        return SecuritiesPaperOrder(
            order_id=str(row["order_id"]),
            run_id=str(row["run_id"]),
            opportunity_id=(
                str(row["opportunity_id"]) if row["opportunity_id"] is not None else None
            ),
            authorization_id=str(row["authorization_id"]),
            position_id=(str(row["position_id"]) if row["position_id"] is not None else None),
            instrument_id=str(row["instrument_id"]),
            action=SecuritiesOrderAction(str(row["action"])),
            requested_quantity=Quantity(
                _database_decimal(row["requested_quantity"]),
                _database_decimal(row["quantity_increment"]),
            ),
            filled_quantity=Quantity(
                _database_decimal(row["filled_quantity"]),
                _database_decimal(row["quantity_increment"]),
            ),
            reserved_amount=Money(_database_decimal(row["reserved_amount"])),
            decision_at=row["decision_at"],
            earliest_fill_at=row["earliest_fill_at"],
            expires_at=row["expires_at"],
            state=SecuritiesOrderState(str(row["state"])),
            last_sequence=int(row["last_sequence"]),
            idempotency_key=str(row["idempotency_key"]),
            canonical_hash=ContentHash(str(row["canonical_hash"])),
        )

    @staticmethod
    def _position_from_row(row: sa.RowMapping) -> SecuritiesPositionState:
        return SecuritiesPositionState(
            position_id=str(row["position_id"]),
            run_id=str(row["run_id"]),
            campaign_id=str(row["campaign_id"]),
            instrument_id=str(row["instrument_id"]),
            current_symbol=str(row["current_symbol"]),
            open_quantity=Quantity(
                _database_decimal(row["open_quantity"]),
                _database_decimal(row["quantity_increment"]),
            ),
            average_cost=_database_decimal(row["average_cost"]),
            cumulative_purchase_cost=Money(_database_decimal(row["cumulative_purchase_cost"])),
            cumulative_sale_proceeds=Money(_database_decimal(row["cumulative_sale_proceeds"])),
            realized_pnl=_database_decimal(row["realized_pnl"]),
            unrealized_pnl=_database_decimal(row["unrealized_pnl"]),
            last_mark_price=(
                _database_decimal(row["last_mark_price"])
                if row["last_mark_price"] is not None
                else None
            ),
            last_mark_at=row["last_mark_at"],
            state=SecuritiesPositionStatus(str(row["state"])),
            originating_fill_id=str(row["originating_fill_id"]),
            row_version=int(row["row_version"]),
            canonical_hash=ContentHash(str(row["canonical_hash"])),
        )

    @staticmethod
    def _position_values(position: SecuritiesPositionState) -> dict[str, object]:
        return {
            "position_id": position.position_id,
            "run_id": position.run_id,
            "campaign_id": position.campaign_id,
            "instrument_id": position.instrument_id,
            "current_symbol": position.current_symbol,
            "open_quantity": position.open_quantity.value,
            "quantity_increment": position.open_quantity.increment,
            "average_cost": position.average_cost,
            "cumulative_purchase_cost": position.cumulative_purchase_cost.amount,
            "cumulative_sale_proceeds": position.cumulative_sale_proceeds.amount,
            "realized_pnl": position.realized_pnl,
            "unrealized_pnl": position.unrealized_pnl,
            "last_mark_price": position.last_mark_price,
            "last_mark_at": position.last_mark_at,
            "state": position.state.value,
            "originating_fill_id": position.originating_fill_id,
            "row_version": position.row_version,
            "canonical_hash": position.canonical_hash.value,
        }

    def save_policy(
        self,
        policy: SecuritiesExecutionPolicy,
    ) -> SecuritiesExecutionPolicy:
        values: dict[str, object] = {
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "execution_model": policy.execution_model,
            "permitted_directions": [item.value for item in policy.permitted_directions],
            "permitted_asset_types": [item.value for item in policy.permitted_asset_types],
            "fill_price_rule": policy.fill_price_rule,
            "decision_to_fill_delay_rule": policy.decision_to_fill_delay_rule,
            "half_spread_per_share": policy.half_spread_per_share,
            "slippage_per_share": policy.slippage_per_share,
            "commission_per_share": policy.commission_per_share,
            "commission_rule": policy.commission_rule,
            "volume_participation_cap": policy.volume_participation_cap.value,
            "minimum_quantity": policy.minimum_quantity,
            "maximum_quantity": policy.maximum_quantity,
            "quantity_increment": policy.quantity_increment,
            "unfilled_expiration_sessions": policy.unfilled_expiration_sessions,
            "allow_partial_fills": policy.allow_partial_fills,
            "session_policy": policy.session_policy,
            "corporate_action_policy": policy.corporate_action_policy,
            "canonical_payload": canonical_json_bytes(policy.canonical_payload).decode(),
            "canonical_hash": policy.canonical_hash.value,
            "created_at": policy.created_at,
            "superseded_at": policy.superseded_at,
        }
        with self._session_factory() as session, session.begin():
            self._insert_immutable(
                session,
                table=securities_execution_policy,
                identity_column=securities_execution_policy.c.policy_id,
                identity=policy.policy_id,
                values=values,
                hash_column=securities_execution_policy.c.canonical_hash,
                expected_hash=policy.canonical_hash.value,
            )
        return policy

    @staticmethod
    def _run_values(run: SecuritiesPaperRun) -> dict[str, object]:
        return {
            "run_id": run.run_id,
            "campaign_id": run.campaign_id,
            "trial_authorization_id": run.trial_authorization_id,
            "strategy_id": run.strategy_id,
            "strategy_version": run.strategy_version,
            "strategy_configuration_hash": run.strategy_configuration_hash.value,
            "data_batch_id": run.data_batch_id,
            "data_batch_hash": run.data_batch_hash.value,
            "execution_policy_id": run.execution_policy_id,
            "execution_policy_hash": run.execution_policy_hash.value,
            "start_date": date.fromisoformat(run.start_date),
            "end_date": date.fromisoformat(run.end_date),
            "state": run.state.value,
            "signal_count": run.signal_count,
            "opportunity_count": run.opportunity_count,
            "accepted_opportunity_count": run.accepted_opportunity_count,
            "rejected_opportunity_count": run.rejected_opportunity_count,
            "order_count": run.order_count,
            "fill_count": run.fill_count,
            "position_count": run.position_count,
            "closed_position_count": run.closed_position_count,
            "mark_count": run.mark_count,
            "corporate_action_application_count": (run.corporate_action_application_count),
            "starting_cash": run.starting_cash.amount,
            "ending_cash": run.ending_cash.amount,
            "gross_realized_pnl": run.gross_realized_pnl,
            "net_realized_pnl": run.net_realized_pnl,
            "maximum_drawdown": run.maximum_drawdown,
            "idempotency_key": run.idempotency_key,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "failure_code": run.failure_code,
            "canonical_input_hash": run.canonical_input_hash.value,
            "canonical_result_hash": (
                run.canonical_result_hash.value if run.canonical_result_hash is not None else None
            ),
        }

    def save_run(self, run: SecuritiesPaperRun) -> SecuritiesPaperRun:
        values = self._run_values(run)
        try:
            with self._session_factory() as session, session.begin():
                existing = (
                    session.execute(
                        select(securities_paper_run)
                        .where(
                            securities_paper_run.c.campaign_id == run.campaign_id,
                            securities_paper_run.c.idempotency_key == run.idempotency_key,
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
                    session.execute(securities_paper_run.insert().values(**values))
                    return run
                if str(existing["canonical_input_hash"]) != run.canonical_input_hash.value:
                    raise SecuritiesPaperIdempotencyConflictError(
                        "Securities PAPER run idempotency conflict"
                    )
                current = self._run_from_row(existing)
                incoming_counts = (
                    run.opportunity_count,
                    run.accepted_opportunity_count,
                    run.rejected_opportunity_count,
                    run.order_count,
                    run.fill_count,
                    run.position_count,
                    run.closed_position_count,
                    run.mark_count,
                    run.corporate_action_application_count,
                )
                current_counts = (
                    current.opportunity_count,
                    current.accepted_opportunity_count,
                    current.rejected_opportunity_count,
                    current.order_count,
                    current.fill_count,
                    current.position_count,
                    current.closed_position_count,
                    current.mark_count,
                    current.corporate_action_application_count,
                )
                if any(
                    incoming < retained
                    for incoming, retained in zip(
                        incoming_counts,
                        current_counts,
                        strict=True,
                    )
                ):
                    return current
                session.execute(
                    securities_paper_run.update()
                    .where(securities_paper_run.c.run_id == current.run_id)
                    .values(**values)
                )
                return run
        except IntegrityError as error:
            raise SecuritiesPaperIdempotencyConflictError(
                "Securities PAPER run conflicts with durable evidence"
            ) from error

    def run(self, run_id: str) -> SecuritiesPaperRun:
        with self._session_factory() as session:
            row = (
                session.execute(
                    select(securities_paper_run).where(securities_paper_run.c.run_id == run_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecuritiesPaperApplicationError("Securities PAPER run was not found")
        return self._run_from_row(row)

    def save_opportunity(
        self,
        opportunity: SecuritiesPaperOpportunity,
    ) -> SecuritiesPaperOpportunity:
        values: dict[str, object] = {
            "opportunity_id": opportunity.opportunity_id,
            "run_id": opportunity.run_id,
            "signal_id": opportunity.signal_id,
            "instrument_id": opportunity.instrument_id,
            "decision_at": opportunity.decision_at,
            "earliest_fill_at": opportunity.earliest_fill_at,
            "reference_price": opportunity.reference_price,
            "requested_quantity": opportunity.requested_quantity.value,
            "quantity_increment": opportunity.requested_quantity.increment,
            "intended_exposure": opportunity.intended_exposure.amount,
            "modeled_spread": opportunity.modeled_spread.amount,
            "modeled_slippage": opportunity.modeled_slippage.amount,
            "modeled_commission": opportunity.modeled_commission.amount,
            "maximum_loss": opportunity.maximum_loss.amount,
            "holding_horizon_sessions": opportunity.holding_horizon_sessions,
            "evidence_bar_ids": list(opportunity.evidence_bar_ids),
            "configuration_hash": opportunity.configuration_hash.value,
            "canonical_hash": opportunity.canonical_hash.value,
        }
        with self._session_factory() as session, session.begin():
            inserted = self._insert_immutable(
                session,
                table=securities_paper_opportunity,
                identity_column=securities_paper_opportunity.c.opportunity_id,
                identity=opportunity.opportunity_id,
                values=values,
                hash_column=securities_paper_opportunity.c.canonical_hash,
                expected_hash=opportunity.canonical_hash.value,
            )
            if inserted:
                session.execute(
                    securities_paper_run.update()
                    .where(securities_paper_run.c.run_id == opportunity.run_id)
                    .values(opportunity_count=(securities_paper_run.c.opportunity_count + 1))
                )
        return opportunity

    def save_authorization(
        self,
        authorization: SecuritiesPaperAuthorization,
    ) -> SecuritiesPaperAuthorization:
        values: dict[str, object] = {
            "authorization_id": authorization.authorization_id,
            "opportunity_id": authorization.opportunity_id,
            "allocation_id": authorization.allocation_id,
            "risk_decision_id": authorization.risk_decision_id,
            "reservation_id": authorization.reservation_id,
            "accepted": authorization.accepted,
            "quantity": authorization.quantity.value,
            "quantity_increment": authorization.quantity.increment,
            "maximum_loss": authorization.maximum_loss.amount,
            "reason_codes": list(authorization.reason_codes),
            "canonical_hash": authorization.canonical_hash.value,
        }
        with self._session_factory() as session, session.begin():
            inserted = self._insert_immutable(
                session,
                table=securities_paper_authorization,
                identity_column=securities_paper_authorization.c.authorization_id,
                identity=authorization.authorization_id,
                values=values,
                hash_column=securities_paper_authorization.c.canonical_hash,
                expected_hash=authorization.canonical_hash.value,
            )
            if inserted and not authorization.accepted:
                run_id = session.scalar(
                    select(securities_paper_opportunity.c.run_id).where(
                        securities_paper_opportunity.c.opportunity_id
                        == authorization.opportunity_id
                    )
                )
                if run_id is None:
                    raise SecuritiesPaperApplicationError("Securities opportunity was not found")
                session.execute(
                    securities_paper_run.update()
                    .where(securities_paper_run.c.run_id == run_id)
                    .values(
                        rejected_opportunity_count=(
                            securities_paper_run.c.rejected_opportunity_count + 1
                        )
                    )
                )
        return authorization

    @staticmethod
    def _order_values(order: SecuritiesPaperOrder) -> dict[str, object]:
        return {
            "order_id": order.order_id,
            "run_id": order.run_id,
            "opportunity_id": order.opportunity_id,
            "authorization_id": order.authorization_id,
            "position_id": order.position_id,
            "instrument_id": order.instrument_id,
            "action": order.action.value,
            "requested_quantity": order.requested_quantity.value,
            "quantity_increment": order.requested_quantity.increment,
            "filled_quantity": order.filled_quantity.value,
            "reserved_amount": order.reserved_amount.amount,
            "decision_at": order.decision_at,
            "earliest_fill_at": order.earliest_fill_at,
            "expires_at": order.expires_at,
            "state": order.state.value,
            "last_sequence": order.last_sequence,
            "idempotency_key": order.idempotency_key,
            "canonical_hash": order.canonical_hash.value,
        }

    @staticmethod
    def _fill_values(fill: SecuritiesFillEvidence) -> dict[str, object]:
        return {
            "fill_id": fill.fill_id,
            "run_id": fill.run_id,
            "order_id": fill.order_id,
            "position_id": fill.position_id,
            "instrument_id": fill.instrument_id,
            "action": fill.action.value,
            "quantity": fill.quantity.value,
            "quantity_increment": fill.quantity.increment,
            "fill_price": fill.fill_price,
            "notional": fill.notional.amount,
            "modeled_spread": fill.modeled_spread.amount,
            "modeled_slippage": fill.modeled_slippage.amount,
            "modeled_commission": fill.modeled_commission.amount,
            "decision_at": fill.decision_at,
            "eligible_fill_at": fill.eligible_fill_at,
            "occurred_at": fill.occurred_at,
            "bar_id": fill.bar_id,
            "execution_policy_hash": fill.execution_policy_hash.value,
            "state": fill.state.value,
            "reason_codes": list(fill.reason_codes),
            "canonical_hash": fill.canonical_hash.value,
            "increment_ordinal": fill.increment_ordinal,
            "cumulative_quantity": (fill.cumulative_quantity or fill.quantity).value,
            "remaining_quantity": (
                fill.remaining_quantity or Quantity(0, fill.quantity.increment)
            ).value,
            "execution_source": fill.execution_source.value,
            "source_evidence_id": fill.source_evidence_id or fill.fill_id,
            "economic_identity_hash": (fill.economic_identity_hash or fill.canonical_hash).value,
        }

    @staticmethod
    def _fill_from_row(row: sa.RowMapping) -> SecuritiesFillEvidence:
        return SecuritiesFillEvidence(
            fill_id=str(row["fill_id"]),
            run_id=str(row["run_id"]),
            order_id=str(row["order_id"]),
            position_id=str(row["position_id"]) if row["position_id"] is not None else None,
            instrument_id=str(row["instrument_id"]),
            action=SecuritiesOrderAction(str(row["action"])),
            quantity=Quantity(
                _database_decimal(row["quantity"]), _database_decimal(row["quantity_increment"])
            ),
            fill_price=_database_decimal(row["fill_price"])
            if row["fill_price"] is not None
            else None,
            notional=Money(_database_decimal(row["notional"])),
            modeled_spread=Money(_database_decimal(row["modeled_spread"])),
            modeled_slippage=Money(_database_decimal(row["modeled_slippage"])),
            modeled_commission=Money(_database_decimal(row["modeled_commission"])),
            decision_at=row["decision_at"],
            eligible_fill_at=row["eligible_fill_at"],
            occurred_at=row["occurred_at"],
            bar_id=str(row["bar_id"]),
            execution_policy_hash=ContentHash(str(row["execution_policy_hash"])),
            state=SecuritiesFillState(str(row["state"])),
            reason_codes=tuple(str(value) for value in row["reason_codes"]),
            canonical_hash=ContentHash(str(row["canonical_hash"])),
            increment_ordinal=int(row["increment_ordinal"]),
            cumulative_quantity=Quantity(
                _database_decimal(row["cumulative_quantity"]),
                _database_decimal(row["quantity_increment"]),
            ),
            remaining_quantity=Quantity(
                _database_decimal(row["remaining_quantity"]),
                _database_decimal(row["quantity_increment"]),
            ),
            execution_source=SecuritiesExecutionSource(str(row["execution_source"])),
            source_evidence_id=str(row["source_evidence_id"]),
            economic_identity_hash=ContentHash(str(row["economic_identity_hash"])),
        )

    def save_execution(
        self,
        *,
        order: SecuritiesPaperOrder,
        fill: SecuritiesFillEvidence,
        position: SecuritiesPositionState | None,
        lineage: SecuritiesExecutionLineage,
        journals: tuple[Journal, ...],
    ) -> None:
        try:
            with self._session_factory() as session, session.begin():
                existing_order_hash = session.scalar(
                    select(securities_paper_order.c.canonical_hash).where(
                        securities_paper_order.c.order_id == order.order_id
                    )
                )
                if existing_order_hash is not None:
                    if str(existing_order_hash) != order.canonical_hash.value:
                        raise SecuritiesPaperIdempotencyConflictError(
                            "Securities Order identity has conflicting content"
                        )
                    return
                inserted = self._insert_immutable(
                    session,
                    table=securities_paper_order,
                    identity_column=securities_paper_order.c.order_id,
                    identity=order.order_id,
                    values=self._order_values(order),
                    hash_column=securities_paper_order.c.canonical_hash,
                    expected_hash=order.canonical_hash.value,
                )
                self._insert_immutable(
                    session,
                    table=securities_fill_evidence,
                    identity_column=securities_fill_evidence.c.fill_id,
                    identity=fill.fill_id,
                    values=self._fill_values(fill),
                    hash_column=securities_fill_evidence.c.canonical_hash,
                    expected_hash=fill.canonical_hash.value,
                )
                if position is not None:
                    self._insert_immutable(
                        session,
                        table=securities_position_state,
                        identity_column=securities_position_state.c.position_id,
                        identity=position.position_id,
                        values=self._position_values(position),
                        hash_column=securities_position_state.c.canonical_hash,
                        expected_hash=position.canonical_hash.value,
                    )
                lineage_values: dict[str, object] = {
                    "lineage_id": lineage.lineage_id,
                    "run_id": lineage.run_id,
                    "signal_id": lineage.signal_id,
                    "opportunity_id": lineage.opportunity_id,
                    "strategy_authority_decision_id": (lineage.strategy_authority_decision_id),
                    "trial_authorization_id": lineage.trial_authorization_id,
                    "allocation_id": lineage.allocation_id,
                    "risk_decision_id": lineage.risk_decision_id,
                    "reservation_id": lineage.reservation_id,
                    "order_id": lineage.order_id,
                    "fill_id": lineage.fill_id,
                    "position_id": lineage.position_id,
                    "execution_policy_hash": lineage.execution_policy_hash.value,
                    "data_evidence_hash": lineage.data_evidence_hash.value,
                    "external_run_id": lineage.external_run_id,
                    "canonical_hash": lineage.canonical_hash.value,
                }
                self._insert_immutable(
                    session,
                    table=securities_execution_lineage,
                    identity_column=securities_execution_lineage.c.lineage_id,
                    identity=lineage.lineage_id,
                    values=lineage_values,
                    hash_column=securities_execution_lineage.c.canonical_hash,
                    expected_hash=lineage.canonical_hash.value,
                )
                if inserted:
                    ledger = PostgresLedgerRepository(session)
                    for journal in journals:
                        ledger.post_securities_journal(position.campaign_id, journal) if (
                            position is not None
                        ) else None
                    session.execute(
                        securities_paper_run.update()
                        .where(securities_paper_run.c.run_id == order.run_id)
                        .values(
                            accepted_opportunity_count=(
                                securities_paper_run.c.accepted_opportunity_count + 1
                            ),
                            order_count=securities_paper_run.c.order_count + 1,
                            fill_count=securities_paper_run.c.fill_count
                            + int(fill.quantity.value > 0),
                            position_count=securities_paper_run.c.position_count
                            + int(position is not None),
                        )
                    )
        except IntegrityError as error:
            raise SecuritiesPaperIdempotencyConflictError(
                "Securities execution conflicts with durable evidence"
            ) from error

    def save_pending_order(self, order: SecuritiesPaperOrder) -> SecuritiesPaperOrder:
        if order.state is not SecuritiesOrderState.READY or order.filled_quantity.value != 0:
            raise SecuritiesPaperApplicationError("pending Securities Order must be unfilled READY")
        try:
            with self._session_factory() as session, session.begin():
                existing = session.scalar(
                    select(securities_paper_order.c.canonical_hash).where(
                        securities_paper_order.c.order_id == order.order_id
                    )
                )
                if existing is not None:
                    if str(existing) != order.canonical_hash.value:
                        raise SecuritiesPaperIdempotencyConflictError(
                            "Securities pending Order identity conflict"
                        )
                    return order
                self._insert_immutable(
                    session,
                    table=securities_paper_order,
                    identity_column=securities_paper_order.c.order_id,
                    identity=order.order_id,
                    values=self._order_values(order),
                    hash_column=securities_paper_order.c.canonical_hash,
                    expected_hash=order.canonical_hash.value,
                )
                session.execute(
                    securities_paper_run.update()
                    .where(securities_paper_run.c.run_id == order.run_id)
                    .values(
                        accepted_opportunity_count=(
                            securities_paper_run.c.accepted_opportunity_count + 1
                        ),
                        order_count=securities_paper_run.c.order_count + 1,
                    )
                )
        except IntegrityError as error:
            raise SecuritiesPaperIdempotencyConflictError(
                "Securities pending Order conflicts with durable evidence"
            ) from error
        return order

    def cancel_open_order(self, order_id: str) -> SecuritiesPaperOrder:
        with self._session_factory() as session, session.begin():
            row = (
                session.execute(
                    select(securities_paper_order)
                    .where(securities_paper_order.c.order_id == order_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise SecuritiesPaperApplicationError("Securities PAPER Order was not found")
            order = self._order_from_row(row)
            if order.state is SecuritiesOrderState.CANCELED:
                return order
            if order.state not in {
                SecuritiesOrderState.READY,
                SecuritiesOrderState.PARTIALLY_FILLED,
            }:
                raise SecuritiesPaperApplicationError("Securities PAPER Order cannot be canceled")
            canceled = replace(
                order,
                reserved_amount=Money(0),
                state=SecuritiesOrderState.CANCELED,
                last_sequence=order.last_sequence + 1,
            )
            session.execute(
                securities_paper_order.update()
                .where(securities_paper_order.c.order_id == order_id)
                .values(**self._order_values(canceled))
            )
        return canceled

    def fills_for_order(self, order_id: str) -> tuple[SecuritiesFillEvidence, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                select(securities_fill_evidence)
                .where(securities_fill_evidence.c.order_id == order_id)
                .order_by(securities_fill_evidence.c.increment_ordinal)
            ).mappings()
            return tuple(self._fill_from_row(row) for row in rows)

    def order(self, order_id: str) -> SecuritiesPaperOrder:
        with self._session_factory() as session:
            row = (
                session.execute(
                    select(securities_paper_order).where(
                        securities_paper_order.c.order_id == order_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecuritiesPaperApplicationError("Securities PAPER Order was not found")
        return self._order_from_row(row)

    def fill_by_source(
        self,
        *,
        execution_source: SecuritiesExecutionSource,
        source_evidence_id: str,
    ) -> SecuritiesFillEvidence | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    select(securities_fill_evidence).where(
                        securities_fill_evidence.c.execution_source == execution_source.value,
                        securities_fill_evidence.c.source_evidence_id == source_evidence_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else self._fill_from_row(row)

    def fill_by_economic_identity(
        self, economic_identity_hash: ContentHash
    ) -> SecuritiesFillEvidence | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    select(securities_fill_evidence).where(
                        securities_fill_evidence.c.economic_identity_hash
                        == economic_identity_hash.value
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else self._fill_from_row(row)

    def append_fill_increment(
        self,
        *,
        order: SecuritiesPaperOrder,
        fill: SecuritiesFillEvidence,
        position: SecuritiesPositionState | None,
        journals: tuple[Journal, ...],
    ) -> None:
        try:
            with self._session_factory() as session, session.begin():
                current = (
                    session.execute(
                        select(securities_paper_order)
                        .where(securities_paper_order.c.order_id == order.order_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if current is None:
                    raise SecuritiesPaperApplicationError("Securities PAPER Order was not found")
                history = (
                    session.execute(
                        select(securities_fill_evidence)
                        .where(securities_fill_evidence.c.order_id == order.order_id)
                        .order_by(securities_fill_evidence.c.increment_ordinal)
                        .with_for_update()
                    )
                    .mappings()
                    .all()
                )
                existing = next((row for row in history if row["fill_id"] == fill.fill_id), None)
                if existing is not None:
                    if str(existing["canonical_hash"]) != fill.canonical_hash.value:
                        raise SecuritiesPaperIdempotencyConflictError(
                            "Securities fill identity conflict"
                        )
                    return
                prior_quantity = sum(
                    (_database_decimal(row["quantity"]) for row in history), Decimal()
                )
                if (
                    fill.increment_ordinal != len(history) + 1
                    or fill.cumulative_quantity is None
                    or fill.cumulative_quantity.value != prior_quantity + fill.quantity.value
                    or fill.cumulative_quantity.value
                    > _database_decimal(current["requested_quantity"])
                ):
                    raise SecuritiesPaperApplicationError("invalid Securities fill increment")
                duplicate = session.scalar(
                    select(securities_fill_evidence.c.fill_id).where(
                        securities_fill_evidence.c.economic_identity_hash
                        == (fill.economic_identity_hash or fill.canonical_hash).value
                    )
                )
                if duplicate is not None:
                    raise SecuritiesPaperIdempotencyConflictError(
                        "duplicate Securities economic fill"
                    )
                self._insert_immutable(
                    session,
                    table=securities_fill_evidence,
                    identity_column=securities_fill_evidence.c.fill_id,
                    identity=fill.fill_id,
                    values=self._fill_values(fill),
                    hash_column=securities_fill_evidence.c.canonical_hash,
                    expected_hash=fill.canonical_hash.value,
                )
                session.execute(
                    securities_paper_order.update()
                    .where(securities_paper_order.c.order_id == order.order_id)
                    .values(**self._order_values(order))
                )
                if position is not None:
                    previous = (
                        session.execute(
                            select(securities_position_state)
                            .where(securities_position_state.c.position_id == position.position_id)
                            .with_for_update()
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if previous is None:
                        self._insert_immutable(
                            session,
                            table=securities_position_state,
                            identity_column=securities_position_state.c.position_id,
                            identity=position.position_id,
                            values=self._position_values(position),
                            hash_column=securities_position_state.c.canonical_hash,
                            expected_hash=position.canonical_hash.value,
                        )
                    else:
                        if int(previous["row_version"]) + 1 != position.row_version:
                            raise SecuritiesPaperApplicationError(
                                "stale Securities Position increment"
                            )
                        session.execute(
                            securities_position_state.update()
                            .where(securities_position_state.c.position_id == position.position_id)
                            .values(**self._position_values(position))
                        )
                if position is not None:
                    ledger = PostgresLedgerRepository(session)
                    for journal in journals:
                        ledger.post_securities_journal(position.campaign_id, journal)
        except IntegrityError as error:
            raise SecuritiesPaperIdempotencyConflictError(
                "Securities fill increment conflicts with durable evidence"
            ) from error

    def save_mark(self, applied: AppliedSecuritiesMark) -> None:
        mark = applied.mark
        with self._session_factory() as session, session.begin():
            current = (
                session.execute(
                    select(securities_position_state)
                    .where(securities_position_state.c.position_id == applied.position.position_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            inserted = self._insert_immutable(
                session,
                table=securities_mark_evidence,
                identity_column=securities_mark_evidence.c.mark_id,
                identity=mark.mark_id,
                values={
                    "mark_id": mark.mark_id,
                    "run_id": mark.run_id,
                    "position_id": mark.position_id,
                    "instrument_id": mark.instrument_id,
                    "source_bar_id": mark.source_bar_id,
                    "marked_at": mark.marked_at,
                    "mark_price": mark.mark_price,
                    "quantity": mark.quantity.value,
                    "quantity_increment": mark.quantity.increment,
                    "market_value": mark.market_value.amount,
                    "unrealized_pnl": mark.unrealized_pnl,
                    "execution_policy_hash": mark.execution_policy_hash.value,
                    "source_lineage_hash": mark.source_lineage_hash.value,
                    "canonical_hash": mark.canonical_hash.value,
                },
                hash_column=securities_mark_evidence.c.canonical_hash,
                expected_hash=mark.canonical_hash.value,
            )
            if inserted:
                if int(current["row_version"]) + 1 != applied.position.row_version:
                    raise SecuritiesPaperApplicationError("stale Securities Position mark")
                session.execute(
                    securities_position_state.update()
                    .where(securities_position_state.c.position_id == applied.position.position_id)
                    .values(**self._position_values(applied.position))
                )
                session.execute(
                    securities_paper_run.update()
                    .where(securities_paper_run.c.run_id == mark.run_id)
                    .values(mark_count=securities_paper_run.c.mark_count + 1)
                )

    def mark(
        self,
        *,
        position_id: str,
        source_bar_id: str,
    ) -> SecuritiesMarkEvidence | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    select(securities_mark_evidence).where(
                        securities_mark_evidence.c.position_id == position_id,
                        securities_mark_evidence.c.source_bar_id == source_bar_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return SecuritiesMarkEvidence(
            mark_id=str(row["mark_id"]),
            run_id=str(row["run_id"]),
            position_id=str(row["position_id"]),
            instrument_id=str(row["instrument_id"]),
            source_bar_id=str(row["source_bar_id"]),
            marked_at=row["marked_at"],
            mark_price=_database_decimal(row["mark_price"]),
            quantity=Quantity(
                _database_decimal(row["quantity"]),
                _database_decimal(row["quantity_increment"]),
            ),
            market_value=Money(_database_decimal(row["market_value"])),
            unrealized_pnl=_database_decimal(row["unrealized_pnl"]),
            execution_policy_hash=ContentHash(str(row["execution_policy_hash"])),
            source_lineage_hash=ContentHash(str(row["source_lineage_hash"])),
            canonical_hash=ContentHash(str(row["canonical_hash"])),
        )

    def save_action(
        self,
        applied: AppliedCorporateAction,
        *,
        journal: Journal | None,
    ) -> None:
        item = applied.application
        with self._session_factory() as session, session.begin():
            current = (
                session.execute(
                    select(securities_position_state)
                    .where(securities_position_state.c.position_id == applied.position.position_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if journal is not None:
                PostgresLedgerRepository(session).post_securities_journal(
                    str(current["campaign_id"]),
                    journal,
                )
            inserted = self._insert_immutable(
                session,
                table=securities_corporate_action_application,
                identity_column=(securities_corporate_action_application.c.application_id),
                identity=item.application_id,
                values={
                    "application_id": item.application_id,
                    "action_batch_id": item.action_batch_id,
                    "action_id": item.action_id,
                    "position_id": item.position_id,
                    "applied_at": item.applied_at,
                    "prior_quantity": item.prior_quantity.value,
                    "resulting_quantity": item.resulting_quantity.value,
                    "quantity_increment": item.resulting_quantity.increment,
                    "prior_cost_basis": item.prior_cost_basis.amount,
                    "resulting_cost_basis": item.resulting_cost_basis.amount,
                    "cash_entitlement": item.cash_entitlement.amount,
                    "ledger_journal_id": item.ledger_journal_id,
                    "state": item.state.value,
                    "reason_codes": list(item.reason_codes),
                    "canonical_hash": item.canonical_hash.value,
                },
                hash_column=securities_corporate_action_application.c.canonical_hash,
                expected_hash=item.canonical_hash.value,
            )
            if inserted:
                if int(current["row_version"]) + 1 != applied.position.row_version:
                    raise SecuritiesPaperApplicationError(
                        "stale Securities Position corporate action"
                    )
                session.execute(
                    securities_position_state.update()
                    .where(securities_position_state.c.position_id == applied.position.position_id)
                    .values(**self._position_values(applied.position))
                )
                session.execute(
                    securities_paper_run.update()
                    .where(securities_paper_run.c.run_id == applied.position.run_id)
                    .values(
                        corporate_action_application_count=(
                            securities_paper_run.c.corporate_action_application_count + 1
                        )
                    )
                )

    def action_application(
        self,
        *,
        position_id: str,
        action_id: str,
    ) -> SecuritiesCorporateActionApplication | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    select(securities_corporate_action_application).where(
                        securities_corporate_action_application.c.position_id == position_id,
                        securities_corporate_action_application.c.action_id == action_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return SecuritiesCorporateActionApplication(
            application_id=str(row["application_id"]),
            action_batch_id=str(row["action_batch_id"]),
            action_id=str(row["action_id"]),
            position_id=str(row["position_id"]),
            applied_at=row["applied_at"],
            prior_quantity=Quantity(
                _database_decimal(row["prior_quantity"]),
                _database_decimal(row["quantity_increment"]),
            ),
            resulting_quantity=Quantity(
                _database_decimal(row["resulting_quantity"]),
                _database_decimal(row["quantity_increment"]),
            ),
            prior_cost_basis=Money(_database_decimal(row["prior_cost_basis"])),
            resulting_cost_basis=Money(_database_decimal(row["resulting_cost_basis"])),
            cash_entitlement=Money(_database_decimal(row["cash_entitlement"])),
            ledger_journal_id=(
                str(row["ledger_journal_id"]) if row["ledger_journal_id"] is not None else None
            ),
            state=SecuritiesActionApplicationState(str(row["state"])),
            reason_codes=tuple(str(item) for item in row["reason_codes"]),
            canonical_hash=ContentHash(str(row["canonical_hash"])),
        )

    def save_exit(
        self,
        *,
        decision: SecuritiesExitDecision,
        order: SecuritiesPaperOrder,
        fill: SecuritiesFillEvidence,
        applied: AppliedSecuritiesExit,
        journals: tuple[Journal, ...],
    ) -> None:
        with self._session_factory() as session, session.begin():
            current = (
                session.execute(
                    select(securities_position_state)
                    .where(securities_position_state.c.position_id == applied.position.position_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            decision_values: dict[str, object] = {
                "exit_decision_id": decision.exit_decision_id,
                "run_id": decision.run_id,
                "position_id": decision.position_id,
                "instrument_id": decision.instrument_id,
                "reason": decision.reason.value,
                "decision_at": decision.decision_at,
                "earliest_fill_at": decision.earliest_fill_at,
                "source_identity": decision.source_identity,
                "canonical_input_hash": decision.canonical_input_hash.value,
                "canonical_result_hash": decision.canonical_result_hash.value,
            }
            inserted = self._insert_immutable(
                session,
                table=securities_exit_decision,
                identity_column=securities_exit_decision.c.exit_decision_id,
                identity=decision.exit_decision_id,
                values=decision_values,
                hash_column=securities_exit_decision.c.canonical_result_hash,
                expected_hash=decision.canonical_result_hash.value,
            )
            self._insert_immutable(
                session,
                table=securities_paper_order,
                identity_column=securities_paper_order.c.order_id,
                identity=order.order_id,
                values=self._order_values(order),
                hash_column=securities_paper_order.c.canonical_hash,
                expected_hash=order.canonical_hash.value,
            )
            self._insert_immutable(
                session,
                table=securities_fill_evidence,
                identity_column=securities_fill_evidence.c.fill_id,
                identity=fill.fill_id,
                values=self._fill_values(fill),
                hash_column=securities_fill_evidence.c.canonical_hash,
                expected_hash=fill.canonical_hash.value,
            )
            if inserted:
                if int(current["row_version"]) + 1 != applied.position.row_version:
                    raise SecuritiesPaperApplicationError("stale Securities Position exit")
                ledger = PostgresLedgerRepository(session)
                for journal in journals:
                    ledger.post_securities_journal(
                        applied.position.campaign_id,
                        journal,
                    )
                session.execute(
                    securities_position_state.update()
                    .where(securities_position_state.c.position_id == applied.position.position_id)
                    .values(**self._position_values(applied.position))
                )
                session.execute(
                    securities_paper_run.update()
                    .where(securities_paper_run.c.run_id == order.run_id)
                    .values(
                        order_count=securities_paper_run.c.order_count + 1,
                        fill_count=securities_paper_run.c.fill_count + 1,
                        closed_position_count=(
                            securities_paper_run.c.closed_position_count
                            + int(applied.position.state is SecuritiesPositionStatus.CLOSED)
                        ),
                    )
                )

    def save_conformance(
        self,
        report: SecuritiesPaperConformanceReport,
        external: ExternalSecuritiesPaperRun,
    ) -> SecuritiesPaperConformanceReport:
        with self._session_factory() as session, session.begin():
            self._insert_immutable(
                session,
                table=external_securities_paper_run,
                identity_column=external_securities_paper_run.c.external_run_id,
                identity=external.external_run_id,
                values={
                    "external_run_id": external.external_run_id,
                    "engine_identity": external.engine_identity,
                    "engine_version": external.engine_version,
                    "state": external.state,
                    "apex_run_id": external.apex_run_id,
                    "request_hash": external.request_hash.value,
                    "result_hash": (
                        external.result_hash.value if external.result_hash is not None else None
                    ),
                    "order_count": external.order_count,
                    "fill_count": external.fill_count,
                    "position_count": external.position_count,
                    "ending_cash": (
                        external.ending_cash.amount if external.ending_cash is not None else None
                    ),
                    "realized_pnl": external.realized_pnl,
                    "maximum_drawdown": external.maximum_drawdown,
                    "canonical_events": canonical_json_bytes(external.canonical_events).decode(),
                    "warnings": list(external.warnings),
                },
                hash_column=external_securities_paper_run.c.request_hash,
                expected_hash=external.request_hash.value,
            )
            self._insert_immutable(
                session,
                table=securities_paper_conformance_report,
                identity_column=securities_paper_conformance_report.c.report_id,
                identity=report.report_id,
                values={
                    "report_id": report.report_id,
                    "paper_run_id": report.paper_run_id,
                    "external_run_id": report.external_run_id,
                    "classification": report.classification.value,
                    "matching_event_count": report.matching_event_count,
                    "differing_event_count": report.differing_event_count,
                    "cash_difference": report.cash_difference,
                    "realized_pnl_difference": report.realized_pnl_difference,
                    "reason_codes": list(report.reason_codes),
                    "canonical_input_hash": report.canonical_input_hash.value,
                    "canonical_result_hash": report.canonical_result_hash.value,
                },
                hash_column=(securities_paper_conformance_report.c.canonical_result_hash),
                expected_hash=report.canonical_result_hash.value,
            )
        return report

    def positions(self, campaign_id: str) -> tuple[SecuritiesPositionState, ...]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(securities_position_state)
                    .where(securities_position_state.c.campaign_id == campaign_id)
                    .order_by(securities_position_state.c.position_id)
                )
                .mappings()
                .all()
            )
        return tuple(self._position_from_row(row) for row in rows)

    def position(self, position_id: str) -> SecuritiesPositionState:
        with self._session_factory() as session:
            row = (
                session.execute(
                    select(securities_position_state).where(
                        securities_position_state.c.position_id == position_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecuritiesPaperApplicationError("Securities Position was not found")
        return self._position_from_row(row)

    def has_order(self, order_id: str) -> bool:
        with self._session_factory() as session:
            return (
                session.scalar(
                    select(securities_paper_order.c.order_id).where(
                        securities_paper_order.c.order_id == order_id
                    )
                )
                is not None
            )

    def status(self, campaign_id: str) -> dict[str, object]:
        with self._session_factory() as session:
            run_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(securities_paper_run)
                    .where(securities_paper_run.c.campaign_id == campaign_id)
                )
                or 0
            )
            completed_run_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(securities_paper_run)
                    .where(
                        securities_paper_run.c.campaign_id == campaign_id,
                        securities_paper_run.c.state.in_(
                            ("COMPLETED", "PARTIAL", "FAILED", "CANCELED")
                        ),
                    )
                )
                or 0
            )
            accepted_opportunity_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(
                        securities_paper_authorization.join(
                            securities_paper_opportunity,
                            securities_paper_authorization.c.opportunity_id
                            == securities_paper_opportunity.c.opportunity_id,
                        ).join(
                            securities_paper_run,
                            securities_paper_opportunity.c.run_id == securities_paper_run.c.run_id,
                        )
                    )
                    .where(
                        securities_paper_run.c.campaign_id == campaign_id,
                        securities_paper_authorization.c.accepted.is_(True),
                    )
                )
                or 0
            )
            cumulative_authorized_risk = _database_decimal(
                session.scalar(
                    select(
                        func.coalesce(
                            func.sum(securities_paper_authorization.c.maximum_loss),
                            0,
                        )
                    )
                    .select_from(
                        securities_paper_authorization.join(
                            securities_paper_opportunity,
                            securities_paper_authorization.c.opportunity_id
                            == securities_paper_opportunity.c.opportunity_id,
                        ).join(
                            securities_paper_run,
                            securities_paper_opportunity.c.run_id == securities_paper_run.c.run_id,
                        )
                    )
                    .where(
                        securities_paper_run.c.campaign_id == campaign_id,
                        securities_paper_authorization.c.accepted.is_(True),
                    )
                )
                or 0
            )
            order_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(
                        securities_paper_order.join(
                            securities_paper_run,
                            securities_paper_order.c.run_id == securities_paper_run.c.run_id,
                        )
                    )
                    .where(securities_paper_run.c.campaign_id == campaign_id)
                )
                or 0
            )
            fill_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(
                        securities_fill_evidence.join(
                            securities_paper_run,
                            securities_fill_evidence.c.run_id == securities_paper_run.c.run_id,
                        )
                    )
                    .where(securities_paper_run.c.campaign_id == campaign_id)
                )
                or 0
            )
            open_positions = int(
                session.scalar(
                    select(func.count())
                    .select_from(securities_position_state)
                    .where(
                        securities_position_state.c.campaign_id == campaign_id,
                        securities_position_state.c.open_quantity > 0,
                    )
                )
                or 0
            )
            closed_positions = int(
                session.scalar(
                    select(func.count())
                    .select_from(securities_position_state)
                    .where(
                        securities_position_state.c.campaign_id == campaign_id,
                        securities_position_state.c.open_quantity == 0,
                    )
                )
                or 0
            )
            balances = PostgresLedgerRepository(session).balances(campaign_id)
            signed = sa.case(
                (
                    ledger_posting.c.side == PostingSide.DEBIT.value,
                    ledger_posting.c.amount,
                ),
                else_=-ledger_posting.c.amount,
            )
            account_rows = session.execute(
                select(
                    ledger_account.c.account_code,
                    func.coalesce(func.sum(signed), 0),
                )
                .select_from(
                    ledger_posting.join(ledger_journal).join(
                        ledger_account,
                        ledger_posting.c.account_id == ledger_account.c.account_id,
                    )
                )
                .where(
                    ledger_journal.c.campaign_id == campaign_id,
                    ledger_journal.c.status == "POSTED",
                )
                .group_by(ledger_account.c.account_code)
            ).all()
            account_balances = {
                str(code): _database_decimal(amount) for code, amount in account_rows
            }
            realized = -account_balances.get(
                AccountCode.REALIZED_PNL.value,
                Decimal(0),
            )
            dividend = -account_balances.get(
                AccountCode.DIVIDEND_INCOME.value,
                Decimal(0),
            )
            fees = account_balances.get(AccountCode.FEE_EXPENSE.value, Decimal(0))
        return {
            "campaign_id": campaign_id,
            "paper_run_count": run_count,
            "completed_run_count": completed_run_count,
            "accepted_opportunity_count": accepted_opportunity_count,
            "order_count": order_count,
            "fill_count": fill_count,
            "open_position_count": open_positions,
            "closed_position_count": closed_positions,
            "cash": balances.settled_cash.amount,
            "ledger_equity": balances.ledger_equity.amount,
            "position_cost": account_balances.get(
                AccountCode.POSITION_COST.value,
                Decimal(0),
            ),
            "cumulative_authorized_risk": cumulative_authorized_risk,
            "realized_pnl": realized,
            "dividend_income": dividend,
            "fees": fees,
            "net_pnl": balances.trading_pnl,
        }

    def presentation(self, campaign_id: str) -> dict[str, object]:
        """Build the local PAPER read model directly from authoritative rows."""

        with self._session_factory() as session:

            def rows(
                table: sa.Table,
                condition: sa.ColumnElement[bool],
                *order_by: sa.ColumnElement[object],
            ) -> list[dict[str, object]]:
                statement = select(table).where(condition)
                if order_by:
                    statement = statement.order_by(*order_by)
                return [dict(item) for item in session.execute(statement).mappings().all()]

            runs = rows(
                securities_paper_run,
                securities_paper_run.c.campaign_id == campaign_id,
                securities_paper_run.c.started_at,
            )
            run_ids = tuple(str(item["run_id"]) for item in runs)
            match_runs = securities_paper_order.c.run_id.in_(run_ids) if run_ids else sa.false()
            paper_orders = rows(
                securities_paper_order,
                match_runs,
                securities_paper_order.c.decision_at,
                securities_paper_order.c.order_id,
            )
            fills = rows(
                securities_fill_evidence,
                (securities_fill_evidence.c.run_id.in_(run_ids) if run_ids else sa.false()),
                securities_fill_evidence.c.occurred_at,
                securities_fill_evidence.c.fill_id,
            )
            positions = rows(
                securities_position_state,
                securities_position_state.c.campaign_id == campaign_id,
                securities_position_state.c.position_id,
            )
            position_ids = tuple(str(item["position_id"]) for item in positions)
            marks = rows(
                securities_mark_evidence,
                (
                    securities_mark_evidence.c.position_id.in_(position_ids)
                    if position_ids
                    else sa.false()
                ),
                securities_mark_evidence.c.marked_at,
            )
            actions = rows(
                securities_corporate_action_application,
                (
                    securities_corporate_action_application.c.position_id.in_(position_ids)
                    if position_ids
                    else sa.false()
                ),
                securities_corporate_action_application.c.applied_at,
            )
            exits = rows(
                securities_exit_decision,
                (
                    securities_exit_decision.c.position_id.in_(position_ids)
                    if position_ids
                    else sa.false()
                ),
                securities_exit_decision.c.decision_at,
            )
            opportunities = rows(
                securities_paper_opportunity,
                (securities_paper_opportunity.c.run_id.in_(run_ids) if run_ids else sa.false()),
                securities_paper_opportunity.c.decision_at,
            )
            opportunity_ids = tuple(str(item["opportunity_id"]) for item in opportunities)
            authorizations = rows(
                securities_paper_authorization,
                (
                    securities_paper_authorization.c.opportunity_id.in_(opportunity_ids)
                    if opportunity_ids
                    else sa.false()
                ),
                securities_paper_authorization.c.authorization_id,
            )
            lineage = rows(
                securities_execution_lineage,
                (securities_execution_lineage.c.run_id.in_(run_ids) if run_ids else sa.false()),
                securities_execution_lineage.c.lineage_id,
            )
            conformance = rows(
                securities_paper_conformance_report,
                (
                    securities_paper_conformance_report.c.paper_run_id.in_(run_ids)
                    if run_ids
                    else sa.false()
                ),
                securities_paper_conformance_report.c.report_id,
            )
            external_ids = tuple(str(item["external_run_id"]) for item in conformance)
            external_runs = rows(
                external_securities_paper_run,
                (
                    external_securities_paper_run.c.external_run_id.in_(external_ids)
                    if external_ids
                    else sa.false()
                ),
                external_securities_paper_run.c.external_run_id,
            )
            trial_ids = tuple(str(item["trial_authorization_id"]) for item in runs)
            trials = rows(
                paper_trial_authorization,
                (
                    paper_trial_authorization.c.authorization_id.in_(trial_ids)
                    if trial_ids
                    else sa.false()
                ),
                paper_trial_authorization.c.authorized_at,
            )
            policy_ids = tuple(str(item["execution_policy_id"]) for item in runs)
            policies = rows(
                securities_execution_policy,
                (
                    securities_execution_policy.c.policy_id.in_(policy_ids)
                    if policy_ids
                    else sa.false()
                ),
                securities_execution_policy.c.created_at,
            )
            journals = rows(
                ledger_journal,
                ledger_journal.c.campaign_id == campaign_id,
                ledger_journal.c.occurred_at,
                ledger_journal.c.journal_id,
            )
            journal_ids = tuple(str(item["journal_id"]) for item in journals)
            postings = rows(
                ledger_posting,
                (ledger_posting.c.journal_id.in_(journal_ids) if journal_ids else sa.false()),
                ledger_posting.c.journal_id,
                ledger_posting.c.sequence,
            )
        return {
            "status": self.status(campaign_id),
            "runs": runs,
            "trials": trials,
            "policies": policies,
            "opportunities": opportunities,
            "authorizations": authorizations,
            "orders": paper_orders,
            "fills": fills,
            "positions": positions,
            "marks": marks,
            "corporate_actions": actions,
            "exits": exits,
            "lineage": lineage,
            "conformance": conformance,
            "external_runs": external_runs,
            "journals": journals,
            "postings": postings,
        }


class PostgresSecuritiesPaperCampaignBoundary:
    """Read Campaign control and ledger state without granting new authority."""

    def __init__(self, engine: Engine) -> None:
        self._session_factory = sessionmaker(
            bind=engine,
            expire_on_commit=False,
            future=True,
        )

    def snapshot(self, campaign_id: str) -> CampaignSnapshot:
        with self._session_factory() as session:
            campaign = PostgresCampaignRepository(session).get(campaign_id)
            if campaign is None:
                raise SecuritiesPaperApplicationError("Campaign was not found")
            balances = PostgresLedgerRepository(session).balances(campaign_id)
            active_securities_risk = _database_decimal(
                session.scalar(
                    select(
                        func.coalesce(
                            func.sum(
                                securities_position_state.c.average_cost
                                * securities_position_state.c.open_quantity
                            ),
                            0,
                        )
                    ).where(
                        securities_position_state.c.campaign_id == campaign_id,
                        securities_position_state.c.state != SecuritiesPositionStatus.CLOSED.value,
                        securities_position_state.c.open_quantity > 0,
                    )
                )
                or 0
            )
        return CampaignSnapshot(
            campaign=campaign,
            ledger_equity=balances.ledger_equity,
            settled_cash=balances.settled_cash,
            active_reservations=Money(active_securities_risk),
        )

    def operating_state(self, campaign_id: str) -> str:
        with self._session_factory() as session:
            state = session.scalar(
                select(campaign_operating_state.c.state).where(
                    campaign_operating_state.c.campaign_id == campaign_id
                )
            )
        return str(state) if state is not None else "RUNNING"
