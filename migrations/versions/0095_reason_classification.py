"""Counterfactual Decision Classification & Interpretation Versioning v1.

Separates immutable historical decision truth from evolvable analytical
interpretation of it.  Counterfactual Decision Ledger v1 (migration 0094)
computed ``reason_families`` inline inside ``CounterfactualDecision`` and
folded it into that row's own ``canonical_hash`` -- so correcting a
classification mapping (mapping a previously-unmapped raw reason code, for
example) would change the ``canonical_hash`` of an already-retained,
otherwise-unchanged decision, and the next idempotent resync of that exact
same origin decision would raise ``COUNTERFACTUAL_DECISION_IDENTITY_CONFLICT``
purely because classifier code evolved -- never because the underlying
decision did.  ``domain.counterfactual_ledger.CounterfactualDecision`` no
longer carries ``reason_families`` at all; classification now lives in its
own module (``domain.counterfactual_classification``) and its own table
(``counterfactual_decision_classification``), added here, with an explicit
named/versioned classifier contract (``REASON_CLASSIFIER_V1``/``_V2``, each
fingerprinted by its own ``classifier_policy_hash``).  A classification row
references a decision by ``decision_id`` + ``decision_canonical_hash`` --
the same "reference by id and hash" idiom ``CounterfactualDecision`` itself
already uses for its own origin pointers -- and is itself immutable and
append-only; classifying under a new version always adds a new row,
side-by-side with every earlier version's row for the same decision, never
mutating or replacing one.

This migration does two things to the 521 ``counterfactual_decision`` rows
already retained in every real environment this runs against:

1. Corrects ``canonical_hash``/``payload`` to the new formula -- the exact
   formula ``domain.counterfactual_ledger.decision_canonical_hash``
   implements today, reproduced below as a frozen, migration-local copy (see
   "Historical Reproducibility Hardening" below for why it is a frozen copy
   rather than a live import) -- which drops ``reason_families`` from what
   ``CounterfactualDecision.canonical_hash`` attests to.  Every other field
   -- ``decision_id``, ``origin_decision_id``, ``origin_canonical_hash``,
   ``raw_reason_codes``, ``disposition``, ``evaluated_at``, ``explanation``,
   ... -- is carried through unchanged, byte-for-byte, from each row's own
   already-retained ``payload``; nothing is invented or guessed.
   ``decision_id`` (the stable anchor a future counterfactual-outcome table
   will foreign-key against) was already independent of ``reason_families``
   before this migration and is not touched by it.
2. Explicitly identifies each row's existing (pre-this-migration)
   ``reason_families`` value as classifier v1's own retained output, by
   inserting one ``counterfactual_decision_classification`` row per decision
   tagged ``classifier_version = 'REASON_CLASSIFIER_V1'`` -- sourced
   verbatim from that same already-retained ``payload``, never recomputed
   from scratch and never silently discarded.  This is the "leave v1 rows
   untouched and create versioned interpretation records" path: the 521
   *decision events* stay exactly 521 decision events (no row added, none
   removed, none duplicated in ``counterfactual_decision`` itself); this
   migration only ever adds ``counterfactual_decision_classification`` rows,
   which are a separate, differently-counted concept (Part 11).

The existing 521 rows are protected by the same append-only immutability
trigger every retained Apex row carries (``apex_reject_mutation``) -- as in
migration ``0093_tmc_component_as_of``, this migration disables that trigger
for the narrow, documented purpose of correcting a genuine schema/formula
defect, and re-enables it (in a ``finally``) before returning.  This is not
a precedent for application code to ever update a retained row.

Downgrading this migration refuses while any classification evidence is
retained (mirrors 0094's own downgrade guard).  It deliberately does *not*
revert the ``counterfactual_decision.canonical_hash``/``payload`` correction
even once no classification rows remain: that correction fixes a genuine
defect (canonical content silently depending on evolvable classifier code),
and reintroducing it would not be a safe rollback, it would be reintroducing
the bug this milestone exists to fix.

Historical Reproducibility Hardening (applied after this migration first
shipped and had already run against every real environment; the revision
id, the DDL, and every computed value below are unchanged by this -- see
this change's own commit message and
``tests/test_migration_0095_reproducibility.py`` for the byte-identical
proof).  This migration originally imported
``decision_canonical_hash`` from ``domain.counterfactual_ledger``,
``classification_identity``/``classification_canonical_hash``/
``REASON_CLASSIFIER_V1``/``REASON_CLASSIFIER_V1_POLICY_HASH`` from
``domain.counterfactual_classification``, and ``canonical_json_bytes`` from
``domain.values`` directly -- the only migration among 0001-0095 to import
from ``apex.domain`` at all; every other migration imports only DDL-safe
helpers from ``apex.adapters.postgres`` (see that module's own
``IMMUTABLE_TABLES``/``durable_metadata`` comment -- "keep ... as the exact
revision-N snapshot" -- for the doctrine this restores).  A fresh
``0001 -> head`` chain resolves those imports against whatever
``apex.domain`` looks like *at chain-run time*, not against the code that
existed when this migration was authored; a later, legitimate change to
those live formulas (an added field on ``CounterfactualDecision``, for
instance) could silently change what a fresh chain backfills versus what
already ran here, or break the call outright on a changed signature.  The
private functions/constants immediately below are therefore frozen,
migration-local copies of those exact formulas as they existed when this
migration was authored, narrowed to the types this migration's own backfill
inputs ever produce: ``str``/``int``/``None``/``list``/``dict`` read back
via ``json.loads`` from each row's own already-retained ``payload``, plus
one genuine ``datetime`` this migration creates itself
(``classification_payload["classified_at"]``) -- never ``Decimal``,
``Enum``, or a dataclass.  Never redirect them back to importing
``apex.domain``, and never edit them to track a later domain change: a
correction to live semantics belongs in new application code, and, if
already-retained rows need it too, a new migration with its own frozen copy
-- never a retroactive edit here.

Revision ID: 0095_reason_classification
Revises: 0094_counterfactual_ledger
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0095_reason_classification"
down_revision: str | None = "0094_counterfactual_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COUNTERFACTUAL_CLASSIFICATION_TABLES = ("counterfactual_decision_classification",)

# ---------------------------------------------------------------------------
# Historical Reproducibility Hardening -- frozen, migration-local formulas.
#
# Everything in this section is a byte-for-byte frozen copy of a formula that
# used to be imported live from ``apex.domain`` (see the module docstring).
# It must never be edited to track a later change to the live
# ``domain.counterfactual_ledger``/``domain.counterfactual_classification``/
# ``domain.values`` implementations, and never redirected back to importing
# them: a fresh ``0001 -> head`` chain must keep computing exactly what this
# migration computed the day it was authored, independent of how those
# modules evolve afterwards.  Intentionally narrower than
# ``domain.values.canonical_json_bytes`` -- it supports only the types this
# migration's own backfill inputs ever produce: ``str``/``int``/``bool``/
# ``None``/``list``/``tuple``/``dict`` (with string keys) read back via
# ``json.loads`` from each row's own already-retained ``payload``, plus one
# genuine ``datetime`` -- ``backfilled_at`` below, freshly created by this
# migration itself (not decoded from JSON) for ``classification_payload
# ["classified_at"]``.  Never ``Decimal``, ``Enum``, or a dataclass: nothing
# this migration ever builds carries one.
# ---------------------------------------------------------------------------


def _canonical_timestamp(value: datetime) -> str:
    """Frozen copy of ``domain.values.canonical_timestamp`` exactly as it
    existed when this migration was authored."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    utc_value = value.astimezone(UTC)
    timespec = "microseconds" if utc_value.microsecond else "seconds"
    return utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _canonicalize(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, datetime):
        return _canonical_timestamp(value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("canonical object keys must be strings")
        return {key: _canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonicalize(item) for item in value]
    raise ValueError(f"unsupported canonical value for migration 0095: {type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _content_digest(value: object) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _deterministic_id(namespace: str, value: object) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_]*", namespace) is None:
        raise ValueError("identifier namespace is invalid")
    return f"{namespace}_{_content_digest(value)[:24]}"


_COUNTERFACTUAL_DECISION_V1 = "COUNTERFACTUAL_DECISION_V1"
_COUNTERFACTUAL_DECISION_CLASSIFICATION_V1 = "COUNTERFACTUAL_DECISION_CLASSIFICATION_V1"
_REASON_CLASSIFIER_V1 = "REASON_CLASSIFIER_V1"
# The exact ``REASON_CLASSIFIER_V1_POLICY_HASH`` value at the moment this
# migration was authored: a fixed content hash over classifier v1's mapping
# table, not a formula over any row input, so freezing it is simply
# recording the number rather than reproducing logic.  Cross-checked against
# every already-retained ``REASON_CLASSIFIER_V1`` row's own stored
# ``classifier_policy_hash`` and against ``domain.counterfactual_
# classification.REASON_CLASSIFIER_V1_POLICY_HASH`` at the moment this
# hardening was authored.
_REASON_CLASSIFIER_V1_POLICY_HASH = (
    "0cc48c93151dbf7c27acd8303b8f33e136d6c8ebdb78edcde28490c911db7884"
)


def _decision_canonical_hash(
    *,
    decision_id: str,
    origin: str,
    origin_decision_id: str,
    origin_canonical_hash: str,
    origin_state: str,
    subject_projection_id: str,
    subject_source_record_type: str,
    subject_source_record_id: str,
    venue: str,
    opportunity_set_id: str | None,
    mandate_id: str | None,
    mandate_lineage_id: str | None,
    mandate_version: int | None,
    mandate_hash: str | None,
    portfolio_snapshot_id: str | None,
    portfolio_snapshot_hash: str | None,
    evaluated_at: str,
    disposition: str,
    raw_reason_codes: tuple[str, ...],
    explanation: str,
) -> str:
    """Frozen copy of ``domain.counterfactual_ledger.decision_canonical_hash``
    exactly as it existed when this migration was authored.  See the module
    docstring's "Historical Reproducibility Hardening" section; never edit
    this to track a later change to that function."""

    payload = (
        _COUNTERFACTUAL_DECISION_V1,
        decision_id,
        origin,
        origin_decision_id,
        origin_canonical_hash,
        origin_state,
        subject_projection_id,
        subject_source_record_type,
        subject_source_record_id,
        venue,
        opportunity_set_id,
        mandate_id,
        mandate_lineage_id,
        mandate_version,
        mandate_hash,
        portfolio_snapshot_id,
        portfolio_snapshot_hash,
        evaluated_at,
        disposition,
        raw_reason_codes,
        explanation,
        "NONE",
    )
    return _content_digest(payload)


def _classification_identity(*, decision_id: str, classifier_version: str) -> str:
    """Frozen copy of
    ``domain.counterfactual_classification.classification_identity`` exactly
    as it existed when this migration was authored."""

    return _deterministic_id(
        "counterfactual_decision_classification",
        (_COUNTERFACTUAL_DECISION_CLASSIFICATION_V1, decision_id, classifier_version),
    )


def _classification_canonical_hash(
    *,
    classification_id: str,
    decision_id: str,
    decision_canonical_hash: str,
    classifier_version: str,
    classifier_policy_hash: str,
    reason_families: tuple[str, ...],
) -> str:
    """Frozen copy of
    ``domain.counterfactual_classification.classification_canonical_hash``
    exactly as it existed when this migration was authored."""

    payload = (
        _COUNTERFACTUAL_DECISION_CLASSIFICATION_V1,
        classification_id,
        decision_id,
        decision_canonical_hash,
        classifier_version,
        classifier_policy_hash,
        tuple(reason_families),
    )
    return _content_digest(payload)


def _backfill_v1_classification_and_correct_canonical_hash() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text('SELECT decision_id, payload FROM "counterfactual_decision"')
    ).fetchall()
    if not rows:
        return
    # One fixed timestamp for the whole backfill, not one per row: this is
    # honestly "when this migration first materialized classifier v1's
    # interpretation as its own explicit row," not a reconstructed
    # historical classification moment -- and ``classified_at`` is excluded
    # from ``classification_canonical_hash`` entirely, so this choice
    # affects no retained content, only operator-visible metadata.
    backfilled_at = datetime.now(UTC)
    bind.execute(
        sa.text(
            "ALTER TABLE counterfactual_decision "
            "DISABLE TRIGGER apex_immutable_counterfactual_decision_trigger"
        )
    )
    try:
        for decision_id, payload in rows:
            value = json.loads(payload)
            if "reason_families" not in value:
                # Defensive only: under normal alembic operation this
                # migration runs exactly once per row, so every retained
                # row still has its original v1 ``reason_families`` key at
                # this point.  Skipping (rather than erroring or
                # reprocessing) is the strictly safer response if it is
                # ever re-entered against an already-corrected row.
                continue
            old_reason_families = tuple(str(item) for item in value["reason_families"])
            new_hash = _decision_canonical_hash(
                decision_id=value["decision_id"],
                origin=value["origin"],
                origin_decision_id=value["origin_decision_id"],
                origin_canonical_hash=value["origin_canonical_hash"],
                origin_state=value["origin_state"],
                subject_projection_id=value["subject_projection_id"],
                subject_source_record_type=value["subject_source_record_type"],
                subject_source_record_id=value["subject_source_record_id"],
                venue=value["venue"],
                opportunity_set_id=value["opportunity_set_id"],
                mandate_id=value["mandate_id"],
                mandate_lineage_id=value["mandate_lineage_id"],
                mandate_version=value["mandate_version"],
                mandate_hash=value["mandate_hash"],
                portfolio_snapshot_id=value["portfolio_snapshot_id"],
                portfolio_snapshot_hash=value["portfolio_snapshot_hash"],
                evaluated_at=value["evaluated_at"],
                disposition=value["disposition"],
                raw_reason_codes=tuple(str(item) for item in value["raw_reason_codes"]),
                explanation=value["explanation"],
            )
            new_payload = {key: item for key, item in value.items() if key != "reason_families"}
            new_payload["canonical_hash"] = new_hash
            bind.execute(
                sa.text(
                    "UPDATE counterfactual_decision SET payload = :payload, "
                    "canonical_hash = :canonical_hash WHERE decision_id = :decision_id"
                ),
                {
                    "payload": _canonical_json_bytes(new_payload).decode(),
                    "canonical_hash": new_hash,
                    "decision_id": decision_id,
                },
            )

            classification_id = _classification_identity(
                decision_id=decision_id, classifier_version=_REASON_CLASSIFIER_V1
            )
            classification_hash = _classification_canonical_hash(
                classification_id=classification_id,
                decision_id=decision_id,
                decision_canonical_hash=new_hash,
                classifier_version=_REASON_CLASSIFIER_V1,
                classifier_policy_hash=_REASON_CLASSIFIER_V1_POLICY_HASH,
                reason_families=old_reason_families,
            )
            classification_payload = {
                "classification_id": classification_id,
                "decision_id": decision_id,
                "decision_canonical_hash": new_hash,
                "classifier_version": _REASON_CLASSIFIER_V1,
                "classifier_policy_hash": _REASON_CLASSIFIER_V1_POLICY_HASH,
                "reason_families": list(old_reason_families),
                "classified_at": backfilled_at,
                "canonical_hash": classification_hash,
                "authority": "NONE",
            }
            bind.execute(
                sa.text(
                    "INSERT INTO counterfactual_decision_classification "
                    "(classification_id, decision_id, classifier_version, classified_at, "
                    "payload, canonical_hash, authority) VALUES "
                    "(:classification_id, :decision_id, :classifier_version, :classified_at, "
                    ":payload, :canonical_hash, 'NONE')"
                ),
                {
                    "classification_id": classification_id,
                    "decision_id": decision_id,
                    "classifier_version": _REASON_CLASSIFIER_V1,
                    "classified_at": backfilled_at,
                    "payload": _canonical_json_bytes(classification_payload).decode(),
                    "canonical_hash": classification_hash,
                },
            )
    finally:
        bind.execute(
            sa.text(
                "ALTER TABLE counterfactual_decision "
                "ENABLE TRIGGER apex_immutable_counterfactual_decision_trigger"
            )
        )


def upgrade() -> None:
    op.create_table(
        "counterfactual_decision_classification",
        sa.Column("classification_id", sa.String(300), primary_key=True),
        sa.Column(
            "decision_id",
            sa.String(300),
            sa.ForeignKey("counterfactual_decision.decision_id"),
            nullable=False,
        ),
        sa.Column("classifier_version", sa.String(40), nullable=False),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "decision_id",
            "classifier_version",
            name="uq_counterfactual_decision_classification_version",
        ),
        sa.CheckConstraint(
            # Enumerated explicitly, mirroring 0094's own ``origin``/``venue``/
            # ``disposition`` CHECK constraints: a new classifier version is
            # always a deliberate migration, never an open free-text value.
            "classifier_version IN ('REASON_CLASSIFIER_V1','REASON_CLASSIFIER_V2')",
            name="ck_counterfactual_decision_classification_version",
        ),
        sa.CheckConstraint(
            "length(canonical_hash) = 64", name="ck_counterfactual_decision_classification_hash"
        ),
        sa.CheckConstraint(
            "authority = 'NONE'", name="ck_counterfactual_decision_classification_authority"
        ),
    )
    op.create_index(
        "ix_counterfactual_decision_classification_decision_id",
        "counterfactual_decision_classification",
        ["decision_id"],
    )
    op.create_index(
        "ix_counterfactual_decision_classification_version",
        "counterfactual_decision_classification",
        ["classifier_version"],
    )

    _backfill_v1_classification_and_correct_canonical_hash()

    for table in COUNTERFACTUAL_CLASSIFICATION_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM counterfactual_decision_classification) THEN "
            "RAISE EXCEPTION "
            "'cannot downgrade 0095 with retained counterfactual-classification evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_immutable_counterfactual_decision_classification_trigger "
            "ON counterfactual_decision_classification"
        )
    )
    op.drop_table("counterfactual_decision_classification")
    # Deliberately not reverted: see the module docstring. The
    # ``counterfactual_decision.canonical_hash``/``payload`` correction this
    # migration applied stays in place even after downgrading past it.
