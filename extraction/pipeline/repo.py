"""All database access for the batch pipeline, scoped to one model.

Every method that changes more than one row runs in a single transaction, so a crash leaves
either all of a step or none of it. The connection must be in autocommit mode (db.connect(True)),
because transactions are opened explicitly with conn.transaction()."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pipeline.records import product_row
from pipeline.storage import Storage, pdf_key

UNRESOLVED_STATES = ("prepared", "submitting", "unknown", "needs_review")
IN_FLIGHT = ("reserved", "submitted")

_BRAND_KEY = "lower(regexp_replace(btrim(%s), '\\s+', ' ', 'g'))"
_SUBMISSION = """
    s.submission_id::text as submission_id, s.state, s.anthropic_batch_id, s.candidate_batch_id,
    s.batch_created_at, s.batch_ended_at, s.next_check_at, s.check_attempts,
    coalesce(s.review_reason, '') as review_reason, s.created_at,
    (select count(*) from page_extractions p where p.submission_id = s.submission_id) as count"""


@dataclass(frozen=True)
class PageReservation:
    catalog_id: int
    page: int
    dpi: int
    model_dpi: int
    render_s: float


def _dumps(value: Any) -> str:
    return json.dumps(value, default=float)  # Decimal weights


def brand_from_key(key: str) -> str:
    """Catalog files are named after their brand: 'Jack_Jones.pdf' -> 'Jack Jones'. Reviewers fix mistakes."""
    return key.replace("_", " ").strip() or key


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class Repo:
    def __init__(self, conn: psycopg.Connection, model: str):
        self.conn = conn
        self.model = model
        self._subcategories: dict[tuple[str, str], int] = {}

    def _all(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else []

    def fetch(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        """Read-only query helper for reports and benchmarks."""
        return self._all(query, params)

    def _one(self, query: str, params: Any = None) -> dict[str, Any] | None:
        rows = self._all(query, params)
        return rows[0] if rows else None

    # Catalogues

    def ensure_catalog(self, key: str, path: Path, storage: Storage) -> int:
        """Return the catalog_id for this PDF, registering it (and its brand) on first sight.

        A re-upload is recognised by its hash, whatever the file is called. On first sight the PDF is
        uploaded to storage before the catalog row is written, so a registered catalogue always has its
        PDF; `storage_path` holds the storage key, not a local path."""
        sha = file_sha256(path)
        row = self._one("select catalog_id from catalog where file_sha256 = %s", (sha,))
        if row:
            return row["catalog_id"]
        storage.put_file(pdf_key(sha), path, "application/pdf")
        brand_name = brand_from_key(key)
        with self.conn.transaction():
            brand = self._one(
                "insert into brand (brand_name) values (%s) "
                "on conflict on constraint brand_name_key_unique do nothing returning brand_id", (brand_name,))
            if brand is None:
                brand = self._one(f"select brand_id from brand where name_key = {_BRAND_KEY}", (brand_name,))
            created = self._one(
                "insert into catalog (catalog_name, brand_id, file_sha256, storage_path) "
                "values (%s, %s, %s, %s) returning catalog_id",
                (key, brand["brand_id"], sha, pdf_key(sha)))
        return created["catalog_id"]

    def page_statuses(self, catalog_id: int) -> dict[int, str]:
        rows = self._all("select page, status from page_extractions where catalog_id = %s and model = %s",
                         (catalog_id, self.model))
        return {row["page"]: row["status"] for row in rows}

    # Submissions

    def create_submission(self, submission_id: str, created_at: datetime,
                          pages: list[PageReservation]) -> dict[tuple[int, int], int]:
        """One transaction: the submission row (state 'prepared'), every page reserved, and each
        page's attempt row carrying its render time. Returns {(catalog_id, page): extraction_id}.

        Raises if any page is already ok or in flight; nothing is written in that case."""
        with self.conn.transaction():
            self.conn.execute(
                "insert into batch_submissions (submission_id, state, created_at, updated_at) "
                "values (%s, 'prepared', %s, %s)", (submission_id, created_at, created_at))
            reserved = self._all(
                """insert into page_extractions (catalog_id, page, model, status, dpi, model_dpi, submission_id)
                   select t.c, t.p, %s, 'reserved', t.d, t.md, %s::uuid
                   from unnest(%s::bigint[], %s::int[], %s::int[], %s::int[]) as t(c, p, d, md)
                   on conflict (catalog_id, page, model) do update
                     set status = 'reserved', dpi = excluded.dpi, model_dpi = excluded.model_dpi,
                         submission_id = excluded.submission_id, page_type = null, updated_at = now()
                     where page_extractions.status in ('error', 'released')
                   returning extraction_id, catalog_id, page""",
                (self.model, submission_id,
                 [p.catalog_id for p in pages], [p.page for p in pages],
                 [p.dpi for p in pages], [p.model_dpi for p in pages]))
            if len(reserved) != len(pages):
                raise RuntimeError("some pages are already extracted or in flight; nothing was reserved")
            ids = {(row["catalog_id"], row["page"]): row["extraction_id"] for row in reserved}
            render = {ids[(p.catalog_id, p.page)]: round(p.render_s, 3) for p in pages}
            self._insert_attempts(submission_id, render)
        return ids

    def _insert_attempts(self, submission_id: str, render_s: dict[int, float]) -> None:
        self.conn.execute(
            """insert into extraction_attempts (extraction_id, attempt_no, submission_id, render_s)
               select t.e, coalesce((select max(a.attempt_no) from extraction_attempts a
                                     where a.extraction_id = t.e), 0) + 1, %s::uuid, t.r
               from unnest(%s::bigint[], %s::numeric[]) as t(e, r)""",
            (submission_id, list(render_s), list(render_s.values())))

    def record_render_failure(self, catalog_id: int, page: int, dpi: int, error: str, render_s: float) -> None:
        """A page that could not even be rendered: an error row, so it is retried on the next submit."""
        with self.conn.transaction():
            row = self._one(
                """insert into page_extractions (catalog_id, page, model, status, dpi, model_dpi)
                   values (%s, %s, %s, 'error', %s, %s)
                   on conflict (catalog_id, page, model) do update
                     set status = 'error', submission_id = null, updated_at = now()
                     where page_extractions.status in ('error', 'released')
                   returning extraction_id""", (catalog_id, page, self.model, dpi, dpi))
            if row is None:
                return
            self.conn.execute(
                """insert into extraction_attempts (extraction_id, attempt_no, render_s, error)
                   values (%s, (select coalesce(max(attempt_no), 0) + 1 from extraction_attempts
                                where extraction_id = %s), %s, %s)""",
                (row["extraction_id"], row["extraction_id"], round(render_s, 3), error))

    def set_state(self, submission_id: str, state: str, reason: str | None = None) -> None:
        self.conn.execute(
            "update batch_submissions set state = %s, review_reason = coalesce(%s, review_reason), "
            "updated_at = now() where submission_id = %s", (state, reason, submission_id))

    def set_candidate(self, submission_id: str, batch_id: str) -> None:
        self.conn.execute("update batch_submissions set candidate_batch_id = %s, updated_at = now() "
                          "where submission_id = %s", (batch_id, submission_id))

    def schedule_search(self, submission_id: str, attempts: int, next_check_at: datetime) -> None:
        self.conn.execute(
            "update batch_submissions set check_attempts = %s, next_check_at = %s, updated_at = now() "
            "where submission_id = %s", (attempts, next_check_at, submission_id))

    def record_batch(self, submission_id: str, batch_id: str) -> None:
        """One transaction: attach the batch id, mark the submission submitted, mark its pages submitted.
        Safe to repeat."""
        with self.conn.transaction():
            self.conn.execute(
                "update batch_submissions set anthropic_batch_id = %s, state = 'submitted', "
                "next_check_at = null, updated_at = now() where submission_id = %s", (batch_id, submission_id))
            self.conn.execute(
                "update page_extractions set status = 'submitted', updated_at = now() "
                "where submission_id = %s and status = 'reserved'", (submission_id,))

    def release(self, submission_id: str) -> None:
        """One transaction: its pages become eligible again and its never-sent attempts are dropped.
        Only for a submission whose create call was never made or was refused."""
        with self.conn.transaction():
            self.conn.execute(
                "delete from extraction_attempts where submission_id = %s and error is null "
                "and api_s is null and input_tokens = 0 and output_tokens = 0", (submission_id,))
            self.conn.execute(
                "update page_extractions set status = 'released', updated_at = now() "
                "where submission_id = %s and status in ('reserved', 'submitted')", (submission_id,))
            self.conn.execute("update batch_submissions set state = 'released', updated_at = now() "
                              "where submission_id = %s", (submission_id,))

    def unresolved(self) -> list[dict[str, Any]]:
        return self._all(f"select {_SUBMISSION} from batch_submissions s where s.state = any(%s) "
                         "order by s.created_at", (list(UNRESOLVED_STATES),))

    def get_unresolved(self, submission_id: str) -> dict[str, Any] | None:
        return self._one(f"select {_SUBMISSION} from batch_submissions s "
                         "where s.submission_id::text = %s and s.state = any(%s)",
                         (submission_id, list(UNRESOLVED_STATES)))

    def known_batch_ids(self) -> set[str]:
        rows = self._all("select anthropic_batch_id from batch_submissions where anthropic_batch_id is not null")
        return {row["anthropic_batch_id"] for row in rows}

    def extraction_ids(self, submission_id: str) -> list[int]:
        rows = self._all("select extraction_id from page_extractions where submission_id = %s "
                         "order by extraction_id", (submission_id,))
        return [row["extraction_id"] for row in rows]

    # Batches awaiting collection

    def open_batches(self) -> list[dict[str, Any]]:
        return self._all(
            f"select {_SUBMISSION} from batch_submissions s where s.state = 'submitted' "
            "and exists (select 1 from page_extractions p where p.submission_id = s.submission_id "
            "and p.model = %s) order by s.created_at", (self.model,))

    def all_batches(self) -> list[dict[str, Any]]:
        return self._all(
            f"select {_SUBMISSION} from batch_submissions s "
            "where s.anthropic_batch_id is not null order by s.created_at")

    def update_timing(self, submission_id: str, created: datetime | None, ended: datetime | None) -> bool:
        """Copy Anthropic's created/ended times. Returns True if anything changed."""
        row = self._one(
            "update batch_submissions set batch_created_at = coalesce(%s, batch_created_at), "
            "batch_ended_at = coalesce(%s, batch_ended_at), updated_at = now() "
            "where submission_id = %s and (batch_created_at is distinct from coalesce(%s, batch_created_at) "
            "or batch_ended_at is distinct from coalesce(%s, batch_ended_at)) returning submission_id",
            (created, ended, submission_id, created, ended))
        return row is not None

    def submission_pages(self, submission_id: str) -> dict[int, dict[str, Any]]:
        rows = self._all(
            """select p.extraction_id, p.page, p.dpi, p.status, c.catalog_name, c.storage_path
               from page_extractions p join catalog c using (catalog_id)
               where p.submission_id = %s""", (submission_id,))
        return {row["extraction_id"]: row for row in rows}

    def complete(self, submission_id: str) -> None:
        self.conn.execute("update batch_submissions set state = 'completed', updated_at = now() "
                          "where submission_id = %s and state = 'submitted'", (submission_id,))

    def finish_page(self, extraction_id: int, submission_id: str, *, error: str | None,
                    input_tokens: int, output_tokens: int, cost_usd: float,
                    page_type: str | None = None, raw_output: dict[str, Any] | None = None,
                    image_path: str | None = None, products: list[dict[str, Any]] = ()) -> bool:
        """Status, attempt, raw output and product rows, written by one statement (so atomically,
        in one round trip). Returns False, writing nothing, if the page was already collected."""
        rows = []
        if not error:
            for position, product in enumerate(products, start=1):
                row = product_row(product, position)
                row["sub_id"] = self._subcategory_id(row.pop("category"), row.pop("subcategory"))
                rows.append(row)
        result = self._one(
            """with moved as (
                 update page_extractions set status = %(status)s, page_type = %(page_type)s, updated_at = now()
                 where extraction_id = %(eid)s and submission_id = %(sid)s::uuid and status = 'submitted'
                 returning extraction_id),
               attempt as (
                 update extraction_attempts set input_tokens = %(in_tok)s, output_tokens = %(out_tok)s,
                        cost_usd = %(cost)s, error = %(error)s
                 where extraction_id = %(eid)s and submission_id = %(sid)s::uuid
                   and exists (select 1 from moved) returning 1),
               data as (
                 insert into extraction_data (extraction_id, raw_output, image_path)
                 select %(eid)s, %(raw)s::jsonb, %(image)s from moved where %(raw)s::jsonb is not null
                 on conflict (extraction_id) do nothing returning 1),
               prods as (
                 insert into product (extraction_id, position, product_name, product_description, variant,
                        price_amount, price_currency, tax_note, weight, weight_unit, sub_id, confidence,
                        extraction_flags)
                 select %(eid)s, r.position, r.product_name, r.product_description, r.variant, r.price_amount,
                        r.price_currency, r.tax_note, r.weight, r.weight_unit, r.sub_id, r.confidence,
                        coalesce(r.extraction_flags, '{}')
                 from moved, jsonb_to_recordset(%(rows)s::jsonb) as r(
                        position int, product_name text, product_description text, variant text,
                        price_amount numeric, price_currency text, tax_note text, weight numeric,
                        weight_unit text, sub_id bigint, confidence numeric, extraction_flags text[])
                 on conflict (extraction_id, position) do nothing returning 1)
               select count(*) as moved from moved""",
            {"status": "error" if error else "ok", "page_type": page_type, "eid": extraction_id,
             "sid": submission_id, "in_tok": input_tokens, "out_tok": output_tokens,
             "cost": round(cost_usd, 6), "error": error,
             "raw": Jsonb(raw_output) if raw_output is not None and not error else None,
             "image": image_path, "rows": Jsonb(rows, dumps=_dumps)})
        return result["moved"] > 0

    def _subcategory_id(self, category: str | None, subcategory: str | None) -> int | None:
        """Both names must be printed on the page; the lookup ignores case and spacing."""
        if not category or not subcategory:
            return None
        key = (category.lower(), subcategory.lower())
        if key not in self._subcategories:
            self._subcategories[key] = self._lookup_subcategory(category, subcategory)
        return self._subcategories[key]

    def _lookup_subcategory(self, category: str, subcategory: str) -> int:
        cat = self._one("select category_id from category where lower(btrim(category_name)) = lower(%s)",
                        (category,))
        if cat is None:
            cat = self._one("insert into category (category_name) values (%s) returning category_id", (category,))
        sub = self._one("select sub_id from subcategory where category_id = %s "
                        "and lower(btrim(subcat_name)) = lower(%s)", (cat["category_id"], subcategory))
        if sub is None:
            sub = self._one("insert into subcategory (subcat_name, category_id) values (%s, %s) returning sub_id",
                            (subcategory, cat["category_id"]))
        return sub["sub_id"]

    # Reporting

    def catalog_stats(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        return self._all(
            """select c.catalog_id, c.catalog_name,
                      count(p.*) filter (where p.status = 'ok') as pages_ok,
                      count(p.*) filter (where p.status = 'error') as pages_error,
                      count(p.*) filter (where p.status = any(%s)) as pages_in_flight,
                      coalesce(a.tries, 0) as attempts, coalesce(a.input_tokens, 0) as input_tokens,
                      coalesce(a.output_tokens, 0) as output_tokens, coalesce(a.cost_usd, 0) as cost_usd,
                      coalesce(a.render_s, 0) as render_s
               from catalog c
               left join page_extractions p on p.catalog_id = c.catalog_id and p.model = %s
               left join (select pe.catalog_id, count(*) as tries, sum(x.input_tokens) as input_tokens,
                                 sum(x.output_tokens) as output_tokens, sum(x.cost_usd) as cost_usd,
                                 sum(x.render_s) as render_s
                          from extraction_attempts x join page_extractions pe using (extraction_id)
                          where pe.model = %s group by pe.catalog_id) a on a.catalog_id = c.catalog_id
               where (%s::text[] is null or c.catalog_name = any(%s))
               group by c.catalog_id, c.catalog_name, a.tries, a.input_tokens,
                        a.output_tokens, a.cost_usd, a.render_s
               order by c.catalog_name""",
            (list(IN_FLIGHT), self.model, self.model, names, names))

    def page_rows(self, catalog_id: int) -> list[dict[str, Any]]:
        return self._all(
            """select p.page, p.status, coalesce(a.tries, 0) as attempts, coalesce(a.input_tokens, 0) as input_tokens,
                      coalesce(a.output_tokens, 0) as output_tokens, coalesce(a.cost_usd, 0) as cost_usd,
                      coalesce(a.render_s, 0) as render_s, coalesce(a.api_s, 0) as api_s,
                      (select count(*) from product d where d.extraction_id = p.extraction_id) as products,
                      (select x.error from extraction_attempts x where x.extraction_id = p.extraction_id
                       order by x.attempt_no desc limit 1) as error
               from page_extractions p
               left join (select extraction_id, count(*) as tries, sum(input_tokens) as input_tokens,
                                 sum(output_tokens) as output_tokens, sum(cost_usd) as cost_usd,
                                 sum(render_s) as render_s, sum(api_s) as api_s
                          from extraction_attempts group by extraction_id) a using (extraction_id)
               where p.catalog_id = %s and p.model = %s order by p.page""", (catalog_id, self.model))
