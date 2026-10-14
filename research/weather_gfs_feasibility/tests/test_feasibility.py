from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research.weather_gfs_feasibility import archive
from research.weather_gfs_feasibility.archive import NoaaGfsArchive, SnapshotResult
from research.weather_gfs_feasibility.core import (
    BudgetError,
    ByteRange,
    CoverageError,
    FeasibilityError,
    RequestBudget,
    binding_configuration,
    daily_maximum,
    gfs_object_key,
    latest_gfs_initialization,
    local_contract_hours,
    nearest_gfs_grid_point,
    parse_gfs_index,
    temperature_byte_range,
)
from research.weather_gfs_feasibility.run import _compact_report

PACKAGE = Path(__file__).resolve().parents[1]


def test_binding_configuration_is_canonical_and_has_reviewed_station() -> None:
    configuration = binding_configuration(PACKAGE / "binding.json")

    assert configuration["selected_series"] == "KXHIGHLAX"
    assert configuration["station"] == {
        "identifier": "KLAX",
        "metadata_url": "https://api.weather.gov/stations/KLAX",
        "name": "Los Angeles, Los Angeles International Airport",
    }
    assert configuration["timezone"] == "America/Los_Angeles"


@pytest.mark.parametrize(
    ("lead_hours", "expected_cycle"),
    ((48, datetime(2026, 5, 19, 6, tzinfo=UTC)), (24, datetime(2026, 5, 20, 6, tzinfo=UTC))),
)
def test_latest_cycle_at_or_before_as_of_is_selected(
    lead_hours: int, expected_cycle: datetime
) -> None:
    close_time = datetime(2026, 5, 21, 7, 59, tzinfo=UTC)
    as_of = close_time - timedelta(hours=lead_hours)

    assert latest_gfs_initialization(as_of) == expected_cycle
    assert latest_gfs_initialization(as_of) <= as_of


def test_twelve_hour_latest_cycle_does_not_backfill_local_day() -> None:
    close_time = datetime(2026, 5, 21, 7, 59, tzinfo=UTC)
    initialization = latest_gfs_initialization(close_time - timedelta(hours=12))

    with pytest.raises(CoverageError, match="cannot cover every local contract-day hour"):
        local_contract_hours(
            initialization=initialization,
            contract_date=date(2026, 5, 20),
            timezone_name="America/Los_Angeles",
        )


def test_object_key_and_dst_local_day_coverage() -> None:
    assert gfs_object_key(datetime(2026, 5, 20, 6, tzinfo=UTC), 24) == (
        "gfs.20260520/06/atmos/gfs.t06z.pgrb2.0p25.f024"
    )
    spring = local_contract_hours(
        initialization=datetime(2026, 3, 7, 6, tzinfo=UTC),
        contract_date=date(2026, 3, 8),
        timezone_name="America/Los_Angeles",
    )
    autumn = local_contract_hours(
        initialization=datetime(2026, 10, 31, 6, tzinfo=UTC),
        contract_date=date(2026, 11, 1),
        timezone_name="America/Los_Angeles",
    )

    assert len(spring) == 23
    assert len(autumn) == 25


def test_nearest_gfs_grid_and_daily_maximum_are_deterministic() -> None:
    grid = nearest_gfs_grid_point(Decimal("33.93806"), Decimal("-118.38889"))

    assert grid.latitude == Decimal("34.00")
    assert grid.longitude == Decimal("-118.50")
    assert grid.distance_km == Decimal("12.346")
    assert daily_maximum((Decimal("72.0"), Decimal("80.1"), Decimal("79.9"))) == Decimal("80.1")


def test_index_range_rejects_duplicates_and_missing_next_offset() -> None:
    records = parse_gfs_index(
        b"1:0:d=2026052006:PRMSL:mean sea level:anl:\n"
        b"2:100:d=2026052006:TMP:2 m above ground:24 hour fcst:\n"
        b"3:250:d=2026052006:RH:2 m above ground:24 hour fcst:\n"
    )
    assert temperature_byte_range(records) == ByteRange(start=100, end=249)

    with pytest.raises(FeasibilityError, match="ambiguous"):
        temperature_byte_range(
            parse_gfs_index(
                b"1:0:d=2026052006:TMP:2 m above ground:24 hour fcst:\n"
                b"2:100:d=2026052006:TMP:2 m above ground:24 hour fcst:\n"
                b"3:250:d=2026052006:RH:2 m above ground:24 hour fcst:\n"
            )
        )
    with pytest.raises(FeasibilityError, match="next offset"):
        temperature_byte_range(
            parse_gfs_index(b"1:0:d=2026052006:TMP:2 m above ground:24 hour fcst:\n")
        )


def test_request_and_download_bounds_reject_before_overrun() -> None:
    budget = RequestBudget(maximum_requests=2, maximum_bytes=9)
    budget.record_request()
    budget.record_request()
    with pytest.raises(BudgetError):
        budget.record_request()
    budget.record_bytes(9)
    with pytest.raises(BudgetError):
        budget.record_bytes(1)


class _Response:
    def __init__(self, *, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _: int | None = None) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status


def test_unsigned_http_range_and_download_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Any] = []

    def fake_urlopen(request: Any, timeout: int) -> _Response:
        requests.append((request, timeout))
        if request.headers.get("Range"):
            return _Response(
                status=206,
                headers={
                    "Content-Length": "4",
                    "Content-Range": "bytes 100-103/500",
                    "ETag": "range",
                },
                body=b"GRIB",
            )
        return _Response(status=200, headers={"Content-Length": "3", "ETag": "index"}, body=b"idx")

    monkeypatch.setattr(archive, "urlopen", fake_urlopen)
    client = NoaaGfsArchive(
        budget=RequestBudget(maximum_requests=2, maximum_bytes=7), timeout_seconds=1
    )
    _, index = client.index("gfs.20260520/06/atmos/gfs.t06z.pgrb2.0p25.f024")
    _, field = client.field("gfs.20260520/06/atmos/gfs.t06z.pgrb2.0p25.f024", ByteRange(100, 103))

    assert index.sha256 == "c66a8eb7e6a7e395bf000e8884c7de131a5cf32a9f42de44a1b9ceaf47c8900e"
    assert field.sha256 == "1ed0532076444a48a796ea8edeb377ad0d4180936bc47d97fd199a5216b16f11"
    assert requests[0][0].headers.get("Authorization") is None
    assert requests[1][0].headers["Range"] == "bytes=100-103"
    assert client.budget.requests == 2
    assert client.budget.downloaded_bytes == 7


def test_research_package_has_no_production_or_postgres_imports() -> None:
    prohibited = {"apex", "alembic", "psycopg", "sqlalchemy"}
    for source in PACKAGE.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
        assert all(name.split(".", maxsplit=1)[0] not in prohibited for name in imported)


def test_compact_report_keeps_manifest_details_out_of_stdout() -> None:
    snapshot = SnapshotResult(
        contract_date=date(2026, 5, 20),
        lead_hours=48,
        as_of=datetime(2026, 5, 19, 7, 59, tzinfo=UTC),
        initialization=datetime(2026, 5, 19, 6, tzinfo=UTC),
        complete=True,
        reason=None,
        fields=(),
        daily_maximum=Decimal("74.51"),
    )
    report: dict[str, object] = {
        "classification": "GFS_ARCHIVE_PARTIALLY_FEASIBLE",
        "binding": {
            "requested_series": "KXTEMPLAXH",
            "selected_series": "KXHIGHLAX",
            "configuration_hash": "binding-hash",
        },
        "selected_events": [
            {
                "contract_date": "2026-05-20",
                "ticker": "KXHIGHLAX-26MAY20-B69.5",
                "event_ticker": "KXHIGHLAX-26MAY20",
                "rule_sha256": "rule-hash",
                "rule": "retained in the local manifest only",
            }
        ],
        "grid": {"method": "NEAREST_GRID_POINT"},
        "bounds": {"requests_used": 480},
        "local_report": ".local/research/weather_gfs_feasibility/manifest.json",
    }

    compact = _compact_report(report=report, snapshots=(snapshot,))

    assert compact["selected_events"] == [
        {
            "date": "2026-05-20",
            "ticker": "KXHIGHLAX-26MAY20-B69.5",
            "event_ticker": "KXHIGHLAX-26MAY20",
            "rule_sha256": "rule-hash",
        }
    ]
    assert compact["snapshots"] == [
        {
            "contract_date": date(2026, 5, 20),
            "lead_hours": 48,
            "as_of": "2026-05-19T07:59:00Z",
            "initialization": "2026-05-19T06:00:00Z",
            "complete": True,
            "reason": None,
            "field_count": 0,
            "daily_maximum": Decimal("74.51"),
        }
    ]
