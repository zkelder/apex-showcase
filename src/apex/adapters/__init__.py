"""Offline adapters for the Apex golden simulation slice."""

from apex.adapters.canonical_report import CanonicalReportRenderer, report_document, report_sha256
from apex.adapters.fixtures import (
    REGISTERED_FIXTURE_IDS,
    DecisionFixture,
    FixtureError,
    FixtureFormatError,
    FixtureIntegrityError,
    FixtureNotAllowedError,
    FixtureReader,
    SettlementFixture,
)
from apex.adapters.simulator import DeterministicSimulator

__all__ = [
    "REGISTERED_FIXTURE_IDS",
    "CanonicalReportRenderer",
    "DecisionFixture",
    "DeterministicSimulator",
    "FixtureError",
    "FixtureFormatError",
    "FixtureIntegrityError",
    "FixtureNotAllowedError",
    "FixtureReader",
    "SettlementFixture",
    "report_document",
    "report_sha256",
]
