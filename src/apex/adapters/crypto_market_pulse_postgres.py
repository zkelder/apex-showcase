"""PostgreSQL persistence for immutable crypto market-pulse evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, TypeVar

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.crypto_research_postgres import _data, _payload
from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.application.crypto_research import CryptoResearchConflict
from apex.domain.crypto_market_pulse import (
    CryptoMarketChangeSummary,
    CryptoOpportunityLineage,
    CryptoPulseCheckpoint,
    CryptoPulseSnapshot,
    CryptoRefreshEvent,
    CryptoRefreshPlan,
    CryptoRefreshRun,
    OpportunityTransition,
    PersistenceClassification,
    PulseQuality,
    RefreshState,
)
from apex.domain.crypto_universe import CryptoDailyObservation, CryptoProductObservation
from apex.domain.research_evidence import EvidenceClass

metadata = sa.MetaData()


def _table(name: str, identity: str, *columns: sa.Column[Any]) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(identity, sa.String(160), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
    )


plan_table = _table(
    "crypto_market_pulse_plan",
    "plan_id",
    sa.Column("universe_snapshot_id", sa.String(160), nullable=False),
    sa.Column("enabled", sa.Boolean(), nullable=False),
)
refresh_table = _table(
    "crypto_market_pulse_refresh",
    "refresh_id",
    sa.Column("plan_id", sa.String(160), nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("universe_snapshot_id", sa.String(160), nullable=False),
)
event_table = _table(
    "crypto_market_pulse_event",
    "event_id",
    sa.Column("refresh_id", sa.String(160), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("state", sa.String(40), nullable=False),
)
checkpoint_table = _table(
    "crypto_market_pulse_checkpoint",
    "checkpoint_id",
    sa.Column("refresh_id", sa.String(160), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("product_id", sa.String(160), nullable=False),
    sa.Column("quality", sa.String(40), nullable=False),
)
pulse_table = _table(
    "crypto_market_pulse_snapshot",
    "pulse_id",
    sa.Column("refresh_id", sa.String(160), nullable=False),
    sa.Column("universe_snapshot_id", sa.String(160), nullable=False),
    sa.Column("manifest_hash", sa.String(64), nullable=False),
)
lineage_table = _table(
    "crypto_market_pulse_opportunity_lineage",
    "lineage_id",
    sa.Column("refresh_id", sa.String(160), nullable=False),
    sa.Column("canonical_asset_id", sa.String(80), nullable=False),
    sa.Column("persistence", sa.String(40), nullable=False),
)
summary_table = _table(
    "crypto_market_pulse_change_summary",
    "summary_id",
    sa.Column("refresh_id", sa.String(160), nullable=False),
    sa.Column("pulse_id", sa.String(160), nullable=False),
)


class _Canonical(Protocol):
    @property
    def canonical_hash(self) -> str: ...


T = TypeVar("T", bound=_Canonical)


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CryptoResearchConflict("CRYPTO_PULSE_COLLECTION_INVALID")
    return tuple(str(item) for item in value)


def _pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise CryptoResearchConflict("CRYPTO_PULSE_PAIRS_INVALID")
    return tuple((str(item[0]), str(item[1])) for item in value if len(item) == 2)


def _decimal_pairs(value: object) -> tuple[tuple[str, Decimal], ...]:
    if not isinstance(value, list):
        raise CryptoResearchConflict("CRYPTO_PULSE_DECIMAL_PAIRS_INVALID")
    return tuple((str(item[0]), Decimal(str(item[1]))) for item in value if len(item) == 2)


def _int_pairs(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, list):
        raise CryptoResearchConflict("CRYPTO_PULSE_INT_PAIRS_INVALID")
    return tuple((str(item[0]), int(item[1])) for item in value if len(item) == 2)


def _observation(value: object) -> CryptoProductObservation | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("candles"), list):
        raise CryptoResearchConflict("CRYPTO_PULSE_OBSERVATION_INVALID")
    candles = tuple(
        CryptoDailyObservation(
            bucket_start=_time(item["bucket_start"]),
            close=Decimal(str(item["close"])),
            volume=Decimal(str(item["volume"])),
        )
        for item in value["candles"]
    )
    return CryptoProductObservation(
        observation_id=str(value["observation_id"]),
        source_capture_id=str(value["source_capture_id"]),
        product_id=str(value["product_id"]),
        interval_start=_time(value["interval_start"]),
        interval_end=_time(value["interval_end"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        candles=candles,
        missing_buckets=int(value["missing_buckets"]),
        duplicate_records=int(value["duplicate_records"]),
        rejected_records=int(value["rejected_records"]),
        request_count=int(value["request_count"]),
        raw_evidence_hashes=_strings(value["raw_evidence_hashes"]),
        source_contract_version=str(value["source_contract_version"]),
        parser_version=str(value["parser_version"]),
        checkpoint_ordinal=int(value["checkpoint_ordinal"]),
        completed_at=_time(value["completed_at"]),
        canonical_hash=str(value["canonical_hash"]),
        canonical_asset_id=str(value["canonical_asset_id"]),
        authority="NONE",
    )


def _plan(row: RowMapping) -> CryptoRefreshPlan:
    value = _data(row)
    return CryptoRefreshPlan(
        plan_id=str(value["plan_id"]),
        version=int(value["version"]),
        provider=str(value["provider"]),
        universe_policy_id=str(value["universe_policy_id"]),
        universe_snapshot_id=str(value["universe_snapshot_id"]),
        maximum_product_count=int(value["maximum_product_count"]),
        observation_cadence_hours=int(value["observation_cadence_hours"]),
        universe_refresh_cadence_days=int(value["universe_refresh_cadence_days"]),
        required_observation_types=_strings(value["required_observation_types"]),
        freshness_limit_hours=int(value["freshness_limit_hours"]),
        maximum_requests=int(value["maximum_requests"]),
        maximum_bytes=int(value["maximum_bytes"]),
        request_timeout_seconds=int(value["request_timeout_seconds"]),
        maximum_runtime_milliseconds=int(value["maximum_runtime_milliseconds"]),
        discovery_method_id=str(value["discovery_method_id"]),
        opportunity_policy_id=str(value["opportunity_policy_id"]),
        actor=str(value["actor"]),
        enabled=bool(value["enabled"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        paper_eligible=False,
        live_eligible=False,
        capital_authority="NONE",
    )


def _refresh(row: RowMapping) -> CryptoRefreshRun:
    value = _data(row)
    return CryptoRefreshRun(
        refresh_id=str(value["refresh_id"]),
        plan_id=str(value["plan_id"]),
        as_of=_time(value["as_of"]),
        starting_universe_snapshot_id=str(value["starting_universe_snapshot_id"]),
        request_product_ids=_strings(value["request_product_ids"]),
        universe_refresh_due=bool(value["universe_refresh_due"]),
        interval_start=_time(value["interval_start"]),
        interval_end=_time(value["interval_end"]),
        maximum_requests=int(value["maximum_requests"]),
        maximum_bytes=int(value["maximum_bytes"]),
        maximum_runtime_milliseconds=int(value["maximum_runtime_milliseconds"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _event(row: RowMapping) -> CryptoRefreshEvent:
    value = _data(row)
    return CryptoRefreshEvent(
        event_id=str(value["event_id"]),
        refresh_id=str(value["refresh_id"]),
        ordinal=int(value["ordinal"]),
        state=RefreshState(value["state"]),
        occurred_at=_time(value["occurred_at"]),
        warnings=_strings(value["warnings"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _checkpoint(row: RowMapping) -> CryptoPulseCheckpoint:
    value = _data(row)

    def optional_decimal(name: str) -> Decimal | None:
        return None if value[name] is None else Decimal(str(value[name]))

    return CryptoPulseCheckpoint(
        checkpoint_id=str(value["checkpoint_id"]),
        refresh_id=str(value["refresh_id"]),
        ordinal=int(value["ordinal"]),
        canonical_asset_id=str(value["canonical_asset_id"]),
        product_id=str(value["product_id"]),
        product_status=str(value["product_status"]),
        status_observed_at=_time(value["status_observed_at"]),
        observation=_observation(value["observation"]),
        latest_completed_close=optional_decimal("latest_completed_close"),
        freshness_seconds=(
            None if value["freshness_seconds"] is None else int(value["freshness_seconds"])
        ),
        quality=PulseQuality(value["quality"]),
        quality_reasons=_strings(value["quality_reasons"]),
        raw_bytes=int(value["raw_bytes"]),
        runtime_milliseconds=int(value["runtime_milliseconds"]),
        source_locator=(None if value["source_locator"] is None else str(value["source_locator"])),
        best_bid=optional_decimal("best_bid"),
        best_ask=optional_decimal("best_ask"),
        midpoint=optional_decimal("midpoint"),
        spread_bps=optional_decimal("spread_bps"),
        visible_depth=optional_decimal("visible_depth"),
        quote_limitation=str(value["quote_limitation"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        quote_captured_at=(
            None if value.get("quote_captured_at") is None else _time(value["quote_captured_at"])
        ),
        quote_raw_evidence_hash=(
            None
            if value.get("quote_raw_evidence_hash") is None
            else str(value["quote_raw_evidence_hash"])
        ),
        visible_bid_depth=(
            None
            if value.get("visible_bid_depth") is None
            else Decimal(str(value["visible_bid_depth"]))
        ),
        visible_ask_depth=(
            None
            if value.get("visible_ask_depth") is None
            else Decimal(str(value["visible_ask_depth"]))
        ),
    )


def _pulse(row: RowMapping) -> CryptoPulseSnapshot:
    value = _data(row)
    return CryptoPulseSnapshot(
        pulse_id=str(value["pulse_id"]),
        refresh_id=str(value["refresh_id"]),
        universe_snapshot_id=str(value["universe_snapshot_id"]),
        as_of=_time(value["as_of"]),
        eligible_assets=_strings(value["eligible_assets"]),
        selected_routes=_pairs(value["selected_routes"]),
        checkpoint_ids=_strings(value["checkpoint_ids"]),
        observation_ids=_strings(value["observation_ids"]),
        event_time=_time(value["event_time"]),
        ingested_at=_time(value["ingested_at"]),
        maximum_freshness_seconds=int(value["maximum_freshness_seconds"]),
        missing_observation_count=int(value["missing_observation_count"]),
        current_statuses=_pairs(value["current_statuses"]),
        manifest_hash=str(value["manifest_hash"]),
        limitations=_strings(value["limitations"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _lineage(row: RowMapping) -> CryptoOpportunityLineage:
    value = _data(row)

    def optional_decimal(name: str) -> Decimal | None:
        return None if value[name] is None else Decimal(str(value[name]))

    def optional_int(name: str) -> int | None:
        return None if value[name] is None else int(value[name])

    def optional_time(name: str) -> datetime | None:
        return None if value[name] is None else _time(value[name])

    def optional_string(name: str) -> str | None:
        return None if value[name] is None else str(value[name])

    return CryptoOpportunityLineage(
        lineage_id=str(value["lineage_id"]),
        refresh_id=str(value["refresh_id"]),
        pulse_id=str(value["pulse_id"]),
        as_of=_time(value["as_of"]),
        canonical_asset_id=str(value["canonical_asset_id"]),
        product_id=str(value["product_id"]),
        current_opportunity_id=optional_string("current_opportunity_id"),
        prior_opportunity_id=optional_string("prior_opportunity_id"),
        transition=OpportunityTransition(value["transition"]),
        persistence=PersistenceClassification(value["persistence"]),
        first_positive_at=optional_time("first_positive_at"),
        consecutive_positive_refreshes=int(value["consecutive_positive_refreshes"]),
        comparable_refresh_count=int(value["comparable_refresh_count"]),
        current_score=optional_decimal("current_score"),
        prior_score=optional_decimal("prior_score"),
        score_delta=optional_decimal("score_delta"),
        current_rank=optional_int("current_rank"),
        prior_rank=optional_int("prior_rank"),
        rank_delta=optional_int("rank_delta"),
        expiration=optional_time("expiration"),
        superseding_opportunity_id=optional_string("superseding_opportunity_id"),
        transition_reasons=_strings(value["transition_reasons"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _summary(row: RowMapping) -> CryptoMarketChangeSummary:
    value = _data(row)
    return CryptoMarketChangeSummary(
        summary_id=str(value["summary_id"]),
        refresh_id=str(value["refresh_id"]),
        pulse_id=str(value["pulse_id"]),
        prior_pulse_id=(None if value["prior_pulse_id"] is None else str(value["prior_pulse_id"])),
        assets_added=_strings(value["assets_added"]),
        assets_removed=_strings(value["assets_removed"]),
        route_changes=_strings(value["route_changes"]),
        newly_positive=_strings(value["newly_positive"]),
        persistent_positive=_strings(value["persistent_positive"]),
        reversals=_strings(value["reversals"]),
        expired_opportunities=_strings(value["expired_opportunities"]),
        largest_score_increases=_decimal_pairs(value["largest_score_increases"]),
        largest_score_decreases=_decimal_pairs(value["largest_score_decreases"]),
        rank_changes=_int_pairs(value["rank_changes"]),
        market_breadth=Decimal(str(value["market_breadth"])),
        positive_score_count=int(value["positive_score_count"]),
        rejected_count=int(value["rejected_count"]),
        data_quality_failures=_strings(value["data_quality_failures"]),
        warnings=_strings(value["warnings"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


class PostgresCryptoMarketPulseRepository(PostgresCryptoUniverseRepository):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

    def _get(
        self,
        table: sa.Table,
        column: sa.Column[Any],
        identity: str,
        decode: Callable[[RowMapping], T],
    ) -> T | None:
        row = self._one(sa.select(table).where(column == identity))
        return decode(row) if row is not None else None

    def _save(
        self,
        *,
        table: sa.Table,
        identity_name: str,
        identity: str,
        value: T,
        prior: T | None,
        extra: dict[str, Any],
    ) -> T:
        if prior is not None:
            if prior.canonical_hash != value.canonical_hash:
                raise CryptoResearchConflict("CRYPTO_PULSE_IDENTITY_CONFLICT")
            return prior
        self._insert(
            table,
            {
                identity_name: identity,
                **extra,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_PULSE_IDENTITY_CONFLICT",
        )
        return value

    def refresh_plan(self, plan_id: str) -> CryptoRefreshPlan | None:
        return self._get(plan_table, plan_table.c.plan_id, plan_id, _plan)

    def save_refresh_plan(self, plan: CryptoRefreshPlan) -> CryptoRefreshPlan:
        return self._save(
            table=plan_table,
            identity_name="plan_id",
            identity=plan.plan_id,
            value=plan,
            prior=self.refresh_plan(plan.plan_id),
            extra={
                "universe_snapshot_id": plan.universe_snapshot_id,
                "enabled": plan.enabled,
            },
        )

    def refresh_plans(self) -> tuple[CryptoRefreshPlan, ...]:
        return tuple(
            _plan(row) for row in self._rows(sa.select(plan_table).order_by(plan_table.c.plan_id))
        )

    def refresh_run(self, refresh_id: str) -> CryptoRefreshRun | None:
        return self._get(refresh_table, refresh_table.c.refresh_id, refresh_id, _refresh)

    def refresh_for_plan_as_of(self, plan_id: str, as_of: datetime) -> CryptoRefreshRun | None:
        row = self._one(
            sa.select(refresh_table).where(
                refresh_table.c.plan_id == plan_id,
                refresh_table.c.as_of == as_of,
            )
        )
        return _refresh(row) if row is not None else None

    def save_refresh_run(self, refresh: CryptoRefreshRun) -> CryptoRefreshRun:
        occupied = self.refresh_for_plan_as_of(refresh.plan_id, refresh.as_of)
        if occupied is not None and occupied.refresh_id != refresh.refresh_id:
            raise CryptoResearchConflict("CRYPTO_PULSE_PLAN_AS_OF_CONFLICT")
        return self._save(
            table=refresh_table,
            identity_name="refresh_id",
            identity=refresh.refresh_id,
            value=refresh,
            prior=self.refresh_run(refresh.refresh_id),
            extra={
                "plan_id": refresh.plan_id,
                "as_of": refresh.as_of,
                "universe_snapshot_id": refresh.starting_universe_snapshot_id,
            },
        )

    def refresh_runs(self) -> tuple[CryptoRefreshRun, ...]:
        return tuple(
            _refresh(row)
            for row in self._rows(
                sa.select(refresh_table).order_by(refresh_table.c.as_of, refresh_table.c.refresh_id)
            )
        )

    def refresh_events(self, refresh_id: str) -> tuple[CryptoRefreshEvent, ...]:
        return tuple(
            _event(row)
            for row in self._rows(
                sa.select(event_table)
                .where(event_table.c.refresh_id == refresh_id)
                .order_by(event_table.c.ordinal)
            )
        )

    def save_refresh_event(self, event: CryptoRefreshEvent) -> CryptoRefreshEvent:
        prior = self._get(event_table, event_table.c.event_id, event.event_id, _event)
        return self._save(
            table=event_table,
            identity_name="event_id",
            identity=event.event_id,
            value=event,
            prior=prior,
            extra={
                "refresh_id": event.refresh_id,
                "ordinal": event.ordinal,
                "state": event.state.value,
            },
        )

    def pulse_checkpoint(self, checkpoint_id: str) -> CryptoPulseCheckpoint | None:
        return self._get(
            checkpoint_table, checkpoint_table.c.checkpoint_id, checkpoint_id, _checkpoint
        )

    def save_pulse_checkpoint(self, checkpoint: CryptoPulseCheckpoint) -> CryptoPulseCheckpoint:
        return self._save(
            table=checkpoint_table,
            identity_name="checkpoint_id",
            identity=checkpoint.checkpoint_id,
            value=checkpoint,
            prior=self.pulse_checkpoint(checkpoint.checkpoint_id),
            extra={
                "refresh_id": checkpoint.refresh_id,
                "ordinal": checkpoint.ordinal,
                "product_id": checkpoint.product_id,
                "quality": checkpoint.quality.value,
            },
        )

    def pulse_checkpoints(self, refresh_id: str) -> tuple[CryptoPulseCheckpoint, ...]:
        return tuple(
            _checkpoint(row)
            for row in self._rows(
                sa.select(checkpoint_table)
                .where(checkpoint_table.c.refresh_id == refresh_id)
                .order_by(checkpoint_table.c.ordinal)
            )
        )

    def pulse_snapshot(self, pulse_id: str) -> CryptoPulseSnapshot | None:
        return self._get(pulse_table, pulse_table.c.pulse_id, pulse_id, _pulse)

    def save_pulse_snapshot(self, pulse: CryptoPulseSnapshot) -> CryptoPulseSnapshot:
        return self._save(
            table=pulse_table,
            identity_name="pulse_id",
            identity=pulse.pulse_id,
            value=pulse,
            prior=self.pulse_snapshot(pulse.pulse_id),
            extra={
                "refresh_id": pulse.refresh_id,
                "universe_snapshot_id": pulse.universe_snapshot_id,
                "manifest_hash": pulse.manifest_hash,
            },
        )

    def pulse_snapshots(self) -> tuple[CryptoPulseSnapshot, ...]:
        # ``pulse_id`` is a content-derived string, not a chronological key: an
        # id such as "...public-book-2026-08-02-v1..." sorts after
        # "...genuine-2026-07-30-v1..." (and even after a same-day cycle's own
        # fresh pulse) purely on ASCII order. Callers throughout this module and
        # ``PostgresOperatingStateSource`` rely on the *last* element of this
        # tuple meaning "most recently observed pulse" (see ``pulses[-1]`` /
        # ``prior_pulses[-1]``), so the retained rows must be reordered by their
        # genuine ``as_of``/``ingested_at`` evidence after decoding rather than
        # left in primary-key string order.
        decoded = tuple(
            _pulse(row)
            for row in self._rows(sa.select(pulse_table).order_by(pulse_table.c.pulse_id))
        )
        return tuple(
            sorted(decoded, key=lambda item: (item.as_of, item.ingested_at, item.pulse_id))
        )

    def opportunity_lineage(self, lineage_id: str) -> CryptoOpportunityLineage | None:
        return self._get(lineage_table, lineage_table.c.lineage_id, lineage_id, _lineage)

    def save_opportunity_lineages(
        self, values: tuple[CryptoOpportunityLineage, ...]
    ) -> tuple[CryptoOpportunityLineage, ...]:
        return tuple(
            self._save(
                table=lineage_table,
                identity_name="lineage_id",
                identity=value.lineage_id,
                value=value,
                prior=self.opportunity_lineage(value.lineage_id),
                extra={
                    "refresh_id": value.refresh_id,
                    "canonical_asset_id": value.canonical_asset_id,
                    "persistence": value.persistence.value,
                },
            )
            for value in values
        )

    def opportunity_lineages(self) -> tuple[CryptoOpportunityLineage, ...]:
        return tuple(
            _lineage(row)
            for row in self._rows(sa.select(lineage_table).order_by(lineage_table.c.lineage_id))
        )

    def market_change_summary(self, summary_id: str) -> CryptoMarketChangeSummary | None:
        return self._get(summary_table, summary_table.c.summary_id, summary_id, _summary)

    def save_market_change_summary(
        self, summary: CryptoMarketChangeSummary
    ) -> CryptoMarketChangeSummary:
        return self._save(
            table=summary_table,
            identity_name="summary_id",
            identity=summary.summary_id,
            value=summary,
            prior=self.market_change_summary(summary.summary_id),
            extra={"refresh_id": summary.refresh_id, "pulse_id": summary.pulse_id},
        )

    def market_change_summaries(self) -> tuple[CryptoMarketChangeSummary, ...]:
        return tuple(
            _summary(row)
            for row in self._rows(sa.select(summary_table).order_by(summary_table.c.summary_id))
        )
