"""A read-only *view*, never a new capture, over several already-genuine,
contiguous ``CryptoHistoricalCapture`` windows (milestone section 10/12).

``application.historical_replay.HistoricalReplayEngine`` takes exactly one
``crypto_capture_id`` per invocation (``CryptoHistoricalEvidencePort.
candles(capture_id)``). This environment's genuine BTC-USD history already
exists as three separate, contiguous, non-overlapping COMPLETED captures
(``btc-usd-sealed-holdout-capture-2020-07-01-2022-07-01-v1``, ``btc-usd-
genuine-replication-2022-07-01-2024-07-01-v1``, ``btc-usd-genuine-2024-07-
01-2026-07-01-v3``) rather than one spanning the whole range. Rather than
re-running a fourth genuine capture over an already-retained span (which
the milestone explicitly asks us not to do -- "no genuine operating worker
active" / "bounded requests"), this adapter concatenates the exact retained
``CryptoDailyCandle`` rows from all three under one virtual capture id.

Every candle returned is a genuine, previously-persisted row from a real,
governed capture; nothing here fabricates, interpolates, or re-fetches
anything. Any capture id other than the registered combined one is passed
straight through to the wrapped repository unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from apex.domain.crypto_research import CryptoDailyCandle


class CryptoCandleSource(Protocol):
    def candles(self, capture_id: str) -> tuple[CryptoDailyCandle, ...]: ...


# The three genuine (non-fixture), COMPLETED, contiguous, non-overlapping
# BTC-USD captures already retained in this environment as of the genuine
# multi-asset milestone (2020-07-01 through 2026-06-30 combined, verified
# gap-free at the exact 2022-06-30/2022-07-01 and 2024-06-30/2024-07-01
# boundaries). Any later genuinely-retained capture can be added here in
# chronological order without touching the combining logic itself.
COMBINED_GENUINE_BTC_CAPTURE_ID = "btc-usd-genuine-combined-2020-07-01-2026-06-30-v1"
COMBINED_GENUINE_BTC_CAPTURES: Mapping[str, tuple[str, ...]] = {
    COMBINED_GENUINE_BTC_CAPTURE_ID: (
        "btc-usd-sealed-holdout-capture-2020-07-01-2022-07-01-v1",
        "btc-usd-genuine-replication-2022-07-01-2024-07-01-v1",
        "btc-usd-genuine-2024-07-01-2026-07-01-v3",
    )
}


class CombinedCryptoHistoricalEvidenceSource:
    """Wraps a real ``CryptoCandleSource`` (e.g.
    ``PostgresCryptoResearchRepository``); satisfies the identical
    ``CryptoHistoricalEvidencePort`` protocol the replay engine already
    depends on, so it is a drop-in substitute, not a parallel mechanism."""

    def __init__(
        self, *, source: CryptoCandleSource, combined_captures: Mapping[str, tuple[str, ...]]
    ) -> None:
        """``combined_captures`` maps one virtual capture id to the ordered
        tuple of genuine, already-retained capture ids it concatenates."""

        self._source = source
        self._combined_captures = dict(combined_captures)

    def candles(self, capture_id: str) -> tuple[CryptoDailyCandle, ...]:
        underlying_ids = self._combined_captures.get(capture_id)
        if underlying_ids is None:
            return self._source.candles(capture_id)
        by_bucket: dict[object, CryptoDailyCandle] = {}
        for underlying_id in underlying_ids:
            for candle in self._source.candles(underlying_id):
                # Contiguous, non-overlapping captures are not expected to
                # collide; a genuine duplicate bucket keeps whichever candle
                # sorts first by candle_id, deterministically, rather than
                # silently picking "last write wins".
                existing = by_bucket.get(candle.bucket_start)
                if existing is None or candle.candle_id < existing.candle_id:
                    by_bucket[candle.bucket_start] = candle
        return tuple(sorted(by_bucket.values(), key=lambda item: item.bucket_start))
