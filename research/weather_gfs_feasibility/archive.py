"""Bounded unsigned HTTPS access to the public NOAA GFS archive only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from research.weather_gfs_feasibility.core import (
    GFS_HOST,
    BudgetError,
    ByteRange,
    DecodeError,
    DownloadedObject,
    FeasibilityError,
    FieldEvidence,
    GridPoint,
    RequestBudget,
    decode_temperature_message,
    gfs_object_key,
    latest_gfs_initialization,
    local_contract_hours,
    parse_gfs_index,
    sha256_hex,
    temperature_byte_range,
)


@dataclass(frozen=True)
class SnapshotResult:
    contract_date: date
    lead_hours: int
    as_of: datetime
    initialization: datetime
    complete: bool
    reason: str | None
    fields: tuple[FieldEvidence, ...]
    daily_maximum: Decimal | None


class NoaaGfsArchive:
    """A small no-credential client that never requests a full global object."""

    def __init__(
        self,
        *,
        budget: RequestBudget,
        timeout_seconds: int,
        cache_directory: Path | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise FeasibilityError("timeout seconds must be positive")
        self._budget = budget
        self._timeout_seconds = timeout_seconds
        self._cache_directory = cache_directory

    @property
    def budget(self) -> RequestBudget:
        return self._budget

    def _read(
        self, *, key: str, byte_range: ByteRange | None, maximum_bytes: int
    ) -> tuple[bytes, DownloadedObject]:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise FeasibilityError("unsafe NOAA archive object key")
        cached = self._cached_object(key=key, byte_range=byte_range)
        if cached is not None:
            content, identity = cached
            if len(content) > maximum_bytes:
                raise FeasibilityError("cached NOAA response exceeds the bounded message size")
            return content, identity
        headers = {"User-Agent": "apex-gfs-feasibility/0.1 (research-only)"}
        if byte_range is not None:
            headers["Range"] = f"bytes={byte_range.start}-{byte_range.end}"
        url = f"https://{GFS_HOST}/{key}"
        last_error: Exception | None = None
        for attempt in range(2):
            self._budget.record_request()
            try:
                request = Request(url, headers=headers)
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    status = getattr(response, "status", response.getcode())
                    if byte_range is not None and status != 206:
                        raise FeasibilityError("NOAA range request did not return partial content")
                    if byte_range is None and status != 200:
                        raise FeasibilityError("NOAA index request did not return success")
                    declared_size = response.headers.get("Content-Length")
                    if declared_size is not None:
                        try:
                            declared = int(declared_size)
                        except ValueError as error:
                            raise FeasibilityError(
                                "NOAA response has invalid content length"
                            ) from error
                        if declared > maximum_bytes:
                            raise FeasibilityError("NOAA response exceeds the bounded message size")
                        self._budget.require_bytes(declared)
                    content = response.read(maximum_bytes + 1)
                    if len(content) > maximum_bytes:
                        raise FeasibilityError("NOAA response exceeds the bounded message size")
                    if (
                        byte_range is not None
                        and len(content) != byte_range.end - byte_range.start + 1
                    ):
                        raise FeasibilityError(
                            "NOAA range response is truncated or has unexpected length"
                        )
                    self._budget.record_bytes(len(content))
                    object_identity = DownloadedObject(
                        key=key,
                        etag=response.headers.get("ETag"),
                        sha256=sha256_hex(content),
                        bytes_downloaded=len(content),
                        content_range=response.headers.get("Content-Range"),
                    )
                    self._store_cached_object(
                        key=key,
                        byte_range=byte_range,
                        content=content,
                        identity=object_identity,
                    )
                    return content, object_identity
            except HTTPError as error:
                if error.code >= 500 and attempt == 0:
                    last_error = error
                    continue
                raise FeasibilityError(f"NOAA archive HTTP {error.code} for {key}") from error
            except (TimeoutError, URLError) as error:
                if attempt == 0:
                    last_error = error
                    continue
                raise FeasibilityError(f"NOAA archive network failure for {key}") from error
        raise FeasibilityError(
            "NOAA archive request failed after one bounded retry"
        ) from last_error

    def _cache_paths(self, *, key: str, byte_range: ByteRange | None) -> tuple[Path, Path] | None:
        if self._cache_directory is None:
            return None
        range_label = "index" if byte_range is None else f"{byte_range.start}-{byte_range.end}"
        digest = sha256_hex(f"{key}|{range_label}")
        return (
            self._cache_directory / "noaa" / f"{digest}.bin",
            self._cache_directory / "noaa" / f"{digest}.json",
        )

    def _cached_object(
        self, *, key: str, byte_range: ByteRange | None
    ) -> tuple[bytes, DownloadedObject] | None:
        paths = self._cache_paths(key=key, byte_range=byte_range)
        if paths is None:
            return None
        body_path, metadata_path = paths
        if not body_path.is_file() or not metadata_path.is_file():
            return None
        try:
            content = body_path.read_bytes()
            raw_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(raw_metadata, dict):
                return None
            identity = DownloadedObject(
                key=str(raw_metadata["key"]),
                etag=raw_metadata.get("etag")
                if isinstance(raw_metadata.get("etag"), str)
                else None,
                sha256=str(raw_metadata["sha256"]),
                bytes_downloaded=int(raw_metadata["bytes_downloaded"]),
                content_range=raw_metadata.get("content_range")
                if isinstance(raw_metadata.get("content_range"), str)
                else None,
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if identity.key != key or identity.bytes_downloaded != len(content):
            return None
        if identity.sha256 != sha256_hex(content):
            return None
        return content, identity

    def _store_cached_object(
        self,
        *,
        key: str,
        byte_range: ByteRange | None,
        content: bytes,
        identity: DownloadedObject,
    ) -> None:
        paths = self._cache_paths(key=key, byte_range=byte_range)
        if paths is None:
            return
        body_path, metadata_path = paths
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(content)
        metadata_path.write_text(
            json.dumps(
                {
                    "bytes_downloaded": identity.bytes_downloaded,
                    "content_range": identity.content_range,
                    "etag": identity.etag,
                    "key": identity.key,
                    "sha256": identity.sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    def index(self, key: str) -> tuple[bytes, DownloadedObject]:
        return self._read(key=f"{key}.idx", byte_range=None, maximum_bytes=1_000_000)

    def field(self, key: str, byte_range: ByteRange) -> tuple[bytes, DownloadedObject]:
        return self._read(key=key, byte_range=byte_range, maximum_bytes=32_000_000)


def reconstruct_daily_maximum(
    *,
    archive: NoaaGfsArchive,
    contract_date: date,
    close_time: datetime,
    lead_hours: int,
    timezone_name: str,
    grid_point: GridPoint,
    output_unit: str,
) -> SnapshotResult:
    if close_time.tzinfo is None:
        raise FeasibilityError("Kalshi close time must include a timezone")
    if lead_hours not in (12, 24, 48):
        raise FeasibilityError("lead hours must be exactly one of 12, 24, or 48")
    as_of = close_time.astimezone(UTC) - timedelta(hours=lead_hours)
    initialization = latest_gfs_initialization(as_of)
    try:
        forecast_hours = local_contract_hours(
            initialization=initialization,
            contract_date=contract_date,
            timezone_name=timezone_name,
        )
    except FeasibilityError as error:
        return SnapshotResult(
            contract_date=contract_date,
            lead_hours=lead_hours,
            as_of=as_of,
            initialization=initialization,
            complete=False,
            reason=str(error),
            fields=(),
            daily_maximum=None,
        )
    try:
        archive.budget.require_requests(2 * len(forecast_hours))
    except BudgetError as error:
        return SnapshotResult(
            contract_date=contract_date,
            lead_hours=lead_hours,
            as_of=as_of,
            initialization=initialization,
            complete=False,
            reason=str(error),
            fields=(),
            daily_maximum=None,
        )
    evidence: list[FieldEvidence] = []
    for forecast_hour in forecast_hours:
        key = gfs_object_key(initialization, forecast_hour.forecast_hour)
        try:
            raw_index, index_object = archive.index(key)
            selected_range = temperature_byte_range(parse_gfs_index(raw_index))
            message, message_object = archive.field(key, selected_range)
            decoded = decode_temperature_message(
                message=message,
                expected_valid_time=forecast_hour.valid_time,
                grid_point=grid_point,
                output_unit=output_unit,
            )
            evidence.append(
                FieldEvidence(
                    forecast_hour=forecast_hour.forecast_hour,
                    valid_time=forecast_hour.valid_time,
                    object_key=key,
                    index=index_object,
                    field_range=selected_range,
                    message=message_object,
                    temperature=decoded,
                )
            )
        except (BudgetError, DecodeError, FeasibilityError) as error:
            return SnapshotResult(
                contract_date=contract_date,
                lead_hours=lead_hours,
                as_of=as_of,
                initialization=initialization,
                complete=False,
                reason=str(error),
                fields=tuple(evidence),
                daily_maximum=None,
            )
    return SnapshotResult(
        contract_date=contract_date,
        lead_hours=lead_hours,
        as_of=as_of,
        initialization=initialization,
        complete=True,
        reason=None,
        fields=tuple(evidence),
        daily_maximum=max(item.temperature.value for item in evidence),
    )
