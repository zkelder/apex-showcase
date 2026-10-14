"""Trusted local artifact storage for immutable Strategy Lab research outputs.

The store intentionally accepts only generated relative identifiers.  It is not
an upload or model-import mechanism: callers cannot point it at arbitrary files
and model deserialization remains confined to Apex-created artifacts.
"""

from __future__ import annotations

import os
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from apex.application.strategy_lab import (
    StrategyLabArtifact,
    StrategyLabArtifactError,
    StrategyLabArtifactStore,
)


class LocalStrategyLabArtifactStore(StrategyLabArtifactStore):
    """Immutable, bounded artifacts rooted beneath ``.local/strategy_lab``."""

    # Genuine Securities rows retain every contributing RAW-bar identity.  The
    # bounded 128 MiB ceiling accommodates that immutable lineage while still
    # preventing this local-only store from becoming an arbitrary bulk sink.
    _MAX_BYTES = 128 * 1024 * 1024
    _ALLOWED_SUFFIXES = frozenset((".json", ".jsonl", ".joblib"))

    def __init__(self, root: Path | None = None) -> None:
        configured = root or Path(".local") / "strategy_lab" / "artifacts"
        self._root = configured.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise StrategyLabArtifactError("managed artifact root is invalid")

    @property
    def root(self) -> Path:
        return self._root

    def write_bytes(self, relative_location: str, payload: bytes) -> StrategyLabArtifact:
        target = self._target(relative_location)
        if not payload or len(payload) > self._MAX_BYTES:
            raise StrategyLabArtifactError("artifact byte size is outside the allowed bound")
        artifact = StrategyLabArtifact(
            relative_location=relative_location,
            sha256=sha256(payload).hexdigest(),
            byte_size=len(payload),
        )
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise StrategyLabArtifactError("immutable artifact location already exists")
            # Content-addressed replay may safely reuse an already committed
            # immutable artifact, but no different payload may claim its path.
            self.read_bytes(artifact)
            return artifact
        target.parent.mkdir(parents=True, exist_ok=True)
        self._assert_parent_chain(target.parent)
        temporary_name: str | None = None
        try:
            with NamedTemporaryFile(dir=target.parent, prefix=".apex-", delete=False) as handle:
                temporary_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary = Path(temporary_name)
            if temporary.is_symlink() or not temporary.is_file():
                raise StrategyLabArtifactError("artifact temporary file is invalid")
            os.replace(temporary, target)
        except OSError as error:
            raise StrategyLabArtifactError("immutable artifact write failed") from error
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return artifact

    def read_bytes(self, artifact: StrategyLabArtifact) -> bytes:
        target = self._target(artifact.relative_location)
        if not target.exists() or target.is_symlink() or not target.is_file():
            raise StrategyLabArtifactError("trusted artifact is unavailable")
        mode = target.stat().st_mode
        if not stat.S_ISREG(mode):
            raise StrategyLabArtifactError("trusted artifact is not a regular file")
        payload = target.read_bytes()
        if len(payload) != artifact.byte_size or sha256(payload).hexdigest() != artifact.sha256:
            raise StrategyLabArtifactError("trusted artifact hash does not match durable metadata")
        return payload

    def exists(self, artifact: StrategyLabArtifact) -> bool:
        try:
            self.read_bytes(artifact)
        except StrategyLabArtifactError:
            return False
        return True

    def _target(self, relative_location: str) -> Path:
        path = PurePosixPath(relative_location)
        if (
            not relative_location
            or path.is_absolute()
            or ".." in path.parts
            or any(part in ("", ".") for part in path.parts)
            or path.suffix not in self._ALLOWED_SUFFIXES
        ):
            raise StrategyLabArtifactError("artifact location must be a safe managed relative path")
        target = (self._root / Path(*path.parts)).resolve(strict=False)
        if not target.is_relative_to(self._root):
            raise StrategyLabArtifactError("artifact location escapes the managed root")
        return target

    def _assert_parent_chain(self, parent: Path) -> None:
        current = parent
        while current != self._root:
            if current.is_symlink():
                raise StrategyLabArtifactError("artifact parent may not be a symlink")
            current = current.parent
