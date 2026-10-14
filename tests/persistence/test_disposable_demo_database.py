from __future__ import annotations

from dataclasses import replace

import pytest
import sqlalchemy as sa
from alembic import command

from apex.adapters.postgres import PostgresSettings, create_postgres_engine
from scripts import dev as dev_script

pytestmark = pytest.mark.postgres


def _settings(postgres_settings: PostgresSettings, token: str) -> PostgresSettings:
    return replace(
        postgres_settings,
        database=dev_script.disposable_demo_database_name(process_id=731, token=token),
    )


def test_disposable_demo_database_names_and_environment_are_bounded(
    postgres_settings: PostgresSettings,
) -> None:
    first = dev_script.disposable_demo_database_name(process_id=731, token="alpha1")
    second = dev_script.disposable_demo_database_name(process_id=731, token="beta2")
    assert first != second
    assert first.startswith("apex_demo_")
    with pytest.raises(dev_script.DisposableDemoDatabaseError):
        dev_script._validate_disposable_demo_database_name("apex")
    with pytest.raises(dev_script.DisposableDemoDatabaseError):
        dev_script._validate_disposable_demo_database_name("apex_test")

    settings = _settings(postgres_settings, "envcheck")
    environment = dev_script._database_environment(settings)
    assert environment["APEX_POSTGRES_HOST"] == "127.0.0.1"
    # Not a hardcoded "55432": a linked git worktree may legitimately run its
    # own isolated local Postgres on a different port (see
    # scripts/dev-worktree-env.sh). What this asserts is that the child
    # environment faithfully mirrors whatever port `settings` actually
    # resolved from `PostgresSettings.from_env()`, not a specific literal.
    assert environment["APEX_POSTGRES_PORT"] == str(settings.port)
    assert environment["APEX_POSTGRES_DB"] == settings.database
    assert environment["APEX_POSTGRES_TEST_DB"] == postgres_settings.test_database


def test_empty_disposable_database_migrates_and_preflights_without_engine_split(
    postgres_settings: PostgresSettings,
) -> None:
    settings = _settings(postgres_settings, "migration")
    protected_before = dev_script._protected_database_state(postgres_settings)
    normal_engine = create_postgres_engine(postgres_settings)
    dev_script._create_disposable_demo_database(settings)
    try:
        with normal_engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT current_database()"))
                == postgres_settings.database
            )
        dev_script._migrate_disposable_demo_database(settings)
        # Tracks the true current alembic head, not a fixed historical marker --
        # update alongside migrations/versions/ when a new head lands (currently
        # 0108_kalshi_baseline_v2).
        assert (
            dev_script._schema_preflight_disposable_demo_database(settings)
            == "0118_evidence_gap_recovery"
        )
        disposable_engine = create_postgres_engine(settings)
        try:
            with disposable_engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT current_database()")) == settings.database
                tables = set(sa.inspect(connection).get_table_names())
                assert dev_script._DISPOSABLE_DEMO_REQUIRED_TABLES.issubset(tables)
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "0118_evidence_gap_recovery"
                )
        finally:
            disposable_engine.dispose()
    finally:
        normal_engine.dispose()
        dev_script._drop_disposable_demo_database(settings)
    assert not dev_script._database_exists(settings, settings.database)
    assert dev_script._protected_database_state(postgres_settings) == protected_before


def test_stamped_unmigrated_disposable_database_fails_preflight_before_demo_seed(
    postgres_settings: PostgresSettings,
) -> None:
    settings = _settings(postgres_settings, "stamped")
    dev_script._create_disposable_demo_database(settings)
    try:
        engine = create_postgres_engine(settings)
        try:
            with engine.connect() as connection:
                command.stamp(dev_script._alembic_config(connection), "head")
                connection.commit()
        finally:
            engine.dispose()
        with pytest.raises(
            dev_script.DisposableDemoSchemaPreflightError,
            match=r"lifecycle_stage=schema_preflight.*operator_action",
        ) as error:
            dev_script._schema_preflight_disposable_demo_database(settings)
        assert error.value.observed_revisions == ("0118_evidence_gap_recovery",)
        assert "operator_action" in error.value.missing_tables
    finally:
        dev_script._drop_disposable_demo_database(settings)
    assert not dev_script._database_exists(settings, settings.database)


def test_cleanup_targets_only_the_exact_disposable_database(
    postgres_settings: PostgresSettings,
) -> None:
    first = _settings(postgres_settings, "cleanupone")
    second = _settings(postgres_settings, "cleanuptwo")
    protected_before = dev_script._protected_database_state(postgres_settings)
    dev_script._create_disposable_demo_database(first)
    dev_script._create_disposable_demo_database(second)
    try:
        dev_script._drop_disposable_demo_database(first)
        assert not dev_script._database_exists(first, first.database)
        assert dev_script._database_exists(second, second.database)
        assert dev_script._protected_database_state(postgres_settings) == protected_before
    finally:
        dev_script._drop_disposable_demo_database(second)


@pytest.mark.parametrize("was_running", (True, False))
def test_demo_wrapper_propagates_one_database_and_preserves_postgres_state(
    monkeypatch: pytest.MonkeyPatch,
    was_running: bool,
) -> None:
    stages: list[tuple[str, str | None]] = []

    monkeypatch.setattr(dev_script, "_postgres_was_running", lambda: was_running)
    monkeypatch.setattr(dev_script, "db_up", lambda: stages.append(("db_up", None)))
    monkeypatch.setattr(
        dev_script,
        "db_down",
        lambda *, volumes: stages.append(("db_down", str(volumes))),
    )
    monkeypatch.setattr(
        dev_script, "disposable_demo_database_name", lambda: "apex_demo_731_wrapper"
    )
    monkeypatch.setattr(
        dev_script,
        "_protected_database_state",
        lambda value: (("apex", 1, ""), ("apex_test", 2, "")),
    )
    monkeypatch.setattr(
        dev_script,
        "_create_disposable_demo_database",
        lambda value: stages.append(("create", value.database)),
    )
    monkeypatch.setattr(
        dev_script,
        "_migrate_disposable_demo_database",
        lambda value: stages.append(("migrate", value.database)),
    )
    monkeypatch.setattr(
        dev_script,
        "_schema_preflight_disposable_demo_database",
        lambda value: "0094_counterfactual_ledger",
    )
    monkeypatch.setattr(
        dev_script,
        "_drop_disposable_demo_database",
        lambda value: stages.append(("drop", value.database)),
    )
    monkeypatch.setattr(dev_script, "_database_exists", lambda value, name: False)

    def run(arguments, *, environment=None) -> None:
        assert environment is not None
        stages.append(("demo", environment["APEX_POSTGRES_DB"]))

    monkeypatch.setattr(dev_script, "_run", run)
    dev_script.evidence_review_paper_trial_demo()

    assert ("migrate", "apex_demo_731_wrapper") in stages
    assert ("demo", "apex_demo_731_wrapper") in stages
    assert ("drop", "apex_demo_731_wrapper") in stages
    assert ("db_down", "False") in stages if not was_running else ("db_down", "False") not in stages
