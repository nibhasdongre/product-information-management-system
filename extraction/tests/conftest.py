"""Database fixtures. Tests that need Postgres use the `env` or `conn` fixture; they are skipped
when DATABASE_URL is unset. All of them run in a throwaway schema that is dropped afterwards,
so they never touch real data. The schema name is exported as PIM_DB_SCHEMA, so code under test
and child processes connect to the same schema.

The database can be a Supabase project or a plain Postgres (as in the GitHub Actions job). On a plain
Postgres, `supabase_shim.sql` supplies the pieces of Supabase the schema relies on."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg import sql

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for path in (ROOT, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
load_dotenv(ROOT.parent / ".env")

from pipeline import db  # noqa: E402

_TRUNCATE = ("truncate product_tag, tag, product, extraction_attempts, extraction_data, page_extractions, "
             "batch_submissions, review, catalog, brand, subcategory, category restart identity cascade")


def _admin() -> psycopg.Connection:
    admin = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    has_supabase = admin.execute("select exists (select 1 from pg_namespace where nspname = 'auth')").fetchone()[0]
    if not has_supabase:  # plain Postgres: add the roles and auth objects the schema expects
        admin.execute((HERE / "supabase_shim.sql").read_text(encoding="utf-8"))
    return admin


@pytest.fixture(scope="session")
def test_schema():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    name = f"pim_test_{uuid.uuid4().hex[:10]}"
    admin = _admin()
    admin.execute(sql.SQL("create schema {}").format(sql.Identifier(name)))
    os.environ["PIM_DB_SCHEMA"] = name
    try:
        with db.connect(autocommit=True) as conn:
            db.migrate(conn)               # every run exercises the same path as `python -m pipeline db migrate`
        yield name
    finally:
        os.environ.pop("PIM_DB_SCHEMA", None)
        admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def conn(test_schema):
    with db.connect(autocommit=True) as c:
        c.execute(_TRUNCATE)
        yield c


@pytest.fixture
def env(tmp_path, conn):
    from fakes import make_env
    yield make_env(tmp_path, conn=conn)


@pytest.fixture
def fresh_schema(test_schema, monkeypatch):
    """An empty schema of its own, for tests that build the database themselves (migrations)."""
    name = f"pim_mig_{uuid.uuid4().hex[:10]}"
    admin = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    admin.execute(sql.SQL("create schema {}").format(sql.Identifier(name)))
    monkeypatch.setenv("PIM_DB_SCHEMA", name)
    with db.connect(autocommit=True) as c:
        try:
            yield c
        finally:
            c.close()
            admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(name)))
            admin.close()
