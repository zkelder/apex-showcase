from __future__ import annotations

import ast
import sys
import tomllib
from collections.abc import Iterator
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src" / "apex"
DOMAIN_ROOT = SOURCE_ROOT / "domain"
APPLICATION_ROOT = SOURCE_ROOT / "application"
MIGRATIONS_ROOT = ROOT / "migrations"
# The only ``apex`` surface a migration may import from -- the same narrow,
# DDL-only adapter helpers (``immutable_trigger_ddl``, table/column
# definitions, ...) every migration from 0001 has used.  Never
# ``apex.domain``/``apex.application``: see
# ``test_migrations_never_import_mutable_domain_or_application_modules``.
_MIGRATION_ALLOWED_APEX_ROOTS = ("apex.adapters",)
POSTGRES_ADAPTER = SOURCE_ROOT / "adapters" / "postgres.py"
_POSTGRES_ADAPTER_IMPORT_ROOTS = frozenset({"os", "psycopg", "sqlalchemy"})
KALSHI_PUBLIC_ADAPTER = SOURCE_ROOT / "adapters" / "kalshi_public.py"
_KALSHI_PUBLIC_ADAPTER_IMPORT_ROOTS = frozenset({"http", "socket", "urllib"})
ALPACA_MARKET_DATA_ADAPTER = SOURCE_ROOT / "adapters" / "alpaca_market_data.py"
_ALPACA_MARKET_DATA_ADAPTER_IMPORT_ROOTS = frozenset({"http", "os", "ssl", "urllib"})
ALPACA_PAPER_BROKER_ADAPTER = SOURCE_ROOT / "adapters" / "alpaca_paper_broker.py"
_ALPACA_PAPER_BROKER_ADAPTER_IMPORT_ROOTS = frozenset({"os", "urllib"})
RESEARCH_EVIDENCE_SOURCE_ADAPTER = SOURCE_ROOT / "adapters" / "research_evidence_source.py"
_RESEARCH_EVIDENCE_SOURCE_ADAPTER_IMPORT_ROOTS = frozenset({"http", "ssl"})
OPTIONS_REFERENCE_SOURCE_ADAPTER = SOURCE_ROOT / "adapters" / "options_reference_source.py"
_OPTIONS_REFERENCE_SOURCE_ADAPTER_IMPORT_ROOTS = frozenset({"http", "ssl"})
CORPORATE_FILINGS_SOURCE_ADAPTER = SOURCE_ROOT / "adapters" / "corporate_filings_source.py"
_CORPORATE_FILINGS_SOURCE_ADAPTER_IMPORT_ROOTS = frozenset({"http", "ssl"})
COINBASE_PUBLIC_ADAPTER = SOURCE_ROOT / "adapters" / "coinbase_public.py"
COINBASE_UNIVERSE_PUBLIC_ADAPTER = SOURCE_ROOT / "adapters" / "coinbase_universe_public.py"
COINBASE_STATEFUL_PUBLIC_ADAPTER = SOURCE_ROOT / "adapters" / "crypto_stateful_research_sources.py"
_COINBASE_PUBLIC_ADAPTER_IMPORT_ROOTS = frozenset({"http", "ssl", "urllib"})
COINBASE_PRIVATE_READ_ADAPTER = SOURCE_ROOT / "adapters" / "coinbase_read_only.py"
_COINBASE_PRIVATE_READ_ADAPTER_IMPORT_ROOTS = frozenset({"cryptography", "http", "os", "ssl"})
COINBASE_PREVIEW_ADAPTER = SOURCE_ROOT / "adapters" / "coinbase_provider_preview.py"
_COINBASE_PREVIEW_ADAPTER_IMPORT_ROOTS = frozenset({"cryptography", "http", "ssl"})
KALSHI_PRIVATE_READ_ADAPTER = SOURCE_ROOT / "adapters" / "kalshi_read_only.py"
_KALSHI_PRIVATE_READ_ADAPTER_IMPORT_ROOTS = frozenset({"cryptography", "http", "os", "ssl"})
XNYS_CALENDAR_ADAPTER = SOURCE_ROOT / "adapters" / "xnys_calendar.py"
_XNYS_CALENDAR_ADAPTER_IMPORT_ROOTS = frozenset({"importlib", "pandas_market_calendars"})
OPERATOR_API = SOURCE_ROOT / "operator_api.py"
_OPERATOR_API_IMPORT_ROOTS = frozenset({"fastapi", "pydantic"})
STRATEGY_LAB_APPLICATION = APPLICATION_ROOT / "strategy_lab.py"
STRATEGY_LAB_ARTIFACT_ADAPTER = SOURCE_ROOT / "adapters" / "strategy_lab_artifacts.py"
RESEARCH_INTEGRITY_ARTIFACT_ADAPTER = SOURCE_ROOT / "adapters" / "research_integrity_artifacts.py"
CANDIDATE_GENERATION_TOOL_ADAPTER = SOURCE_ROOT / "adapters" / "candidate_generation_tools.py"
OPERATIONAL_BACKUP_ADAPTER = SOURCE_ROOT / "adapters" / "operational_backup.py"
STRATEGY_LAB_SHADOW_MODEL_ADAPTER = SOURCE_ROOT / "adapters" / "strategy_lab_shadow_models.py"
SECURITIES_BASELINE_SIMULATOR_ADAPTER = (
    SOURCE_ROOT / "adapters" / "securities_baseline_simulators.py"
)
TYPED_POSTGRES_ADAPTERS = frozenset(
    {
        SOURCE_ROOT / "adapters" / "securities_baselines_postgres.py",
        SOURCE_ROOT / "adapters" / "securities_paper_postgres.py",
        SOURCE_ROOT / "adapters" / "securities_postgres.py",
        SOURCE_ROOT / "adapters" / "research_integrity_postgres.py",
        SOURCE_ROOT / "adapters" / "candidate_generation_postgres.py",
        SOURCE_ROOT / "adapters" / "candidate_generation_demo.py",
        SOURCE_ROOT / "adapters" / "batch_learning_postgres.py",
        SOURCE_ROOT / "adapters" / "batch_learning_operations.py",
        SOURCE_ROOT / "adapters" / "batch_learning_demo.py",
        SOURCE_ROOT / "adapters" / "real_securities_postgres.py",
        SOURCE_ROOT / "adapters" / "strategy_lab_event_markets.py",
        SOURCE_ROOT / "adapters" / "strategy_lab_postgres.py",
        SOURCE_ROOT / "adapters" / "securities_strategy_lab_dataset_postgres.py",
        SOURCE_ROOT / "adapters" / "securities_strategy_intelligence_postgres.py",
        SOURCE_ROOT / "adapters" / "securities_ensemble_postgres.py",
        SOURCE_ROOT / "adapters" / "sustained_shadow_postgres.py",
        SOURCE_ROOT / "adapters" / "external_paper_postgres.py",
        SOURCE_ROOT / "adapters" / "portfolio_intelligence_postgres.py",
        SOURCE_ROOT / "adapters" / "sustained_shadow_operations_postgres.py",
        SOURCE_ROOT / "adapters" / "research_evidence_postgres.py",
        SOURCE_ROOT / "adapters" / "crypto_research_postgres.py",
        SOURCE_ROOT / "adapters" / "crypto_dossier_postgres.py",
        SOURCE_ROOT / "adapters" / "crypto_challenger_postgres.py",
        SOURCE_ROOT / "adapters" / "crypto_campaign_postgres.py",
        SOURCE_ROOT / "adapters" / "crypto_campaign_candidate_postgres.py",
        SOURCE_ROOT / "adapters" / "crypto_campaign_generic_postgres.py",
        SOURCE_ROOT / "adapters" / "crypto_universe_postgres.py",
        SOURCE_ROOT / "adapters" / "crypto_market_pulse_postgres.py",
        SOURCE_ROOT / "adapters" / "shared_opportunity_postgres.py",
        SOURCE_ROOT / "adapters" / "shared_opportunity_sources.py",
        SOURCE_ROOT / "adapters" / "total_market_context_postgres.py",
        SOURCE_ROOT / "adapters" / "total_market_context_sources.py",
        SOURCE_ROOT / "adapters" / "historical_total_market_context_sources.py",
        SOURCE_ROOT / "adapters" / "operating_cycle_postgres.py",
        SOURCE_ROOT / "adapters" / "operating_cycle_tasks.py",
        SOURCE_ROOT / "adapters" / "crypto_paper_postgres.py",
        SOURCE_ROOT / "adapters" / "kalshi_operating_postgres.py",
        SOURCE_ROOT / "adapters" / "cross_venue_allocation_postgres.py",
        SOURCE_ROOT / "adapters" / "performance_attribution_postgres.py",
        SOURCE_ROOT / "adapters" / "performance_attribution_sources.py",
        SOURCE_ROOT / "adapters" / "research_operations_postgres.py",
        SOURCE_ROOT / "adapters" / "research_operations_sources.py",
        SOURCE_ROOT / "adapters" / "first_generated_research_method_sources.py",
        SOURCE_ROOT / "adapters" / "first_generated_research_method_acceptance.py",
        SOURCE_ROOT / "adapters" / "crypto_stateful_research_postgres.py",
        SOURCE_ROOT / "adapters" / "generated_crypto_challenger_acceptance.py",
        SOURCE_ROOT / "adapters" / "live_readiness_postgres.py",
        SOURCE_ROOT / "adapters" / "live_readiness_acceptance.py",
        SOURCE_ROOT / "adapters" / "live_preflight_postgres.py",
        SOURCE_ROOT / "adapters" / "operational_reliability_postgres.py",
        SOURCE_ROOT / "adapters" / "operational_reliability_acceptance.py",
        SOURCE_ROOT / "adapters" / "coinbase_read_only_postgres.py",
        SOURCE_ROOT / "adapters" / "coinbase_read_only_acceptance.py",
        SOURCE_ROOT / "adapters" / "kalshi_read_only_postgres.py",
        SOURCE_ROOT / "adapters" / "kalshi_read_only_acceptance.py",
        SOURCE_ROOT / "adapters" / "operational_backup.py",
        SOURCE_ROOT / "adapters" / "security_reference_postgres.py",
        SOURCE_ROOT / "adapters" / "broad_equity_postgres.py",
        SOURCE_ROOT / "adapters" / "options_intelligence_postgres.py",
        SOURCE_ROOT / "adapters" / "security_identity_postgres.py",
        SOURCE_ROOT / "adapters" / "historical_market_data_postgres.py",
        SOURCE_ROOT / "adapters" / "historical_replay_postgres.py",
        SOURCE_ROOT / "adapters" / "research_methodology_postgres.py",
        SOURCE_ROOT / "adapters" / "genuine_multi_asset_history_postgres.py",
        SOURCE_ROOT / "adapters" / "cross_market_research_postgres.py",
        SOURCE_ROOT / "adapters" / "research_calibration_postgres.py",
        SOURCE_ROOT / "adapters" / "research_calibration_v2_postgres.py",
        SOURCE_ROOT / "adapters" / "research_replication_postgres.py",
        SOURCE_ROOT / "adapters" / "options_quote_intelligence_postgres.py",
        SOURCE_ROOT / "adapters" / "options_structures_postgres.py",
        SOURCE_ROOT / "adapters" / "options_research_operations_postgres.py",
        SOURCE_ROOT / "adapters" / "corporate_fundamentals_postgres.py",
        SOURCE_ROOT / "adapters" / "corporate_market_research_postgres.py",
        SOURCE_ROOT / "adapters" / "corporate_calibration_depth_postgres.py",
        SOURCE_ROOT / "adapters" / "corporate_regime_followup_postgres.py",
        SOURCE_ROOT / "adapters" / "portfolio_state_postgres.py",
        SOURCE_ROOT / "adapters" / "portfolio_state_sources.py",
        SOURCE_ROOT / "adapters" / "investment_mandate_postgres.py",
        SOURCE_ROOT / "adapters" / "counterfactual_ledger_postgres.py",
        SOURCE_ROOT / "adapters" / "counterfactual_followup_postgres.py",
        SOURCE_ROOT / "adapters" / "counterfactual_outcome_postgres.py",
        SOURCE_ROOT / "adapters" / "counterfactual_outcome_sources.py",
        SOURCE_ROOT / "adapters" / "kalshi_baseline_quote_postgres.py",
        SOURCE_ROOT / "adapters" / "securities_structure_outcome_postgres.py",
        SOURCE_ROOT / "adapters" / "live_market_observation_postgres.py",
        SOURCE_ROOT / "adapters" / "kalshi_resolution_postgres.py",
        SOURCE_ROOT / "adapters" / "external_strategy_research_postgres.py",
        SOURCE_ROOT / "adapters" / "strategy_evaluation_history_postgres.py",
        SOURCE_ROOT / "adapters" / "strategy_paper_trial_lifecycle_postgres.py",
        SOURCE_ROOT / "adapters" / "strategy_version_provenance_postgres.py",
        SOURCE_ROOT / "adapters" / "strategy_trial_outcome_assessment_postgres.py",
        SOURCE_ROOT / "adapters" / "evidence_recovery_postgres.py",
        SOURCE_ROOT / "adapters" / "schema_compatibility_postgres.py",
    }
)
_STRATEGY_LAB_ML_IMPORT_ROOTS = frozenset({"joblib", "numpy", "sklearn"})
_SECURITIES_SIMULATOR_IMPORT_ROOTS = frozenset({"pandas", "vectorbt"})
_CANDIDATE_GENERATION_TOOL_IMPORT_ROOTS = frozenset({"mlflow", "optuna", "os"})
_OPERATIONAL_BACKUP_IMPORT_ROOTS = frozenset({"psycopg", "sqlalchemy", "subprocess"})

_DOMAIN_IO_ROOTS = frozenset(
    {
        "ctypes",
        "ftplib",
        "http",
        "imaplib",
        "io",
        "os",
        "pathlib",
        "pickle",
        "poplib",
        "shelve",
        "smtplib",
        "socket",
        "sqlite3",
        "ssl",
        "subprocess",
        "tempfile",
        "urllib",
        "webbrowser",
        "xmlrpc",
    }
)
_RUNTIME_CAPABILITY_ROOTS = frozenset(
    {
        "_socket",
        "ctypes",
        "ftplib",
        "http",
        "importlib",
        "imaplib",
        "os",
        "pkg_resources",
        "pkgutil",
        "poplib",
        "requests",
        "smtplib",
        "socket",
        "ssl",
        "subprocess",
        "urllib",
        "webbrowser",
        "websockets",
        "xmlrpc",
    }
)
_FORBIDDEN_SOURCE_PARTS = frozenset(
    {
        "broker",
        "brokers",
        "credential",
        "credentials",
        "exchange",
        "exchanges",
        "llm",
        "provider",
        "providers",
        "secret",
        "secrets",
        "signer",
        "signing",
        "wallet",
        "wallets",
    }
)


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py"))) if root.is_dir() else ()


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT / "src").with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_name(path: Path) -> str:
    module = _module_name(path)
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def _imports(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package_name(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = "." * node.level + (node.module or "")
                yield node.lineno, resolve_name(relative, package)
            elif node.module is not None:
                yield node.lineno, node.module


def _format_violations(violations: list[tuple[Path, int, str]]) -> str:
    return "\n".join(
        f"{path.relative_to(ROOT)}:{line}: {detail}" for path, line, detail in violations
    )


def _migration_imports(path: Path) -> Iterator[tuple[int, str]]:
    """Like ``_imports`` above, but for ``migrations/`` rather than
    ``src/apex/``: Alembic loads each migration as a standalone module, not
    as part of a package, so there is no enclosing package to resolve a
    relative import against the way ``_imports`` resolves one inside
    ``apex.domain``/``apex.application``. No migration has ever used one
    (checked below by treating any ``node.level`` as a raw, unresolved dotted
    name -- e.g. ``.foo`` -- which cannot match an allowed root and so always
    surfaces as a violation rather than being silently skipped)."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, ("." * node.level) + (node.module or "")


def test_domain_imports_only_pure_standard_library_and_domain_modules() -> None:
    files = _python_files(DOMAIN_ROOT)
    assert files, "the golden slice must contain an apex.domain package"

    violations: list[tuple[Path, int, str]] = []
    for path in files:
        for line, imported in _imports(path):
            root = imported.partition(".")[0]
            if root in _DOMAIN_IO_ROOTS:
                violations.append((path, line, f"domain imports I/O-capable module {imported!r}"))
            elif imported == "apex" or (
                imported.startswith("apex.") and not imported.startswith("apex.domain")
            ):
                violations.append((path, line, f"domain crosses its boundary via {imported!r}"))
            elif root != "apex" and root != "__future__" and root not in sys.stdlib_module_names:
                violations.append((path, line, f"domain imports third-party module {imported!r}"))

    assert not violations, _format_violations(violations)


def test_application_does_not_depend_on_adapters() -> None:
    files = _python_files(APPLICATION_ROOT)
    assert files, "the golden slice must contain an apex.application package"

    violations = [
        (path, line, f"application imports adapter module {imported!r}")
        for path in files
        for line, imported in _imports(path)
        if imported == "apex.adapters" or imported.startswith("apex.adapters.")
    ]
    assert not violations, _format_violations(violations)


def test_migrations_never_import_mutable_domain_or_application_modules() -> None:
    """A migration may import the standard library, ``alembic``,
    ``sqlalchemy``, and ``apex.adapters.*`` (the narrow DDL-only surface
    every migration from 0001 has used) -- and nothing else from ``apex``.
    In particular, never ``apex.domain`` or ``apex.application``: a fresh
    ``0001 -> head`` chain resolves a migration's imports against *today's*
    application code, not the code that existed when that migration was
    authored, so importing evolvable domain/application logic would make a
    historical migration's behavior silently depend on how live code has
    changed since -- rather than staying pinned to what it computed the day
    it shipped.  This is exactly the defect Migration 0095 Historical
    Reproducibility Hardening v1 found (``0095_reason_classification`` was
    the only migration among 0001-0095 that imported from ``apex.domain``)
    and fixed by freezing that migration's formulas as migration-local code
    -- see that migration's own module docstring. This structural test is
    what would have caught the original defect, and is what keeps it, or
    anything like it, from recurring in any future migration."""

    files = _python_files(MIGRATIONS_ROOT)
    assert files, "the golden slice must contain a migrations package"

    violations: list[tuple[Path, int, str]] = []
    for path in files:
        for line, imported in _migration_imports(path):
            root = imported.partition(".")[0]
            if root != "apex":
                if (
                    root not in {"alembic", "sqlalchemy", "__future__"}
                    and root not in sys.stdlib_module_names
                ):
                    violations.append(
                        (path, line, f"migration imports third-party module {imported!r}")
                    )
                continue
            is_allowed_adapter_import = any(
                imported == allowed or imported.startswith(f"{allowed}.")
                for allowed in _MIGRATION_ALLOWED_APEX_ROOTS
            )
            if not is_allowed_adapter_import:
                violations.append(
                    (path, line, f"migration imports non-adapter apex module {imported!r}")
                )

    assert not violations, _format_violations(violations)


def test_runtime_has_no_third_party_or_external_execution_dependencies() -> None:
    files = _python_files(SOURCE_ROOT)
    assert files, "the golden slice source package is missing"

    violations: list[tuple[Path, int, str]] = []
    for path in files:
        for line, imported in _imports(path):
            root = imported.partition(".")[0]
            if path == POSTGRES_ADAPTER and root in _POSTGRES_ADAPTER_IMPORT_ROOTS:
                continue
            if path == KALSHI_PUBLIC_ADAPTER and root in _KALSHI_PUBLIC_ADAPTER_IMPORT_ROOTS:
                continue
            if (
                path == ALPACA_MARKET_DATA_ADAPTER
                and root in _ALPACA_MARKET_DATA_ADAPTER_IMPORT_ROOTS
            ):
                continue
            if (
                path == ALPACA_PAPER_BROKER_ADAPTER
                and root in _ALPACA_PAPER_BROKER_ADAPTER_IMPORT_ROOTS
            ):
                continue
            if (
                path == RESEARCH_EVIDENCE_SOURCE_ADAPTER
                and root in _RESEARCH_EVIDENCE_SOURCE_ADAPTER_IMPORT_ROOTS
            ):
                continue
            if (
                path == OPTIONS_REFERENCE_SOURCE_ADAPTER
                and root in _OPTIONS_REFERENCE_SOURCE_ADAPTER_IMPORT_ROOTS
            ):
                continue
            if (
                path == CORPORATE_FILINGS_SOURCE_ADAPTER
                and root in _CORPORATE_FILINGS_SOURCE_ADAPTER_IMPORT_ROOTS
            ):
                continue
            if (
                path
                in {
                    COINBASE_PUBLIC_ADAPTER,
                    COINBASE_UNIVERSE_PUBLIC_ADAPTER,
                    COINBASE_STATEFUL_PUBLIC_ADAPTER,
                }
                and root in _COINBASE_PUBLIC_ADAPTER_IMPORT_ROOTS
            ):
                continue
            if (
                path == COINBASE_PRIVATE_READ_ADAPTER
                and root in _COINBASE_PRIVATE_READ_ADAPTER_IMPORT_ROOTS
            ):
                continue
            if path == COINBASE_PREVIEW_ADAPTER and root in _COINBASE_PREVIEW_ADAPTER_IMPORT_ROOTS:
                continue
            if (
                path == KALSHI_PRIVATE_READ_ADAPTER
                and root in _KALSHI_PRIVATE_READ_ADAPTER_IMPORT_ROOTS
            ):
                continue
            if path == XNYS_CALENDAR_ADAPTER and root in _XNYS_CALENDAR_ADAPTER_IMPORT_ROOTS:
                continue
            if path == OPERATOR_API and root in _OPERATOR_API_IMPORT_ROOTS:
                continue
            if path == STRATEGY_LAB_APPLICATION and root in _STRATEGY_LAB_ML_IMPORT_ROOTS:
                continue
            if path == STRATEGY_LAB_SHADOW_MODEL_ADAPTER and root in _STRATEGY_LAB_ML_IMPORT_ROOTS:
                continue
            if path == STRATEGY_LAB_ARTIFACT_ADAPTER and root == "os":
                continue
            if path == RESEARCH_INTEGRITY_ARTIFACT_ADAPTER and root == "os":
                continue
            if (
                path == CANDIDATE_GENERATION_TOOL_ADAPTER
                and root in _CANDIDATE_GENERATION_TOOL_IMPORT_ROOTS
            ):
                continue
            if (
                path == SECURITIES_BASELINE_SIMULATOR_ADAPTER
                and root in _SECURITIES_SIMULATOR_IMPORT_ROOTS
            ):
                continue
            if path in TYPED_POSTGRES_ADAPTERS and root == "sqlalchemy":
                continue
            if path == OPERATIONAL_BACKUP_ADAPTER and root in _OPERATIONAL_BACKUP_IMPORT_ROOTS:
                continue
            if root in _RUNTIME_CAPABILITY_ROOTS:
                violations.append(
                    (path, line, f"runtime imports forbidden capability module {imported!r}")
                )
            elif root != "apex" and root != "__future__" and root not in sys.stdlib_module_names:
                violations.append((path, line, f"runtime imports third-party module {imported!r}"))

    assert not violations, _format_violations(violations)

    with (ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)
    assert project["project"].get("dependencies", []) == [
        "cryptography>=50,<51",
        "fastapi>=0.115,<1",
        "flask==3.1.3",
        "flask-cors==6.0.1",
        "gunicorn==23.0.0",
        "mlflow-skinny==3.14.0",
        "optuna==4.9.0",
        "pandas-market-calendars==5.2.0",
        "pydantic>=2.10,<3",
        "scikit-learn==1.6.1",
        "uvicorn>=0.34,<1",
        "vectorbt==1.1.0",
    ]
    assert project["project"]["optional-dependencies"]["persistence"] == [
        "alembic>=1.14,<2",
        "psycopg[binary]>=3.2,<4",
        "sqlalchemy>=2.0,<3",
    ]


def test_only_postgres_adapter_imports_persistence_packages() -> None:
    violations = [
        (path, line, f"persistence dependency escapes PostgreSQL adapter via {imported!r}")
        for path in _python_files(SOURCE_ROOT)
        for line, imported in _imports(path)
        if imported.partition(".")[0] in {"alembic", "psycopg", "sqlalchemy"}
        and path != POSTGRES_ADAPTER
        and not (
            path == OPERATIONAL_BACKUP_ADAPTER
            and imported.partition(".")[0] in {"psycopg", "sqlalchemy"}
        )
        and not (path in TYPED_POSTGRES_ADAPTERS and imported.partition(".")[0] == "sqlalchemy")
    ]
    assert not violations, _format_violations(violations)


def test_typed_dependency_exceptions_are_exact_and_local() -> None:
    assert {
        imported.partition(".")[0]
        for _line, imported in _imports(STRATEGY_LAB_APPLICATION)
        if imported.partition(".")[0] in _STRATEGY_LAB_ML_IMPORT_ROOTS
    } == _STRATEGY_LAB_ML_IMPORT_ROOTS
    assert {
        path.name
        for path in TYPED_POSTGRES_ADAPTERS
        if any(imported.partition(".")[0] == "sqlalchemy" for _line, imported in _imports(path))
    } == {
        "research_integrity_postgres.py",
        "candidate_generation_postgres.py",
        "candidate_generation_demo.py",
        "batch_learning_postgres.py",
        "batch_learning_operations.py",
        "batch_learning_demo.py",
        "real_securities_postgres.py",
        "securities_baselines_postgres.py",
        "securities_paper_postgres.py",
        "securities_postgres.py",
        "securities_strategy_intelligence_postgres.py",
        "securities_ensemble_postgres.py",
        "sustained_shadow_postgres.py",
        "strategy_lab_event_markets.py",
        "strategy_lab_postgres.py",
        "securities_strategy_lab_dataset_postgres.py",
        "external_paper_postgres.py",
        "portfolio_intelligence_postgres.py",
        "sustained_shadow_operations_postgres.py",
        "research_evidence_postgres.py",
        "crypto_research_postgres.py",
        "crypto_dossier_postgres.py",
        "crypto_challenger_postgres.py",
        "crypto_campaign_postgres.py",
        "crypto_campaign_candidate_postgres.py",
        "crypto_campaign_generic_postgres.py",
        "crypto_market_pulse_postgres.py",
        "crypto_universe_postgres.py",
        "shared_opportunity_postgres.py",
        "shared_opportunity_sources.py",
        "total_market_context_postgres.py",
        "total_market_context_sources.py",
        "operating_cycle_postgres.py",
        "operating_cycle_tasks.py",
        "crypto_paper_postgres.py",
        "kalshi_operating_postgres.py",
        "cross_venue_allocation_postgres.py",
        "performance_attribution_postgres.py",
        "performance_attribution_sources.py",
        "research_operations_postgres.py",
        "research_operations_sources.py",
        "first_generated_research_method_sources.py",
        "first_generated_research_method_acceptance.py",
        "crypto_stateful_research_postgres.py",
        "generated_crypto_challenger_acceptance.py",
        "live_readiness_postgres.py",
        "live_readiness_acceptance.py",
        "live_preflight_postgres.py",
        "operational_reliability_postgres.py",
        "operational_reliability_acceptance.py",
        "coinbase_read_only_postgres.py",
        "coinbase_read_only_acceptance.py",
        "kalshi_read_only_postgres.py",
        "kalshi_read_only_acceptance.py",
        "operational_backup.py",
        "security_reference_postgres.py",
        "broad_equity_postgres.py",
        "options_intelligence_postgres.py",
        "security_identity_postgres.py",
        "historical_market_data_postgres.py",
        "historical_replay_postgres.py",
        "historical_total_market_context_sources.py",
        "research_methodology_postgres.py",
        "genuine_multi_asset_history_postgres.py",
        "cross_market_research_postgres.py",
        "research_calibration_postgres.py",
        "research_calibration_v2_postgres.py",
        "research_replication_postgres.py",
        "options_quote_intelligence_postgres.py",
        "options_structures_postgres.py",
        "options_research_operations_postgres.py",
        "corporate_fundamentals_postgres.py",
        "corporate_market_research_postgres.py",
        "corporate_calibration_depth_postgres.py",
        "corporate_regime_followup_postgres.py",
        "portfolio_state_postgres.py",
        "portfolio_state_sources.py",
        "investment_mandate_postgres.py",
        "counterfactual_ledger_postgres.py",
        "counterfactual_followup_postgres.py",
        "counterfactual_outcome_postgres.py",
        "counterfactual_outcome_sources.py",
        "kalshi_baseline_quote_postgres.py",
        "securities_structure_outcome_postgres.py",
        "live_market_observation_postgres.py",
        "kalshi_resolution_postgres.py",
        "external_strategy_research_postgres.py",
        "strategy_evaluation_history_postgres.py",
        "strategy_paper_trial_lifecycle_postgres.py",
        "strategy_version_provenance_postgres.py",
        "strategy_trial_outcome_assessment_postgres.py",
        "evidence_recovery_postgres.py",
        "schema_compatibility_postgres.py",
    }
    assert {
        imported.partition(".")[0]
        for _line, imported in _imports(SECURITIES_BASELINE_SIMULATOR_ADAPTER)
        if imported.partition(".")[0] in _SECURITIES_SIMULATOR_IMPORT_ROOTS
    } == _SECURITIES_SIMULATOR_IMPORT_ROOTS
    assert {
        imported.partition(".")[0]
        for _line, imported in _imports(CANDIDATE_GENERATION_TOOL_ADAPTER)
        if imported.partition(".")[0] in _CANDIDATE_GENERATION_TOOL_IMPORT_ROOTS
    } == _CANDIDATE_GENERATION_TOOL_IMPORT_ROOTS


def test_runtime_has_no_dynamic_loading_or_future_capability_packages() -> None:
    violations: list[tuple[Path, int, str]] = []
    for path in _python_files(SOURCE_ROOT):
        forbidden_parts = _FORBIDDEN_SOURCE_PARTS.intersection(
            part.lower() for part in path.relative_to(SOURCE_ROOT).parts
        )
        for part in sorted(forbidden_parts):
            violations.append((path, 1, f"forbidden future-capability path component {part!r}"))

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = ast.unparse(node.func)
            if call_name == "__import__" or call_name.endswith(".import_module"):
                violations.append((path, node.lineno, f"dynamic import via {call_name!r}"))
            if call_name.endswith(".entry_points") or call_name.endswith(".iter_entry_points"):
                violations.append((path, node.lineno, f"plugin discovery via {call_name!r}"))

    assert not violations, _format_violations(violations)
