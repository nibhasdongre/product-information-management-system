"""Postgres connection (Supabase session pooler), schema helpers, and the batch lock.

The session pooler (port 5432) is required: advisory locks and `set search_path` both live
as long as the session, which the transaction pooler (port 6543) does not guarantee."""
from __future__ import annotations

import os
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
    """Run one migration file in a single transaction (all or nothing)."""
    with conn.transaction():
        conn.execute(path.read_text(encoding="utf-8"))
