"""Bounded PostgreSQL logical backup and disposable restore verification."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import sqlalchemy as sa
from psycopg import sql
from sqlalchemy.engine import Engine

from apex.adapters.live_readiness_acceptance import financial_counts
from apex.adapters.operational_reliability_postgres import (
    PostgresOperationalReliabilityRepository,
)
from apex.adapters.postgres import PostgresSettings
from apex.domain.operational_reliability import (
    BackupManifest,
    OperationalReliabilityError,
    RestoreVerification,
    VerificationStatus,
)
from apex.domain.values import content_digest


class PostgresLogicalBackupDrill:
    """Uses the local Compose PostgreSQL tools and never replaces the active DB."""

    def __init__(
        self,
        *,
        engine: Engine,
        settings: PostgresSettings,
        repository: PostgresOperationalReliabilityRepository,
        repository_root: Path,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._repository = repository
        self._repository_root = repository_root.resolve()
        self._backup_root = (
            self._repository_root / ".local/operational/backups" / self._settings.database
        ).resolve()
        if not self._backup_root.is_relative_to(self._repository_root):
            raise OperationalReliabilityError("BACKUP_ROOT_OUTSIDE_REPOSITORY")

    @staticmethod
    def _schema_revision(engine: Engine) -> str:
        with engine.connect() as connection:
            value = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        if value is None:
            raise OperationalReliabilityError("BACKUP_SCHEMA_REVISION_MISSING")
        return str(value)

    @staticmethod
    def _table_counts(
        engine: Engine, names: tuple[str, ...] | None = None
    ) -> tuple[tuple[str, int], ...]:
        inspected = sa.inspect(engine)
        tables = tuple(sorted(names or tuple(inspected.get_table_names())))
        retained: list[tuple[str, int]] = []
        with engine.connect() as connection:
            for table in tables:
                if table == "alembic_version":
                    continue
                if table not in inspected.get_table_names():
                    retained.append((table, -1))
                    continue
                quoted = connection.dialect.identifier_preparer.quote(table)
                count = int(connection.scalar(sa.text(f"SELECT count(*) FROM {quoted}")) or 0)
                retained.append((table, count))
        return tuple(retained)

    @staticmethod
    def _evidence_hash(engine: Engine, names: tuple[str, ...] | None = None) -> str:
        inspected = sa.inspect(engine)
        tables = tuple(sorted(names or tuple(inspected.get_table_names())))
        values: list[tuple[str, tuple[str, ...]]] = []
        with engine.connect() as connection:
            for table in tables:
                if table not in inspected.get_table_names():
                    values.append((table, ("MISSING",)))
                    continue
                columns = {item["name"] for item in inspected.get_columns(table)}
                hash_column = next(
                    (
                        candidate
                        for candidate in (
                            "canonical_hash",
                            "canonical_payload_hash",
                            "domain_content_hash",
                        )
                        if candidate in columns
                    ),
                    None,
                )
                if hash_column is None:
                    continue
                quoted_table = connection.dialect.identifier_preparer.quote(table)
                quoted_column = connection.dialect.identifier_preparer.quote(hash_column)
                rows = connection.execute(
                    sa.text(
                        f"SELECT {quoted_column} FROM {quoted_table} "
                        f"WHERE {quoted_column} IS NOT NULL ORDER BY {quoted_column}"
                    )
                ).scalars()
                values.append((table, tuple(str(item) for item in rows)))
        return content_digest(values)

    def _restore_database_name(self, manifest_id: str) -> str:
        suffix = hashlib.sha256(manifest_id.encode()).hexdigest()[:12]
        return f"apex_restore_{suffix}"

    def _database_admin(self, database_name: str, *, create: bool) -> None:
        with (
            psycopg.connect(
                host=self._settings.host,
                port=self._settings.port,
                dbname="postgres",
                user=self._settings.user,
                password=self._settings.password,
                autocommit=True,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
            )
            if create:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                        sql.Identifier(database_name)
                    )
                )

    def _dump(self, target: Path) -> None:
        with target.open("wb") as output:
            result = subprocess.run(
                (
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    self._settings.user,
                    "-d",
                    self._settings.database,
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                ),
                cwd=self._repository_root,
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
        if result.returncode != 0:
            target.unlink(missing_ok=True)
            raise OperationalReliabilityError("POSTGRES_LOGICAL_BACKUP_FAILED")

    def _restore(self, source: Path, database_name: str) -> None:
        with source.open("rb") as backup:
            result = subprocess.run(
                (
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    "pg_restore",
                    "-U",
                    self._settings.user,
                    "-d",
                    database_name,
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                ),
                cwd=self._repository_root,
                stdin=backup,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
        if result.returncode != 0:
            raise OperationalReliabilityError("POSTGRES_DISPOSABLE_RESTORE_FAILED")

    def execute(
        self,
        *,
        manifest_id: str,
        verification_id: str,
        performed_at: datetime,
    ) -> tuple[BackupManifest, RestoreVerification, bool]:
        retained_manifest = self._repository.backup_manifest(manifest_id)
        retained_verification = self._repository.restore_verification(verification_id)
        if retained_manifest is not None and retained_verification is not None:
            return retained_manifest, retained_verification, True
        if retained_verification is not None:
            raise OperationalReliabilityError("BACKUP_REPLAY_LINEAGE_INCOMPLETE")

        self._backup_root.mkdir(parents=True, exist_ok=True)
        target = (self._backup_root / f"{manifest_id}.dump").resolve()
        if target.parent != self._backup_root:
            raise OperationalReliabilityError("BACKUP_TARGET_PATH_INVALID")
        financial_before = financial_counts(self._engine)
        if retained_manifest is None:
            source_revision = self._schema_revision(self._engine)
            source_counts = self._table_counts(self._engine)
            source_tables = tuple(name for name, _ in source_counts)
            source_hash = self._evidence_hash(self._engine, source_tables)
            self._dump(target)
            backup_bytes = target.read_bytes()
            manifest = BackupManifest.create(
                manifest_id=manifest_id,
                schema_revision=source_revision,
                source_database_identity=self._settings.database,
                backup_timestamp=performed_at,
                storage_reference=str(target.relative_to(self._repository_root)),
                file_size_bytes=len(backup_bytes),
                sha256=hashlib.sha256(backup_bytes).hexdigest(),
                table_counts=source_counts,
                evidence_exclusions=(
                    "DECRYPTED_PROVIDER_SECRETS_NEVER_AVAILABLE_TO_APEX",
                    "LOCAL_ENVIRONMENT_VALUES",
                    "OPERATING_SYSTEM_SECRET_VALUES",
                ),
                secret_handling="OPAQUE_CREDENTIAL_REFERENCES_ONLY_NO_DECRYPTED_SECRET_VALUES",
                selected_evidence_hash=source_hash,
            )
            self._repository.save_backup_manifest(manifest)
        else:
            manifest = retained_manifest
            backup_bytes = target.read_bytes() if target.is_file() else b""
            if (
                len(backup_bytes) != manifest.file_size_bytes
                or hashlib.sha256(backup_bytes).hexdigest() != manifest.sha256
            ):
                raise OperationalReliabilityError("RETAINED_BACKUP_ARTIFACT_MISMATCH")
            source_revision = manifest.schema_revision
            source_counts = manifest.table_counts
            source_tables = tuple(name for name, _ in source_counts)
            source_hash = manifest.selected_evidence_hash

        restore_database = self._restore_database_name(manifest_id)
        failures: list[str] = []
        restored_revision: str | None = None
        restored_hash: str | None = None
        comparisons: tuple[tuple[str, int], ...] = ()
        destroyed = False
        started_at = performed_at
        try:
            self._database_admin(restore_database, create=True)
            self._restore(target, restore_database)
            restored_engine = sa.create_engine(
                self._settings.sqlalchemy_url(database=restore_database),
                pool_pre_ping=True,
                hide_parameters=True,
            )
            try:
                restored_revision = self._schema_revision(restored_engine)
                restored_counts = dict(self._table_counts(restored_engine, source_tables))
                comparisons = tuple(
                    (name, restored_counts.get(name, -1) - expected)
                    for name, expected in source_counts
                )
                restored_hash = self._evidence_hash(restored_engine, source_tables)
                if restored_revision != source_revision:
                    failures.append("RESTORED_SCHEMA_REVISION_MISMATCH")
                if any(delta != 0 for _, delta in comparisons):
                    failures.append("RESTORED_TABLE_COUNT_MISMATCH")
                if restored_hash != source_hash:
                    failures.append("RESTORED_EVIDENCE_HASH_MISMATCH")
            finally:
                restored_engine.dispose()
        except (OperationalReliabilityError, psycopg.Error, sa.exc.SQLAlchemyError):
            failures.append("DISPOSABLE_RESTORE_EXECUTION_FAILED")
        finally:
            try:
                self._database_admin(restore_database, create=False)
                destroyed = True
            except psycopg.Error:
                failures.append("DISPOSABLE_DATABASE_CLEANUP_FAILED")

        financial_after = financial_counts(self._engine)
        active_unchanged = financial_before == financial_after
        if not active_unchanged:
            failures.append("ACTIVE_DATABASE_FINANCIAL_COUNTS_CHANGED")
        status = (
            VerificationStatus.VERIFIED if not failures and destroyed else VerificationStatus.FAILED
        )
        verification = RestoreVerification.create(
            verification_id=verification_id,
            manifest_id=manifest_id,
            disposable_database_identity=restore_database,
            expected_schema_revision=source_revision,
            restored_schema_revision=restored_revision,
            protected_count_comparisons=comparisons,
            restored_evidence_hash=restored_hash,
            status=status,
            failures=tuple(sorted(set(failures))),
            started_at=started_at,
            completed_at=max(datetime.now(tz=UTC), started_at),
            disposable_database_destroyed=destroyed,
            active_database_unchanged=active_unchanged,
        )
        self._repository.save_restore_verification(verification)
        return manifest, verification, False
