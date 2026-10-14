"""Safe immutable JSONL artifacts for aligned research return evidence."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from apex.application.research_integrity import (
    ResearchIntegrityApplicationError,
    ResearchReturnSeriesArtifact,
    ResearchSeriesType,
)
from apex.domain.research_integrity import ResearchFrequency, ReturnObservation
from apex.domain.values import canonical_json_bytes, content_digest, deterministic_id, parse_decimal


class LocalResearchReturnArtifactStore:
    """Bounded non-executable evidence rooted beneath ``.local/research-integrity``."""

    _MAX_BYTES = 32 * 1024 * 1024

    def __init__(self, root: Path | None = None) -> None:
        configured = root or Path(".local") / "research-integrity" / "return-series"
        self._root = configured.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_ROOT_INVALID")

    @property
    def root(self) -> Path:
        return self._root

    def write(
        self,
        *,
        experiment_id: str,
        trial_id: str,
        series_type: ResearchSeriesType,
        frequency: ResearchFrequency,
        observations: Sequence[ReturnObservation],
        return_policy_hash: str,
        missing_period_policy: str,
    ) -> ResearchReturnSeriesArtifact:
        if not observations:
            raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_EMPTY")
        payload = b"".join(
            canonical_json_bytes(
                {
                    "period_id": item.period_id,
                    "value": format(item.value, "f"),
                    "group_id": item.group_id,
                    "instrument_id": item.instrument_id,
                }
            )
            + b"\n"
            for item in observations
        )
        if len(payload) > self._MAX_BYTES:
            raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_TOO_LARGE")
        digest = sha256(payload).hexdigest()
        relative_path = f"{experiment_id}/{trial_id}/{series_type.value.lower()}-{digest}.jsonl"
        target = self._target(relative_path)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or target.read_bytes() != payload:
                raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_IMMUTABILITY_CONFLICT")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._assert_parent_chain(target.parent)
            temporary_name: str | None = None
            try:
                with NamedTemporaryFile(
                    dir=target.parent, prefix=".apex-research-", delete=False
                ) as handle:
                    temporary_name = handle.name
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, target)
            except OSError as error:
                raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_WRITE_FAILED") from error
            finally:
                if temporary_name is not None:
                    Path(temporary_name).unlink(missing_ok=True)
        metadata = {
            "trial_id": trial_id,
            "series_type": series_type.value,
            "frequency": frequency.value,
            "start_period": observations[0].period_id,
            "end_period": observations[-1].period_id,
            "observation_count": len(observations),
            "relative_path": relative_path,
            "sha256": digest,
            "byte_size": len(payload),
            "return_policy_hash": return_policy_hash,
            "missing_period_policy": missing_period_policy,
        }
        return ResearchReturnSeriesArtifact(
            artifact_id=deterministic_id("research_return_series_artifact", metadata),
            trial_id=trial_id,
            series_type=series_type,
            frequency=frequency,
            start_period=observations[0].period_id,
            end_period=observations[-1].period_id,
            observation_count=len(observations),
            relative_path=relative_path,
            sha256=digest,
            byte_size=len(payload),
            return_policy_hash=return_policy_hash,
            missing_period_policy=missing_period_policy,
            canonical_result_hash=content_digest(metadata),
        )

    def read(self, artifact: ResearchReturnSeriesArtifact) -> tuple[ReturnObservation, ...]:
        target = self._target(artifact.relative_path)
        if not target.exists() or target.is_symlink() or not target.is_file():
            raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_UNAVAILABLE")
        if not stat.S_ISREG(target.stat().st_mode):
            raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_NOT_REGULAR")
        payload = target.read_bytes()
        if len(payload) != artifact.byte_size or sha256(payload).hexdigest() != artifact.sha256:
            raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_HASH_MISMATCH")
        try:
            values = tuple(
                ReturnObservation(
                    period_id=str(item["period_id"]),
                    value=parse_decimal(item["value"], allow_negative=True),
                    group_id=str(item["group_id"]),
                    instrument_id=str(item["instrument_id"]),
                )
                for line in payload.splitlines()
                for item in (json.loads(line),)
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_SCHEMA_INVALID") from error
        if (
            len(values) != artifact.observation_count
            or not values
            or values[0].period_id != artifact.start_period
            or values[-1].period_id != artifact.end_period
        ):
            raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_METADATA_MISMATCH")
        return values

    def _target(self, relative_path: str) -> Path:
        path = PurePosixPath(relative_path)
        if (
            not relative_path
            or path.is_absolute()
            or ".." in path.parts
            or any(part in ("", ".") for part in path.parts)
            or path.suffix != ".jsonl"
        ):
            raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_PATH_UNSAFE")
        target = (self._root / Path(*path.parts)).resolve(strict=False)
        if not target.is_relative_to(self._root):
            raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_PATH_ESCAPES_ROOT")
        return target

    def _assert_parent_chain(self, parent: Path) -> None:
        current = parent
        while current != self._root:
            if current.is_symlink():
                raise ResearchIntegrityApplicationError("RETURN_ARTIFACT_PARENT_SYMLINK")
            current = current.parent
