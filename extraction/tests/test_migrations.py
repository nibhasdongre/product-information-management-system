"""Tracked, incremental migrations. Each test builds its own empty schema (see conftest.fresh_schema),
and the applied-migrations table lives inside that schema, so nothing here touches a real project."""
import shutil

import psycopg
import pytest
from psycopg import sql

from pipeline import db

FILES = db.migration_files()
INIT = FILES[0]                           # (version, name, path) of the real first schema


def recorded(conn) -> list[str]:
    _, table = db._tracking_table()
    return [r[0] for r in conn.execute(sql.SQL("select version from {} order by version").format(table))]


def tables(conn) -> set[str]:
    rows = conn.execute("select table_name from information_schema.tables where table_schema = current_schema()")
    return {r[0] for r in rows}


def folder(tmp_path, **files):
    """A migrations folder holding the real first schema plus the given extra files."""
    directory = tmp_path / "migrations"
    directory.mkdir(parents=True)
    shutil.copy(INIT[2], directory / INIT[2].name)
    for name, body in files.items():
        (directory / f"{name}.sql").write_text(body, encoding="utf-8")
    return directory


def test_file_names_are_checked(tmp_path):
    folder(tmp_path, bad_name="select 1;")
    with pytest.raises(ValueError, match="bad migration file name"):
        db.migration_files(tmp_path / "migrations")
    twin = folder(tmp_path / "twin", **{"20261109000000_a": "select 1;", "20261109000000_b": "select 1;"})
    with pytest.raises(ValueError, match="share the version"):
        db.migration_files(twin)


def test_a_fresh_database_gets_each_migration_once_and_it_is_recorded(fresh_schema):
    messages = db.migrate(fresh_schema)
    assert messages == [f"applied {path.name}" for _, _, path in FILES]
    assert db.check(fresh_schema)["missing"] == [] and db.check(fresh_schema)["no_rls"] == []
    assert recorded(fresh_schema) == [version for version, _, _ in FILES]
    assert db.migrate(fresh_schema) == []                      # nothing left to do


def test_a_database_from_before_tracking_is_baselined_not_rebuilt(fresh_schema):
    db.apply_migration(fresh_schema, INIT[2])                  # built the old way: tables exist, nothing recorded
    fresh_schema.execute("insert into brand (brand_name) values ('Kept')")
    messages = db.migrate(fresh_schema)
    assert messages[0].startswith("baseline")
    assert messages[1:] == [f"applied {path.name}" for _, _, path in FILES[1:]]    # the rest still apply
    assert recorded(fresh_schema) == [version for version, _, _ in FILES]
    assert fresh_schema.execute("select count(*) from brand").fetchone()[0] == 1     # data untouched
    assert db.migrate(fresh_schema) == []


def test_a_half_built_schema_is_refused(fresh_schema):
    fresh_schema.execute("create table brand (brand_id int)")
    with pytest.raises(RuntimeError, match="only part of the first schema"):
        db.migrate(fresh_schema)
    assert recorded(fresh_schema) == []


def test_a_failing_migration_changes_nothing_and_is_retried_after_the_fix(fresh_schema, tmp_path):
    directory = folder(tmp_path, **{
        "20261109000000_add_note": "create table note (id int);",
        "20261110000000_broken": "create table half_done (id int); select 1 / 0;",
    })
    with pytest.raises(psycopg.errors.DivisionByZero):
        db.migrate(fresh_schema, directory)
    assert recorded(fresh_schema) == [INIT[0], "20261109000000"]       # earlier ones kept
    assert "note" in tables(fresh_schema) and "half_done" not in tables(fresh_schema)   # the broken one rolled back

    (directory / "20261110000000_broken.sql").write_text("create table half_done (id int);", encoding="utf-8")
    assert db.migrate(fresh_schema, directory) == ["applied 20261110000000_broken.sql"]
    assert recorded(fresh_schema) == [INIT[0], "20261109000000", "20261110000000"]


def test_an_applied_migration_with_no_file_is_flagged(fresh_schema):
    db.migrate(fresh_schema)
    _, table = db._tracking_table()
    fresh_schema.execute(sql.SQL("insert into {} (version, name) values ('20990101000000', 'from_elsewhere')").format(table))
    assert db.migrate(fresh_schema) == ["WARNING: migration 20990101000000 is applied but has no file here"]


def test_migrate_needs_an_autocommit_connection(fresh_schema):
    fresh_schema.autocommit = False
    with pytest.raises(ValueError, match="autocommit"):
        db.migrate(fresh_schema)
    fresh_schema.rollback()
    fresh_schema.autocommit = True
