"""Managed local Securities Stage 1 data and deterministic calendar adapters."""

from __future__ import annotations

import csv
import json
import stat
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from apex.application.securities import SecuritiesCalendarPort, SecuritiesManagedDataset
from apex.domain.securities import (
    SecuritiesAssetClass,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesCalendarSession,
    SecuritiesCorporateActionEvidence,
    SecuritiesCorporateActionType,
    SecuritiesError,
    SecuritiesInstrument,
)
from apex.domain.values import canonical_json_bytes, canonical_timestamp


class SecuritiesManagedDataError(SecuritiesError):
    """Managed data is missing, unsafe, malformed, or has changed."""


class LocalSecuritiesDatasetAdapter:
    """Immutable JSONL evidence rooted beneath ``.local/securities/data``."""

    MAX_SOURCE_BYTES = 25 * 1024 * 1024
    MAX_RECORDS = 250_000
    _ALLOWED_SUFFIXES = frozenset((".jsonl", ".csv"))

    def __init__(self, root: Path | None = None) -> None:
        configured = root or Path(".local") / "securities" / "data"
        self._root = configured.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise SecuritiesManagedDataError("managed securities data root is invalid")

    @property
    def root(self) -> Path:
        return self._root

    def load_managed_source(
        self, *, managed_source: str, dataset_name: str
    ) -> SecuritiesManagedDataset:
        source = self._target(managed_source)
        payload = self._read_bounded_regular_file(source)
        records = (
            self._parse_jsonl(payload) if source.suffix == ".jsonl" else self._parse_csv(payload)
        )
        normalized = tuple(self._normalize_record(record) for record in records)
        canonical_payload = b"".join(canonical_json_bytes(record) + b"\n" for record in normalized)
        canonical_sha = sha256(canonical_payload).hexdigest()
        canonical_relative = f"canonical/{canonical_sha}.jsonl"
        self._write_immutable(canonical_relative, canonical_payload)
        instruments: list[SecuritiesInstrument] = []
        sessions: list[SecuritiesCalendarSession] = []
        bars: list[SecuritiesBarEvidence] = []
        actions: list[SecuritiesCorporateActionEvidence] = []
        for record in normalized:
            record_type = _required_string(record, "record_type")
            if record_type == "instrument":
                instruments.append(self._instrument(record))
            elif record_type == "calendar_session":
                sessions.append(self._session(record))
            elif record_type == "bar":
                bars.append(self._bar(record))
            elif record_type == "corporate_action":
                actions.append(self._action(record))
            else:
                raise SecuritiesManagedDataError(
                    f"unsupported securities record_type: {record_type}"
                )
        return SecuritiesManagedDataset(
            source_artifact=managed_source,
            source_sha256=sha256(payload).hexdigest(),
            source_byte_size=len(payload),
            canonical_artifact=canonical_relative,
            canonical_sha256=canonical_sha,
            canonical_byte_size=len(canonical_payload),
            instruments=tuple(instruments),
            sessions=tuple(sessions),
            bars=tuple(bars),
            corporate_actions=tuple(actions),
        )

    def verify_batch_artifact(self, batch: object) -> None:
        relative = getattr(batch, "canonical_artifact", None)
        expected_hash = getattr(batch, "canonical_sha256", None)
        expected_size = getattr(batch, "canonical_byte_size", None)
        if (
            not isinstance(relative, str)
            or not isinstance(expected_hash, str)
            or not isinstance(expected_size, int)
        ):
            raise SecuritiesManagedDataError("batch artifact metadata is invalid")
        payload = self._read_bounded_regular_file(self._target(relative))
        if len(payload) != expected_size or sha256(payload).hexdigest() != expected_hash:
            raise SecuritiesManagedDataError(
                "managed securities artifact hash does not match durable evidence"
            )

    def write_generated_jsonl(
        self, *, relative_location: str, records: Iterable[Mapping[str, object]]
    ) -> str:
        """Write an Apex-generated fixture without accepting an arbitrary path."""

        normalized = tuple(self._normalize_record(dict(record)) for record in records)
        payload = b"".join(canonical_json_bytes(record) + b"\n" for record in normalized)
        self._write_immutable(relative_location, payload)
        return relative_location

    def inventory(self) -> tuple[dict[str, object], ...]:
        """Return a bounded managed-data readiness inventory without importing it."""

        result: list[dict[str, object]] = []
        for path in sorted(self._root.rglob("*")):
            if len(result) >= 1_000:
                break
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self._root).as_posix()
            try:
                payload = self._read_bounded_regular_file(path)
            except SecuritiesManagedDataError:
                continue
            result.append(
                {
                    "relative_location": relative,
                    "byte_size": len(payload),
                    "sha256": sha256(payload).hexdigest(),
                    "canonical": relative.startswith("canonical/"),
                }
            )
        return tuple(result)

    def _target(self, relative_location: str) -> Path:
        relative = PurePosixPath(relative_location)
        if (
            not relative_location
            or relative.is_absolute()
            or ".." in relative.parts
            or any(part in ("", ".") for part in relative.parts)
            or relative.suffix not in self._ALLOWED_SUFFIXES
        ):
            raise SecuritiesManagedDataError(
                "managed source must be a safe relative JSONL or CSV path"
            )
        target = (self._root / Path(*relative.parts)).resolve(strict=False)
        if not target.is_relative_to(self._root):
            raise SecuritiesManagedDataError("managed source escapes the data root")
        return target

    def _read_bounded_regular_file(self, path: Path) -> bytes:
        if not path.exists() or path.is_symlink() or not path.is_file():
            raise SecuritiesManagedDataError("managed securities source is unavailable")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise SecuritiesManagedDataError("managed securities source is not a regular file")
        size = path.stat().st_size
        if size <= 0 or size > self.MAX_SOURCE_BYTES:
            raise SecuritiesManagedDataError("managed securities source exceeds the size bound")
        return path.read_bytes()

    def _write_immutable(self, relative_location: str, payload: bytes) -> None:
        if not payload or len(payload) > self.MAX_SOURCE_BYTES:
            raise SecuritiesManagedDataError("canonical securities artifact exceeds the size bound")
        target = self._target(relative_location)
        if target.exists() or target.is_symlink():
            existing = self._read_bounded_regular_file(target)
            if existing != payload:
                raise SecuritiesManagedDataError(
                    "immutable managed securities location contains different evidence"
                )
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        self._assert_parent_chain(target.parent)
        temporary_name: str | None = None
        try:
            with NamedTemporaryFile(
                dir=target.parent, prefix=".apex-securities-", delete=False
            ) as handle:
                temporary_name = handle.name
                handle.write(payload)
                handle.flush()
            temporary = Path(temporary_name)
            if temporary.is_symlink() or not temporary.is_file():
                raise SecuritiesManagedDataError("managed temporary artifact is invalid")
            temporary.replace(target)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def _assert_parent_chain(self, parent: Path) -> None:
        current = parent
        while current != self._root:
            if current.is_symlink():
                raise SecuritiesManagedDataError("managed securities parent may not be a symlink")
            current = current.parent

    def _parse_jsonl(self, payload: bytes) -> tuple[dict[str, object], ...]:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SecuritiesManagedDataError("managed JSONL must be UTF-8") from error
        records: list[dict[str, object]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                decoded = json.loads(line, parse_float=_reject_json_float)
            except (json.JSONDecodeError, SecuritiesManagedDataError) as error:
                raise SecuritiesManagedDataError(
                    f"managed JSONL line {line_number} is invalid"
                ) from error
            if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
                raise SecuritiesManagedDataError("managed JSONL records must be objects")
            records.append(decoded)
            if len(records) > self.MAX_RECORDS:
                raise SecuritiesManagedDataError("managed dataset exceeds the record bound")
        if not records:
            raise SecuritiesManagedDataError("managed dataset is empty")
        return tuple(records)

    def _parse_csv(self, payload: bytes) -> tuple[dict[str, object], ...]:
        try:
            lines = payload.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise SecuritiesManagedDataError("managed CSV must be UTF-8") from error
        reader = csv.DictReader(lines)
        if reader.fieldnames is None or "record_type" not in reader.fieldnames:
            raise SecuritiesManagedDataError("managed CSV requires a record_type column")
        records: list[dict[str, object]] = []
        for row in reader:
            records.append(
                {
                    key: value
                    for key, value in row.items()
                    if key is not None and value not in (None, "")
                }
            )
            if len(records) > self.MAX_RECORDS:
                raise SecuritiesManagedDataError("managed dataset exceeds the record bound")
        if not records:
            raise SecuritiesManagedDataError("managed dataset is empty")
        return tuple(records)

    def _normalize_record(self, record: Mapping[str, object]) -> dict[str, object]:
        record_type = _required_string(record, "record_type")
        allowed = _RECORD_FIELDS.get(record_type)
        if allowed is None:
            raise SecuritiesManagedDataError(f"unsupported securities record_type: {record_type}")
        unknown = set(record) - allowed
        if unknown:
            raise SecuritiesManagedDataError(
                f"unsupported {record_type} fields: {', '.join(sorted(unknown))}"
            )
        normalized = {key: record[key] for key in sorted(record)}
        if any(isinstance(value, float) for value in normalized.values()):
            raise SecuritiesManagedDataError("authoritative securities values cannot be floats")
        return normalized

    def _instrument(self, record: Mapping[str, object]) -> SecuritiesInstrument:
        return SecuritiesInstrument(
            instrument_id=_required_string(record, "instrument_id"),
            symbol=_required_string(record, "symbol"),
            asset_class=SecuritiesAssetClass(_required_string(record, "asset_class")),
            exchange_mic=_required_string(record, "exchange_mic"),
            currency=_required_string(record, "currency"),
            timezone=_required_string(record, "timezone"),
            valid_from=_required_string(record, "valid_from"),
            valid_to=_optional_string(record, "valid_to"),
            source_identity=_required_string(record, "source_identity"),
            content_hash=_optional_string(record, "content_hash"),
        )

    def _session(self, record: Mapping[str, object]) -> SecuritiesCalendarSession:
        session = SecuritiesCalendarSession.create(
            calendar_identity=_required_string(record, "calendar_identity"),
            calendar_version=_required_string(record, "calendar_version"),
            timezone=_required_string(record, "timezone"),
            session_date=_required_string(record, "session_date"),
            opens_at=_timestamp(record, "opens_at"),
            closes_at=_timestamp(record, "closes_at"),
            is_early_close=_required_bool(record, "is_early_close"),
            source_identity=_required_string(record, "source_identity"),
        )
        supplied_id = _optional_string(record, "session_id")
        supplied_hash = _optional_string(record, "content_hash")
        if supplied_id is not None and supplied_id != session.session_id:
            raise SecuritiesManagedDataError(
                "calendar session identifier does not match canonical evidence"
            )
        if supplied_hash is not None and supplied_hash != session.content_hash:
            raise SecuritiesManagedDataError(
                "calendar session hash does not match canonical evidence"
            )
        return session

    def _bar(self, record: Mapping[str, object]) -> SecuritiesBarEvidence:
        return SecuritiesBarEvidence(
            instrument_id=_required_string(record, "instrument_id"),
            interval=SecuritiesBarInterval(_required_string(record, "interval")),
            adjustment_state=_required_string(record, "adjustment_state"),
            period_start=_timestamp(record, "period_start"),
            period_end=_timestamp(record, "period_end"),
            available_at=_timestamp(record, "available_at"),
            open=_authoritative_scalar(record, "open"),
            high=_authoritative_scalar(record, "high"),
            low=_authoritative_scalar(record, "low"),
            close=_authoritative_scalar(record, "close"),
            volume=_authoritative_scalar(record, "volume"),
            source_identity=_required_string(record, "source_identity"),
            bar_id=_optional_string(record, "bar_id"),
            content_hash=_optional_string(record, "content_hash"),
        )

    def _action(self, record: Mapping[str, object]) -> SecuritiesCorporateActionEvidence:
        return SecuritiesCorporateActionEvidence(
            instrument_id=_required_string(record, "instrument_id"),
            action_type=SecuritiesCorporateActionType(_required_string(record, "action_type")),
            effective_date=_required_string(record, "effective_date"),
            available_at=_timestamp(record, "available_at"),
            split_ratio=_optional_scalar(record, "split_ratio"),
            cash_amount=_optional_scalar(record, "cash_amount"),
            successor_symbol=_optional_string(record, "successor_symbol"),
            source_identity=_required_string(record, "source_identity"),
            action_id=_optional_string(record, "action_id"),
            content_hash=_optional_string(record, "content_hash"),
        )


class RetainedSecuritiesCalendar(SecuritiesCalendarPort):
    """Query one immutable set of retained calendar sessions."""

    def __init__(self, sessions: Sequence[SecuritiesCalendarSession]) -> None:
        self._sessions = tuple(sorted(sessions, key=lambda item: item.session_date))

    def sessions_between(
        self,
        *,
        calendar_identity: str,
        start_date: str,
        end_date: str,
    ) -> tuple[SecuritiesCalendarSession, ...]:
        return tuple(
            session
            for session in self._sessions
            if session.calendar_identity == calendar_identity
            and start_date <= session.session_date <= end_date
        )

    def is_session(self, *, calendar_identity: str, session_date: str) -> bool:
        return any(
            session.calendar_identity == calendar_identity and session.session_date == session_date
            for session in self._sessions
        )

    def previous_session(
        self, *, calendar_identity: str, session_date: str
    ) -> SecuritiesCalendarSession | None:
        candidates = self.sessions_between(
            calendar_identity=calendar_identity,
            start_date="0001-01-01",
            end_date=session_date,
        )
        strict = [session for session in candidates if session.session_date < session_date]
        return strict[-1] if strict else None

    def next_session(
        self, *, calendar_identity: str, session_date: str
    ) -> SecuritiesCalendarSession | None:
        strict = [
            session
            for session in self._sessions
            if session.calendar_identity == calendar_identity
            and session.session_date > session_date
        ]
        return strict[0] if strict else None


_RECORD_FIELDS = {
    "instrument": frozenset(
        {
            "record_type",
            "instrument_id",
            "symbol",
            "asset_class",
            "exchange_mic",
            "currency",
            "timezone",
            "valid_from",
            "valid_to",
            "source_identity",
            "content_hash",
        }
    ),
    "calendar_session": frozenset(
        {
            "record_type",
            "session_id",
            "calendar_identity",
            "calendar_version",
            "timezone",
            "session_date",
            "opens_at",
            "closes_at",
            "is_early_close",
            "source_identity",
            "content_hash",
        }
    ),
    "bar": frozenset(
        {
            "record_type",
            "bar_id",
            "instrument_id",
            "interval",
            "adjustment_state",
            "period_start",
            "period_end",
            "available_at",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "source_identity",
            "content_hash",
        }
    ),
    "corporate_action": frozenset(
        {
            "record_type",
            "action_id",
            "instrument_id",
            "action_type",
            "effective_date",
            "available_at",
            "split_ratio",
            "cash_amount",
            "successor_symbol",
            "source_identity",
            "content_hash",
        }
    ),
}


def securities_record(value: object) -> dict[str, object]:
    """Render one domain item as canonical managed JSONL source data."""

    if isinstance(value, SecuritiesInstrument):
        return {
            "record_type": "instrument",
            "instrument_id": value.instrument_id,
            "symbol": value.symbol,
            "asset_class": value.asset_class,
            "exchange_mic": value.exchange_mic,
            "currency": value.currency,
            "timezone": value.timezone,
            "valid_from": value.valid_from,
            "valid_to": value.valid_to,
            "source_identity": value.source_identity,
            "content_hash": value.content_hash,
        }
    if isinstance(value, SecuritiesCalendarSession):
        return {
            "record_type": "calendar_session",
            "session_id": value.session_id,
            "calendar_identity": value.calendar_identity,
            "calendar_version": value.calendar_version,
            "timezone": value.timezone,
            "session_date": value.session_date,
            "opens_at": canonical_timestamp(value.opens_at),
            "closes_at": canonical_timestamp(value.closes_at),
            "is_early_close": value.is_early_close,
            "source_identity": value.source_identity,
            "content_hash": value.content_hash,
        }
    if isinstance(value, SecuritiesBarEvidence):
        return {
            "record_type": "bar",
            "bar_id": value.bar_id,
            "instrument_id": value.instrument_id,
            "interval": value.interval,
            "adjustment_state": value.adjustment_state,
            "period_start": canonical_timestamp(value.period_start),
            "period_end": canonical_timestamp(value.period_end),
            "available_at": canonical_timestamp(value.available_at),
            "open": value.open,
            "high": value.high,
            "low": value.low,
            "close": value.close,
            "volume": value.volume,
            "source_identity": value.source_identity,
            "content_hash": value.content_hash,
        }
    if isinstance(value, SecuritiesCorporateActionEvidence):
        return {
            "record_type": "corporate_action",
            "action_id": value.action_id,
            "instrument_id": value.instrument_id,
            "action_type": value.action_type,
            "effective_date": value.effective_date,
            "available_at": canonical_timestamp(value.available_at),
            "split_ratio": value.split_ratio,
            "cash_amount": value.cash_amount,
            "successor_symbol": value.successor_symbol,
            "source_identity": value.source_identity,
            "content_hash": value.content_hash,
        }
    raise SecuritiesManagedDataError("unsupported generated securities record")


def _reject_json_float(_value: str) -> object:
    raise SecuritiesManagedDataError(
        "JSON numeric fractions are not authoritative; use fixed-point strings"
    )


def _required_string(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise SecuritiesManagedDataError(f"{key} must be a non-empty string")
    return value


def _optional_string(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SecuritiesManagedDataError(f"{key} must be a non-empty string when supplied")
    return value


def _required_bool(record: Mapping[str, object], key: str) -> bool:
    value = record.get(key)
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    raise SecuritiesManagedDataError(f"{key} must be a boolean")


def _timestamp(record: Mapping[str, object], key: str) -> datetime:
    value = _required_string(record, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SecuritiesManagedDataError(f"{key} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SecuritiesManagedDataError(f"{key} must be timezone-aware")
    return parsed


def _authoritative_scalar(record: Mapping[str, object], key: str) -> str | int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SecuritiesManagedDataError(f"{key} must be a fixed-point string or integer")
    return value


def _optional_scalar(record: Mapping[str, object], key: str) -> str | int | None:
    value = record.get(key)
    if value is None:
        return None
    return _authoritative_scalar(record, key)
