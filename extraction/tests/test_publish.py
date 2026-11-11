"""Publishing product images: cutting a crop from a PDF, and the run that does it for reviewed catalogues.
Images go to local storage here; the real bucket is checked by hand with `python -m pipeline storage check`."""
import io
import uuid

import pymupdf
import pytest
from PIL import Image
from psycopg.types.json import Jsonb

from pipeline import publish
from pipeline.storage import LocalStorage, StorageError, pdf_key

RED, BLUE, WHITE = (255, 0, 0), (0, 0, 255), (255, 255, 255)
RED_BLOCK = {"x": 0, "y": 0, "w": 0.5, "h": 0.5}
BLUE_BLOCK = {"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5}


def colour_pdf(path, rotation=0, pages=2):
    """Pages 300x600 pt, white, with a red block in the top-left quarter and a blue one in the bottom-right."""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=300, height=600)
        page.draw_rect(pymupdf.Rect(0, 0, 150, 300), color=None, fill=(1, 0, 0))
        page.draw_rect(pymupdf.Rect(150, 300, 300, 600), color=None, fill=(0, 0, 1))
        if rotation:
            page.set_rotation(rotation)
    doc.save(str(path))
    doc.close()
    return path


def decode(data):
    image = Image.open(io.BytesIO(data))
    assert image.format == "WEBP"
    return image.convert("RGB")


def near(pixel, colour, tolerance=40):
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, colour))


def centre(image):
    return image.getpixel((image.width // 2, image.height // 2))


# ---- names ---------------------------------------------------------------------------------------

def test_the_image_name_follows_the_pdf_page_and_crop():
    crop = {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4}
    key = publish.image_key("a" * 64, 3, crop)
    assert key == publish.image_key("a" * 64, 3, dict(reversed(list(crop.items()))))      # key order does not matter
    assert key.startswith("products/") and len(key) == len("products/") + 24
    others = {publish.image_key("b" * 64, 3, crop), publish.image_key("a" * 64, 4, crop),
              publish.image_key("a" * 64, 3, {**crop, "x": 0.11}), publish.image_key("a" * 64, 3, None)}
    assert key not in others and len(others) == 4


# ---- cutting -------------------------------------------------------------------------------------

def test_a_crop_is_cut_from_the_right_place_in_two_sizes(tmp_path):
    with pymupdf.open(str(colour_pdf(tmp_path / "c.pdf"))) as doc:
        card, detail = publish.cut(doc, 1, RED_BLOCK)
        assert near(centre(decode(detail)), RED) and near(centre(decode(card)), RED)
        card2, detail2 = publish.cut(doc, 2, BLUE_BLOCK)
        assert near(centre(decode(detail2)), BLUE)
    c, d = decode(card), decode(detail)
    assert d.size == (600, 1200) and max(c.size) == publish.CARD_PX            # a 150x300 pt crop at the 4x cap; card fits 480
    assert abs(c.width / c.height - d.width / d.height) < 0.02                              # same shape


def test_no_crop_means_the_whole_page(tmp_path):
    with pymupdf.open(str(colour_pdf(tmp_path / "c.pdf"))) as doc:
        _, detail = publish.cut(doc, 1, None)
    image = decode(detail)
    assert abs(image.width / image.height - 0.5) < 0.01 and max(image.size) == publish.DETAIL_PX
    assert near(image.getpixel((5, 5)), RED) and near(image.getpixel((image.width - 5, image.height - 5)), BLUE)
    assert near(image.getpixel((image.width - 5, 5)), WHITE)


def test_a_small_crop_is_not_blown_up_past_the_cap(tmp_path):
    with pymupdf.open(str(colour_pdf(tmp_path / "c.pdf"))) as doc:
        card, detail = publish.cut(doc, 1, {"x": 0, "y": 0, "w": 0.1, "h": 0.1})           # 30 x 60 pt
    assert decode(detail).size == (120, 240)                                                # 4x, the cap
    assert decode(card).size == (120, 240)                                                  # a card is never enlarged


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_a_crop_matches_the_page_as_displayed_when_the_page_is_rotated(tmp_path, rotation):
    """The review screen shows a rotated page rotated, and saves the crop against that view."""
    with pymupdf.open(str(colour_pdf(tmp_path / "c.pdf", rotation))) as doc:
        shown = doc[0].get_pixmap(matrix=pymupdf.Matrix(0.2, 0.2))                         # what a reader sees
        view = Image.frombytes("RGB", (shown.width, shown.height), shown.samples)
        for fx, fy in [(0.1, 0.1), (0.6, 0.1), (0.1, 0.6), (0.6, 0.6)]:
            expected = view.getpixel((int((fx + 0.15) * view.width), int((fy + 0.15) * view.height)))
            _, detail = publish.cut(doc, 1, {"x": fx, "y": fy, "w": 0.3, "h": 0.3})
            assert near(centre(decode(detail)), expected), (rotation, fx, fy, expected)


def test_a_crop_outside_the_page_is_an_error_not_a_blank_image(tmp_path):
    with pymupdf.open(str(colour_pdf(tmp_path / "c.pdf"))) as doc:
        with pytest.raises(ValueError, match="outside the page"):
            publish.cut(doc, 1, {"x": 1.5, "y": 0, "w": 0.2, "h": 0.2})


# ---- the run -------------------------------------------------------------------------------------

@pytest.fixture
def reviewers(conn):
    ids = []
    for name in ("pub1", "pub2"):
        uid = uuid.uuid4()
        conn.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{name}-{uid.hex[:6]}@t.example"))
        conn.execute("insert into app_user (user_id, email, role) values (%s, %s, 'reviewer')", (uid, f"{name}@t.example"))
        ids.append(uid)
    yield ids
    conn.execute("delete from review")
    conn.execute("delete from auth.users where id = any(%s)", (ids,))


class Shop:
    """Catalogues with real PDFs in private storage, an empty public image store, and some products."""

    def __init__(self, conn, tmp_path, reviewers):
        self.conn, self.tmp, self.reviewers = conn, tmp_path, reviewers
        self.pdfs, self.images = LocalStorage(tmp_path / "pdfs"), LocalStorage(tmp_path / "images")

    def catalogue(self, name, done=2):
        """done: how many review levels are finished (0, 1 or 2). Returns (catalog_id, {page: extraction_id})."""
        path = colour_pdf(self.tmp / f"{name}.pdf")
        sha = uuid.uuid4().hex * 2
        key = pdf_key(sha)
        self.pdfs.put(key, path.read_bytes(), "application/pdf")
        brand = self.conn.execute("insert into brand (brand_name) values (%s) returning brand_id", (name,)).fetchone()[0]
        cid = self.conn.execute("insert into catalog (catalog_name, brand_id, file_sha256, storage_path, page_count) "
                                "values (%s, %s, %s, %s, 2) returning catalog_id", (name, brand, sha, key)).fetchone()[0]
        extractions = {p: self.conn.execute(
            "insert into page_extractions (catalog_id, page, model, status, page_type, dpi, model_dpi) "
            "values (%s, %s, 'm', 'ok', 'product', 72, 72) returning extraction_id", (cid, p)).fetchone()[0]
            for p in (1, 2)}
        if done == 1:
            self.conn.execute("insert into review (catalog_id, reviewer_1_id, level_1_at) values (%s, %s, now())",
                              (cid, self.reviewers[0]))
        elif done == 2:
            self.conn.execute("insert into review (catalog_id, reviewer_1_id, level_1_at, reviewer_2_id, level_2_at) "
                              "values (%s, %s, now(), %s, now())", (cid, self.reviewers[0], self.reviewers[1]))
        return cid, extractions

    def sha(self, name):
        return self.conn.execute("select file_sha256 from catalog where catalog_name = %s", (name,)).fetchone()[0]

    def product(self, extraction, position, name, crop=None):
        return self.conn.execute(
            "insert into product (extraction_id, position, product_name, price_amount, crop) values (%s, %s, %s, 10, %s) "
            "returning product_id", (extraction, position, name, None if crop is None else Jsonb(crop))).fetchone()[0]

    def path(self, product_id):
        return self.conn.execute("select cropped_path from product where product_id = %s", (product_id,)).fetchone()[0]


@pytest.fixture
def shop(conn, tmp_path, reviewers):
    return Shop(conn, tmp_path, reviewers)


def test_a_run_publishes_reviewed_products_only_and_is_safe_to_repeat(env, shop):
    _, ex = shop.catalogue("Done")
    cup = shop.product(ex[1], 1, "Cup", RED_BLOCK)
    jar = shop.product(ex[2], 1, "Jar", None)                                       # no crop: the whole page
    twin = shop.product(ex[1], 2, "Twin", RED_BLOCK)                                # same crop as Cup
    nothing = shop.product(ex[2], 2, "No image", {"none": True})                    # the reviewer chose no image
    _, ex1 = shop.catalogue("HalfDone", done=1)
    half = shop.product(ex1[1], 1, "Half", RED_BLOCK)
    _, ex0 = shop.catalogue("NotReviewed", done=0)
    raw = shop.product(ex0[1], 1, "Raw", RED_BLOCK)

    report = publish.publish(env.repo, shop.pdfs, shop.images)
    assert (report.published, report.changed_meanwhile, report.failed) == (3, 0, [])
    assert shop.path(cup) == shop.path(twin) == publish.image_key(shop.sha("Done"), 1, RED_BLOCK)
    assert shop.path(jar).startswith("products/") and shop.path(jar) != shop.path(cup)
    assert shop.path(nothing) is None and shop.path(half) is None and shop.path(raw) is None
    card = decode(shop.images.get(f"{shop.path(cup)}/card.webp"))
    detail = decode(shop.images.get(f"{shop.path(cup)}/detail.webp"))
    assert near(centre(detail), RED) and max(card.size) == publish.CARD_PX
    assert near(decode(shop.images.get(f"{shop.path(jar)}/detail.webp")).getpixel((5, 5)), RED)

    again = publish.publish(env.repo, shop.pdfs, shop.images)
    assert (again.published, again.failed) == (0, [])                               # nothing left to do


def test_one_image_is_cut_once_for_products_with_the_same_crop(env, shop):
    class Counting(LocalStorage):
        puts = 0

        def put(self, key, data, content_type, cache_control=None):
            Counting.puts += 1
            super().put(key, data, content_type, cache_control)

    images = Counting(shop.tmp / "counted")
    _, ex = shop.catalogue("Done")
    for position in (1, 2, 3):
        shop.product(ex[1], position, f"P{position}", RED_BLOCK)
    assert publish.publish(env.repo, shop.pdfs, images).published == 3
    assert Counting.puts == 2                                                       # a card and a detail, once


def test_a_crop_changed_during_the_run_is_left_for_the_next_run(env, shop):
    _, ex = shop.catalogue("Done")
    cup = shop.product(ex[1], 1, "Cup", RED_BLOCK)
    assert env.repo.set_cropped_path(cup, BLUE_BLOCK, "products/stale") is False
    assert shop.path(cup) is None
    assert env.repo.set_cropped_path(cup, RED_BLOCK, "products/fresh") is True
    assert env.repo.set_cropped_path(cup, RED_BLOCK, "products/other") is False       # already published
    assert shop.path(cup) == "products/fresh"


def test_a_reviewer_changing_the_crop_gets_a_new_image_on_the_next_run(env, shop):
    _, ex = shop.catalogue("Done")
    cup = shop.product(ex[1], 1, "Cup", RED_BLOCK)
    publish.publish(env.repo, shop.pdfs, shop.images)
    old = shop.path(cup)
    env.conn.execute("update product set crop = %s, cropped_path = null where product_id = %s",
                     (Jsonb(BLUE_BLOCK), cup))                                       # what rv_finish does on a crop change
    assert publish.publish(env.repo, shop.pdfs, shop.images).published == 1
    new = shop.path(cup)
    assert new != old and new == publish.image_key(shop.sha("Done"), 1, BLUE_BLOCK)
    assert near(centre(decode(shop.images.get(f"{new}/detail.webp"))), BLUE)
    assert shop.images.get(f"{old}/detail.webp")                                     # the old file is left alone


def test_a_missing_pdf_fails_that_catalogue_and_the_others_still_publish(env, shop):
    _, exg = shop.catalogue("Good")
    good = shop.product(exg[1], 1, "Good", RED_BLOCK)
    _, exb = shop.catalogue("Bad")
    bad = shop.product(exb[1], 1, "Bad", RED_BLOCK)
    shop.pdfs.delete(env.q("select storage_path from catalog where catalog_name = 'Bad'")[0]["storage_path"])
    report = publish.publish(env.repo, shop.pdfs, shop.images)
    assert report.published == 1 and [pid for pid, _ in report.failed] == [bad]
    assert "PDF unavailable" in report.failed[0][1]
    assert shop.path(good) is not None and shop.path(bad) is None


def test_a_failed_upload_leaves_the_product_pending_and_the_next_run_finishes_it(env, shop):
    class Flaky(LocalStorage):
        fail = True

        def put(self, key, data, content_type, cache_control=None):
            if Flaky.fail and key.endswith("detail.webp"):
                raise StorageError("connection reset")
            super().put(key, data, content_type, cache_control)

    images = Flaky(shop.tmp / "flaky")
    _, ex = shop.catalogue("Done")
    cup = shop.product(ex[1], 1, "Cup", RED_BLOCK)
    jar = shop.product(ex[2], 1, "Jar", None)
    report = publish.publish(env.repo, shop.pdfs, images)
    assert report.published == 0 and sorted(pid for pid, _ in report.failed) == sorted([cup, jar])
    assert shop.path(cup) is None                                                   # never recorded without both files
    Flaky.fail = False
    assert publish.publish(env.repo, shop.pdfs, images).published == 2
    assert shop.path(cup) and shop.path(jar)


def test_a_bad_crop_in_the_database_fails_that_product_only(env, shop):
    _, ex = shop.catalogue("Done")
    bad = shop.product(ex[1], 1, "Bad", {"x": 2, "y": 0, "w": 0.5, "h": 0.5})
    good = shop.product(ex[1], 2, "Good", RED_BLOCK)
    report = publish.publish(env.repo, shop.pdfs, shop.images)
    assert report.published == 1 and [pid for pid, _ in report.failed] == [bad]
    assert shop.path(good) is not None and shop.path(bad) is None


def test_nothing_to_publish_is_not_an_error(env, shop):
    report = publish.publish(env.repo, shop.pdfs, shop.images)
    assert (report.published, report.changed_meanwhile, report.failed) == (0, 0, [])
