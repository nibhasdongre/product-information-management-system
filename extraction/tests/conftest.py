"""Database fixtures. Tests that need Postgres use the `env` or `conn` fixture; they are skipped
when DATABASE_URL is unset. All of them run in a throwaway schema that is dropped afterwards,
so they never touch real data. The schema name is exported as PIM_DB_SCHEMA, so code under test
and child processes connect to the same schema."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
load_dotenv(ROOT.parent / ".env")

from pipeline import db  # noqa: E402

_TRUNCATE = ("truncate product_tag, tag, product, extraction_attempts, extraction_data, page_extractions, "
             "batch_submissions, review, catalog, brand, subcategory, category restart identity cascade")


@pytest.fixture(scope="session")
def test_schema():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    name = f"pim_test_{uuid.uuid4().hex[:10]}"
    admin = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    admin.execute(sql.SQL("create schema {}").format(sql.Identifier(name)))
    os.environ["PIM_DB_SCHEMA"] = name
    try:
        with db.connect() as conn:
            for migration in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
                db.apply_migration(conn, migration)
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
