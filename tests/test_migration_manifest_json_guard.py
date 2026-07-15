from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "f2a6c3d9e14b_cast_manifest_json_to_jsonb_in_guard.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("manifest_json_guard_migration", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Dialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _Bind:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = _Dialect(dialect_name)


class _OperationRecorder:
    def __init__(self, dialect_name: str) -> None:
        self.bind = _Bind(dialect_name)
        self.statements: list[str] = []

    def get_bind(self) -> _Bind:
        return self.bind

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def test_manifest_json_guard_is_a_forward_revision_of_initial_schema() -> None:
    migration = _load_migration()

    assert migration.revision == "f2a6c3d9e14b"
    assert migration.down_revision == "8db5f0998fbf"


def test_upgrade_replaces_guard_and_compares_manifest_as_jsonb() -> None:
    migration = _load_migration()
    operations = _OperationRecorder("postgresql")
    migration.op = operations

    migration.upgrade()

    assert len(operations.statements) == 1
    statement = operations.statements[0]
    assert "CREATE OR REPLACE FUNCTION skillhub_guard_skill_version()" in statement
    assert (
        "NEW.manifest_json::jsonb IS DISTINCT FROM OLD.manifest_json::jsonb" in statement
    )
    assert "DROP FUNCTION" not in statement
    assert "CREATE TRIGGER" not in statement


def test_upgrade_is_a_noop_outside_postgresql() -> None:
    migration = _load_migration()
    operations = _OperationRecorder("sqlite")
    migration.op = operations

    migration.upgrade()

    assert operations.statements == []


def test_downgrade_restores_the_previous_guard_definition() -> None:
    migration = _load_migration()
    operations = _OperationRecorder("postgresql")
    migration.op = operations

    migration.downgrade()

    assert len(operations.statements) == 1
    statement = operations.statements[0]
    assert "NEW.manifest_json IS DISTINCT FROM OLD.manifest_json" in statement
    assert "manifest_json::jsonb" not in statement
