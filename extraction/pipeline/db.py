"""Postgres connection (Supabase session pooler) and schema helpers."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

EXPECTED_TABLES = (
    "brand", "category", "subcategory", "catalog", "batch_submissions", "page_extractions",
    "extraction_data", "extraction_attempts", "product", "tag", "product_tag", "review",
)
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def connect(autocommit: bool = False) -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (put it in the project-root .env).")
    return psycopg.connect(url, autocommit=autocommit, connect_timeout=15)


def check(conn: psycopg.Connection) -> dict:
    """Server version, which expected tables exist, and which have RLS enabled."""
    with conn.cursor() as cur:
        cur.execute("select version()")
        version = cur.fetchone()[0]
        cur.execute(
            "select c.relname, c.relrowsecurity from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind = 'r'"
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
