"""Live database checks on a throwaway schema built from the migration files (see conftest.py).
Skipped unless DATABASE_URL is set. The real project database is never touched."""
from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from pipeline import db


def test_schema_has_all_tables_with_rls(conn):
    result = db.check(conn)
    assert result["missing"] == []
    assert result["no_rls"] == []


def _page(conn) -> int:
    brand = conn.execute("insert into brand (brand_name) values ('Test Brand') returning brand_id").fetchone()[0]
    cat = conn.execute(
        "insert into catalog (catalog_name, brand_id, file_sha256, storage_path) "
        "values ('t', %s, repeat('a', 64), 'x') returning catalog_id", (brand,)).fetchone()[0]
    return conn.execute(
        "insert into page_extractions (catalog_id, page, model, status, dpi, model_dpi) "
        "values (%s, 1, 'm', 'ok', 150, 150) returning extraction_id", (cat,)).fetchone()[0]


def test_extraction_data_is_append_only(conn):
    ext = _page(conn)
    conn.execute("insert into extraction_data (extraction_id, raw_output) values (%s, '{}')", (ext,))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("update extraction_data set raw_output = '{\"a\": 1}' where extraction_id = %s", (ext,))


def test_brand_names_unique_after_normalising(conn):
    conn.execute("insert into brand (brand_name) values ('Borosil')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("insert into brand (brand_name) values ('  borosil ')")


def test_anon_role_sees_nothing(conn):
    _page(conn)
    # Supabase grants anon access to the public schema by default; give the test schema the same
    # grants, so a zero count below is down to RLS and not a missing privilege.
    schema = sql.Identifier(conn.execute("select current_schema()").fetchone()[0])
    conn.execute(sql.SQL("grant usage on schema {} to anon").format(schema))
    conn.execute(sql.SQL("grant select on all tables in schema {} to anon").format(schema))
    assert conn.execute("select count(*) from page_extractions").fetchone()[0] == 1
    with conn.transaction():
        conn.execute("set local role anon")
        assert conn.execute("select count(*) from page_extractions").fetchone()[0] == 0
