"""Pure selection, indexing, extraction, and provenance helpers for the spike."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

GFS_BUCKET = "noaa-gfs-bdp-pds"
GFS_HOST = f"{GFS_BUCKET}.s3.amazonaws.com"
GFS_GRID_STEP = Decimal("0.25")
GFS_HOURLY_MAX_FORECAST_HOUR = 120
GFS_CYCLE_HOURS = (0, 6, 12, 18)


class FeasibilityError(ValueError):
    """A retained reason why a bounded feasibility operation is invalid."""


class CoverageError(FeasibilityError):
    """The selected latest run cannot cover every local contract-day hour."""


class IndexError(FeasibilityError):
    """A GFS index cannot identify one safe field byte range."""


class BudgetError(FeasibilityError):
    """A request or downloaded-byte cap would be exceeded."""


class DecodeError(FeasibilityError):
    """A bounded GRIB message does not prove the expected temperature field."""


@dataclass(frozen=True)
class GridPoint:
    requested_latitude: Decimal
    requested_longitude: Decimal
    latitude: Decimal
    longitude: Decimal
    distance_km: Decimal
    method: str = "NEAREST_GRID_POINT"


@dataclass(frozen=True)
class ForecastHour:
    forecast_hour: int
    valid_time: datetime


@dataclass(frozen=True)
class IndexRecord:
    record_number: int
    byte_offset: int
    field: str
    level: str
    raw: str


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int


@dataclass(frozen=True)
class DownloadedObject:
    key: str
    etag: str | None
    sha256: str
    bytes_downloaded: int
    content_range: str | None


@dataclass(frozen=True)
class DecodedTemperature:
    valid_time: datetime
    source_kelvin: Decimal
    value: Decimal
    unit: str
    grid_latitude: Decimal
    grid_longitude: Decimal


@dataclass(frozen=True)
class FieldEvidence:
    forecast_hour: int
    valid_time: datetime
    object_key: str
    index: DownloadedObject
    field_range: ByteRange
    message: DownloadedObject
    temperature: DecodedTemperature


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def sha256_hex(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def binding_configuration(path: Path) -> dict[str, object]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise FeasibilityError("binding configuration must be a JSON object")
    payload: dict[str, object] = {str(key): value for key, value in decoded.items()}
    configured_hash = payload.pop("canonical_configuration_hash", None)
    calculated_hash = sha256_hex(canonical_json_bytes(payload))
    if configured_hash != calculated_hash:
        raise FeasibilityError("binding configuration hash does not match its canonical content")
    payload["canonical_configuration_hash"] = calculated_hash
    return payload


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise FeasibilityError("timestamp must include an offset")
    return parsed.astimezone(UTC)


def latest_gfs_initialization(as_of: datetime) -> datetime:
    if as_of.tzinfo is None:
        raise FeasibilityError("as-of timestamp must be timezone-aware")
    utc_value = as_of.astimezone(UTC)
    cycle_hour = max(hour for hour in GFS_CYCLE_HOURS if hour <= utc_value.hour)
    return utc_value.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)


def gfs_object_key(initialization: datetime, forecast_hour: int) -> str:
    if initialization.tzinfo is None or initialization.astimezone(UTC) != initialization:
        raise FeasibilityError("GFS initialization must be UTC")
    if not 0 <= forecast_hour <= 384:
        raise FeasibilityError("GFS forecast hour must be between 0 and 384")
    stamp = initialization.strftime("%Y%m%d")
    cycle = initialization.strftime("%H")
    return f"gfs.{stamp}/{cycle}/atmos/gfs.t{cycle}z.pgrb2.0p25.f{forecast_hour:03d}"


def local_contract_hours(
    *, initialization: datetime, contract_date: date, timezone_name: str
) -> tuple[ForecastHour, ...]:
    if initialization.tzinfo is None:
        raise FeasibilityError("GFS initialization must be timezone-aware")
    initialization = initialization.astimezone(UTC)
    timezone = ZoneInfo(timezone_name)
    local_start = datetime.combine(contract_date, time.min, tzinfo=timezone)
    local_end = datetime.combine(contract_date + timedelta(days=1), time.min, tzinfo=timezone)
    valid_time = local_start.astimezone(UTC)
    end_time = local_end.astimezone(UTC)
    selected: list[ForecastHour] = []
    missing: list[str] = []
    while valid_time < end_time:
        delta_seconds = int((valid_time - initialization).total_seconds())
        if delta_seconds % 3600 != 0:
            raise CoverageError("GFS valid time is not aligned to an hourly forecast")
        forecast_hour = delta_seconds // 3600
        if forecast_hour < 0 or forecast_hour > GFS_HOURLY_MAX_FORECAST_HOUR:
            missing.append(valid_time.isoformat().replace("+00:00", "Z"))
        else:
            selected.append(ForecastHour(forecast_hour=forecast_hour, valid_time=valid_time))
        valid_time += timedelta(hours=1)
    if missing:
        raise CoverageError(
            "latest GFS initialization cannot cover every local contract-day hour: "
            + ", ".join(missing)
        )
    return tuple(selected)


def _rounded_index(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def _normalized_longitude(longitude: Decimal) -> Decimal:
    normalized = longitude % Decimal("360")
    return normalized if normalized >= 0 else normalized + Decimal("360")


def _display_longitude(longitude: Decimal) -> Decimal:
    return longitude - Decimal("360") if longitude > Decimal("180") else longitude


def _haversine_km(
    latitude_a: Decimal, longitude_a: Decimal, latitude_b: Decimal, longitude_b: Decimal
) -> Decimal:
    radians = math.pi / 180
    lat_a = float(latitude_a) * radians
    lon_a = float(longitude_a) * radians
    lat_b = float(latitude_b) * radians
    lon_b = float(longitude_b) * radians
    sine_latitude = math.sin((lat_b - lat_a) / 2)
    sine_longitude = math.sin((lon_b - lon_a) / 2)
    haversine = sine_latitude**2 + math.cos(lat_a) * math.cos(lat_b) * sine_longitude**2
    kilometers = 6371.0088 * 2 * math.asin(math.sqrt(haversine))
    return Decimal(str(kilometers)).quantize(Decimal("0.001"))


def nearest_gfs_grid_point(latitude: Decimal, longitude: Decimal) -> GridPoint:
    if not Decimal("-90") <= latitude <= Decimal("90"):
        raise FeasibilityError("latitude is outside the GFS global grid")
    latitude_index = _rounded_index((Decimal("90") - latitude) / GFS_GRID_STEP)
    latitude_index = min(720, max(0, latitude_index))
    grid_latitude = Decimal("90") - GFS_GRID_STEP * latitude_index
    longitude_index = _rounded_index(_normalized_longitude(longitude) / GFS_GRID_STEP) % 1440
    grid_longitude = _display_longitude(GFS_GRID_STEP * longitude_index)
    return GridPoint(
        requested_latitude=latitude,
        requested_longitude=longitude,
        latitude=grid_latitude,
        longitude=grid_longitude,
        distance_km=_haversine_km(latitude, longitude, grid_latitude, grid_longitude),
    )


def parse_gfs_index(raw: bytes) -> tuple[IndexRecord, ...]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise IndexError("GFS index is not UTF-8 text") from error
    records: list[IndexRecord] = []
    previous_offset = -1
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split(":")
        if len(fields) < 5:
            raise IndexError("GFS index record is malformed")
        try:
            record_number = int(fields[0])
            byte_offset = int(fields[1])
        except ValueError as error:
            raise IndexError("GFS index record has a non-integer record or offset") from error
        if record_number <= 0 or byte_offset <= previous_offset:
            raise IndexError("GFS index offsets must be strictly increasing")
        records.append(
            IndexRecord(
                record_number=record_number,
                byte_offset=byte_offset,
                field=fields[3].strip(),
                level=fields[4].strip(),
                raw=line,
            )
        )
        previous_offset = byte_offset
    if not records:
        raise IndexError("GFS index contains no records")
    return tuple(records)


def temperature_byte_range(records: tuple[IndexRecord, ...]) -> ByteRange:
    matches = [
        index
        for index, record in enumerate(records)
        if record.field == "TMP" and record.level == "2 m above ground"
    ]
    if not matches:
        raise IndexError("GFS index has no TMP:2 m above ground field")
    if len(matches) != 1:
        raise IndexError("GFS index has ambiguous TMP:2 m above ground fields")
    match = matches[0]
    if match + 1 >= len(records):
        raise IndexError("GFS index cannot bound the final TMP field without a next offset")
    start = records[match].byte_offset
    end = records[match + 1].byte_offset - 1
    if start < 0 or end < start:
        raise IndexError("GFS index selected an invalid byte range")
    return ByteRange(start=start, end=end)


@dataclass
class RequestBudget:
    maximum_requests: int
    maximum_bytes: int
    requests: int = 0
    downloaded_bytes: int = 0

    def require_requests(self, count: int) -> None:
        if count < 0 or self.requests + count > self.maximum_requests:
            raise BudgetError("request limit would be exceeded before a complete snapshot starts")

    def record_request(self) -> None:
        self.require_requests(1)
        self.requests += 1

    def require_bytes(self, count: int) -> None:
        if count < 0 or self.downloaded_bytes + count > self.maximum_bytes:
            raise BudgetError("download limit would be exceeded")

    def record_bytes(self, count: int) -> None:
        self.require_bytes(count)
        self.downloaded_bytes += count


def _eccodes_module() -> Any:
    try:
        return importlib.import_module("eccodes")
    except ModuleNotFoundError as error:
        raise DecodeError("research-only eccodes decoder is unavailable") from error


def _codes_value(module: Any, handle: Any, key: str) -> Any:
    try:
        return module.codes_get(handle, key)
    except Exception as error:  # ecCodes exposes implementation-specific exception classes.
        raise DecodeError(f"GRIB message is missing required key {key}") from error


def _grid_position(
    *,
    latitude: Decimal,
    longitude: Decimal,
    latitude_first: Decimal,
    longitude_first: Decimal,
    latitude_increment: Decimal,
    longitude_increment: Decimal,
    ni: int,
    nj: int,
    j_scans_positively: bool,
) -> tuple[int, int, Decimal, Decimal]:
    target_longitude = _normalized_longitude(longitude)
    first_longitude = _normalized_longitude(longitude_first)
    longitude_steps = (
        _rounded_index((target_longitude - first_longitude) / longitude_increment) % ni
    )
    if j_scans_positively:
        latitude_steps = _rounded_index((latitude - latitude_first) / latitude_increment)
    else:
        latitude_steps = _rounded_index((latitude_first - latitude) / latitude_increment)
    if not 0 <= longitude_steps < ni or not 0 <= latitude_steps < nj:
        raise DecodeError("selected grid point is outside decoded GRIB grid")
    actual_longitude = _display_longitude(
        _normalized_longitude(first_longitude + longitude_increment * longitude_steps)
    )
    actual_latitude = latitude_first + (
        latitude_increment * latitude_steps
        if j_scans_positively
        else -latitude_increment * latitude_steps
    )
    return longitude_steps, latitude_steps, actual_latitude, actual_longitude


def decode_temperature_message(
    *,
    message: bytes,
    expected_valid_time: datetime,
    grid_point: GridPoint,
    output_unit: str,
) -> DecodedTemperature:
    module = _eccodes_module()
    handle = module.codes_new_from_message(message)
    if handle is None:
        raise DecodeError("ecCodes could not open the selected GRIB message")
    try:
        if _codes_value(module, handle, "typeOfLevel") != "heightAboveGround":
            raise DecodeError("GRIB field is not at height above ground")
        if Decimal(str(_codes_value(module, handle, "level"))) != Decimal("2"):
            raise DecodeError("GRIB field is not at two metres above ground")
        if _codes_value(module, handle, "units") != "K":
            raise DecodeError("GRIB temperature units are not verified Kelvin")
        if _codes_value(module, handle, "gridType") != "regular_ll":
            raise DecodeError("GRIB field is not on the expected regular latitude-longitude grid")
        validity_date = str(_codes_value(module, handle, "validityDate"))
        validity_time = f"{int(_codes_value(module, handle, 'validityTime')):04d}"
        decoded_valid_time = datetime.strptime(validity_date + validity_time, "%Y%m%d%H%M").replace(
            tzinfo=UTC
        )
        if decoded_valid_time != expected_valid_time.astimezone(UTC):
            raise DecodeError("GRIB valid time does not match the requested forecast hour")
        ni = int(_codes_value(module, handle, "Ni"))
        nj = int(_codes_value(module, handle, "Nj"))
        longitude_index, latitude_index, actual_latitude, actual_longitude = _grid_position(
            latitude=grid_point.latitude,
            longitude=grid_point.longitude,
            latitude_first=Decimal(
                str(_codes_value(module, handle, "latitudeOfFirstGridPointInDegrees"))
            ),
            longitude_first=Decimal(
                str(_codes_value(module, handle, "longitudeOfFirstGridPointInDegrees"))
            ),
            latitude_increment=Decimal(
                str(_codes_value(module, handle, "jDirectionIncrementInDegrees"))
            ),
            longitude_increment=Decimal(
                str(_codes_value(module, handle, "iDirectionIncrementInDegrees"))
            ),
            ni=ni,
            nj=nj,
            j_scans_positively=bool(_codes_value(module, handle, "jScansPositively")),
        )
        values = module.codes_get_values(handle)
        position = latitude_index * ni + longitude_index
        if position >= len(values):
            raise DecodeError("GRIB values do not cover the selected grid position")
        kelvin = Decimal(str(values[position]))
        if output_unit == "F":
            value = (kelvin - Decimal("273.15")) * Decimal("9") / Decimal("5") + Decimal("32")
        elif output_unit == "C":
            value = kelvin - Decimal("273.15")
        elif output_unit == "K":
            value = kelvin
        else:
            raise DecodeError("configured temperature unit is unsupported")
        return DecodedTemperature(
            valid_time=decoded_valid_time,
            source_kelvin=kelvin,
            value=value,
            unit=output_unit,
            grid_latitude=actual_latitude,
            grid_longitude=actual_longitude,
        )
    finally:
        module.codes_release(handle)


def daily_maximum(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise CoverageError("a daily maximum requires at least one selected local-hour temperature")
    return max(values)


def compact_json(value: object) -> str:
    def convert(item: object) -> object:
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, datetime):
            return item.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, Path):
            return str(item)
        if hasattr(item, "__dataclass_fields__"):
            return {key: convert(value) for key, value in asdict(cast(Any, item)).items()}
        if isinstance(item, dict):
            return {str(key): convert(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(value) for value in item]
        return item

    return json.dumps(convert(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
