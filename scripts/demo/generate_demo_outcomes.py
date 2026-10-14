"""Generate the static demo's deterministic-outcomes data from Apex's real offline demo.

Runs ``scripts/dev.py demo-core`` for each of the eight fixture outcomes with the
documented seed, captures the canonical report and its hash, and writes one JSON file
for the static demo. Output is byte-stable: the script runs every fixture twice and
refuses to write if any report differs between runs.

Usage:  uv run python scripts/demo/generate_demo_outcomes.py [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED = "20260716"
CAPITAL = "1000.00"
FIXTURES: tuple[tuple[str, str], ...] = (
    ("golden", "Settled trade"),
    ("no-proposal", "No proposal"),
    ("policy-rejected", "Rejected by allocation policy"),
    ("invariant-rejected", "Rejected by the risk kernel"),
    ("unfilled-expired", "Resting order expires unfilled"),
    ("partial-fill-canceled", "Partial fill, remainder canceled"),
    ("partial-fill-canceled-settled-no", "Partial fill, settled NO"),
    ("partial-fill-canceled-settled-void", "Partial fill, settled VOID"),
)


def run_fixture(fixture: str) -> tuple[dict[str, object], str]:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "dev.py"),
            "demo-core",
            "--fixture",
            fixture,
            "--capital",
            CAPITAL,
            "--seed",
            SEED,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout.strip().splitlines()[0])
    # The canonical report hash is printed on stderr by the demo command.
    return report, completed.stderr.strip().splitlines()[-1].strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "frontend" / "public" / "demo-data" / "fixture-outcomes.json",
    )
    arguments = parser.parse_args()
    outcomes: list[dict[str, object]] = []
    for fixture, title in FIXTURES:
        first, first_hash = run_fixture(fixture)
        second, second_hash = run_fixture(fixture)
        if first != second or first_hash != second_hash:
            raise SystemExit(f"fixture {fixture} is not deterministic across runs")
        outcomes.append(
            {"fixture": fixture, "title": title, "report_hash": first_hash, "report": first}
        )
    document = {"seed": int(SEED), "capital": CAPITAL, "outcomes": outcomes}
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    for item in outcomes:
        report = item["report"]
        assert isinstance(report, dict)
        print(f"{item['fixture']:38s} {report.get('run_outcome')}  {str(item['report_hash'])[:16]}")


if __name__ == "__main__":
    main()
