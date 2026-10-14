"""Schema proof for external PAPER evidence projections."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.postgres


def test_external_paper_projection_tables_are_retained_separately_from_finance(
    postgres_engine: Engine,
) -> None:
    tables = set(sa.inspect(postgres_engine).get_table_names())
    assert {
        "external_paper_capability_snapshot",
        "external_paper_authorization",
        "external_paper_instruction",
        "external_paper_submission_attempt",
        "external_paper_broker_evidence",
        "external_paper_campaign_observation",
        "external_paper_reconciliation",
        "external_paper_campaign_control",
        "external_paper_validated_fill",
        "external_paper_fill_application",
        "external_paper_internal_execution_binding",
    }.issubset(tables)
