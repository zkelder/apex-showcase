from __future__ import annotations

from datetime import UTC, datetime

from apex.adapters.combined_crypto_historical_evidence import CombinedCryptoHistoricalEvidenceSource
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.research_evidence import EvidenceClass


def _candle(capture_id: str, day: str) -> CryptoDailyCandle:
    return CryptoDailyCandle.create(
        capture_id=capture_id,
        attempt_id=f"attempt-{capture_id}",
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        bucket_start=datetime.fromisoformat(f"{day}T00:00:00+00:00"),
        open="100",
        high="101",
        low="99",
        close="100.5",
        volume="10",
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw_response_hash="a" * 64,
    )


class FakeSource:
    def __init__(self, by_capture: dict[str, tuple[CryptoDailyCandle, ...]]) -> None:
        self._by_capture = by_capture

    def candles(self, capture_id: str) -> tuple[CryptoDailyCandle, ...]:
        return self._by_capture.get(capture_id, ())


def test_combined_source_concatenates_contiguous_captures_in_order() -> None:
    early = (_candle("cap-a", "2020-07-01"), _candle("cap-a", "2020-07-02"))
    late = (_candle("cap-b", "2020-07-03"), _candle("cap-b", "2020-07-04"))
    source = FakeSource({"cap-a": early, "cap-b": late})
    combined = CombinedCryptoHistoricalEvidenceSource(
        source=source, combined_captures={"combined-v1": ("cap-a", "cap-b")}
    )
    result = combined.candles("combined-v1")
    assert [c.bucket_start.date().isoformat() for c in result] == [
        "2020-07-01",
        "2020-07-02",
        "2020-07-03",
        "2020-07-04",
    ]


def test_unregistered_capture_id_passes_through_to_the_wrapped_source() -> None:
    real = (_candle("cap-real", "2024-01-01"),)
    source = FakeSource({"cap-real": real})
    combined = CombinedCryptoHistoricalEvidenceSource(source=source, combined_captures={})
    assert combined.candles("cap-real") == real


def test_duplicate_bucket_resolves_deterministically_by_lowest_candle_id() -> None:
    same_day_a = _candle("cap-a", "2020-07-01")
    same_day_b = _candle("cap-b", "2020-07-01")
    source = FakeSource({"cap-a": (same_day_a,), "cap-b": (same_day_b,)})
    combined = CombinedCryptoHistoricalEvidenceSource(
        source=source, combined_captures={"combined-v1": ("cap-a", "cap-b")}
    )
    result = combined.candles("combined-v1")
    assert len(result) == 1
    expected = min(same_day_a, same_day_b, key=lambda item: item.candle_id)
    assert result[0].candle_id == expected.candle_id
