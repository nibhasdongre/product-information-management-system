"""Postgres connection (Supabase session pooler), schema helpers, and the batch lock.

The session pooler (port 5432) is required: advisory locks and `set search_path` both live
as long as the session, which the transaction pooler (port 6543) does not guarantee."""
from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql

EXPECTED_TABLES = (
    "brand", "category", "subcategory", "catalog", "batch_submissions", "page_extractions",
    "extraction_data", "extraction_attempts", "product", "tag", "product_tag", "review",
)
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


class LockHeld(RuntimeError):
    """Another process holds the batch lock."""


def connect(autocommit: bool = False) -> psycopg.Connection:
    """Open a connection. PIM_DB_SCHEMA (used by the tests) points the session at another schema."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (put it in the project-root .env).")
    conn = psycopg.connect(url, autocommit=True, connect_timeout=15)
    schema = os.environ.get("PIM_DB_SCHEMA")
    if schema:
        conn.execute(sql.SQL("set search_path to {}").format(sql.Identifier(schema)))
    conn.autocommit = autocommit
    return conn


@contextmanager
def advisory_lock(name: str = "batch") -> Iterator[None]:
    """Hold a Postgres advisory lock for the block, or raise LockHeld at once if it is taken.

    The lock belongs to a dedicated connection, so it is released when the block ends or the
    process dies. It is scoped to the current schema, so test schemas never block each other."""
    conn = connect(autocommit=True)
    try:
        row = conn.execute("select pg_try_advisory_lock(hashtext(current_schema() || %s))",
                           (f":{name}",)).fetchone()
        if not row[0]:
            raise LockHeld(f"another process holds the '{name}' lock")
        yield
    finally:
        conn.close()


def check(conn: psycopg.Connection) -> dict:
    """Server version, which expected tables exist, and which have RLS enabled."""
    with conn.cursor() as cur:
        cur.execute("select version()")
        version = cur.fetchone()[0]
        cur.execute(
            "select c.relname, c.relrowsecurity from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = current_schema() and c.relkind = 'r'"
        )
        found = dict(cur.fetchall())
    return {
        "version": version,
        "missing": [t for t in EXPECTED_TABLES if t not in found],
        "no_rls": [t for t in EXPECTED_TABLES if t in found and not found[t]],
    }


def apply_migration(conn: psycopg.Connection, path: Path) -> None:
    """Run one migration file in a single transaction (all or nothing), without recording it."""
    with conn.transaction():
        conn.execute(path.read_text(encoding="utf-8"))


_MIGRATION_NAME = re.compile(r"(\d{14})_(.+)\.sql")


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, str, Path]]:
    """(version, name, path) for each migration, in order. Files are `<14-digit version>_<name>.sql`."""
    found: list[tuple[str, str, Path]] = []
    seen: set[str] = set()
    for path in sorted(Path(directory).glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise ValueError(f"bad migration file name {path.name!r}: expected <14-digit version>_<name>.sql")
        version, name = match.groups()
        if version in seen:
            raise ValueError(f"two migrations share the version {version}")
        seen.add(version)
        found.append((version, name, path))
    return found


def _tracking_table() -> tuple[str, sql.Composable]:
    """Applied migrations are recorded in the table the Supabase CLI uses, so `supabase db push` can take
    over later. Tests (PIM_DB_SCHEMA set) keep their own copy inside the test schema."""
    schema = os.environ.get("PIM_DB_SCHEMA") or "supabase_migrations"
    return schema, sql.Identifier(schema, "schema_migrations")


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every migration not yet recorded, in order, and return what happened.

    Each migration and its record are written in one transaction, so a failing migration leaves the
    database as it was and is retried next time. A database built before migrations were tracked (it
    already has the first schema and nothing is recorded) is baselined: the first migration is recorded
    as applied and not run again. Needs an autocommit connection."""
    if not conn.autocommit:
        raise ValueError("migrate needs an autocommit connection")
    files = migration_files(directory)
    schema, table = _tracking_table()
    conn.execute(sql.SQL("create schema if not exists {}").format(sql.Identifier(schema)))
    conn.execute(sql.SQL("create table if not exists {} "
                         "(version text not null primary key, statements text[], name text)").format(table))
    record = sql.SQL("insert into {} (version, statements, name) values (%s, %s, %s)").format(table)
    applied = {row[0] for row in conn.execute(sql.SQL("select version from {}").format(table)).fetchall()}
    messages: list[str] = []

    if not applied and files:
        missing = check(conn)["missing"]
        if not missing:
            version, name, path = files[0]
            conn.execute(record, (version, [path.read_text(encoding="utf-8")], name))
            applied.add(version)
            messages.append(f"baseline: the schema was already there from before migrations were tracked; "
                            f"recorded {path.name} as applied")
        elif len(missing) < len(EXPECTED_TABLES):
            raise RuntimeError("the database has only part of the first schema (missing: "
                               + ", ".join(missing) + "); fix it by hand before migrating")

    for version, name, path in files:
        if version in applied:
            continue
        text = path.read_text(encoding="utf-8")
        with conn.transaction():
            conn.execute(text)
            conn.execute(record, (version, [text], name))
        messages.append(f"applied {path.name}")

    known = {version for version, _, _ in files}
    messages += [f"WARNING: migration {version} is applied but has no file here" for version in sorted(applied - known)]
    return messages
