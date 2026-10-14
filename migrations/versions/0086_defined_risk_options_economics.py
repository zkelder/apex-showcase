"""Defined-Risk Options Structure Economics and Research Integration v1.

Seven new, narrowly-scoped, immutable tables for genuine defined-risk
options structure research evidence -- built strictly on top of the
already-retained genuine quote-chain evidence from ``0085_options_quote_
intelligence`` (``options_chain_snapshot``, ``options_quote_observation``),
never re-deriving a second option-contract identity model or a second
quote-chain capture pipeline:

* ``options_fee_policy`` -- a declared, versioned per-contract fee
  assumption (``domain.options_structures.OptionsFeePolicy``). Retains
  whether the fee is a genuinely evidenced provider rate or an explicitly
  labeled ``FEE_EVIDENCE_INCOMPLETE`` conservative research assumption --
  zero fees are never used silently.
* ``options_slippage_policy`` -- conservative structure slippage
  methodology, derived from quote evidence
  (``domain.options_structures.OptionsSlippagePolicy``).
* ``structure_pricing_policy`` -- the declared side-aware conservative
  entry-price convention (``domain.options_structures.
  StructurePricingPolicy``): buy the long leg at ask, sell the short leg
  at bid; midpoint is descriptive only and never executable.
* ``options_structure_research_policy`` -- one versioned, bounded policy
  declaring supported structure types, DTE/moneyness bands, minimum quote
  quality, maximum relative spread/fee burden/capital required, and
  candidate-generation bounds, referencing exactly one fee/slippage/
  pricing policy row each (``domain.options_structures.
  OptionsStructureResearchPolicy``). Declared before any evaluation result
  exists.
* ``option_structure_candidate`` -- one immutable retained defined-risk
  structure (long call vertical, long put vertical, short call credit
  vertical, or short put credit vertical) binding both legs' exact
  contract and quote-observation identity, its source chain snapshot, and
  the three cost-assumption policies that produced its economics
  (``domain.options_structures.OptionStructureCandidate``). Structure
  identity is deliberately narrower than full content -- changing either
  leg, quote evidence, pricing policy, or cost assumption creates a
  different ``structure_id`` -- mirroring ``options_chain_snapshot.
  snapshot_id``'s identical narrower-than-content design one migration
  earlier.
* ``options_structure_research_snapshot`` -- the immutable retained
  research snapshot per underlying/capture: policy, source chain snapshot,
  structures considered/rejected/retained, and limitations, all
  content-addressed so exact replay from the identical evidence/policy is
  a durable no-op rather than a duplicate row.
* ``options_structure_research_feature`` -- the flat cross-market
  research-seam projection (one row per retained structure): normalized
  debit/credit, width, max profit/loss, breakeven distance, net Greeks,
  IV/skew/term context, liquidity/cost burden, and quality label. Feature
  evidence only -- no hypothesis or Opportunity is generated here.

Every table is immutable (``apex.adapters.postgres.immutable_trigger_ddl``)
and ``authority``-checked. Downgrade refuses whenever any evidence is
retained. No order, position, reservation, PAPER, or LIVE-authority column
exists anywhere in this migration; nothing here submits an options order,
creates a PAPER order, or creates a Shared Opportunity.

Revision ID: 0086_defined_risk_options_eco
Revises: 0085_options_quote_intelligence

Note: the revision id is a shortened form of this file's descriptive name
(``alembic_version.version_num`` is ``VARCHAR(32)``; the full descriptive
name is 35 characters). Abbreviating the revision id while keeping a fully
descriptive filename already has precedent in this migration history --
e.g. ``0010_public_orderbook_complement_scanning.py`` has ``revision =
"0010_public_orderbook_complement"``, and ``0050_crypto_strategy_evidence_
dossier.py`` has ``revision = "0050_crypto_dossier"``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0086_defined_risk_options_eco"
down_revision: str | None = "0085_options_quote_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "options_fee_policy",
    "options_slippage_policy",
    "structure_pricing_policy",
    "options_structure_research_policy",
    "option_structure_candidate",
    "options_structure_research_snapshot",
    "options_structure_research_feature",
)
ALL_TABLES = IMMUTABLE_TABLES

_STRUCTURE_TYPES = (
    "'CALL_DEBIT_VERTICAL','PUT_DEBIT_VERTICAL','CALL_CREDIT_VERTICAL','PUT_CREDIT_VERTICAL'"
)


def upgrade() -> None:
    op.create_table(
        "options_fee_policy",
        sa.Column("policy_id", sa.String(160), primary_key=True),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column("per_contract_fee", sa.Numeric(18, 8), nullable=False),
        sa.Column("fee_evidence_status", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("per_contract_fee >= 0", name="ck_ofp_fee_non_negative"),
        sa.CheckConstraint(
            "fee_evidence_status IN ('EVIDENCED','FEE_EVIDENCE_INCOMPLETE')",
            name="ck_ofp_evidence_status",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_ofp_authority"),
    )

    op.create_table(
        "options_slippage_policy",
        sa.Column("policy_id", sa.String(160), primary_key=True),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column("methodology", sa.String(80), nullable=False),
        sa.Column("adverse_fraction_of_spread", sa.Numeric(18, 8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "adverse_fraction_of_spread >= 0 AND adverse_fraction_of_spread <= 1",
            name="ck_osp_fraction_range",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_osp_authority"),
    )

    op.create_table(
        "structure_pricing_policy",
        sa.Column("policy_id", sa.String(160), primary_key=True),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_spp_authority"),
    )

    op.create_table(
        "options_structure_research_policy",
        sa.Column("policy_id", sa.String(160), primary_key=True),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column("dte_minimum", sa.Integer(), nullable=False),
        sa.Column("dte_maximum", sa.Integer(), nullable=False),
        sa.Column("maximum_structures_per_underlying", sa.Integer(), nullable=False),
        sa.Column("maximum_total_structures", sa.Integer(), nullable=False),
        sa.Column(
            "pricing_policy_id",
            sa.String(160),
            sa.ForeignKey("structure_pricing_policy.policy_id"),
            nullable=False,
        ),
        sa.Column(
            "fee_policy_id",
            sa.String(160),
            sa.ForeignKey("options_fee_policy.policy_id"),
            nullable=False,
        ),
        sa.Column(
            "slippage_policy_id",
            sa.String(160),
            sa.ForeignKey("options_slippage_policy.policy_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "dte_minimum >= 0 AND dte_minimum < dte_maximum AND dte_maximum <= 730",
            name="ck_osrp_dte_range",
        ),
        sa.CheckConstraint(
            "maximum_structures_per_underlying >= 1 AND maximum_structures_per_underlying <= 500",
            name="ck_osrp_max_per_underlying",
        ),
        sa.CheckConstraint(
            "maximum_total_structures >= maximum_structures_per_underlying "
            "AND maximum_total_structures <= 5000",
            name="ck_osrp_max_total",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_osrp_authority"),
    )

    op.create_table(
        "option_structure_candidate",
        sa.Column("structure_id", sa.String(160), primary_key=True),
        sa.Column("structure_type", sa.String(40), nullable=False),
        sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
        sa.Column("expiration", sa.String(10), nullable=False),
        sa.Column(
            "structure_policy_id",
            sa.String(160),
            sa.ForeignKey("options_structure_research_policy.policy_id"),
            nullable=False,
        ),
        sa.Column(
            "source_chain_snapshot_id",
            sa.String(160),
            sa.ForeignKey("options_chain_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("long_canonical_contract_id", sa.String(160), nullable=False),
        sa.Column("short_canonical_contract_id", sa.String(160), nullable=False),
        sa.Column(
            "long_observation_id",
            sa.String(160),
            sa.ForeignKey("options_quote_observation.observation_id"),
            nullable=False,
        ),
        sa.Column(
            "short_observation_id",
            sa.String(160),
            sa.ForeignKey("options_quote_observation.observation_id"),
            nullable=False,
        ),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(f"structure_type IN ({_STRUCTURE_TYPES})", name="ck_osc_structure_type"),
        sa.CheckConstraint(
            "long_canonical_contract_id <> short_canonical_contract_id", name="ck_osc_distinct_legs"
        ),
        sa.CheckConstraint(
            "long_observation_id <> short_observation_id", name="ck_osc_distinct_observations"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_osc_authority"),
    )
    op.create_index(
        "ix_osc_underlying_expiration",
        "option_structure_candidate",
        ["underlying_canonical_security_id", "expiration"],
    )
    op.create_index(
        "ix_osc_structure_policy", "option_structure_candidate", ["structure_policy_id"]
    )
    op.create_index(
        "ix_osc_source_snapshot", "option_structure_candidate", ["source_chain_snapshot_id"]
    )

    op.create_table(
        "options_structure_research_snapshot",
        sa.Column("research_snapshot_id", sa.String(160), primary_key=True),
        sa.Column(
            "structure_policy_id",
            sa.String(160),
            sa.ForeignKey("options_structure_research_policy.policy_id"),
            nullable=False,
        ),
        sa.Column(
            "source_chain_snapshot_id",
            sa.String(160),
            sa.ForeignKey("options_chain_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_ossnap_authority"),
    )
    op.create_index(
        "ix_ossnap_underlying_evaluated",
        "options_structure_research_snapshot",
        ["underlying_canonical_security_id", "evaluated_at"],
    )
    op.create_index(
        "ix_ossnap_source_snapshot",
        "options_structure_research_snapshot",
        ["source_chain_snapshot_id"],
    )

    op.create_table(
        "options_structure_research_feature",
        sa.Column("feature_id", sa.String(160), primary_key=True),
        sa.Column(
            "structure_id",
            sa.String(160),
            sa.ForeignKey("option_structure_candidate.structure_id"),
            nullable=False,
        ),
        sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
        sa.Column("as_of", sa.String(10), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("structure_id", name="uq_osfeature_structure"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_osfeature_authority"),
    )
    op.create_index(
        "ix_osfeature_underlying_as_of",
        "options_structure_research_feature",
        ["underlying_canonical_security_id", "as_of"],
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0086 with retained defined-risk options structure evidence'; "
            "END IF; END $$"
        )
    )
    for table in reversed(IMMUTABLE_TABLES):
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("options_structure_research_feature")
    op.drop_table("options_structure_research_snapshot")
    op.drop_table("option_structure_candidate")
    op.drop_table("options_structure_research_policy")
    op.drop_table("structure_pricing_policy")
    op.drop_table("options_slippage_policy")
    op.drop_table("options_fee_policy")
