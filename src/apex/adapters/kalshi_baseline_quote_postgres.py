"""Immutable PostgreSQL evidence for Kalshi Decision-Linked Baseline Quote
Evidence v1.

Persists one ``KalshiBaselineQuoteEvidence`` row per genuinely captured
``decision_id`` -- append-only, never mutated, never deleted. Also composes
the batched, N+1-safe read source the recurring capture task
(``application.kalshi_baseline_quote.SyncKalshiBaselineQuoteEvidence``) uses
to find Kalshi decisions still missing a baseline quote and to read the
already-retained ``binary_complement_evaluation``/``binary_complement_leg``
and ``threshold_bundle_evaluation``/``threshold_matched_slice``/
``threshold_market_relationship`` rows those decisions already reference. No
provider client, credential, or network capability lives here, and no row
this module writes ever carries an ``authority`` other than ``'NONE'``.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.counterfactual_ledger_postgres import _decision, decision_table
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.domain.counterfactual_ledger import CounterfactualDecision
from apex.domain.kalshi_baseline_quote import (
    KalshiBaselineLegRole,
    KalshiBaselineQuoteError,
    KalshiBaselineQuoteEvidence,
    KalshiBaselineQuoteLeg,
    KalshiMarketQuoteBaselineEvidence,
    KalshiMarketQuoteLeg,
    KalshiOrderbookTopOfBook,
    KalshiQuoteKind,
    KalshiQuoteLegRole,
)
from apex.domain.public_complement import (
    BinaryComplementEvaluation,
    ComplementFillSlice,
    ComplementState,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.public_threshold import (
    ThresholdBundleEvaluation,
    ThresholdEvaluationReasonCode,
    ThresholdEvaluationState,
    ThresholdMatchedSlice,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import ContentHash, canonical_json_bytes

metadata = sa.MetaData()

quote_table = sa.Table(
    "kalshi_baseline_quote",
    metadata,
    sa.Column("quote_id", sa.String(300), primary_key=True),
    sa.Column(
        "decision_id",
        sa.String(300),
        sa.ForeignKey("counterfactual_decision.decision_id"),
        nullable=False,
        unique=True,
    ),
    sa.Column("policy_version", sa.String(60), nullable=False),
    sa.Column("venue", sa.String(20), nullable=False),
    sa.Column("subject_source_record_type", sa.String(80), nullable=False),
    sa.Column("decision_evaluated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("run_id", sa.String(300), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    # Kalshi Baseline Quote Capture & Economic Maturation v1 (migration
    # 0108): first-class discriminator between the two structurally
    # distinct evidence shapes one row can carry -- lets an operator/read
    # query count rows by kind without parsing ``payload`` JSON.
    sa.Column("quote_kind", sa.String(30), nullable=False),
)

KALSHI_BASELINE_QUOTE_TABLES = ("kalshi_baseline_quote",)

# Read-only proxies for tables genuinely owned and written elsewhere
# (``adapters.postgres``'s ``binary_complement_evaluation``/
# ``binary_complement_leg``/``threshold_bundle_evaluation``/
# ``threshold_matched_slice``/``threshold_market_relationship``).
# Deliberately declared locally, under this module's own ``MetaData``,
# rather than imported from ``adapters.postgres`` directly: ``postgres.py``
# itself must import ``KALSHI_BASELINE_QUOTE_TABLES`` from *this* module (to
# register the immutability trigger and the persistence table-name registry
# below) -- importing the reverse direction here would create a genuine
# module-load-time circular import between the two.  Mirrors the existing
# ``counterfactual_ledger_postgres.decision_table`` idiom: a second,
# read-oriented ``sa.Table`` declaration for a table whose schema is owned
# and migrated elsewhere is a normal, already-established SQLAlchemy Core
# pattern in this codebase -- multiple ``Table`` objects under different
# ``MetaData`` instances can safely build SQL against the same physical
# Postgres table, and this module never issues DDL against these five
# tables, only bounded, indexed ``SELECT``s.  Only the columns this module
# actually reads are declared.
_evidence_metadata = sa.MetaData()

_binary_complement_evaluation_table = sa.Table(
    "binary_complement_evaluation",
    _evidence_metadata,
    sa.Column("evaluation_id", sa.String(160), primary_key=True),
    sa.Column("ingestion_run_id", sa.String(160)),
    sa.Column("orderbook_snapshot_id", sa.String(160)),
    sa.Column("market_snapshot_id", sa.String(160)),
    sa.Column("fee_snapshot_id", sa.String(160)),
    sa.Column("provider", sa.String(30)),
    sa.Column("provider_market_ticker", sa.String(300)),
    sa.Column("evaluator_version", sa.String(80)),
    sa.Column("evaluated_at", sa.DateTime(timezone=True)),
    sa.Column("maximum_depth", sa.Integer),
    sa.Column("maximum_bundle_quantity", sa.Numeric),
    sa.Column("rounding_identity", sa.String(160)),
    sa.Column("matched_quantity", sa.Numeric),
    sa.Column("weighted_yes_ask", sa.Numeric),
    sa.Column("weighted_no_ask", sa.Numeric),
    sa.Column("gross_acquisition_cost", sa.Numeric),
    sa.Column("gross_settlement_value", sa.Numeric),
    sa.Column("gross_complement_gap", sa.Numeric),
    sa.Column("yes_modeled_taker_fee", sa.Numeric),
    sa.Column("no_modeled_taker_fee", sa.Numeric),
    sa.Column("modeled_taker_fees", sa.Numeric),
    sa.Column("conservative_rounding_reserve", sa.Numeric),
    sa.Column("total_modeled_acquisition_cost", sa.Numeric),
    sa.Column("conservative_net_lower_bound", sa.Numeric),
    sa.Column("net_lower_bound_per_contract", sa.Numeric),
    sa.Column("state", sa.String(20)),
    sa.Column("reason_codes", ARRAY(sa.Text)),
    sa.Column("canonical_input_hash", sa.String(64)),
    extend_existing=True,
)

_binary_complement_leg_table = sa.Table(
    "binary_complement_leg",
    _evidence_metadata,
    sa.Column("slice_id", sa.String(160), primary_key=True),
    sa.Column("evaluation_id", sa.String(160)),
    sa.Column("ordinal", sa.Integer),
    sa.Column("purchased_side", sa.String(3)),
    sa.Column("source_level_id", sa.String(160)),
    sa.Column("quantity", sa.Numeric),
    sa.Column("ask_price", sa.Numeric),
    sa.Column("acquisition_cost", sa.Numeric),
    sa.Column("modeled_trade_fee", sa.Numeric),
    sa.Column("rounding_reserve", sa.Numeric),
    extend_existing=True,
)

_threshold_bundle_evaluation_table = sa.Table(
    "threshold_bundle_evaluation",
    _evidence_metadata,
    sa.Column("evaluation_id", sa.String(160), primary_key=True),
    sa.Column("scan_id", sa.String(160)),
    sa.Column("round_ordinal", sa.Integer),
    sa.Column("relationship_id", sa.String(160)),
    sa.Column("broader_orderbook_snapshot_id", sa.String(160)),
    sa.Column("narrower_orderbook_snapshot_id", sa.String(160)),
    sa.Column("broader_fee_snapshot_id", sa.String(160)),
    sa.Column("narrower_fee_snapshot_id", sa.String(160)),
    sa.Column("broader_observed_at", sa.DateTime(timezone=True)),
    sa.Column("narrower_observed_at", sa.DateTime(timezone=True)),
    sa.Column("observation_skew", sa.Interval()),
    sa.Column("maximum_observation_skew", sa.Interval()),
    sa.Column("matched_quantity", sa.Numeric),
    sa.Column("broader_yes_weighted_ask", sa.Numeric),
    sa.Column("narrower_no_weighted_ask", sa.Numeric),
    sa.Column("broader_cost", sa.Numeric),
    sa.Column("narrower_cost", sa.Numeric),
    sa.Column("gross_acquisition_cost", sa.Numeric),
    sa.Column("settlement_floor_per_pair", sa.Numeric),
    sa.Column("gross_lower_bound_value", sa.Numeric),
    sa.Column("broader_modeled_fee", sa.Numeric),
    sa.Column("narrower_modeled_fee", sa.Numeric),
    sa.Column("total_modeled_fee", sa.Numeric),
    sa.Column("conservative_rounding_reserve", sa.Numeric),
    sa.Column("total_modeled_acquisition_cost", sa.Numeric),
    sa.Column("conservative_net_lower_bound", sa.Numeric),
    sa.Column("conservative_lower_bound_per_contract", sa.Numeric),
    sa.Column("state", sa.String(20)),
    sa.Column("reason_codes", ARRAY(sa.Text)),
    sa.Column("canonical_configuration_hash", sa.String(64)),
    sa.Column("canonical_input_hash", sa.String(64)),
    extend_existing=True,
)

_threshold_matched_slice_table = sa.Table(
    "threshold_matched_slice",
    _evidence_metadata,
    sa.Column("slice_id", sa.String(160), primary_key=True),
    sa.Column("evaluation_id", sa.String(160)),
    sa.Column("ordinal", sa.Integer),
    sa.Column("broader_yes_level_id", sa.String(160)),
    sa.Column("narrower_no_level_id", sa.String(160)),
    sa.Column("quantity", sa.Numeric),
    sa.Column("broader_yes_ask", sa.Numeric),
    sa.Column("narrower_no_ask", sa.Numeric),
    sa.Column("broader_cost", sa.Numeric),
    sa.Column("narrower_cost", sa.Numeric),
    sa.Column("broader_fee", sa.Numeric),
    sa.Column("narrower_fee", sa.Numeric),
    sa.Column("broader_rounding_reserve", sa.Numeric),
    sa.Column("narrower_rounding_reserve", sa.Numeric),
    sa.Column("settlement_floor_value", sa.Numeric),
    sa.Column("conservative_lower_bound", sa.Numeric),
    extend_existing=True,
)

_threshold_market_relationship_table = sa.Table(
    "threshold_market_relationship",
    _evidence_metadata,
    sa.Column("relationship_id", sa.String(160), primary_key=True),
    sa.Column("broader_market_ticker", sa.String(300)),
    sa.Column("narrower_market_ticker", sa.String(300)),
    extend_existing=True,
)

# Kalshi Baseline Quote Capture & Economic Maturation v1: read-only proxies
# for the durable Kalshi orderbook archive (``adapters.postgres``'s
# ``market_orderbook_snapshot``/``market_orderbook_level``, migrations
# 0010/0011) -- the exact, already-retained, immutable evidence every
# ``binary_complement_evaluation``/``threshold_bundle_evaluation`` row's own
# ``orderbook_snapshot_id`` pointer(s) already resolve to (confirmed by
# direct inspection: 100% referential integrity across every retained
# row). Same "second Table declaration under this module's own MetaData"
# idiom as the four tables immediately above, for the identical
# circular-import reason.
_market_orderbook_snapshot_table = sa.Table(
    "market_orderbook_snapshot",
    _evidence_metadata,
    sa.Column("orderbook_snapshot_id", sa.String(160), primary_key=True),
    sa.Column("provider_market_ticker", sa.String(300)),
    sa.Column("observed_at", sa.DateTime(timezone=True)),
    sa.Column("yes_bid_level_count", sa.Integer),
    sa.Column("no_bid_level_count", sa.Integer),
    sa.Column("data_quality_state", sa.String(20)),
    extend_existing=True,
)

_market_orderbook_level_table = sa.Table(
    "market_orderbook_level",
    _evidence_metadata,
    sa.Column("level_id", sa.String(160), primary_key=True),
    sa.Column("orderbook_snapshot_id", sa.String(160)),
    sa.Column("side", sa.String(20)),
    sa.Column("bid_price", sa.Numeric),
    extend_existing=True,
)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_RETAINED_PAYLOAD_INVALID")
    return value


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_RETAINED_COLLECTION_INVALID")
    return tuple(str(item) for item in value)


def _leg(value: Mapping[str, object]) -> KalshiBaselineQuoteLeg:
    return KalshiBaselineQuoteLeg(
        ordinal=int(str(value["ordinal"])),
        leg_role=KalshiBaselineLegRole(str(value["leg_role"])),
        market_ticker=str(value["market_ticker"]),
        purchased_side=str(value["purchased_side"]),
        source_level_id=str(value["source_level_id"]),
        quantity=_decimal(value["quantity"]),
        acquisition_price=_decimal(value["acquisition_price"]),
        acquisition_cost=_decimal(value["acquisition_cost"]),
        modeled_fee=_decimal(value["modeled_fee"]),
        rounding_reserve=_decimal(value["rounding_reserve"]),
    )


def _matched_trade_quote(row: RowMapping) -> KalshiBaselineQuoteEvidence:
    value = _data(row)
    return KalshiBaselineQuoteEvidence(
        quote_id=str(value["quote_id"]),
        policy_version=str(value["policy_version"]),
        decision_id=str(value["decision_id"]),
        decision_canonical_hash=str(value["decision_canonical_hash"]),
        venue=SharedVenue(value["venue"]),
        subject_projection_id=str(value["subject_projection_id"]),
        subject_source_record_type=str(value["subject_source_record_type"]),
        subject_source_record_id=str(value["subject_source_record_id"]),
        subject_source_canonical_hash=str(value["subject_source_canonical_hash"]),
        instrument_id=str(value["instrument_id"]),
        product_or_market_id=str(value["product_or_market_id"]),
        market_tickers=_strings(value["market_tickers"]),
        decision_evaluated_at=_time(value["decision_evaluated_at"]),
        evaluation_observed_at=tuple(_time(item) for item in value["evaluation_observed_at"]),
        captured_at=_time(value["captured_at"]),
        evaluator_version=str(value["evaluator_version"]),
        fee_model_identity=str(value["fee_model_identity"]),
        rounding_identity=str(value["rounding_identity"]),
        legs=tuple(_leg(item) for item in value["legs"]),
        matched_quantity=_decimal(value["matched_quantity"]),
        gross_acquisition_cost=_decimal(value["gross_acquisition_cost"]),
        total_modeled_fees=_decimal(value["total_modeled_fees"]),
        total_modeled_acquisition_cost=_decimal(value["total_modeled_acquisition_cost"]),
        conservative_net_lower_bound=_decimal(value["conservative_net_lower_bound"]),
        modeled_settlement_value=_decimal(value["modeled_settlement_value"]),
        settlement_value_basis=str(value["settlement_value_basis"]),
        source_evidence_class=str(value["source_evidence_class"]),
        run_id=str(value["run_id"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _market_quote_leg(value: Mapping[str, object]) -> KalshiMarketQuoteLeg:
    return KalshiMarketQuoteLeg(
        ordinal=int(str(value["ordinal"])),
        leg_role=KalshiQuoteLegRole(str(value["leg_role"])),
        market_ticker=str(value["market_ticker"]),
        orderbook_snapshot_id=str(value["orderbook_snapshot_id"]),
        quote_observed_at=_time(value["quote_observed_at"]),
        yes_bid=_decimal(value["yes_bid"]),
        no_bid=_decimal(value["no_bid"]),
        yes_ask=_decimal(value["yes_ask"]),
        no_ask=_decimal(value["no_ask"]),
        implied_yes_probability=_decimal(value["implied_yes_probability"]),
    )


def _market_quote_baseline(row: RowMapping) -> KalshiMarketQuoteBaselineEvidence:
    value = _data(row)
    return KalshiMarketQuoteBaselineEvidence(
        quote_id=str(value["quote_id"]),
        policy_version=str(value["policy_version"]),
        decision_id=str(value["decision_id"]),
        decision_canonical_hash=str(value["decision_canonical_hash"]),
        venue=SharedVenue(value["venue"]),
        subject_projection_id=str(value["subject_projection_id"]),
        subject_source_record_type=str(value["subject_source_record_type"]),
        subject_source_record_id=str(value["subject_source_record_id"]),
        subject_source_canonical_hash=str(value["subject_source_canonical_hash"]),
        instrument_id=str(value["instrument_id"]),
        product_or_market_id=str(value["product_or_market_id"]),
        decision_evaluated_at=_time(value["decision_evaluated_at"]),
        legs=tuple(_market_quote_leg(item) for item in value["legs"]),
        captured_at=_time(value["captured_at"]),
        evaluator_version=str(value["evaluator_version"]),
        source_evidence_class=str(value["source_evidence_class"]),
        run_id=str(value["run_id"]),
        canonical_hash=str(value["canonical_hash"]),
        quote_kind=str(value["quote_kind"]),
        authority="NONE",
    )


def _quote(row: RowMapping) -> KalshiBaselineQuoteEvidence | KalshiMarketQuoteBaselineEvidence:
    """Dispatches on the row's own ``quote_kind`` column -- never on
    ``payload`` shape sniffing -- to reconstruct whichever of the two
    structurally distinct evidence types this row genuinely is."""

    kind = str(row["quote_kind"])
    if kind == KalshiQuoteKind.MARKET_QUOTE_OBSERVATION.value:
        return _market_quote_baseline(row)
    if kind == KalshiQuoteKind.MATCHED_TRADE_EXECUTION.value:
        return _matched_trade_quote(row)
    raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_RETAINED_QUOTE_KIND_INVALID")


class PostgresKalshiBaselineQuoteRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def quote(
        self, quote_id: str
    ) -> KalshiBaselineQuoteEvidence | KalshiMarketQuoteBaselineEvidence | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(sa.select(quote_table).where(quote_table.c.quote_id == quote_id))
                .mappings()
                .one_or_none()
            )
        return None if row is None else _quote(row)

    def quote_for_decision(
        self, decision_id: str
    ) -> KalshiBaselineQuoteEvidence | KalshiMarketQuoteBaselineEvidence | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(quote_table).where(quote_table.c.decision_id == decision_id)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _quote(row)

    def save_quote(
        self, *, quote: KalshiBaselineQuoteEvidence | KalshiMarketQuoteBaselineEvidence
    ) -> bool:
        """Persist one baseline quote of either kind. Idempotent: replaying
        the identical ``quote_id``/``canonical_hash`` (a genuine
        crash-resume replay, or a later cycle catching up on the same
        still-uncaptured decision) returns ``True`` and writes nothing; a
        conflicting payload under the same ``quote_id`` raises rather than
        silently overwriting retained evidence -- mirrors
        ``PostgresCounterfactualOutcomeRepository.save_aggregate``/
        ``PostgresCounterfactualFollowUpRepository.save_observation``
        exactly. The ``decision_id`` unique constraint is a second,
        defensive layer: even a caller that bypassed
        ``kalshi_baseline_quote_id`` and somehow computed a different
        ``quote_id`` string for the same decision could not insert a second
        row for it -- correct for either kind, since the two are mutually
        exclusive per decision by construction (see
        ``domain.kalshi_baseline_quote``'s market-quote-observation module
        comment)."""

        quote_kind = (
            KalshiQuoteKind.MATCHED_TRADE_EXECUTION.value
            if isinstance(quote, KalshiBaselineQuoteEvidence)
            else KalshiQuoteKind.MARKET_QUOTE_OBSERVATION.value
        )
        try:
            with self._engine.begin() as connection:
                existing = connection.scalar(
                    sa.select(quote_table.c.canonical_hash).where(
                        quote_table.c.quote_id == quote.quote_id
                    )
                )
                if existing is not None:
                    if existing != quote.canonical_hash:
                        raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_IDENTITY_CONFLICT")
                    return True
                connection.execute(
                    quote_table.insert().values(
                        quote_id=quote.quote_id,
                        decision_id=quote.decision_id,
                        policy_version=quote.policy_version,
                        venue=quote.venue.value,
                        subject_source_record_type=quote.subject_source_record_type,
                        decision_evaluated_at=quote.decision_evaluated_at,
                        captured_at=quote.captured_at,
                        run_id=quote.run_id,
                        payload=_payload(quote),
                        canonical_hash=quote.canonical_hash,
                        authority="NONE",
                        quote_kind=quote_kind,
                    )
                )
            return False
        except IntegrityError as error:
            raise KalshiBaselineQuoteError("KALSHI_BASELINE_QUOTE_PERSISTENCE_CONFLICT") from error

    def all_quotes(
        self,
    ) -> tuple[KalshiBaselineQuoteEvidence | KalshiMarketQuoteBaselineEvidence, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(quote_table).order_by(quote_table.c.quote_id)
            ).mappings()
            return tuple(_quote(row) for row in rows)

    def matched_trade_quotes(self) -> tuple[KalshiBaselineQuoteEvidence, ...]:
        """Only the ``MATCHED_TRADE_EXECUTION`` (V1) subset -- the exact
        input ``domain.counterfactual_outcome.reduce_kalshi_binary_complement
        _outcome``/``COUNTERFACTUAL_OUTCOME_POLICY_V2`` requires. A
        ``MARKET_QUOTE_OBSERVATION`` row would not even satisfy that
        function's own type (it has no ``settlement_value_basis``/
        ``total_modeled_acquisition_cost``), so filtering at the SQL layer
        -- rather than letting a caller filter a mixed ``all_quotes()``
        result by hand -- makes the exclusion structural, not a convention
        every caller must remember."""

        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(quote_table)
                .where(quote_table.c.quote_kind == KalshiQuoteKind.MATCHED_TRADE_EXECUTION.value)
                .order_by(quote_table.c.quote_id)
            ).mappings()
            return tuple(_matched_trade_quote(row) for row in rows)

    def census(self) -> dict[str, object]:
        """Bounded operator diagnostic: retained baseline-quote counts by
        source evaluator type and by evidence kind, plus the most recent
        successful capture time -- never a substitute for reading a
        specific quote's own leg manifest."""

        with self._engine.connect() as connection:
            by_type: dict[str, int] = {
                str(source_type): int(count)
                for source_type, count in connection.execute(
                    sa.select(quote_table.c.subject_source_record_type, sa.func.count()).group_by(
                        quote_table.c.subject_source_record_type
                    )
                ).all()
            }
            by_kind: dict[str, int] = {
                str(kind): int(count)
                for kind, count in connection.execute(
                    sa.select(quote_table.c.quote_kind, sa.func.count()).group_by(
                        quote_table.c.quote_kind
                    )
                ).all()
            }
            total = connection.scalar(sa.select(sa.func.count()).select_from(quote_table))
            latest_captured_at = connection.scalar(
                sa.select(sa.func.max(quote_table.c.captured_at))
            )
        return {
            "total_quote_rows": int(total or 0),
            "by_source_record_type": by_type,
            "by_quote_kind": by_kind,
            "matched_trade_execution_rows": by_kind.get(
                KalshiQuoteKind.MATCHED_TRADE_EXECUTION.value, 0
            ),
            "market_quote_observation_rows": by_kind.get(
                KalshiQuoteKind.MARKET_QUOTE_OBSERVATION.value, 0
            ),
            "latest_captured_at": latest_captured_at,
        }


def _decimal_from_row(value: object) -> Decimal:
    return Decimal(str(value))


def _fill_slice(row: RowMapping) -> ComplementFillSlice:
    return ComplementFillSlice(
        slice_id=str(row["slice_id"]),
        evaluation_id=str(row["evaluation_id"]),
        ordinal=int(row["ordinal"]),
        purchased_side=str(row["purchased_side"]),
        source_level_id=str(row["source_level_id"]),
        quantity=_decimal_from_row(row["quantity"]),
        ask_price=_decimal_from_row(row["ask_price"]),
        acquisition_cost=_decimal_from_row(row["acquisition_cost"]),
        modeled_trade_fee=_decimal_from_row(row["modeled_trade_fee"]),
        rounding_reserve=_decimal_from_row(row["rounding_reserve"]),
    )


def _matched_slice(row: RowMapping) -> ThresholdMatchedSlice:
    return ThresholdMatchedSlice(
        slice_id=str(row["slice_id"]),
        evaluation_id=str(row["evaluation_id"]),
        ordinal=int(row["ordinal"]),
        broader_yes_level_id=str(row["broader_yes_level_id"]),
        narrower_no_level_id=str(row["narrower_no_level_id"]),
        quantity=_decimal_from_row(row["quantity"]),
        broader_yes_ask=_decimal_from_row(row["broader_yes_ask"]),
        narrower_no_ask=_decimal_from_row(row["narrower_no_ask"]),
        broader_cost=_decimal_from_row(row["broader_cost"]),
        narrower_cost=_decimal_from_row(row["narrower_cost"]),
        broader_fee=_decimal_from_row(row["broader_fee"]),
        narrower_fee=_decimal_from_row(row["narrower_fee"]),
        broader_rounding_reserve=_decimal_from_row(row["broader_rounding_reserve"]),
        narrower_rounding_reserve=_decimal_from_row(row["narrower_rounding_reserve"]),
        settlement_floor_value=_decimal_from_row(row["settlement_floor_value"]),
        conservative_lower_bound=_decimal_from_row(row["conservative_lower_bound"]),
    )


class PostgresKalshiBaselineQuoteEvidenceSource:
    """Read-only, batched (never N+1) evidence source for
    ``application.kalshi_baseline_quote.SyncKalshiBaselineQuoteEvidence``.
    Every method here issues a small, fixed number of queries regardless of
    how many decisions are pending -- never one query per decision."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.quotes = PostgresKalshiBaselineQuoteRepository(engine)
        self._shared_opportunity = PostgresSharedOpportunityRepository(engine)

    def projections_by_id(
        self, projection_ids: Collection[str]
    ) -> Mapping[str, SharedOpportunityProjection]:
        """Delegates to the existing, already-batched ``PostgresShared
        OpportunitySource.projections_by_id`` -- one ``WHERE projection_id
        IN (...)`` round trip, the identical read
        ``RetainedCounterfactualOutcomeSource`` already relies on; never a
        new query pattern."""

        return self._shared_opportunity.projections_by_id(projection_ids)

    def orderbook_top_of_book(
        self, snapshot_ids: Collection[str]
    ) -> dict[str, KalshiOrderbookTopOfBook]:
        """Batched, N+1-safe: exactly two queries regardless of how many
        snapshot ids are named -- one for snapshot metadata, one aggregate
        ``GROUP BY (orderbook_snapshot_id, side)`` for the top-of-book
        (``MAX``) bid on each side. A side with zero retained levels for a
        given snapshot simply has no row in the aggregate, which is exactly
        how ``KalshiOrderbookTopOfBook.yes_bid``/``no_bid`` distinguish
        "genuinely one-sided" from every other case -- never inferred,
        never a sentinel."""

        if not snapshot_ids:
            return {}
        with self._engine.connect() as connection:
            snapshot_rows = connection.execute(
                sa.select(_market_orderbook_snapshot_table).where(
                    _market_orderbook_snapshot_table.c.orderbook_snapshot_id.in_(snapshot_ids)
                )
            ).mappings()
            snapshots = {str(row["orderbook_snapshot_id"]): row for row in snapshot_rows}
            if not snapshots:
                return {}
            bid_rows = connection.execute(
                sa.select(
                    _market_orderbook_level_table.c.orderbook_snapshot_id,
                    _market_orderbook_level_table.c.side,
                    sa.func.max(_market_orderbook_level_table.c.bid_price).label("best_bid"),
                )
                .where(_market_orderbook_level_table.c.orderbook_snapshot_id.in_(snapshots.keys()))
                .group_by(
                    _market_orderbook_level_table.c.orderbook_snapshot_id,
                    _market_orderbook_level_table.c.side,
                )
            ).all()
        best_bid_by_snapshot_and_side = {
            (str(snapshot_id), str(side)): _decimal_from_row(best_bid)
            for snapshot_id, side, best_bid in bid_rows
        }
        return {
            snapshot_id: KalshiOrderbookTopOfBook(
                orderbook_snapshot_id=snapshot_id,
                market_ticker=str(row["provider_market_ticker"]),
                observed_at=_time(row["observed_at"]),
                data_quality_state=str(row["data_quality_state"]),
                yes_bid=best_bid_by_snapshot_and_side.get((snapshot_id, "YES_BID")),
                no_bid=best_bid_by_snapshot_and_side.get((snapshot_id, "NO_BID")),
            )
            for snapshot_id, row in snapshots.items()
        }

    def pending_kalshi_decisions(self, *, limit: int) -> tuple[CounterfactualDecision, ...]:
        """KALSHI-venue decisions with no retained baseline quote yet,
        oldest ``decision_id`` first, bounded to ``limit`` -- one indexed
        query with a ``NOT EXISTS`` anti-join, never a per-decision probe.
        Filters to ``SUPPORTED_BASELINE_QUOTE_SOURCE_RECORD_TYPES`` in
        Python after this bounded read (the source-record-type lives inside
        ``decision_table.payload``'s JSON, not a first-class indexed
        column) -- correct because the caller (``SyncKalshiBaselineQuoteEvidence
        .execute``) treats an unsupported type exactly like "nothing to
        capture" for that row, at zero additional query cost."""

        not_captured = ~sa.exists(
            sa.select(sa.literal(1)).where(
                quote_table.c.decision_id == decision_table.c.decision_id
            )
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(decision_table)
                .where(decision_table.c.venue == SharedVenue.KALSHI.value, not_captured)
                .order_by(decision_table.c.decision_id)
                .limit(limit)
            ).mappings()
            return tuple(_decision(row) for row in rows)

    def binary_complement_evidence(
        self, evaluation_ids: tuple[str, ...]
    ) -> dict[str, tuple[BinaryComplementEvaluation, tuple[ComplementFillSlice, ...]]]:
        if not evaluation_ids:
            return {}
        with self._engine.connect() as connection:
            evaluation_rows = connection.execute(
                sa.select(_binary_complement_evaluation_table).where(
                    _binary_complement_evaluation_table.c.evaluation_id.in_(evaluation_ids)
                )
            ).mappings()
            evaluations = {str(row["evaluation_id"]): row for row in evaluation_rows}
            leg_rows = connection.execute(
                sa.select(_binary_complement_leg_table)
                .where(_binary_complement_leg_table.c.evaluation_id.in_(evaluations.keys()))
                .order_by(
                    _binary_complement_leg_table.c.evaluation_id,
                    _binary_complement_leg_table.c.ordinal,
                )
            ).mappings()
            legs_by_evaluation: dict[str, list[ComplementFillSlice]] = {}
            for row in leg_rows:
                legs_by_evaluation.setdefault(str(row["evaluation_id"]), []).append(
                    _fill_slice(row)
                )
        return {
            evaluation_id: (
                _binary_complement_evaluation(row),
                tuple(legs_by_evaluation.get(evaluation_id, ())),
            )
            for evaluation_id, row in evaluations.items()
        }

    def threshold_bundle_evidence(
        self, evaluation_ids: tuple[str, ...]
    ) -> dict[
        str,
        tuple[ThresholdBundleEvaluation, tuple[ThresholdMatchedSlice, ...], str, str],
    ]:
        if not evaluation_ids:
            return {}
        with self._engine.connect() as connection:
            evaluation_rows = connection.execute(
                sa.select(_threshold_bundle_evaluation_table).where(
                    _threshold_bundle_evaluation_table.c.evaluation_id.in_(evaluation_ids)
                )
            ).mappings()
            evaluations = {str(row["evaluation_id"]): row for row in evaluation_rows}
            relationship_ids = {str(row["relationship_id"]) for row in evaluations.values()}
            relationship_rows = (
                connection.execute(
                    sa.select(
                        _threshold_market_relationship_table.c.relationship_id,
                        _threshold_market_relationship_table.c.broader_market_ticker,
                        _threshold_market_relationship_table.c.narrower_market_ticker,
                    ).where(
                        _threshold_market_relationship_table.c.relationship_id.in_(relationship_ids)
                    )
                ).mappings()
                if relationship_ids
                else ()
            )
            tickers_by_relationship = {
                str(row["relationship_id"]): (
                    str(row["broader_market_ticker"]),
                    str(row["narrower_market_ticker"]),
                )
                for row in relationship_rows
            }
            slice_rows = connection.execute(
                sa.select(_threshold_matched_slice_table)
                .where(_threshold_matched_slice_table.c.evaluation_id.in_(evaluations.keys()))
                .order_by(
                    _threshold_matched_slice_table.c.evaluation_id,
                    _threshold_matched_slice_table.c.ordinal,
                )
            ).mappings()
            slices_by_evaluation: dict[str, list[ThresholdMatchedSlice]] = {}
            for row in slice_rows:
                slices_by_evaluation.setdefault(str(row["evaluation_id"]), []).append(
                    _matched_slice(row)
                )
        result: dict[
            str, tuple[ThresholdBundleEvaluation, tuple[ThresholdMatchedSlice, ...], str, str]
        ] = {}
        for evaluation_id, row in evaluations.items():
            tickers = tickers_by_relationship.get(str(row["relationship_id"]))
            if tickers is None:
                continue
            broader_ticker, narrower_ticker = tickers
            result[evaluation_id] = (
                _threshold_bundle_evaluation(row),
                tuple(slices_by_evaluation.get(evaluation_id, ())),
                broader_ticker,
                narrower_ticker,
            )
        return result


def _binary_complement_evaluation(row: RowMapping) -> BinaryComplementEvaluation:
    return BinaryComplementEvaluation(
        evaluation_id=str(row["evaluation_id"]),
        ingestion_run_id=str(row["ingestion_run_id"]),
        orderbook_snapshot_id=str(row["orderbook_snapshot_id"]),
        market_snapshot_id=str(row["market_snapshot_id"]),
        fee_snapshot_id=(None if row["fee_snapshot_id"] is None else str(row["fee_snapshot_id"])),
        provider=PublicMarketProvider(row["provider"]),
        provider_market_ticker=str(row["provider_market_ticker"]),
        evaluator_version=str(row["evaluator_version"]),
        evaluated_at=_time(row["evaluated_at"]),
        maximum_depth=int(row["maximum_depth"]),
        maximum_bundle_quantity=_decimal_from_row(row["maximum_bundle_quantity"]),
        rounding_identity=str(row["rounding_identity"]),
        matched_quantity=_decimal_from_row(row["matched_quantity"]),
        weighted_yes_ask=(
            None if row["weighted_yes_ask"] is None else _decimal_from_row(row["weighted_yes_ask"])
        ),
        weighted_no_ask=(
            None if row["weighted_no_ask"] is None else _decimal_from_row(row["weighted_no_ask"])
        ),
        gross_acquisition_cost=_decimal_from_row(row["gross_acquisition_cost"]),
        gross_settlement_value=_decimal_from_row(row["gross_settlement_value"]),
        gross_complement_gap=_decimal_from_row(row["gross_complement_gap"]),
        yes_modeled_taker_fee=_decimal_from_row(row["yes_modeled_taker_fee"]),
        no_modeled_taker_fee=_decimal_from_row(row["no_modeled_taker_fee"]),
        modeled_taker_fees=_decimal_from_row(row["modeled_taker_fees"]),
        conservative_rounding_reserve=_decimal_from_row(row["conservative_rounding_reserve"]),
        total_modeled_acquisition_cost=_decimal_from_row(row["total_modeled_acquisition_cost"]),
        conservative_net_lower_bound=_decimal_from_row(row["conservative_net_lower_bound"]),
        net_lower_bound_per_contract=(
            None
            if row["net_lower_bound_per_contract"] is None
            else _decimal_from_row(row["net_lower_bound_per_contract"])
        ),
        state=ComplementState(row["state"]),
        reason_codes=tuple(row["reason_codes"] or ()),
        canonical_input_hash=ContentHash(str(row["canonical_input_hash"])),
    )


def _threshold_bundle_evaluation(row: RowMapping) -> ThresholdBundleEvaluation:
    return ThresholdBundleEvaluation(
        evaluation_id=str(row["evaluation_id"]),
        scan_id=str(row["scan_id"]),
        round_ordinal=int(row["round_ordinal"]),
        relationship_id=str(row["relationship_id"]),
        broader_orderbook_snapshot_id=(
            None
            if row["broader_orderbook_snapshot_id"] is None
            else str(row["broader_orderbook_snapshot_id"])
        ),
        narrower_orderbook_snapshot_id=(
            None
            if row["narrower_orderbook_snapshot_id"] is None
            else str(row["narrower_orderbook_snapshot_id"])
        ),
        broader_fee_snapshot_id=(
            None if row["broader_fee_snapshot_id"] is None else str(row["broader_fee_snapshot_id"])
        ),
        narrower_fee_snapshot_id=(
            None
            if row["narrower_fee_snapshot_id"] is None
            else str(row["narrower_fee_snapshot_id"])
        ),
        broader_observed_at=(
            None if row["broader_observed_at"] is None else _time(row["broader_observed_at"])
        ),
        narrower_observed_at=(
            None if row["narrower_observed_at"] is None else _time(row["narrower_observed_at"])
        ),
        observation_skew=row["observation_skew"],
        maximum_observation_skew=row["maximum_observation_skew"],
        matched_quantity=_decimal_from_row(row["matched_quantity"]),
        broader_yes_weighted_ask=(
            None
            if row["broader_yes_weighted_ask"] is None
            else _decimal_from_row(row["broader_yes_weighted_ask"])
        ),
        narrower_no_weighted_ask=(
            None
            if row["narrower_no_weighted_ask"] is None
            else _decimal_from_row(row["narrower_no_weighted_ask"])
        ),
        broader_cost=_decimal_from_row(row["broader_cost"]),
        narrower_cost=_decimal_from_row(row["narrower_cost"]),
        gross_acquisition_cost=_decimal_from_row(row["gross_acquisition_cost"]),
        settlement_floor_per_pair=_decimal_from_row(row["settlement_floor_per_pair"]),
        gross_lower_bound_value=_decimal_from_row(row["gross_lower_bound_value"]),
        broader_modeled_fee=_decimal_from_row(row["broader_modeled_fee"]),
        narrower_modeled_fee=_decimal_from_row(row["narrower_modeled_fee"]),
        total_modeled_fee=_decimal_from_row(row["total_modeled_fee"]),
        conservative_rounding_reserve=_decimal_from_row(row["conservative_rounding_reserve"]),
        total_modeled_acquisition_cost=_decimal_from_row(row["total_modeled_acquisition_cost"]),
        conservative_net_lower_bound=_decimal_from_row(row["conservative_net_lower_bound"]),
        conservative_lower_bound_per_contract=(
            None
            if row["conservative_lower_bound_per_contract"] is None
            else _decimal_from_row(row["conservative_lower_bound_per_contract"])
        ),
        state=ThresholdEvaluationState(row["state"]),
        reason_codes=tuple(
            ThresholdEvaluationReasonCode(code) for code in (row["reason_codes"] or ())
        ),
        canonical_configuration_hash=ContentHash(str(row["canonical_configuration_hash"])),
        canonical_input_hash=ContentHash(str(row["canonical_input_hash"])),
    )
