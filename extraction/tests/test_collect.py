"""Collecting a finished batch: what lands in the database, and that it is safe to repeat."""
from decimal import Decimal

from fakes import FakeProvider

from pipeline import batch, submission
from pipeline.providers.base import PageExtraction
from pipeline.report import catalog_rows


class RichProvider(FakeProvider):
    """Returns realistic products, including a printed category and a weight."""

    def parse_result(self, message, metrics):
        products = [
            {"product_name": "Steel Bottle", "product_description": "1 L flask", "price_amount": 499.0,
             "price_currency": "INR", "tax_note": "+GST", "weight": "247 gms", "variant_label": "Large",
             "category": "Drinkware", "subcategory": "Bottles", "confidence": 0.93,
             "extraction_flags": [], "colours": ["red"], "eco_friendly": True},
            {"product_name": "Steel Mug", "price_amount": None, "weight": "about a pound", "category": "drinkware",
             "subcategory": "bottles", "confidence": 7, "extraction_flags": ["price_not_found"]},
        ]
        return PageExtraction(page_type="product", products=products, metrics=metrics)


def _submit_and_end(env, provider, results_ids=None):
    submission.submit(provider=provider, model=env.model, repo=env.repo, settings=env.settings,
                      catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)
    batch_id = env.server.batch_ids()[0]
    env.server.end(batch_id, results_ids=results_ids)
    return batch_id


def _collect(env, provider):
    return batch.collect(provider, env.repo, env.model, env.settings)


def test_catalog_and_brand_are_registered_from_the_file_name(env):
    _submit_and_end(env, FakeProvider(env.server))
    row = env.q("select c.catalog_name, b.brand_name from catalog c join brand b using (brand_id)")[0]
    assert (row["catalog_name"], row["brand_name"]) == ("Test", "Test")


def test_collect_writes_raw_output_products_and_costs(env):
    provider = RichProvider(env.server)
    _submit_and_end(env, provider)
    assert len(_collect(env, provider)) == 1

    assert env.page_statuses() == ["ok"] * 3
    raw = env.q("select raw_output from extraction_data limit 1")[0]["raw_output"]
    assert raw["page_type"] == "product" and raw["products"][0]["colours"] == ["red"]

    first = env.q("select * from product where product_name = 'Steel Bottle' limit 1")[0]
    assert (first["variant"], first["weight_unit"], first["price_currency"]) == ("Large", "gms", "INR")
    assert first["weight"] == Decimal("247") and first["price_amount"] == Decimal("499.00")
    second = env.q("select * from product where product_name = 'Steel Mug' limit 1")[0]
    assert second["weight"] is None and second["price_amount"] is None
    assert second["confidence"] is None
    assert "confidence_out_of_range" in second["extraction_flags"] and "price_not_found" in second["extraction_flags"]

    # "Drinkware/Bottles" and "drinkware/bottles" are one category and one subcategory.
    assert env.q("select count(*) as n from category")[0]["n"] == 1
    assert env.q("select count(*) as n from subcategory")[0]["n"] == 1
    assert first["sub_id"] == second["sub_id"] is not None

    # 3 pages x (1000 in x $2 + 100 out x $10) / 1M x 0.5 batch discount
    stats = catalog_rows(env.repo.catalog_stats())[0]
    assert (stats["pages_ok"], stats["attempts"]) == (3, 3)
    assert stats["input_tokens"] == 3000 and stats["output_tokens"] == 300
    assert abs(float(stats["cost_usd"]) - 0.0045) < 1e-9


def test_collecting_twice_changes_nothing(env):
    provider = FakeProvider(env.server)
    batch_id = _submit_and_end(env, provider)
    assert _collect(env, provider) == [batch_id]
    products = env.q("select count(*) as n from product")[0]["n"]

    assert _collect(env, provider) == []
    assert env.q("select count(*) as n from product")[0]["n"] == products == 3
    assert env.q("select count(*) as n from extraction_attempts")[0]["n"] == 3


def test_interrupted_collect_resumes_without_duplicates(env, monkeypatch):
    provider = FakeProvider(env.server)
    _submit_and_end(env, provider)
    real = env.repo.finish_page
    calls = {"n": 0}

    def crash_on_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash while collecting")
        return real(*args, **kwargs)

    monkeypatch.setattr(env.repo, "finish_page", crash_on_second)
    try:
        _collect(env, provider)
    except RuntimeError:
        pass
    monkeypatch.undo()
    assert sorted(env.page_statuses()) == ["ok", "submitted", "submitted"]
    assert env.submissions()[0]["state"] == "submitted"

    assert len(_collect(env, provider)) == 1
    assert env.page_statuses() == ["ok"] * 3
    assert env.q("select count(*) as n from product")[0]["n"] == 3
    assert env.submissions()[0]["state"] == "completed"


def test_page_missing_from_the_results_becomes_an_error_and_is_retried_with_history(env):
    provider = FakeProvider(env.server)
    submission.submit(provider=provider, model=env.model, repo=env.repo, settings=env.settings,
                      catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)
    batch_id = env.server.batch_ids()[0]
    wanted = env.server.batch(batch_id)["custom_ids"][:2]  # the batch returns only two of three results
    env.server.end(batch_id, results_ids=wanted)
    _collect(env, provider)

    assert sorted(env.page_statuses()) == ["error", "ok", "ok"]
    failed = env.q("select extraction_id, error from extraction_attempts where error is not null")
    assert len(failed) == 1 and "no result" in failed[0]["error"]

    report = submission.submit(provider=provider, model=env.model, repo=env.repo, settings=env.settings,
                               catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)
    assert report.submitted == 1 and report.skipped == 2
    attempts = env.q("select attempt_no from extraction_attempts where extraction_id = %s order by attempt_no",
                     (failed[0]["extraction_id"],))
    assert [a["attempt_no"] for a in attempts] == [1, 2]


def test_a_renamed_copy_of_a_registered_pdf_is_the_same_catalog(env):
    _submit_and_end(env, FakeProvider(env.server))
    copy = env.catalog_dir / "Other_Name.pdf"
    copy.write_bytes((env.catalog_dir / "Test.pdf").read_bytes())
    first = env.repo.ensure_catalog("Test", env.catalog_dir / "Test.pdf")
    assert env.repo.ensure_catalog("Other_Name", copy) == first
    assert env.q("select count(*) as n from catalog")[0]["n"] == 1
