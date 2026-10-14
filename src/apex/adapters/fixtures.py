"""Strict, offline loading for statically registered synthetic fixture packs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn, cast

from apex.domain.campaign import OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS
from apex.domain.market import SettlementOutcome, void_settlement_term_content_hash
from apex.domain.values import ContentHash

_DECISION_FILENAME = "decision_inputs.json"
_EXECUTION_FILENAME = "execution_script.json"
_SETTLEMENT_FILENAME = "settlement_outcome.json"
_MANIFEST_FILENAME = "manifest.json"
_PARTIAL_FILL_CANCELED_SETTLED_NO_FIXTURE_ID = "partial-fill-canceled-settled-no"
_PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID = "partial-fill-canceled-settled-void"
_PARTIAL_FILL_FIXTURE_IDS = frozenset(
    (
        "partial-fill-canceled",
        _PARTIAL_FILL_CANCELED_SETTLED_NO_FIXTURE_ID,
        _PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID,
    )
)
_RULE_BOUND_SETTLEMENT_FIXTURE_IDS = frozenset(
    (
        _PARTIAL_FILL_CANCELED_SETTLED_NO_FIXTURE_ID,
        _PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID,
    )
)
_REGISTERED_FILES = MappingProxyType(
    {
        "golden": frozenset((_DECISION_FILENAME, _SETTLEMENT_FILENAME)),
        "invariant-rejected": frozenset((_DECISION_FILENAME,)),
        "no-proposal": frozenset((_DECISION_FILENAME,)),
        "partial-fill-canceled": frozenset((_DECISION_FILENAME, _EXECUTION_FILENAME)),
        "partial-fill-canceled-settled-no": frozenset(
            (_DECISION_FILENAME, _EXECUTION_FILENAME, _SETTLEMENT_FILENAME)
        ),
        "partial-fill-canceled-settled-void": frozenset(
            (_DECISION_FILENAME, _EXECUTION_FILENAME, _SETTLEMENT_FILENAME)
        ),
        "policy-rejected": frozenset((_DECISION_FILENAME,)),
        "unfilled-expired": frozenset((_DECISION_FILENAME, _EXECUTION_FILENAME)),
    }
)
_MARKET_OBSERVATION_SOURCE_IDS = MappingProxyType(
    {
        "golden": "source-book-1",
        "invariant-rejected": "source-book-1",
        "no-proposal": "source-book-1",
        "partial-fill-canceled": "source-book-1",
        "partial-fill-canceled-settled-no": "source-book-1",
        "partial-fill-canceled-settled-void": "source-book-1",
        "policy-rejected": "source-book-1",
        "unfilled-expired": "source-book-1",
    }
)
_REGISTERED_SETTLEMENT_SOURCE_IDS = MappingProxyType(
    {
        _PARTIAL_FILL_CANCELED_SETTLED_NO_FIXTURE_ID: "settlement-partial-fill-canceled-no",
        _PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID: ("settlement-partial-fill-canceled-void"),
    }
)
REGISTERED_FIXTURE_IDS = tuple(sorted(_REGISTERED_FILES))
_DECIMAL_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class FixtureError(ValueError):
    """Base class for fixture boundary failures."""


class FixtureNotAllowedError(FixtureError):
    """The caller requested anything other than the registered fixture pack."""


class FixtureIntegrityError(FixtureError):
    """Frozen fixture bytes do not match their manifest."""


class FixtureFormatError(FixtureError):
    """Fixture JSON or a field violates the strict fixture schema."""


@dataclass(frozen=True, slots=True)
class SourceRecordFixture:
    source_id: str
    source_event_at: datetime
    available_at: datetime
    sequence: int
    schema_version: str
    raw_payload: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class ContractRuleFixture:
    rule_version_id: str
    exact_text: str
    resolution_source: str
    effective_at: datetime
    close_at: datetime
    determination_at: datetime
    expected_settlement_at: datetime
    content_hash: str


@dataclass(frozen=True, slots=True)
class VoidSettlementRuleTermFixture:
    outcome_classification: str
    settlement_value: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class VoidContractRuleFixture(ContractRuleFixture):
    void_settlement_term: VoidSettlementRuleTermFixture


@dataclass(frozen=True, slots=True)
class InstrumentFixture:
    instrument_id: str
    category: str
    payout_unit: str
    price_tick: str
    quantity_increment: str
    yes_ask: str
    no_ask: str
    visible_yes_quantity: str
    yes_settlement_value: str
    no_settlement_value: str
    void_settlement_value: str


@dataclass(frozen=True, slots=True)
class ReferenceEstimateFixture:
    estimate_id: str
    source_id: str
    probability_yes: str
    probability_no: str
    probability_void: str
    published_at: datetime
    expires_at: datetime
    method_version: str


@dataclass(frozen=True, slots=True)
class CampaignFixture:
    campaign_id: str
    currency: str
    campaign_contribution: str
    authorized_campaign_loss: str
    authorized_loss_fraction: str


@dataclass(frozen=True, slots=True)
class PolicyFixture:
    policy_version_id: str
    profile: str
    requested_risk_fraction: str
    max_trade_loss_fraction: str
    max_open_loss_fraction: str
    min_synthetic_edge: str
    max_spread: str
    legacy_max_data_age_seconds: int
    max_capital_lockup_seconds: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class ExecutionScriptFixture:
    script_id: str
    kind: str
    opened_at: datetime
    filled_at: datetime
    opened_sequence: int
    fill_sequence: int
    fill_price: str
    fee_per_contract: str


@dataclass(frozen=True, slots=True)
class UnfilledExpiryExecutionScriptFixture:
    file_hash: str
    fixture_id: str
    script_id: str
    kind: str
    side: str
    executable_yes_ask: str
    executable_no_ask: str
    visible_yes_quantity: str
    opened_at: datetime
    expires_at: datetime
    opened_sequence: int
    expired_sequence: int


@dataclass(frozen=True, slots=True)
class PartialFillCanceledExecutionScriptFixture:
    file_hash: str
    fixture_id: str
    script_id: str
    kind: str
    side: str
    executable_yes_ask: str
    executable_no_ask: str
    visible_yes_quantity: str
    ordered_quantity: str
    fill_quantity: str
    remaining_quantity: str
    opened_at: datetime
    filled_at: datetime
    canceled_at: datetime
    opened_sequence: int
    fill_sequence: int
    canceled_sequence: int
    fill_price: str
    fee_per_contract: str


type FixtureExecutionScript = (
    ExecutionScriptFixture
    | UnfilledExpiryExecutionScriptFixture
    | PartialFillCanceledExecutionScriptFixture
)


@dataclass(frozen=True, slots=True)
class DecisionFixture:
    fixture_id: str
    schema_version: str
    decision_data_hash: str
    file_hash: str
    logical_time: datetime
    decision_cutoff: datetime
    campaign: CampaignFixture
    policy: PolicyFixture
    instrument: InstrumentFixture
    contract_rule: ContractRuleFixture
    source_records: tuple[SourceRecordFixture, ...]
    reference_estimate: ReferenceEstimateFixture
    strategy_version: str
    fee_model_version: str
    execution: FixtureExecutionScript
    strategy_decision_at: datetime
    fee_per_contract: str
    resting_limit_price: str | None
    order_expires_at: datetime | None
    authorization_at: datetime
    market_observation_source_id: str


@dataclass(frozen=True, slots=True)
class SettlementFixture:
    fixture_id: str
    schema_version: str
    file_hash: str
    source_id: str
    instrument_id: str
    rule_version_id: str
    source_event_at: datetime
    available_at: datetime
    settlement_value: str
    raw_payload: str
    raw_payload_hash: str


@dataclass(frozen=True, slots=True)
class RuleBoundSettlementFixture(SettlementFixture):
    resolution_source: str
    rule_content_hash: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class ClassifiedRuleBoundSettlementFixture(SettlementFixture):
    resolution_source: str
    rule_content_hash: str
    outcome_classification: str
    content_hash: str


def _reject_json_number(value: str) -> NoReturn:
    raise FixtureFormatError(f"JSON numeric tokens are forbidden; found {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FixtureFormatError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _decode_json(data: bytes, filename: str) -> dict[str, object]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FixtureFormatError(f"{filename} must be UTF-8") from error
    try:
        decoded = cast(
            object,
            json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_int=_reject_json_number,
                parse_float=_reject_json_number,
                parse_constant=_reject_json_number,
            ),
        )
    except json.JSONDecodeError as error:
        raise FixtureFormatError(f"invalid JSON in {filename}: {error.msg}") from error
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise FixtureFormatError(f"{filename} must contain one JSON object")
    return cast(dict[str, object], decoded)


def _object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FixtureFormatError(f"{path} must be an object")
    return cast(dict[str, object], value)


def _array(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise FixtureFormatError(f"{path} must be an array")
    return value


def _fields(value: dict[str, object], expected: frozenset[str], path: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise FixtureFormatError(f"{path} fields mismatch; missing={missing}, extra={extra}")


def _string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise FixtureFormatError(f"{path} must be a string")
    return value


def _identifier(value: object, path: str) -> str:
    result = _string(value, path)
    if _IDENTIFIER_PATTERN.fullmatch(result) is None:
        raise FixtureFormatError(f"{path} is not a canonical identifier")
    return result


def _decimal_string(value: object, path: str) -> str:
    result = _string(value, path)
    if _DECIMAL_PATTERN.fullmatch(result) is None:
        raise FixtureFormatError(f"{path} must be an unsigned fixed-point decimal string")
    whole, separator, fractional = result.partition(".")
    significant = (whole.lstrip("0") + fractional).lstrip("0") or "0"
    if len(significant) > 18 or (separator and len(fractional) > 8):
        raise FixtureFormatError(f"{path} exceeds supported precision or scale")
    return result


def _integer_string(value: object, path: str) -> int:
    result = _string(value, path)
    if re.fullmatch(r"0|[1-9][0-9]*", result) is None:
        raise FixtureFormatError(f"{path} must be a canonical unsigned integer string")
    return int(result)


def _timestamp(value: object, path: str) -> datetime:
    result = _string(value, path)
    try:
        parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        message = f"{path} must be a whole-second UTC timestamp ending in Z"
        raise FixtureFormatError(message) from error
    return parsed


def _hash_string(value: object, path: str) -> str:
    result = _string(value, path)
    if _SHA256_PATTERN.fullmatch(result) is None:
        raise FixtureFormatError(f"{path} must be a lowercase SHA-256 digest")
    return result


def _verify_inline_hash(payload: str, expected_hash: str, path: str) -> None:
    actual_hash = sha256(payload.encode("utf-8")).hexdigest()
    if actual_hash != expected_hash:
        raise FixtureIntegrityError(f"{path} content hash mismatch")


def _canonical_document_hash(document: dict[str, object], excluded_field: str) -> str:
    evidence = {key: value for key, value in document.items() if key != excluded_field}
    canonical_bytes = json.dumps(
        evidence,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical_bytes).hexdigest()


class FixtureReader:
    """Load only registered local fixture files, with no discovery or fallback."""

    def __init__(self, root: Path | None = None) -> None:
        default_root = Path(__file__).resolve().parents[3] / "fixtures"
        self._root = (root or default_root).resolve()

    def load_decision(self, fixture_id: str) -> DecisionFixture:
        data, content_hash = self._read_verified(fixture_id, _DECISION_FILENAME)
        external_execution: (
            UnfilledExpiryExecutionScriptFixture | PartialFillCanceledExecutionScriptFixture | None
        ) = None
        if _EXECUTION_FILENAME in self._registered_files(fixture_id):
            execution_data, execution_hash = self._read_verified(
                fixture_id,
                _EXECUTION_FILENAME,
            )
            if fixture_id == "unfilled-expired":
                external_execution = self._parse_unfilled_expiry_execution(
                    execution_data,
                    execution_hash,
                    fixture_id,
                )
            elif fixture_id in _PARTIAL_FILL_FIXTURE_IDS:
                external_execution = self._parse_partial_fill_canceled_execution(
                    execution_data,
                    execution_hash,
                    fixture_id,
                )
            else:
                raise FixtureIntegrityError(
                    "registered split fixture has no fixed execution parser"
                )
        return self._parse_decision(
            data,
            content_hash,
            fixture_id,
            external_execution=external_execution,
        )

    def load_settlement(self, fixture_id: str) -> SettlementFixture:
        if _SETTLEMENT_FILENAME not in self._registered_files(fixture_id):
            raise FixtureNotAllowedError(f"fixture pack has no settlement: {fixture_id!r}")
        data, content_hash = self._read_verified(fixture_id, _SETTLEMENT_FILENAME)
        settlement = self._parse_settlement(data, content_hash, fixture_id)
        if fixture_id in _RULE_BOUND_SETTLEMENT_FIXTURE_IDS:
            expected_type: (
                type[RuleBoundSettlementFixture] | type[ClassifiedRuleBoundSettlementFixture]
            )
            if fixture_id == _PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID:
                expected_type = ClassifiedRuleBoundSettlementFixture
            else:
                expected_type = RuleBoundSettlementFixture
            if not isinstance(settlement, expected_type):
                raise FixtureIntegrityError(
                    "registered rule-bound settlement has the wrong fixture type"
                )
            self._validate_rule_bound_partial_fill_settlement(
                self.load_decision(fixture_id),
                settlement,
            )
        return settlement

    def manifest_hash(self, fixture_id: str) -> str:
        """Hash the verified manifest bytes for final report identity only."""

        self._manifest(fixture_id)
        path = self._fixed_path(self._pack_directory(fixture_id), _MANIFEST_FILENAME)
        return sha256(path.read_bytes()).hexdigest()

    def _pack_directory(self, fixture_id: str) -> Path:
        self._registered_files(fixture_id)
        pack = self._root / fixture_id
        if pack.is_symlink():
            raise FixtureIntegrityError("fixture pack directory may not be a symbolic link")
        return pack

    @staticmethod
    def _registered_files(fixture_id: str) -> frozenset[str]:
        try:
            return _REGISTERED_FILES[fixture_id]
        except KeyError as error:
            raise FixtureNotAllowedError(
                f"fixture pack is not registered: {fixture_id!r}"
            ) from error

    def _fixed_path(self, pack: Path, filename: str) -> Path:
        candidate = pack / filename
        if candidate.is_symlink():
            raise FixtureIntegrityError(f"fixture file may not be a symbolic link: {filename}")
        resolved_pack = pack.resolve()
        resolved_candidate = candidate.resolve()
        if resolved_candidate.parent != resolved_pack:
            raise FixtureIntegrityError(f"fixture file escaped its registered pack: {filename}")
        if not resolved_candidate.is_file():
            raise FixtureIntegrityError(f"required fixture file is missing: {filename}")
        return resolved_candidate

    def _manifest(self, fixture_id: str) -> dict[str, str]:
        pack = self._pack_directory(fixture_id)
        path = self._fixed_path(pack, _MANIFEST_FILENAME)
        document = _decode_json(path.read_bytes(), _MANIFEST_FILENAME)
        manifest_fields = frozenset(("fixture_id", "schema_version", "evidence", "files"))
        _fields(document, manifest_fields, "manifest")
        if _identifier(document["fixture_id"], "manifest.fixture_id") != fixture_id:
            raise FixtureIntegrityError("manifest fixture_id does not match the registered pack")
        if _string(document["schema_version"], "manifest.schema_version") != "1":
            raise FixtureFormatError("unsupported manifest schema_version")
        if _string(document["evidence"], "manifest.evidence") != "SYNTHETIC":
            raise FixtureFormatError("fixture evidence must be SYNTHETIC")
        files = _object(document["files"], "manifest.files")
        expected_filenames = self._registered_files(fixture_id)
        if frozenset(files) != expected_filenames:
            raise FixtureFormatError("manifest files do not match the registered fixture spec")
        return {
            filename: _hash_string(files[filename], f"manifest.files.{filename}")
            for filename in sorted(expected_filenames)
        }

    def _read_verified(self, fixture_id: str, filename: str) -> tuple[dict[str, object], str]:
        if filename not in self._registered_files(fixture_id):
            raise FixtureNotAllowedError(
                f"file is not registered for fixture {fixture_id!r}: {filename}"
            )
        expected_hash = self._manifest(fixture_id)[filename]
        path = self._fixed_path(self._pack_directory(fixture_id), filename)
        raw_bytes = path.read_bytes()
        actual_hash = sha256(raw_bytes).hexdigest()
        if actual_hash != expected_hash:
            raise FixtureIntegrityError(f"fixture hash mismatch: {filename}")
        return _decode_json(raw_bytes, filename), actual_hash

    def _parse_decision(
        self,
        document: dict[str, object],
        content_hash: str,
        expected_fixture_id: str,
        *,
        external_execution: (
            UnfilledExpiryExecutionScriptFixture | PartialFillCanceledExecutionScriptFixture | None
        ),
    ) -> DecisionFixture:
        expected = frozenset(
            (
                "fixture_id",
                "schema_version",
                "logical_time",
                "decision_cutoff",
                "campaign",
                "policy",
                "instrument",
                "contract_rule",
                "source_records",
                "reference_estimate",
                "strategy_version",
                "fee_model_version",
            )
        )
        if external_execution is None:
            expected |= frozenset(("execution",))
        authorization_fields: frozenset[str] = frozenset()
        if expected_fixture_id in {
            "invariant-rejected",
            "partial-fill-canceled",
            "partial-fill-canceled-settled-no",
            "partial-fill-canceled-settled-void",
            "unfilled-expired",
        }:
            authorization_fields = frozenset(
                (
                    "authorization_at",
                    "market_observation_source_id",
                    "maximum_operational_market_data_age_seconds",
                )
            )
        expected |= authorization_fields
        resting_fields: frozenset[str] = frozenset()
        if expected_fixture_id in {
            "partial-fill-canceled",
            "partial-fill-canceled-settled-no",
            "partial-fill-canceled-settled-void",
            "unfilled-expired",
        }:
            resting_fields = frozenset(
                (
                    "strategy_decision_at",
                    "order_side",
                    "order_limit_price",
                    "order_expires_at",
                    "fee_per_contract",
                )
            )
        expected |= resting_fields
        _fields(document, expected, "decision")
        fixture_id = _identifier(document["fixture_id"], "decision.fixture_id")
        if fixture_id != expected_fixture_id:
            raise FixtureIntegrityError("decision fixture_id does not match its pack")
        schema_version = _string(document["schema_version"], "decision.schema_version")
        if schema_version != "1":
            raise FixtureFormatError("unsupported decision schema_version")
        campaign = self._parse_campaign(_object(document["campaign"], "decision.campaign"))
        policy = self._parse_policy(_object(document["policy"], "decision.policy"))
        instrument = self._parse_instrument(_object(document["instrument"], "decision.instrument"))
        rule = self._parse_rule(
            _object(document["contract_rule"], "decision.contract_rule"),
            fixture_id,
        )
        if isinstance(rule, VoidContractRuleFixture) and (
            rule.void_settlement_term.outcome_classification != SettlementOutcome.VOID.value
            or Decimal(rule.void_settlement_term.settlement_value)
            != Decimal(instrument.void_settlement_value)
        ):
            raise FixtureIntegrityError(
                "VOID rule term does not match the instrument settlement value"
            )
        records = tuple(
            self._parse_source_record(_object(record, f"decision.source_records[{index}]"), index)
            for index, record in enumerate(
                _array(document["source_records"], "decision.source_records")
            )
        )
        if not records:
            raise FixtureFormatError("decision.source_records must not be empty")
        estimate = self._parse_estimate(
            _object(document["reference_estimate"], "decision.reference_estimate")
        )
        source_ids = {record.source_id for record in records}
        sequences = {record.sequence for record in records}
        if len(source_ids) != len(records):
            raise FixtureFormatError("source record identifiers must be unique")
        if len(sequences) != len(records):
            raise FixtureFormatError("source record sequences must be unique")
        if estimate.source_id not in source_ids:
            raise FixtureFormatError("reference estimate source_id is not a source record")
        if instrument.category == "SPORTS":
            raise FixtureFormatError("sports fixtures are prohibited")
        logical_time = _timestamp(document["logical_time"], "decision.logical_time")
        decision_cutoff = _timestamp(document["decision_cutoff"], "decision.decision_cutoff")
        estimate_source_records = tuple(
            record for record in records if record.source_id == estimate.source_id
        )
        if len(estimate_source_records) != 1:
            raise FixtureFormatError("reference estimate must identify exactly one source record")
        if estimate_source_records[0].available_at > decision_cutoff:
            raise FixtureFormatError(
                "reference estimate source is not available at the decision cutoff"
            )
        if external_execution is None:
            execution: FixtureExecutionScript = self._parse_execution(
                _object(document["execution"], "decision.execution")
            )
        else:
            execution = external_execution
        strategy_decision_at = execution.opened_at
        fee_per_contract: str
        resting_limit_price: str | None = None
        order_expires_at: datetime | None = None
        if resting_fields:
            strategy_decision_at = _timestamp(
                document["strategy_decision_at"],
                "decision.strategy_decision_at",
            )
            if _string(document["order_side"], "decision.order_side") != "YES":
                if expected_fixture_id == "unfilled-expired":
                    raise FixtureFormatError("unfilled-expired order side must be YES")
                raise FixtureFormatError("partial-fill-canceled order side must be YES")
            resting_limit_price = _decimal_string(
                document["order_limit_price"],
                "decision.order_limit_price",
            )
            order_expires_at = _timestamp(
                document["order_expires_at"],
                "decision.order_expires_at",
            )
            fee_per_contract = _decimal_string(
                document["fee_per_contract"],
                "decision.fee_per_contract",
            )
        elif isinstance(execution, ExecutionScriptFixture):
            fee_per_contract = execution.fee_per_contract
        else:
            raise FixtureFormatError("external execution requires resting order intent")
        market_observation_source_id = _MARKET_OBSERVATION_SOURCE_IDS[fixture_id]
        authorization_at = strategy_decision_at
        if authorization_fields:
            fixture_market_source_id = _identifier(
                document["market_observation_source_id"],
                "decision.market_observation_source_id",
            )
            if fixture_market_source_id != market_observation_source_id:
                raise FixtureIntegrityError(
                    "market observation source does not match the registered fixture spec"
                )
            declared_maximum = _integer_string(
                document["maximum_operational_market_data_age_seconds"],
                "decision.maximum_operational_market_data_age_seconds",
            )
            if declared_maximum != OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS:
                raise FixtureFormatError(
                    "fixture operational freshness maximum does not match the sealed invariant"
                )
            authorization_at = _timestamp(document["authorization_at"], "decision.authorization_at")
        matching_market_records = tuple(
            record for record in records if record.source_id == market_observation_source_id
        )
        if len(matching_market_records) != 1:
            raise FixtureFormatError(
                "registered market observation must identify exactly one source record"
            )
        if matching_market_records[0].available_at > decision_cutoff:
            raise FixtureFormatError(
                "registered market observation is not eligible at the decision cutoff"
            )
        if authorization_at < strategy_decision_at:
            raise FixtureFormatError("risk authorization cannot precede the strategy decision time")
        if logical_time < authorization_at:
            raise FixtureFormatError("logical time cannot precede risk authorization")
        if isinstance(execution, UnfilledExpiryExecutionScriptFixture):
            if resting_limit_price is None or order_expires_at is None:
                raise FixtureFormatError("unfilled execution is missing resting order intent")
            if not (
                decision_cutoff
                <= strategy_decision_at
                < authorization_at
                < execution.opened_at
                < execution.expires_at
            ):
                raise FixtureFormatError(
                    "unfilled decision, authorization, open, and expiration times are invalid"
                )
            if logical_time != execution.expires_at:
                raise FixtureFormatError(
                    "unfilled logical time must equal deterministic expiration"
                )
            if order_expires_at != execution.expires_at:
                raise FixtureIntegrityError(
                    "decision order expiration does not match execution script"
                )
            if order_expires_at > min(estimate.expires_at, rule.close_at):
                raise FixtureFormatError("order expiration exceeds proposal availability")
            if (
                execution.side != "YES"
                or execution.executable_yes_ask != instrument.yes_ask
                or execution.executable_no_ask != instrument.no_ask
                or execution.visible_yes_quantity != instrument.visible_yes_quantity
            ):
                raise FixtureIntegrityError("execution script book does not match decision inputs")
            limit_value = Decimal(resting_limit_price)
            ask_value = Decimal(instrument.yes_ask)
            tick_value = Decimal(instrument.price_tick)
            if limit_value <= 0 or limit_value >= ask_value or limit_value % tick_value != 0:
                raise FixtureFormatError(
                    "resting YES limit must be positive, tick-aligned, and below the ask"
                )
        if isinstance(execution, PartialFillCanceledExecutionScriptFixture):
            if resting_limit_price is None or order_expires_at is None:
                raise FixtureFormatError("partial-fill execution is missing resting order intent")
            if not (
                decision_cutoff
                <= strategy_decision_at
                < authorization_at
                < execution.opened_at
                < execution.filled_at
                < execution.canceled_at
                < order_expires_at
            ):
                raise FixtureFormatError(
                    "partial-fill decision, authorization, execution, cancellation, "
                    "and expiration times are invalid"
                )
            if fixture_id in _RULE_BOUND_SETTLEMENT_FIXTURE_IDS:
                if logical_time < rule.expected_settlement_at:
                    raise FixtureFormatError(
                        "settled partial-fill logical time must reach expected settlement"
                    )
            elif logical_time != execution.canceled_at:
                raise FixtureFormatError(
                    "partial-fill logical time must equal deterministic cancellation"
                )
            if order_expires_at > min(estimate.expires_at, rule.close_at):
                raise FixtureFormatError("order expiration exceeds proposal availability")
            if (
                execution.side != "YES"
                or execution.executable_yes_ask != instrument.yes_ask
                or execution.executable_no_ask != instrument.no_ask
                or execution.visible_yes_quantity != instrument.visible_yes_quantity
            ):
                raise FixtureIntegrityError(
                    "partial-fill execution book does not match decision inputs"
                )
            if execution.fee_per_contract != fee_per_contract:
                raise FixtureIntegrityError(
                    "partial-fill execution fee does not match decision inputs"
                )
            limit_value = Decimal(resting_limit_price)
            ask_value = Decimal(instrument.yes_ask)
            fill_price = Decimal(execution.fill_price)
            fee_value = Decimal(execution.fee_per_contract)
            tick_value = Decimal(instrument.price_tick)
            increment_value = Decimal(instrument.quantity_increment)
            ordered_quantity = Decimal(execution.ordered_quantity)
            fill_quantity = Decimal(execution.fill_quantity)
            remaining_quantity = Decimal(execution.remaining_quantity)
            visible_quantity = Decimal(instrument.visible_yes_quantity)
            if tick_value <= 0 or increment_value <= 0:
                raise FixtureFormatError(
                    "partial-fill price tick and quantity increment must be positive"
                )
            if limit_value <= 0 or limit_value >= ask_value or limit_value % tick_value != 0:
                raise FixtureFormatError(
                    "partial-fill YES limit must be positive, tick-aligned, and below the ask"
                )
            if fill_price != limit_value or fill_price % tick_value != 0:
                raise FixtureIntegrityError(
                    "partial-fill price must equal the authorized tick-aligned limit"
                )
            if fee_value <= 0:
                raise FixtureFormatError("partial-fill fee per contract must be positive")
            if (
                ordered_quantity <= 0
                or fill_quantity <= 0
                or fill_quantity >= ordered_quantity
                or remaining_quantity != ordered_quantity - fill_quantity
                or ordered_quantity > visible_quantity
                or ordered_quantity % increment_value != 0
                or fill_quantity % increment_value != 0
                or remaining_quantity % increment_value != 0
            ):
                raise FixtureFormatError(
                    "partial-fill quantities must be positive, exact, increment-aligned, "
                    "within visibility, and leave a nonzero remainder"
                )
        decision_document = {
            key: value
            for key, value in document.items()
            if key not in {"execution", *authorization_fields}
        }
        decision_bytes = json.dumps(
            decision_document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return DecisionFixture(
            fixture_id=fixture_id,
            schema_version=schema_version,
            decision_data_hash=sha256(decision_bytes).hexdigest(),
            file_hash=content_hash,
            logical_time=logical_time,
            decision_cutoff=decision_cutoff,
            campaign=campaign,
            policy=policy,
            instrument=instrument,
            contract_rule=rule,
            source_records=records,
            reference_estimate=estimate,
            strategy_version=_identifier(document["strategy_version"], "decision.strategy_version"),
            fee_model_version=_identifier(
                document["fee_model_version"], "decision.fee_model_version"
            ),
            execution=execution,
            strategy_decision_at=strategy_decision_at,
            fee_per_contract=fee_per_contract,
            resting_limit_price=resting_limit_price,
            order_expires_at=order_expires_at,
            authorization_at=authorization_at,
            market_observation_source_id=market_observation_source_id,
        )

    def _parse_campaign(self, value: dict[str, object]) -> CampaignFixture:
        _fields(
            value,
            frozenset(
                (
                    "campaign_id",
                    "currency",
                    "campaign_contribution",
                    "authorized_campaign_loss",
                    "authorized_loss_fraction",
                )
            ),
            "campaign",
        )
        currency = _string(value["currency"], "campaign.currency")
        if currency != "USD":
            raise FixtureFormatError("the golden slice supports USD only")
        return CampaignFixture(
            campaign_id=_identifier(value["campaign_id"], "campaign.campaign_id"),
            currency=currency,
            campaign_contribution=_decimal_string(
                value["campaign_contribution"], "campaign.campaign_contribution"
            ),
            authorized_campaign_loss=_decimal_string(
                value["authorized_campaign_loss"], "campaign.authorized_campaign_loss"
            ),
            authorized_loss_fraction=_decimal_string(
                value["authorized_loss_fraction"], "campaign.authorized_loss_fraction"
            ),
        )

    def _parse_policy(self, value: dict[str, object]) -> PolicyFixture:
        expected = frozenset(
            (
                "policy_version_id",
                "profile",
                "requested_risk_fraction",
                "max_trade_loss_fraction",
                "max_open_loss_fraction",
                "min_synthetic_edge",
                "max_spread",
                "max_data_age_seconds",
                "max_capital_lockup_seconds",
                "content_hash",
            )
        )
        _fields(value, expected, "policy")
        policy_version_id = _identifier(value["policy_version_id"], "policy.policy_version_id")
        profile = _identifier(value["profile"], "policy.profile")
        requested_risk_fraction = _decimal_string(
            value["requested_risk_fraction"], "policy.requested_risk_fraction"
        )
        max_trade_loss_fraction = _decimal_string(
            value["max_trade_loss_fraction"], "policy.max_trade_loss_fraction"
        )
        max_open_loss_fraction = _decimal_string(
            value["max_open_loss_fraction"], "policy.max_open_loss_fraction"
        )
        min_synthetic_edge = _decimal_string(
            value["min_synthetic_edge"], "policy.min_synthetic_edge"
        )
        max_spread = _decimal_string(value["max_spread"], "policy.max_spread")
        legacy_max_data_age_seconds = _integer_string(
            value["max_data_age_seconds"], "policy.max_data_age_seconds"
        )
        if legacy_max_data_age_seconds != OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS:
            raise FixtureFormatError(
                "schema-v1 policy data age must match the sealed operational invariant"
            )
        max_capital_lockup_seconds = _integer_string(
            value["max_capital_lockup_seconds"],
            "policy.max_capital_lockup_seconds",
        )
        content_hash = _hash_string(value["content_hash"], "policy.content_hash")
        policy_payload = "|".join(
            (
                policy_version_id,
                profile,
                requested_risk_fraction,
                max_trade_loss_fraction,
                max_open_loss_fraction,
                min_synthetic_edge,
                max_spread,
                str(legacy_max_data_age_seconds),
                str(max_capital_lockup_seconds),
            )
        )
        _verify_inline_hash(policy_payload, content_hash, "policy")
        return PolicyFixture(
            policy_version_id=policy_version_id,
            profile=profile,
            requested_risk_fraction=requested_risk_fraction,
            max_trade_loss_fraction=max_trade_loss_fraction,
            max_open_loss_fraction=max_open_loss_fraction,
            min_synthetic_edge=min_synthetic_edge,
            max_spread=max_spread,
            legacy_max_data_age_seconds=legacy_max_data_age_seconds,
            max_capital_lockup_seconds=max_capital_lockup_seconds,
            content_hash=content_hash,
        )

    def _parse_instrument(self, value: dict[str, object]) -> InstrumentFixture:
        expected = frozenset(
            (
                "instrument_id",
                "category",
                "payout_unit",
                "price_tick",
                "quantity_increment",
                "yes_ask",
                "no_ask",
                "visible_yes_quantity",
                "yes_settlement_value",
                "no_settlement_value",
                "void_settlement_value",
            )
        )
        _fields(value, expected, "instrument")
        return InstrumentFixture(
            instrument_id=_identifier(value["instrument_id"], "instrument.instrument_id"),
            category=_identifier(value["category"], "instrument.category"),
            payout_unit=_decimal_string(value["payout_unit"], "instrument.payout_unit"),
            price_tick=_decimal_string(value["price_tick"], "instrument.price_tick"),
            quantity_increment=_decimal_string(
                value["quantity_increment"], "instrument.quantity_increment"
            ),
            yes_ask=_decimal_string(value["yes_ask"], "instrument.yes_ask"),
            no_ask=_decimal_string(value["no_ask"], "instrument.no_ask"),
            visible_yes_quantity=_decimal_string(
                value["visible_yes_quantity"], "instrument.visible_yes_quantity"
            ),
            yes_settlement_value=_decimal_string(
                value["yes_settlement_value"], "instrument.yes_settlement_value"
            ),
            no_settlement_value=_decimal_string(
                value["no_settlement_value"], "instrument.no_settlement_value"
            ),
            void_settlement_value=_decimal_string(
                value["void_settlement_value"], "instrument.void_settlement_value"
            ),
        )

    def _parse_rule(
        self,
        value: dict[str, object],
        fixture_id: str,
    ) -> ContractRuleFixture:
        expected = frozenset(
            (
                "rule_version_id",
                "exact_text",
                "resolution_source",
                "effective_at",
                "close_at",
                "determination_at",
                "expected_settlement_at",
                "content_hash",
            )
        )
        is_void_fixture = fixture_id == _PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID
        if is_void_fixture:
            expected |= frozenset(("void_settlement_term",))
        _fields(value, expected, "contract_rule")
        exact_text = _string(value["exact_text"], "contract_rule.exact_text")
        content_hash = _hash_string(value["content_hash"], "contract_rule.content_hash")
        _verify_inline_hash(exact_text, content_hash, "contract_rule.exact_text")
        common = ContractRuleFixture(
            rule_version_id=_identifier(value["rule_version_id"], "contract_rule.rule_version_id"),
            exact_text=exact_text,
            resolution_source=_string(
                value["resolution_source"], "contract_rule.resolution_source"
            ),
            effective_at=_timestamp(value["effective_at"], "contract_rule.effective_at"),
            close_at=_timestamp(value["close_at"], "contract_rule.close_at"),
            determination_at=_timestamp(
                value["determination_at"], "contract_rule.determination_at"
            ),
            expected_settlement_at=_timestamp(
                value["expected_settlement_at"], "contract_rule.expected_settlement_at"
            ),
            content_hash=content_hash,
        )
        if not is_void_fixture:
            return common

        term_document = _object(
            value["void_settlement_term"],
            "contract_rule.void_settlement_term",
        )
        _fields(
            term_document,
            frozenset(("outcome_classification", "settlement_value", "content_hash")),
            "contract_rule.void_settlement_term",
        )
        classification = _identifier(
            term_document["outcome_classification"],
            "contract_rule.void_settlement_term.outcome_classification",
        )
        if classification != SettlementOutcome.VOID.value:
            raise FixtureIntegrityError("VOID rule term must use VOID classification")
        settlement_value = _decimal_string(
            term_document["settlement_value"],
            "contract_rule.void_settlement_term.settlement_value",
        )
        parsed_value = Decimal(settlement_value)
        if not Decimal(0) < parsed_value <= Decimal(1):
            raise FixtureFormatError("VOID rule term value must be bounded and nonzero")
        if exact_text.count(f"{settlement_value} if it is {classification}") != 1:
            raise FixtureIntegrityError("exact rule text does not authorize its VOID term")
        term_hash = _hash_string(
            term_document["content_hash"],
            "contract_rule.void_settlement_term.content_hash",
        )
        expected_term_hash = void_settlement_term_content_hash(
            ContentHash(content_hash),
            SettlementOutcome.VOID,
            settlement_value,
        ).value
        if term_hash != expected_term_hash:
            raise FixtureIntegrityError("VOID rule term hash does not match the exact rule")
        return VoidContractRuleFixture(
            rule_version_id=common.rule_version_id,
            exact_text=common.exact_text,
            resolution_source=common.resolution_source,
            effective_at=common.effective_at,
            close_at=common.close_at,
            determination_at=common.determination_at,
            expected_settlement_at=common.expected_settlement_at,
            content_hash=common.content_hash,
            void_settlement_term=VoidSettlementRuleTermFixture(
                outcome_classification=classification,
                settlement_value=settlement_value,
                content_hash=term_hash,
            ),
        )

    def _parse_source_record(self, value: dict[str, object], index: int) -> SourceRecordFixture:
        path = f"source_records[{index}]"
        expected = frozenset(
            (
                "source_id",
                "source_event_at",
                "available_at",
                "sequence",
                "schema_version",
                "raw_payload",
                "content_hash",
            )
        )
        _fields(value, expected, path)
        raw_payload = _string(value["raw_payload"], f"{path}.raw_payload")
        content_hash = _hash_string(value["content_hash"], f"{path}.content_hash")
        _verify_inline_hash(raw_payload, content_hash, f"{path}.raw_payload")
        return SourceRecordFixture(
            source_id=_identifier(value["source_id"], f"{path}.source_id"),
            source_event_at=_timestamp(value["source_event_at"], f"{path}.source_event_at"),
            available_at=_timestamp(value["available_at"], f"{path}.available_at"),
            sequence=_integer_string(value["sequence"], f"{path}.sequence"),
            schema_version=_string(value["schema_version"], f"{path}.schema_version"),
            raw_payload=raw_payload,
            content_hash=content_hash,
        )

    def _parse_estimate(self, value: dict[str, object]) -> ReferenceEstimateFixture:
        expected = frozenset(
            (
                "estimate_id",
                "source_id",
                "probability_yes",
                "probability_no",
                "probability_void",
                "published_at",
                "expires_at",
                "method_version",
            )
        )
        _fields(value, expected, "reference_estimate")
        return ReferenceEstimateFixture(
            estimate_id=_identifier(value["estimate_id"], "reference_estimate.estimate_id"),
            source_id=_identifier(value["source_id"], "reference_estimate.source_id"),
            probability_yes=_decimal_string(
                value["probability_yes"], "reference_estimate.probability_yes"
            ),
            probability_no=_decimal_string(
                value["probability_no"], "reference_estimate.probability_no"
            ),
            probability_void=_decimal_string(
                value["probability_void"], "reference_estimate.probability_void"
            ),
            published_at=_timestamp(value["published_at"], "reference_estimate.published_at"),
            expires_at=_timestamp(value["expires_at"], "reference_estimate.expires_at"),
            method_version=_identifier(
                value["method_version"], "reference_estimate.method_version"
            ),
        )

    def _parse_execution(self, value: dict[str, object]) -> ExecutionScriptFixture:
        expected = frozenset(
            (
                "script_id",
                "kind",
                "opened_at",
                "filled_at",
                "opened_sequence",
                "fill_sequence",
                "fill_price",
                "fee_per_contract",
            )
        )
        _fields(value, expected, "execution")
        kind = _string(value["kind"], "execution.kind")
        if kind != "FULL_MARKETABLE_FILL":
            raise FixtureFormatError("registered execution must be FULL_MARKETABLE_FILL")
        return ExecutionScriptFixture(
            script_id=_identifier(value["script_id"], "execution.script_id"),
            kind=kind,
            opened_at=_timestamp(value["opened_at"], "execution.opened_at"),
            filled_at=_timestamp(value["filled_at"], "execution.filled_at"),
            opened_sequence=_integer_string(value["opened_sequence"], "execution.opened_sequence"),
            fill_sequence=_integer_string(value["fill_sequence"], "execution.fill_sequence"),
            fill_price=_decimal_string(value["fill_price"], "execution.fill_price"),
            fee_per_contract=_decimal_string(
                value["fee_per_contract"], "execution.fee_per_contract"
            ),
        )

    def _parse_unfilled_expiry_execution(
        self,
        document: dict[str, object],
        content_hash: str,
        expected_fixture_id: str,
    ) -> UnfilledExpiryExecutionScriptFixture:
        expected = frozenset(
            (
                "fixture_id",
                "schema_version",
                "script_id",
                "kind",
                "side",
                "executable_yes_ask",
                "executable_no_ask",
                "visible_yes_quantity",
                "opened_at",
                "expires_at",
                "opened_sequence",
                "expired_sequence",
            )
        )
        _fields(document, expected, "execution_script")
        fixture_id = _identifier(document["fixture_id"], "execution_script.fixture_id")
        if fixture_id != expected_fixture_id:
            raise FixtureIntegrityError("execution script fixture_id does not match its pack")
        if _string(document["schema_version"], "execution_script.schema_version") != "1":
            raise FixtureFormatError("unsupported execution script schema_version")
        kind = _string(document["kind"], "execution_script.kind")
        if kind != "NONMARKETABLE_EXPIRE_UNFILLED":
            raise FixtureFormatError("unfilled execution script kind is invalid")
        side = _string(document["side"], "execution_script.side")
        if side != "YES":
            raise FixtureFormatError("unfilled execution script side must be YES")
        opened_at = _timestamp(document["opened_at"], "execution_script.opened_at")
        expires_at = _timestamp(document["expires_at"], "execution_script.expires_at")
        if expires_at <= opened_at:
            raise FixtureFormatError("execution expiration must occur after opening")
        opened_sequence = _integer_string(
            document["opened_sequence"],
            "execution_script.opened_sequence",
        )
        expired_sequence = _integer_string(
            document["expired_sequence"],
            "execution_script.expired_sequence",
        )
        if opened_sequence != 1 or expired_sequence != 2 or expired_sequence != opened_sequence + 1:
            raise FixtureFormatError(
                "unfilled execution sequences must be contiguous and exactly 1 then 2"
            )
        return UnfilledExpiryExecutionScriptFixture(
            file_hash=content_hash,
            fixture_id=fixture_id,
            script_id=_identifier(document["script_id"], "execution_script.script_id"),
            kind=kind,
            side=side,
            executable_yes_ask=_decimal_string(
                document["executable_yes_ask"],
                "execution_script.executable_yes_ask",
            ),
            executable_no_ask=_decimal_string(
                document["executable_no_ask"],
                "execution_script.executable_no_ask",
            ),
            visible_yes_quantity=_decimal_string(
                document["visible_yes_quantity"],
                "execution_script.visible_yes_quantity",
            ),
            opened_at=opened_at,
            expires_at=expires_at,
            opened_sequence=opened_sequence,
            expired_sequence=expired_sequence,
        )

    def _parse_partial_fill_canceled_execution(
        self,
        document: dict[str, object],
        content_hash: str,
        expected_fixture_id: str,
    ) -> PartialFillCanceledExecutionScriptFixture:
        expected = frozenset(
            (
                "fixture_id",
                "schema_version",
                "script_id",
                "kind",
                "side",
                "executable_yes_ask",
                "executable_no_ask",
                "visible_yes_quantity",
                "ordered_quantity",
                "fill_quantity",
                "remaining_quantity",
                "opened_at",
                "filled_at",
                "canceled_at",
                "opened_sequence",
                "fill_sequence",
                "canceled_sequence",
                "fill_price",
                "fee_per_contract",
            )
        )
        _fields(document, expected, "execution_script")
        fixture_id = _identifier(document["fixture_id"], "execution_script.fixture_id")
        if fixture_id != expected_fixture_id:
            raise FixtureIntegrityError("execution script fixture_id does not match its pack")
        if _string(document["schema_version"], "execution_script.schema_version") != "1":
            raise FixtureFormatError("unsupported execution script schema_version")
        kind = _string(document["kind"], "execution_script.kind")
        if kind != "PARTIAL_FILL_THEN_CANCEL":
            raise FixtureFormatError("partial-fill execution script kind is invalid")
        side = _string(document["side"], "execution_script.side")
        if side != "YES":
            raise FixtureFormatError("partial-fill execution script side must be YES")
        opened_at = _timestamp(document["opened_at"], "execution_script.opened_at")
        filled_at = _timestamp(document["filled_at"], "execution_script.filled_at")
        canceled_at = _timestamp(document["canceled_at"], "execution_script.canceled_at")
        if not opened_at < filled_at < canceled_at:
            raise FixtureFormatError(
                "partial-fill execution times must be strictly open, fill, then cancel"
            )
        opened_sequence = _integer_string(
            document["opened_sequence"],
            "execution_script.opened_sequence",
        )
        fill_sequence = _integer_string(
            document["fill_sequence"],
            "execution_script.fill_sequence",
        )
        canceled_sequence = _integer_string(
            document["canceled_sequence"],
            "execution_script.canceled_sequence",
        )
        if (
            opened_sequence != 1
            or fill_sequence != 2
            or canceled_sequence != 3
            or fill_sequence != opened_sequence + 1
            or canceled_sequence != fill_sequence + 1
        ):
            raise FixtureFormatError(
                "partial-fill execution sequences must be contiguous and exactly 1, 2, 3"
            )
        return PartialFillCanceledExecutionScriptFixture(
            file_hash=content_hash,
            fixture_id=fixture_id,
            script_id=_identifier(document["script_id"], "execution_script.script_id"),
            kind=kind,
            side=side,
            executable_yes_ask=_decimal_string(
                document["executable_yes_ask"],
                "execution_script.executable_yes_ask",
            ),
            executable_no_ask=_decimal_string(
                document["executable_no_ask"],
                "execution_script.executable_no_ask",
            ),
            visible_yes_quantity=_decimal_string(
                document["visible_yes_quantity"],
                "execution_script.visible_yes_quantity",
            ),
            ordered_quantity=_decimal_string(
                document["ordered_quantity"],
                "execution_script.ordered_quantity",
            ),
            fill_quantity=_decimal_string(
                document["fill_quantity"],
                "execution_script.fill_quantity",
            ),
            remaining_quantity=_decimal_string(
                document["remaining_quantity"],
                "execution_script.remaining_quantity",
            ),
            opened_at=opened_at,
            filled_at=filled_at,
            canceled_at=canceled_at,
            opened_sequence=opened_sequence,
            fill_sequence=fill_sequence,
            canceled_sequence=canceled_sequence,
            fill_price=_decimal_string(
                document["fill_price"],
                "execution_script.fill_price",
            ),
            fee_per_contract=_decimal_string(
                document["fee_per_contract"],
                "execution_script.fee_per_contract",
            ),
        )

    def _parse_settlement(
        self,
        document: dict[str, object],
        content_hash: str,
        expected_fixture_id: str,
    ) -> SettlementFixture:
        expected = frozenset(
            (
                "fixture_id",
                "schema_version",
                "source_id",
                "instrument_id",
                "rule_version_id",
                "source_event_at",
                "available_at",
                "settlement_value",
                "raw_payload",
                "raw_payload_hash",
            )
        )
        if expected_fixture_id in _RULE_BOUND_SETTLEMENT_FIXTURE_IDS:
            expected |= frozenset(
                (
                    "resolution_source",
                    "rule_content_hash",
                    "content_hash",
                )
            )
        if expected_fixture_id == _PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID:
            expected |= frozenset(("outcome_classification",))
        _fields(document, expected, "settlement")
        fixture_id = _identifier(document["fixture_id"], "settlement.fixture_id")
        if fixture_id != expected_fixture_id:
            raise FixtureIntegrityError("settlement fixture_id does not match its pack")
        schema_version = _string(document["schema_version"], "settlement.schema_version")
        required_schema_version = (
            "2" if expected_fixture_id in _RULE_BOUND_SETTLEMENT_FIXTURE_IDS else "1"
        )
        if schema_version != required_schema_version:
            raise FixtureFormatError("unsupported settlement schema_version")
        raw_payload = _string(document["raw_payload"], "settlement.raw_payload")
        raw_payload_hash = _hash_string(document["raw_payload_hash"], "settlement.raw_payload_hash")
        _verify_inline_hash(raw_payload, raw_payload_hash, "settlement.raw_payload")
        source_id = _identifier(document["source_id"], "settlement.source_id")
        instrument_id = _identifier(document["instrument_id"], "settlement.instrument_id")
        rule_version_id = _identifier(document["rule_version_id"], "settlement.rule_version_id")
        source_event_at = _timestamp(document["source_event_at"], "settlement.source_event_at")
        available_at = _timestamp(document["available_at"], "settlement.available_at")
        settlement_value = _decimal_string(
            document["settlement_value"], "settlement.settlement_value"
        )
        if expected_fixture_id in _RULE_BOUND_SETTLEMENT_FIXTURE_IDS:
            resolution_source = _string(
                document["resolution_source"],
                "settlement.resolution_source",
            )
            rule_content_hash = _hash_string(
                document["rule_content_hash"],
                "settlement.rule_content_hash",
            )
            canonical_content_hash = _hash_string(
                document["content_hash"],
                "settlement.content_hash",
            )
            if _canonical_document_hash(document, "content_hash") != canonical_content_hash:
                raise FixtureIntegrityError("settlement canonical content hash mismatch")
            if expected_fixture_id == _PARTIAL_FILL_CANCELED_SETTLED_VOID_FIXTURE_ID:
                return ClassifiedRuleBoundSettlementFixture(
                    fixture_id=fixture_id,
                    schema_version=schema_version,
                    file_hash=content_hash,
                    source_id=source_id,
                    instrument_id=instrument_id,
                    rule_version_id=rule_version_id,
                    source_event_at=source_event_at,
                    available_at=available_at,
                    settlement_value=settlement_value,
                    raw_payload=raw_payload,
                    raw_payload_hash=raw_payload_hash,
                    resolution_source=resolution_source,
                    rule_content_hash=rule_content_hash,
                    outcome_classification=_string(
                        document["outcome_classification"],
                        "settlement.outcome_classification",
                    ),
                    content_hash=canonical_content_hash,
                )
            return RuleBoundSettlementFixture(
                fixture_id=fixture_id,
                schema_version=schema_version,
                file_hash=content_hash,
                source_id=source_id,
                instrument_id=instrument_id,
                rule_version_id=rule_version_id,
                source_event_at=source_event_at,
                available_at=available_at,
                settlement_value=settlement_value,
                raw_payload=raw_payload,
                raw_payload_hash=raw_payload_hash,
                resolution_source=resolution_source,
                rule_content_hash=rule_content_hash,
                content_hash=canonical_content_hash,
            )
        return SettlementFixture(
            fixture_id=fixture_id,
            schema_version=schema_version,
            file_hash=content_hash,
            source_id=source_id,
            instrument_id=instrument_id,
            rule_version_id=rule_version_id,
            source_event_at=source_event_at,
            available_at=available_at,
            settlement_value=settlement_value,
            raw_payload=raw_payload,
            raw_payload_hash=raw_payload_hash,
        )

    def _validate_rule_bound_partial_fill_settlement(
        self,
        decision: DecisionFixture,
        settlement: RuleBoundSettlementFixture | ClassifiedRuleBoundSettlementFixture,
    ) -> None:
        execution = decision.execution
        if not isinstance(execution, PartialFillCanceledExecutionScriptFixture):
            raise FixtureIntegrityError(
                "rule-bound settlement requires partial-fill cancellation execution"
            )
        if settlement.source_id != _REGISTERED_SETTLEMENT_SOURCE_IDS[decision.fixture_id]:
            raise FixtureIntegrityError(
                "settlement source does not match the registered fixture spec"
            )
        if settlement.instrument_id != decision.instrument.instrument_id:
            raise FixtureIntegrityError("settlement instrument does not match decision inputs")
        if settlement.rule_version_id != decision.contract_rule.rule_version_id:
            raise FixtureIntegrityError("settlement rule version does not match decision inputs")
        if settlement.rule_content_hash != decision.contract_rule.content_hash:
            raise FixtureIntegrityError(
                "settlement rule content hash does not match decision inputs"
            )
        if settlement.resolution_source != decision.contract_rule.resolution_source:
            raise FixtureIntegrityError(
                "settlement resolution source does not match decision inputs"
            )
        if settlement.source_event_at != decision.contract_rule.determination_at:
            raise FixtureIntegrityError(
                "settlement source event does not match rule determination time"
            )
        if settlement.available_at <= settlement.source_event_at:
            raise FixtureFormatError("settlement availability must be after its source event")
        order_expires_at = decision.order_expires_at
        if order_expires_at is None:
            raise FixtureIntegrityError("rule-bound settlement requires a frozen order expiration")
        last_pre_settlement_time = max(
            decision.decision_cutoff,
            decision.strategy_decision_at,
            decision.authorization_at,
            execution.opened_at,
            execution.filled_at,
            execution.canceled_at,
            order_expires_at,
        )
        if settlement.available_at <= last_pre_settlement_time:
            raise FixtureFormatError(
                "settlement must be unavailable through cancellation and expiration"
            )
        if settlement.available_at > decision.logical_time:
            raise FixtureFormatError(
                "settlement availability cannot follow the frozen logical time"
            )
        settlement_value = Decimal(settlement.settlement_value)
        payout_unit = Decimal(decision.instrument.payout_unit)
        if settlement_value < 0 or settlement_value > payout_unit or settlement_value > Decimal(1):
            raise FixtureFormatError("settlement value must be bounded by the payout unit")
        if isinstance(settlement, ClassifiedRuleBoundSettlementFixture):
            if not isinstance(decision.contract_rule, VoidContractRuleFixture):
                raise FixtureIntegrityError(
                    "classified VOID settlement requires an exact VOID rule term"
                )
            rule_term = decision.contract_rule.void_settlement_term
            if settlement.outcome_classification != "VOID":
                raise FixtureIntegrityError("settlement outcome classification must be VOID")
            if settlement_value <= 0:
                raise FixtureFormatError("VOID settlement value must be nonzero")
            if (
                settlement.outcome_classification != rule_term.outcome_classification
                or settlement.settlement_value != rule_term.settlement_value
            ):
                raise FixtureIntegrityError(
                    "settlement classification and value do not match the exact VOID rule term"
                )
            if settlement_value != Decimal(decision.instrument.void_settlement_value):
                raise FixtureIntegrityError(
                    "settlement value must equal the instrument VOID settlement value"
                )
            expected_raw_payload = (
                f"resolution:{settlement.outcome_classification};"
                f"settlement_value={settlement.settlement_value};"
                f"instrument={settlement.instrument_id};"
                f"rule={settlement.rule_version_id};"
                f"rule_hash={settlement.rule_content_hash};"
                f"source={settlement.resolution_source}"
            )
            if settlement.raw_payload != expected_raw_payload:
                raise FixtureIntegrityError(
                    "settlement raw payload does not match its authenticated fields"
                )
        elif settlement_value != Decimal(decision.instrument.no_settlement_value):
            raise FixtureIntegrityError(
                "settlement value must equal the instrument NO settlement value"
            )
        if settlement.available_at - settlement.source_event_at != timedelta(seconds=3):
            raise FixtureIntegrityError(
                "settlement availability lag does not match the registered fixture"
            )
