"""Live database checks. Skipped unless DATABASE_URL is set. Every write is rolled back."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT.parent / ".env")

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")

from pipeline import db  # noqa: E402


@pytest.fixture
def conn():
    with db.connect() as c:
        yield c
        c.rollback()


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
    conn.execute("set local role anon")
    assert conn.execute("select count(*) from page_extractions").fetchone()[0] == 0
