"""Managed immutable total-return batch derivation for Securities research."""

from __future__ import annotations

from datetime import datetime

from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter, securities_record
from apex.application.securities import (
    ImportSecuritiesData,
    ImportSecuritiesDataCommand,
    SecuritiesRepository,
)
from apex.domain.securities import SecuritiesMarketDataBatch
from apex.domain.securities_total_return import (
    TOTAL_RETURN_POLICY_ID,
    TotalReturnLineage,
    derive_total_return_bars,
)
from apex.domain.values import content_digest


def derive_total_return_batch(
    *,
    repository: SecuritiesRepository,
    source_batch_id: str,
    idempotency_key: str,
    imported_at: datetime,
) -> tuple[SecuritiesMarketDataBatch, TotalReturnLineage]:
    instruments, sessions, bars, actions = repository.get_dataset(source_batch_id)
    lineage = derive_total_return_bars(bars=bars, actions=actions)
    adapter = LocalSecuritiesDatasetAdapter()
    identity = content_digest(
        {"policy": TOTAL_RETURN_POLICY_ID, "source": source_batch_id, "result": lineage.result_hash}
    )
    source = f"generated/total-return-{identity}.jsonl"
    adapter.write_generated_jsonl(
        relative_location=source,
        records=tuple(
            securities_record(item) for item in (*instruments, *sessions, *lineage.bars, *actions)
        ),
    )
    batch = ImportSecuritiesData(repository=repository, market_data=adapter).execute(
        ImportSecuritiesDataCommand(
            managed_source=source,
            dataset_name="genuine-apex-derived-total-return-v1",
            idempotency_key=idempotency_key,
            imported_at=imported_at,
        )
    )
    return batch, lineage
