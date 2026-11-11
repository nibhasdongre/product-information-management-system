"""Benchmark maths (no database) and the full benchmark path (database, throwaway schema)."""
import csv
from argparse import Namespace

import pytest
from fakes import FakeProvider

from pipeline import batch, benchmark, submission
from pipeline.providers.base import PageExtraction


def test_wilson_interval_is_wide_for_small_samples_and_bounded():
    low, high = benchmark.wilson(3, 3)
    assert high == 1.0 and 0.4 < low < 0.5          # 3 of 3 is not "100% reliable"
    low, high = benchmark.wilson(90, 100)
    assert 0.82 < low < 0.84 and 0.94 < high < 0.96
    assert benchmark.wilson(0, 0) is None


def test_percentile_interpolates():
    assert benchmark.percentile([10, 20, 30, 40], 0.5) == 25
    assert benchmark.percentile([7], 0.95) == 7
    assert benchmark.percentile([0, 100], 0.95) == pytest.approx(95)


def _page(missed, *products):
    return {"catalog": "C", "page": "1", "missed": missed, "products": list(products)}


def _p(name_ok=True, price=None, priced=True, conf=0.9):
    return {"name_ok": name_ok, "price_check": price if name_ok else None, "priced": priced, "confidence": conf}


def test_audit_metrics_precision_recall_price_and_fabrication():
    pages = [
        _page(1, _p(price="correct"), _p(price="wrong"), _p(price="fabricated"), _p(name_ok=False, conf=0.5)),
        _page(0, _p(price="correct"), _p(price="missing", priced=False), _p(price="none", priced=False)),
    ]
    a = benchmark.audited(pages)
    assert a["products"] == 7 and a["missed"] == 1
    assert (a["name_precision"]["k"], a["name_precision"]["n"]) == (6, 7)      # one invented row
    assert (a["name_recall"]["k"], a["name_recall"]["n"]) == (6, 7)            # one product missed
    assert a["name_f1"] == pytest.approx(6 / 7)
    assert (a["missing_product_rate"]["k"], a["missing_product_rate"]["n"]) == (1, 7)
    # truth has a price for: correct x2, wrong x1, missing x1 = 4; extractor gave: correct x2, wrong, fabricated = 4
    assert (a["price_accuracy"]["k"], a["price_accuracy"]["n"]) == (2, 4)
    assert (a["price_fabrication"]["k"], a["price_fabrication"]["n"]) == (1, 4)
    assert (a["error_rate_low_confidence"]["k"], a["error_rate_low_confidence"]["n"]) == (1, 1)
    assert a["error_rate_high_confidence"]["n"] == 6


def test_audit_with_no_pages_reports_no_rates_instead_of_dividing_by_zero():
    a = benchmark.audited([])
    assert a["name_precision"]["rate"] is None and a["name_f1"] is None


class PricedProvider(FakeProvider):
    def parse_result(self, message, metrics):
        products = [{"product_name": "Mug", "price_amount": 100.0, "confidence": 0.9, "weight": "250 g"},
                    {"product_name": "Cup", "price_amount": None, "confidence": 0.6, "extraction_flags": ["price_not_found"]}]
        return PageExtraction(page_type="product", products=products, metrics=metrics)


def _run(env, provider):
    submission.submit(provider=provider, model=env.model, repo=env.repo, settings=env.settings, storage=env.storage,
                      catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)
    env.server.end(env.server.batch_ids()[0])
    batch.collect(provider, env.repo, env.model)


def _args(**kw):
    base = dict(catalogs=[], export_audit=None, audit=None, audit_size=100, seed=1, project_pages=None,
                manual_min_per_product=None, assisted_min_per_product=None, json=None)
    return Namespace(**{**base, **kw})


def test_benchmark_on_an_empty_database_does_not_crash(env, capsys):
    assert benchmark.run(env.repo, _args()) == 0
    out = capsys.readouterr().out
    assert "n/a" in out and "no audit sheet" in out


def test_export_with_nothing_collected_says_what_to_do(env, tmp_path):
    with pytest.raises(RuntimeError, match="batch sync"):
        benchmark.run(env.repo, _args(export_audit=str(tmp_path / "audit.csv")))


def test_measured_reviewed_and_audited_numbers_end_to_end(env, tmp_path):
    provider = PricedProvider(env.server)
    _run(env, provider)

    report = benchmark.build(env.repo, None, None, 1000, None, None)
    t = report["measured"]["totals"]
    assert (t["ok"], t["products"], t["no_price"], t["low_confidence"]) == (3, 6, 3, 3)
    assert report["cost"]["total_usd"] == pytest.approx(0.0045)
    assert report["cost"]["per_page_usd"] == pytest.approx(0.0015)
    assert report["cost"]["per_product_usd"] == pytest.approx(0.00075)
    assert report["cost"]["projected_usd"] == pytest.approx(1.5)
    assert report["reliability"]["success"]["rate"] == 1.0
    assert report["reliability"]["retried_pages"]["k"] == 0
    assert report["latency"]["batches"] == 1 and report["latency"]["p50_s"] >= 0
    assert report["reviewed"]["pages"] == 0

    sheet = tmp_path / "audit.csv"
    benchmark.run(env.repo, _args(export_audit=str(sheet), audit_size=3))
    with pytest.raises(FileExistsError):                       # never overwrite a sheet someone filled in
        benchmark.run(env.repo, _args(export_audit=str(sheet)))
    with open(sheet, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["catalog"] == "Test" and len({r["page"] for r in rows}) == 3

    # Auditor: Mug correct and priced right; Cup was priced on the page but we missed it; 1 product missed per page.
    for row in rows:
        row["name_correct"] = "y"
        row["price_check"] = "correct" if row["product_name"] == "Mug" else "missing"
        row["missed_on_page"] = "1" if row["position"] == "1" else ""
    with open(sheet, "w", newline="", encoding="utf-8-sig") as fh:
        out = csv.DictWriter(fh, fieldnames=benchmark.AUDIT_COLUMNS)
        out.writeheader()
        out.writerows(rows)
    pages, problems = benchmark.load_audit(sheet)
    assert problems == [] and len(pages) == 3
    a = benchmark.audited(pages)
    assert a["name_recall"]["k"] == 6 and a["name_recall"]["n"] == 9          # 6 found, 3 missed
    assert a["price_accuracy"]["k"] == 3 and a["price_accuracy"]["n"] == 6
    assert a["price_fabrication"]["k"] == 0 and a["price_fabrication"]["n"] == 3

    # A reviewer finishes level 1 after changing a price and deleting a row.
    cid = env.q("select catalog_id from catalog")[0]["catalog_id"]
    first = env.q("select extraction_id from page_extractions order by page limit 1")[0]["extraction_id"]
    env.conn.execute("insert into review (catalog_id, reviewer_1_id, level_1_at) values (%s, gen_random_uuid(), now())", (cid,))
    env.conn.execute("update product set price_amount = 120 where extraction_id = %s and position = 1", (first,))
    env.conn.execute("delete from product where extraction_id = %s and position = 2", (first,))
    rv = benchmark.reviewed(env.repo, None)
    assert (rv["pages"], rv["raw_products"], rv["matched"], rv["deleted"], rv["added"]) == (3, 6, 5, 1, 0)
    assert rv["by_field"]["price_amount"] == 1
    assert (rv["product_correction"]["k"], rv["field_correction"]["k"]) == (1, 1)

    text = benchmark.render(benchmark.build(env.repo, None, sheet, None, 6.0, 1.5))
    assert "name precision" in text and "price fabrication" in text and "75% less" in text


def test_audit_sheet_problems_are_reported_not_hidden(tmp_path):
    sheet = tmp_path / "audit.csv"
    with open(sheet, "w", newline="", encoding="utf-8-sig") as fh:
        out = csv.DictWriter(fh, fieldnames=benchmark.AUDIT_COLUMNS)
        out.writeheader()
        out.writerow({"catalog": "C", "page": "1", "position": "1", "product_name": "A", "price_amount": "",
                      "name_correct": "y", "price_check": "fabricated", "missed_on_page": "0"})   # fabricated, but no price given
        out.writerow({"catalog": "C", "page": "2", "position": "1", "product_name": "B", "price_amount": "5",
                      "name_correct": "maybe", "price_check": "correct", "missed_on_page": "0"})
        out.writerow({"catalog": "C", "page": "3", "position": "1", "product_name": "D", "price_amount": "5"})  # not audited
    pages, problems = benchmark.load_audit(sheet)
    assert len(problems) == 2 and any("contradicts" in p for p in problems) and any("y or n" in p for p in problems)
    assert [p["page"] for p in pages] == ["1", "2"]
