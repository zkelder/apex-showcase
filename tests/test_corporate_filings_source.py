"""Parsing proofs for the bounded SEC ``data.sec.gov`` submissions/
companyfacts clients (Corporate Fundamental, Filing, and Event Intelligence
v1). Pure parsing only -- no database, no server, no network; genuine
capture is exercised separately in the bounded acceptance run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from apex.adapters.corporate_filings_source import (
    MAX_COMPANYFACTS_BYTES,
    MAX_SUBMISSIONS_BYTES,
    SEC_XBRL_DATA_HOST,
    HttpsSecXbrlTransport,
    SecFilingsSourceError,
    SecXbrlFilingsClient,
    parse_sec_companyfacts,
    parse_sec_submissions,
    parse_sec_submissions_page,
)

STABLE_SECURITY_ID = "sec-aapl"
CIK = "320193"


def _submissions_payload(
    *,
    accession_numbers: list[str],
    forms: list[str],
    filing_dates: list[str],
    acceptance: list[str],
    report_dates: list[str] | None = None,
    items: list[str] | None = None,
    primary_documents: list[str] | None = None,
    files: list[dict[str, object]] | None = None,
) -> bytes:
    count = len(accession_numbers)
    payload = {
        "cik": "320193",
        "filings": {
            "recent": {
                "accessionNumber": accession_numbers,
                "filingDate": filing_dates,
                "reportDate": report_dates or [""] * count,
                "acceptanceDateTime": acceptance,
                "form": forms,
                "fileNumber": ["001-36743"] * count,
                "items": items or [""] * count,
                "isXBRL": [1] * count,
                "isInlineXBRL": [1] * count,
                "primaryDocument": primary_documents or ["doc.htm"] * count,
            },
            "files": files or [],
        },
    }
    return json.dumps(payload).encode("utf-8")


def test_parse_sec_submissions_retains_only_bounded_forms_and_builds_source_url() -> None:
    payload = _submissions_payload(
        accession_numbers=[
            "0000320193-24-000123",
            "0000320193-24-000100",
            "0000320193-24-000050",
            "0000320193-99-000001",
        ],
        forms=["10-K", "8-K", "10-Q", "10-K405"],
        filing_dates=["2024-11-01", "2024-08-01", "2024-05-02", "1999-01-01"],
        acceptance=[
            "2024-11-01T16:30:15.000Z",
            "2024-08-01T16:30:15.000Z",
            "2024-05-02T16:30:00.000Z",
            "",
        ],
        report_dates=["2024-09-28", "", "2024-03-30", ""],
        items=["", "2.02,9.01", "", ""],
        files=[{"name": "CIK0000320193-submissions-001.json", "filingCount": 999}],
    )
    result = parse_sec_submissions(payload=payload, stable_security_id=STABLE_SECURITY_ID, cik=CIK)
    assert {item.form.value for item in result.filings} == {"10-K", "8-K", "10-Q"}
    assert result.malformed_entry_count == 0
    assert result.forms_observed == (("10-K", 1), ("10-K405", 1), ("10-Q", 1), ("8-K", 1))
    assert result.additional_page_files == ("CIK0000320193-submissions-001.json",)
    tenk = next(item for item in result.filings if item.form.value == "10-K")
    assert (
        tenk.source_url
        == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/doc.htm"
    )
    eightk = next(item for item in result.filings if item.form.value == "8-K")
    assert eightk.items == ("2.02", "9.01")
    assert result.accepted_at_by_accession["0000320193-24-000123"] == datetime(
        2024, 11, 1, 16, 30, 15, tzinfo=UTC
    )
    assert "0000320193-99-000001" not in result.accepted_at_by_accession


def test_parse_sec_submissions_is_deterministic_for_identical_payload() -> None:
    payload = _submissions_payload(
        accession_numbers=["0000320193-24-000123"],
        forms=["10-K"],
        filing_dates=["2024-11-01"],
        acceptance=["2024-11-01T16:30:15.000Z"],
        report_dates=["2024-09-28"],
    )
    first = parse_sec_submissions(payload=payload, stable_security_id=STABLE_SECURITY_ID, cik=CIK)
    second = parse_sec_submissions(payload=payload, stable_security_id=STABLE_SECURITY_ID, cik=CIK)
    assert first.filings[0].filing_event_id == second.filings[0].filing_event_id


def test_parse_sec_submissions_rejects_cik_mismatch() -> None:
    payload = _submissions_payload(
        accession_numbers=["0000320193-24-000123"],
        forms=["10-K"],
        filing_dates=["2024-11-01"],
        acceptance=["2024-11-01T16:30:15.000Z"],
    )
    with pytest.raises(SecFilingsSourceError):
        parse_sec_submissions(payload=payload, stable_security_id=STABLE_SECURITY_ID, cik="999999")


def test_parse_sec_submissions_column_length_mismatch_is_rejected() -> None:
    malformed = {
        "cik": "320193",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-24-000123", "0000320193-24-000124"],
                "filingDate": ["2024-11-01"],  # deliberately short
                "form": ["10-K", "10-K"],
            },
            "files": [],
        },
    }
    with pytest.raises(SecFilingsSourceError):
        parse_sec_submissions(
            payload=json.dumps(malformed).encode("utf-8"),
            stable_security_id=STABLE_SECURITY_ID,
            cik=CIK,
        )


def test_parse_sec_submissions_rejects_oversized_payload() -> None:
    with pytest.raises(SecFilingsSourceError):
        parse_sec_submissions(
            payload=b"x" * (MAX_SUBMISSIONS_BYTES + 1),
            stable_security_id=STABLE_SECURITY_ID,
            cik=CIK,
        )


def test_parse_sec_submissions_page_shares_the_same_row_shape() -> None:
    page_payload = {
        "accessionNumber": ["0000320193-18-000070"],
        "filingDate": ["2018-11-05"],
        "reportDate": ["2018-09-29"],
        "acceptanceDateTime": ["2018-11-05T18:15:00.000Z"],
        "form": ["10-K"],
        "fileNumber": ["001-36743"],
        "items": [""],
        "isXBRL": [1],
        "isInlineXBRL": [1],
        "primaryDocument": ["aapl-20180929.htm"],
    }
    result = parse_sec_submissions_page(
        payload=json.dumps(page_payload).encode("utf-8"),
        stable_security_id=STABLE_SECURITY_ID,
        cik=CIK,
    )
    assert len(result.filings) == 1
    assert result.additional_page_files == ()


def _companyfacts_payload(entries: dict[str, object]) -> bytes:
    payload = {"cik": 320193, "facts": entries}
    return json.dumps(payload).encode("utf-8")


def test_parse_sec_companyfacts_falls_back_to_filed_only_on_inconsistent_acceptance() -> None:
    """Regression proof from genuine SEC capture acceptance: a real
    accession's submissions-sourced acceptance time was several days
    *before* the same accession's companyfacts-reported ``filed`` date.
    The fact must still be retained (never dropped or crashed on) with an
    honest filed-only point-in-time anchor, not the untrustworthy
    cross-reference."""
    payload = _companyfacts_payload(
        {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "end": "2017-10-19",
                                "val": 120927437,
                                "accn": "0000008858-17-000072",
                                "fy": 2018,
                                "fp": "Q1",
                                "form": "10-Q",
                                "filed": "2017-10-30",
                            }
                        ]
                    }
                }
            }
        }
    )
    result = parse_sec_companyfacts(
        payload=payload,
        stable_security_id=STABLE_SECURITY_ID,
        cik=CIK,
        accepted_at_by_accession={
            "0000008858-17-000072": datetime(2017, 10, 27, 22, 40, 31, tzinfo=UTC)
        },
    )
    assert result.malformed_entry_count == 0
    assert len(result.facts) == 1
    assert result.facts[0].accepted_at is None
    assert result.facts[0].filed_at == datetime(2017, 10, 30, tzinfo=UTC)
    assert result.facts[0].effective_available_at == datetime(2017, 10, 30, tzinfo=UTC)


def test_parse_sec_companyfacts_extracts_only_the_bounded_vocabulary() -> None:
    payload = _companyfacts_payload(
        {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {
                                "start": "2023-10-01",
                                "end": "2024-09-28",
                                "val": 391035000000,
                                "accn": "0000320193-24-000123",
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2024-11-01",
                            }
                        ]
                    }
                },
                # A concept outside the bounded vocabulary -- must never
                # produce a retained fact regardless of how well-formed.
                "AccountsPayableCurrent": {
                    "units": {
                        "USD": [
                            {
                                "end": "2024-09-28",
                                "val": 68960000000,
                                "accn": "0000320193-24-000123",
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2024-11-01",
                            }
                        ]
                    }
                },
            }
        }
    )
    result = parse_sec_companyfacts(
        payload=payload,
        stable_security_id=STABLE_SECURITY_ID,
        cik=CIK,
        accepted_at_by_accession={
            "0000320193-24-000123": datetime(2024, 11, 1, 16, 30, 15, tzinfo=UTC)
        },
    )
    assert len(result.facts) == 1
    assert result.facts[0].concept.value == "REVENUE"
    assert result.facts[0].accepted_at == datetime(2024, 11, 1, 16, 30, 15, tzinfo=UTC)


def test_parse_sec_companyfacts_skips_malformed_entries_without_failing_the_whole_response() -> (
    None
):
    payload = _companyfacts_payload(
        {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "end": "2024-09-28",
                                "val": 364980000000,
                                "accn": "0000320193-24-000123",
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2024-11-01",
                            },
                            {
                                "end": "2024-09-28",
                                "val": "not-a-number",
                                "accn": "0000320193-24-000123",
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2024-11-01",
                            },
                        ]
                    }
                }
            }
        }
    )
    result = parse_sec_companyfacts(
        payload=payload, stable_security_id=STABLE_SECURITY_ID, cik=CIK, accepted_at_by_accession={}
    )
    assert len(result.facts) == 1
    assert result.malformed_entry_count == 1


def test_parse_sec_companyfacts_tracks_unmapped_fiscal_periods() -> None:
    payload = _companyfacts_payload(
        {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {
                                "start": "2024-01-01",
                                "end": "2024-06-30",
                                "val": 1000,
                                "accn": "0000320193-24-000123",
                                "fy": 2024,
                                "fp": "H1",
                                "form": "10-K",
                                "filed": "2024-11-01",
                            }
                        ]
                    }
                }
            }
        }
    )
    result = parse_sec_companyfacts(
        payload=payload, stable_security_id=STABLE_SECURITY_ID, cik=CIK, accepted_at_by_accession={}
    )
    assert result.facts == ()
    assert result.unmapped_fiscal_period_count == 1


def test_parse_sec_companyfacts_counts_entries_reported_under_an_alternate_unit() -> None:
    """Revenue is bounded to ``USD`` (``SEC_UNIT_KEY_BY_FACT_UNIT``); a
    genuine ``USD`` entry is retained as a fact, while a same-tag entry SEC
    reported under a different unit key is never retained as a fact but is
    still counted -- a real "unit mismatch" quality signal (section 17),
    never silently dropped without a trace."""
    payload = _companyfacts_payload(
        {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {
                                "start": "2023-10-01",
                                "end": "2024-09-28",
                                "val": 391035000000,
                                "accn": "0000320193-24-000123",
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2024-11-01",
                            }
                        ],
                        "USD/shares": [
                            {
                                "start": "2023-10-01",
                                "end": "2024-09-28",
                                "val": 1,
                                "accn": "0000320193-24-000123",
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2024-11-01",
                            }
                        ],
                    }
                }
            }
        }
    )
    result = parse_sec_companyfacts(
        payload=payload, stable_security_id=STABLE_SECURITY_ID, cik=CIK, accepted_at_by_accession={}
    )
    assert len(result.facts) == 1
    assert result.facts[0].unit.value == "USD"
    assert result.alternate_unit_entry_count == 1


def test_parse_sec_companyfacts_alternate_unit_count_is_zero_when_none_present() -> None:
    payload = _companyfacts_payload(
        {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "end": "2024-09-28",
                                "val": 364980000000,
                                "accn": "0000320193-24-000123",
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2024-11-01",
                            }
                        ]
                    }
                }
            }
        }
    )
    result = parse_sec_companyfacts(
        payload=payload, stable_security_id=STABLE_SECURITY_ID, cik=CIK, accepted_at_by_accession={}
    )
    assert len(result.facts) == 1
    assert result.alternate_unit_entry_count == 0


def test_parse_sec_companyfacts_handles_an_issuer_with_no_facts_block_honestly() -> None:
    payload = json.dumps({"cik": 320193, "entityName": "Fund Co"}).encode("utf-8")
    result = parse_sec_companyfacts(
        payload=payload, stable_security_id=STABLE_SECURITY_ID, cik=CIK, accepted_at_by_accession={}
    )
    assert result.facts == ()
    assert result.malformed_entry_count == 0


def test_parse_sec_companyfacts_rejects_oversized_payload() -> None:
    with pytest.raises(SecFilingsSourceError):
        parse_sec_companyfacts(
            payload=b"x" * (MAX_COMPANYFACTS_BYTES + 1),
            stable_security_id=STABLE_SECURITY_ID,
            cik=CIK,
            accepted_at_by_accession={},
        )


def test_parse_sec_companyfacts_rejects_cik_mismatch() -> None:
    payload = _companyfacts_payload({})
    with pytest.raises(SecFilingsSourceError):
        parse_sec_companyfacts(
            payload=payload,
            stable_security_id=STABLE_SECURITY_ID,
            cik="1",
            accepted_at_by_accession={},
        )


def test_transport_refuses_a_non_allowlisted_host_or_path() -> None:
    transport = HttpsSecXbrlTransport()
    with pytest.raises(SecFilingsSourceError):
        transport.get(
            host="example.com",
            path="/submissions/CIK0000320193.json",
            user_agent="Apex/1.0 (test@apex.local)",
            timeout_seconds=5,
            maximum_response_bytes=1000,
        )
    with pytest.raises(SecFilingsSourceError):
        transport.get(
            host=SEC_XBRL_DATA_HOST,
            path="/other/path.json",
            user_agent="Apex/1.0 (test@apex.local)",
            timeout_seconds=5,
            maximum_response_bytes=1000,
        )


def test_client_rejects_an_unidentifying_user_agent() -> None:
    client = SecXbrlFilingsClient()
    with pytest.raises(Exception):  # noqa: B017 - validate_sec_user_agent's own ValueError family
        client.fetch_submissions(cik=CIK, user_agent="no-contact-info")


def test_client_builds_the_documented_zero_padded_cik_paths() -> None:
    captured: dict[str, str] = {}

    class _RecordingTransport:
        def get(
            self,
            *,
            host: str,
            path: str,
            user_agent: str,
            timeout_seconds: int,
            maximum_response_bytes: int,
        ) -> object:
            captured["path"] = path
            raise RuntimeError("stop before any real network attempt")

    client = SecXbrlFilingsClient(transport=_RecordingTransport())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client.fetch_submissions(cik=CIK, user_agent="Apex/1.0 (test@apex.local)")
    assert captured["path"] == "/submissions/CIK0000320193.json"
    with pytest.raises(RuntimeError):
        client.fetch_companyfacts(cik=CIK, user_agent="Apex/1.0 (test@apex.local)")
    assert captured["path"] == "/api/xbrl/companyfacts/CIK0000320193.json"
