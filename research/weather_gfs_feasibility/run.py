"""CLI for the bounded GFS archive feasibility spike and its offline fixture."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from research.weather_gfs_feasibility.archive import (
    NoaaGfsArchive,
    SnapshotResult,
    reconstruct_daily_maximum,
)
from research.weather_gfs_feasibility.core import (
    BudgetError,
    CoverageError,
    FeasibilityError,
    RequestBudget,
    binding_configuration,
    compact_json,
    daily_maximum,
    gfs_object_key,
    latest_gfs_initialization,
    local_contract_hours,
    nearest_gfs_grid_point,
    parse_gfs_index,
    sha256_hex,
    temperature_byte_range,
)
from research.weather_gfs_feasibility.kalshi import (
    KalshiPublicEvidence,
    MarketEvidence,
    SeriesResolution,
    select_spanning_events,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIGURATION_PATH = Path(__file__).with_name("binding.json")
LOCAL_OUTPUT_ROOT = ROOT / ".local" / "research" / "weather_gfs_feasibility"


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_leads(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item) for item in raw.split(",") if item)
    except ValueError as error:
        raise argparse.ArgumentTypeError("lead hours must be comma-separated integers") from error
    if values != (48, 24, 12):
        raise argparse.ArgumentTypeError("lead hours must be exactly 48,24,12 in that order")
    return values


def _write_local_report(report: dict[str, object]) -> Path:
    run_name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = LOCAL_OUTPUT_ROOT / run_name
    target.mkdir(parents=True, exist_ok=False)
    report_path = target / "feasibility-report.json"
    report_path.write_text(compact_json(report) + "\n", encoding="utf-8")
    return report_path


def _binding_record(
    configuration: dict[str, object], resolution: SeriesResolution
) -> dict[str, object]:
    return {
        "requested_series": resolution.requested_series,
        "selected_series": resolution.selected_series,
        "fallback_reason": resolution.fallback_reason,
        "configuration_hash": configuration["canonical_configuration_hash"],
        "configuration_version": configuration["binding_version"],
        "rule_identity": configuration["rule_identity"],
        "contract_terms": configuration["contract_terms"],
        "station": configuration["station"],
        "latitude": configuration["latitude"],
        "longitude": configuration["longitude"],
        "timezone": configuration["timezone"],
        "temperature_unit": configuration["temperature_unit"],
        "contract_type": configuration["contract_type"],
        "measurement_period": configuration["measurement_period"],
        "settlement_location_notes": configuration["settlement_location_notes"],
        "kalshi_metadata_hashes": resolution.source_response_hashes,
    }


def _source_facts() -> dict[str, object]:
    return {
        "bucket": "noaa-gfs-bdp-pds",
        "host": "noaa-gfs-bdp-pds.s3.amazonaws.com",
        "access": (
            "unsigned public HTTPS only; no AWS account, credentials, signing, or private endpoint"
        ),
        "cycles_utc": ["00Z", "06Z", "12Z", "18Z"],
        "product": "gfs.tHHz.pgrb2.0p25.fFFF with .idx sidecar; TMP:2 m above ground",
        "license": (
            "NOAA NODD public-use data; retain source attribution and do not imply endorsement"
        ),
        "documentation": [
            "https://aws.amazon.com/marketplace/pp/prodview-hok7o2o24ktfi",
            "https://www.ncei.noaa.gov/products/ncei-data-noaa-open-dissemination-program",
            "https://www.nco.ncep.noaa.gov/pmb/products/gfs/",
        ],
    }


def _snapshot_record(snapshot: SnapshotResult) -> dict[str, object]:
    decoded = json.loads(compact_json(snapshot))
    if not isinstance(decoded, dict):
        raise FeasibilityError("snapshot serialization must be a JSON object")
    return {str(key): value for key, value in decoded.items()}


def _compact_report(
    *, report: dict[str, object], snapshots: tuple[SnapshotResult, ...]
) -> dict[str, object]:
    binding = report["binding"]
    selected_events = report["selected_events"]
    if not isinstance(binding, dict) or not isinstance(selected_events, list):
        raise FeasibilityError("full feasibility report has an invalid compact-summary shape")
    compact_events: list[dict[str, object]] = []
    for event in selected_events:
        if not isinstance(event, dict):
            raise FeasibilityError("full feasibility report has an invalid selected event")
        compact_events.append(
            {
                "date": event["contract_date"],
                "ticker": event["ticker"],
                "event_ticker": event["event_ticker"],
                "rule_sha256": event["rule_sha256"],
            }
        )
    return {
        "classification": report["classification"],
        "binding": {
            "requested_series": binding["requested_series"],
            "selected_series": binding["selected_series"],
            "configuration_hash": binding["configuration_hash"],
        },
        "selected_events": compact_events,
        "grid": report["grid"],
        "snapshots": [
            {
                "contract_date": snapshot.contract_date,
                "lead_hours": snapshot.lead_hours,
                "as_of": _timestamp(snapshot.as_of),
                "initialization": _timestamp(snapshot.initialization),
                "complete": snapshot.complete,
                "reason": None
                if snapshot.reason is None
                else snapshot.reason.split(":", maxsplit=1)[0],
                "field_count": len(snapshot.fields),
                "daily_maximum": snapshot.daily_maximum,
            }
            for snapshot in snapshots
        ],
        "bounds": report["bounds"],
        "local_report": report["local_report"],
    }


def _classification(
    *,
    events: tuple[MarketEvidence, ...],
    snapshots: tuple[SnapshotResult, ...],
) -> str:
    if not events:
        return "GFS_ARCHIVE_NOT_FEASIBLE"
    complete = sum(snapshot.complete for snapshot in snapshots)
    required = len(events) * 3
    complete_dates = {snapshot.contract_date for snapshot in snapshots if snapshot.complete}
    if len(events) >= 5 and complete >= 17 and len(complete_dates) >= 5:
        return "GFS_ARCHIVE_FEASIBLE"
    if any(snapshot.fields for snapshot in snapshots):
        return "GFS_ARCHIVE_PARTIALLY_FEASIBLE"
    if any(snapshot.reason and "NOAA archive" in snapshot.reason for snapshot in snapshots):
        return "GFS_ARCHIVE_NOT_FEASIBLE"
    if required and any(snapshot.reason for snapshot in snapshots):
        return "GFS_ARCHIVE_PARTIALLY_FEASIBLE"
    return "GFS_ARCHIVE_NOT_FEASIBLE"


def run_real(
    *,
    series: str,
    sample_dates: int,
    lead_hours: tuple[int, ...],
    maximum_requests: int,
    maximum_download_mb: int,
    timeout_seconds: int,
) -> dict[str, object]:
    if not 1 <= sample_dates <= 6:
        raise FeasibilityError("sample dates must be between 1 and 6")
    if maximum_requests <= 0 or maximum_download_mb <= 0:
        raise FeasibilityError("request and download bounds must be positive")
    configuration = binding_configuration(CONFIGURATION_PATH)
    kalshi = KalshiPublicEvidence(timeout_seconds=timeout_seconds)
    resolution = kalshi.resolve_daily_high_binding(
        requested_series=series,
        configuration=configuration,
    )
    if resolution.selected_series is None:
        blocker_report: dict[str, object] = {
            "classification": "KALSHI_LOCATION_BINDING_BLOCKER",
            "binding": _binding_record(configuration, resolution),
            "reason": resolution.fallback_reason,
            "source_facts": _source_facts(),
        }
        blocker_report["local_report"] = str(_write_local_report(blocker_report))
        print(compact_json(blocker_report))
        return blocker_report
    events = kalshi.historical_daily_high_events(series_ticker=resolution.selected_series)
    selected_events = select_spanning_events(events, sample_dates)
    binding = _binding_record(configuration, resolution)
    grid = nearest_gfs_grid_point(
        Decimal(str(configuration["latitude"])),
        Decimal(str(configuration["longitude"])),
    )
    print("REVIEWED_BINDING=" + compact_json(binding))
    print("SELECTED_GRID=" + compact_json(grid))
    for event in selected_events:
        print(
            "SELECTED_EVENT="
            + compact_json(
                {
                    "date": event.contract_date,
                    "ticker": event.ticker,
                    "event_ticker": event.event_ticker,
                    "close_time": event.close_time,
                    "rule_sha256": event.rule_sha256,
                }
            )
        )
    budget = RequestBudget(
        maximum_requests=maximum_requests,
        maximum_bytes=maximum_download_mb * 1024 * 1024,
    )
    archive = NoaaGfsArchive(budget=budget, timeout_seconds=timeout_seconds)
    snapshots: list[SnapshotResult] = []
    for event in selected_events:
        close_time = datetime.fromisoformat(event.close_time.replace("Z", "+00:00"))
        for lead in lead_hours:
            as_of = close_time.astimezone(UTC) - timedelta(hours=lead)
            initialization = latest_gfs_initialization(as_of)
            print(
                "SNAPSHOT_PLAN="
                + compact_json(
                    {
                        "date": event.contract_date,
                        "lead_hours": lead,
                        "as_of": as_of,
                        "initialization": initialization,
                        "requests_used_before": budget.requests,
                    }
                )
            )
            result = reconstruct_daily_maximum(
                archive=archive,
                contract_date=event.contract_date,
                close_time=close_time,
                lead_hours=lead,
                timezone_name=str(configuration["timezone"]),
                grid_point=grid,
                output_unit=str(configuration["temperature_unit"]),
            )
            snapshots.append(result)
            print(
                "SNAPSHOT_RESULT="
                + compact_json(
                    {
                        "date": result.contract_date,
                        "lead_hours": result.lead_hours,
                        "complete": result.complete,
                        "reason": result.reason,
                        "daily_maximum": result.daily_maximum,
                        "fields": len(result.fields),
                        "requests_used": budget.requests,
                    }
                )
            )
    snapshot_tuple = tuple(snapshots)
    report: dict[str, object] = {
        "classification": _classification(events=selected_events, snapshots=snapshot_tuple),
        "source_facts": _source_facts(),
        "binding": binding,
        "selected_events": [json.loads(compact_json(event)) for event in selected_events],
        "grid": json.loads(compact_json(grid)),
        "snapshots": [_snapshot_record(snapshot) for snapshot in snapshot_tuple],
        "bounds": {
            "sample_dates": sample_dates,
            "lead_hours": list(lead_hours),
            "maximum_requests": maximum_requests,
            "maximum_download_mb": maximum_download_mb,
            "requests_used": budget.requests,
            "downloaded_bytes": budget.downloaded_bytes,
        },
    }
    report["local_report"] = str(_write_local_report(report))
    print(
        "FEASIBILITY_REPORT="
        + compact_json(_compact_report(report=report, snapshots=snapshot_tuple))
    )
    return report


def run_fixture() -> dict[str, object]:
    configuration = binding_configuration(CONFIGURATION_PATH)
    as_of = datetime(2026, 5, 20, 7, 59, tzinfo=UTC)
    assert latest_gfs_initialization(as_of) == datetime(2026, 5, 20, 6, tzinfo=UTC)
    assert latest_gfs_initialization(datetime(2026, 5, 20, 6, tzinfo=UTC)) == datetime(
        2026, 5, 20, 6, tzinfo=UTC
    )
    assert gfs_object_key(datetime(2026, 5, 20, 6, tzinfo=UTC), 24) == (
        "gfs.20260520/06/atmos/gfs.t06z.pgrb2.0p25.f024"
    )
    spring_hours = local_contract_hours(
        initialization=datetime(2026, 3, 7, 6, tzinfo=UTC),
        contract_date=datetime(2026, 3, 8, tzinfo=UTC).date(),
        timezone_name="America/Los_Angeles",
    )
    fall_hours = local_contract_hours(
        initialization=datetime(2026, 10, 31, 6, tzinfo=UTC),
        contract_date=datetime(2026, 11, 1, tzinfo=UTC).date(),
        timezone_name="America/Los_Angeles",
    )
    assert len(spring_hours) == 23
    assert len(fall_hours) == 25
    try:
        local_contract_hours(
            initialization=datetime(2026, 5, 20, 12, tzinfo=UTC),
            contract_date=datetime(2026, 5, 20, tzinfo=UTC).date(),
            timezone_name="America/Los_Angeles",
        )
    except CoverageError:
        pass
    else:
        raise AssertionError("latest later cycle must not silently fill earlier local hours")
    grid = nearest_gfs_grid_point(Decimal("33.93806"), Decimal("-118.38889"))
    assert grid.method == "NEAREST_GRID_POINT"
    assert grid.latitude == Decimal("34.00")
    assert grid.longitude == Decimal("-118.50")
    raw_index = (
        b"1:0:d=2026052006:PRMSL:mean sea level:anl:\n"
        b"2:100:d=2026052006:TMP:2 m above ground:24 hour fcst:\n"
        b"3:250:d=2026052006:RH:2 m above ground:24 hour fcst:\n"
    )
    selected_range = temperature_byte_range(parse_gfs_index(raw_index))
    assert selected_range.start == 100 and selected_range.end == 249
    try:
        temperature_byte_range(
            parse_gfs_index(raw_index.replace(b"RH:2 m above ground", b"TMP:2 m above ground"))
        )
    except FeasibilityError:
        pass
    else:
        raise AssertionError("duplicate temperature index entries must be rejected")
    try:
        temperature_byte_range(
            parse_gfs_index(b"1:0:d=2026052006:TMP:2 m above ground:24 hour fcst:\n")
        )
    except FeasibilityError:
        pass
    else:
        raise AssertionError("an unbounded final index record must be rejected")
    assert daily_maximum((Decimal("71.2"), Decimal("75.0"), Decimal("73.4"))) == Decimal("75.0")
    assert sha256_hex(b"synthetic-grib-derived-temperature") == (
        "322c9343af485ed1303815feb1fbf1221d9b3c32bb46da72d056e50701d6aeb5"
    )
    budget = RequestBudget(maximum_requests=2, maximum_bytes=100)
    budget.record_request()
    budget.record_request()
    try:
        budget.record_request()
    except BudgetError:
        pass
    else:
        raise AssertionError("request bound must reject a third request")
    ignored_probe = ".local/research/weather_gfs_feasibility/fixture/probe.json"
    check_ignore = subprocess.run(
        ["git", "check-ignore", "--quiet", ignored_probe],
        cwd=ROOT,
        check=False,
    )
    if check_ignore.returncode != 0:
        raise AssertionError("generated research artifacts are not ignored")
    prohibited = {"apex", "alembic", "psycopg", "sqlalchemy"}
    for path in Path(__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
        if any(name.split(".", maxsplit=1)[0] in prohibited for name in imported):
            raise AssertionError("research package imports a production service or PostgreSQL")
    result = {
        "fixture": "GFS_ARCHIVE_FEASIBILITY_FIXTURE_PASSED",
        "configuration_hash": configuration["canonical_configuration_hash"],
        "dst_hours": {"spring": len(spring_hours), "fall": len(fall_hours)},
        "grid": json.loads(compact_json(grid)),
        "range": json.loads(compact_json(selected_range)),
    }
    print(compact_json(result))
    return result


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        description="bounded NOAA GFS archive feasibility research"
    )
    subparsers = command_parser.add_subparsers(dest="command", required=True)
    real = subparsers.add_parser("real")
    real.add_argument("--series", required=True)
    real.add_argument("--sample-dates", type=int, required=True)
    real.add_argument("--lead-hours", type=_parse_leads, required=True)
    real.add_argument("--max-requests", type=int, required=True)
    real.add_argument("--max-download-mb", type=int, required=True)
    real.add_argument("--timeout-seconds", type=int, default=10)
    subparsers.add_parser("fixture")
    return command_parser


def main() -> None:
    arguments = parser().parse_args()
    if arguments.command == "fixture":
        run_fixture()
        return
    run_real(
        series=arguments.series,
        sample_dates=arguments.sample_dates,
        lead_hours=arguments.lead_hours,
        maximum_requests=arguments.max_requests,
        maximum_download_mb=arguments.max_download_mb,
        timeout_seconds=arguments.timeout_seconds,
    )


if __name__ == "__main__":
    main()
